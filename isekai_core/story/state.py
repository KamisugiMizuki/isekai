"""产品状态与返回信封（OC_STORY_LAYER_SPEC §3.5 / §4.4 / §七）：底层结果 → 用户能理解的状态。

三条硬规矩：

- **OC 层不复制会话核心状态机**：`queued` / `processing` / `fixed` / `accepted` / `failed`
  仍留在会话与投递层，这里只消费它们的公开投影（§4.4 末段）；
- **一个产品轮次只显示一个主状态**，投递结果另作附加状态（§4.4 首段）；
- **不重新定义成功语义**：`expressed` 只表示「消息已固化」，既不暗示已送达，
  也不暗示她说的内容变成了世界公共事实（§3.5）。
"""

from __future__ import annotations

from typing import Any

#: 产品状态闭集（§4.4 表格左列）
PRODUCT_STATES: tuple[str, ...] = (
    "preparing",
    "available",
    "catching_up",
    "generating",
    "expressed",
    "deferred",
    "handoff",
    "blocked",
)

#: 场景级状态：打开角色时能看到的长期状态（轮次级另外三值只在轮次上出现）
SCENE_STATES: tuple[str, ...] = ("preparing", "available", "catching_up", "blocked")

#: 会话核心返回的产品结果（§3.5）：本层只在这五个值里回报
RESULT_STATUSES: tuple[str, ...] = ("expressed", "waiting", "blocked", "deferred", "handoff")

LABELS: dict[str, str] = {
    "preparing": "准备中",
    "available": "可联络",
    "catching_up": "世界追赶中",
    "generating": "生成中",
    "expressed": "已表达",
    "deferred": "暂缓",
    "handoff": "需转交",
    "blocked": "被阻断",
}

#: 用户可做的事（§4.4 第三列）：机器可读的键 + 产品语言的标签
ACTIONS: dict[str, tuple[str, ...]] = {
    "preparing": ("wait", "edit_creation"),
    "available": ("share", "ask", "followup"),
    "catching_up": ("wait", "read_history"),
    "generating": ("wait", "cancel_local"),
    "expressed": ("read_delivery", "continue"),
    "deferred": ("continue",),
    "handoff": ("open_flow",),
    "blocked": ("view_reason", "recover_or_export"),
}

ACTION_LABELS: dict[str, str] = {
    "wait": "等待",
    "edit_creation": "修改创作输入",
    "share": "分享",
    "ask": "询问",
    "followup": "追问",
    "read_history": "查看已完成历史",
    "cancel_local": "取消仍可取消的本地操作",
    "read_delivery": "查看投递状态",
    "continue": "继续联络",
    "open_flow": "打开对应流程",
    "view_reason": "查看原因",
    "recover_or_export": "恢复或导出",
}

#: 「不得暗示」（§4.4 第四列）：客户端文案与探针共用一份，别在界面文案里自由发挥
MUST_NOT_IMPLY: dict[str, str] = {
    "preparing": "角色已经生活在完整世界里",
    "available": "当前一定有新故事",
    "catching_up": "目标时刻已经发生",
    "generating": "回复已经固化",
    "expressed": "通道已经送达或世界事实已改变",
    "deferred": "系统丢失了一个必达剧情",
    "handoff": "普通聊天已执行了请求",
    "blocked": "可以靠重试绕过闸门",
}

#: 转交轮次的入站标记：`error_code = handoff:<目标流程>`
HANDOFF_PREFIX = "handoff:"

#: 失败类别（§七 第 5 条要明确区分的五种），用于把底层错误翻译成产品状态
ERROR_KINDS: dict[str, tuple[str, ...]] = {
    "model": (
        "llm_unreachable",
        "llm_unavailable",
        "llm_rejected",
        "llm_bad_response",
        "llm_not_configured",
        "empty_completion",
        "truncated_completion",
        "generation_failed",
        "internal",
    ),
    "world": ("overloaded", "rate_limited", "catching_up"),
    "storage": ("persistence_blocked",),
    "version": (
        "state_blocked",
        "voided",
        "frozen",
        "archived",
        "binding_expired",
        "compatibility_blocked",
        "rollback",
    ),
    "channel": (
        "protocol_error",
        "bad_frame",
        "unsupported_type",
        "auth_required",
        "auth_failed",
        "unsupported_capability",
        "unknown_thread",
        "not_found",
        "invalid_input",
    ),
}

#: 失败类别 → 产品状态：模型失败与「没想好怎么说」都只是这一轮没有新增事实，
#: 存储 / 版本 / 通道闸门则是不允许继续（§七）。
KIND_STATE: dict[str, str] = {
    "model": "deferred",
    "world": "catching_up",
    "storage": "blocked",
    "version": "blocked",
    "channel": "blocked",
    "routing": "handoff",
}

#: 失败类别 → 用户可理解的一句话（不含内部字段、错误码与技术细节）
KIND_NOTES: dict[str, str] = {
    "model": "这次没生成出可用的回应；已发生的经历与她听过的事都还在，可以再聊一次。",
    "world": "世界还在追赶，这一刻还没成为已完成的事实。",
    "storage": "存储不可用：先不产生新的内容，等恢复后再继续。",
    "version": "当前状态不允许继续（版本 / 成员资格 / 实例状态），可以查看原因或恢复、导出。",
    "channel": "这条没能按通道协定的格式送达，固化内容没有改动。",
    "routing": "这条请求不在普通联络里执行，已经交给对应流程。",
}


def error_kind(code: str) -> str:
    """底层错误码 → 失败类别；不认识的一律按「世界」类处理（不改事实、可重试）。"""
    text = str(code or "")
    if text.startswith(HANDOFF_PREFIX):
        return "routing"
    for kind, codes in ERROR_KINDS.items():
        if text in codes:
            return kind
    return "world"


def error_state(code: str) -> str:
    """底层错误码 → 产品状态。"""
    return KIND_STATE[error_kind(code)]


def scene_state(
    *,
    scope_ready: bool,
    compatibility_blocked: bool = False,
    persistence_blocked: bool = False,
    timeline_state: str = "",
    catching_up: bool = False,
) -> str:
    """场景级主状态（§4.4 前四行 + 被阻断）：打开角色时看到的那一个。"""
    if not scope_ready:
        return "preparing"
    if persistence_blocked or compatibility_blocked:
        return "blocked"
    if str(timeline_state) in ("frozen", "archived"):
        return "blocked"
    if catching_up:
        return "catching_up"
    return "available"


def turn_state(
    *,
    scene: str,
    inbound_state: str = "",
    error_code: str = "",
    has_reply: bool = False,
) -> str:
    """轮次级主状态（§4.4 全部八行里挑一个）。

    `scene` 是场景级状态；闸门优先级最高（不能靠重试绕过），其次是轮次自身的事实。
    """
    if scene == "blocked":
        return "blocked"
    state = str(inbound_state or "")
    code = str(error_code or "")
    if state in ("queued", "processing"):
        # 世界追赶中优先于「生成中」：这一刻还没成为已完成的事实（§4.3）
        return "catching_up" if scene == "catching_up" else "generating"
    if state == "cancelled":
        if code.startswith(HANDOFF_PREFIX):
            return "handoff"
        return "blocked" if error_state(code) == "blocked" else "deferred"
    if state == "failed":
        return "deferred"
    if state == "done" and has_reply:
        return "expressed"
    return scene if scene != "available" else "deferred"


def result_of(product_state: str) -> str:
    """产品状态 → §3.5 的五类返回结果。"""
    return {
        "preparing": "waiting",
        "available": "waiting",
        "catching_up": "waiting",
        "generating": "waiting",
        "expressed": "expressed",
        "deferred": "deferred",
        "handoff": "handoff",
        "blocked": "blocked",
    }.get(str(product_state), "waiting")


def delivery_extra(rollup: str, *, batches: int = 0) -> dict[str, Any]:
    """投递附加状态（§4.4：只作附加，不当主状态）。

    `expressed` 只说明已固化；是否送达要看这里，`unknown` / `failed` 不得说成成功（§7 表）。
    """
    state = str(rollup or "unknown")
    return {
        "state": state,
        "batches": int(batches),
        "delivered": state in ("delivered", "accepted"),
        "note": {
            "delivered": "已送达通道",
            "accepted": "通道已确认接收",
            "sent": "已发出，尚未收到确认",
            "pending": "等待投递",
            "unknown": "投递结果未知：不当作已送达",
            "failed": "投递失败：内容已固化，可重发，但不会重新生成",
            "incompatible": "通道当前限额与固化内容不兼容：不重排、不裁剪、不重新生成",
        }.get(state, "投递状态未知"),
    }


def envelope(
    *,
    instance_id: str,
    timeline_id: str,
    product_state: str,
    character_id: str = "",
    session_id: str = "",
    reason: str = "",
    observed_revision: int = 0,
    world_time: int = 0,
    processed_watermark: int = 0,
    runtime_generation: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """本层的统一返回（§3.5 作用域 + §4.4 主状态 + 可做的事 + 不得暗示）。"""
    state = str(product_state)
    out: dict[str, Any] = {
        "status": result_of(state),
        "product_state": state,
        "label": LABELS.get(state, state),
        "instance_id": str(instance_id),
        "timeline_id": str(timeline_id),
        "character_id": str(character_id),
        "session_id": str(session_id),
        "observed_revision": int(observed_revision),
        "world_time": int(world_time),
        "processed_watermark": int(processed_watermark),
        "runtime_generation": int(runtime_generation),
        "can": list(ACTIONS.get(state, ())),
        "must_not_imply": MUST_NOT_IMPLY.get(state, ""),
    }
    if reason:
        out["reason"] = str(reason)
    out.update(extra or {})
    return out
