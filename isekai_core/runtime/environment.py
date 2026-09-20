"""环境事实状态（WORLD_RUNTIME_SPEC §11.2）。

可选状态域：世界包声明类型、单位、取值域、初始值、变化来源与观察条件；**未声明的类型
不存在可供引用的真值**。状态只由世界时刻（声明的自然变化）、合法事件、生活线或声明
的自然条件推进——文本生成改不动它。认知接口只返回角色在对应条件下能观察到的投影。

纯函数层：不碰数据库、不调模型。
"""

from __future__ import annotations

from typing import Any

from ..world.cards import region_of
from . import events

#: 自然变化来源前缀：`natural:<名称>` 表示该类型按世界时间确定地走
NATURAL_PREFIX = "natural:"


def env_types(package: dict[str, Any]) -> dict[str, dict[str, Any]]:
    environment = package.get("environment") if isinstance(package.get("environment"), dict) else {}
    return {
        str(item.get("id")): item
        for item in environment.get("types") or []
        if isinstance(item, dict) and item.get("id")
    }


def declared(package: dict[str, Any], type_id: str) -> dict[str, Any] | None:
    """未声明的环境类型没有真值可引用（§11.2）。"""
    return env_types(package).get(str(type_id))


def initial_rows(
    package: dict[str, Any], *, instance_id: str, timeline_id: str, world_seconds: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for type_id, item in sorted(env_types(package).items()):
        out.append(
            {
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "type_id": type_id,
                "value": str(item.get("initial")),
                "unit": str(item.get("unit") or ""),
                "source": "initial",
                "from_world": int(world_seconds),
                "expiry": str(item.get("expiry") or "until_cleared"),
                "updated_world": int(world_seconds),
            }
        )
    return out


def _natural_drift(item: dict[str, Any], *, world_seconds: int, day_seconds: int) -> str | None:
    """声明的自然变化：按世界日桶确定地取值（同一时刻处处同值，不依赖进程随机化）。"""
    values = [str(value) for value in item.get("values") or []]
    if not values:
        return None
    bucket = int(world_seconds) // max(1, int(day_seconds))
    index = int(events.stable_key(item.get("id"), "env", bucket), 16) % len(values)
    return values[index]


def advance_rows(
    rows: list[dict[str, Any]],
    types: dict[str, dict[str, Any]],
    *,
    from_world: int,
    to_world: int,
    day_seconds: int,
) -> list[dict[str, Any]]:
    """推进到 to_world：只动声明了自然变化来源的类型；其余保持（等合法事件）。"""
    out: list[dict[str, Any]] = []
    for row in rows:
        item = types.get(str(row["type_id"]))
        if item is None:
            continue
        sources = [str(name) for name in item.get("sources") or []]
        if not any(name.startswith(NATURAL_PREFIX) for name in sources):
            continue
        value = _natural_drift(item, world_seconds=to_world, day_seconds=day_seconds)
        if value is None or value == str(row.get("value")):
            continue
        out.append(
            {
                **row,
                "value": value,
                "source": next(name for name in sources if name.startswith(NATURAL_PREFIX)),
                "from_world": int(to_world),
                "updated_world": int(to_world),
            }
        )
    _ = from_world
    return out


def apply_effects(
    rows: list[dict[str, Any]],
    effects: list[dict[str, Any]],
    types: dict[str, dict[str, Any]],
    *,
    world_seconds: int,
) -> list[dict[str, Any]]:
    """环境效果：只改已声明的类型、只取取值域内的值（越权的效果在这里被挡下）。"""
    by_type = {str(row["type_id"]): row for row in rows}
    out: list[dict[str, Any]] = []
    for effect in effects:
        if str(effect.get("kind")) != "environment_state":
            continue
        type_id = str(effect.get("target") or "")
        item = types.get(type_id)
        row = by_type.get(type_id)
        if item is None or row is None:
            continue
        value = effect.get("value")
        if value not in (item.get("values") or []):
            continue
        out.append(
            {
                **row,
                "value": str(value),
                "source": f"event:{effect.get('event_id') or ''}",
                "from_world": int(world_seconds),
                "expiry": str(effect.get("expiry") or row.get("expiry") or "until_cleared"),
                "updated_world": int(world_seconds),
            }
        )
    return out


def observers_of(item: dict[str, Any]) -> tuple[set[str], dict[str, str]]:
    """观察者名单与按观察者的精度说明；未声明观察者 = 对谁都不可观察。"""
    raw = item.get("observers")
    if isinstance(raw, dict):
        return {str(key) for key in raw}, {str(key): str(value) for key, value in raw.items()}
    if isinstance(raw, list):
        names = {str(name) for name in raw}
        return names, {name: str(item.get("observe") or "") for name in names}
    return set(), {}


def observations(
    rows: list[dict[str, Any]],
    types: dict[str, dict[str, Any]],
    card: dict[str, Any],
    *,
    world_seconds: int,
) -> list[dict[str, Any]]:
    """某个角色能观察到的环境投影：对得上观察条件才给，并带上该角色能有的精度。"""
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    who = {
        str((card.get("meta") or {}).get("card_id") or ""),
        str(card.get("role_id") or ""),
        str(identity.get("race_id") or ""),
        region_of(card),
    }
    who.discard("")
    out: list[dict[str, Any]] = []
    for row in rows:
        item = types.get(str(row["type_id"]))
        if item is None or int(row.get("from_world") or 0) > world_seconds:
            continue
        names, precision = observers_of(item)
        if not names:
            continue  # 没声明谁看得到 = 对谁都不可观察
        matched = who & names
        if "all" in names:
            matched = matched or {"all"}
        if not matched:
            continue
        note = next((precision.get(name, "") for name in sorted(matched) if precision.get(name)), "")
        out.append(
            {
                "type_id": str(row["type_id"]),
                "name": str(item.get("name") or ""),
                "value": str(row.get("value")),
                "unit": str(row.get("unit") or ""),
                "observe": note or str(item.get("observe") or ""),
                "from_world": int(row.get("from_world") or 0),
            }
        )
    return out
