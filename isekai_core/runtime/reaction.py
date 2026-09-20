"""短期反应状态域（WORLD_RUNTIME_SPEC §11.1）。

设计要点（照 §11.1 原文）：
- 不设通用情绪数值：一条反应就是**有来源的素材**——触发来源、角色当时的观察 / 获知、影响方向、
  强度区间、开始水位、预计失效条件、当前表达倾向；没有来源的标签不进入状态。
- 生命周期「候选 → 采纳 → 活跃 → 减弱 / 暂停 → 失效 / 转长期」；暂停只表示暂时确认不了，不等于撤销；
  失效后仍可在历史或记忆里被合法回忆。
- 同一来源只建一条记录（id 由来源派生）：重复回忆不叠加事实、不把形容词当新证据；
  相反依据可以降低、覆盖或终止反应，但不抹除已发生的经历。
- 只约束表达与生活线选择，不改世界事实、角色卡、公理或他人状态。

阶段推进写成**纯函数**（`advance(rows, watermark=..., cleared=...)`）：分批补算与连续推进结果一致。
"""

from __future__ import annotations

from typing import Any, Iterable

from .events import stable_key

STAGES = ("candidate", "adopted", "active", "fading", "paused", "expired", "long_term")
#: 还能进入表达的语气阶段（失效的不再引用，但历史与记忆照旧）
LIVE_STAGES = ("adopted", "active", "fading", "paused", "long_term")
INTENSITIES = ("low", "mid", "high")

#: 后果类效果 → 强度区间的固定档（区间表述，不是全局情绪分数）
EFFECT_INTENSITY = {
    "route_blocked": "high",
    "activity_constraint": "high",
    "environment_state": "mid",
    "institution_state": "mid",
    "custom_state": "low",
    "source_delay": "low",
    "public_notice": "low",
    "rumor_spread": "low",
}
#: 经历类来源的强度：身历其境重，旁观 / 记事轻
EXPERIENCE_INTENSITY = {"life": "mid", "event": "mid", "claim": "low", "dialog": "mid", "recall": "low"}

_TENDENCY_BY_KIND = {
    "route_blocked": "出门这件事变得不顺手，做什么都先掂量路上的麻烦",
    "activity_constraint": "原定的安排被压住，心里惦记着没做成的那些事",
    "environment_state": "对天色与冷暖格外在意",
    "institution_state": "对当权者与规矩多留了一份心眼",
    "custom_state": "守着这地方一贯的做法过日子",
    "source_delay": "消息来得慢，说话时带着等回音的迟疑",
    "public_notice": "按通告上的说法安排自己的事",
    "rumor_spread": "听到的风声还悬着，说出口时留了余地",
}


def reaction_id(source_kind: str, source_ref: str) -> str:
    """同一来源只建一条：id 由（来源类型, 来源标识）派生。"""
    return f"rx-{stable_key(source_kind, source_ref)[:12]}"


def _row(
    source_kind: str,
    source_ref: str,
    *,
    instance_id: str,
    timeline_id: str,
    character_id: str,
    direction: int,
    intensity: str,
    tendency: str,
    basis: str,
    started_world: int,
    expiry_condition: str,
) -> dict[str, Any]:
    if intensity not in INTENSITIES:
        intensity = "mid"
    return {
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "character_id": character_id,
        "id": reaction_id(source_kind, source_ref),
        "source_kind": source_kind,
        "source_ref": str(source_ref),
        "direction": 1 if int(direction) >= 0 else -1,
        "intensity": intensity,
        "stage": "candidate",
        "tendency": str(tendency or ""),
        "basis": str(basis or ""),
        "started_world": int(started_world),
        "expiry_condition": str(expiry_condition or ""),
        "updated_world": int(started_world),
    }


def from_effect(
    effect: dict[str, Any],
    *,
    character_id: str,
    carried: bool = True,
) -> dict[str, Any]:
    """事件后果 → 短期反应：有后果就有来源，强度按后果类型的固定档。"""
    kind = str(effect.get("kind") or "")
    return _row(
        "event_effect",
        str(effect.get("id") or ""),
        instance_id=str(effect.get("instance_id") or ""),
        timeline_id=str(effect.get("timeline_id") or ""),
        character_id=character_id,
        direction=1 if carried else -1,
        intensity=EFFECT_INTENSITY.get(kind, "mid"),
        tendency=_TENDENCY_BY_KIND.get(kind, ""),
        basis=f"后果类型：{kind}",
        started_world=int(effect.get("from_world") or 0),
        expiry_condition=f"解除方式：{effect.get('expiry') or 'until_cleared'}",
    )


def from_experience(experience: dict[str, Any], *, character_id: str) -> dict[str, Any]:
    """角色实际经历 → 短期反应（生活线 / 事件的亲历素材）。

    经历本身不带正负：方向默认「承接」（+1），相反依据由后续素材或解除路径处理；
    `confidence` 在库里是采信字样（如 experienced），不当数值用。
    """
    kind = str(experience.get("kind") or "event")
    confidence = experience.get("confidence")
    positive = not (isinstance(confidence, (int, float)) and float(confidence) < 0)
    return _row(
        "experience",
        str(experience.get("id") or ""),
        instance_id=str(experience.get("instance_id") or ""),
        timeline_id=str(experience.get("timeline_id") or ""),
        character_id=character_id,
        direction=1 if positive else -1,
        intensity=EXPERIENCE_INTENSITY.get(kind, "mid"),
        tendency=str(experience.get("summary") or ""),
        basis=f"亲身经历：{kind}",
        started_world=int(experience.get("world_seconds") or 0),
        expiry_condition="被后续经历覆盖，或该情境过去",
    )


def from_dialog(
    *,
    instance_id: str,
    timeline_id: str,
    character_id: str,
    message_id: str,
    world_seconds: int,
    direction: int = 1,
    tendency: str = "",
    basis: str = "",
) -> dict[str, Any]:
    """对话决定 / 记忆回想 → 短期反应（会话层与记忆层用它落有来源的素材）。"""
    return _row(
        "dialog",
        str(message_id),
        instance_id=instance_id,
        timeline_id=timeline_id,
        character_id=character_id,
        direction=direction,
        intensity="mid",
        tendency=tendency,
        basis=basis or "对话里定下的事",
        started_world=int(world_seconds),
        expiry_condition="被后续对话改变，或轮次过去",
    )


def advance(row: dict[str, Any], *, watermark: int, cleared: Iterable[str] = ()) -> dict[str, Any]:
    """阶段推进（纯函数）：候选 → 采纳 → 活跃 → 减弱 → 失效。

    - 到水位的候选转采纳，采纳转活跃；
    - 依据的后果被解除 → 减弱；已经减弱又仍无依据 → 失效（两步收尾，确定性）；
    - 暂停 / 转长期由显式依据决定（`pause` / `promote` 在调用方写阶段），这里不猜；
    - 分批补算与一次推进结果一致：只看 (当前阶段, 水位, 解除集合)。
    """
    out = dict(row)
    stage = str(out.get("stage") or "candidate")
    live = str(out.get("source_ref") or "") in set(str(item) for item in cleared or ())
    now = int(watermark)
    if stage == "candidate" and int(out.get("started_world") or 0) <= now:
        out["stage"] = "adopted"
    elif stage == "adopted":
        out["stage"] = "active"
    if live and str(out.get("stage")) in ("adopted", "active"):
        out["stage"] = "fading"
    elif live and str(out.get("stage")) == "fading":
        out["stage"] = "expired"
    return out


def _lower(intensity: str) -> str:
    index = INTENSITIES.index(intensity) if intensity in INTENSITIES else 1
    return INTENSITIES[max(0, index - 1)]


def merge(current: dict[str, Any] | None, incoming: dict[str, Any]) -> dict[str, Any]:
    """同一来源只建一条：重复素材不叠加；相反依据可减弱 / 终止，但不抹除经历。"""
    if current is None:
        return incoming
    merged = dict(current)
    merged["updated_world"] = max(int(current.get("updated_world") or 0), int(incoming.get("updated_world") or 0))
    if incoming.get("basis"):
        merged["basis"] = str(incoming["basis"])
    if int(incoming.get("direction") or 0) == int(current.get("direction") or 0):
        # 重复回忆同一来源：不加事实、不叠强度（§11.1 末条）
        return merged
    merged["direction"] = int(incoming["direction"])
    merged["intensity"] = _lower(str(current.get("intensity") or "mid"))
    merged["stage"] = "expired" if merged["intensity"] == "low" and str(current.get("intensity")) == "low" else "fading"
    if incoming.get("tendency"):
        merged["tendency"] = str(incoming["tendency"])
    return merged


def tendency_block(rows: Iterable[dict[str, Any]], *, watermark: int) -> str:
    """给表达用的当前处境：只取还有效的反应，按（阶段, id）稳定排序，最多三条。"""
    alive = [
        row
        for row in rows
        if str(row.get("stage")) in LIVE_STAGES
        and int(row.get("started_world") or 0) <= int(watermark)
        and str(row.get("tendency") or "").strip()
    ]
    alive.sort(key=lambda row: (STAGES.index(str(row.get("stage") or "candidate")), str(row.get("id") or "")))
    lines = [str(row["tendency"]).strip() for row in alive[:3]]
    return "\n".join(f"- {line}" for line in lines)
