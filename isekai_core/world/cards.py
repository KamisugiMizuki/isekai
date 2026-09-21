"""角色卡：结构、模板、单卡校验与创建前联合校验。

卡片是「她起初是谁」的声明式数据（CHARACTER_CARD_SPEC §2、附录 A）：
- 只保存用户确认的最终版本，不保留 AI 生成历史；
- 年龄由出生时刻与世界时刻推导，不手填；
- 初始知识必须有来源，且获知时间不晚于初始时刻；
- 锚点置信度必须落在运行层规定的生成区间内（0.75–0.99）。
"""

from __future__ import annotations

from typing import Any

from .package import new_package_id
from .validate import EXPIRY_KINDS, SUPPORTED_EFFECTS, _all_ids, lifespan_errors

DRIVERS = ("anchor", "event", "dialog", "time")
CONFIDENCE_BANDS = {
    "anchor": (0.75, 0.99),
    "event": (0.40, 0.95),
    "dialog": (0.15, 0.70),
    "time": (0.05, 0.50),
}
KNOWLEDGE_SOURCES = ("canon", "narrative", "historiography", "self")
HARD_ALLOWED = ("self_experience", "small_env", "user_contact")


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def days_per_year(calendar: dict[str, Any]) -> int:
    months = calendar.get("months") if isinstance(calendar, dict) else None
    if not isinstance(months, list) or not months:
        return 0
    return sum(int(m.get("days", 0)) for m in months if isinstance(m, dict))


def template_card(package: dict[str, Any], *, name: str = "未命名角色") -> dict[str, Any]:
    """表单式角色卡骨架：引用目标世界包中真实存在的种族 / 渠道 / 联络机制。"""
    races = package.get("races") if isinstance(package.get("races"), list) else []
    roles = package.get("roles") if isinstance(package.get("roles"), list) else []
    sources = package.get("sources") if isinstance(package.get("sources"), list) else []
    comms = package.get("comms", {}).get("mechanisms") if isinstance(package.get("comms"), dict) else []
    life = package.get("life") if isinstance(package.get("life"), list) else []
    return {
        "meta": {"schema": "1.0", "card_id": f"cc-{new_package_id()[3:]}", "confirmed": False},
        "identity": {
            "name": name,
            "race_id": races[0].get("id") if races else "",
            "born": 0,
            "gender": "",
            "occupation": "",
            "self_identity": "",
        },
        "background": {"creator": "", "self_knowledge": ""},
        "region": "",
        "role_id": roles[0].get("id") if roles else None,
        "channels": [{"source_id": sources[0].get("id"), "conditions": ""}] if sources else [],
        "initial_knowledge": [],
        "comms": [{"mechanism_id": comms[0].get("id"), "note": ""}] if comms else [],
        "first_contact": {"stance": "", "intent": ""},
        "initial_units": [{"id": "iu-1", "semantic": "", "driver": "anchor", "confidence": 0.85, "basis": ""}],
        # 打算（可选）：角色自己惦记着的事，附受支持的行动效果（WORLD_RUNTIME_SPEC §11.3）
        "intents": [],
        "cognition": {"mode": "soft", "sources": ["self_experience", "user_contact"]},
        "life_template": {
            "sleep": True,
            "routine_note": "",
            "windows": [
                {"start": 0, "end": int(package.get("calendar", {}).get("day_seconds", 86400)), "activity": (life[0].get("windows") or [{}])[0].get("activity", "") if life else ""},
            ],
        },
        "appearance": "",
    }


def region_of(card: dict[str, Any]) -> str:
    """角色的生活区域：卡片顶层字段 `region`（供认知与事件范围匹配）。

    早期草稿把它写在 `identity.region` 下，这里兜底读一次——两处读法不一致会让
    「按区域声明的观察条件 / 事件范围」永远匹配不上。
    """
    top = str(card.get("region") or "")
    if top:
        return top
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    return str(identity.get("region") or "")


def effective_lifespan(identity: dict[str, Any], race: dict[str, Any] | None) -> dict[str, Any]:
    """个体寿命的生效形态（§十 残余「个体覆盖的优先级与冲突规则」）。

    优先级：卡片 `identity.died`（固死，另有分支）> 卡片 `identity.lifespan` > 种族 `lifespan`。
    卡级覆盖是合法的个体差异（不是冲突）：只要卡片声明了寿命形态（`mode` 或 `max_years`），就用它，
    哪怕种族声明的是 `long` / `unbounded`；返回空表 = 不据以推算寿终。
    """
    own = identity.get("lifespan") if isinstance(identity.get("lifespan"), dict) else {}
    if own.get("mode") is not None or isinstance(own.get("max_years"), int):
        return own
    if isinstance(race, dict) and isinstance(race.get("lifespan"), dict):
        return race["lifespan"]
    return {}


def validate_card(card: dict[str, Any], package: dict[str, Any], *, moment: int) -> list[str]:
    """单卡校验：身份 / 渠道 / 知识 / 单元 / 认知 / 日程。moment = 实例初始世界秒。"""
    errors: list[str] = []
    if not isinstance(card, dict):
        return ["角色卡必须是 JSON 对象"]
    calendar = package.get("calendar", {}) if isinstance(package.get("calendar"), dict) else {}
    day = calendar.get("day_seconds") if isinstance(calendar.get("day_seconds"), int) else 0
    seconds_per_year = day * days_per_year(calendar)

    identity = card.get("identity")
    if not isinstance(identity, dict):
        errors.append("identity: 缺少身份段")
        identity = {}
    if not _text(identity.get("name")):
        errors.append("identity.name: 缺少姓名")
    if not _text(identity.get("occupation")):
        errors.append("identity.occupation: 缺少职业")
    if not _text(identity.get("self_identity")):
        errors.append("identity.self_identity: 缺少自我认同")

    races = {r.get("id"): r for r in package.get("races", []) if isinstance(r, dict)}
    race = races.get(identity.get("race_id"))
    if race is None:
        errors.append(f"identity.race_id: 引用的种族不存在 {identity.get('race_id')!r}")
    born = identity.get("born")
    if not isinstance(born, int) or isinstance(born, bool):
        errors.append("identity.born: 缺少世界秒出生时刻（整数；纪元开始前为负数）")
    elif born > moment:
        errors.append("identity.born: 出生时刻不能晚于实例初始时刻")
    else:
        # 个体寿命覆盖（§十 残余）：卡片可声明自己的寿命形态，与种族带同一套形态校验
        own = identity.get("lifespan")
        if own is not None:
            errors.extend(lifespan_errors(own, "identity.lifespan"))
        died = identity.get("died")
        fixed_death = isinstance(died, int) and not isinstance(died, bool)
        if died is not None and not fixed_death:
            errors.append("identity.died: 必须是世界秒整数（固死）")
        elif fixed_death and died < born:
            errors.append("identity.died: 固死时刻不能早于出生时刻")
        # 有固死就按固死走，不再拿寿命带推相容性；否则用**生效形态**（卡片覆盖优先于种族带）
        if not fixed_death and (race is not None or isinstance(own, dict)):
            effective = effective_lifespan(identity, race)
            mode = effective.get("mode")
            if mode is None:
                max_years = effective.get("max_years")
                if seconds_per_year and isinstance(max_years, int) and born + max_years * seconds_per_year < moment:
                    source = (
                        "卡片覆盖"
                        if isinstance(own, dict) and own.get("max_years") is not None
                        else str(race.get("name"))
                    )
                    errors.append(
                        f"identity: 出生与寿命覆盖不相容（{source} 最长 {max_years} 年，初始时刻已超出）"
                    )

    sources = {s.get("id") for s in package.get("sources", []) if isinstance(s, dict)}
    for index, channel in enumerate(card.get("channels") or []):
        if not isinstance(channel, dict):
            errors.append(f"channels[{index}]: 条目必须是对象")
            continue
        if channel.get("source_id") not in sources:
            errors.append(f"channels[{index}].source_id: 渠道悬空 {channel.get('source_id')!r}")
        if not _text(channel.get("conditions")):
            errors.append(f"channels[{index}].conditions: 缺少可接触条件")
    if not (card.get("channels") or []):
        errors.append("channels: 至少声明一条信息渠道（可为『无』的显式声明）")

    mechanisms = {m.get("id") for m in (package.get("comms", {}).get("mechanisms") or []) if isinstance(m, dict)}
    comms = card.get("comms") or []
    if not comms:
        errors.append("comms: 必须声明与用户的双向联络方式")
    for index, item in enumerate(comms):
        if not isinstance(item, dict) or item.get("mechanism_id") not in mechanisms:
            errors.append(f"comms[{index}].mechanism_id: 联络机制不在世界包允许范围内")

    errors.extend(_validate_knowledge(card, package, moment=moment))
    errors.extend(_validate_cognition(card))
    errors.extend(_validate_units(card))
    errors.extend(_validate_intents(card, package, calendar))
    errors.extend(_validate_life(card, package, day=day))
    if not isinstance(card.get("first_contact"), dict):
        errors.append("first_contact: 缺少初见设定（姿态与意向）")
    return errors


def _validate_knowledge(card: dict[str, Any], package: dict[str, Any], *, moment: int) -> list[str]:
    entries = card.get("initial_knowledge")
    if not isinstance(entries, list):
        return ["initial_knowledge: 必须是列表"]
    pools = {
        "canon": {c.get("id") for c in package.get("canon", []) if isinstance(c, dict)},
        "narrative": {n.get("id") for n in package.get("narratives", []) if isinstance(n, dict)},
        "historiography": {h.get("id") for h in package.get("historiography", []) if isinstance(h, dict)},
    }
    errors: list[str] = []
    known_items = {h.get("id"): h for h in package.get("historiography", []) if isinstance(h, dict)}
    for index, entry in enumerate(entries):
        where = f"initial_knowledge[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{where}: 条目必须是对象")
            continue
        kind = entry.get("ref_type")
        if kind not in KNOWLEDGE_SOURCES:
            errors.append(f"{where}.ref_type: 必须是 {'/'.join(KNOWLEDGE_SOURCES)} 之一")
            continue
        if kind == "self":
            if not _text(entry.get("claim")):
                errors.append(f"{where}.claim: 自身经历必须写明内容")
            continue
        ref = entry.get("ref_id")
        if ref not in pools[kind]:
            errors.append(f"{where}.ref_id: 引用的{kind}条目不存在 {ref!r}")
            continue
        obtained = entry.get("obtained_at")
        if obtained is None or not isinstance(obtained, int) or obtained < 0:
            errors.append(f"{where}.obtained_at: 必须是非负世界秒获知时间")
        elif obtained > moment:
            errors.append(f"{where}.obtained_at: 获知时间晚于初始时刻")
        if kind == "canon":
            # 实情层条目必须由某一部传本收录，否则等于卡片自己给出一个世界没有的来源
            covering = [
                unit.get("title")
                for unit in known_items.values()
                if ref in (unit.get("entries") if isinstance(unit.get("entries"), list) else [])
            ]
            if not covering:
                errors.append(f"{where}.ref_id: 没有任何传本收录该实情条目 {ref!r}")
        if kind == "historiography":
            unit = known_items.get(ref) or {}
            compiled = unit.get("compiled_at") or unit.get("written_at")
            if isinstance(compiled, int) and isinstance(obtained, int) and obtained < compiled:
                errors.append(f"{where}: 史料获知早于成书（{unit.get('title')}）")
            # 必须明确掌握哪些条目：不能由职业或渠道资格自动获得整部史料（CHARACTER_CARD §5.2）
            scope = entry.get("scope")
            entries = unit.get("entries") if isinstance(unit.get("entries"), list) else []
            if not isinstance(scope, list) or not scope:
                errors.append(f"{where}.scope: 引用史料必须写明所掌握的条目或范围")
            else:
                outside = [item for item in scope if item not in entries]
                if outside:
                    errors.append(f"{where}.scope: 超出该传本的条目范围：{'、'.join(str(item) for item in outside)}")
    return errors


def _validate_cognition(card: dict[str, Any]) -> list[str]:
    cognition = card.get("cognition")
    if not isinstance(cognition, dict):
        return ["cognition: 缺少认知边界声明"]
    mode = cognition.get("mode")
    if mode not in ("soft", "hard"):
        return ["cognition.mode: 必须是 soft 或 hard"]
    sources = cognition.get("sources")
    if not isinstance(sources, list) or not sources:
        # 附录 A：不能只写一个 mode 而省掉过滤依据（soft 也要写清来源范围）
        return [
            "cognition.sources: 必须列出信息来源范围（soft / hard 都要写过滤依据）"
            if mode == "soft"
            else "cognition.sources: 硬约束必须列出允许的信息来源"
        ]
    errors: list[str] = []
    if mode == "hard":
        for index, source in enumerate(sources):
            if source not in HARD_ALLOWED:
                errors.append(f"cognition.sources[{index}]: 硬约束不得开放 {source!r}（仅限自身经历 / 小环境 / 用户通讯）")
        # 硬约束把自身经历挡在外面、卡片却带着自身经历条目：确认前就该挡住（§5.2）
        if "self_experience" not in sources:
            has_self = any(
                isinstance(entry, dict) and entry.get("ref_type") == "self"
                for entry in (card.get("initial_knowledge") or [])
            )
            if has_self:
                errors.append(
                    "cognition: 硬约束未开放 self_experience，但初始知识里有自身经历条目（自相矛盾）"
                )
    return errors


def _validate_intents(card: dict[str, Any], package: dict[str, Any], calendar: dict[str, Any]) -> list[str]:
    """打算：对象 / 依据 / 强度 / 目标时间窗 / 前置条件 / 受支持的行动效果（§11.3）。"""
    intents = card.get("intents")
    if intents is None:
        return []
    if not isinstance(intents, list):
        return ["intents: 必须是列表"]
    errors: list[str] = []
    day = int(calendar.get("day_seconds") or 0)
    known = _all_ids(package)
    for index, item in enumerate(intents):
        where = f"intents[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: 条目必须是对象")
            continue
        if not _text(item.get("object")):
            errors.append(f"{where}.object: 缺打算的对象")
        if not _text(item.get("basis")):
            errors.append(f"{where}.basis: 缺角色已知依据（打算必须有可知来源）")
        strength = item.get("strength")
        if not isinstance(strength, (int, float)) or not 0 <= float(strength) <= 1:
            errors.append(f"{where}.strength: 需要 0..1 的意向强度")
        window = item.get("window") if isinstance(item.get("window"), dict) else {}
        start, end = window.get("from"), window.get("to")
        if not isinstance(start, int) or not isinstance(end, int) or end <= start:
            errors.append(f"{where}.window: 需要 from < to 的世界秒时间窗")
        elif day and (start % day > day or end % day > day):
            errors.append(f"{where}.window: 时间窗必须能按锁定历法解释")
        for ref in item.get("preconditions") or []:
            if str(ref) not in known:
                errors.append(f"{where}.preconditions: 引用不存在的标识 {ref!r}")
        effect = item.get("effect")
        if not isinstance(effect, dict) or not _text(effect.get("kind")):
            errors.append(f"{where}.effect: 打算必须挂一个受支持的行动效果（没有可执行效果就不能提交事件）")
            continue
        if str(effect.get("kind")) not in SUPPORTED_EFFECTS:
            errors.append(f"{where}.effect.kind: 未支持的效果类型 {effect.get('kind')!r}")
        if effect.get("target") is not None and str(effect.get("target")) not in known:
            errors.append(f"{where}.effect.target: 指向未登记对象 {effect.get('target')!r}")
        if str(effect.get("expiry")) not in EXPIRY_KINDS:
            errors.append(f"{where}.effect.expiry: 必须是 {' / '.join(EXPIRY_KINDS)} 之一")
    return errors


def _validate_units(card: dict[str, Any]) -> list[str]:
    units = card.get("initial_units")
    if not isinstance(units, list) or not units:
        return ["initial_units: 至少一个初始性格单元"]
    errors: list[str] = []
    seen: set[str] = set()
    semantics: dict[str, str] = {}
    anchors = 0
    for index, unit in enumerate(units):
        where = f"initial_units[{index}]"
        if not isinstance(unit, dict):
            errors.append(f"{where}: 条目必须是对象")
            continue
        ident = unit.get("id")
        if not _text(ident):
            errors.append(f"{where}: 缺少稳定标识")
        elif ident in seen:
            errors.append(f"{where}: 标识重复 {ident}")
        else:
            seen.add(ident)
        if not _text(unit.get("semantic")):
            errors.append(f"{where}.semantic: 缺少语义")
        else:
            # 同一语义换 id 重复登记＝重复计权（§5.2）；确认前就挡住，别让运行期到场两次
            key = " ".join(str(unit["semantic"]).split())
            if key in semantics:
                errors.append(f"{where}.semantic: 与 {semantics[key]} 语义重复（同一句换名字重复计权）")
            else:
                semantics[key] = str(ident or where)
        if not _text(unit.get("basis")):
            errors.append(f"{where}.basis: 缺少驱动依据")
        driver = unit.get("driver")
        if driver not in DRIVERS:
            errors.append(f"{where}.driver: 必须是 {'/'.join(DRIVERS)} 之一")
            continue
        if driver == "anchor":
            anchors += 1
        confidence = unit.get("confidence")
        low, high = CONFIDENCE_BANDS[driver]
        if not isinstance(confidence, (int, float)) or not low <= float(confidence) <= high:
            errors.append(f"{where}.confidence: {driver} 初始置信度必须在 [{low}, {high}] 内")
    if anchors == 0:
        errors.append("initial_units: 至少一个锚点单元（初始辨识特征）")
    return errors


def _validate_life(card: dict[str, Any], package: dict[str, Any], *, day: int) -> list[str]:
    template = card.get("life_template")
    if not isinstance(template, dict):
        return ["life_template: 缺少生活线模板"]
    errors: list[str] = []
    if not isinstance(template.get("sleep"), bool):
        errors.append("life_template.sleep: 必须显式声明是否睡眠")
    windows = template.get("windows")
    if not isinstance(windows, list) or not windows:
        return errors + ["life_template.windows: 至少一个世界时间窗"]

    allowed = _allowed_activities(package, card)
    spans: list[tuple[int, int]] = []
    tails: list[tuple[int, int]] = []
    wrap_count = 0
    for index, window in enumerate(windows):
        where = f"life_template.windows[{index}]"
        if not isinstance(window, dict):
            errors.append(f"{where}: 条目必须是对象")
            continue
        start, end = window.get("start"), window.get("end")
        if not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
            errors.append(f"{where}: 需要 0 ≤ start < end 的世界秒区间")
            continue
        if day and start >= day:
            errors.append(f"{where}: 起点超出世界日（日长 {day}）")
            continue
        if day and end > day:
            wrap_count += 1
            if end > 2 * day:
                errors.append(f"{where}: 跨日窗口不得超过一个世界日")
                continue
            tails.append((0, end - day))  # 跨午夜后回到日首的尾巴
        spans.append((start, day if day and end > day else end))
        activity = window.get("activity")
        if not _text(activity):
            errors.append(f"{where}.activity: 缺少活动标识")
        elif allowed is not None and activity not in allowed:
            errors.append(f"{where}.activity: 活动 {activity!r} 未在世界包对应模板中声明")
        for alt in window.get("alternatives") or []:
            if allowed is not None and alt not in allowed:
                errors.append(f"{where}.alternatives: 允许变化 {alt!r} 未在世界包中声明")
    if wrap_count > 1:
        errors.append("life_template.windows: 最多一个窗口跨世界日")
    spans.sort()
    for previous, current in zip(spans, spans[1:]):
        if current[0] < previous[1]:
            errors.append(f"life_template.windows: 活动区间重叠 [{current[0]},{current[1]})")
    for tail_start, tail_end in tails:
        for span_start, span_end in spans:
            if span_start < tail_end and tail_start < span_end:
                errors.append(f"life_template.windows: 跨日窗口的日首段 [{tail_start},{tail_end}) 与其他活动重叠")
    if not template.get("routine_note") is None and not isinstance(template.get("routine_note"), str):
        errors.append("life_template.routine_note: 必须是文本")
    return errors


def _allowed_activities(package: dict[str, Any], card: dict[str, Any]) -> set[str] | None:
    """活动必须真实存在于世界包：挂角色模板时限于该模板，未挂模板时限于世界级合法活动集合。"""
    life = {t.get("id"): t for t in package.get("life", []) if isinstance(t, dict)}
    role_id = card.get("role_id")
    if role_id:
        for role in package.get("roles", []):
            if isinstance(role, dict) and role.get("id") == role_id:
                template = life.get(role.get("life_template")) or {}
                return {w.get("activity") for w in template.get("windows", []) if isinstance(w, dict)}
        return None
    # 未挂模板不等于可以自创活动（§5.2「引用的活动真实存在」）：仍要落在世界包声明过的活动里
    declared: set[str] = set()
    for template in life.values():
        for window in template.get("windows") or []:
            if isinstance(window, dict) and _text(window.get("activity")):
                declared.add(str(window["activity"]))
    return declared or None


def validate_assembly(package: dict[str, Any], cards: list[dict[str, Any]], *, moment: int) -> list[str]:
    """创建前联合校验（CHARACTER_CARD_SPEC §5.2）：未确认、悬空、越权、日程非法都在此挡住。"""
    errors: list[str] = []
    if not cards:
        errors.append("至少一名角色（实例创建前装配）")
        return errors
    seen: set[str] = set()
    for index, card in enumerate(cards):
        meta = card.get("meta") if isinstance(card, dict) else None
        if not isinstance(meta, dict) or meta.get("confirmed") is not True:
            errors.append(f"cards[{index}]: 角色卡未经用户确认（meta.confirmed）")
        card_id = (meta or {}).get("card_id")
        if not isinstance(card_id, str) or not card_id.strip():
            # 姓名只用于呈现；没有稳定标识的角色在实例里既记不了状态也取不回来（§4 / 附录 A）
            errors.append(f"cards[{index}]: 缺少稳定角色标识（meta.card_id）")
        elif card_id in seen:
            errors.append(f"cards[{index}]: 角色标识在实例内重复 {card_id}")
        else:
            seen.add(card_id)
        for message in validate_card(card, package, moment=moment):
            errors.append(f"cards[{index}]: {message}")
    if not any(card.get("initial_knowledge") for card in cards if isinstance(card, dict)):
        errors.append("装配校验：至少一名角色须凭初始知识 / 亲历 / 允许渠道接触一项世界内容")
    role_ids = {r.get("id") for r in package.get("roles", []) if isinstance(r, dict)}
    for index, card in enumerate(cards):
        role_id = card.get("role_id")
        if role_id is not None and role_id not in role_ids:
            errors.append(f"cards[{index}].role_id: 引用的角色模板不存在 {role_id!r}")
    return errors
