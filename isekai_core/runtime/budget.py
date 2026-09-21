"""跨任务调用预算（WORLD_RUNTIME_SPEC §2.8）。

三层：实例总预算 > 时间线预算 > 单任务预算；每个外部调用在发起前**原子预占**，成功 / 失败 /
超时 / 取消都按真实消耗结算。优先级固定（下面的 PRIORITIES 顺序即优先级），预算紧张时先停
最低优先级的新调用，并给更高优先级的任务留出保留额度。

纯函数：`decide()` 只做算术与判定，不碰数据库、不调模型。
"""

from __future__ import annotations

from typing import Any

#: 固定优先级（越靠前越高，§2.8）：安全校验与事实一致性 > 已接受对话的最终提交 >
#: 世界事实推进所需的确定性处理 > 用户已确认的回填 / 展开 > 生活计划与记忆提取 >
#: 主动消息文本 > 向量化与非必要润色。
PRIORITIES: tuple[str, ...] = (
    "safety",
    "dialog_commit",
    "deterministic_facts",
    "confirmed_backfill",
    "plan_memory",
    "proactive_text",
    "embedding_polish",
)

#: 会被优先停掉的档位（这些任务额外需要保留额度之外的空间）
LOW_PRIORITY_FROM = 5

TASK_PRIORITY: dict[str, str] = {
    "event_render": "confirmed_backfill",
    "event_expand": "confirmed_backfill",
    "intent_propose": "plan_memory",
    "life_refine": "plan_memory",
    "memory_extract": "plan_memory",
    "narrative_audit": "plan_memory",   # 每条可见回复的后验审计（§6.2）：与记忆提取同档
    "proactive_text": "proactive_text",
    "embedding": "embedding_polish",
}


def priority_of(task: str) -> str:
    return TASK_PRIORITY.get(task, "plan_memory")


def priority_index(name: str) -> int:
    try:
        return PRIORITIES.index(name)
    except ValueError:
        return len(PRIORITIES) - 1


def task_index(task: str) -> int:
    return priority_index(priority_of(task))


def decide(
    *,
    usage: dict[str, Any],
    timeline_id: str,
    task: str,
    priority: int,
    tokens_est: int,
    limits: dict[str, int],
    reserved_for_higher: dict[int, int] | None = None,
) -> dict[str, Any]:
    """判断这次调用能不能发起：三层逐层检查，还要给更高优先级留出保留额度。"""
    instance_used = int(usage.get("instance") or 0)
    line_used = int((usage.get("timelines") or {}).get(timeline_id, 0))
    task_used = int((usage.get("tasks") or {}).get(f"{timeline_id}|{task}", 0))
    instance_limit = int(limits.get("instance_tokens_per_day") or 0)
    line_limit = int(limits.get("timeline_tokens_per_day") or 0)
    task_limit = int(limits.get("task_tokens_per_day") or 0)
    blocked: list[str] = []
    if instance_limit and instance_used + tokens_est > instance_limit:
        blocked.append("instance")
    if line_limit and line_used + tokens_est > line_limit:
        blocked.append("timeline")
    if task_limit and task_used + tokens_est > task_limit:
        blocked.append("task")
    if priority >= LOW_PRIORITY_FROM:
        reserve = int((reserved_for_higher or {}).get(priority, 0))
        if instance_limit and instance_used + tokens_est + reserve > instance_limit:
            blocked.append("reserved_for_higher")
    return {
        "ok": not blocked,
        "blocked": blocked,
        "priority": PRIORITIES[priority] if 0 <= priority < len(PRIORITIES) else "unknown",
        "used": {"instance": instance_used, "timeline": line_used, "task": task_used},
        "limits": {
            "instance_tokens_per_day": instance_limit,
            "timeline_tokens_per_day": line_limit,
            "task_tokens_per_day": task_limit,
        },
    }


def estimate_tokens(text: str) -> int:
    """粗略量级估计（中文按字计、其他按 4 字符 1 token），只用于预占，不作为账单。"""
    text = text or ""
    wide = sum(1 for char in text if ord(char) > 0x2E80)
    narrow = len(text) - wide
    return int(wide * 1.2 + narrow / 3.2) + 64


def usage_from_result(result: Any) -> int:
    """从模型调用结果里取真实 token 量级（缺字段就按 0，不猜）。"""
    if not isinstance(result, dict):
        return 0
    for key in ("total_tokens", "tokens", "usage_tokens"):
        value = result.get(key)
        if isinstance(value, int) and value > 0:
            return value
    usage = result.get("usage")
    if isinstance(usage, dict):
        total = sum(int(usage.get(key) or 0) for key in ("prompt_tokens", "completion_tokens"))
        return total
    return 0
