"""事件文本表述与惰性展开（EVENT_ENGINE_SPEC §3.2 / §3.4）。

LLM 只做一件事：把**已经确定**的骨架说成人话。它不决定发生时间、数值、参与者生死、
不新增因果转移；校验不过就重试，仍不过就退回不新增事实的模板（读取、重启、换消费者
都不重新生成已固化的文本）。展开（§3.4）只展开既定内容，产出的是**派生记录**，
不原地改写原事件或最初文本，也不虚构读书经历。
"""

from __future__ import annotations

import re
from typing import Any

#: 数字（含中文数字）——校验用：骨架里的数字必须还在，正文不许冒出新数字
_NUMBERS = re.compile(r"\d+(?:\.\d+)?")


def skeleton_prompt(event: dict[str, Any], claims: list[dict[str, Any]]) -> list[dict[str, str]]:
    """把骨架交给模型：只允许换说法，不允许改事实。"""
    lines = [
        "你在为一个虚构世界把既定的**事实骨架**写成一句平实的实情叙述（给世界内部一致性用），",
        "并为若干来源各写一句该来源会怎么传出去的说法。",
        "硬约束：不许改动时间、数量、参与者和生死；不许补出未写出的原因、责任人、结果；",
        "不要把原始世界秒写进正文；不要新增任何骨架里没有的数字；",
        "不许把不确定写成确定；每句不超过 60 字；只输出 JSON。",
        f"事实骨架：{event.get('summary', '')}",
        f"（仅供你参考的时刻，不要写进正文：世界秒 {event.get('world_seconds', 0)}）",
        "来源列表："
        + ("；".join(f"{item.get('source_id')}={item.get('audience')}" for item in claims) or "无"),
        '输出格式：{"detail": "……", "claims": {"<来源标识>": "……"}}',
    ]
    return [{"role": "system", "content": "\n".join(lines)}]


def facts_preserved(rendered: str, skeleton: str) -> bool:
    """最小事实校验：骨架里的数字必须保留，正文不得出现骨架外的新数字。"""
    want = set(_NUMBERS.findall(skeleton))
    got = set(_NUMBERS.findall(rendered))
    if not want:
        return True
    return want <= got and got <= want


def parse_render(text: str, claims: list[dict[str, Any]]) -> dict[str, Any] | None:
    """解析模型输出；缺字段或与来源对不上就判失败（由调用方重试或退回模板）。"""
    import json

    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[-1] if "\n" in raw else raw
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict) or not str(payload.get("detail") or "").strip():
        return None
    rendered_claims = payload.get("claims") if isinstance(payload.get("claims"), dict) else {}
    out: dict[str, Any] = {"detail": str(payload["detail"]).strip(), "claims": {}}
    for item in claims:
        value = rendered_claims.get(str(item.get("source_id")))
        if isinstance(value, str) and value.strip():
            out["claims"][str(item.get("source_id"))] = value.strip()
    return out


def expand_prompt(claim: dict[str, Any], *, question: str) -> list[dict[str, str]]:
    """惰性展开：只展开既定内容，依据不足就保留不知道（§3.4）。"""
    return [
        {
            "role": "system",
            "content": "\n".join(
                [
                    "你要把一条**已经存在**的世界内记载展开成两三句同一来源口吻的文字。",
                    "只用给到的记载本身：不补出未确定的参与者、生死、死因、幕后责任人、结果或状态；",
                    "依据不足就明确写成「不可考 / 没有记下」，不要猜测；不要写阅读经历；",
                    "只输出正文，不要解释你的做法。",
                    f"记载（来源 {claim.get('source_id', '')}）：{claim.get('text', '')}",
                    f"要展开的问题：{question}",
                ]
            ),
        }
    ]


def expansion_is_grounded(text: str, claim: dict[str, Any]) -> bool:
    """展开不得引入原记载之外的新数字（最小事实护栏）。"""
    want = set(_NUMBERS.findall(str(claim.get("text") or "")))
    got = set(_NUMBERS.findall(text))
    return got <= want
