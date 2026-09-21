"""世界事件引擎（EVENT_ENGINE_SPEC §二–§七，阶段 3）。

纯函数层：候选抽样、前置条件、效果骨架、说法与获知、素材资格。
不碰数据库、不碰 LLM——落库与文本表述在 `service` / `render` 里。

要点：
- 抽样只用**锁定种子 + 规则版本 + 历法日 + 槽序**，固定哈希（不用进程随机化的 `hash()`），
  同一输入在桌面 / 安卓 / 不同分批下必得同一候选（附录 B #1、附录 A）；
- 数量先定预算再取候选：上限是硬预算，下限是「有合法来源时」的目标，无合法候选就是没发生；
- 固定节庆先占当日名额，日期不漂移（§四）；
- 事实效果只由受支持规则确定，语言产物不改数值、时间或参与者（§3.2、附录 B #4）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable

from ..world.cards import effective_lifespan, region_of
from ..world.validate import DENSITY_TARGETS, EXPIRY_KINDS, SUPPORTED_EFFECTS as SUPPORTED_EFFECT_NAMES

#: 候选槽建议起点（附录 A）：槽数是搜索空间，不是发生数量
SLOTS_BY_DENSITY: dict[str, int] = {"稀疏": 2, "常规": 4, "丰盛": 8}

#: 受支持的效果闭集由设定层持有（校验器与引擎共用一份）
SUPPORTED_EFFECTS = set(SUPPORTED_EFFECT_NAMES)

# 同刻多效果的固定优先规则（EVENT_ENGINE_SPEC §六）：数字大 = 更强。
# 冲突时按从小到大依次施加、以最强的一条为准：物理通行 > 活动限制 > 环境 > 制度 > 惯例 > 渠道 > 通告 > 风闻
# （硬约束压过软约束，事实压过传播）。模板 / 世界包可在效果上声明 priority 覆盖（§十一「同刻多效果优先规则」）。
EFFECT_PRIORITY: dict[str, int] = {
    "rumor_spread": 10,
    "public_notice": 20,
    "source_delay": 30,
    "custom_state": 40,
    "institution_state": 50,
    "environment_state": 60,
    "activity_constraint": 70,
    "route_blocked": 80,
}
DEFAULT_EFFECT_PRIORITY = 0


def effect_priority(effect: dict[str, Any]) -> int:
    """单条效果的优先值：模板声明优先，其次按效果类型的固定档位。"""
    declared = effect.get("priority")
    if isinstance(declared, bool) or not isinstance(declared, int):
        declared = None
    if declared is not None:
        return int(declared)
    return EFFECT_PRIORITY.get(str(effect.get("kind") or ""), DEFAULT_EFFECT_PRIORITY)


def event_priority(event: dict[str, Any]) -> int:
    """事件在同刻顺序里的档位 = 它最强的一条效果（同一张优先表，不另建一套）。"""
    values = [effect_priority(item) for item in (event.get("effects") or []) if isinstance(item, dict)]
    return max(values) if values else DEFAULT_EFFECT_PRIORITY


def stable_key(*parts: Any) -> str:
    """固定编码 + 固定哈希：同一输入处处同键，不依赖进程随机化（附录 A）。"""
    payload = "\x1f".join("" if part is None else str(part) for part in parts)
    return hashlib.blake2b(payload.encode("utf-8"), digest_size=12).hexdigest()


def daily_budget(seed: str, rules_version: str, day_index: int, density: str) -> int:
    """当日世界级事件预算：区间内按稳定哈希取值，上限是硬预算（§四）。"""
    low, high = DENSITY_TARGETS.get(density, (0, 0))
    if high <= low:
        return low
    return low + int(stable_key(seed, rules_version, day_index, "budget"), 16) % (high - low + 1)


def fixed_events(package: dict[str, Any], *, day_index: int, calendar: Any) -> list[dict[str, Any]]:
    """包内固定事件（节庆）当日的条目：日期由锁定历法决定，不随机漂移（§四）。"""
    events = package.get("events") if isinstance(package.get("events"), dict) else {}
    fixed = events.get("calendar") if isinstance(events.get("calendar"), list) else []
    view = calendar.to_calendar(day_index * calendar.day_seconds)
    out: list[dict[str, Any]] = []
    for index, item in enumerate(fixed):
        if not isinstance(item, dict):
            continue
        if int(item.get("month") or 0) == int(view["month"]) and int(item.get("day") or 0) == int(view["day"]):
            out.append(
                {
                    "slot": f"fixed-{index}",
                    "family": str(item.get("family") or ""),
                    "template": str(item.get("id") or f"fc-{index}"),
                    "summary": str(item.get("name") or "固定事件"),
                    "effects": [],
                    "preconditions": [],
                    "fixed": True,
                }
            )
    return out


def _templates(package: dict[str, Any]) -> list[dict[str, Any]]:
    events = package.get("events") if isinstance(package.get("events"), dict) else {}
    families = events.get("families") if isinstance(events.get("families"), list) else []
    out: list[dict[str, Any]] = []
    for family in families:
        if not isinstance(family, dict):
            continue
        for template in family.get("templates") or []:
            if isinstance(template, dict):
                out.append({"family": str(family.get("id") or ""), **template})
    return sorted(out, key=lambda item: str(item.get("id")))  # 稳定顺序：不随 dict 顺序漂移


def draw_slot(
    package: dict[str, Any], *, seed: str, rules_version: str, day_index: int, slot_index: int
) -> dict[str, Any] | None:
    """一个槽 → 一个候选模板（按权重稳定选择）；候选相同不等于结果相同（§3.1）。"""
    templates = _templates(package)
    if not templates:
        return None
    weights = [max(0, int(item.get("weight") or 1)) for item in templates]
    total = sum(weights)
    if total <= 0:
        return None
    draw = int(stable_key(seed, rules_version, day_index, "slot", slot_index), 16) % total
    running = 0
    for template, weight in zip(templates, weights):
        running += weight
        if draw < running:
            return {
                "slot": f"s{slot_index}",
                "family": template["family"],
                "template": str(template.get("id") or ""),
                "summary": str(template.get("summary") or ""),
                "effects": [item for item in template.get("effects") or [] if isinstance(item, dict)],
                "preconditions": [str(item) for item in template.get("preconditions") or []],
                "fixed": False,
            }
    return None


def unmet_preconditions(
    preconditions: Iterable[str], *, events: set[str], effects: set[str]
) -> list[str]:
    """前置条件：已登记的背景内容默认成立；指向事件 / 后果的必须有实际记录（§3.1）。

    没有合法候选就不发生——不为凑密度凭空添加实体或违反世界规则。
    """
    unmet: list[str] = []
    for item in preconditions:
        key = str(item)
        if key.startswith("ev-") and key not in events:
            unmet.append(key)
        elif key.startswith("fx-") and key not in effects:
            unmet.append(key)
    return unmet


def plan_day(
    package: dict[str, Any],
    *,
    seed: str,
    rules_version: str,
    day_index: int,
    calendar: Any,
    events: set[str],
    effects: set[str],
) -> list[dict[str, Any]]:
    """当日候选：固定事件先占名额，再用剩余额度取随机槽；前置条件不足者不发生（§四）。"""
    density = str((package.get("events") or {}).get("density") or "稀疏")
    budget = daily_budget(seed, rules_version, day_index, density)
    chosen = list(fixed_events(package, day_index=day_index, calendar=calendar))[:budget]
    slots = SLOTS_BY_DENSITY.get(density, 2)
    for slot_index in range(slots):
        if len(chosen) >= budget:
            break
        candidate = draw_slot(
            package, seed=seed, rules_version=rules_version, day_index=day_index, slot_index=slot_index
        )
        if candidate is None or candidate["slot"] in {item["slot"] for item in chosen}:
            continue
        if unmet_preconditions(candidate["preconditions"], events=events, effects=effects):
            continue  # 候选落空即不重抽（附录 A）
        chosen.append(candidate)
    return chosen


def event_id(seed: str, rules_version: str, day_index: int, slot: str) -> str:
    """稳定事件标识：同一逻辑事件在任何进程 / 分批下同一身份（§3.2、附录 A）。"""
    return f"ev-{stable_key(seed, rules_version, day_index, slot)[:12]}"


def effect_rows(
    event: dict[str, Any],
    *,
    instance_id: str,
    timeline_id: str,
    event_ident: str,
    world_seconds: int,
    family: str = "",
) -> list[dict[str, Any]]:
    """效果状态：只落受支持的闭集类型，并保留失效方式与恢复条件（§二、§六）。"""
    out: list[dict[str, Any]] = []
    for index, effect in enumerate(event.get("effects") or []):
        kind = str(effect.get("kind") or "")
        if kind not in SUPPORTED_EFFECTS:
            continue  # 未支持的效果由校验器先行拒绝，这里再兜一层
        expiry = str(effect.get("expiry") or "until_cleared")
        if expiry not in EXPIRY_KINDS:
            expiry = "until_cleared"
        out.append(
            {
                "id": f"fx-{stable_key(event_ident, index)[:12]}",
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "event_id": event_ident,
                "target": str(effect.get("target") or ""),
                "kind": kind,
                "family": str(family),
                "priority": effect_priority(effect),
                "value": None if effect.get("value") is None else str(effect.get("value")),
                "from_world": int(world_seconds),
                "expiry": expiry,
                "recovery": str(effect.get("recovery") or ""),
                "active": 1,
                "cleared_at": None,
            }
        )
    return out


def claim_rows(
    event: dict[str, Any],
    *,
    package: dict[str, Any],
    instance_id: str,
    timeline_id: str,
    event_ident: str,
    world_seconds: int,
    calendar: Any,
) -> list[dict[str, Any]]:
    """说法集合：每条带来源渠道、受众条件、最早传播时刻与可信线索（§二、§五）。

    阶段 3 的说法由骨架直接派生（真实、片面的版本）；失真 / 立场改写属内容生成路径，
    需要时再经 LLM 在骨架约束内产出，不能反过来决定事件事实。
    """
    sources = [item for item in package.get("sources") or [] if isinstance(item, dict)]
    out: list[dict[str, Any]] = []
    for index, source in enumerate(sources):
        delay = int(source.get("delay_seconds") or 0)
        out.append(
            {
                "id": f"cl-{stable_key(event_ident, source.get('id'))[:12]}",
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "event_id": event_ident,
                "source_id": str(source.get("id") or ""),
                "text": str(event.get("summary") or ""),
                "audience": str(source.get("audience") or "公开"),
                "earliest_world": int(world_seconds) + max(0, delay),
                "credibility": "recorded",
                "_order": index,
            }
        )
    return out


def grants(
    event: dict[str, Any],
    claims: list[dict[str, Any]],
    card: dict[str, Any],
    *,
    world_seconds: int,
    calendar: Any,
) -> list[dict[str, Any]]:
    """某角色的获知记录：亲历（参与者 / 受影响目标）或经卡片渠道接触说法（§五、§13）。

    只处理**已成立**的获知：渠道要有、传播时刻要已到；「可能听到」不算已经听到。
    """
    character_id = str((card.get("meta") or {}).get("card_id") or "")
    channels = {str(item.get("source_id")) for item in card.get("channels") or []}
    raw_effects = event.get("effects")
    if isinstance(raw_effects, str):  # 落库后 effects 是 JSON 文本
        try:
            raw_effects = json.loads(raw_effects)
        except json.JSONDecodeError:
            raw_effects = []
    targets = {str(item.get("target") or "") for item in raw_effects or [] if isinstance(item, dict)}
    role = str(card.get("role_id") or "")
    out: list[dict[str, Any]] = []
    involved = role in targets or region_of(card) in targets
    if involved:
        out.append(
            {
                "id": f"kn-{stable_key(event['id'], character_id, 'self')[:12]}",
                "instance_id": event["instance_id"],
                "timeline_id": event["timeline_id"],
                "character_id": character_id,
                "world_seconds": int(world_seconds),
                "kind": "observation",
                "target": str(event["id"]),
                "source": "亲历",
                "stance": "experienced",
                "text": str(event.get("summary") or ""),
            }
        )
    for claim in claims:
        if str(claim.get("source_id")) not in channels:
            continue
        if int(claim.get("earliest_world") or 0) > world_seconds:
            continue  # 尚未传播到达
        out.append(
            {
                "id": f"kn-{stable_key(claim['id'], character_id)[:12]}",
                "instance_id": event["instance_id"],
                "timeline_id": event["timeline_id"],
                "character_id": character_id,
                "world_seconds": max(int(world_seconds), int(claim["earliest_world"])),
                "kind": "claim",
                "target": str(claim["id"]),
                "source": str(claim.get("source_id") or ""),
                "stance": "recorded",
                "text": str(claim.get("text") or ""),
            }
        )
    return out


def event_moment(seed: str, rules_version: str, day_index: int, slot: str, day_seconds: int) -> int:
    """事件在当日内的确定时刻（稳定哈希；同刻顺序另有 seq）。"""
    offset = int(stable_key(seed, rules_version, day_index, slot, "at"), 16) % max(1, int(day_seconds))
    return int(day_index) * int(day_seconds) + offset


def claim_grant(
    claim: dict[str, Any], card: dict[str, Any], *, world_seconds: int
) -> dict[str, Any] | None:
    """传播到达后的获知：渠道不匹配或还没到传播时刻就不给（§五）。"""
    character_id = str((card.get("meta") or {}).get("card_id") or "")
    channels = {str(item.get("source_id")) for item in card.get("channels") or []}
    if not character_id or str(claim.get("source_id")) not in channels:
        return None
    earliest = int(claim.get("earliest_world") or 0)
    if earliest > world_seconds:
        return None
    return {
        "id": f"kn-{stable_key(claim['id'], character_id)[:12]}",
        "instance_id": claim["instance_id"],
        "timeline_id": claim["timeline_id"],
        "character_id": character_id,
        "world_seconds": max(int(world_seconds), earliest),
        "kind": "claim",
        "target": str(claim["id"]),
        "source": str(claim.get("source_id") or ""),
        "stance": "recorded",
        "text": str(claim.get("text") or ""),
    }


def share_qualified(event: dict[str, Any]) -> bool:
    """素材资格（§七）：有分享价值、该角色已获知后才成为该角色的候选素材。"""
    return bool(event.get("share_value")) or float(event.get("importance") or 0) >= 0.5


def detail_text(event: dict[str, Any]) -> str:
    """未生成表述时的确定性兜底：只描述骨架，不新增事实（§2.6、§3.2）。"""
    summary = str(event.get("summary") or "").strip()
    return summary or "（无表述）"


def backfill_rows(
    package: dict[str, Any],
    *,
    instance_id: str,
    timeline_id: str,
    seed: str,
    rules_version: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """历史回填（§3.3）：把包内既定的史料与初始事实落成历史条目。

    与运行期共用同一事件模型；**不再次施加效果**（灾害、性格冲击、获知都不重放），
    素材侧也不因导入 / 启动变成新近素材。阶段 3 只回填包内已写定的内容，不凭空生成。
    """
    events: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    fixed = package.get("initial_state") if isinstance(package.get("initial_state"), dict) else {}
    canon = {str(item.get("id")): item for item in package.get("canon") or [] if isinstance(item, dict)}
    narratives = {str(item.get("id")): item for item in package.get("narratives") or [] if isinstance(item, dict)}
    for index, ident in enumerate(fixed.get("events") or []):
        item = canon.get(str(ident)) or {}
        events.append(
            {
                "id": f"ev-h{index:03d}-{stable_key(seed, 'backfill', ident)[:8]}",
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "world_seconds": int(item.get("at") or 0),
                "seq": index,
                "kind": "world",
                "family": "",
                "template": str(ident),
                "source": "backfill",
                "summary": str(item.get("statement") or ident),
                "detail": str(item.get("statement") or ident),
                "text_source": "template",
                "effects": "[]",
                "share_value": int(bool(item.get("share_value"))),
                "importance": float(item.get("importance") or 0.0),
                "created_real": 0.0,
            }
        )
    for index, ident in enumerate(fixed.get("rumors") or []):
        item = narratives.get(str(ident)) or {}
        # 说法层的正文字段是 text（实情层才是 statement）——写死 statement 会让回填退化成标识
        statement = str(item.get("text") or item.get("statement") or ident)
        events.append(
            {
                "id": f"ev-h{100 + index:03d}-{stable_key(seed, 'backfill', ident)[:8]}",
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "world_seconds": int(item.get("at") or 0),
                "seq": 100 + index,
                "kind": "world",
                "family": "",
                "template": str(ident),
                "source": "backfill",
                "summary": statement,
                "detail": statement,
                "text_source": "template",
                "effects": "[]",
                "share_value": int(bool(item.get("share_value"))),
                "importance": float(item.get("importance") or 0.0),
                "created_real": 0.0,
            }
        )
        claims.append(
            {
                "id": f"cl-h{100 + index:03d}-{stable_key(seed, 'backfill', ident)[:8]}",
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "event_id": f"ev-h{100 + index:03d}-{stable_key(seed, 'backfill', ident)[:8]}",
                "source_id": str(item.get("source_id") or (item.get("sources") or [""])[0] or ""),
                "text": statement,
                "audience": str(item.get("audience") or "公开"),
                "earliest_world": int(item.get("at") or 0),
                "credibility": "recorded",
            }
        )
    _ = rules_version
    return events, claims


# ---------- 创建期历史回填的编纂计划（WORLD_SETTING_SPEC §3.6） ----------

BACKFILL_VOLUME_YEARS = 10  # 暂按十年一卷（§3.6 条 3，起步口径，不冻结）
BACKFILL_BATCH_MIN, BACKFILL_BATCH_MAX = 10, 20  # 10–20 条一批
BACKFILL_MAX_VOLUMES = 24  # 超长时代的折半阈值：卷数超它即折半抽样（跨度不能线性放大，DESIGN.md 回填一节）
BACKFILL_KEY_FIGURE_LIMIT = 12  # 回填期「确定生死与活动区间」的要点人物上限（种子定序取前 N，其余留白）
BACKFILL_ENTITY_LIMIT = 32  # 回填期具名对象的登记体量上限（名册生成预算）


def backfill_sample_volumes(volume_count: int, *, limit: int = BACKFILL_MAX_VOLUMES) -> tuple[list[int], int]:
    """超长时代：卷数超阈值就按折半抽样（逐轮步长 ×2 的等距抽样），未列卷 = 跨时代留白。

    返回 (保留的卷下标, 步长)。折半只改**抽样密度**、不改事实，所以与种子无关；留白不是缺载。
    """
    count = max(0, int(volume_count))
    step = 1
    while count // step > max(1, int(limit)):
        step *= 2
    return [index for index in range(count) if index % step == 0], step


def backfill_key_figures(
    package: dict[str, Any], *, seed: str, rules_version: str, limit: int = BACKFILL_KEY_FIGURE_LIMIT
) -> list[str]:
    """要点人物挑选：登记人物按种子定序取前 N（同种子同人选），其余保留在册但不铺生死。

    只挑人物（`kind=person`）：组织 / 地点 / 物件没有寿命推演可言。
    """
    persons = [
        str(item.get("id"))
        for item in package.get("entities") or []
        if isinstance(item, dict) and str(item.get("kind") or "") == "person" and item.get("id")
    ]
    persons.sort()
    persons.sort(key=lambda ident: stable_key(seed, rules_version, ident))
    return persons[: max(1, int(limit))]


def backfill_volume_index(calendar: Any, at: int) -> int:
    """所在卷（十年一卷）：按世界时刻换算，不查现实日期库。"""
    span = max(1, int(getattr(calendar, "year_seconds", 0) or 1)) * BACKFILL_VOLUME_YEARS
    return int(at) // span


def backfill_batch_sizes(count: int) -> list[int]:
    """把 count 条拆成若干批：每批尽量落在 10–20；总数不足一批就不凑量。"""
    if count <= 0:
        return []
    if count <= BACKFILL_BATCH_MAX:
        return [count]
    parts = -(-count // BACKFILL_BATCH_MAX)
    base, rest = divmod(count, parts)
    return [base + (1 if index < rest else 0) for index in range(parts)]


def backfill_plan(
    package: dict[str, Any], *, seed: str, rules_version: str, calendar: Any
) -> dict[str, Any]:
    """分时代（卷）× 传本 × 10–20 条一批的编纂计划（§3.6 条 2/3）。

    - 只组织包内**已写定**的材料（canon 实情条目 + narratives 说法条目）：没有合法候选就是留白，
      不为凑量添加事实；计划里给出 `need_text`，谁缺「一句话」文本一眼可见。
    - 批内顺序按创建期固定的种子确定（同一种子处处同序），只影响编纂顺序，不改事实。
    - `selection` 是各传本的选载范围：哪个传本承载哪些条目。
    """
    fixed = package.get("initial_state") if isinstance(package.get("initial_state"), dict) else {}
    canon = {str(i.get("id")): i for i in package.get("canon") or [] if isinstance(i, dict)}
    narratives = {str(i.get("id")): i for i in package.get("narratives") or [] if isinstance(i, dict)}
    entries: list[dict[str, Any]] = []
    for ident in fixed.get("events") or []:
        item = canon.get(str(ident)) or {}
        entries.append(
            {
                "ident": str(ident),
                "layer": "canon",
                "at": int(item.get("at") or 0),
                "source_id": "",
                "need_text": not str(item.get("statement") or "").strip(),
            }
        )
    for ident in fixed.get("rumors") or []:
        item = narratives.get(str(ident)) or {}
        text = str(item.get("text") or item.get("statement") or "").strip()
        entries.append(
            {
                "ident": str(ident),
                "layer": "claim",
                "at": int(item.get("at") or 0),
                "source_id": str(item.get("source_id") or (item.get("sources") or [""])[0] or ""),
                "need_text": (not text) or text == str(ident),
            }
        )
    entries.sort(key=lambda row: str(row["ident"]))  # 先立稳定基线，再按种子打散
    entries.sort(key=lambda row: stable_key(seed, rules_version, str(row["ident"])))
    grouped: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for row in entries:
        grouped.setdefault((backfill_volume_index(calendar, int(row["at"])), str(row["source_id"])), []).append(row)
    span = max(1, int(getattr(calendar, "year_seconds", 0) or 1)) * BACKFILL_VOLUME_YEARS
    volumes: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    selection: dict[str, list[str]] = {}
    for (volume_index, source_id), group in sorted(grouped.items()):
        idents = [str(row["ident"]) for row in group]
        volumes.append(
            {
                "volume": volume_index,
                "source_id": source_id,
                "from_world": volume_index * span,
                "to_world": (volume_index + 1) * span,
                "entries": idents,
            }
        )
        offset = 0
        for size in backfill_batch_sizes(len(group)):
            chunk = group[offset : offset + size]
            offset += size
            batches.append(
                {
                    "volume": volume_index,
                    "source_id": source_id,
                    "entries": [str(row["ident"]) for row in chunk],
                    "need_text": [str(row["ident"]) for row in chunk if row["need_text"]],
                }
            )
        selection.setdefault(source_id, []).extend(idents)
    total_volumes = max((int(v["volume"]) for v in volumes), default=-1) + 1
    keep, step = backfill_sample_volumes(total_volumes)
    keep_set = set(keep)
    listed_idents = {ident for volume in volumes if int(volume["volume"]) in keep_set for ident in volume["entries"]}
    if step > 1:
        volumes = [volume for volume in volumes if int(volume["volume"]) in keep_set]
        batches = [batch for batch in batches if int(batch["volume"]) in keep_set]
        selection = {
            source_id: [ident for ident in idents if ident in listed_idents]
            for source_id, idents in selection.items()
        }
    persons = [
        str(item.get("id"))
        for item in package.get("entities") or []
        if isinstance(item, dict) and str(item.get("kind") or "") == "person" and item.get("id")
    ]
    return {
        "volumes": volumes,
        "batches": batches,
        "selection": selection,
        "volume_years": BACKFILL_VOLUME_YEARS,
        "batch_bounds": [BACKFILL_BATCH_MIN, BACKFILL_BATCH_MAX],
        "total": len(entries),
        "listed": len(listed_idents),
        "with_text": sum(1 for row in entries if not row["need_text"]),
        "sampling": {
            "volumes": total_volumes,
            "kept": len(keep),
            "step": step,
            "blanked": len(entries) - len(listed_idents),
        },
        "key_figures": backfill_key_figures(package, seed=seed, rules_version=rules_version),
        "roster_budget": {
            "limit": BACKFILL_ENTITY_LIMIT,
            "registered": len(persons),
            "room": max(0, BACKFILL_ENTITY_LIMIT - len(persons)),
        },
    }


def backfill_product_errors(
    package: dict[str, Any], rows: list[dict[str, Any]], claims: list[dict[str, Any]]
) -> list[str]:
    """创建期联合校验（§3.6 条 4）：回填产物自身要立得住，才谈得上固化。

    - 每条历史条目都要有「一句话」级文本（退化成标识 = 没写完，不是留白）
    - 说法条目的来源必须是包内声明过的传本（不挂到未声明来源上）
    """
    errors: list[str] = []
    known_sources = {str(item.get("id")) for item in package.get("sources") or [] if isinstance(item, dict)}
    for row in rows:
        text = str(row.get("summary") or "").strip()
        if not text or text == str(row.get("template") or ""):
            errors.append(f"历史条目缺少一句话文本：{row.get('template') or row.get('id')}")
    for claim in claims:
        source_id = str(claim.get("source_id") or "")
        if source_id and known_sources and source_id not in known_sources:
            errors.append(f"说法引用了未声明的传本：{source_id}（{claim.get('id')}）")
    return errors


def dump_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉内部排序键（`_order`）后交给存储层。"""
    return [{key: value for key, value in row.items() if not key.startswith("_")} for row in rows]


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


# ---------- 生死事件（§四：寿终由寿命模型与世界时刻推出，单独记账） ----------


def death_moment(card: dict[str, Any], package: dict[str, Any], calendar: Any) -> int | None:
    """角色的寿终世界时刻：卡片固化的死亡优先，其次卡片 `identity.lifespan` 覆盖，最后种族寿命上限。

    种族或卡片声明 `mode`（long / unbounded）时不推寿终——不能替设定发明死亡。
    """
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    fixed = identity.get("died")
    if isinstance(fixed, int):
        return int(fixed)
    born = identity.get("born")
    if not isinstance(born, int):
        return None
    race_id = identity.get("race_id")
    races = {str(item.get("id")): item for item in package.get("races") or [] if isinstance(item, dict)}
    race = races.get(str(race_id))
    if race is None:
        return None
    lifespan = effective_lifespan(identity, race)
    if lifespan.get("mode") is not None:
        return None  # 声明了 long / unbounded：不替设定发明死亡
    max_years = lifespan.get("max_years")
    if not isinstance(max_years, int) or max_years <= 0:
        return None
    return int(born) + int(max_years) * int(calendar.year_seconds)


def is_dead(instance_id: str, timeline_id: str, character_id: str, rows: list[dict[str, Any]]) -> bool:
    """该角色是否已有身故记录（按事件模板内的角色标识判定，不另立字段）。"""
    marker = f"death:{character_id}"
    return any(
        str(item.get("template")) == marker and int(item.get("world_seconds") or 0) > 0 for item in rows
    )


def death_event(
    card: dict[str, Any],
    *,
    instance_id: str,
    timeline_id: str,
    world_seconds: int,
    calendar: Any,
    seed: str,
) -> dict[str, Any]:
    """身故事件：单独记账（不占每日随机密度），可产生死讯说法。"""
    character_id = str((card.get("meta") or {}).get("card_id") or "")
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    born = int(identity.get("born") or 0)
    name = str(identity.get("name") or "某人")
    age = max(0, (int(world_seconds) - born) // max(1, int(calendar.year_seconds)))
    ident = f"ev-death-{stable_key(instance_id, timeline_id, character_id)[:10]}"
    return {
        "id": ident,
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "world_seconds": int(world_seconds),
        "seq": 0,
        "kind": "character",
        "family": "",
        "template": f"death:{character_id}",
        "source": "engine",
        "subject_name": name,
        "summary": f"{name}身故，享年 {age}",
        "detail": f"{name}身故，享年 {age}",
        "text_source": "template",
        "effects": [],
        "share_value": 0,
        "importance": 0.8,
        "created_real": 0.0,
        "_seed": seed,
    }
