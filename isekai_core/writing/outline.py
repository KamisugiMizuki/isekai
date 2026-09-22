"""大纲模型（WRITING_ASSISTANT_SPEC §三 / §4.2 / §七）：分层约束、条目状态机、偏离判定。

纯逻辑：不碰数据库、不调模型、不写世界。三条硬规矩：

- **大纲是约束与目标，不是已经发生的事实**（§十-1）：本模块产出的任何东西都不能当世界真值；
- **条目状态只由可追溯的世界事实、角色认知、规则结果或创作者决定推动**（§4.2）：
  自然语言草稿、模型判断和「看起来像发生了」都不能单独推动**硬约束**达成；
- **禁止事项永远不会「达成」**：它触发就是偏离，不是进度。
"""

from __future__ import annotations

import json
from typing import Any, Iterable

#: 约束层级（§三 表格左列）：默认强度跟着层级走，可被创作者改写（§5.3「调整软约束」）
LAYERS: tuple[str, ...] = ("theme", "required_node", "forbidden", "character_arc", "pacing", "variable_material")
LAYER_LABELS: dict[str, str] = {
    "theme": "主题约束",
    "required_node": "必达节点",
    "forbidden": "禁止事项",
    "character_arc": "角色弧线",
    "pacing": "节奏目标",
    "variable_material": "可变素材",
}
STRENGTHS: tuple[str, ...] = ("soft", "medium", "hard")
DEFAULT_STRENGTH: dict[str, str] = {
    "theme": "soft",
    "required_node": "hard",
    "forbidden": "hard",
    "character_arc": "medium",
    "pacing": "medium",
    "variable_material": "soft",
}
SCOPES: tuple[str, ...] = ("world", "timeline", "chapter", "scene", "character")
#: 条目状态（§4.2）：deviated 不等于 achieved，只有明确选择替代路径 / 改写 / 接受偏离才变
STATUSES: tuple[str, ...] = ("unstarted", "in_progress", "achieved", "deviated", "abandoned")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    # 允许从 unstarted 直接落到 achieved / deviated：作者事后认账不该逼他先补一次 in_progress；
    # 禁止的是回退（achieved → unstarted）与复活（abandoned → *）这类会掩盖历史的方向。
    "unstarted": ("in_progress", "achieved", "deviated", "abandoned"),
    "in_progress": ("achieved", "deviated", "abandoned"),
    # 接受偏离之后改写路径可以继续推进；达成过的条目被回滚抹掉依据时也只能走 deviated，
    # 不能靠「改回未开始」把历史抹平。
    "deviated": ("in_progress", "achieved", "abandoned"),
    "achieved": ("deviated",),
    "abandoned": (),
}
#: 需要外部依据才允许标记达成的强度（硬约束不能靠一句「我觉得到了」）
EVIDENCE_REQUIRED: tuple[str, ...] = ("hard",)


def normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """补默认值并收敛字段：调用方只给关心的字段，存储与判定用同一份形状。"""
    layer = str(item.get("layer") or "")
    strength = str(item.get("strength") or DEFAULT_STRENGTH.get(layer, "medium"))
    return {
        "id": str(item.get("id") or ""),
        "layer": layer,
        "strength": strength,
        "title": str(item.get("title") or ""),
        "statement": str(item.get("statement") or ""),
        "scope": str(item.get("scope") or "timeline"),
        "scope_refs": [str(name) for name in item.get("scope_refs") or []],
        "preconditions": [str(name) for name in item.get("preconditions") or []],
        "success_criteria": str(item.get("success_criteria") or ""),
        "alternatives": [str(name) for name in item.get("alternatives") or []],
        "depends_on": [str(name) for name in item.get("depends_on") or []],
        #: 判定用的世界引用（事件 / 说法标识）：只读比对，不给写权限
        "watch_refs": [str(name) for name in item.get("watch_refs") or []],
        "deadline_world": int(item.get("deadline_world") or 0),
        "status": str(item.get("status") or "unstarted"),
        "reason": str(item.get("reason") or ""),
        "evidence_refs": [str(name) for name in item.get("evidence_refs") or []],
        "evaluated_world": int(item.get("evaluated_world") or 0),
    }


def normalize_outline(outline: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(outline.get("id") or ""),
        "name": str(outline.get("name") or ""),
        "note": str(outline.get("note") or ""),
        "items": [normalize_item(item) for item in outline.get("items") or [] if isinstance(item, dict)],
    }


def validate_outline(outline: dict[str, Any]) -> list[str]:
    """校验（§三 条目必填项 + §4.2）：空清单=通过。候选未通过校验不得落盘。"""
    errors: list[str] = []
    if not str(outline.get("id") or "").strip():
        errors.append("大纲缺少稳定标识 id")
    items = [normalize_item(item) for item in outline.get("items") or [] if isinstance(item, dict)]
    if not items:
        errors.append("大纲至少要有 0 条以上条目——空大纲不算大纲")
        return errors
    seen: set[str] = set()
    for item in items:
        where = f"条目 {item['id'] or '（缺 id）'}"
        if not item["id"]:
            errors.append(f"{where} 缺少稳定标识")
        elif item["id"] in seen:
            errors.append(f"{where} 标识重复")
        seen.add(item["id"])
        if item["layer"] not in LAYERS:
            errors.append(f"{where} 层级不在闭集：{item['layer'] or '（空）'}")
        if item["strength"] not in STRENGTHS:
            errors.append(f"{where} 强度不在闭集：{item['strength']}")
        if item["scope"] not in SCOPES:
            errors.append(f"{where} 适用范围不在闭集：{item['scope']}")
        if not item["statement"].strip():
            errors.append(f"{where} 缺少条目内容")
        if not item["success_criteria"].strip():
            errors.append(f"{where} 缺少成功判据（过度指定的反面：判据写功能，不写某个具体场景）")
        if item["status"] not in STATUSES:
            errors.append(f"{where} 状态不在闭集：{item['status']}")
        if item["layer"] == "forbidden" and item["status"] == "achieved":
            errors.append(f"{where} 禁止事项不存在「已达成」：触发就是偏离")
    known = {item["id"] for item in items}
    for item in items:
        missing = [name for name in item["depends_on"] if name not in known]
        if missing:
            errors.append(f"条目 {item['id']} 依赖了不在大纲里的条目：{'、'.join(missing)}")
    return errors


def transition(current: str, target: str) -> str:
    """条目状态迁移（§4.2）：非法迁移直接抛错并列出合法去向，不做静默降级。"""
    allowed = TRANSITIONS.get(str(current), ())
    if str(target) not in allowed:
        raise ValueError(
            f"条目状态不能从 {current} 变为 {target}；合法去向：{'、'.join(allowed) or '（终态）'}"
        )
    return str(target)


def evidence_ok(item: dict[str, Any], resolved: Iterable[str]) -> bool:
    """达成是否带得住：硬约束必须有在世界里对得上的依据（规则成功 ≠ 世界已改）。"""
    if item["strength"] not in EVIDENCE_REQUIRED:
        return True
    have = {str(name) for name in resolved}
    return bool([ref for ref in item["evidence_refs"] if ref in have] or [ref for ref in item["watch_refs"] if ref in have])


def evaluate(items: Iterable[dict[str, Any]], *, refs: set[str], world_time: int) -> dict[str, Any]:
    """只读评估（§七 三类偏离里的「大纲偏离」+ §十二 第 3 / 7 / 9 行）。

    - `evidence`：世界里已经能对上的引用（给创作者推进用的依据，**不自动改状态**）；
    - `gaps`：未达成的硬约束——必达节点到点未见、硬约束长期没动；
    - `deviations`：禁止事项被触发，或已有依据与达成标记不一致（回滚后尤其明显）。

    本函数不做写入，也不调用模型：「看起来像发生了」不能单独推动达成（§4.2）。
    """
    evidence: list[dict[str, Any]] = []
    gaps: list[dict[str, Any]] = []
    deviations: list[dict[str, Any]] = []
    for item in items:
        hit = sorted(set(item["watch_refs"]) & refs)
        if hit:
            evidence.append({"item_id": item["id"], "refs": hit, "layer": item["layer"]})
        if item["layer"] == "forbidden" and hit:
            deviations.append({
                "kind": "forbidden_triggered",
                "item_id": item["id"],
                "detail": f"禁止事项被触发：{'、'.join(hit)}",
                "need": "改大纲 / 改世界 / 接受偏离（三选一，不能自动牺牲一方）",
            })
        if item["strength"] == "hard" and item["layer"] != "forbidden" and item["status"] != "achieved":
            deadline = int(item["deadline_world"] or 0)
            if deadline and world_time >= deadline:
                gaps.append({
                    "kind": "required_missing",
                    "item_id": item["id"],
                    "detail": f"必达节点到点未见（期限 {deadline}，当前 {world_time}）",
                    "need": "报告缺口或提出候选；不自动制造世界事实",
                })
            elif not hit and item["status"] == "unstarted":
                gaps.append({
                    "kind": "not_started",
                    "item_id": item["id"],
                    "detail": "硬约束还没开始，世界材料里也没有对应依据",
                    "need": "补充创作条件或提出候选",
                })
        if item["status"] == "achieved":
            # 判据是「当初支撑达成的引用」：写进决定的依据引用优先，没写才退回 watch_refs。
            # 回滚删掉那段未来后，这些引用会从世界里消失——正是「按目标时间线重新评估」要报的偏离。
            recorded = [ref for ref in (item.get("evidence_refs") or []) if ref] or [
                ref for ref in item["watch_refs"] if ref
            ]
            lost = sorted({ref for ref in recorded if ref not in refs})
            if lost:
                deviations.append({
                    "kind": "evidence_lost",
                    "item_id": item["id"],
                    "detail": f"标记为已达成，但依据已不在当前时间线：{'、'.join(lost[:3])}"
                              "（回滚 / 分支后需重新评估）",
                    "need": "重新评估并给出决定：接受偏离、改写条目或用新依据重新达成",
                })
    return {"evidence": evidence, "gaps": gaps, "deviations": deviations}


def dump_items(items: Iterable[dict[str, Any]]) -> str:
    return json.dumps([normalize_item(item) for item in items], ensure_ascii=False)


def load_items(raw: Any) -> list[dict[str, Any]]:
    if isinstance(raw, (list, tuple)):
        return [normalize_item(item) for item in raw if isinstance(item, dict)]
    try:
        parsed = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return [normalize_item(item) for item in parsed if isinstance(item, dict)]
