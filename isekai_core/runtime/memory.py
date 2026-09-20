"""角色记忆（MEMORY_SPEC）：纯函数层——提取校验、衰减、排序融合、简报打包。

主语的**角色**，中心是**角色经历**。这里只做算术与文本处理：不碰数据库、不调模型。
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

#: 规范记忆类型（§三）：事实、片段、承诺、印象
KINDS: tuple[str, ...] = ("fact", "fragment", "promise", "impression")

#: 单次提取的材料条数上限（长 prompt 会让推理型模型把预算烧空、返回空文本）
BATCH_SIZE = 8

#: 一到两句话的硬上限（蒸馏不丢时间/否定/来源，只压长度）
MAX_TEXT_CHARS = 120

#: 强度低于此值进入归档（§六）；归档不是权限豁免
ARCHIVE_BELOW = 0.15

#: 每次成功采纳进入上下文后的强化幅度与上限（§5.3）
REINFORCE_STEP = 0.05
STRENGTH_CAP = 1.0

#: 世界日衰减率（每世界日按比例衰减）
DECAY_PER_DAY = 0.02


def distill(text: str, *, limit: int = MAX_TEXT_CHARS) -> str:
    """蒸馏为一到两句话：压空白、截断在句读处，不删否定词与来源字样。"""
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    for mark in ("。", "；", "，", ".", ";", ","):
        index = cut.rfind(mark)
        if index >= limit // 2:
            return cut[: index + 1]
    return cut.rstrip() + "…"


def decayed_strength(
    strength: float,
    *,
    from_world: int,
    to_world: int,
    day_seconds: int,
    per_day: float = DECAY_PER_DAY,
) -> float:
    """按**世界时长**衰减（§六）：小步与批量补算等价；冻结期间不调用即不衰减。

    时间不进反退（时钟倒拨保护）时按 0 处理，绝不回升。
    """
    days = max(0.0, (int(to_world) - int(from_world)) / max(1, int(day_seconds)))
    value = float(strength) * math.exp(-per_day * days)
    return max(0.0, min(STRENGTH_CAP, value))


def state_for(strength: float) -> str:
    """强度决定状态：活跃 / 衰减中 / 归档（§三）。"""
    if strength < ARCHIVE_BELOW:
        return "archived"
    if strength < 0.5:
        return "decaying"
    return "active"


def reinforce(strength: float, *, step: float = REINFORCE_STEP) -> float:
    """被成功采纳的一轮实际用到才强化，只改易被想起程度、不改采信（§5.3）。"""
    return max(0.0, min(STRENGTH_CAP, float(strength) + step))


def _tokens(text: str) -> set[str]:
    """粗粒度词元：中文字 + 西文词（够用的确定性相似度，不引入分词依赖）。"""
    text = str(text or "")
    words = set(re.findall(r"[a-zA-Z0-9_]+", text.lower()))
    chars = {char for char in text if "\u4e00" <= char <= "\u9fff"}
    return words | chars


def relevance(query: str, text: str) -> float:
    """全文相关性（0..1）：查询词元命中比例，带长度惩罚。"""
    if not query:
        return 0.0
    wanted = _tokens(query)
    if not wanted:
        return 0.0
    hits = len(wanted & _tokens(text))
    return hits / (len(wanted) ** 0.5) / (1.0 + 0.02 * max(0, len(str(text)) - 40))


def rank(
    *,
    query: str,
    entries: list[dict[str, Any]],
    now_world: int,
    day_seconds: int,
    vector_scores: dict[str, float] | None = None,
    recency_weight: float = 0.2,
    strength_weight: float = 0.3,
) -> list[dict[str, Any]]:
    """排序（§5.1）：全文与向量**先归一或秩融合**，再加强度与适度时间权重；同分用稳定标识。"""
    vec = dict(vector_scores or {})

    def _norm(values: dict[str, float]) -> dict[str, float]:
        if not values:
            return {}
        low, high = min(values.values()), max(values.values())
        if high - low < 1e-9:
            return {key: 1.0 for key in values}
        return {key: (value - low) / (high - low) for key, value in values.items()}

    text_raw = {str(item["id"]): relevance(query, str(item.get("text") or "")) for item in entries}
    text_score = _norm(text_raw)
    vec_score = _norm(vec)
    scored: list[dict[str, Any]] = []
    for item in entries:
        ident = str(item["id"])
        parts = [text_score.get(ident, 0.0)]
        if vec_score:
            parts.append(vec_score.get(ident, 0.0))
        base = sum(parts) / len(parts)
        age_days = max(0.0, (int(now_world) - int(item.get("learned_world") or 0)) / max(1, int(day_seconds)))
        recency = 1.0 / (1.0 + age_days / 30.0)
        score = base + strength_weight * float(item.get("strength") or 0.0) + recency_weight * recency
        scored.append({**item, "score": round(score, 6), "relevance": round(base, 6)})
    scored.sort(key=lambda item: (-item["score"], str(item["id"])))
    return scored


def pack_brief(
    entries: list[dict[str, Any]],
    *,
    budget_tokens: int,
    limit: int = 6,
) -> dict[str, Any]:
    """简报（§5.1）：按预算打包、保留来源与确信线索；只进生成上下文，不展示给用户。"""
    lines: list[str] = []
    used: list[str] = []
    budget = max(0, int(budget_tokens))
    for item in entries[: max(0, int(limit))]:
        source = str(item.get("source_label") or "")
        confidence = float(item.get("confidence") or 0.0)
        state = str(item.get("state") or "active")
        cue = ""
        if confidence < 0.5:
            cue = "（她不太确定）"
        elif confidence < 0.75:
            cue = "（她记得大概）"
        if state == "archived":
            cue += "（模糊回想）"
        line = f"- {distill(item.get('text') or '', limit=80)}{cue}"
        if source:
            line += f"〔{source}〕"
        cost = len(line) + 4
        if budget and sum(len(item) + 4 for item in lines) + cost > budget:
            break
        lines.append(line)
        used.append(str(item["id"]))
    return {"lines": lines, "ids": used, "text": "\n".join(lines)}


def extraction_prompt(
    *,
    name: str,
    world_label: str,
    items: list[dict[str, Any]],
    existing: list[dict[str, Any]] | None = None,
) -> list[dict[str, str]]:
    """提取提示（§4.1）：只给该角色已接触的材料，答案必须引用给定来源。"""
    lines = [
        f"你在为角色「{name}」整理记忆，现在是 {world_label}。",
        "只从下面的材料里提取**她值得记住的**内容，输出 JSON 数组，每项：",
        '{"text":"一到两句话","kind":"fact|fragment|promise|impression",'
        '"ref":"材料编号","strength":0.0-1.0,"confidence":0.0-1.0}',
        "规则：",
        "1) text 不超过两句话，保留时间、否定与不确定性；不要写她没有接触过的世界真相；",
        "2) ref 必须是下面材料的编号；不得新增来源，不得编造细节；",
        "3) 联络者（用户）说的外界信息只能记成「联络者所述」，不能当成她亲历；",
        "4) 她的打算记 kind=promise，并在 text 里写明对象与依据；",
        "5) 没有值得记的就输出 []。",
    ]
    if existing:
        lines.append("她已经记得的（同源重复不必再提，相反的新说法要保留来源）：")
        for item in existing[:8]:
            lines.append(f"- [{item.get('id')}] {item.get('text')}")
    lines.append("材料：")
    for item in items:
        when = item.get("when") or ""
        source = item.get("source") or ""
        lines.append(f"[{item['ref']}]（{source}{('，' + when) if when else ''}）{item.get('text')}")
    return [{"role": "system", "content": "\n".join(lines)}]


def parse_extraction(text: str, valid_refs: set[str]) -> list[dict[str, Any]]:
    """解析并校验提取结果：来源必须真实、类型在闭集、文本有界；不合规整条丢弃。"""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = raw.rstrip("`")
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, list):
        return []
    out: list[dict[str, Any]] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        ref = str(item.get("ref") or "")
        if ref not in valid_refs:
            continue  # 来源不存在 / 不是给她的材料 → 丢弃（不拿猜测填空）
        kind = str(item.get("kind") or "")
        if kind not in KINDS:
            continue
        distilled = distill(item.get("text") or "")
        if not distilled:
            continue

        def _num(key: str, default: float) -> float:
            value = item.get(key)
            if isinstance(value, (int, float)):
                return max(0.0, min(1.0, float(value)))
            return default

        out.append(
            {
                "text": distilled,
                "kind": kind,
                "ref": ref,
                "strength": _num("strength", 0.6),
                "confidence": _num("confidence", 0.7),
            }
        )
    return out


def same_fact(a: str, b: str) -> bool:
    """同事实判定（去重用）：词元重合度高且不矛盾（不靠向量相似合并相反陈述）。"""
    left, right = _tokens(a), _tokens(b)
    if not left or not right:
        return False
    overlap = len(left & right) / len(left | right)
    return overlap >= 0.6


NEGATIONS = ("不", "没", "无", "非", "别", "未", "没在", "不再")


def contradicts(a: str, b: str) -> bool:
    """是否互相矛盾（保留双方、建立替代关系，而不是静默覆写）。

    只看**差异部分**里的否定词：共同语境里的「未毕」之类不该把两句都判成否定。
    """
    if not same_fact(a, b):
        return False
    left_tokens, right_tokens = _tokens(a), _tokens(b)
    neg = lambda items: {  # noqa: E731  单字否定词（中文按字切）
        token for token in items if token in NEGATIONS or any(word in token for word in NEGATIONS)
    }
    return bool(neg(left_tokens - right_tokens)) != bool(neg(right_tokens - left_tokens))


def source_label(sources: list[dict[str, Any]]) -> str:
    """来源标签：让角色能说清「亲历 / 听说 / 读到的 / 联络者所述」（§三）。"""
    labels: list[str] = []
    for item in sources:
        kind = str(item.get("kind") or "")
        role = str(item.get("source_role") or "")
        via = str(item.get("via") or "")
        if kind == "experience":
            labels.append("亲历")
        elif kind == "claim":
            labels.append("听说" if via else "读到的")
        elif kind == "dialog":
            labels.append("联络者所述" if role == "user" else "对话")
        elif kind == "intent":
            labels.append("她自己的打算")
        else:
            labels.append("记忆")
    seen: list[str] = []
    for item in labels:
        if item not in seen:
            seen.append(item)
    return "／".join(seen)
