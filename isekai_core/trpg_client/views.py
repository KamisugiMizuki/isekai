"""TRPG 客户端的四个产品面与显示闸门（TRPG_CLIENT_SPEC §5 / §6 / §7.2 / §十二）。

产品面只做「取真实投影 → 按受众裁剪 → 起名」；文本表达在 expression.py。
客户端不复制世界真值、规则状态与角色认知（§18.11），也不新增旁路 API（§15）。
"""

from __future__ import annotations

import json
from typing import Any

from .states import campaign_line, next_step_copy, read_only, user_state

#: §十二 视角闭集（大小写不敏感；核心用小写）
VIEWERS = ("gm_only", "public_party")
#: 定向受众前缀
SCOPED_PREFIXES = ("character:", "player:", "npc:")

#: §12 末步：玩家面禁止出现的字段名（出现即判泄漏）
DENY_KEYS = {
    "resolution", "raw_resolution", "rolls", "roll", "dice", "dc", "secret_dc", "difficulty",
    "modifier", "modifiers", "rule_state_patch", "base_patch", "patch", "gm_only", "audit", "raw",
    "opaque_state", "hidden", "secret",
}

#: 效果类型 → 公开说法（EVENT_ENGINE_SPEC 的受支持闭集，避免客户端临场发明数值系统）
EFFECT_LABELS: dict[str, str] = {
    "source_delay": "渠道受阻",
    "route_blocked": "通行受阻",
    "activity_constraint": "活动受限",
    "public_notice": "公开通告",
    "rumor_spread": "风闻流传",
    "institution_state": "制度状态",
    "custom_state": "惯例现状",
    "environment_state": "环境状态",
}


def audience_valid(value: str) -> bool:
    """受众闭集校验（§十二）：两份固定项 + 三类带标识的。自由字符串不当作公开。"""
    text = str(value or "").strip().lower()
    if text in VIEWERS:
        return True
    prefix, _, rest = text.partition(":")
    return f"{prefix}:" in SCOPED_PREFIXES and bool(rest)


def audience_allows(material_audience: str, viewer: str) -> bool:
    """材料受众 → 观察者是否允许看到。未标记 = 最严（仅 GM），不按公开处理。"""
    material = str(material_audience or "").strip().lower() or "gm_only"
    who = str(viewer or "").strip().lower()
    if who == "gm_only":
        return True
    if material == "gm_only":
        return False
    if material == "public_party":
        return True
    return material == who


def _walk(node: Any, viewer: str, path: str, out: list[dict[str, str]]) -> None:
    if isinstance(node, dict):
        aud = node.get("audience") or node.get("visibility") or node.get("viewer")
        if isinstance(aud, str) and aud and not audience_allows(aud, viewer):
            out.append({"path": path, "why": f"材料受众 {aud} 不允许 {viewer}", "kind": "audience"})
        for key, value in node.items():
            if str(key).lower() in DENY_KEYS and str(viewer).lower() != "gm_only":
                out.append({"path": f"{path}.{key}" if path else str(key), "why": "玩家面不得出现该字段",
                            "kind": "field"})
            _walk(value, viewer, f"{path}.{key}" if path else str(key), out)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _walk(value, viewer, f"{path}[{index}]", out)


def display_gate(bundle: Any, viewer: str) -> list[dict[str, str]]:
    """§十二 的客户端最后一道闸门：返回违规清单（空 = 干净）。

    这是检查器，不是过滤器：裁剪由组装时的 `audience_allows` 完成，闸门用来证明没漏。
    """
    out: list[dict[str, str]] = []
    _walk(bundle, viewer, "", out)
    return out


def campaign_item(
    campaign: dict[str, Any],
    *,
    instance: dict[str, Any] | None = None,
    timeline: dict[str, Any] | None = None,
    plugin: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    choices: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """§5.1 战役选择项 / 状态条。ID 与内部版本号折叠显示，规则版本不藏。"""
    status = str(campaign.get("status") or "")
    plugin = plugin if isinstance(plugin, dict) else {}
    return {
        "display_name": str(campaign.get("display_name") or campaign.get("campaign_id") or ""),
        "campaign_id": str(campaign.get("campaign_id") or ""),
        "ruleset": str(campaign.get("ruleset_id") or plugin.get("id") or ""),
        "ruleset_version": str(campaign.get("ruleset_version") or plugin.get("version") or ""),
        "host_mode": str(campaign.get("host_mode") or ""),
        "status": status,
        "status_line": campaign_line(status),
        "instance": str((instance or {}).get("display_name") or campaign.get("instance_id") or ""),
        "instance_id": str(campaign.get("instance_id") or ""),
        "timeline": str((timeline or {}).get("name") or campaign.get("timeline_id") or ""),
        "timeline_id": str(campaign.get("timeline_id") or ""),
        "state_revision": int(campaign.get("state_revision") or 0),
        "current_scene_id": str(campaign.get("current_scene_id") or ""),
        "needs_attention": next_step_copy(campaign_status=status, actions=actions or [], choices=choices or []),
        "read_only": read_only(status),
    }


def scene_face(
    view: dict[str, Any],
    *,
    audience: str,
    campaign: dict[str, Any] | None = None,
    cognition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§5.2 场景面：六类材料分开摆，缺材料就明说缺，不拿日志凑。"""
    campaign = campaign if isinstance(campaign, dict) else {}
    scene = view.get("scene") if isinstance(view.get("scene"), dict) else {}
    status = str(campaign.get("status") or "")
    choices = [item for item in (view.get("pending_choices") or []) if isinstance(item, dict)]
    actions = [item for item in (view.get("actions") or []) if isinstance(item, dict)]
    recent = [item for item in (view.get("recent") or []) if isinstance(item, dict)]
    cognition = cognition if isinstance(cognition, dict) else {}
    unknowns = [
        {"kind": "known_unknown", "text": str(item.get("text") or ""), "stage": str(item.get("stage") or "")}
        for item in (cognition.get("known_unknowns") or [])
        if isinstance(item, dict) and str(item.get("text") or "")
    ]
    for item in actions:
        state = user_state(str(item.get("status") or ""))
        if not state.get("known"):
            unknowns.append({"kind": "unknown_state", "text": state["label"], "stage": "action"})
    if not scene and not read_only(status):
        unknowns.append({"kind": "empty_scene", "text": "当前还没有已确认的现场信息", "stage": "scene"})
    copy = next_step_copy(campaign_status=status, actions=actions, choices=choices)
    return {
        "audience": audience,
        "read_only": read_only(status),
        "scene": {
            "scene_id": str(scene.get("scene_id") or ""),
            "title": str(scene.get("title") or ""),
            "description": str(scene.get("description") or ""),
            "advance_mode": str(scene.get("advance_mode") or ""),
            "scene_revision": int(scene.get("revision") or scene.get("scene_revision") or 0),
            "location_refs": list(scene.get("location_refs") or []),
            "participants": list(scene.get("participants") or []),
        },
        "public_facts": [item for item in (scene.get("public_facts") or []) if isinstance(item, dict)],
        "unknowns": unknowns,
        "risks": [item for item in (scene.get("active_risks") or []) if isinstance(item, dict)],
        "available_actions": list(scene.get("available_actions") or []) if not read_only(status) else [],
        "action_results": [{"action_id": str(item.get("action_id") or ""), "status": str(item.get("status") or ""),
                            "state": user_state(str(item.get("status") or ""))} for item in recent],
        "unfinished_actions": [{"action_id": str(item.get("action_id") or ""),
                                "action_revision": int(item.get("action_revision") or 1),
                                "status": str(item.get("status") or ""),
                                "state": user_state(str(item.get("status") or ""))} for item in actions],
        "next_choice": choices[0] if choices else None,
        "choice_count": len(choices),
        "next": copy,
        "empty": not scene,
    }


def party_face(
    view: dict[str, Any],
    *,
    audience: str,
    active_character_id: str = "",
    cognition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§6 队伍面：当前查看谁就显示谁的私密材料，不把同一用户的多个角色合并。"""
    scene = view.get("scene") if isinstance(view.get("scene"), dict) else {}
    private = scene.get("private_views") if isinstance(scene.get("private_views"), dict) else {}
    cognition = cognition if isinstance(cognition, dict) else {}
    own: list[dict[str, Any]] = []
    others: list[str] = []
    for key, value in private.items():
        if str(key) == str(audience) or (active_character_id and str(key) == f"character:{active_character_id}"):
            own = list(value or []) if isinstance(value, list) else []
        else:
            others.append(str(key))
    known = [{"text": str(item.get("text") or ""), "when": str(item.get("when") or ""),
              "kind": str(item.get("kind") or "")}
             for item in (cognition.get("observations") or []) if isinstance(item, dict)]
    claims = [{"text": str(item.get("text") or ""), "stance": str(item.get("stance") or ""),
               "source": str(item.get("source") or "")}
              for item in (cognition.get("claims") or []) if isinstance(item, dict)]
    return {
        "audience": audience,
        "active_character_id": str(active_character_id or ""),
        "public_party": list(scene.get("public_facts") or []),
        "private_views": own,
        "known": known,
        "claims": claims,
        "switching_note": ("同一用户控制多个角色时只切换 actor / audience，私密认知不合并（C4）"
                           if others else ""),
        "other_audiences": others,
    }


def gm_face(
    view: dict[str, Any],
    *,
    campaign: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    plugin: dict[str, Any] | None = None,
    preview: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§7.2 主持面：结构、闸门、工作区与入口。原始材料在 expression 的 GM 层给。"""
    view = view if isinstance(view, dict) else {}
    campaign = campaign if isinstance(campaign, dict) else (view.get("campaign") or {})
    scene = view.get("scene") if isinstance(view.get("scene"), dict) else {}
    status = str(campaign.get("status") or "")
    rule = view.get("rule_state") if isinstance(view.get("rule_state"), dict) else {}
    plugin = plugin if isinstance(plugin, dict) else {}
    actions = [item for item in (view.get("actions") or []) if isinstance(item, dict)]
    choices = [item for item in (view.get("pending_choices") or []) if isinstance(item, dict)]
    review_queue = [
        {"kind": "action", "action_id": str(item.get("action_id") or ""), "status": str(item.get("status") or ""),
         "reason": str(item.get("failure_code") or ""),
         "state": user_state(str(item.get("status") or ""))}
        for item in actions
        if str(item.get("status") or "") in ("awaiting_gm_review", "reviewing", "conflict", "stale",
                                             "plugin_failed", "interrupted", "rejected")
    ]
    return {
        "campaign": campaign_item(campaign, actions=actions, choices=choices),
        "scene_structure": {
            "scene_id": str(scene.get("scene_id") or ""),
            "title": str(scene.get("title") or ""),
            "advance_mode": str(scene.get("advance_mode") or ""),
            "participants": list(scene.get("participants") or []),
            "risks": list(scene.get("active_risks") or []),
            "scene_revision": int(scene.get("revision") or scene.get("scene_revision") or 0),
        },
        "rule_state": {"ruleset": str(campaign.get("ruleset_id") or campaign.get("ruleset") or ""),
                       "ruleset_version": str(campaign.get("ruleset_version") or ""),
                       "base_revision": int(rule.get("base_revision") or 0),
                       "state_revision": int(rule.get("revision") or 0)},
        "plugin": {"id": str((plugin or {}).get("id") or ""), "version": str((plugin or {}).get("version") or ""),
                   "modes": list((plugin or {}).get("modes") or [])},
        "unfinished_actions": [{"action_id": str(item.get("action_id") or ""),
                                "action_revision": int(item.get("action_revision") or 1),
                                "status": str(item.get("status") or ""),
                                "state": user_state(str(item.get("status") or ""))} for item in actions],
        "review_queue": review_queue,
        "open_choices": choices,
        "direct_change_form": {  # §十三：入口字段固定，客户端不自造骰点
            "fields": ["target_ref", "kind", "op", "audience", "reason", "idempotency_key"],
            "sources": ["gm_declaration"],
            "note": "永远由 GM 明确提交；故事意图只能形成主持候选",
        },
        "version_block": version_block(campaign, plugin),
        "player_preview": preview,
        "read_only": read_only(status),
    }


def version_block(campaign: dict[str, Any], plugin: dict[str, Any] | None = None,
                  state_version: str = "") -> dict[str, Any]:
    """§14.4 规则版本阻断：可用 / 不可用操作分明，人工接受是主持人承担的决定。

    现有证据判定：规则状态行记录的版本（写这份状态时插件声明的版本，人工接受会改写它）
    与插件当前声明的版本不一致 = 阻断。
    """
    campaign = campaign if isinstance(campaign, dict) else {}
    plugin = plugin if isinstance(plugin, dict) else {}
    declared = str(plugin.get("version") or "")
    recorded = str(state_version or campaign.get("ruleset_version") or "")
    flag = campaign.get("version_block") if isinstance(campaign.get("version_block"), dict) else {}
    blocked = bool(flag.get("blocked")) or bool(plugin) and bool(declared) and bool(recorded) and declared != recorded
    if not blocked:
        return {"blocked": False}
    reason = str(flag.get("reason") or "")
    if not reason:
        reason = f"规则状态写在版本 {recorded}，当前插件声明 {declared}：不能安全解释这份状态"
    return {
        "blocked": True,
        "reason": reason,
        "allowed": ["查看原因", "导出", "选择转换器", "人工接受新版本", "归档"],
        "forbidden": ["继续裁定", "提交行动", "假设状态已迁移"],
        "note": "人工接受新版本不是自动转换",
    }


def as_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)
