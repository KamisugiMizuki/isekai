"""角色打算与重议（WORLD_RUNTIME_SPEC §11.3）、局部故事收束（§11.4）。

纯函数层：打算是角色状态而非世界事实；重议是确定性的（读当时快照与角色可知信息），
只有**受支持的行动效果**才能提交事件——没有可执行效果就记录延期 / 放弃 / 继续等待，
不建通用规划器、不把后台决策伪装成已完成行动。
"""

from __future__ import annotations

import json
from typing import Any

from . import events

#: 打算状态流：候选意向 → 角色采纳 → 等待条件 → 执行 / 延期 / 放弃
STAGES: tuple[str, ...] = ("candidate", "adopted", "waiting", "done", "deferred", "abandoned")

#: 终态（§11.4 至少区分这些；持续中＝仍 adopted / waiting）
TERMINAL: dict[str, str] = {
    "done": "达成",
    "deferred": "延期",
    "abandoned": "放弃",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def initial_rows(
    card: dict[str, Any], *, instance_id: str, timeline_id: str, world_seconds: int
) -> list[dict[str, Any]]:
    """卡片的打算落成角色状态；阶段=adopted（依据已在创建前校验把关）。"""
    character_id = str((card.get("meta") or {}).get("card_id") or "")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(card.get("intents") or []):
        if not isinstance(item, dict):
            continue
        window = item.get("window") if isinstance(item.get("window"), dict) else {}
        rows.append(
            {
                "id": str(item.get("id") or f"in-{index + 1}"),
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
                "object": str(item.get("object") or ""),
                "basis": str(item.get("basis") or ""),
                "strength": float(item.get("strength") or 0.5),
                "window_from": int(window.get("from") or 0),
                "window_to": int(window.get("to") or 0),
                "preconditions": _json([str(ref) for ref in item.get("preconditions") or []]),
                "effect": _json(item.get("effect") or {}),
                "stage": "adopted",
                "note": "",
                "source_world": int(world_seconds),
                "updated_world": int(world_seconds),
            }
        )
    return rows


def parse(row: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """读侧统一解析：preconditions / effect 落库是 JSON 文本。"""
    try:
        preconditions = json.loads(str(row.get("preconditions") or "[]"))
    except json.JSONDecodeError:
        preconditions = []
    try:
        effect = json.loads(str(row.get("effect") or "{}"))
    except json.JSONDecodeError:
        effect = {}
    return [str(item) for item in preconditions], effect if isinstance(effect, dict) else {}


def blockers(row: dict[str, Any], effects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """仍有效的后果里，哪些挡住了这条打算（角色可知的阻碍）。"""
    _, effect = parse(row)
    targets = {str(effect.get("target") or "")}
    targets.discard("")
    out = []
    for item in effects:
        if str(item.get("target")) in targets and str(item.get("event_id")) != str(row.get("id")):
            out.append(item)
    return out


def decide(
    row: dict[str, Any],
    *,
    world_seconds: int,
    events_present: set[str],
    active_effects: list[dict[str, Any]],
) -> str:
    """重议（确定性）：keep / act / wait / defer / abandon（§11.3）。"""
    stage = str(row.get("stage"))
    if stage in ("done", "abandoned"):
        return "keep"
    preconditions, effect = parse(row)
    unmet = events.unmet_preconditions(preconditions, events=events_present, effects=set())
    window_from, window_to = int(row.get("window_from") or 0), int(row.get("window_to") or 0)
    blocked = blockers(row, active_effects)
    if world_seconds < window_from:
        return "keep"
    if world_seconds <= window_to:
        if unmet or blocked:
            return "wait"
        return "act" if effect else "keep"  # 没有受支持效果就不能提交事件
    # 目标时间窗已过
    if not unmet and not blocked and effect:
        return "act"  # 迟到的执行仍然合法（窗口不是硬失效）
    return "defer" if stage != "deferred" else "abandon"


def action_event(
    row: dict[str, Any],
    *,
    instance_id: str,
    timeline_id: str,
    world_seconds: int,
    calendar: Any,
) -> dict[str, Any]:
    """把打算执行成一条角色行动事件（只挂该打算声明的受支持效果）。"""
    _, effect = parse(row)
    ident = f"ev-act-{events.stable_key(row['instance_id'], row['timeline_id'], row['character_id'], row['id'])[:10]}"
    return {
        "id": ident,
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "world_seconds": int(world_seconds),
        "seq": int(events.stable_key(ident)[:6], 16),
        "kind": "character",
        "family": "",
        "template": str(row["id"]),
        "source": "character_action",
        "summary": str(row.get("object") or ""),
        "detail": str(row.get("object") or ""),
        "text_source": "template",
        "effects": _json([effect] if effect else []),
        "share_value": 0,
        "importance": 0.4,
        "created_real": 0.0,
        "_calendar": calendar,
    }


def story_units(rows: list[dict[str, Any]], event_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """局部故事单元（§11.4）：可追溯的组合视图，不另立一套事实。"""
    by_template: dict[str, list[dict[str, Any]]] = {}
    for item in event_rows:
        by_template.setdefault(str(item.get("template") or ""), []).append(item)
    units: list[dict[str, Any]] = []
    for row in rows:
        related = sorted(
            by_template.get(str(row["id"]), []), key=lambda item: int(item["world_seconds"])
        )
        terminal = TERMINAL.get(str(row.get("stage")), "持续中")
        units.append(
            {
                "intent": str(row["id"]),
                "character": str(row["character_id"]),
                "object": str(row.get("object") or ""),
                "basis": str(row.get("basis") or ""),
                "stage": str(row.get("stage")),
                "terminal": terminal,
                "obstacles": str(row.get("note") or ""),
                "effects": [effect for item in related for effect in [item.get("effects")]],
                "unresolved": terminal == "延期" or str(row.get("stage")) in ("adopted", "waiting"),
                "events": [str(item["id"]) for item in related],
            }
        )
    return units
