"""用户可见面与黑箱边界（OC_STORY_LAYER_SPEC §六）。**白名单构造，不做透传。**

- 默认可见：当前角色 / 世界 / 会话名称、已完成的世界时刻（现实时间与世界时间分别标出）、
  角色消息与其投递状态、必要反馈（创作 / 导入 / 版本 / 世界停止推进）、高级模式里的版本操作结果；
- 默认不可见：实情层与未获知事件、事件表 / 记忆表 / 认知条目 / 内部评分、候选池 / 分享分数 /
  后验审计细节、模型提示词与数据库结构；
- 所以这里的函数**只从参数拼字段**：底层行里的多余列不会被顺手带出去，
  `internals_in()` 给测试与探针一个可核对的判据（§6.2 的那些名字一个都不许出现在产品面上）。
"""

from __future__ import annotations

from typing import Any, Iterable

#: 产品面上不许出现的内部字段名（§6.2）：出现在键里即越界
INTERNAL_TOKENS: tuple[str, ...] = (
    "truth",
    "canon",
    "claim",
    "cognition",
    "memory",
    "score",
    "prompt",
    "audit",
    "candidate",
    "narrative",
    "reaction",
    "intent",
    "effect",
    "share_drive",
    "drama",
    "embedding",
    "budget",
    "ledger",
    "rule_state",
    "event_draft",
    "reality",
)

#: 消息作者 → 产品语言（用自己的话，不用内部 role 值）
AUTHORS: dict[str, str] = {"user": "you", "character": "her", "notice": "system"}


def internals_in(payload: Any, *, _path: str = "") -> list[str]:
    """产品面自检：返回命中的内部字段路径（空 = 干净）。只查键，不查正文。"""
    found: list[str] = []
    if isinstance(payload, dict):
        for key, value in payload.items():
            name = str(key)
            low = name.lower()
            if any(token in low for token in INTERNAL_TOKENS):
                found.append(f"{_path}{name}")
            found.extend(internals_in(value, _path=f"{_path}{name}."))
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found.extend(internals_in(item, _path=f"{_path}{index}."))
    return found


def message_row(row: dict[str, Any], *, text: str = "", delivery: str = "") -> dict[str, Any]:
    """一条可见消息：作者、正文、处理状态，以及她那条的投递状态（§6.1）。"""
    out: dict[str, Any] = {
        "seq": int(row.get("seq") or 0),
        "from": AUTHORS.get(str(row.get("role") or ""), "system"),
        "text": str(text or ""),
        "state": str(row.get("state") or ""),
    }
    if delivery:
        out["delivery"] = str(delivery)
    return out


def home_payload(
    *,
    scene: dict[str, Any],
    character: tuple[str, str],
    world_name: str,
    timeline: tuple[str, str],
    session: dict[str, str],
    time_info: dict[str, Any],
    messages: Iterable[dict[str, Any]] = (),
    notes: Iterable[str] = (),
) -> dict[str, Any]:
    """打开角色会话时能看到的那一屏（§6.1 默认可见）。"""
    return {
        "product_state": str(scene.get("product_state") or ""),
        "label": str(scene.get("label") or ""),
        "can": list(scene.get("can") or ()),
        "reason": str(scene.get("reason") or ""),
        "must_not_imply": str(scene.get("must_not_imply") or ""),
        "character": {"id": str(character[0]), "name": str(character[1])},
        "world": {"name": str(world_name)},
        "timeline": {"id": str(timeline[0]), "name": str(timeline[1])},
        "session": {
            "id": str(session.get("id") or ""),
            "channel": str(session.get("channel") or ""),
            "thread": str(session.get("thread") or ""),
        },
        "time": dict(time_info),
        "messages": [dict(item) for item in messages],
        "notes": [str(item) for item in notes],
    }


def enter_step(
    key: str, label: str, *, client_action: str, done: bool, hint: str = ""
) -> dict[str, Any]:
    """首次进入的一步（§4.1）：产品语言的标签 + 客户端该做什么，不暴露内部术语。"""
    return {
        "key": str(key),
        "label": str(label),
        "client_action": str(client_action),
        "done": bool(done),
        "hint": str(hint),
    }
