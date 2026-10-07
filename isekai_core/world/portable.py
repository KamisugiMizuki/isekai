"""实例导入导出：单一文件容器、版本检查与原子导入。

容器是未加密的 JSON 单文件（WORLD_SETTING_SPEC §7）：
  {container: 清单, setting: 锁定设定快照, runtime: 对话等运行部分, integrity: 指纹}
不含任何凭据、通道绑定、投递回执或去重作废记录；导入总是创建**新实例**并默认冻结，
原实例（若同名）不受影响，名称冲突按 §7.4 自动追加序号。
"""

from __future__ import annotations

import hashlib
import json
import secrets
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
from .package import PackageError, atomic_write_text, clone_package, read_json_file
from .validate import validate_package


def _digest(payload: dict[str, Any]) -> str:
    """**旧口径**：整容器规范化序列化的 sha256。

    只用于读回旧导出件（它们只带这一档指纹）；新导出一律走 `_section_digest`
    （分节原始字节，C-10 / §7.1），不再为了算摘要把整包 sort_keys 重排一遍。
    """
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _section_digest(value: Any) -> str:
    """分节的原始字节摘要（§7.1 / 附录②，C-10）：只序列化这一节，不做整包规范化。"""
    raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _section_digests(container: dict[str, Any]) -> dict[str, str]:
    """`setting` / `runtime` 各算一档，允许分节独立校验。"""
    return {
        "setting": _section_digest(container.get("setting")),
        "runtime": _section_digest(container.get("runtime")),
    }


def _writing_payload(store: Store, instance_id: str, timelines: list[dict[str, Any]]) -> dict[str, Any]:
    """编剧层随件（WRITING_ASSISTANT_SPEC §4.1 / §八）：定义是作者资产、状态与候选按线。

    导出按**引用闭包**取大纲定义：只带本实例各线绑定 / 候选引用到的那几份，不把整个
    作者资产库塞进实例副本。定义 id 不重铸（导入时按 id 幂等落库，见 `_restore_writing`）。
    """
    states: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    outline_ids: set[str] = set()
    for item in timelines:
        timeline_id = str(item["id"])
        saved_states = store.wa_state_list(instance_id, timeline_id)
        saved_candidates = store.wa_candidate_list(instance_id, timeline_id)
        states.extend(saved_states)
        candidates.extend(saved_candidates)
        for row in (*saved_states, *saved_candidates):
            outline_id = str(row.get("outline_id") or "")
            if outline_id:
                outline_ids.add(outline_id)
    outlines: list[dict[str, Any]] = []
    for outline_id in sorted(outline_ids):
        row = store.wa_outline_get(outline_id)
        if row is not None:
            outlines.append(row)
    return {"outlines": outlines, "states": states, "candidates": candidates}


def _commit_entry(store: Store, item: dict[str, Any]) -> dict[str, Any]:
    """容器里的一条提交：管理元数据 + **存储形态**的提交快照与它的 base（§7.1）。

    快照按库内的存储形态随件（`kind` / `base` / `body` 的 delta 链，或早期库里的裸物化正文），
    不调 `commit_snapshot_get()` 逐条物化成全量——那会把 O(历史 + 变更) 撑成
    O(提交数 × 历史)（P0-5）。
    """
    from ..runtime import versioning  # 局部导入：版本层在 runtime 层，顶层导入会成环

    commit_id = str(item["id"])
    raw = store.commit_snapshot_stored(commit_id)
    snapshot = versioning.parse_snapshot(raw) if raw else None
    return {
        "id": item["id"],
        "timeline_id": item["timeline_id"],
        "kind": item["kind"],
        "moment": item["moment"],
        "note": item["note"],
        "created_at": item["created_at"],
        # 提交闭包（§7.1）：没有快照，导入件就回滚不了、也分不出有历史的新线
        "snapshot": snapshot,
        # delta 链的依赖（与库内 `commit_snapshot.base_commit_id` 同一口径，取值取正文里的 base——
        # 库里读列与读正文走的是同一条链，见 `Store.commit_snapshot_get`）：导入按它重建链（§7.3）
        "base_commit_id": versioning.snapshot_kind(raw)[1] if raw else "",
    }


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
            "commits": [_commit_entry(store, item) for item in commits],
            "seed": row["seed"],
            "moment": row["moment"],
            # 角色状态按已完成水位导出；不导出待生效倍率命令、投递回执与通道绑定（§2.6 / §2.3.6）
            "state": runtime_state,
            # 编剧层：大纲定义（引用闭包）+ 各线绑定状态与候选 / 决定，随件走完整性摘要
            "writing": _writing_payload(store, instance_id, timelines),
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
        # 摘要按分节原始字节各算一档（§7.1 / 附录②，C-10）：构建同趟产出，导出不再自我复验
        "integrity": {"algorithm": "sha256", "sections": _section_digests(payload)},
    }


def write_export(store: Store, instance_id: str, path: str | Path) -> dict[str, Any]:
    """先写临时文件、原子发布（§7.1）：中途失败不留下伪装成功的包、也不破坏先前导出件。

    导出**不自我复验**：`build_container` 已经同趟算好分节摘要，不再把自己刚构建的容器
    整体重新规范化序列化一遍、重算同一个 digest（P1-13）。落盘复用包 / 卡片那一条
    原子写助手（同目录临时文件 → `fsync` → `os.replace`，失败清理临时文件）。
    """
    container = build_container(store, instance_id)
    target = Path(path)
    atomic_write_text(target, json.dumps(container, ensure_ascii=False, indent=2))
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
    """校验清单里的完整性摘要，只回答「文件是否完整 / 被改动」（§7.5：不冒充来源认证）。

    两套口径都能读：

    - **分节原始字节**（新导出件）：`setting` / `runtime` 各一档，各自可独立校验
      （§7.1 / 附录②，C-10）——复算不必把整容器规范化重序列化；
    - **整包规范化序列化**（旧导出件）：只有 `digest` 一档时照旧验（§7.3 兼容旧容器）。

    同一份清单同时带两档时，任一档对上即通过：两档都是损坏检测，不是来源认证。
    """
    integrity = container.get("integrity")
    if not isinstance(integrity, dict):
        raise InstanceError("导入件缺少完整性指纹")
    sections = integrity.get("sections") if isinstance(integrity.get("sections"), dict) else {}
    legacy = str(integrity.get("digest") or "")
    if sections:
        current = _section_digests(container)
        mismatched = [name for name, value in current.items() if str(sections.get(name) or "") != value]
        if not mismatched:
            return
        if legacy:
            payload = {"setting": container.get("setting"), "runtime": container.get("runtime")}
            if _digest(payload) == legacy:
                return
        raise InstanceError("导出件完整性校验失败（文件被改动或不完整），未导入")
    if not legacy:
        raise InstanceError("导入件缺少完整性指纹")
    payload = {"setting": container.get("setting"), "runtime": container.get("runtime")}
    if _digest(payload) != legacy:
        raise InstanceError("导出件完整性校验失败（文件被改动或不完整），未导入")


def import_instance(store: Store, container: dict[str, Any], *, display_name: str | None = None) -> dict[str, Any]:
    """导入 = 校验 → 创建新实例（默认冻结）→ 恢复对话；任一步失败不留半个实例。

    「一次过」（§7.3 / P1-13 / P1-14）：容器在这里校验**一轮**（结构 + 引用闭包 + 摘要），
    创建实例时走「已校验」快路径（`validated=True`），不再把同一套逐卡校验与包级 id 集
    重建跑第二轮；提交快照也不在导入期先合成为全量再由容器内容覆盖——
    只由容器内容按存储形态一次性写回。
    """
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

    # 隔离暂存位置已完成的那一轮校验：这里只复核锁定设定本身（不重复两轮）
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
        validated=True,             # 上面那一轮就是全部校验；创建期不再重跑
        materialize_snapshots=False,  # 快照由容器内容写入（不先合成再覆盖）
    )
    try:
        session_map = _restore_sessions(store, row["id"], runtime, timeline_map)
        _restore_runtime_state(store, row["id"], runtime, timeline_map)
        _restore_commit_snapshots(store, row["id"], runtime, commit_map, timeline_map, session_map)
        kept = _restore_writing(store, row["id"], runtime.get("writing"), timeline_map)
        if kept:
            # 本机已有同名大纲且内容不同：保留本机那份并如实回报，不静默替换作者资产（§4.1）
            row["writing_kept_outlines"] = kept
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


def _restore_writing(
    store: Store, instance_id: str, writing: Any, timeline_map: dict[str, str]
) -> list[str]:
    """编剧层随件导回：大纲定义按 id 幂等落库，绑定状态与候选写回新实例 / 新线。

    - 定义是**作者资产**：id 不重铸（否则同一份大纲会在导入端变成新资产，跨线复用失效）；
      本机已有同名大纲则**保留本机那份**，内容不同才回报到 `writing_kept_outlines`；
    - 状态与候选按实例 + 时间线走：时间线用导入端的新 id，映射不到的行直接丢（不塞悬空行）。
    """
    if not isinstance(writing, dict):
        return []
    kept: list[str] = []
    for row in writing.get("outlines") or []:
        if not isinstance(row, dict):
            continue
        outline_id = str(row.get("id") or "")
        if not outline_id:
            continue
        existing = store.wa_outline_get(outline_id)
        if existing is not None:
            if str(existing.get("payload") or "") != str(row.get("payload") or ""):
                kept.append(outline_id)
            continue
        store.wa_outline_put(
            {
                "id": outline_id,
                "name": str(row.get("name") or ""),
                "payload": str(row.get("payload") or "{}"),
                "created_real": float(row.get("created_real") or time.time()),
            }
        )
    for key, put in (("states", store.wa_state_put), ("candidates", store.wa_candidate_put)):
        for row in writing.get(key) or []:
            if not isinstance(row, dict):
                continue
            new_timeline = timeline_map.get(str(row.get("timeline_id") or ""))
            if not new_timeline:
                continue
            put({**row, "instance_id": instance_id, "timeline_id": new_timeline})
    return kept


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


def _versioning() -> Any:
    """版本层（快照的存储形态与物化）在 runtime 层：顶层导入会成环，统一走这一条局部导入。"""
    from ..runtime import versioning

    return versioning


def _is_stored_form(snapshot: Any) -> bool:
    """容器里的快照是不是**存储形态**（`{kind, base?, body}`）。

    老容器的 `snapshot` 是物化好的全量正文（`note` / `world` / `runtime` / `dialog`…，没有 `kind`），
    两种形态都要能读（§7.3 兼容旧容器）。
    """
    versioning = _versioning()
    return (
        isinstance(snapshot, dict)
        and str(snapshot.get("kind") or "") in (versioning.SNAPSHOT_FULL, versioning.SNAPSHOT_DELTA)
        and "body" in snapshot
    )


def _relabel_body(
    body: dict[str, Any], instance_id: str, timeline_id: str, session_map: dict[str, str]
) -> dict[str, Any]:
    """物化正文的本地标识改写：运行载荷归到新实例 / 新线，对话行归到新会话。

    会话映射不到的对话行直接丢掉（不往新库里塞指向不存在会话的行）。
    """
    rows = dict(body)
    rows["runtime"] = _relabel_payload(dict(body.get("runtime") or {}), instance_id, timeline_id)
    dialog: list[dict[str, Any]] = []
    for row in body.get("dialog") or []:
        if not isinstance(row, dict):
            continue
        session_id = session_map.get(str(row.get("session_id") or ""))
        if session_id is None:
            continue
        dialog.append({**row, "session_id": session_id})
    rows["dialog"] = dialog
    return rows


def _relabel_delta(
    body: dict[str, Any], instance_id: str, timeline_id: str, session_map: dict[str, str]
) -> dict[str, Any]:
    """delta 正文里的行按同一条规则改写（不物化、不重编码）。

    行键（`id:…` / `message_id:…` / `seq:…`）只由行自己的主键字段算出，改写
    `instance_id` / `timeline_id` / `session_id` 不改键，所以 `order` / `deleted` 照旧可用；
    会话映射不到的对话行从 added / replaced 里去掉（与物化正文同一口径）。
    """
    out = dict(body)
    sections: dict[str, Any] = {}
    for name, part in (body.get("sections") or {}).items():
        if not isinstance(part, dict):
            continue
        piece = dict(part)
        for bucket in ("added", "replaced"):
            kept: list[dict[str, Any]] = []
            for row in part.get(bucket) or []:
                if not isinstance(row, dict):
                    continue
                item = dict(row)
                if "instance_id" in item:
                    item["instance_id"] = instance_id
                if "timeline_id" in item:
                    item["timeline_id"] = timeline_id
                if "session_id" in item:
                    session_id = session_map.get(str(item.get("session_id") or ""))
                    if session_id is None:
                        continue  # 会话没导进来，这行不留
                    item["session_id"] = session_id
                kept.append(item)
            piece[bucket] = kept
        sections[str(name)] = piece
    out["sections"] = sections
    return out


def _stored_snapshot_payload(
    snapshot: dict[str, Any],
    *,
    base_commit_id: str,
    commit_map: dict[str, str],
    instance_id: str,
    timeline_id: str,
    session_map: dict[str, str],
) -> tuple[str, str]:
    """把容器里的一条快照还原成**库内存储形态**的 `(payload, base_commit_id)`（§7.1 / §7.3）。

    delta 的 base 是导出端的提交标识，这里按 `commit_map` 重映射到新提交标识；
    base 找不到（提交闭包不完整）即拒绝导入，不静默留一条链断掉的快照。
    """
    kind = str(snapshot.get("kind") or _versioning().SNAPSHOT_FULL)
    if kind == _versioning().SNAPSHOT_DELTA:
        old_base = str(base_commit_id or snapshot.get("base") or "")
        new_base = commit_map.get(old_base, "")
        if not old_base or not new_base:
            raise InstanceError(
                f"导入件的提交闭包不完整：提交 {old_base or '（缺失）'} 没有随件（delta 链断在这里）"
            )
        body = snapshot.get("body") if isinstance(snapshot.get("body"), dict) else {}
        relabeled = _relabel_delta(body, instance_id, timeline_id, session_map)
        return _versioning().dump_snapshot(relabeled, kind=_versioning().SNAPSHOT_DELTA, base=new_base), new_base
    body = snapshot.get("body") if isinstance(snapshot.get("body"), dict) else snapshot
    return _versioning().dump_snapshot(_relabel_body(body, instance_id, timeline_id, session_map)), ""


def _restore_commit_snapshots(
    store: Store,
    instance_id: str,
    runtime: dict[str, Any],
    commit_map: dict[str, str],
    timeline_map: dict[str, str],
    session_map: dict[str, str],
) -> int:
    """提交快照随件恢复（§7.1 提交闭包）：导入件的回滚 / 分叉不丢历史。

    - 容器里是**存储形态**（新导出件）：原样写回并按 base 重建 delta 链（P0-5 / P1-14），
      不做物化、不重新编码；只把链上的提交标识换成导入端的新标识；
    - 容器里是**物化正文**（老导出件）：按老口径改写本地标识后整条写回（§7.3 兼容旧容器）；
    - 容器没带快照的提交（例如只有管理元数据、没有提交的无闭包容器补出的那条初始提交）：
      就地补一份全量，保证「回滚到创建点 / 从创建提交分叉」在新库里仍然可用。
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
        if _is_stored_form(snapshot):
            payload, base = _stored_snapshot_payload(
                snapshot,
                base_commit_id=str(item.get("base_commit_id") or ""),
                commit_map=commit_map,
                instance_id=instance_id,
                timeline_id=new_timeline,
                session_map=session_map,
            )
        else:  # 老容器：物化正文
            payload, base = _versioning().dump_snapshot(
                _relabel_body(snapshot, instance_id, new_timeline, session_map)
            ), ""
        store.commit_snapshot_put_stored(new_commit, instance_id, payload, base_commit_id=base)
        written += 1
    # 没有随件快照的提交（极简容器补出的那条）：就地补一份全量——这里运行状态已经装载完，
    # 快照反映的就是导入后的水位；不补的话该提交回滚 / 分叉都不可用。
    versioning = _versioning()
    for row in store.commit_list(instance_id):
        commit_id = str(row["id"])
        if store.commit_snapshot_stored(commit_id) is not None:
            continue
        store.commit_snapshot_put_stored(
            commit_id,
            instance_id,
            versioning.dump_snapshot(
                versioning.snapshot_of(store, instance_id, str(row["timeline_id"]))
            ),
        )
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
