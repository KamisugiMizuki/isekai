"""性格单元引擎（WORLD_RUNTIME_SPEC §10、附录 A）。

纯函数式：输入单元行列表，输出新行列表，不碰数据库。规则要点：
- 四种驱动的生成区间只约束**初始**置信度；后续更新限制在 [0,1]，允许低于区间并最终归档；
- 后来的弱驱动不能把已有高置信单元直接裁到该驱动的生成上限；
- 非锚点 c < 0.05 归档（保留依据，可从阈值附近渐进唤醒）；锚点不归档、保有保护下限；
- 驱动迁移隐式：累积稳定度 + 长期强化把普通单元固化为锚点，不产生可见迁移记录；
- 同一来源键只消费一次（重试 / 重算不重复强化）。
"""

from __future__ import annotations

import json
from typing import Any

MODES = ("anchor", "event", "dialog", "time")

#: 初始置信度区间（生成区间，不是终身硬下限）
BANDS: dict[str, tuple[float, float]] = {
    "anchor": (0.75, 0.99),
    "event": (0.40, 0.95),
    "dialog": (0.15, 0.70),
    "time": (0.05, 0.50),
}

#: 变化步长起点 / 每世界日衰减起点（附录 A 标定输入）
STEP: dict[str, float] = {"anchor": 0.01, "event": 0.10, "dialog": 0.06, "time": 0.03}
DECAY_PER_DAY: dict[str, float] = {"anchor": 0.001, "event": 0.02, "dialog": 0.05, "time": 0.08}

#: 归档阈值与锚点保护下限
ARCHIVE_BELOW = 0.05
# ponytail: 锚点下限是标定常数，长期表现需按 §10.3 校准后再定值
ANCHOR_FLOOR = 0.60
#: 非锚点固化为锚点所需：稳定度累积与置信度门槛（隐式迁移，不对用户可见）
PROMOTE_STABILITY = 6.0
PROMOTE_CONFIDENCE = 0.72

MAX_DRIFT_STEP = 0.15  # 单次驱动的最大变化，保证连续


def initial_rows(card: dict[str, Any], *, instance_id: str, timeline_id: str, world_seconds: int) -> list[dict[str, Any]]:
    """角色卡 → 初始单元行；卡片已通过校验（区间与锚点数量在那边把关）。"""
    character_id = str((card.get("meta") or {}).get("card_id") or "")
    rows: list[dict[str, Any]] = []
    for unit in card.get("initial_units") or []:
        rows.append(
            {
                "id": str(unit["id"]),
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
                "mode": str(unit["driver"]),
                "semantic": str(unit["semantic"]),
                "basis": str(unit.get("basis") or ""),
                "confidence": float(unit["confidence"]),
                "stability": 0.0,
                "archived": 0,
                "consumed": "[]",
                "updated_world": int(world_seconds),
            }
        )
    return rows


def _consumed(row: dict[str, Any]) -> list[str]:
    raw = row.get("consumed") or "[]"
    if isinstance(raw, list):
        return [str(item) for item in raw]
    try:
        parsed = json.loads(str(raw))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def apply_time(rows: list[dict[str, Any]], *, from_world: int, to_world: int, day_seconds: int) -> list[dict[str, Any]]:
    """世界时间衰减（按世界时长结算，在线小步与离线大步等价）。"""
    if to_world <= from_world or day_seconds <= 0:
        return rows
    days = (to_world - from_world) / day_seconds
    updated: list[dict[str, Any]] = []
    for row in rows:
        mode = str(row["mode"])
        drop = DECAY_PER_DAY.get(mode, 0.0) * days
        row = dict(row)
        row["confidence"] = _clamp(float(row["confidence"]) - drop)
        row["updated_world"] = int(to_world)
        updated.append(row)
    return archive_pass(updated)


def apply_drive(
    rows: list[dict[str, Any]],
    *,
    mode: str,
    source_key: str,
    semantic: str | None,
    strength: float,
    positive: bool,
    world_seconds: int,
) -> list[dict[str, Any]]:
    """一次驱动：强化已有的同义单元，或（事件 / 对话驱动）新建候选单元。

    `source_key` 是幂等键：同一来源只消费一次，重试与重算不重复强化。
    """
    if mode not in MODES:
        raise ValueError(f"未知驱动：{mode}")
    amount = min(MAX_DRIFT_STEP, STEP.get(mode, 0.05) * max(0.0, min(1.0, strength)))
    if amount <= 0:
        return rows
    updated: list[dict[str, Any]] = []
    matched = False
    for row in rows:
        row = dict(row)
        consumed = _consumed(row)
        if source_key in consumed:
            updated.append(row)
            continue
        same_semantic = semantic is not None and str(row["semantic"]) == semantic
        if not matched and same_semantic:
            delta = amount if positive else -amount
            row["confidence"] = _clamp(float(row["confidence"]) + delta)
            if positive:
                row["stability"] = float(row.get("stability") or 0.0) + 1.0
            consumed.append(source_key)
            row["consumed"] = json.dumps(consumed, ensure_ascii=False)
            row["updated_world"] = int(world_seconds)
            matched = True
        updated.append(row)
    if not matched and semantic:
        low, high = BANDS[mode]
        seeded = _clamp(high * max(0.2, strength) if positive else low * 0.5)
        updated.append(
            {
                "id": f"u-{abs(hash((source_key, semantic))) % (10**10):010d}",
                "instance_id": rows[0]["instance_id"] if rows else "",
                "timeline_id": rows[0]["timeline_id"] if rows else "",
                "character_id": rows[0]["character_id"] if rows else "",
                "mode": mode,
                "semantic": semantic,
                "basis": f"来自 {source_key}",
                "confidence": seeded,
                "stability": 1.0 if positive else 0.0,
                "archived": 0,
                "consumed": json.dumps([source_key], ensure_ascii=False),
                "updated_world": int(world_seconds),
            }
        )
    return promote(archive_pass(updated))


def archive_pass(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """非锚点低于阈值即归档；锚点受保护，只降到保护下限。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        row = dict(row)
        confidence = float(row["confidence"])
        if str(row["mode"]) == "anchor":
            row["confidence"] = max(ANCHOR_FLOOR, confidence)
            row["archived"] = 0
        elif confidence < ARCHIVE_BELOW:
            row["archived"] = 1
        out.append(row)
    return out


def promote(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """隐式迁移：长期稳定的普通单元固化为锚点（不写迁移记录、不对用户可见）。"""
    for row in rows:
        if str(row["mode"]) == "anchor":
            continue
        if float(row.get("stability") or 0.0) >= PROMOTE_STABILITY and float(row["confidence"]) >= PROMOTE_CONFIDENCE:
            row["mode"] = "anchor"
            row["basis"] = f"{row.get('basis') or ''}（长期稳定）".strip()
    return rows


def visible(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """参与表达的单元：未归档，按置信度排序（锚点优先参与风格与句式）。"""
    return sorted(
        (dict(row) for row in rows if not int(row.get("archived") or 0)),
        key=lambda item: (str(item["mode"]) != "anchor", -float(item["confidence"])),
    )
