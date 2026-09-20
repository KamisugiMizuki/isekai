"""阶段 4：版本管理——提交 / 分叉 / 回滚（WORLD_RUNTIME_SPEC §5 / §6 / §7）。

存储用**全量快照**（§六：完整快照与 diff 等价，本阶段先用全量验证相同语义）。
纳入提交的是世界运行状态；本机激活集合、视图、待生效倍率、通道绑定与凭据、投递回执、
运行世代等**控制状态**不进快照，也不随回滚恢复。
"""

from __future__ import annotations

import json
import time
from typing import Any


class VersionError(ValueError):
    """版本操作的前置条件不满足（提交不存在、线不存在等）。"""


def snapshot_of(store: Any, instance_id: str, timeline_id: str, *, note: str = "") -> dict[str, Any]:
    """取一致快照（§5.1）：整条线的世界运行状态 + 该线的对话原文与提取任务。"""
    clock = store.clock_get(timeline_id) or {}
    runtime = store.runtime_dump(instance_id, timeline_id, watermark=int(clock.get("processed_world") or 0))
    sessions = [
        item
        for item in store.instance_sessions(instance_id)
        if str(item.get("timeline_id")) == timeline_id
    ]
    dialog: list[dict[str, Any]] = []
    for session in sessions:
        page = store.history_page(str(session["id"]), limit=10000)
        for row in page.get("messages") or []:
            dialog.append({**row, "session_id": str(session["id"])})
    from ..version import DATA_FORMAT_VERSION, RULES_VERSION

    world_seed = ""
    try:
        from .service import RuntimeService  # 局部导入避免循环

        world_seed = RuntimeService(store).seed_of(store.instance_get(instance_id) or {})
    except Exception:  # 种子取不到不影响快照（空串如实记录）
        world_seed = ""
    return {
        "note": str(note or ""),
        "world": int(clock.get("processed_world") or 0),
        "rate": int(clock.get("rate") or 1),
        # 语义元数据（§5.1）：确定性复算要用的规则版本、数据格式与锁定种子，与抽样同源
        "rules_version": str(RULES_VERSION),
        "data_format": str(DATA_FORMAT_VERSION),
        "seed": str(world_seed),
        "sessions": [
            {key: value for key, value in item.items() if key in (
                "id", "instance_id", "timeline_id", "character_id", "channel_id", "thread_id", "created_at"
            )}
            for item in sessions
        ],
        "dialog": dialog,
        "runtime": runtime,
    }


def make_commit_row(commit_id: str, instance_id: str, timeline_id: str, *, kind: str, moment: int, note: str = "") -> dict[str, Any]:
    return {
        "id": commit_id,
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "kind": str(kind),
        "moment": int(moment),
        "note": str(note or ""),
        "created_at": time.time(),
    }


def public_commit(row: dict[str, Any]) -> dict[str, Any]:
    """列表只给管理元数据：标识 / 时间 / 备注 / 来源关系，不生成泄漏剧情的摘要（§5.1）。"""
    return {
        "id": str(row["id"]),
        "kind": str(row["kind"]),
        "moment": int(row["moment"]),
        "note": str(row.get("note") or ""),
        "timeline_id": str(row["timeline_id"]),
        "created_at": float(row.get("created_at") or 0.0),
    }


def parse_snapshot(payload: str | bytes | None) -> dict[str, Any]:
    if not payload:
        return {}
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", "replace")
    try:
        data = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise VersionError("快照损坏") from exc
    return data if isinstance(data, dict) else {}
