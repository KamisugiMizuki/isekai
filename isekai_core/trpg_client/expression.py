"""TRPG 客户端的结果表达（TRPG_CLIENT_SPEC §8 / §9 / §10 / §11 / §十三）。

四层信息的边界（§8）：
  第一层 玩家面（简要）—— 只有公开材料；
  第二层 玩家可追问（简要）—— 行动理解 / 规则依据 / 结果等级 / 代价 / 提交状态；
  第三层 GM 审计 —— 引用、revision、发布范围；
  第四层 原始规则材料 —— 仅 GM。

关于「玩家默认看到结果等级」（§20.4）：现有插件协议里 `resolution` 没有公开标记（§8.3 末句要求
先消费插件现有受众标记），因此玩家面的等级只在投影给得出时显示，玩家面的事实来自**已提交的
公开世界后果与公开说法**；GM 面显示插件声明的 outcome / degree 与原始材料。想让玩家看到等级，
要么插件把摘要写成公开材料，要么由 Campaign Runtime 增加公开摘要字段（属运行时契约变更，
不在客户端内发明）。
"""

from __future__ import annotations

import json
from typing import Any

from .states import user_state
from .views import EFFECT_LABELS, audience_allows, display_gate

#: 行动状态的「结果」判定（§8.1 第一层）：等待选择 / 无法裁定 / 没有变化 / 已固化
RESULT_KINDS = {
    "choice": "等待选择",
    "unresolved": "无法裁定",
    "no_change": "没有变化",
    "committed": "已固化",
}


def _loads(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def level_of(resolution: Any) -> dict[str, str]:
    """插件声明的结果等级（§8.2 第二层）：只取等级字段，不含骰点与难度。"""
    data = _loads(resolution, {}) or {}
    if not isinstance(data, dict):
        return {}
    return {key: str(data.get(key)) for key in ("system", "outcome", "degree") if data.get(key)}


def raw_of(resolution: Any) -> dict[str, Any]:
    """第四层原始材料（原始骰点、秘密难度、隐藏修正）：只有 GM 层使用。"""
    data = _loads(resolution, {}) or {}
    return data if isinstance(data, dict) else {}


def effects_lines(items: list[dict[str, Any]]) -> list[str]:
    """已提交的世界效果 → 中性陈述。客户端不把效果名翻译成剧情文风。"""
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        target = str(item.get("target") or item.get("target_ref") or "")
        label = EFFECT_LABELS.get(kind, kind or "世界状态")
        value = item.get("value")
        detail = "" if value in (None, "", [], {}) else f"（{json.dumps(value, ensure_ascii=False)}）"
        out.append(f"{target or '现场'}：{label}{detail}")
    return out


def facts_from_payload(payload: dict[str, Any], *, viewer: str) -> list[dict[str, Any]]:
    """已提交的后果（§4.2 world_view）：世界效果按变化意图的受众逐条放行。

    事件的 `effects` 列不带受众，面向谁的账记在规范化后的 `changes` 上（§3.8）：这里按
    变化意图的 `visibility` / 目标把效果配回去，玩家面只拿得到允许受众的效果。
    """
    if not isinstance(payload, dict):
        return []
    effects = [item for item in (payload.get("effects") or []) if isinstance(item, dict)]
    facts: list[dict[str, Any]] = []
    for change in payload.get("changes") or []:
        if not isinstance(change, dict):
            continue
        visibility = change.get("visibility")
        allowed = [visibility] if isinstance(visibility, str) else list(visibility or [])
        if allowed and not any(audience_allows(item, viewer) for item in allowed):
            continue
        if not allowed and viewer != "gm_only":
            continue
        targets = {str(item) for item in list(change.get("target_refs") or []) + list(change.get("subject_refs") or [])}
        for effect in effects:
            target = str(effect.get("target") or "")
            if targets and target not in targets:
                continue
            kind = str(effect.get("kind") or "")
            facts.append({"kind": kind, "target": target, "value": effect.get("value"),
                          "visibility": ",".join(allowed) or "gm_only",
                          "label": EFFECT_LABELS.get(kind, kind),
                          "change_kind": str(change.get("kind") or ""),
                          "certainty": str(change.get("certainty") or "confirmed")})
    return facts


def public_claims(claims: list[dict[str, Any]], *, viewer: str) -> list[dict[str, Any]]:
    """公开说法（§3.9）：说法不等于事实，客户端分开表达。"""
    return [
        {"text": str(item.get("text") or ""), "audience": str(item.get("audience") or "")}
        for item in claims
        if isinstance(item, dict) and str(item.get("text") or "")
        and audience_allows(str(item.get("audience") or ""), viewer)
    ]


def _choice_count(commit: dict[str, Any]) -> int:
    value = commit.get("open_choices")
    if isinstance(value, list):
        return len(value)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def result_kind(*, action_status: str, commit_status: str, choice_count: int, effect_count: int) -> str:
    """第一层的判定链（顺序即优先级，全部取自真实状态，不猜插件内部）。"""
    if choice_count or str(action_status) == "awaiting_choice":
        return "choice"
    if str(commit_status) in ("needs_review", "rejected", "conflict", "stale", "plugin_failed", "interrupted"):
        return "unresolved"
    if str(action_status) in ("plugin_failed", "interrupted", "conflict", "stale", "rejected",
                              "awaiting_gm_review", "reviewing"):
        return "unresolved"
    if str(commit_status) in ("committed", "duplicate"):
        return "no_change" if effect_count == 0 else "committed"
    return "unresolved"


def player_result(
    *,
    action: dict[str, Any],
    viewer: str,
    commit: dict[str, Any] | None = None,
    facts: list[dict[str, Any]] | None = None,
    claims: list[dict[str, Any]] | None = None,
    scene: dict[str, Any] | None = None,
    next_copy: dict[str, Any] | None = None,
    ruleset_id: str = "",
    ruleset_version: str = "",
    transitioned: bool = False,
    extra_note: str = "",
) -> dict[str, Any]:
    """第一层 + 第二层：玩家面（§8.1 / §8.2）。只消费公开材料，GM 私有材料一律不进来。"""
    commit = commit if isinstance(commit, dict) else {}
    scene = scene if isinstance(scene, dict) else {}
    action = action if isinstance(action, dict) else {}
    facts = facts if isinstance(facts, list) else []
    claims = claims if isinstance(claims, list) else []
    commit_status = str(commit.get("status") or "")
    action_status = str(action.get("status") or "")
    effect_count = len(facts) if facts else int(commit.get("effects") or 0)
    kind = result_kind(action_status=action_status, commit_status=commit_status,
                       choice_count=_choice_count(commit), effect_count=effect_count)
    state = user_state(action_status)
    happened = effects_lines(facts) if facts else []
    if kind == "committed" and not happened:
        happened = [f"世界后果已固化（{effect_count} 项）；细节未公开"]
    if kind == "no_change":
        happened = ["这次没有改变世界（行动本身已记账）"]
    if transitioned and kind in ("committed", "no_change"):
        # §8.1 第一层：场景变化要单独说（世界后果与局面推进不是一回事）
        happened.append("局面已经推进（场景版本 "
                         f"{int((commit.get('scene_revision') or 0))}）")
    public = {
        "kind": kind,
        "kind_text": RESULT_KINDS[kind],
        "action_status": action_status,
        "user_state": state["label"],
        "happened": happened,
        "state": str(scene.get("title") or scene.get("description") or ""),
        "next": str((next_copy or {}).get("next") or ""),
        "note": extra_note,
    }
    reasons = {
        "action_understanding": {
            "actor": str(action.get("actor_id") or ""),
            "intent": str(action.get("intent") or ""),
            "target_refs": _loads(action.get("target_refs"), []),
            "method": str(action.get("method") or ""),
            "action_revision": int(action.get("action_revision") or 1),
        },
        "rule_basis": {"ruleset": str(ruleset_id), "ruleset_version": str(ruleset_version)},
        "level": {},  # 见模块 docstring：插件没标公开就没有公开等级
        "public_cost": [line for line in happened if "受阻" in line or "受限" in line or "债务" in line],
        "public_summary": [item["text"] for item in claims],
        "submit": commit_status or "not_attempted",
        "commit": {key: commit.get(key) for key in
                   ("joint_commit_id", "campaign_revision", "world_revision", "state_revisions",
                    "scene_id", "scene_revision", "open_choices", "effects", "claims") if key in commit},
    }
    return {"result": public, "reasons": reasons}


def gm_result(
    *,
    action: dict[str, Any],
    resolution: Any,
    commit: dict[str, Any] | None = None,
    facts: list[dict[str, Any]] | None = None,
    plugin: dict[str, Any] | None = None,
    snapshot_revision: int = 0,
    rule_state_base_revision: int = 0,
    scene_transition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """第三层 GM 审计（§8.3）：引用、revision 与发布范围，供主持人解释与复核。"""
    action = action if isinstance(action, dict) else {}
    commit = commit if isinstance(commit, dict) else {}
    plugin = plugin if isinstance(plugin, dict) else {}
    transition = scene_transition if isinstance(scene_transition, dict) else {}
    audit = {
        "action_id": str(action.get("action_id") or ""),
        "action_revision": int(action.get("action_revision") or 1),
        "actor_id": str(action.get("actor_id") or ""),
        "audience": str(action.get("audience") or ""),
        "ruleset": str(commit.get("ruleset_id") or plugin.get("id") or ""),
        "ruleset_version": str(commit.get("ruleset_version") or plugin.get("version") or ""),
        "plugin": {"id": str(plugin.get("id") or ""), "version": str(plugin.get("version") or "")},
        "snapshot_revision": int(snapshot_revision or 0),
        "rule_state_base_revision": int(rule_state_base_revision or 0),
        "rule_state_revision": int((commit.get("state_revisions") or {}).get("base_revision") or 0)
        if isinstance(commit.get("state_revisions"), dict) else 0,
        "resolution_ref": f"{action.get('action_id')}#{action.get('action_revision')}",
        "level": level_of(resolution),
        "effects": list(facts or []),
        "transition": transition,
        "joint_commit_id": str(commit.get("joint_commit_id") or ""),
        "failure_code": str(action.get("failure_code") or ""),
        "publish_scope": "gm_only" if not facts else "gm_only + 公开材料见玩家层",
    }
    return {"audit": audit, "raw_resolution": raw_of(resolution)}


def time_lines(*, payload: dict[str, Any] | None, commit: dict[str, Any] | None = None) -> dict[str, str]:
    """§11：规则时间与世界时间分开所有、分开显示（规则节拍 / 世界时间 / 事实结算）。"""
    payload = payload if isinstance(payload, dict) else {}
    commit = commit if isinstance(commit, dict) else {}
    transition_block = payload.get("scene_transition") if isinstance(payload.get("scene_transition"), dict) else {}
    request = payload.get("world_time_request") or transition_block.get("world_time_request") \
        or commit.get("world_time_request") or {}
    request = request if isinstance(request, dict) else {}
    transition = transition_block
    applied = bool(commit.get("world_time_applied")) if commit else False
    requested = int(request.get("seconds") or 0)
    if commit:
        advanced = requested if applied else 0
    else:
        advanced = 0
    rule_beat = "规则节拍：场景已转换" if transition else "规则节拍：本次裁定没有推进节拍"
    if advanced:
        world = f"世界时间：已前进 {advanced} 秒（原因：{request.get('reason') or '裁定要求'}）"
    elif commit:
        world = "世界时间：未移动"
    elif requested:
        world = f"世界时间：本次裁定请求前进 {requested} 秒（提交成功后才移动）"
    else:
        world = "世界时间：未移动"
    if advanced:
        settled = ("事实结算：时间前移后产生世界结果"
                   if int(commit.get("effects") or 0) else "事实结算：时间前移后没有产生世界结果")
    else:
        settled = ""
    return {"rule_beat": rule_beat, "world_time": world, "settled": settled}


def choice_card(choice: dict[str, Any], *, viewer: str) -> dict[str, Any]:
    """§10.1 待选择：只给合法选项，未选择分支不得进入玩家表达（§18.6）。"""
    choice = choice if isinstance(choice, dict) else {}
    row_audience = str(choice.get("audience") or "public_party")
    options = []
    for item in choice.get("choices") or []:
        if isinstance(item, dict):
            options.append({"option_id": str(item.get("id") or item.get("option_id") or ""),
                            "text": str(item.get("text") or item.get("label") or ""),
                            "visibility": str(item.get("visibility") or item.get("audience") or row_audience)})
        else:
            # 插件给的是纯文本选项（`["追上去", "先撤退"]`）：受众跟待选择行走
            options.append({"option_id": str(item), "text": str(item), "visibility": row_audience})
    visible = [item for item in options if audience_allows(item["visibility"], viewer)]
    return {
        "choice_id": str(choice.get("choice_id") or ""),
        "question": str(choice.get("prompt_ref") or choice.get("question") or ""),
        "options": visible,
        "hidden_options": len(options) - len(visible),
        "status": str(choice.get("status") or ""),
        "note": "未选择的任何分支都不是世界事实",
    }


def failure_lines(*, action: dict[str, Any], commit: dict[str, Any] | None = None,
                  facts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """§9.3 失败分支：代价 / 信息 / 待补充条件 / 待选择 / 明确无变化，五类分开说。"""
    action = action if isinstance(action, dict) else {}
    commit = commit if isinstance(commit, dict) else {}
    facts = facts if isinstance(facts, list) else []
    status = str(commit.get("status") or action.get("status") or "")
    code = str(action.get("failure_code") or "")
    compensations = [line for line in effects_lines(facts) if any(word in line for word in ("受阻", "受限", "债务"))]
    branches: dict[str, Any] = {
        "cost": compensations,
        "information": [],
        "needs_input": [],
        "needs_choice": [],
        "no_change": [],
    }
    if code in ("needs_input", "input_required") or status == "needs_input":
        branches["needs_input"] = [code or "需要补充条件"]
    if _choice_count(commit):
        branches["needs_choice"] = [f"待选择 {_choice_count(commit)} 项"]
    if status in ("needs_review", "rejected", "conflict", "stale", "plugin_failed", "interrupted"):
        branches["information"] = [f"真实状态：{status}"]
    if status in ("committed", "duplicate") and not facts:
        branches["no_change"] = ["规则与世界都没有变化（行动已记账）"]
    said = any(branches[key] for key in ("cost", "information", "needs_input", "needs_choice", "no_change"))
    if not said:
        branches["no_change"] = ["明确无变化：没有产生代价、信息、待补充条件或待选择"]
    return branches


def check_player_bundle(bundle: dict[str, Any], *, viewer: str) -> list[dict[str, str]]:
    """闸门便捷入口：玩家面组装完成后过一遍 §十二 的检查。"""
    return display_gate(bundle, viewer)
