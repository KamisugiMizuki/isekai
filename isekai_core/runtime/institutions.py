"""制度与惯例状态（阶段 6：世界自演化）。

世界包声明制度（组织的职权、职位、空缺规则）与文化惯例（适用群体、当前做法、允许变化
范围）；运行层只让它们沿**已有依据**变化——合法事件效果、角色行动结果或声明的自然推导，
每条变化都带来源与发生时刻。未声明的对象没有可引用的真值，文本生成改不动它。

纯函数层：不碰数据库、不调模型。
"""

from __future__ import annotations

from typing import Any

from ..world.validate import change_allowed, custom_index, office_index

#: 空缺期间的事务判定结果
ACTIVE = "active"
CONTINUES = "continues"
SUSPENDED = "suspended"


def office_rows(
    package: dict[str, Any], *, instance_id: str, timeline_id: str, world_seconds: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for office_id, office in sorted(office_index(package).items()):
        out.append(
            {
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "office_id": office_id,
                "institution_id": str(office["institution_id"]),
                "institution_name": str(office["institution_name"]),
                "name": str(office.get("name") or office_id),
                "holder": str(office.get("holder") or ""),
                "continues": list(office["continues"]),
                "suspended": list(office["suspended"]),
                "source": "initial",
                "from_world": int(world_seconds),
                "updated_world": int(world_seconds),
            }
        )
    return out


def custom_rows(
    package: dict[str, Any], *, instance_id: str, timeline_id: str, world_seconds: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for custom_id, custom in sorted(custom_index(package).items()):
        forms = [str(item) for item in custom.get("forms") or []]
        out.append(
            {
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "custom_id": custom_id,
                "name": str(custom.get("name") or custom_id),
                "applies_to": str(custom.get("applies_to") or ""),
                "form": str(custom.get("practice") or ""),
                "forms": forms,
                "basis": str(custom.get("basis") or ""),
                "source": "initial",
                "from_world": int(world_seconds),
                "updated_world": int(world_seconds),
            }
        )
    return out


def matter_status(row: dict[str, Any], matter: str) -> str | None:
    """空缺期间某事务继续还是暂停——**可判定**：职位有人即 active，空缺按声明规则判。

    未声明的事务返回 None（不是「默认照旧」也不是「默认停摆」）。
    """
    if str(row.get("holder") or ""):
        return ACTIVE
    if matter in [str(item) for item in row.get("continues") or []]:
        return CONTINUES
    if matter in [str(item) for item in row.get("suspended") or []]:
        return SUSPENDED
    return None


def apply_effects(
    rows: list[dict[str, Any]],
    customs: list[dict[str, Any]],
    effects: list[dict[str, Any]],
    package: dict[str, Any],
    *,
    world_seconds: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把合法事件效果落到制度 / 惯例状态上（不改世界真值以外的东西）。

    返回 (新状态, 本次实际发生的变化)——变化带来源标识与发生时刻。
    """
    by_office = {str(row["office_id"]): dict(row) for row in rows}
    by_custom = {str(row["custom_id"]): dict(row) for row in customs}
    changed: list[dict[str, Any]] = []
    for effect in effects or []:
        kind = str(effect.get("kind") or "")
        if kind not in ("institution_state", "custom_state"):
            continue
        target = str(effect.get("target") or "")
        value = str(effect.get("value") or "")
        allowed, _ = change_allowed(package, kind=kind, target=target, value=value)
        if not allowed:
            continue  # 未声明或越界：不落状态（调用方在包校验期已拦一次）
        source = str(effect.get("source") or effect.get("event_id") or "")
        if kind == "institution_state" and target in by_office:
            row = by_office[target]
            if str(row["holder"]) == value:
                continue
            row["holder"] = value
            row["source"] = source or row["source"]
            row["from_world"] = int(world_seconds)
            row["updated_world"] = int(world_seconds)
            changed.append(dict(row))
        elif kind == "custom_state" and target in by_custom:
            row = by_custom[target]
            if str(row["form"]) == value:
                continue
            row["form"] = value
            row["source"] = source or row["source"]
            row["from_world"] = int(world_seconds)
            row["updated_world"] = int(world_seconds)
            changed.append(dict(row))
    return list(by_office.values()), list(by_custom.values())


def _known_changes(
    effects: list[dict[str, Any]], known_targets: set[str], *, world_seconds: int
) -> dict[str, dict[str, Any]]:
    """她获知过的、且已发生的变化：目标 → 最近一次她知道的取值。"""
    latest: dict[str, dict[str, Any]] = {}
    for effect in effects or []:
        kind = str(effect.get("kind") or "")
        if kind not in ("institution_state", "custom_state"):
            continue
        if int(effect.get("from_world") or 0) > int(world_seconds):
            continue
        event_id = str(effect.get("event_id") or "")
        if event_id not in known_targets:
            continue
        target = str(effect.get("target") or "")
        previous = latest.get(target)
        if previous is None or int(effect.get("from_world") or 0) >= int(previous.get("from_world") or 0):
            latest[target] = effect
    return latest


def vacancies_for_deaths(
    rows: list[dict[str, Any]],
    deaths: list[dict[str, Any]],
    package: dict[str, Any],
    *,
    world_seconds: int,
) -> list[dict[str, Any]]:
    """声明的延续规则：在任者身故 → 该职位出缺。

    依据=身故事件（来源标识与发生时刻都登记在变化行上）；这里只做「出缺」，不替世界
    决定由谁承接——承接是后续合法事件或角色行动的事。
    """
    name_of_entity = {
        _str(item.get("id")): _str(item.get("name"))
        for item in package.get("entities") or []
        if isinstance(item, dict)
    }
    out: list[dict[str, Any]] = []
    for row in rows:
        holder = str(row.get("holder") or "")
        if not holder:
            continue
        for death in deaths:
            card_id = str(death.get("card_id") or "")
            names = {str(name) for name in death.get("names") or []}
            holder_name = name_of_entity.get(holder, "")
            if holder == card_id or (holder_name and holder_name in names):
                out.append(
                    {
                        **row,
                        "holder": "",
                        "source": str(death.get("event_id") or row.get("source") or ""),
                        "from_world": int(world_seconds),
                        "updated_world": int(world_seconds),
                    }
                )
                break
    return out


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def observations(
    rows: list[dict[str, Any]],
    customs: list[dict[str, Any]],
    known_targets: set[str],
    *,
    world_seconds: int,
    holder_names: dict[str, str] | None = None,
    effects: list[dict[str, Any]] | None = None,
    declared_offices: dict[str, str] | None = None,
    declared_customs: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """角色视角的制度 / 惯例现状。

    初始状态是公开常识；后续变化按**她获知的来源事件**重建——没听说的人仍然按上一次
    她知道的说法讲，而不是凭空升级成新事实，也不因为别人改了就把这件事从她记忆里抹去。
    """
    names = holder_names or {}
    declared = declared_offices or {}
    declared_customs = declared_customs or {}
    known = _known_changes(effects or [], known_targets, world_seconds=world_seconds)
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: str(item["office_id"])):
        belief = known.get(str(row["office_id"]))
        if belief is None:
            # 没听说过后来的变化：她只知道声明的初始状态（不是「当下真值」）
            holder = str(declared.get(str(row["office_id"]), row.get("holder") or ""))
        else:
            holder = str(belief.get("value") or "")
        if belief is not None:
            row = {**row, "from_world": int(belief.get("from_world") or row.get("from_world") or 0)}
        if holder:
            value = f"{names.get(holder, holder)}在任"
            note = ""
        else:
            continues = "、".join(str(item) for item in row.get("continues") or [])
            suspended = "、".join(str(item) for item in row.get("suspended") or [])
            value = "空缺"
            note = f"照旧：{continues or '—'}；暂停：{suspended or '—'}"
        out.append(
            {
                "name": f"{row.get('institution_name')}·{row.get('name')}",
                "value": value,
                "note": note,
                "since": int(row.get("from_world") or 0),
            }
        )
    for row in sorted(customs, key=lambda item: str(item["custom_id"])):
        belief = known.get(str(row["custom_id"]))
        if belief is None:
            form = str(declared_customs.get(str(row["custom_id"]), row.get("form") or ""))
        else:
            form = str(belief.get("value") or "")
        out.append(
            {
                "name": str(row.get("name") or ""),
                "value": f"现行做法：{form}",
                "note": f"适用：{row.get('applies_to')}" if row.get("applies_to") else "",
                "since": int(row.get("from_world") or 0),
            }
        )
    return out
