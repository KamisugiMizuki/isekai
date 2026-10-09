"""生活线：从角色卡模板展开「她在做什么」的轻量真值序列（WORLD_RUNTIME_SPEC §11）。

- 只有世界时间窗 + 活动标识，不含坐标、路网或通行模拟；
- 模板确定活动骨架；LLM 只细化表述，失败用模板（阶段 2 先落模板表述，语言细化随阶段 3）；
- 每个角色、每个世界日只形成一份有效计划（写入即固化，重启不重抽）；
- 计划不等于经历：未来活动块不可当作已经发生。
"""

from __future__ import annotations

import json
from typing import Any

from .calendar import Calendar
from .events import stable_key


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
        # 计划 id 必须**确定性**（§2.2#14「同设定 + 前序状态 → 同答案」）：原实现用 `secrets.token_hex`，
        # 同一实例、同一世界日重跑会得到不同 id ⇒ 导出件不可逐字节复现，回滚 / 重放也会「同一天两个计划」。
        # 计划真正的主键是 (实例, 线, 角色, 世界日)，所以直接用稳定哈希派生。
        "id": f"lp-{stable_key(instance_id, timeline_id, character_id, int(day_index))[:12]}",
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


#: B-5：身体后果档位的**严重度顺序**（从轻到重）；取该角色身上最重的一档
CASUALTY_ORDER: tuple[str, ...] = ("轻伤", "重伤", "失能", "死亡")


def casualty_grade(effects: list[dict[str, Any]] | None, character_id: str) -> str:
    """该角色身上最重的身体后果档位（没有则返回空串）。纯函数、只看 target 与 value。"""
    grades = {
        str(item.get("value") or "")
        for item in effects or []
        if str(item.get("kind")) == "casualty" and str(item.get("target")) == character_id
    }
    for grade in reversed(CASUALTY_ORDER):
        if grade in grades:
            return grade
    return ""


def apply_casualty(plan: dict[str, Any], grade: str) -> dict[str, Any]:
    """B-5：身体后果**改变计划本身**，而不只是出现在约束说明里。纯函数、确定性（同一输入同一结果）。

    - `轻伤`：不改活动窗口（说明里已带档位）；
    - `重伤`：保留原窗口，但**清空备选**并标注——受了重伤就不该还留着「或去别处」的活动弹性；
    - `失能` / `死亡`：整日窗口合并为**静养**，不再外出（`死亡` 由归档路径接管，这里只做兜底）。

    说明：只改写**本批新建**的计划（计划按世界日一次写定，已固化的当日计划不回改）。
    """
    if grade not in ("重伤", "失能", "死亡"):
        return plan
    # `expand_plan` 落库时把窗口序列化成 JSON 字符串（`_collect_batch` 用 `json.loads` 读回），
    # 所以这里两种形态都要认：列表（内存态）与字符串（持久化态），改写后按原形态写回。
    # 持久化形态是 `{"windows": [...]}`（`_collect_batch` 用 `json.loads(...).get("windows")` 读回），
    # 内存态则是裸列表——两种都要认，且改写后**按原形态写回**，否则读回方会解析失败。
    raw_windows = plan.get("windows")
    wrapped = isinstance(raw_windows, str)
    if wrapped:
        try:
            body = json.loads(raw_windows or "{}")
        except json.JSONDecodeError:
            return plan
        windows = list(body.get("windows") or []) if isinstance(body, dict) else list(body or [])
    else:
        windows = list(raw_windows or [])
    if not windows:
        return plan
    if grade in ("失能", "死亡"):
        start = min(int(item.get("start") or 0) for item in windows)
        end = max(int(item.get("end") or 0) for item in windows)
        rewritten = [{
            "start": start,
            "end": end,
            "activity": "静养",
            "alternatives": [],
            "note": f"身体后果：{grade}（活动窗口合并为静养）",
        }]
    else:
        rewritten = [
            {
                **item,
                "alternatives": [],
                "note": (f"{item.get('note') or ''}（重伤：活动受限）").strip(),
            }
            for item in windows
        ]
    if wrapped:
        return {**plan, "windows": json.dumps({"windows": rewritten}, ensure_ascii=False)}
    return {**plan, "windows": rewritten}


def effect_note(effects: list[dict[str, Any]] | None, character_id: str, role_id: str, region: str) -> str:
    """仍有效的后果对该角色当前活动的约束说明（§六）：只影响描述与经历，不改计划本身。"""
    targets = {character_id, role_id, region}
    # B-5：身体后果要**带上档位**才读得出约束强度（只印 `casualty` 无法区分轻伤与失能）
    labels: set[str] = set()
    for item in effects or []:
        if str(item.get("target")) not in targets:
            continue
        kind = str(item.get("kind") or "")
        if not kind:
            continue
        if kind == "casualty":
            grade = str(item.get("value") or "")
            labels.add(f"casualty:{grade}" if grade else kind)
        else:
            labels.add(kind)
    return "、".join(sorted(labels))


def activity_label(window: dict[str, Any] | None) -> str:
    if not window:
        return ""
    label = str(window.get("activity") or "")
    alternatives = window.get("alternatives") or []
    return f"{label}（或 {alternatives[0]}）" if alternatives else label
