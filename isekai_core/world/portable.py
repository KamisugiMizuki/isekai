"""实例导入导出：单一文件容器、版本检查与原子导入。

容器是未加密的 JSON 单文件（WORLD_SETTING_SPEC §7）：
  {container: 清单, setting: 锁定设定快照, runtime: 对话等运行部分, integrity: 指纹}
不含任何凭据、通道绑定、投递回执或去重作废记录；导入总是创建**新实例**并默认冻结，
原实例（若同名）不受影响，名称冲突按 §7.4 自动追加序号。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import time
from pathlib import Path
from typing import Any

from ..store import Store, _relabel_payload
from ..version import (
    APP_VERSION,
    CAPABILITIES,
    CONTAINER_FORMAT,
    CONTAINER_VERSION,
    DATA_FORMAT_VERSION,
    RULES_VERSION,
)
from .cards import validate_assembly
from .instances import InstanceError, create_instance
from .package import PackageError, clone_package, read_json_file
from .validate import validate_package


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_container(store: Store, instance_id: str) -> dict[str, Any]:
    row = store.instance_get(instance_id)
    if row is None:
        raise InstanceError(f"实例不存在：{instance_id}")
    setting = json.loads(row["setting"])
    sessions = store.instance_sessions(instance_id)
    messages = store.instance_messages(instance_id)
    timelines = store.timeline_list(instance_id)
    commits = store.commit_list(instance_id)
    runtime_state: dict[str, dict[str, Any]] = {}
    for item in timelines:
        clock = store.clock_get(item["id"])
        watermark = int(clock["processed_world"]) if clock else int(row["moment"])
        from ..runtime import versioning  # 局部导入：版本层在 runtime 层，顶层导入会成环

        runtime_state[item["id"]] = {
            "watermark": watermark,
            # 有效倍率取「待生效命令折进后」的值：只读 clock 行会把陈旧倍率带进导出件（§7.1）
            "rate": versioning.recorded_rate(store, item["id"]),
            **store.runtime_dump(instance_id, item["id"], watermark=watermark),
        }
    payload = {
        "setting": setting,
        "runtime": {
            "sessions": [
                {
                    "id": item["id"],
                    "timeline_id": item["timeline_id"],
                    "character_id": item["character_id"],
                    "created_at": item["created_at"],
                }
                for item in sessions
            ],
            "messages": messages,
            "timelines": [
                {
                    "id": item["id"],
                    "name": item["name"],
                    # 便携包不带本机控制状态（§7.1 / 附录 B）：激活集合与当前视图留在本机
                    "state": "frozen",
                    "source_commit": item["source_commit"],
                    "created_at": item["created_at"],
                }
                for item in timelines
            ],
            "commits": [
                {
                    "id": item["id"],
                    "timeline_id": item["timeline_id"],
                    "kind": item["kind"],
                    "moment": item["moment"],
                    "note": item["note"],
                    "created_at": item["created_at"],
                    # 提交闭包（§7.1）：没有快照，导入件就回滚不了、也分不出有历史的新线
                    "snapshot": store.commit_snapshot_get(str(item["id"])),
                }
                for item in commits
            ],
            "seed": row["seed"],
            "moment": row["moment"],
            # 角色状态按已完成水位导出；不导出待生效倍率命令、投递回执与通道绑定（§2.6 / §2.3.6）
            "state": runtime_state,
        },
    }
    return {
        "container": {
            "format": CONTAINER_FORMAT,
            "container_version": CONTAINER_VERSION,
            "app_version": APP_VERSION,
            "data_format": row["data_format"],
            "rules_version": row["rules_version"],
            "exported_at": time.time(),
            "name": row["name"],
            "original_name": row["original_name"],
            "package_id": row["package_id"],
            "moment": row["moment"],
            "capabilities": list(CAPABILITIES),
            "counts": {
                "sessions": len(sessions),
                "messages": len(messages),
                "timelines": len(timelines),
                "commits": len(commits),
            },
        },
        "setting": payload["setting"],
        "runtime": payload["runtime"],
        "integrity": {"algorithm": "sha256", "digest": _digest(payload)},
    }


def write_export(store: Store, instance_id: str, path: str | Path) -> dict[str, Any]:
    """先写临时文件、完整校验后再原子发布（§7.1）：中途失败不留下伪装成功的包。"""
    container = build_container(store, instance_id)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(container, ensure_ascii=False, indent=2)
    verify_integrity(container)
    handle = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(target.parent), delete=False, suffix=".tmp"
    )
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    finally:
        handle.close()
    os.replace(handle.name, target)
    return container["container"]


# 容器件上限：整库导出（含全量快照）比世界包大得多，单独给一道更宽但仍有界的闸（§7.3 / §7.5）
MAX_CONTAINER_BYTES = 256 << 20  # 256 MiB


def read_container(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    try:
        raw = read_json_file(file, what="导入文件", limit=MAX_CONTAINER_BYTES)
    except PackageError as exc:
        raise InstanceError(str(exc)) from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("container"), dict):
        raise InstanceError("导入文件缺少 container 段")
    return raw


def check_compatibility(container: dict[str, Any]) -> tuple[str, str]:
    """返回 (状态, 原因)；状态为 compatible / incompatible。不修改任何数据。"""
    head = container.get("container") or {}
    if head.get("format") != CONTAINER_FORMAT:
        return "incompatible", f"不是本应用的导出件（format={head.get('format')!r}）"
    version = str(head.get("container_version") or "")
    ours_major, incoming_major = CONTAINER_VERSION.split(".")[0], version.split(".")[0]
    if not version or incoming_major != ours_major:
        return "incompatible", f"容器格式主版本不兼容（导出件 {version or '未知'}，本端 {CONTAINER_VERSION}）；无转换工具时不导入"
    data_format = str(head.get("data_format") or "")
    if data_format.split(".")[0] != DATA_FORMAT_VERSION.split(".")[0]:
        return "incompatible", f"数据格式主版本不兼容（导出件 {data_format or '未知'}，本端 {DATA_FORMAT_VERSION}）；无转换工具时不导入"
    required = head.get("capabilities") or []
    missing = [item for item in required if item not in CAPABILITIES]
    if missing:
        return "incompatible", "导出件要求本端尚不具备的能力：" + "、".join(sorted(missing))
    return "compatible", ""


def verify_integrity(container: dict[str, Any]) -> None:
    integrity = container.get("integrity")
    if not isinstance(integrity, dict) or not integrity.get("digest"):
        raise InstanceError("导入件缺少完整性指纹")
    payload = {"setting": container.get("setting"), "runtime": container.get("runtime")}
    if _digest(payload) != integrity.get("digest"):
        raise InstanceError("导出件完整性校验失败（文件被改动或不完整），未导入")


def import_instance(store: Store, container: dict[str, Any], *, display_name: str | None = None) -> dict[str, Any]:
    """导入 = 校验 → 创建新实例（默认冻结）→ 恢复对话；任一步失败不留半个实例。"""
    status, reason = check_compatibility(container)
    if status != "compatible":
        raise InstanceError(reason)
    verify_integrity(container)

    setting = container.get("setting")
    if not isinstance(setting, dict) or not isinstance(setting.get("world_package"), dict):
        raise InstanceError("导入件缺少锁定的世界包快照")
    package = setting["world_package"]
    cards = setting.get("cards") or []
    moment = int(package.get("calendar", {}).get("initial_moment") or 0)

    errors = validate_package(package) + validate_assembly(package, cards, moment=moment)
    if errors:
        raise InstanceError(["导入件的锁定设定未通过校验："] + errors)

    runtime = container.get("runtime") or {}
    timelines, commits, timeline_map, commit_map = _prepare_graph(runtime, moment)
    row = create_instance(
        store,
        package,
        cards,
        display_name=display_name or str(setting.get("original_name") or ""),
        imported=True,
        seed=str(runtime.get("seed") or "") or None,
        extra_setting={"imported_from": {"exported_at": (container.get("container") or {}).get("exported_at")}},
        timelines=timelines,
        commits=commits,
    )
    try:
        session_map = _restore_sessions(store, row["id"], runtime, timeline_map)
        _restore_runtime_state(store, row["id"], runtime, timeline_map)
        _restore_commit_snapshots(store, row["id"], runtime, commit_map, timeline_map, session_map)
    except Exception:
        store.instance_delete(row["id"])
        raise
    return row


def _restore_runtime_state(
    store: Store, instance_id: str, runtime: dict[str, Any], timeline_map: dict[str, str]
) -> int:
    """按导出水位恢复角色状态；时钟冻结在该水位上，不恢复待生效倍率命令（§2.6）。"""
    state = runtime.get("state") or {}
    if not isinstance(state, dict):
        return 0
    loaded = 0
    for old_id, payload in state.items():
        new_id = timeline_map.get(str(old_id))
        if not new_id or not isinstance(payload, dict):
            continue
        watermark = int(payload.get("watermark") or 0)
        rows = {
            "watermark": watermark,
            "characters": _remap_rows(payload.get("characters"), instance_id, new_id),
            "units": _remap_rows(payload.get("units"), instance_id, new_id),
            "plans": _remap_rows(payload.get("plans"), instance_id, new_id),
            "experiences": _remap_rows(payload.get("experiences"), instance_id, new_id),
            "events": _remap_rows(payload.get("events"), instance_id, new_id),
            "claims": _remap_rows(payload.get("claims"), instance_id, new_id),
            "knowledge": _remap_rows(payload.get("knowledge"), instance_id, new_id),
            "reactions": _remap_rows(payload.get("reactions"), instance_id, new_id),
            "effects": _remap_rows(payload.get("effects"), instance_id, new_id),
            "intents": _remap_rows(payload.get("intents"), instance_id, new_id),
            "environment": _remap_rows(payload.get("environment"), instance_id, new_id),
            "institution": _remap_rows(payload.get("institution"), instance_id, new_id),
            "customs": _remap_rows(payload.get("customs"), instance_id, new_id),
            "disclosure": _remap_rows(payload.get("disclosure"), instance_id, new_id),
            "memories": _remap_rows(payload.get("memories"), instance_id, new_id),
            "memory_tasks": _remap_rows(payload.get("memory_tasks"), instance_id, new_id),
            "citations": [dict(row) for row in (payload.get("citations") or []) if isinstance(row, dict)],
            # 战役运行时（TRPG_CAMPAIGN_RUNTIME_SPEC §十七）：战役、场景、行动、选择、
            # 规则状态附件与联合提交账本都随件；campaign_id 在实例作用域内唯一，不重铸。
            "trpg_campaigns": _remap_rows(payload.get("trpg_campaigns"), instance_id, new_id),
            "trpg_scenes": _remap_rows(payload.get("trpg_scenes"), instance_id, new_id),
            "trpg_actions": _remap_rows(payload.get("trpg_actions"), instance_id, new_id),
            "trpg_choices": _remap_rows(payload.get("trpg_choices"), instance_id, new_id),
            "trpg_rule_states": _remap_rows(payload.get("trpg_rule_states"), instance_id, new_id),
            "trpg_commits": _remap_rows(payload.get("trpg_commits"), instance_id, new_id),
        }
        loaded += store.runtime_load(instance_id, new_id, rows)
        store.clock_put(
            {
                "timeline_id": new_id,
                "base_real": time.time(),
                "base_world": watermark,
                "rate": max(1, int(payload.get("rate") or 1)),  # 不静默改写；超上限由激活时确认（§2.4）
                "high_water_real": time.time(),
                "anchor_real": time.time(),
                "processed_world": watermark,
                "generation": 1,
                "catching_up": 0,
                "limited": 0,
            }
        )
    return loaded


def _remap_rows(rows: Any, instance_id: str, timeline_id: str) -> list[dict[str, Any]]:
    """把快照行里的本地标识换成新实例 / 新时间线（角色标识来自卡片，保持不变）。"""
    out: list[dict[str, Any]] = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        out.append(
            {
                **item,
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "id": f"{str(item.get('id') or 'row').split('-')[0]}-{secrets.token_hex(6)}",
            }
        )
    return out


def _prepare_graph(
    runtime: dict[str, Any], moment: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, str], dict[str, str]]:
    """时间线与提交行重新映射本地标识；导入线一律冻结（§7.3）。"""
    timeline_map: dict[str, str] = {}
    commit_map: dict[str, str] = {}
    source_timelines = [item for item in (runtime.get("timelines") or []) if isinstance(item, dict)]
    source_commits = [item for item in (runtime.get("commits") or []) if isinstance(item, dict)]
    if not source_timelines:
        source_timelines = [{"id": "tl-legacy", "name": "初始时间线", "created_at": time.time()}]
    for item in source_timelines:
        timeline_map[str(item.get("id"))] = f"tl-{secrets.token_hex(4)}"
    for item in source_commits:
        commit_map[str(item.get("id"))] = f"cm-{secrets.token_hex(6)}"
    timelines = [
        {
            "id": timeline_map[str(item.get("id"))],
            "name": str(item.get("name") or "时间线"),
            "state": "frozen",
            "source_commit": commit_map.get(str(item.get("source_commit"))),
            "created_at": float(item.get("created_at") or time.time()),
        }
        for item in source_timelines
    ]
    fallback = timelines[0]["id"]
    commits = [
        {
            "id": commit_map[str(item.get("id"))],
            "timeline_id": timeline_map.get(str(item.get("timeline_id")), fallback),
            "kind": str(item.get("kind") or "import"),
            "moment": int(item.get("moment") if isinstance(item.get("moment"), int) else moment),
            "note": str(item.get("note") or ""),
            "created_at": float(item.get("created_at") or time.time()),
        }
        for item in source_commits
    ]
    if not commits:
        commits = [
            {
                "id": f"cm-{secrets.token_hex(6)}",
                "timeline_id": fallback,
                "kind": "import",
                "moment": moment,
                "note": "导入创建",
                "created_at": time.time(),
            }
        ]
    return timelines, commits, timeline_map, commit_map


def _restore_commit_snapshots(
    store: Store,
    instance_id: str,
    runtime: dict[str, Any],
    commit_map: dict[str, str],
    timeline_map: dict[str, str],
    session_map: dict[str, str],
) -> int:
    """提交快照随件恢复（§7.1 提交闭包）：导入件的回滚 / 分叉不丢历史。

    快照内部的本地标识要按新实例改写：运行载荷走 store 的载荷改写，对话行的会话标识走会话映射；
    映射不到的对话行直接丢掉（不往新库里塞指向不存在会话的行）。
    """
    written = 0
    for item in runtime.get("commits") or []:
        if not isinstance(item, dict):
            continue
        old_commit = str(item.get("id") or "")
        new_commit = commit_map.get(old_commit)
        new_timeline = timeline_map.get(str(item.get("timeline_id") or ""))
        snapshot = item.get("snapshot")
        if not new_commit or not new_timeline or not isinstance(snapshot, dict):
            continue
        rows = dict(snapshot)
        rows["runtime"] = _relabel_payload(
            dict(snapshot.get("runtime") or {}), instance_id, new_timeline
        )
        dialog: list[dict[str, Any]] = []
        for row in snapshot.get("dialog") or []:
            if not isinstance(row, dict):
                continue
            session_id = session_map.get(str(row.get("session_id") or ""))
            if session_id is None:
                continue
            dialog.append({**row, "session_id": session_id})
        rows["dialog"] = dialog
        store.commit_snapshot_put(new_commit, instance_id, json.dumps(rows, ensure_ascii=False))
        written += 1
    return written


def _restore_sessions(
    store: Store, instance_id: str, runtime: dict[str, Any], timeline_map: dict[str, str]
) -> dict[str, str]:
    """恢复会话与对话原文；时间线标识重新映射，投递与绑定不回传（§7.1）。"""
    timelines = store.timeline_list(instance_id)
    if not timelines:
        raise InstanceError("实例缺少初始时间线")
    fallback = timelines[0]["id"]
    sessions = runtime.get("sessions") or []
    messages = runtime.get("messages") or []
    id_map: dict[str, str] = {}
    for item in sessions:
        if not isinstance(item, dict):
            continue
        character_id = str(item.get("character_id") or "character")
        timeline_id = timeline_map.get(str(item.get("timeline_id")), fallback)
        created = store.session_ensure(instance_id, timeline_id, character_id)
        id_map[str(item.get("id"))] = str(created["id"])
    grouped: dict[str, list[dict[str, Any]]] = {}
    for item in messages:
        if not isinstance(item, dict):
            continue
        new_session = id_map.get(str(item.get("session_id") or ""))
        if new_session is None:
            continue
        grouped.setdefault(new_session, []).append(item)
    for session_id, items in grouped.items():
        store.instance_import_messages(session_id, items)
    return id_map


__all__ = [
    "PackageError",
    "build_container",
    "check_compatibility",
    "import_instance",
    "read_container",
    "verify_integrity",
    "write_export",
]
