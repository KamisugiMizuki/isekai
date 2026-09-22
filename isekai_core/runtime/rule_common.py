"""TRPG 规则共用模块（TRPG_RULE_COMMON_MODULE_SPEC）：规则私有结果 → 世界变化意图。

规则私有结果进入世界交界的**唯一**规范化边界（§一）。它不拥有规则裁定、不拥有战役
生命周期、也不提交数据库；只做四件事，全部是确定性函数（不碰库、不调模型）：

- 认出结构化插件错误与坏形状——错误响应里夹带的半成品一律不采信（§3.6 第 1 条）；
- 把已确认后果转成带来源 / 受众 / 时间 / 因果的 `change_intent`（§3.8）；
- 候选 / 未确认 / 无法映射项进 `pending`（只能待审），首版明确不支持的 kind 进
  `rejected` 并给出替代路径——不静默丢弃、不升格、不发明万能字段（§5 / §3.6 第 5 条）；
- 收敛成 `normalized_result`（§3.6），交给联合提交协调器（`runtime/trpg.py::_joint_apply`）。

来源与版本由宿主路径传进来（`origin`）：插件不能靠响应自报来源或版本来取得写权限。

两种兼容输入（§3.7）：完整战役路径用 `consequences`（变化意图类别 + operation）；
B0 无状态 resolver 保留 `effects`（世界效果名）。两者过同一套确定性 / 受众 / 因果检查，
但 B0 不接受规则状态 patch、场景转换与时间消耗——不能借兼容字段伪装成完整战役结果。

映射表（kind → 世界事实效果闭集）只有一份，在 `runtime/change.py`；本模块不另立一套。
"""

from __future__ import annotations

import json
from typing import Any

from ..world.validate import EXPIRY_KINDS, SUPPORTED_EFFECTS
from . import campaign as campaign_mod
from . import change as change_mod
from . import rules as rules_mod

#: 规范化输出的三种状态（§3.6）
STATUSES = ("ready", "rejected", "needs_review")

#: 后果包 `kind` 闭集 = 变化意图类别（§5.1）＋ 只进场景转换的 `player_choice`
CONSEQUENCE_KINDS: tuple[str, ...] = (*change_mod.KINDS, "player_choice")

#: TRPG 来源轴（TRPG_RULES_LAYER_SPEC §5.7）→ 对外接口来源轴（WORLD_RUNTIME_INTERFACE_SPEC §5.1）。
#: `npc_script`（剧本 / NPC 自动行为）在接口轴上归世界过程：它同样不经玩家行动。
SOURCE_MODES: dict[str, str] = {
    "action": "trpg_rule",
    "gm_declaration": "gm_declaration",
    "world_process": "world_process",
    "npc_script": "world_process",
}

#: 首版明确不支持、且不能假装支持的 kind（拒绝理由与替代路径的表在 runtime/change.py）
REFUSED: dict[str, str] = change_mod.REFUSED

#: 世界事实效果闭集名：兼容输入 `effects` 用的就是这一套名字
WORLD_EFFECTS: tuple[str, ...] = tuple(SUPPORTED_EFFECTS)

#: 受众闭集（给错误文案用；判定只有 campaign_mod.audience_ok 一处）
AUDIENCE_HINT = ("public_party", "gm_only", "player:<id>", "character:<id>", "npc:<id>")


def origin_block(
    instance_id: str,
    timeline_id: str,
    *,
    campaign_id: str = "",
    action_ref: str = "",
    source_mode: str = "action",
    source_plugin: str = "",
    expected_revision: int = 0,
    expected_state_revisions: dict[str, int] | None = None,
) -> dict[str, Any]:
    """宿主路径提供的来源与版本（§3.5）：插件响应里的同名字段一概不看。"""
    return {
        "instance_id": str(instance_id or ""),
        "timeline_id": str(timeline_id or ""),
        "campaign_id": str(campaign_id or ""),
        "action_ref": str(action_ref or ""),
        "source_mode": str(source_mode or ""),
        "source_plugin": str(source_plugin or ""),
        "expected_revision": int(expected_revision or 0),
        "expected_state_revisions": {
            str(key): int(value) for key, value in dict(expected_state_revisions or {}).items()
        },
    }


def normalize(
    result: dict[str, Any],
    *,
    origin: dict[str, Any],
    package: dict[str, Any] | None = None,
    audience: Any = campaign_mod.PUBLIC_PARTY,
    campaign: bool = True,
) -> dict[str, Any]:
    """§3.6 规范化输出：把插件响应收敛成 ready / rejected / needs_review 三种结果。

    `package` 是实例设定（`state_change` 要按目标类别落制度 / 惯例 / 环境状态时才用得上）；
    `audience` 是宿主本次提交的受众——插件没声明受众时按它走，自由字符串一律不当「公开」；
    `campaign=False` = B0 兼容路径：规则状态 patch、场景转换与时间消耗都不接受（§3.7）。
    """
    origin = dict(origin or {})
    errors: list[str] = []
    warnings: list[str] = []
    pending: list[dict[str, str]] = []
    refused: list[dict[str, str]] = []

    # 1) 结构化插件错误：错误响应不得携带可采信的 patch / effects / consequences（§3.6 第 1 条）
    plugin_err = rules_mod.plugin_error(result)
    if plugin_err is not None:
        if plugin_err["half_baked"]:
            errors.append(
                "插件错误响应里带了裁定半成品（"
                + "、".join(plugin_err["half_baked"])
                + "）：规约禁止，一律不采信"
            )
        return _result(
            status="rejected" if plugin_err["kind"] == "rejected" else "needs_review",
            origin=origin, resolution={}, patch=None, changes=[], claims=[], transition={},
            time_request=None, errors=errors, warnings=warnings, pending=pending,
            refused=refused, compat_effects=[], plugin_error=plugin_err,
        )

    # 2) 响应形状：resolution / rule_state_patch / 后果包 / claims / scene_transition
    resolution = result.get("resolution")
    if not isinstance(resolution, dict) or not resolution:
        errors.append("插件响应缺少 resolution 对象")
        resolution = {}
    patch = result.get("rule_state_patch")
    if patch is not None:
        errors.extend(campaign_mod.validate_patch(patch))
    if not campaign and patch:
        errors.append("B0 兼容路径不接受 rule_state_patch：持续规则状态要走战役路径（§3.7）")

    raw_items, compat = _materials(result, errors=errors)
    claims_out, claim_index = _claims(result.get("claims"), errors=errors)
    transition = result.get("scene_transition")
    if transition is not None and not isinstance(transition, dict):
        errors.append("scene_transition 必须是对象")
        transition = {}
    transition = dict(transition or {})
    if not campaign and transition:
        errors.append("B0 兼容路径不接受 scene_transition / 待选择：它们属于战役路径（§3.7）")
    choices = _choices(transition, errors=errors)
    time_request = transition.get("world_time_request") or None
    if time_request is not None and not isinstance(time_request, dict):
        errors.append("scene_transition.world_time_request 必须是对象（{seconds, cause?}）")
        time_request = None

    # 3) 逐条规范化后果：已确认 → change_intent（＋世界效果）；其余分类报出
    changes: list[dict[str, Any]] = []
    effects: list[dict[str, Any]] = []
    for index, item in enumerate(raw_items):
        where = f"{'effects' if compat else 'consequences'}[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{where} 必须是对象")
            continue
        ident = str(item.get("consequence_id") or item.get("id") or f"c-{index + 1}")
        kind = str(item.get("kind") or "")
        certainty = str(item.get("certainty") or "confirmed")
        if certainty not in change_mod.CERTAINTIES:
            errors.append(f"{where}.certainty 不在闭集内：{certainty}")
            continue
        if certainty != "confirmed":
            # 候选 / 未确认只能进候选 / 待审，不能直接提交为世界事实（§3.8）
            pending.append({"id": ident, "reason": f"certainty={certainty}：确认之后才能成为世界事实"})
            continue

        if compat:  # 世界效果名（§3.7 兼容输入）：确定性 / 受众 / 因果同样要过
            if kind not in WORLD_EFFECTS:
                errors.append(f"{where}.kind 不是世界事实效果闭集里的类型：{kind or '（空）'}")
                continue
            effects.append(_world_effect(item, where=where, errors=errors, warnings=warnings))
            continue
        if kind not in CONSEQUENCE_KINDS:
            errors.append(
                f"{where}.kind 不在后果包闭集内：{kind or '（空）'}（变化意图类别，或 player_choice；"
                "世界效果名请放进 effects 兼容字段）"
            )
            continue
        if not campaign and kind in ("player_choice", "time_advance"):
            errors.append(f"{where}.kind 在 B0 兼容路径上没有提交路径：{kind}（它们属于战役路径，§3.7）")
            continue
        if kind == "player_choice":
            visibility = _visibility(item.get("visibility"))
            choice = None
            if visibility is None or (visibility and not campaign_mod.audience_ok(visibility)):
                errors.append(f"{where}.visibility 不在受众闭集内（{' / '.join(AUDIENCE_HINT)}）：{item.get('visibility')}")
            else:
                choice = _player_choice(
                    item, ident=ident, audience=visibility or campaign_mod.audience_set(audience),
                    warnings=warnings,
                )
            if visibility is not None and choice is None:
                pending.append({"id": ident, "reason": "player_choice 必须给出 options：未选择的分支只留在场景转换"})
            elif choice is not None:
                choices.append(choice)
            continue
        if kind == "time_advance":
            request = _time_request(item, ident=ident, origin=origin, errors=errors, pending=pending)
            if request is None:
                continue
            if time_request is not None:
                pending.append({"id": ident, "reason": "同一批只能有一个时间请求（场景里已经给了一个）"})
                continue
            time_request = request
            continue
        if kind in REFUSED:
            refused.append({"id": ident, "reason": REFUSED[kind]})
            continue

        change = _intent(
            item, ident=ident, where=where, origin=origin, audience=audience,
            claims=claim_index, claims_out=claims_out, errors=errors, pending=pending, warnings=warnings,
        )
        if change is None:
            continue
        changes.append(change)
        effect = _map_effect(change, package=package or {}, pending=pending)
        if effect is not None:
            effects.append(effect)

    # 4) 最终形状闸：change_intent 列表再过一次共享校验（表在 runtime/change.py）
    if changes:
        errors.extend(change_mod.validate_intents(changes))
    if not effects and (changes or claims_out):
        warnings.append("这批后果里没有可提交的世界事实效果：事件帧与说法不能代替结构化效果（§5.1）")
    if choices:
        transition = {**transition, "available_choices": choices}
    if time_request is not None:
        # 时间消耗不走普通效果（§5.1）：只放独立时间请求，由联合提交管线同批前移时钟
        transition = {**transition, "world_time_request": time_request}

    status = "rejected" if (errors or refused) else ("needs_review" if pending else "ready")
    payload = {
        "resolution": resolution,
        "effects": effects,
        "changes": changes,
        "claims": claims_out,
        "rule_state_patch": patch,
        "scene_transition": transition or {},
        "participants": [str(item) for item in result.get("participants") or [] if str(item or "").strip()],
        "origin": origin,
    }
    return _result(
        status=status, origin=origin, resolution=resolution, patch=patch, changes=changes,
        claims=claims_out, transition=transition, time_request=time_request, errors=errors,
        warnings=warnings, pending=pending, refused=refused,
        compat_effects=[item for item in effects if item.get("compat")], payload=payload,
    )


# ------------------------------------------------------------------ 内部

def _materials(result: dict[str, Any], *, errors: list[str]) -> tuple[list[Any], bool]:
    """取后果材料：`consequences`（战役契约）优先，缺了才认 `effects`（B0 兼容，§3.7）。"""
    items = result.get("consequences")
    if items is None:
        items = result.get("effects")
        if items is None:
            return [], False
        if not isinstance(items, list):
            errors.append("effects 必须是数组")
            return [], True
        return items, True
    if not isinstance(items, list):
        errors.append("consequences 必须是数组")
        return [], False
    return items, False


def _claims(raw: Any, *, errors: list[str]) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """说法 / 获知材料（§3.9）：保留文本、渠道、受众与确信——说法不等于它描述的事实。"""
    if raw is None:
        return [], {}
    if not isinstance(raw, list):
        errors.append("claims 必须是数组")
        return [], {}
    out: list[dict[str, Any]] = []
    index: dict[str, dict[str, Any]] = {}
    for position, item in enumerate(raw):
        if not isinstance(item, dict):
            errors.append(f"claims[{position}] 必须是对象")
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            errors.append(f"claims[{position}].text 不能为空（说法正文只能来自这里，不从值字段现编）")
            continue
        try:
            credibility = float(item.get("credibility") or 0.6)
        except (TypeError, ValueError):
            errors.append(f"claims[{position}].credibility 必须是数字")
            credibility = 0.6
        claim = {
            "text": text,
            "source_id": str(item.get("source_id") or ""),
            "audience": str(item.get("audience") or "public"),
            "credibility": credibility,
        }
        ident = str(item.get("id") or item.get("claim_id") or "")
        if ident:
            claim["id"] = ident
            index[ident] = claim
        out.append(claim)
    return out, index


def _choices(transition: dict[str, Any], *, errors: list[str]) -> list[dict[str, Any]]:
    out = []
    for position, item in enumerate(transition.get("available_choices") or []):
        if not isinstance(item, dict):
            errors.append(f"scene_transition.available_choices[{position}] 必须是对象")
            continue
        out.append(item)
    return out


def _visibility(raw: Any) -> list[str] | None:
    """受众 / 观察者范围：闭集里的受众串（或一串），`{"audience": [...]}` 也认。

    自由字符串**不当作公开**（§3.8）——不是闭集就返回 None，由调用方拒绝。
    """
    if raw is None:
        return []
    items = raw.get("audience") if isinstance(raw, dict) else raw
    if items is None:
        return []
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, list):
        return None
    return [str(item) for item in items if str(item or "").strip()]


def _refs(raw: Any) -> list[str]:
    items = raw if isinstance(raw, list) else [raw]
    return [str(item) for item in items if str(item or "").strip()]


def _intent(
    item: dict[str, Any],
    *,
    ident: str,
    where: str,
    origin: dict[str, Any],
    audience: Any,
    claims: dict[str, dict[str, Any]],
    claims_out: list[dict[str, Any]],
    errors: list[str],
    pending: list[dict[str, str]],
    warnings: list[str],
) -> dict[str, Any] | None:
    """一条已确认后果 → change_intent（§3.8）：结构不完整一律报出，不替它补默认值。"""
    kind = str(item.get("kind") or "")
    operation = str(item.get("operation") or "")
    if operation not in change_mod.OPERATIONS:
        errors.append(f"{where}.operation 必须在闭集内：{' / '.join(change_mod.OPERATIONS)}")
        return None
    target_refs = _refs(item.get("target_refs") if item.get("target_refs") is not None else item.get("target"))
    subject_refs = _refs(item.get("subject_refs") if item.get("subject_refs") is not None else item.get("subject"))
    cause_refs = _refs(item.get("cause_refs") if item.get("cause_refs") is not None else item.get("cause_ref"))
    if not cause_refs:
        # 来源由宿主路径提供：行动路径 = 这次行动，其余路径 = 来源本身（§3.8 末条）
        host = str(origin.get("action_ref") or "") or str(origin.get("source_mode") or "")
        cause_refs = [host] if host else []
    if not cause_refs:
        pending.append({"id": ident, "reason": "缺少因果来源（cause_refs）：回不到行动 / 裁定 / GM 声明 / 世界过程"})
        return None
    visibility = _visibility(item.get("visibility"))
    if visibility is None or (visibility and not campaign_mod.audience_ok(visibility)):
        errors.append(
            f"{where}.visibility 不在受众闭集内（{' / '.join(AUDIENCE_HINT)}）："
            f"{item.get('visibility')}——自由字符串不当作公开（§3.8）"
        )
        return None
    if not visibility:
        visibility = campaign_mod.audience_set(audience)
    expiry = str(item.get("expiry") or "")
    if expiry and expiry not in EXPIRY_KINDS:
        errors.append(f"{where}.expiry 必须是 {' / '.join(EXPIRY_KINDS)} 之一：{expiry}")
        return None
    if expiry == "natural_recovery" and not str(item.get("recovery") or "").strip():
        pending.append({"id": ident, "reason": "natural_recovery 的后果必须写明恢复条件（recovery）"})
        return None
    value = item.get("value")
    if isinstance(value, list) or not isinstance(value, (str, int, float, bool, dict, type(None))):
        errors.append(f"{where}.value 必须是标量或对象：列表会被当成塞进去的自然语言背景（§3.8）")
        return None
    if kind == "knowledge_change":
        value = _knowledge_value(
            item, ident=ident, where=where, claims=claims, claims_out=claims_out,
            visibility=visibility, pending=pending, warnings=warnings,
        )
        if value is None:
            return None
    elif isinstance(value, dict):
        # 世界事实效果的值是标量：结构化的值序列化成 JSON 文本，避免 Python repr 进库
        value = json.dumps(value, ensure_ascii=False)
    effective = int(item.get("effective_time") or origin.get("expected_revision") or 0)
    if effective > int(origin.get("expected_revision") or 0):
        pending.append(
            {
                "id": ident,
                "reason": f"effective_time={effective} 晚于当前世界水位：预约 / 未来计划不能当成已发生"
                "（时间消耗与预约走独立时间语义）",
            }
        )
        return None
    return {
        "id": ident,
        "kind": kind,
        "subject_refs": subject_refs,
        "target_refs": target_refs,
        "operation": operation,
        "value": value,
        "certainty": "confirmed",
        "visibility": visibility,
        "effective_time": effective,
        "expiry": expiry,
        "clear_when": str(item.get("clear_when") or ""),
        "cause_refs": cause_refs,
        "source_mode": SOURCE_MODES.get(str(origin.get("source_mode") or ""), ""),
        "source_module": "trpg",
    }


def _knowledge_value(
    item: dict[str, Any],
    *,
    ident: str,
    where: str,
    claims: dict[str, dict[str, Any]],
    claims_out: list[dict[str, Any]],
    visibility: list[str],
    pending: list[dict[str, str]],
    warnings: list[str],
) -> Any:
    """认知后果的取值：引用声明过的说法，或自带一段文本（§3.9：说法不是事实）。"""
    value = item.get("value")
    if isinstance(value, dict):
        ref = str(value.get("claim_ref") or value.get("claim_id") or "")
        text = str(value.get("text") or "").strip()
    else:
        ref, text = "", str(value or "").strip()
    if ref:
        if ref not in claims:
            pending.append(
                {"id": ident, "reason": f"knowledge_change 引用了没有声明的说法：{ref}（说法要写进 claims[]）"}
            )
            return None
        return {"claim_ref": ref}
    if not text:
        pending.append({"id": ident, "reason": "knowledge_change 需要 value.claim_ref 或 value 文本"})
        return None
    claims_out.append(
        {"text": text, "source_id": "", "audience": str((visibility or ["public"])[0]), "credibility": 0.6}
    )
    warnings.append(
        f"{where}: knowledge_change 直接用文本当说法（没有渠道）——它不会传播到任何角色；"
        "要送达就把说法写进 claims[] 并给 source_id 渠道"
    )
    return {"text": text}


def _map_effect(
    change: dict[str, Any], *, package: dict[str, Any], pending: list[dict[str, str]]
) -> dict[str, Any] | None:
    """把 change_intent 落成世界事实效果（§5.2 首版映射）；映射不出来就如实报出。"""
    kind = str(change["kind"])
    if kind in ("world_event", "knowledge_change"):
        return None  # 事件帧是叙述材料、认知走 claims：都不产生事实效果
    target = (change["target_refs"] or [""])[0]
    if kind == "state_change":
        effect_kind = change_mod.STATE_KIND_BY_TARGET.get(change_mod.classify_target(package, target) or "")
        if effect_kind is None:
            pending.append(
                {
                    "id": str(change["id"]),
                    "reason": f"state_change 的目标不是世界包里已登记的结构：{target or '（空）'}",
                }
            )
            return None
    else:
        effect_kind = change_mod.EFFECT_BY_KIND.get(kind)
    if effect_kind is None:
        pending.append({"id": str(change["id"]), "reason": f"{kind} 没有对应的世界事实效果"})
        return None
    effect: dict[str, Any] = {"kind": effect_kind, "target": target}
    if change.get("value") is not None:
        effect["value"] = change["value"]
    if change.get("expiry"):
        effect["expiry"] = str(change["expiry"])
    elif change.get("clear_when"):
        effect["expiry"] = "until_cleared"
    return effect


def _world_effect(
    item: dict[str, Any], *, where: str, errors: list[str], warnings: list[str]
) -> dict[str, Any]:
    """兼容输入（`effects` / 世界效果名）的规范化：值、有效期与受众都按同一套规则检查。"""
    warnings.append(f"{where}: 用的是世界效果名（兼容输入，§3.7）——建议改用 consequences 的变化意图形态")
    effect: dict[str, Any] = {
        "kind": str(item.get("kind")),
        "target": str(item.get("target") or ""),
        "compat": True,
    }
    visibility = _visibility(item.get("visibility"))
    if visibility is None or (visibility and not campaign_mod.audience_ok(visibility)):
        errors.append(
            f"{where}.visibility 不在受众闭集内（{' / '.join(AUDIENCE_HINT)}）：{item.get('visibility')}"
        )
    value = item.get("value")
    if isinstance(value, dict):
        value = json.dumps(value, ensure_ascii=False)
    if isinstance(value, list) or not isinstance(value, (str, int, float, bool, dict, type(None))):
        errors.append(f"{where}.value 必须是标量或对象")
    elif value is not None:
        effect["value"] = value
    if item.get("expiry"):
        effect["expiry"] = str(item["expiry"])
    if item.get("recovery"):
        effect["recovery"] = str(item["recovery"])
    return effect


def _player_choice(item: dict[str, Any], *, ident: str, audience: Any, warnings: list[str]) -> dict[str, Any] | None:
    """未选分支只进场景转换（§5.1）：不预写效果 / 说法 / 时间 / 认知。"""
    options = item.get("options") or item.get("choices")
    if options is None and isinstance(item.get("value"), list):
        options = item["value"]
    if not isinstance(options, list) or not options:
        return None
    warnings.append(f"consequences: player_choice（{ident}）只进场景转换的 available_choices，不进世界事实")
    return {
        "choice_id": str(item.get("choice_id") or ident),
        "options": options,
        "audience": str((_visibility(item.get("visibility")) or campaign_mod.audience_set(audience))[0]),
        "prompt_ref": str(item.get("prompt_ref") or ""),
    }


def _time_request(
    item: dict[str, Any], *, ident: str, origin: dict[str, Any], errors: list[str], pending: list[dict[str, str]]
) -> dict[str, Any] | None:
    """时间消耗不走普通效果（§5.1）：转成联合时间请求，由提交管线同批前移时钟。"""
    value = item.get("value")
    payload = dict(value) if isinstance(value, dict) else {"seconds": value}
    try:
        seconds = int(payload.get("seconds") or 0)
    except (TypeError, ValueError):
        errors.append(f"time_advance（{ident}）.value.seconds 必须是整数")
        return None
    if seconds <= 0:
        pending.append({"id": ident, "reason": "time_advance 必须是正秒数（世界时间只往前）"})
        return None
    source = str(payload.get("source") or "")
    if source not in ("world_process", "player_action", "gm_declaration"):
        source = "player_action" if str(origin.get("action_ref") or "") else "gm_declaration"
    return {"seconds": seconds, "cause": str(payload.get("cause") or ""), "source": source}


def _result(
    *,
    status: str,
    origin: dict[str, Any],
    resolution: dict[str, Any],
    patch: Any,
    changes: list[dict[str, Any]],
    claims: list[dict[str, Any]],
    transition: dict[str, Any],
    time_request: dict[str, Any] | None,
    errors: list[str],
    warnings: list[str],
    pending: list[dict[str, str]],
    refused: list[dict[str, str]],
    compat_effects: list[dict[str, Any]],
    payload: dict[str, Any] | None = None,
    plugin_error: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """§3.6 的 normalized_result：字段就这些，别在返回值里再塞一套平行表示。"""
    return {
        "status": status,
        "origin": origin,
        "raw_resolution": resolution,
        "rule_state_patch": patch,
        "changes": changes,
        "claims": claims,
        "scene_transition": transition,
        "world_time_request": time_request,
        "errors": errors,
        "warnings": warnings,
        "pending": pending,
        "rejected": refused,
        "compat_effects": compat_effects,
        "plugin_error": plugin_error,
        "payload": payload
        or {
            "resolution": resolution,
            "effects": [],
            "changes": [],
            "claims": [],
            "rule_state_patch": None,
            "scene_transition": {},
            "participants": [],
            "origin": origin,
        },
    }
