"""对外变化契约的纯逻辑层（WORLD_RUNTIME_INTERFACE_SPEC §5.1~§5.3）。

高级模块提交的是结构化 `change_intent`，不是自由文本事实。本模块只做三件事，
全部是确定性函数：不碰数据库、不调模型、不落盘。

- 校验 intent 的形状与闭集；
- 把 intent 翻成既有的 draft 载荷（`effects` / `claims`），**翻不出来的如实拒绝**
  ——首版事实效果闭集（`world.validate.SUPPORTED_EFFECTS`）比 intent 的 kind 窄，
  这层映射就是「哪些说法能变成世界事实」的判据；
- 算预览标识（同一批 intent + 同一基准版本 → 同一个 id，基准变了自动失效）。

落盘仍只有一条原子边界：`store.apply_runtime_batch`（由 `RuntimeService.change_commit` 调用）。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

#: change_intent.kind 闭集（§5.1）
KINDS = (
    "world_event",
    "state_change",
    "resource_change",
    "relation_change",
    "knowledge_change",
    "condition",
    "location_change",
    "time_advance",
    "clock_progress",
)
OPERATIONS = ("create", "set", "change", "add", "remove", "reveal", "advance")
CERTAINTIES = ("confirmed", "candidate", "uncertain")
SOURCE_MODES = ("oc_management", "trpg_rule", "gm_declaration", "world_process")

#: intent.kind → 事实效果闭集里的 kind（§六 效果表）。翻不出来的写在 REFUSED 里。
EFFECT_BY_KIND: dict[str, str] = {
    "condition": "activity_constraint",
    "location_change": "route_blocked",
}
#: 按目标类别落地的 kind：`state_change` 要看目标是职位 / 惯例 / 环境类型哪一类
STATE_KIND_BY_TARGET = {
    "office": "institution_state",
    "custom": "custom_state",
    "environment": "environment_state",
}
#: 首版明确不支持、且**不能**假装支持的 kind：拒绝并给出替代路径（§八 rejected）
REFUSED: dict[str, str] = {
    "resource_change": "首版事实效果闭集里没有资源量：用 state_change / condition 表达，或扩闭集后再接入",
    "relation_change": "关系变化没有对应的事实效果：用 knowledge_change（说法）或扩闭集后再接入",
    "time_advance": "时间推进不走事实提交：用 runtime.time.advance",
    "clock_progress": "时钟推进由倍率与世界推进负责，高级模块不能直接推",
}


def classify_target(package: dict[str, Any], target: str) -> str | None:
    """目标属于哪一类（职位 / 惯例 / 环境类型），判不出返回 None。"""
    world = package.get("world") if isinstance(package.get("world"), dict) else {}
    for entry in world.get("institutions") or []:
        if isinstance(entry, dict):
            for office in entry.get("offices") or []:
                if isinstance(office, dict) and str(office.get("id") or "") == target:
                    return "office"
    for entry in world.get("customs") or []:
        if isinstance(entry, dict) and str(entry.get("id") or "") == target:
            return "custom"
    environment = package.get("environment") if isinstance(package.get("environment"), dict) else {}
    for entry in environment.get("types") or []:
        if isinstance(entry, dict) and str(entry.get("id") or "") == target:
            return "environment"
    return None


def validate_intents(changes: Any) -> list[str]:
    """形状与闭集校验：错误是「这批发不出去」的原因，不吞不猜。"""
    errors: list[str] = []
    if not isinstance(changes, list) or not changes:
        return ["changes 必须是非空数组"]
    for index, item in enumerate(changes):
        where = f"changes[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} 必须是对象")
            continue
        kind = str(item.get("kind") or "")
        if kind not in KINDS:
            errors.append(f"{where}.kind 不在闭集内：{kind or '（空）'}")
        operation = str(item.get("operation") or "")
        if operation not in OPERATIONS:
            errors.append(f"{where}.operation 不在闭集内：{operation or '（空）'}")
        certainty = str(item.get("certainty") or "confirmed")
        if certainty not in CERTAINTIES:
            errors.append(f"{where}.certainty 不在闭集内：{certainty}")
        source_mode = str(item.get("source_mode") or "")
        if source_mode and source_mode not in SOURCE_MODES:
            errors.append(f"{where}.source_mode 不在闭集内：{source_mode}")
        if kind in ("state_change", "condition", "location_change") and not str(item.get("target_refs") or []):
            errors.append(f"{where} 缺少 target_refs")
        if not str(item.get("id") or ""):
            errors.append(f"{where} 缺少 id（用于幂等与审计）")
    return errors


def preview_id(instance_id: str, timeline_id: str, base_revision: int, changes: list[dict[str, Any]]) -> str:
    """预览标识：与基准版本绑死——版本一变，同一个 id 不再匹配，提交时按冲突处理。"""
    blob = json.dumps(
        {"i": instance_id, "t": timeline_id, "r": int(base_revision), "c": changes},
        ensure_ascii=False, sort_keys=True,
    )
    return "pv-" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


def translate(
    changes: list[dict[str, Any]], *, package: dict[str, Any], world_seconds: int
) -> dict[str, Any]:
    """把一批 intent 翻成 draft 载荷（`intent` / `effects` / `claims`）并分类。

    返回 `accepted`（能翻出来的 intent id）/ `rejected`（含替代路径）/ `needs_review`
    （候选与未确认：预览可以看，提交必须挡）。
    """
    effects: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    frames: list[str] = []
    accepted: list[str] = []
    rejected: list[dict[str, str]] = []
    pending: list[dict[str, str]] = []

    for item in changes:
        ident = str(item.get("id") or "")
        kind = str(item.get("kind") or "")
        certainty = str(item.get("certainty") or "confirmed")
        if certainty != "confirmed":
            # §5.1：候选与未确认不能直接改变世界，只能进预览或待确认状态
            pending.append({"id": ident, "reason": f"certainty={certainty}：需要确认后才能提交"})
            continue
        if kind in REFUSED:
            rejected.append({"id": ident, "reason": REFUSED[kind]})
            continue
        if kind == "world_event":
            # 事件帧：供事件行叙述用，本身不产生效果
            frames.append(str(item.get("value") or item.get("intent") or "").strip())
            accepted.append(ident)
            continue
        if kind == "knowledge_change":
            text = str(item.get("value") or "").strip()
            if not text:
                rejected.append({"id": ident, "reason": "knowledge_change 的 value 是说法原文，不能为空"})
                continue
            claims.append({
                "text": text,
                "source_id": str((item.get("cause_refs") or [""])[0] or ""),
                "audience": str(item.get("visibility") or "public"),
            })
            accepted.append(ident)
            continue
        target = str((item.get("target_refs") or [""])[0] or "")
        effect_kind = EFFECT_BY_KIND.get(kind)
        if kind == "state_change":
            effect_kind = STATE_KIND_BY_TARGET.get(classify_target(package, target) or "")
            if effect_kind is None:
                rejected.append({"id": ident, "reason": f"state_change 的目标不在世界包里：{target or '（空）'}"})
                continue
        if effect_kind is None:
            rejected.append({"id": ident, "reason": f"{kind} 没有对应的事实效果"})
            continue
        effect: dict[str, Any] = {"kind": effect_kind, "target": target}
        value = item.get("value")
        if value is not None:
            effect["value"] = str(value)
        expiry = str(item.get("expiry") or "")
        if expiry:
            effect["expiry"] = expiry
        elif str(item.get("clear_when") or ""):
            effect["expiry"] = "until_cleared"
        effects.append(effect)
        accepted.append(ident)

    return {
        "intent": "；".join(frame for frame in frames if frame) or "高级模块提交的变化",
        "effects": effects,
        "claims": claims,
        "accepted": accepted,
        "rejected": rejected,
        "needs_review": pending,
        "when": "now",
        "at_world": int(world_seconds),
    }
