"""主动发言（SESSION_CORE_SPEC §五）：素材、配额、节律、唯一目标、不补发。纯函数层。"""

from __future__ import annotations

from typing import Any

#: 素材新鲜度：超过一个世界日的事实不再用来发起主动消息（过期问候不补发）
FRESH_DAYS = 1


def sleeping(activity: str) -> bool:
    """睡眠期不主动发送（无睡眠角色由 life 模板决定，不强行套现实夜间）。"""
    return str(activity or "").strip().lower() == "sleep"


def candidates(
    knowledge: list[dict[str, Any]],
    *,
    world_seconds: int,
    day_seconds: int,
    consumed: set[str],
    own_actions: set[str] | None = None,
) -> list[dict[str, Any]]:
    """她可分享的素材：已获知、仍在时效内、且没被消费过的事件。

    只吃「她已获知」的东西——全局事件标记不能绕过认知，秘密原文也不在这里送出去。
    """
    own = own_actions or set()
    out: list[dict[str, Any]] = []
    for row in knowledge:
        target = str(row.get("target") or "")
        if not target or target in consumed or target in own:
            continue
        learned = int(row.get("world_seconds") or 0)
        if learned > int(world_seconds):
            continue
        if int(world_seconds) - learned > FRESH_DAYS * max(1, int(day_seconds)):
            continue
        out.append(
            {
                "ref": target,
                "kind": str(row.get("kind") or ""),
                "text": str(row.get("text") or ""),
                "learned_world": learned,
                "source": str(row.get("source") or ""),
            }
        )
    return out


def quota_left(used_today: int, per_day: int) -> int:
    return max(0, int(per_day) - int(used_today))


def should_speak(
    *,
    archived: bool,
    activity: str,
    quota: int,
    materials: list[dict[str, Any]],
) -> tuple[bool, str]:
    """要不要开口：归档 / 睡眠 / 没额度 / 没素材，任何一条不满足就不生成。"""
    if archived:
        return False, "已归档"
    if sleeping(activity):
        return False, "睡眠期"
    if quota <= 0:
        return False, "今日额度用完"
    if not materials:
        return False, "没有可用素材"
    return True, ""


def proactive_text_allowed(text: str) -> bool:
    """最简内容闸：空、超长、或明显是内部字段名的一律不要。"""
    body = str(text or "").strip()
    if not body or len(body) > 400:
        return False
    lowered = body.lower()
    return not any(token in lowered for token in ("system_prompt", "reply_to", "instance_id", "{"))
