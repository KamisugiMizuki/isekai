"""认知接口：按角色返回信息子集（WORLD_RUNTIME_SPEC §13、CHARACTER_CARD_SPEC §6）。

- 输入必须含实例、时间线、角色、查询水位；输出该角色截至该水位已可接触的子集；
- **实情层不进入返回结果**：角色拿到的说法都带来源、获知时间与主观确信度；
- 硬约束角色只接受卡片允许的来源（自身经历 / 小环境 / 用户通讯）；
- 卡片 creator 段是幕后设定，永远不进扮演上下文。
"""

from __future__ import annotations

from typing import Any

HARD_ALLOWED = ("self_experience", "small_env", "user_contact")

STANCE_LABEL = {
    "believed": "当作实情",
    "doubted": "将信将疑",
    "recorded": "只是读到过",
    "experienced": "亲身经历",
    "told": "听人说的",
}


def _source_name(package: dict[str, Any], source_id: str | None) -> str:
    for item in package.get("sources") or []:
        if item.get("id") == source_id:
            return str(item.get("name") or source_id)
    return "无来源" if not source_id else str(source_id)


def _index(items: Any) -> dict[str, dict[str, Any]]:
    return {str(item.get("id")): item for item in items or [] if isinstance(item, dict)}


def knowledge_slice(
    package: dict[str, Any],
    card: dict[str, Any],
    *,
    world_seconds: int,
    experiences: list[dict[str, Any]] | None = None,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """该角色截至 world_seconds 可引用的知识条目（不含任何实情层条目原文）。"""
    cognition = card.get("cognition") or {}
    hard = str(cognition.get("mode") or "soft") == "hard"
    allowed = {str(item) for item in (cognition.get("sources") or [])}

    canon = _index(package.get("canon"))
    narratives = _index(package.get("narratives"))
    storiettes = _index(package.get("historiography"))
    channels = {str(item.get("source_id")) for item in card.get("channels") or []}

    out: list[dict[str, Any]] = []
    for entry in card.get("initial_knowledge") or []:
        kind = str(entry.get("ref_type"))
        if kind == "self":
            if hard and "self_experience" not in allowed:
                continue
            out.append(
                {
                    "text": str(entry.get("claim") or ""),
                    "source": "自己的记忆",
                    "learned": None,
                    "stance": STANCE_LABEL["experienced"],
                }
            )
            continue
        if kind == "narrative":
            item = narratives.get(str(entry.get("ref_id")))
            if not item:
                continue
            source_id = str(item.get("source_id") or "")
            if hard or source_id not in channels:
                continue
            out.append(
                {
                    "text": str(item.get("text") or ""),
                    "source": _source_name(package, source_id),
                    "learned": entry.get("obtained_at"),
                    "stance": STANCE_LABEL.get(str(item.get("confidence") or "believed"), "将信将疑"),
                }
            )
            continue
        if kind == "historiography":
            unit = storiettes.get(str(entry.get("ref_id")))
            if not unit or hard:
                continue
            for ref in entry.get("scope") or []:
                item = canon.get(str(ref)) or narratives.get(str(ref))
                if not item:
                    continue
                text = item.get("statement") or item.get("text") or ""
                out.append(
                    {
                        "text": str(text),
                        "source": f"史料《{unit.get('title')}》",
                        "learned": entry.get("obtained_at"),
                        "stance": STANCE_LABEL["recorded"],
                    }
                )
            continue
        if kind == "canon":
            # 实情层条目只在卡片明确以「读到某本传本」的方式掌握时才返回，且以记录形式呈现
            if hard or not entry.get("ref_id"):
                continue
            title = "未知传本"
            for unit in storiettes.values():
                if entry.get("ref_id") in (unit.get("entries") or []):
                    title = str(unit.get("title") or title)
                    source_id = str(unit.get("id"))
                    break
            item = canon.get(str(entry.get("ref_id")))
            if not item:
                continue
            out.append(
                {
                    "text": str(item.get("statement") or ""),
                    "source": f"史料《{title}》",
                    "learned": entry.get("obtained_at"),
                    "stance": STANCE_LABEL["recorded"],
                }
            )
    for row in experiences or []:
        if int(row.get("world_seconds") or 0) > world_seconds:
            continue
        if str(row.get("kind")) == "knowledge" and hard:
            continue
        out.append(
            {
                "text": str(row.get("summary") or ""),
                "source": "自己的经历",
                "learned": int(row.get("world_seconds") or 0),
                "stance": STANCE_LABEL["experienced"],
            }
        )
    return out[-limit:]


def render_prompt(context: dict[str, Any]) -> str:
    """把已过滤的扮演定义渲染成系统提示（只含可注入内容；幕后设定不在此出现）。"""
    character = context.get("character") or {}
    lines: list[str] = [
        "你在扮演一个生活在既有世界里的角色，只用她的口吻、她的信息作答。",
        f"世界的体裁与边界：{context.get('genre') or '（未声明）'}",
        f"当前世界时刻：{context.get('world_time') or '（未知）'}",
    ]
    if context.get("current_activity"):
        lines.append(f"她此刻正在做的事：{context['current_activity']}")
    lines.append(
        "她的身份："
        + "、".join(
            item
            for item in (
                character.get("name"),
                character.get("gender"),
                character.get("occupation"),
                f"自我认同：{character.get('self_identity')}" if character.get("self_identity") else "",
                f"常在：{character.get('region')}" if character.get("region") else "",
            )
            if item
        )
    )
    if character.get("appearance"):
        lines.append(f"外貌：{character['appearance']}")
    if context.get("self_knowledge"):
        lines.append(f"她自己的记忆：{context['self_knowledge']}")
    first = context.get("first_contact") or {}
    if first.get("stance") or first.get("intent"):
        lines.append(f"与联络者初见的姿态：{first.get('stance', '')}；意向：{first.get('intent', '')}")
    if context.get("voice"):
        lines.append("表达倾向：" + "；".join(context["voice"][:6]))
    for item in context.get("comms") or []:
        lines.append(f"联络方式：{item.get('name', '')}（限制：{item.get('limits', '')}）")
    knowledge = context.get("knowledge") or []
    if knowledge:
        lines.append("她已能接触到以下内容（引用时只按这里的范围与口气，不要扩写）：")
        for item in knowledge:
            lines.append(f"- [{item.get('source')}｜{item.get('stance')}] {item.get('text')}")
    else:
        lines.append("她没有掌握任何世界内信息，只能凭自身经历与联络者的话作答。")
    lines.extend(
        [
            "约束：不确定就说不确定，不知道就说不知道；不要提到你未列出的世界内幕、他人私聊或未来事件；",
            "不要把「听说」说成亲历，不要把推测说成事实；只输出角色要说的话，不解释规则、不输出内部结构。",
        ]
    )
    return "\n".join(lines)


def play_context(
    package: dict[str, Any],
    card: dict[str, Any],
    *,
    world_seconds: int,
    calendar_label: str,
    current_activity: str = "",
    units: list[dict[str, Any]] | None = None,
    experiences: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """扮演定义：只含已过滤内容（体裁 / 通讯 / 自知 / 表达倾向 / 已可接触的知识）。

    刻意**不含**：卡片 creator 段、实情层未获知条目、其他角色对话、未来事件。
    """
    identity = card.get("identity") or {}
    mechanisms = {str(item.get("id")): item for item in (package.get("comms") or {}).get("mechanisms") or []}
    comms = []
    for item in card.get("comms") or []:
        mechanism = mechanisms.get(str(item.get("mechanism_id"))) or {}
        comms.append(
            {
                "name": str(mechanism.get("name") or ""),
                "limits": str(mechanism.get("limits") or ""),
                "note": str(item.get("note") or ""),
            }
        )
    voice = [
        str(row.get("semantic"))
        for row in (units or [])
        if not int(row.get("archived") or 0)
    ]
    return {
        "genre": str((package.get("meta") or {}).get("description") or ""),
        "world_time": calendar_label,
        "current_activity": current_activity,
        "character": {
            "name": str(identity.get("name") or ""),
            "gender": str(identity.get("gender") or ""),
            "occupation": str(identity.get("occupation") or ""),
            "self_identity": str(identity.get("self_identity") or ""),
            "region": str(card.get("region") or ""),
            "appearance": str(card.get("appearance") or ""),
        },
        "self_knowledge": str((card.get("background") or {}).get("self_knowledge") or ""),
        "first_contact": dict(card.get("first_contact") or {}),
        "voice": voice,
        "comms": comms,
        "knowledge": knowledge_slice(
            package, card, world_seconds=world_seconds, experiences=experiences
        ),
    }
