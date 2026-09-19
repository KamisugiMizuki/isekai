"""实例导入导出：单一文件容器、版本检查与原子导入。

容器是未加密的 JSON 单文件（WORLD_SETTING_SPEC §7）：
  {container: 清单, setting: 锁定设定快照, runtime: 对话等运行部分, integrity: 指纹}
不含任何凭据、通道绑定、投递回执或去重作废记录；导入总是创建**新实例**并默认冻结，
原实例（若同名）不受影响，名称冲突按 §7.4 自动追加序号。
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path
from typing import Any

from ..store import Store
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
from .package import PackageError, clone_package
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
            "seed": row["seed"],
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
            "counts": {"sessions": len(sessions), "messages": len(messages)},
        },
        "setting": payload["setting"],
        "runtime": payload["runtime"],
        "integrity": {"algorithm": "sha256", "digest": _digest(payload)},
    }


def write_export(store: Store, instance_id: str, path: str | Path) -> dict[str, Any]:
    container = build_container(store, instance_id)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(container, ensure_ascii=False, indent=2), encoding="utf-8")
    return container["container"]


def read_container(path: str | Path) -> dict[str, Any]:
    file = Path(path)
    try:
        raw = json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise InstanceError(f"导入文件不存在：{file}") from exc
    except json.JSONDecodeError as exc:
        raise InstanceError(f"导入文件不是合法 JSON（{file}）：{exc}") from exc
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
    row = create_instance(
        store,
        package,
        cards,
        display_name=display_name or str(setting.get("original_name") or ""),
        imported=True,
        seed=str(runtime.get("seed") or "") or None,
        extra_setting={"imported_from": {"exported_at": (container.get("container") or {}).get("exported_at")}},
    )
    try:
        _restore_runtime(store, row["id"], runtime)
    except Exception:
        store.instance_delete(row["id"])
        raise
    return row


def _restore_runtime(store: Store, instance_id: str, runtime: dict[str, Any]) -> None:
    """阶段 1 只恢复会话与对话原文：时间线 / 提交图在阶段 4 才正式化，目前按初始提交重建。"""
    timelines = store.timeline_list(instance_id)
    if not timelines:
        raise InstanceError("实例缺少初始时间线")
    timeline_id = timelines[0]["id"]
    sessions = runtime.get("sessions") or []
    messages = runtime.get("messages") or []
    id_map: dict[str, str] = {}
    for item in sessions:
        if not isinstance(item, dict):
            continue
        character_id = str(item.get("character_id") or "character")
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


__all__ = [
    "PackageError",
    "build_container",
    "check_compatibility",
    "import_instance",
    "read_container",
    "verify_integrity",
    "write_export",
]
