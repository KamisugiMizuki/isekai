"""叙事中介层（NARRATIVE_LAYER_SPEC）：候选、有限编织、表达约束与后验一致性。纯函数层。

设计要点（照 NARRATIVE_LAYER_SPEC 原文）：

- 材料只来自**她已获知 / 已亲历**的东西：她的经历与已获知的说法。实情层、未获知事件、
  未来计划不进候选（水位与渠道过滤由调用方给窗口时保证，本层不再放宽）。
- 候选与叙事单元是**派生视图**：不写世界事实、不产生获知、不改公理与计划；没有素材时
  允许什么都不说，不补造故事。
- 排序与「故事感」只影响表达顺序与取舍，不决定世界事件：缺依据时保持并列 / 矛盾 / 未知。
- 有效后果、当前活动与她的打算作为**入口语境**进提示词——`life.effect_note` 已把仍有效的
  后果写进当前活动，本层不另立一份材料文本。
- 关系判定用可得的结构化字段（同一来源渠道 / 同一天）：
  ponytail: 文本级实体匹配先不做，需要更细的「共同指涉」时再加。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable

from . import proactive
from .cognition import STANCE_LABEL
from .events import stable_key

#: 材料入口的优先顺序（§4.2 起点信号）：刚完成的经历 > 已获知的说法
ENTRY_ORDER = ("experience", "knowledge")
#: 经历可以回顾的世界日数（主动素材自己的一日时效见 proactive.FRESH_DAYS）
EXPERIENCE_DAYS = 2
#: 无明确查询主题的开场预筛（不是语义判断，只是省一次上下文）
OPEN_TURN_MAX = 12
GREETINGS = ("你好", "在吗", "在么", "嗨", "喂", "早上好", "晚安", "hi", "hello", "hey")
#: 数字护栏：与 runtime/render.py 同口径（数字是最好核对的点）
_NUMBERS = re.compile(r"\d+(?:\.\d+)?")

AUDIT_SYSTEM = """你要做一次忠实度判断：判断「她这句话」有没有超出「她只知道的事」。
只认五类越界：把听说 / 读到的说成亲眼所见；把早先获知的说成刚刚得知；把没把握的说成确定；
说出材料里没有的参与者、原因、结果或幕后结论；说她此刻并没有在做的事。
换说法、含糊、承认不知道或记不清、只讲一部分、暂缓不谈都不算越界。
只输出 JSON：{"ok": true}，或 {"ok": false, "why": "越界的那处（不超过 20 字）"}。"""


def materials(
    *,
    experiences: Iterable[dict[str, Any]] | None = None,
    knowledge: Iterable[dict[str, Any]] | None = None,
    consumed: Iterable[str] = (),
    world_seconds: int,
    day_seconds: int,
    own_actions: Iterable[str] | None = None,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """她能讲述的材料：已亲历的经历 + 已获知的说法。

    已获知部分复用 `proactive.candidates`（同一份时效与去重口径，不在两处各写一遍）；
    经历只保留仍在回顾窗内、她真正经历过的那几条。
    """
    world = int(world_seconds)
    day = max(1, int(day_seconds))
    taken = {str(item) for item in consumed or ()}
    stance_of = {str(row.get("target") or ""): str(row.get("stance") or "") for row in knowledge or []}
    out: list[dict[str, Any]] = []
    for row in proactive.candidates(
        list(knowledge or []),
        world_seconds=world,
        day_seconds=day,
        consumed=taken,
        own_actions={str(item) for item in own_actions or ()},
    ):
        text = str(row.get("text") or "").strip()
        if not text:
            continue
        out.append(
            {
                "ref": str(row["ref"]),
                "entry": "knowledge",
                "kind": str(row.get("kind") or ""),
                "text": text,
                "source": str(row.get("source") or "") or "无来源",
                "stance": STANCE_LABEL.get(stance_of.get(str(row["ref"]), ""), "只是听说过"),
                "at_world": int(row.get("learned_world") or 0),
            }
        )
    for row in experiences or []:
        ref = str(row.get("id") or "")
        text = str(row.get("summary") or "").strip()
        if not ref or not text or ref in taken:
            continue
        at = int(row.get("world_seconds") or 0)
        if at > world or world - at > EXPERIENCE_DAYS * day:
            continue
        out.append(
            {
                "ref": ref,
                "entry": "experience",
                "kind": str(row.get("kind") or "life"),
                "text": text,
                "source": "自己的经历",
                "stance": STANCE_LABEL["experienced"],
                "at_world": at,
            }
        )
    out.sort(
        key=lambda item: (
            ENTRY_ORDER.index(item["entry"]) if item["entry"] in ENTRY_ORDER else len(ENTRY_ORDER),
            -int(item["at_world"]),
            str(item["ref"]),
        )
    )
    return out[: max(1, int(limit))]


def rank(items: Iterable[dict[str, Any]], *, deferred_refs: Iterable[str] = ()) -> list[dict[str, Any]]:
    """稳定排序（§4.1 / §4.2）：同一水位同一材料永远同一顺序；暂缓过的单元降级但不禁止。"""
    held = {str(item) for item in deferred_refs or ()}
    entry_rank = {name: index for index, name in enumerate(ENTRY_ORDER)}

    def key(item: dict[str, Any]) -> tuple[int, int, int, str]:
        return (
            1 if str(item.get("ref")) in held else 0,
            entry_rank.get(str(item.get("entry")), len(ENTRY_ORDER)),
            -int(item.get("at_world") or 0),
            str(item.get("ref")),
        )

    return sorted(items, key=key)


def relation_of(a: dict[str, Any], b: dict[str, Any], *, day_seconds: int) -> str:
    """材料之间的关系（闭集三值）：同一来源的补充 / 同一天的连续 / 并列（没有依据）。"""
    if (
        str(a.get("entry")) == str(b.get("entry")) == "knowledge"
        and str(a.get("source") or "")
        and str(a.get("source")) == str(b.get("source"))
    ):
        return "补充"
    if str(a.get("entry")) == str(b.get("entry")) == "experience":
        gap = abs(int(a.get("at_world") or 0) - int(b.get("at_world") or 0))
        if gap <= max(1, int(day_seconds)):
            return "连续"
    return "并列"


def unit_id(primary_ref: str) -> str:
    """同一主材复用同一个单元标识：暂缓过的那件事讲出来，是同一个单元的推进。"""
    return f"nu-{stable_key('narrative', str(primary_ref))[:12]}"


def weave(
    ranked: Iterable[dict[str, Any]], *, day_seconds: int, limit_extra: int = 2
) -> dict[str, Any] | None:
    """有限编织（§4.3）：只把**有依据相关**的材料并进同一个单元；缺依据就单材成单元。

    不为了戏剧性补冲突、秘密、责任人或转折——组合只是视图，不是新事实。
    """
    items = list(ranked)
    if not items:
        return None
    primary = dict(items[0])
    extras: list[dict[str, Any]] = []
    for item in items[1:]:
        if len(extras) >= max(0, int(limit_extra)):
            break
        relation = relation_of(primary, item, day_seconds=day_seconds)
        if relation == "并列":
            continue
        extras.append({**item, "relation": relation})
    moments = [int(primary.get("at_world") or 0)] + [int(item.get("at_world") or 0) for item in extras]
    return {
        "id": unit_id(str(primary["ref"])),
        "refs": [str(primary["ref"])] + [str(item["ref"]) for item in extras],
        "primary": str(primary["ref"]),
        "entry": str(primary["entry"]),
        "topic": str(primary["text"])[:40],
        "relation": str(extras[0]["relation"]) if extras else "并列",
        "at_world": max(moments),
        "materials": [primary, *extras],
    }


def constraint_lines(
    unit: dict[str, Any], *, activity: str = "", deferred: bool = False, act: str = ""
) -> list[str]:
    """结构化表达约束（§6.1）：给生成器的范围与口气，不是评分、不是内部字段。"""
    lines = ["她最近能提起的事（只说这些；可以只讲一部分，也可以先不提）："]
    for item in unit.get("materials") or []:
        lines.append(f"- （{item.get('stance')}｜{item.get('source')}）{item.get('text')}")
    if activity:
        lines.append(f"她此刻在做：{activity}")
    lines.append("没列在这里的事她不知道：不要补谁做的、为什么、后来怎样，也不要把听来的说成亲眼见到。")
    lines.append("没把握就用不确定的说法；她自己打算里的事还没做成时只能说打算，不能说已经做了。")
    if act == "承":
        lines.append("这件事还没了结：她自己也在等下文，说不准后面会怎样。")
    elif act == "收":
        lines.append("这件事她心里已经有数了：可以有个收束的说法，不用故意留悬念。")
    if deferred:
        lines.append("这件事她之前没讲出口（还在犹豫）：现在也可以只提一句，或者先按住不说。")
    return lines


def is_open_turn(topic: str) -> bool:
    """无明确查询主题的开场（§5.4 的预筛）：短句或纯招呼语。"""
    text = str(topic or "").strip().lower()
    if not text:
        return True
    if len(text) <= OPEN_TURN_MAX:
        return True
    return any(word in text for word in GREETINGS)


def audit(text: str, unit: dict[str, Any], *, activity: str = "") -> list[dict[str, str]]:
    """最小结构检查（§6.2）：空文本与数字越界。

    语义层（来源 / 时间 / 范围 / 关系 / 处境）走 `audit_request` 那一次便宜判断——
    关键词从来不是唯一判据，判不出来时按通过，不误杀合法叙述。
    """
    body = str(text or "").strip()
    if not body:
        return [{"kind": "empty", "detail": "空文本"}]
    allowed: set[str] = set(_NUMBERS.findall(str(activity or "")))
    for item in unit.get("materials") or []:
        allowed |= set(_NUMBERS.findall(str(item.get("text") or "")))
    extra = sorted(set(_NUMBERS.findall(body)) - allowed)
    if extra:
        return [{"kind": "numbers", "detail": "、".join(extra)}]
    return []


def has_checkable_numbers(text: str) -> bool:
    """数字护栏能核对的前提：正文里有数字（没有就只剩语义判断那一道）。"""
    return bool(_NUMBERS.findall(str(text or "")))


def audit_request(unit: dict[str, Any], text: str, *, activity: str = "") -> list[dict[str, str]]:
    """后验一致性判断的问法（服务层发起一次便宜调用）。"""
    facts = "\n".join(
        f"- （{item.get('stance')}｜{item.get('source')}）{item.get('text')}" for item in unit.get("materials") or []
    )
    return [
        {"role": "system", "content": AUDIT_SYSTEM},
        {
            "role": "user",
            "content": f"她只知道的事：\n{facts}\n她此刻在做：{activity or '（未记录）'}\n她这句话：{text}",
        },
    ]


def parse_audit(text: str) -> tuple[bool, str] | None:
    """解析忠实度判断；解析不出来回 None（调用方按通过处理，不误杀）。"""
    raw = str(text or "").strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("ok"), bool):
        return None
    return bool(payload["ok"]), str(payload.get("why") or "")


# ---------- 戏剧性 / 三幕 / 分享欲（§9.5：原「暂不纳入」项的落地口径） ----------

#: 够戏剧才值得她先动一步、也才压得过「这会儿不想讲」
DRAMA_MIN = 0.5
#: 分享欲低于此值时，素材不够戏剧就先不主动提自己的事
SHARE_DRIVE_MIN = 0.35
ACTS = ("起", "承", "收")


def drama(unit: dict[str, Any], *, blocked: bool = False, unresolved: bool = False) -> float:
    """戏剧性评分（0–1）：有亲历、多来源、有后续、受阻、还悬着。

    这是**排序 / 取舍信号，不是闸门**：它决定她先动哪一步、这会儿说不说，
    不决定世界发生什么——事实仍只从合法候选与受支持效果来（§1.1-2）。
    """
    materials = unit.get("materials") or []
    score = 0.0
    if any(str(item.get("entry")) == "experience" for item in materials):
        score += 0.2
    if len(materials) > 2 or len({str(item.get("source") or "") for item in materials}) > 1:
        score += 0.15
    if str(unit.get("relation")) in ("补充", "连续"):
        score += 0.15
    if blocked:
        score += 0.25
    if unresolved:
        score += 0.3
    return max(0.0, min(1.0, round(score, 4)))


def act_of(unit: dict[str, Any], *, unresolved: bool = False, resolved: bool = False) -> str:
    """三幕位置（组合视图，不是状态机）：起 = 刚起头；承 = 还悬着 / 有后续；收 = 已有终局。

    只用来决定「先想哪一步、用什么口气」；没有合法依据时停在未解决，
    不为了凑完整的故事曲线补事实（§1.1-5）。
    """
    if resolved:
        return "收"
    if unresolved or str(unit.get("relation")) in ("补充", "连续"):
        return "承"
    return "起"


def share_drive(units: Iterable[dict[str, Any]] | None) -> float:
    """分享欲 = 她现有表达倾向单元的**平均置信度**（0–1）。

    不新立平行人格数值：数值就长在既有性格单元里（WORLD_RUNTIME §十），
    黑箱不变——用户看不到这个数，只能感到她这几天话多还是话少。
    """
    live = [
        float(row.get("confidence") or 0.0)
        for row in units or ()
        if not int(row.get("archived") or 0)
    ]
    if not live:
        return 0.5  # 没有可依据的单元：按中性处理，不额外抑制
    return max(0.0, min(1.0, sum(live) / len(live)))


def willingness(drive: float, *, drama_score: float) -> bool:
    """这会儿愿不愿意主动讲自己的事：分享欲低、素材又不戏剧，就先按住不说（§5.2）。"""
    return float(drive) >= SHARE_DRIVE_MIN or float(drama_score) >= DRAMA_MIN


def strict_note() -> str:
    """重试用的加严提醒（主动路径与会话路径同一份措辞，不两处各写一遍）。"""
    return "刚才那版说过了头：只说她确实知道的部分，没把握就用不确定的说法，或者干脆说不知道。"


def _refs_of_row(row: dict[str, Any]) -> list[str]:
    """refs 在库里是 JSON 文本：读侧在这里统一解，不在两处各解一遍。"""
    value = row.get("refs")
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value]
    try:
        parsed = json.loads(str(value or "[]"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def map_payload(
    units: Iterable[dict[str, Any]] | None,
    *,
    text_of: Any = None,
    info_of: Any = None,
) -> dict[str, Any]:
    """故事图谱（管理元数据级）：她讲过的线索、它们之间的关系、以及「没讲出口」的记号。

    - 节点正文只取**已经固化、用户本来就看过**的消息；没讲出口的节点不带内容；
    - 边 = 两条线索共享材料引用（同一件事被再提起）；
    - 不含实情层、未获知内容与他人私聊——黑箱与碎片化不变（DESIGN §2.2-9）。

    ponytail: 边是 O(n²) 的引用比对，够用到现在这个量级（每角色每天至多数条）；
    上百条时改成按 ref 建索引。
    """
    rows = [dict(row) for row in units or ()]
    spoken = sorted(
        (row for row in rows if str(row.get("stage")) == "spoken"),
        key=lambda item: (int(item.get("created_world") or 0), str(item.get("id") or "")),
    )
    held = sorted(
        (row for row in rows if str(row.get("stage")) == "deferred"),
        key=lambda item: (int(item.get("created_world") or 0), str(item.get("id") or "")),
    )
    nodes: list[dict[str, Any]] = []
    refs_of: dict[str, set[str]] = {}
    for row in spoken:
        identifier = str(row.get("id") or "")
        refs_of[identifier] = set(_refs_of_row(row))
        message_id = str(row.get("message_id") or "")
        label = ""
        if callable(text_of):
            label = str(text_of(message_id) or "").strip()
        node: dict[str, Any] = {
            "id": identifier,
            "kind": "spoken",
            "at_world": int(row.get("created_world") or 0),
            "world_day": int(row.get("world_day") or 0),
            "message_id": message_id,
            "label": label[:60] or "（她讲过这件事）",
            "refs": len(refs_of[identifier]),
        }
        if callable(info_of):
            node.update(info_of(row) or {})
        nodes.append(node)
    for row in held:
        nodes.append(
            {
                "id": str(row.get("id") or ""),
                "kind": "deferred",
                "at_world": int(row.get("created_world") or 0),
                "message_id": "",
                "label": "（这件事她没讲出口）",
                "refs": 0,
            }
        )
    edges: list[dict[str, Any]] = []
    ids = [str(node["id"]) for node in nodes if node["kind"] == "spoken"]
    for index, left in enumerate(ids):
        for right in ids[index + 1 :]:
            shared = sorted(refs_of.get(left, set()) & refs_of.get(right, set()))
            if shared:
                edges.append({"from": left, "to": right, "kind": "同一件事又被提起", "refs": shared})
    # 同一天讲的两条按先后串起来：图谱至少看得出她那天在说些什么（不额外暴露内容）
    days: dict[int, list[str]] = {}
    for node in nodes:
        if node["kind"] == "spoken":
            days.setdefault(int(node["world_day"]), []).append(str(node["id"]))
    for day, members in sorted(days.items()):
        for left, right in zip(members, members[1:]):
            edges.append({"from": left, "to": right, "kind": "同一天讲的", "refs": []})
    return {
        "nodes": nodes,
        "edges": edges,
        "counts": {"spoken": len(spoken), "deferred": len(held), "links": len(edges)},
    }

