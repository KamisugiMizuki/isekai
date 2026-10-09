"""世界包校验：结构、唯一标识、引用闭包、历法合法性与双轨边界。

返回错误列表（空列表 = 通过）。规则对应 WORLD_SETTING_SPEC §2.2 与附录 D 的阻断项：
空壳内容、悬空引用、说法无来源、史料引用不存在的条目、历法不自洽都必须挡在创建之前。
"""

from __future__ import annotations

from typing import Any

from ..version import CAPABILITIES
from .package import DENSITIES, PACKAGE_SCHEMA_VERSION

SEGMENT_KEYS = ("id", "name", "start", "end")
LIFESPAN_MODES = ("long", "unbounded")
ENTITY_KINDS = ("person", "org", "place", "item")


def lifespan_errors(lifespan: Any, where: str) -> list[str]:
    """寿命声明的形态校验（种族默认与卡片个体覆盖**共用这一份**，§十 残余「寿命带形态」）。

    两种合法形态，不能混写：

    - `{"mode": "long"|"unbounded"}`：不据以推算寿终（`long` = 极长但本设定内不定年限）；
    - `{"min_years": n, "max_years": m}`：正整数年、`min ≤ max`，寿终按 `born + max 年` 推。
    """
    if not isinstance(lifespan, dict):
        return [f"{where}: 缺少寿命覆盖"]
    errors: list[str] = []
    mode = lifespan.get("mode")
    has_band = lifespan.get("min_years") is not None or lifespan.get("max_years") is not None
    if mode is not None:
        if mode not in LIFESPAN_MODES:
            errors.append(f"{where}.mode: 必须是 {'/'.join(LIFESPAN_MODES)} 之一")
        if has_band:
            errors.append(f"{where}: mode 与 min_years/max_years 不能同时声明（两种形态混写）")
        return errors
    low, high = lifespan.get("min_years"), lifespan.get("max_years")
    ok = (
        isinstance(low, int)
        and not isinstance(low, bool)
        and isinstance(high, int)
        and not isinstance(high, bool)
        and low > 0
        and high >= low
    )
    if not ok:
        errors.append(f"{where}: 需要 min_years ≤ max_years 的正整数年")
    return errors

#: 加载限额（§2.3）：超限明确拒绝，不静默裁掉设定
MAX_DEPTH = 12
MAX_NODES = 20000
MAX_STRING = 4000
MAX_COLLECTION = 500


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _str_value(value: Any) -> str:
    """取字符串值（_text 只回答「是不是非空字符串」，不返回值）。"""
    return value.strip() if isinstance(value, str) else ""


def _ids(items: Any) -> list[str]:
    return [
        str(item.get("id"))
        for item in (items if isinstance(items, list) else [])
        if isinstance(item, dict) and _text(item.get("id"))
    ]


def _check_unique(items: Any, where: str, errors: list[str]) -> None:
    seen: set[str] = set()
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            errors.append(f"{where}: 条目必须是对象")
            continue
        ident = item.get("id")
        if not _text(ident):
            errors.append(f"{where}: 缺少稳定标识 id")
            continue
        if ident in seen:
            errors.append(f"{where}: 标识重复 {ident}")
        seen.add(ident)


def _check_refs(values: Any, known: set[str], where: str, errors: list[str]) -> None:
    for value in values if isinstance(values, list) else []:
        if not _text(value):
            errors.append(f"{where}: 引用必须是非空标识")
        elif value not in known:
            errors.append(f"{where}: 引用不存在的标识 {value}")


def validate_package(package: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(package, dict):
        return ["世界包必须是 JSON 对象"]
    errors.extend(_check_limits(package))

    meta = package.get("meta")
    if not isinstance(meta, dict):
        errors.append("meta: 缺少 meta 段")
        meta = {}
    schema = meta.get("schema")
    if schema != PACKAGE_SCHEMA_VERSION:
        errors.append(f"meta.schema: 期望 {PACKAGE_SCHEMA_VERSION}，实际 {schema!r}")
    if not _text(meta.get("package_id")):
        errors.append("meta.package_id: 缺少稳定世界包标识")
    if not _text(meta.get("original_name")):
        errors.append("meta.original_name: 缺少原始世界包名称")
    if meta.get("density") not in DENSITIES:
        errors.append(f"meta.density: 必须是 {'/'.join(DENSITIES)} 之一")
    # 未知必需能力必须在确认前报错（§2.5）：包声明它需要的运行能力，本端不认识就拒绝
    requires = meta.get("requires", [])
    if not isinstance(requires, list):
        errors.append("meta.requires: 必须是能力标识列表")
    else:
        unknown = [item for item in requires if item not in CAPABILITIES]
        if unknown:
            errors.append("meta.requires: 本端尚不支持的能力：" + "、".join(str(item) for item in unknown))

    _validate_calendar(package.get("calendar"), errors)
    _validate_world(package.get("world"), errors)
    _validate_environment(package.get("environment"), errors)
    known = _validate_canon_sources(package, errors)
    _validate_races_entities(package, errors)
    _validate_institution_refs(package, errors)
    _validate_historiography(package, known, errors)
    # 在册标识集合（含 region）只重建一次往下传（P1-14：同一函数内不重复重建）
    registered = _all_ids(package)
    _validate_events(package, known, errors, registered=registered)
    _validate_event_calendar(package, errors)
    _validate_environment_observers(package, errors, registered=registered)
    _validate_life_roles(package, errors)
    _validate_initial_state(package, known, errors)
    return errors


def _check_limits(package: dict[str, Any]) -> list[str]:
    """加载限额：嵌套深度、节点数、单条文本长度、集合长度（§2.3）。"""
    errors: list[str] = []
    stack: list[tuple[Any, int, str]] = [(package, 1, "")]
    nodes = 0
    while stack:
        node, depth, path = stack.pop()
        nodes += 1
        if nodes > MAX_NODES:
            return errors + [f"世界包节点数超过上限 {MAX_NODES}（加载限额）"]
        if depth > MAX_DEPTH:
            return errors + [f"{path}: 嵌套深度超过上限 {MAX_DEPTH}（加载限额）"]
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}" if path else str(key)
                if isinstance(value, str) and len(value) > MAX_STRING:
                    errors.append(f"{here}: 文本长度 {len(value)} 超过上限 {MAX_STRING}（加载限额）")
                stack.append((value, depth + 1, here))
        elif isinstance(node, list):
            if len(node) > MAX_COLLECTION:
                errors.append(f"{path}: 条目数 {len(node)} 超过上限 {MAX_COLLECTION}（加载限额）")
            for index, value in enumerate(node):
                stack.append((value, depth + 1, f"{path}[{index}]"))
    return errors


def _validate_calendar(calendar: Any, errors: list[str]) -> None:
    if not isinstance(calendar, dict):
        errors.append("calendar: 缺少历法段")
        return
    if not _text(calendar.get("era")):
        errors.append("calendar.era: 缺少纪元名称")
    day = calendar.get("day_seconds")
    if not isinstance(day, int) or day <= 0:
        errors.append("calendar.day_seconds: 必须是正整数的世界日长（世界秒）")
        day = 0
    months = calendar.get("months")
    if not isinstance(months, list) or not months:
        errors.append("calendar.months: 至少一个月")
    else:
        for index, month in enumerate(months):
            if not isinstance(month, dict) or not _text(month.get("name")):
                errors.append(f"calendar.months[{index}]: 缺少月份名称")
            days = month.get("days") if isinstance(month, dict) else None
            if not isinstance(days, int) or days <= 0:
                errors.append(f"calendar.months[{index}].days: 必须是正整数")
    week = calendar.get("week")
    if week is not None:
        if not isinstance(week, dict) or not isinstance(week.get("days"), int) or week.get("days", 0) <= 0:
            errors.append("calendar.week.days: 必须是正整数")
    segments = calendar.get("segments")
    if not isinstance(segments, list) or not segments:
        errors.append("calendar.segments: 至少一个昼夜时段")
    else:
        _check_unique(segments, "calendar.segments", errors)
        cursor = 0
        for index, segment in enumerate(segments):
            if not isinstance(segment, dict):
                errors.append(f"calendar.segments[{index}]: 条目必须是对象")
                continue
            start, end = segment.get("start"), segment.get("end")
            if not isinstance(start, int) or not isinstance(end, int):
                errors.append(f"calendar.segments[{index}]: start/end 必须是世界秒整数")
                continue
            if start < 0 or end <= start or (day and end > day):
                errors.append(f"calendar.segments[{index}]: 区间 [{start},{end}) 不在世界日内")
                continue
            if start != cursor and index > 0:
                errors.append(f"calendar.segments[{index}]: 时段不连续或有重叠（应为 {cursor}）")
            cursor = end
        if day and segments and cursor != day:
            errors.append(f"calendar.segments: 时段未覆盖整日（止于 {cursor}，日长 {day}）")
    moment = calendar.get("initial_moment")
    if not isinstance(moment, int) or moment < 0:
        errors.append("calendar.initial_moment: 必须是非负世界秒")


def _validate_world(world: Any, errors: list[str]) -> None:
    if not isinstance(world, dict):
        errors.append("world: 缺少世界段")
        return
    axioms = world.get("axioms")
    if not isinstance(axioms, list) or not axioms:
        errors.append("world.axioms: 至少一条世界公理（阻断项）")
    else:
        _check_unique(axioms, "world.axioms", errors)
        for index, axiom in enumerate(axioms):
            if isinstance(axiom, dict) and not _text(axiom.get("text")):
                errors.append(f"world.axioms[{index}]: 公理内容为空")
    if not _text(world.get("geography")):
        errors.append("world.geography: 缺少世界地理与空间边界说明")
    if not _text(world.get("society")):
        errors.append("world.society: 缺少社会结构说明")
    lexicon = world.get("lexicon")
    terms = lexicon.get("terms") if isinstance(lexicon, dict) else None
    if not isinstance(terms, list) or not terms:
        errors.append("world.lexicon.terms: 至少一条命名语汇（阻断项）")
    else:
        for index, term in enumerate(terms):
            if not isinstance(term, dict) or not _text(term.get("term")):
                errors.append(f"world.lexicon.terms[{index}]: 缺少词条")
    for key in ("institutions", "customs"):
        items = world.get(key)
        if items is None:
            continue
        if not isinstance(items, list):
            errors.append(f"world.{key}: 必须是列表")
            continue
        _check_unique(items, f"world.{key}", errors)
        for index, item in enumerate(items):
            if not isinstance(item, dict):
                errors.append(f"world.{key}[{index}]: 条目必须是对象")
                continue
            where = f"world.{key}[{index}]"
            if not _text(item.get("name")):
                errors.append(f"{where}: 缺少名称")
            # 制度：职权 / 适用 / 延续与承接；惯例：适用群体 / 做法 / 形成依据 / 允许变化范围
            if key == "institutions":
                for field, hint in (("mandate", "职权"), ("scope", "适用范围"), ("succession", "延续与承接规则")):
                    if not _text(item.get(field)):
                        errors.append(f"{where}.{field}: 已声明制度必须写明{hint}")
                _validate_offices(item, where, errors)
            else:
                for field, hint in (
                    ("applies_to", "适用群体"),
                    ("practice", "当前做法"),
                    ("basis", "形成依据"),
                    ("variation", "允许变化范围"),
                ):
                    if not _text(item.get(field)):
                        errors.append(f"{where}.{field}: 已声明惯例必须写明{hint}")
                _validate_custom_forms(item, where, errors)
    _validate_regions(world.get("regions"), errors)


def _validate_regions(regions: Any, errors: list[str]) -> None:
    """区域登记（§2.2 / 附录 A，P2-11）：稳定标识 + 名称（可选描述），同集合内唯一。

    未声明 `regions` 或声明为空列表 = 本包不使用区域标签，不是错误；一旦声明，
    每一项都要有标识与名称，且标识在集合内唯一。
    """
    if regions is None:
        return
    if not isinstance(regions, list):
        errors.append("world.regions: 必须是列表")
        return
    _check_unique(regions, "world.regions", errors)
    known_ids = set(_ids(regions))
    #: B-4：拓扑只允许「邻接 + 通行档 + 代价」，**任何坐标 / 距离类字段一律拒绝**（防「拓扑 → 路网」滑坡）
    coordinate_like = {"x", "y", "lat", "lon", "lng", "坐标", "distance", "距离", "position", "位置"}
    for index, region in enumerate(regions):
        if not isinstance(region, dict):
            errors.append(f"world.regions[{index}]: 条目必须是对象")
            continue
        if not _text(region.get("name")):
            errors.append(f"world.regions[{index}].name: 缺少区域名称")
        bad_keys = sorted(set(region) & coordinate_like)
        if bad_keys:
            errors.append(
                f"world.regions[{index}]: 不允许坐标 / 距离类字段 {bad_keys}；"
                "B-4 是拓扑（邻接 + 通行档 + 代价），不是坐标或路网系统"
            )
        adjacent = region.get("adjacent")
        if adjacent is None:
            continue
        if not isinstance(adjacent, list):
            errors.append(f"world.regions[{index}].adjacent: 必须是列表")
            continue
        for a_index, edge in enumerate(adjacent):
            where_a = f"world.regions[{index}].adjacent[{a_index}]"
            if not isinstance(edge, dict):
                errors.append(f"{where_a}: 条目必须是对象")
                continue
            extra = sorted(set(edge) - {"to", "通行", "代价", "kind", "cost"})
            if extra:
                errors.append(
                    f"{where_a}: 只允许 to / 通行 / 代价（收到 {extra}）；拓扑不承载坐标、距离或路径"
                )
            to_id = str(edge.get("to") or "")
            if not to_id:
                errors.append(f"{where_a}.to: 缺少相邻区域标识")
            elif to_id not in known_ids:
                errors.append(f"{where_a}.to: 指向未登记区域 {to_id!r}（拓扑必须是引用闭集）")
            if to_id == str(region.get("id") or ""):
                errors.append(f"{where_a}.to: 不得指向自身")
            kind = str(edge.get("通行") or edge.get("kind") or "可通行")
            if kind not in ADJACENCY_KINDS:
                errors.append(f"{where_a}.通行: 必须是 {' / '.join(ADJACENCY_KINDS)} 之一")
            cost = edge.get("代价") if edge.get("代价") is not None else edge.get("cost")
            if cost is None:
                cost = 1
            if isinstance(cost, bool) or not isinstance(cost, int) or not (1 <= cost <= 3):
                errors.append(f"{where_a}.代价: 必须是 1…3 的整数（代价档，不是连续距离）")


def region_ids(package: dict[str, Any]) -> set[str]:
    """已登记区域标识（`world.regions[].id`）：卡片 `region` 只引用这些（P2-11）。"""
    world = package.get("world") if isinstance(package.get("world"), dict) else {}
    return set(_ids(world.get("regions")))


def _validate_offices(institution: dict[str, Any], where: str, errors: list[str]) -> None:
    """职位与空缺规则：职位标识唯一、在任者可空（= 空缺）；空缺期间事务必须可判定。

    在任者必须指向**已登记的实体**（与 `change_allowed` 对制度状态效果的同一把尺）；
    真的出现空缺时，`vacancy_policy` 不能两条都空——否则「空缺期间事务如何继续」无法被一致解释。
    """
    offices = institution.get("offices")
    if offices is None:
        return
    if not isinstance(offices, list):
        errors.append(f"{where}.offices: 必须是列表")
        return
    seen: set[str] = set()
    for index, office in enumerate(offices):
        spot = f"{where}.offices[{index}]"
        if not isinstance(office, dict):
            errors.append(f"{spot}: 条目必须是对象")
            continue
        office_id = _str_value(office.get("id"))
        if not office_id:
            errors.append(f"{spot}: 缺少职位标识")
        elif office_id in seen:
            errors.append(f"{spot}.id: 职位标识重复 {office_id!r}")
        else:
            seen.add(office_id)
        if not _text(office.get("name")):
            errors.append(f"{spot}.name: 缺少职位名称")
        holder = office.get("holder")
        if holder is not None and not isinstance(holder, str):
            errors.append(f"{spot}.holder: 在任者必须是登记实体标识或留空")
    policy = institution.get("vacancy_policy")
    if policy is None:
        return
    if not isinstance(policy, dict):
        errors.append(f"{where}.vacancy_policy: 必须是对象")
        return
    for key in ("continues", "suspended"):
        items = policy.get(key)
        if items is None:
            errors.append(f"{where}.vacancy_policy.{key}: 空缺规则必须显式写明（可空列表，但不能缺）")
            continue
        if not isinstance(items, list) or any(not _text(entry) for entry in items):
            errors.append(f"{where}.vacancy_policy.{key}: 必须是事务名列表")
    vacant = any(
        isinstance(office, dict) and not str(office.get("holder") or "").strip()
        for office in offices
    )
    if vacant and not (policy.get("continues") or policy.get("suspended")):
        errors.append(
            f"{where}.vacancy_policy: 有职位空缺，但『照旧』与『暂停』都是空的——空缺期间的事务无法被一致解释"
        )


def _validate_custom_forms(custom: dict[str, Any], where: str, errors: list[str]) -> None:
    """惯例的可选变化范围：现行做法必须在范围内。"""
    forms = custom.get("forms")
    if forms is None:
        return
    if not isinstance(forms, list) or any(not _text(entry) for entry in forms):
        errors.append(f"{where}.forms: 必须是做法名列表")
        return
    practice = _str_value(custom.get("practice"))
    if practice and practice not in [str(entry) for entry in forms]:
        errors.append(f"{where}.practice: 现行做法必须落在允许变化范围 forms 内")


def office_index(package: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """职位扁平索引：职位标识 → 职位（含所属制度与空缺期间的规则）。"""
    world = package.get("world") if isinstance(package.get("world"), dict) else {}
    out: dict[str, dict[str, Any]] = {}
    for institution in world.get("institutions") or []:
        if not isinstance(institution, dict):
            continue
        inst_id = _str_value(institution.get("id"))
        policy = institution.get("vacancy_policy") if isinstance(institution.get("vacancy_policy"), dict) else {}
        for office in institution.get("offices") or []:
            if not isinstance(office, dict) or not _text(office.get("id")):
                continue
            out[str(office["id"])] = {
                **office,
                "institution_id": inst_id,
                "institution_name": str(institution.get("name") or inst_id),
                "continues": [str(entry) for entry in policy.get("continues") or []],
                "suspended": [str(entry) for entry in policy.get("suspended") or []],
            }
    return out


def custom_index(package: dict[str, Any]) -> dict[str, dict[str, Any]]:
    world = package.get("world") if isinstance(package.get("world"), dict) else {}
    return {
        str(item["id"]): item
        for item in world.get("customs") or []
        if isinstance(item, dict) and item.get("id")
    }


def change_allowed(package: dict[str, Any], *, kind: str, target: str, value: str) -> tuple[bool, str]:
    """变化是否落在声明范围内（阶段 6：不能由文本凭空增删制度与公理）。"""
    if kind == "institution_state":
        if office_index(package).get(str(target)) is None:
            return False, "未声明的职位"
        if str(value) and str(value) not in _entity_ids(package):
            return False, "在任者不是已登记的实体"
        return True, ""
    if kind == "custom_state":
        custom = custom_index(package).get(str(target))
        if custom is None:
            return False, "未声明的惯例"
        forms = [str(entry) for entry in custom.get("forms") or []]
        if str(value) not in forms:
            return False, "做法不在声明的允许变化范围内"
        return True, ""
    return False, "不是制度或惯例类变化"


def _entity_ids(package: dict[str, Any]) -> set[str]:
    return {
        _str_value(item.get("id"))
        for item in package.get("entities") or []
        if isinstance(item, dict) and item.get("id")
    }


def _validate_environment(environment: Any, errors: list[str]) -> None:
    if environment is None:
        return
    if not isinstance(environment, dict):
        errors.append("environment: 必须是对象")
        return
    types = environment.get("types", [])
    if not isinstance(types, list):
        errors.append("environment.types: 必须是列表")
        return
    _check_unique(types, "environment.types", errors)
    for index, item in enumerate(types):
        if not isinstance(item, dict):
            continue
        where = f"environment.types[{index}]"
        if not _text(item.get("name")):
            errors.append(f"{where}: 缺少名称")
        if "initial" not in item:
            errors.append(f"{where}: 缺少初始值")
        if not _text(item.get("unit")):
            errors.append(f"{where}.unit: 已声明的环境类型必须写明单位")
        if not isinstance(item.get("values"), list) or not item.get("values"):
            errors.append(f"{where}.values: 已声明的环境类型必须写明取值域")
        if not _text(item.get("observe")):
            errors.append(f"{where}.observe: 已声明的环境类型必须写明观察条件")
        if not isinstance(item.get("expiry"), (str, list)) or not item.get("expiry"):
            errors.append(f"{where}.expiry: 缺少失效方式")
        if not isinstance(item.get("scope"), (str, list)) or not item.get("scope"):
            errors.append(f"{where}: 缺少作用范围声明")
        sources = item.get("sources")
        if not isinstance(sources, list) or not sources:
            errors.append(f"{where}: 缺少变化来源")


def _validate_canon_sources(package: dict[str, Any], errors: list[str]) -> set[str]:
    """实情层与说法层分开校验：说法必须有来源与获知条件，引用必须闭合。"""
    sources = package.get("sources")
    if not isinstance(sources, list):
        errors.append("sources: 缺少信息来源列表")
        sources = []
    _check_unique(sources, "sources", errors)
    for index, source in enumerate(sources):
        if isinstance(source, dict) and not _text(source.get("reach")):
            errors.append(f"sources[{index}].reach: 缺少接触条件")

    canon = package.get("canon")
    if not isinstance(canon, list):
        errors.append("canon: 缺少实情层条目列表")
        canon = []
    _check_unique(canon, "canon", errors)
    for index, item in enumerate(canon):
        if isinstance(item, dict) and not _text(item.get("statement")):
            errors.append(f"canon[{index}].statement: 实情内容为空")

    canon_ids = set(_ids(canon))
    source_ids = set(_ids(sources))
    narratives = package.get("narratives")
    if not isinstance(narratives, list):
        errors.append("narratives: 缺少说法层条目列表")
        narratives = []
    _check_unique(narratives, "narratives", errors)
    for index, item in enumerate(narratives):
        if not isinstance(item, dict):
            continue
        where = f"narratives[{index}]"
        if not _text(item.get("text")):
            errors.append(f"{where}.text: 说法内容为空")
        if item.get("source_id") is None:
            errors.append(f"{where}.source_id: 说法必须有来源（不得凭空流传）")
        else:
            _check_refs([item.get("source_id")], source_ids, f"{where}.source_id", errors)
        obtain = item.get("obtain")
        if not isinstance(obtain, list) or not obtain:
            errors.append(f"{where}.obtain: 必须声明获知条件")
        ref = item.get("canon_ref")
        if ref is not None:
            _check_refs([ref], canon_ids, f"{where}.canon_ref", errors)
    return canon_ids | set(_ids(narratives))


def _validate_races_entities(package: dict[str, Any], errors: list[str]) -> None:
    races = package.get("races")
    if not isinstance(races, list) or not races:
        errors.append("races: 至少一个种族（个体寿命覆盖的来源）")
        races = []
    _check_unique(races, "races", errors)
    for index, race in enumerate(races):
        if not isinstance(race, dict):
            continue
        if not _text(race.get("name")):
            errors.append(f"races[{index}]: 缺少种族名称")
        lifespan = race.get("lifespan")
        if not isinstance(lifespan, dict):
            errors.append(f"races[{index}].lifespan: 缺少寿命覆盖")
            continue
        errors.extend(lifespan_errors(lifespan, f"races[{index}].lifespan"))
    race_ids = set(_ids(races))

    # B-2：关系声明（可选段）。轴是闭集；强度用**档位**而不是自由数值（可解释、可校验）。
    # v1 只声明**初始关系**；由事件改变关系属 v2（需要效果 schema 承载 主体/对方/轴/档位）。
    relations = package.get("relations")
    if relations is not None:
        if not isinstance(relations, dict):
            errors.append("relations: 必须是映射（axes + initial）")
        else:
            extra_keys = sorted(set(relations) - {"axes", "initial"})
            if extra_keys:
                errors.append(f"relations: 只允许 axes / initial（收到 {extra_keys}）")
            axes = relations.get("axes")
            if not isinstance(axes, list) or not axes:
                errors.append("relations.axes: 至少声明一个启用的关系轴")
                axes = []
            for axis in axes:
                if str(axis) not in RELATION_AXES:
                    errors.append(f"relations.axes: 未知关系轴 {axis!r}（可用：{' / '.join(RELATION_AXES)}）")
            initial = relations.get("initial")
            if initial is not None and not isinstance(initial, list):
                errors.append("relations.initial: 必须是列表")
                initial = []
            for index, item in enumerate(initial or []):
                where_r = f"relations.initial[{index}]"
                if not isinstance(item, dict):
                    continue
                for field_name in ("from", "to", "axis"):
                    if not _text(item.get(field_name)):
                        errors.append(f"{where_r}.{field_name}: 缺少标识")
                if str(item.get("axis") or "") and str(item.get("axis")) not in [str(a) for a in axes]:
                    errors.append(f"{where_r}.axis: 该轴未在 relations.axes 里启用")
                grade = str(item.get("档位") or item.get("grade") or "中")
                if grade not in RELATION_GRADES:
                    errors.append(
                        f"{where_r}.档位: 必须是 {' / '.join(RELATION_GRADES)} 之一（强度用档位，不用自由数值）"
                    )
                extra = sorted(set(item) - {"from", "to", "axis", "档位", "grade"})
                if extra:
                    errors.append(f"{where_r}: 只允许 from / to / axis / 档位（收到 {extra}）")

    # B-3+B-9：域外账本声明（可选段）。只允许**声明式键**——不允许临场发明计数器。
    # v1 只允许存储键；v2 另允许 `derived[]` 里的**声明式推导键**（它们**不落库**，只是只读派生视图）。
    ledger = package.get("ledger")
    if ledger is not None:
        if not isinstance(ledger, list):
            errors.append("ledger: 必须是列表")
            ledger = []
        for index, item in enumerate(ledger):
            where_l = f"ledger[{index}]"
            if not isinstance(item, dict):
                continue
            if str(item.get("scope_kind") or "") not in ("region", "org", "institution"):
                errors.append(f"{where_l}.scope_kind: 必须是 region / org / institution 之一")
            if not _text(item.get("scope_id")):
                errors.append(f"{where_l}.scope_id: 缺少作用域标识")
            if not _text(item.get("key")):
                errors.append(f"{where_l}.key: 缺少计数器 / 比率名")
            initial = item.get("初始值") if item.get("初始值") is not None else item.get("initial")
            if initial is None or isinstance(initial, bool) or not isinstance(initial, int):
                errors.append(f"{where_l}.初始值: 必须是整数（账本是计数器与比率，不存文本）")
            extra = sorted(set(item) - {"scope_kind", "scope_id", "key", "初始值", "initial", "单位", "unit"})
            if extra:
                errors.append(
                    f"{where_l}: 只允许 scope_kind / scope_id / key / 初始值 / 单位（收到 {extra}）；"
                    "账本存储键的数字只能由声明的键承载（推导值请放 derived[]，且它们不落库）"
                )

    # B-3+B-9 v2：账本**推导式**（可选段）。只做**声明式纯函数**，算子闭集，操作数必须是已声明的键。
    _validate_ledger_derived(package, errors)

    # B-1：压力量声明（可选段）。v1 只支持「自然变化」这一来源，且**权重形式写死**。
    pressures = package.get("pressures")
    pressure_ids: set[str] = set()
    if pressures is not None:
        if not isinstance(pressures, list):
            errors.append("pressures: 必须是列表")
            pressures = []
        _check_unique(pressures, "pressures", errors)
        for index, item in enumerate(pressures):
            where_p = f"pressures[{index}]"
            if not isinstance(item, dict):
                continue
            ident = str(item.get("id") or "")
            if not _text(ident):
                errors.append(f"{where_p}.id: 缺少标识")
            else:
                pressure_ids.add(ident)
            if not _text(item.get("name")):
                errors.append(f"{where_p}.name: 缺少名称")
            low = item.get("下限")
            high = item.get("上限")
            base = item.get("初始值")
            for field_name, value in (("下限", low), ("上限", high), ("初始值", base)):
                if value is None or isinstance(value, bool) or not isinstance(value, int):
                    errors.append(f"{where_p}.{field_name}: 必须是整数")
            if isinstance(low, int) and isinstance(high, int) and not isinstance(low, bool) \
                    and not isinstance(high, bool) and high < low:
                errors.append(f"{where_p}: 上限不得小于下限（{high} < {low}）")
            if isinstance(base, int) and isinstance(low, int) and isinstance(high, int) \
                    and not any(isinstance(x, bool) for x in (base, low, high)) \
                    and not (low <= base <= high):
                errors.append(f"{where_p}.初始值: 必须落在一开始声明的区间内（{low}…{high}）")
            drift = item.get("drift")
            if drift is not None and (isinstance(drift, bool) or not isinstance(drift, int)):
                errors.append(f"{where_p}.drift: 必须是整数（每个世界日的自然变化量）")

    entities = package.get("entities")
    if not isinstance(entities, list):
        errors.append("entities: 缺少名册列表")
        entities = []
    elif not entities:
        # 名册是结构引用的落点（在任者、效果目标都指向这里）：空名册让引用无从闭合
        errors.append("entities: 至少一个登记人物（名册；制度在任者与结构引用要闭合到它）")
    _check_unique(entities, "entities", errors)
    for index, item in enumerate(entities):
        if not isinstance(item, dict):
            continue
        where = f"entities[{index}]"
        if not _text(item.get("name")):
            errors.append(f"{where}: 缺少名称")
        if item.get("kind") not in ENTITY_KINDS:
            errors.append(f"{where}.kind: 必须是 {'/'.join(ENTITY_KINDS)} 之一")
        if item.get("race_id") is not None:
            _check_refs([item.get("race_id")], race_ids, f"{where}.race_id", errors)
        # A-9c（**向后不兼容的校验收紧**，见任务清单 A-9c 的登记）：`born` / `died` 原先不被校验，
        # 但运行期把它们当真值消费——`service` 据此推出身故事件（名册实体的寿终）与年龄。
        # 于是「包校验放行 → 运行期当真值用」：`born: "很久以前"` 这类值会让年龄算出垃圾。
        # 现在要求：可选、但必须是整数世界秒；`died` 不得早于 `born`。
        for field in ("born", "died"):
            value = item.get(field)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, int):
                errors.append(f"{where}.{field}: 必须是整数世界秒（缺省表示未登记）")
        born, died = item.get("born"), item.get("died")
        if isinstance(born, int) and isinstance(died, int) and not isinstance(born, bool) \
                and not isinstance(died, bool) and died < born:
            errors.append(f"{where}.died: 不得早于 born（{died} < {born}）")


def _validate_historiography(package: dict[str, Any], known: set[str], errors: list[str]) -> None:
    units = package.get("historiography")
    if not isinstance(units, list):
        errors.append("historiography: 缺少史料列表")
        return
    if not units:
        # 附录 D：至少一部有效传本（阻断项）——空壳计数不算数
        errors.append("historiography: 至少一部有效传本（阻断项）")
        return
    _check_unique(units, "historiography", errors)
    for index, unit in enumerate(units):
        if not isinstance(unit, dict):
            continue
        where = f"historiography[{index}]"
        if not _text(unit.get("title")):
            errors.append(f"{where}.title: 缺少标题")
        contributors = unit.get("contributors")
        if not isinstance(contributors, list) or not contributors:
            errors.append(f"{where}.contributors: 缺少贡献者与角色时段（阻断项）")
        else:
            for c_index, person in enumerate(contributors):
                if not isinstance(person, dict):
                    errors.append(f"{where}.contributors[{c_index}]: 条目必须是对象")
                    continue
                if not _text(person.get("name")):
                    errors.append(f"{where}.contributors[{c_index}]: 缺少贡献者")
                if not _text(person.get("role")):
                    errors.append(f"{where}.contributors[{c_index}].role: 缺少贡献角色")
                if not _text(person.get("period")):
                    errors.append(f"{where}.contributors[{c_index}].period: 缺少贡献时段")
        coverage = unit.get("coverage")
        if not isinstance(coverage, dict):
            errors.append(f"{where}.coverage: 缺少覆盖区间")
        else:
            start, end = coverage.get("from"), coverage.get("to")
            if not isinstance(start, int) or not isinstance(end, int) or end < start or start < 0:
                errors.append(f"{where}.coverage: 需要 0 ≤ from ≤ to 的世界秒区间")
        for key in ("written_at", "compiled_at"):
            value = unit.get(key)
            if value is not None and (not isinstance(value, int) or value < 0):
                errors.append(f"{where}.{key}: 必须是非负世界秒")
        entries = unit.get("entries")
        if not isinstance(entries, list) or not entries:
            errors.append(f"{where}.entries: 史料须有条目范围，不能是空壳（阻断项）")
        else:
            _check_refs(entries, known, f"{where}.entries", errors)


def _validate_institution_refs(package: dict[str, Any], errors: list[str]) -> None:
    """制度/惯例的跨结构一致性（§十 残余「与既有史料、节庆与生活模板的对齐」）。

    - **职位标识全包唯一**：跨制度重名会让 `office_index` 静默覆盖，同一个 id 出现两套归属；
    - **在任者必须是已登记实体**（空串 / 缺省 = 空缺，合法）；
    - 效果目标等结构引用走 `_all_ids`（已含制度 / 职位 / 惯例），所以节庆模板与史料条目
      对 `institution_state` / `custom_state` 的引用天然闭合到这里声明的范围。
    """
    world = package.get("world") if isinstance(package.get("world"), dict) else {}
    entities = _entity_ids(package)
    if not entities:
        # 分段生成时名册段还没落地：这一条是跨段一致性，等名册在了再判（空名册本身由名册段报错）
        return
    seen: dict[str, str] = {}
    for index, institution in enumerate(world.get("institutions") or []):
        if not isinstance(institution, dict):
            continue
        where = f"world.institutions[{index}]"
        institution_id = _str_value(institution.get("id"))
        for o_index, office in enumerate(institution.get("offices") or []):
            if not isinstance(office, dict):
                continue
            office_id = _str_value(office.get("id"))
            if office_id:
                owner = seen.get(office_id)
                if owner is not None and owner != institution_id:
                    errors.append(
                        f"{where}.offices[{o_index}].id: 职位标识 {office_id!r} 已在制度 {owner!r} 里用过"
                        "（跨制度重名会让同一 id 有两套归属）"
                    )
                seen.setdefault(office_id, institution_id)
            holder = str(office.get("holder") or "").strip()
            if holder and holder not in entities:
                errors.append(f"{where}.offices[{o_index}].holder: 在任者不是已登记的实体 {holder!r}")


def _all_ids(package: dict[str, Any]) -> set[str]:
    """包内全部稳定标识：效果目标等结构引用只允许指向这些（附录 C #10）。

    已登记区域（`world.regions[]`）也是在册对象的一种（§2.2 / 附录 A，P2-11）：
    效果 `target` 引用区域标识时在这里闭合。
    """
    found: set[str] = set()
    for key in ("sources", "canon", "narratives", "entities", "races", "life", "roles", "historiography"):
        items = package.get(key)
        if isinstance(items, list):
            found |= set(_ids(items))
    world = package.get("world")
    if isinstance(world, dict):
        for key in ("axioms", "institutions", "customs", "regions"):
            items = world.get(key)
            if isinstance(items, list):
                found |= set(_ids(items))
        for institution in world.get("institutions") or []:
            if isinstance(institution, dict):
                found |= set(_ids(institution.get("offices")))
        lexicon = world.get("lexicon")
        if isinstance(lexicon, dict):
            found |= set(_ids(lexicon.get("terms")))
    environment = package.get("environment")
    if isinstance(environment, dict):
        found |= set(_ids(environment.get("types")))
    comms = package.get("comms")
    if isinstance(comms, dict):
        found |= set(_ids(comms.get("mechanisms")))
    events = package.get("events")
    if isinstance(events, dict):
        families = events.get("families")
        if isinstance(families, list):
            found |= set(_ids(families))
            for family in families:
                if isinstance(family, dict):
                    found |= set(_ids(family.get("templates")))
    return found


#: 事件密度档 → 每历法日的目标区间（上限是硬预算，下限是「有合法来源时」的目标）
DENSITY_TARGETS: dict[str, tuple[int, int]] = {"稀疏": (0, 1), "常规": (1, 3), "丰盛": (2, 5)}

#: 效果失效方式（EVENT_ENGINE_SPEC §二：三类须可区分）
EXPIRY_KINDS: tuple[str, ...] = ("with_cause", "until_cleared", "natural_recovery")

#: B-5：身体后果的**枚举档位**（人类裁决 2026-10-10）。
#: `死亡` 也在列：它的消费路径（死亡事件与 `character_state.archived` **同批原子**）已落地，
#: 复用既有寿终路径的形状，因此不再是「声明了没人消费」的空效果。
CASUALTY_GRADES: tuple[str, ...] = ("轻伤", "重伤", "失能", "死亡")

#: B-2：关系轴闭集与「档位 → 千分比强度」映射。**与 `store.Store` 上的同名常量保持一致**：
#: 包校验与运行期必须共用一这份声明，否则会出现「校验放行、运行期按别的表取值」。
#: B-4：通行档闭集（相邻 / 可通行 / 受阻）。**只有档位与代价档，没有连续距离。**
ADJACENCY_KINDS: tuple[str, ...] = ("相邻", "可通行", "受阻")

RELATION_AXES: tuple[str, ...] = ("亲属", "同僚", "恩情", "债务", "宿怨", "隶属")
RELATION_GRADES: dict[str, int] = {"淡": 200, "中": 500, "深": 800, "极": 1000}

#: 首版受支持的事实效果闭集（EVENT_ENGINE_SPEC §六 / §十一）：未声明的类型必须被拒绝，
#: 不能让生成器临场发明数值系统或未被支持的效果。
SUPPORTED_EFFECTS: dict[str, str] = {
    "source_delay": "渠道受阻",
    "route_blocked": "通行受阻",
    "activity_constraint": "活动受限",
    "public_notice": "公开通告",
    "rumor_spread": "风闻流传",
    "institution_state": "制度状态（只改已声明的职位与在任者）",
    # B-5：身体类后果只按题材选枚举档位，不建立任何数值型健康量（见 EVENT_ENGINE_SPEC §六 档位纪律）
    "casualty": "身体后果（枚举档位：轻伤 / 重伤 / 失能 / 死亡）",
    "custom_state": "文化惯例的现行做法（须落在声明的允许范围内）",
    "environment_state": "环境状态（只改已声明的环境类型与取值域）",
    # B-1 v2：由事件效果改变压力量（**整数增量**，只作用于已声明的压力量；无表达式入口）
    "pressure_change": "压力变化（整数增量，只作用于已声明的压力量）",
}


def _environment_type(package: dict[str, Any], type_id: str) -> dict[str, Any] | None:
    environment = package.get("environment") if isinstance(package.get("environment"), dict) else {}
    for item in environment.get("types") or []:
        if isinstance(item, dict) and str(item.get("id")) == type_id:
            return item
    return None


def _validate_environment_observers(
    package: dict[str, Any], errors: list[str], *, registered: set[str] | None = None
) -> None:
    """环境类型：观察者名单（可选，缺省即无人可见）——与既有的取值域 / 单位 / 期限校验互补。"""
    environment = package.get("environment") if isinstance(package.get("environment"), dict) else {}
    types = environment.get("types") if isinstance(environment.get("types"), list) else []
    known = _all_ids(package) if registered is None else registered
    for index, item in enumerate(types):
        if not isinstance(item, dict):
            continue
        where = f"environment.types[{index}]"
        observers = item.get("observers")
        if observers is None:
            continue
        # 列表 = 谁能观察到；映射 = {观察者标识: 该观察者的精度说明}
        names = list(observers.keys()) if isinstance(observers, dict) else observers
        if not isinstance(observers, (list, dict)):
            errors.append(f"{where}.observers: 必须是列表（角色 / 种族 / 地区标识）或按观察者的映射")
            continue
        for name in names:
            if not isinstance(name, str):
                continue
            value = name.strip()
            if value in ("all", "亲历"):
                continue
            if value not in known:
                errors.append(f"{where}.observers: 引用不存在的标识 {value!r}")


def _validate_ledger_derived(package: dict[str, Any], errors: list[str]) -> None:
    """B-3+B-9 v2：账本推导式的校验。

    **这是「不给表达式语言」这条纪律的守卫**：算子闭集、操作数必须是已声明的账本键、
    推导键不得与存储键重名、只能引用更早声明的推导键（结构上无环）、多一个键即拒。
    """
    from ..runtime.ledger import DERIVED_OPS  # 单一真源：算子闭集定义在纯函数模块里

    derived = package.get("derived")
    if derived is None:
        return
    if not isinstance(derived, list):
        errors.append("derived: 必须是列表")
        return

    stored_keys: set[tuple[str, str, str]] = set()
    for item in package.get("ledger") or []:
        if isinstance(item, dict):
            stored_keys.add((
                str(item.get("scope_kind") or ""),
                str(item.get("scope_id") or ""),
                str(item.get("key") or ""),
            ))

    declared_derived: set[tuple[str, str, str]] = set()
    for index, item in enumerate(derived):
        where = f"derived[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: 必须是映射")
            continue
        extra = sorted(set(item) - {
            "id", "scope_kind", "scope_id", "op", "a", "b", "scale", "unit",
        })
        if extra:
            errors.append(
                f"{where}: 只允许 id / scope_kind / scope_id / op / a / b / scale / unit"
                f"（收到 {extra}）；本内核不给表达式语言，算子闭集只有 {'/'.join(DERIVED_OPS)}"
            )
        ident = str(item.get("id") or "")
        if not _text(ident):
            errors.append(f"{where}.id: 缺少标识")
        scope_kind = str(item.get("scope_kind") or "")
        scope_id = str(item.get("scope_id") or "")
        if scope_kind not in ("region", "org", "institution"):
            errors.append(f"{where}.scope_kind: 必须是 region / org / institution 之一")
        if not _text(scope_id):
            errors.append(f"{where}.scope_id: 缺少作用域标识")

        op = str(item.get("op") or "")
        if op not in DERIVED_OPS:
            errors.append(
                f"{where}.op: 未支持的算子 {op!r}（闭集只有 {'/'.join(DERIVED_OPS)}）"
            )
        scale = item.get("scale")
        if scale is not None and (isinstance(scale, bool) or not isinstance(scale, int) or scale <= 0):
            errors.append(f"{where}.scale: 必须是正整数（千分比分母）")

        # 不得与存储键重名：否则「哪个是真的」不可解释（单一事实源）
        if ident and (scope_kind, scope_id, ident) in stored_keys:
            errors.append(
                f"{where}.id: {ident!r} 与同作用域下的**存储账本键**重名——"
                "推导值不落库，重名会让「哪个是真的」不可解释"
            )
        key = (scope_kind, scope_id, ident)
        if ident and key in declared_derived:
            errors.append(f"{where}.id: 同一作用域下推导键 {ident!r} 重复")
        for operand in ("a", "b"):
            reference = str(item.get(operand) or "")
            if not _text(reference):
                errors.append(f"{where}.{operand}: 缺少操作数（必须是已声明的账本键）")
                continue
            ref_key = (scope_kind, scope_id, reference)
            if ref_key not in stored_keys and ref_key not in declared_derived:
                errors.append(
                    f"{where}.{operand}: 未声明的账本键 {reference!r}"
                    "（只能引用存储键或**更早声明的**推导键）"
                )
        if ident:
            declared_derived.add(key)


def _validate_events(
    package: dict[str, Any], known: set[str], errors: list[str], *, registered: set[str] | None = None
) -> None:
    targets = _all_ids(package) if registered is None else registered
    events = package.get("events")
    families = events.get("families") if isinstance(events, dict) else None
    if not isinstance(families, list) or not families:
        errors.append("events.families: 至少一个事件族（阻断项）")
        return
    # 事件密度是体裁必填属性（EVENT_ENGINE_SPEC §四）
    density = events.get("density") if isinstance(events, dict) else None
    if density not in DENSITY_TARGETS:
        errors.append(f"events.density: 必须是 {' / '.join(DENSITY_TARGETS)} 之一（体裁必填）")
    _check_unique(families, "events.families", errors)
    for index, family in enumerate(families):
        if not isinstance(family, dict):
            continue
        where = f"events.families[{index}]"
        if not _text(family.get("name")):
            errors.append(f"{where}: 缺少事件族名称")
        templates = family.get("templates")
        if not isinstance(templates, list) or not templates:
            errors.append(f"{where}.templates: 事件族至少一个模板")
            continue
        _check_unique(templates, f"{where}.templates", errors)
        for t_index, template in enumerate(templates):
            if not isinstance(template, dict):
                continue
            t_where = f"{where}.templates[{t_index}]"
            if not _text(template.get("summary")):
                errors.append(f"{t_where}.summary: 缺少事件描述")
            # B-1：权重只能声明「读哪个压力量 + 千分比系数」，**不接受任何表达式**（防脚本引擎入口）
            declared_p = template.get("pressure")
            if declared_p is not None:
                if not isinstance(declared_p, dict):
                    errors.append(f"{t_where}.pressure: 必须是 {{id, k}} 映射（不支持表达式）")
                else:
                    extra = sorted(set(declared_p) - {"id", "k"})
                    if extra:
                        errors.append(
                            f"{t_where}.pressure: 只允许 id / k 两个键（收到 {extra}）；"
                            "权重形式写死为 base×(1000+k×pressures)//1000，不支持表达式语言"
                        )
                    # 局部取一次：本函数与名册校验不是同一个函数，跨函数作用域拿不到 `pressure_ids`
                    local_pressures = {
                        str(item.get("id") or "")
                        for item in (package.get("pressures") or [])
                        if isinstance(item, dict)
                    }
                    pid = str(declared_p.get("id") or "")
                    if pid and pid not in local_pressures:
                        errors.append(f"{t_where}.pressure.id: 未声明的压力量 {pid!r}")
                    k_value = declared_p.get("k")
                    if k_value is None or isinstance(k_value, bool) or not isinstance(k_value, int):
                        errors.append(f"{t_where}.pressure.k: 必须是整数千分比系数")
            # B-6.2（**校验收紧，向后不兼容**）：声明了 `claims`（每来源表述）就必须**名实相符**——
            # 键要是已登记来源、值要是非空文本，且各来源文本必须**互异**。
            # 理由：B-6.1 让「同源异文」在机制上可写，但「可写」不等于「会写」；
            # 若允许同文，包作者可以声明了一堆 claims 却全是同一句，差异化名存实亡
            # （这正是 1500 世界日实测「9147 条说法只有 8 句不同文本」的形态）。
            declared_claims = template.get("claims")
            if declared_claims is not None:
                if not isinstance(declared_claims, dict):
                    errors.append(f"{t_where}.claims: 必须是「来源标识 → 表述文本」的映射")
                else:
                    local_sources = set(
                        _ids(package.get("sources") if isinstance(package.get("sources"), list) else [])
                    )
                    texts: dict[str, str] = {}
                    for key, value in declared_claims.items():
                        if str(key) not in local_sources:
                            errors.append(f"{t_where}.claims.{key}: 未登记的来源（来源必须在 sources 里声明）")
                        if not _text(value):
                            errors.append(f"{t_where}.claims.{key}: 表述文本不能为空")
                        texts[str(key)] = str(value)
                    seen_text: dict[str, str] = {}
                    for key, value in texts.items():
                        if value in seen_text:
                            errors.append(
                                f"{t_where}.claims: {key} 与 {seen_text[value]} 的表述完全相同——"
                                "声明多版本说法就要真的不同，同文不构成差异化"
                            )
                        else:
                            seen_text[value] = key
            effects = template.get("effects")
            if not isinstance(effects, list) or not effects:
                errors.append(f"{t_where}.effects: 至少一个事实效果")
            for e_index, effect in enumerate(effects if isinstance(effects, list) else []):
                if not isinstance(effect, dict) or not _text(effect.get("kind")):
                    errors.append(f"{t_where}.effects[{e_index}]: 效果缺少类型")
                    continue
                # 效果目标必须是已登记对象，不能指向未登记的名字（附录 C #10）。
                # **`pressure_change` 是例外**：它引用的压力量有**自己的命名空间**（`pressures[]`），
                # 不是「在册对象」——它的目标在下面的压力分支里单独校验。
                kind_now = str(effect.get("kind"))
                target = effect.get("target")
                if target is not None and target not in targets and kind_now != "pressure_change":
                    errors.append(f"{t_where}.effects[{e_index}].target: 指向未登记对象 {target!r}")
                if str(effect.get("kind")) == "casualty":
                    # B-5：档位必须是枚举值（**不引入任何数值**：无 HP / 无健康值 / 无伤情分值）
                    grade = str(effect.get("value") or "")
                    if grade not in CASUALTY_GRADES:
                        errors.append(
                            f"{t_where}.effects[{e_index}].value: 身体后果档位必须是"
                            f" {'/'.join(CASUALTY_GRADES)} 之一（收到 {grade!r}）；"
                            "本内核不建立数值型健康量，「死亡」档位待死亡路径同批落地后启用"
                        )
                if str(effect.get("kind")) == "pressure_change":
                    # B-1 v2：只允许 {kind, target, value}——多一个键就拒绝（不给表达式入口）。
                    # `target` 必须是已声明的压力量；`value` 必须是整数增量，且不得超过该量声明域的宽度
                    # （否则一条效果就能把世界掀翻）。
                    extra = set(effect) - {"kind", "target", "value"}
                    if extra:
                        errors.append(
                            f"{t_where}.effects[{e_index}]: pressure_change 只允许 kind / target / value"
                            f" 三个键（收到多余键 {sorted(extra)}）；本内核不提供表达式语言"
                        )
                    declared_pressure: dict[str, dict[str, Any]] = {
                        str(item.get("id") or ""): item
                        for item in (package.get("pressures") or [])
                        if isinstance(item, dict) and str(item.get("id") or "")
                    }
                    pid = str(effect.get("target") or "")
                    if pid not in declared_pressure:
                        errors.append(
                            f"{t_where}.effects[{e_index}].target: 未声明的压力量 {pid!r}"
                            "（必须先在 pressures 段声明）"
                        )
                    else:
                        raw_value = effect.get("value")
                        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
                            errors.append(
                                f"{t_where}.effects[{e_index}].value: 压力增量必须是整数"
                                f"（收到 {raw_value!r}）"
                            )
                        else:
                            spec = declared_pressure[pid]
                            low = int(
                                spec.get("下限") if spec.get("下限") is not None else spec.get("min") or 0
                            )
                            high = int(
                                spec.get("上限") if spec.get("上限") is not None else spec.get("max") or 0
                            )
                            if high > low and abs(int(raw_value)) > (high - low):
                                errors.append(
                                    f"{t_where}.effects[{e_index}].value: 增量 {raw_value} 超过压力量"
                                    f" {pid!r} 声明域的宽度 {high - low}——一条效果不得把世界掀翻"
                                )
                if str(effect.get("kind")) not in SUPPORTED_EFFECTS:
                    errors.append(
                        f"{t_where}.effects[{e_index}].kind: 未支持的效果类型 {effect.get('kind')!r}"
                        f"（可用：{' / '.join(SUPPORTED_EFFECTS)}）"
                    )
                # 效果必须声明失效方式（EVENT_ENGINE_SPEC §二）
                if str(effect.get("kind")) in ("institution_state", "custom_state"):
                    ok, reason = change_allowed(
                        package,
                        kind=str(effect.get("kind")),
                        target=str(effect.get("target") or ""),
                        value=str(effect.get("value") or ""),
                    )
                    if not ok:
                        errors.append(f"{t_where}.effects[{e_index}]: {reason}")
                if str(effect.get("kind")) == "environment_state":
                    env = _environment_type(package, str(effect.get("target") or ""))
                    if env is None:
                        errors.append(
                            f"{t_where}.effects[{e_index}].target: 环境效果必须指向已声明的环境类型"
                        )
                    elif effect.get("value") not in (env.get("values") or []):
                        errors.append(
                            f"{t_where}.effects[{e_index}].value: 取值必须是该环境类型取值域内的值"
                        )
                expiry = effect.get("expiry")
                if expiry not in EXPIRY_KINDS:
                    errors.append(
                        f"{t_where}.effects[{e_index}].expiry: 必须是 {' / '.join(EXPIRY_KINDS)} 之一"
                    )
                elif expiry == "natural_recovery" and not _text(effect.get("recovery")):
                    errors.append(f"{t_where}.effects[{e_index}].recovery: 自然恢复须写明条件")
            _check_refs(template.get("preconditions"), known, f"{t_where}.preconditions", errors)


def _validate_event_calendar(package: dict[str, Any], errors: list[str]) -> None:
    """包内固定事件（节庆）：只校验可判定部分——历法内的月日与所属族。"""
    events = package.get("events")
    fixed = events.get("calendar") if isinstance(events, dict) else None
    if fixed is None:
        return
    if not isinstance(fixed, list):
        errors.append("events.calendar: 必须是列表")
        return
    calendar = package.get("calendar") if isinstance(package.get("calendar"), dict) else {}
    months = calendar.get("months") if isinstance(calendar.get("months"), list) else []
    families = {str(item.get("id")) for item in events.get("families") or [] if isinstance(item, dict)}
    _check_unique(fixed, "events.calendar", errors)
    for index, item in enumerate(fixed):
        where = f"events.calendar[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: 条目必须是对象")
            continue
        month = item.get("month")
        day = item.get("day")
        if not isinstance(month, int) or not 1 <= month <= max(1, len(months)):
            errors.append(f"{where}.month: 超出历法月表范围")
        elif not isinstance(day, int) or not 1 <= day <= int(months[month - 1].get("days") or 0):
            errors.append(f"{where}.day: 超出该月天数")
        if str(item.get("family")) not in families:
            errors.append(f"{where}.family: 未登记的事件族 {item.get('family')!r}")

    # 固定事件不因随机预算被丢弃：包内同日固定事件超过密度档上限时必须在创建前报错（附录 B #3）
    bounds = DENSITY_TARGETS.get(str(events.get("density") or ""))
    if bounds:
        per_day: dict[tuple[int, int], int] = {}
        for item in fixed:
            if not isinstance(item, dict):
                continue
            key = (int(item.get("month") or 0), int(item.get("day") or 0))
            per_day[key] = per_day.get(key, 0) + 1
        for (month, day), count in sorted(per_day.items()):
            if count > bounds[1]:
                errors.append(
                    f"events.calendar: 第 {month} 月第 {day} 日的固定事件有 {count} 件，超过密度档"
                    f"『{events.get('density')}』的每日上限 {bounds[1]}——固定事件不因预算被丢弃，"
                    "请减少同日固定事件或调整密度档"
                )


def _validate_life_roles(package: dict[str, Any], errors: list[str]) -> None:
    life = package.get("life")
    if not isinstance(life, list) or not life:
        errors.append("life: 至少一个生活线模板（阻断项）")
        life = []
    _check_unique(life, "life", errors)
    for index, template in enumerate(life):
        if not isinstance(template, dict):
            continue
        where = f"life[{index}]"
        if not isinstance(template.get("sleep"), bool):
            errors.append(f"{where}.sleep: 必须显式声明是否睡眠（阻断项）")
        windows = template.get("windows")
        if not isinstance(windows, list) or not windows:
            errors.append(f"{where}.windows: 至少一个世界时间窗")
            continue
        for w_index, window in enumerate(windows):
            if not isinstance(window, dict):
                errors.append(f"{where}.windows[{w_index}]: 条目必须是对象")
                continue
            start, end = window.get("start"), window.get("end")
            if not isinstance(start, int) or not isinstance(end, int) or end <= start or start < 0:
                errors.append(f"{where}.windows[{w_index}]: 需要 0 ≤ start < end 的世界秒区间")
            if not _text(window.get("activity")):
                errors.append(f"{where}.windows[{w_index}].activity: 缺少活动标识")
    life_ids = set(_ids(life))

    roles = package.get("roles")
    if not isinstance(roles, list) or not roles:
        errors.append("roles: 至少一个可装配的角色模板（阻断项）")
        roles = []
    _check_unique(roles, "roles", errors)
    source_ids = set(_ids(package.get("sources") if isinstance(package.get("sources"), list) else []))
    for index, role in enumerate(roles):
        if not isinstance(role, dict):
            continue
        where = f"roles[{index}]"
        if not _text(role.get("name")):
            errors.append(f"{where}: 缺少角色类型名称")
        _check_refs([role.get("life_template")], life_ids, f"{where}.life_template", errors)
        _check_refs(role.get("channels"), source_ids, f"{where}.channels", errors)

    comms = package.get("comms")
    mechanisms = comms.get("mechanisms") if isinstance(comms, dict) else None
    if not isinstance(mechanisms, list) or not mechanisms:
        errors.append("comms.mechanisms: 至少一种与外界联络的机制（阻断项）")
    else:
        _check_unique(mechanisms, "comms.mechanisms", errors)
        for index, item in enumerate(mechanisms):
            if isinstance(item, dict) and not _text(item.get("limits")):
                errors.append(f"comms.mechanisms[{index}].limits: 缺少限制声明")


def _validate_initial_state(package: dict[str, Any], known: set[str], errors: list[str]) -> None:
    state = package.get("initial_state")
    if not isinstance(state, dict):
        errors.append("initial_state: 缺少初始状态段（阻断项）")
        return
    _check_refs(state.get("events"), known, "initial_state.events", errors)
    _check_refs(state.get("rumors"), known, "initial_state.rumors", errors)
    mysteries = state.get("mysteries")
    if not isinstance(mysteries, list):
        errors.append("initial_state.mysteries: 必须是列表（可为空）")
        return
    _check_unique(mysteries, "initial_state.mysteries", errors)
    for index, mystery in enumerate(mysteries):
        if not isinstance(mystery, dict):
            continue
        if not _text(mystery.get("question")):
            errors.append(f"initial_state.mysteries[{index}].question: 缺少谜题描述")
        refs = mystery.get("refs")
        if not isinstance(refs, list) or not refs:
            errors.append(f"initial_state.mysteries[{index}].refs: 谜题须挂靠至少一处可获知内容")
        else:
            _check_refs(refs, known, f"initial_state.mysteries[{index}].refs", errors)
