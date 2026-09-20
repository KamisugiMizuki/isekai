"""生活线：从角色卡模板展开「她在做什么」的轻量真值序列（WORLD_RUNTIME_SPEC §11）。

- 只有世界时间窗 + 活动标识，不含坐标、路网或通行模拟；
- 模板确定活动骨架；LLM 只细化表述，失败用模板（阶段 2 先落模板表述，语言细化随阶段 3）；
- 每个角色、每个世界日只形成一份有效计划（写入即固化，重启不重抽）；
- 计划不等于经历：未来活动块不可当作已经发生。
"""

from __future__ import annotations

import json
import secrets
from typing import Any

from .calendar import Calendar


def expand_plan(
    card: dict[str, Any],
    calendar: Calendar,
    *,
    day_index: int,
    instance_id: str,
    timeline_id: str,
    created_world: int,
) -> dict[str, Any]:
    """按角色卡模板展开一个世界日的计划（跨日窗口按世界秒展开，不按现实日切）。"""
    character_id = str((card.get("meta") or {}).get("card_id") or "")
    template = card.get("life_template") or {}
    day_seconds = calendar.day_seconds
    day_start = day_index * calendar.day_seconds
    day_end = day_start + calendar.day_seconds
    windows: list[dict[str, Any]] = []
    for item in template.get("windows") or []:
        start, end = int(item["start"]), int(item["end"])
        world_start = day_start + start
        world_end = day_start + end  # end 可超过日长 → 跨日
        windows.append(
            {
                "start": world_start,
                "end": world_end,
                "activity": str(item.get("activity") or ""),
                "alternatives": [str(alt) for alt in item.get("alternatives") or []],
                "note": str(item.get("note") or ""),
            }
        )
        if day_seconds and end > day_seconds:
            # 跨日窗口在本日也有日首那一段（校验器同口径）：不展开它，次日 00:00 起就没有活动解释
            windows.append(
                {
                    "start": day_start,
                    "end": day_start + (end - day_seconds),
                    "activity": str(item.get("activity") or ""),
                    "alternatives": [str(alt) for alt in item.get("alternatives") or []],
                    "note": str(item.get("note") or ""),
                }
            )
    windows.sort(key=lambda item: item["start"])
    return {
        "id": f"lp-{secrets.token_hex(6)}",
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "character_id": character_id,
        "day_index": day_index,
        "windows": json.dumps({"day_start": day_start, "day_end": day_end, "windows": windows}, ensure_ascii=False),
        "state": "fixed",
        "created_world": int(created_world),
        "note": str(template.get("routine_note") or ""),
    }


def current_window(plan: dict[str, Any] | None, world_seconds: int) -> dict[str, Any] | None:
    """当前活动块（只含已开始且未结束的窗口；未来块不算已发生）。"""
    if not plan:
        return None
    try:
        payload = json.loads(str(plan["windows"]))
    except (KeyError, json.JSONDecodeError):
        return None
    for window in payload.get("windows", []):
        if int(window["start"]) <= world_seconds < int(window["end"]):
            return window
    return None


def describe_plan(plan: dict[str, Any] | None, calendar: Calendar, world_seconds: int) -> dict[str, Any]:
    """管理面 / 认知层可用的计划视图：当前活动 + 已过去的块数，不含未来文本细节。"""
    if not plan:
        return {"state": "none"}
    payload = json.loads(str(plan["windows"]))
    windows = payload.get("windows", [])
    done = [item for item in windows if int(item["end"]) <= world_seconds]
    current = current_window(plan, world_seconds)
    return {
        "state": plan.get("state"),
        "day_index": int(plan["day_index"]),
        "day": calendar.describe(int(plan["day_index"]) * calendar.day_seconds)[:20],
        "current": current,
        "completed": len(done),
        "total": len(windows),
    }


def effect_note(effects: list[dict[str, Any]] | None, character_id: str, role_id: str, region: str) -> str:
    """仍有效的后果对该角色当前活动的约束说明（§六）：只影响描述与经历，不改计划本身。"""
    targets = {character_id, role_id, region}
    kinds = sorted(
        {
            str(item.get("kind"))
            for item in effects or []
            if str(item.get("target")) in targets and str(item.get("kind"))
        }
    )
    return "、".join(kinds)


def activity_label(window: dict[str, Any] | None) -> str:
    if not window:
        return ""
    label = str(window.get("activity") or "")
    alternatives = window.get("alternatives") or []
    return f"{label}（或 {alternatives[0]}）" if alternatives else label
