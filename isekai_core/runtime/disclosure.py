"""多角色披露（SESSION_CORE_SPEC §七，阶段 5）。

默认隔离：A 的私聊不进 B 的认知。用户明确向 B 披露某条消息 / 片段时，落地一条**授权记录**
（接收角色、来源、范围、生效水位，随时间线版本化），B 的召回与扮演定义据此看到**转述**——
授权是作用域，不是把 A 的整库复制过去；披露改变的是 B 可访问的认知，不改世界真值，
也不把 A 的经历变成 B 的亲历。

撤回只有一条路：回滚（§7.2），没有单独的撤回入口。
"""

from __future__ import annotations

from typing import Any


def normalize_scope(payload: dict[str, Any]) -> dict[str, Any]:
    """规范化披露范围：必须明确到具体来源，含糊转述不产生授权（§7.1）。"""
    from_character = str(payload.get("from_character") or "")
    to_character = str(payload.get("to_character") or "")
    if not from_character or not to_character:
        raise ValueError("披露需要来源角色与接收角色")
    if from_character == to_character:
        raise ValueError("不能向角色自己披露自己的对话")
    refs = [str(item) for item in (payload.get("refs") or []) if str(item).strip()]
    if not refs:
        raise ValueError("披露范围必须是明确的对话片段（refs 不能为空）")
    return {
        "from_character": from_character,
        "to_character": to_character,
        "refs": refs,
        "note": str(payload.get("note") or ""),
    }


def transcribe_entry(fragment: dict[str, Any]) -> dict[str, Any]:
    """把披露到的片段做成**接收角色的转述条目**：来源标成「联络者转述」，不是亲历（§7.1）。"""
    speaker = str(fragment.get("source_name") or fragment.get("from_character") or "另一个人")
    return {
        "kind": "fragment",
        "text": f"联络者转述了{speaker}说过的话：{str(fragment.get('text') or '')}",
        "sources": [{
            "kind": "dialog",
            "ref": str(fragment.get("ref") or ""),
            "source_role": "other_character",
            "via": str(fragment.get("disclosure_id") or ""),
        }],
    }


def brief_block(fragments: list[dict[str, Any]]) -> list[str]:
    """扮演定义里的披露块：只列已授权的转述，标明是听来的。"""
    if not fragments:
        return []
    lines = ["联络者明确给你看过这些转述（不是你亲历的，别当成你在场）："]
    for item in fragments[:6]:
        speaker = str(item.get("source_name") or "另一个人")
        lines.append(f"- {speaker}说过：{str(item.get('text') or '')}")
    return lines
