"""TRPG 战役运行时的纯逻辑层（TRPG_CAMPAIGN_RUNTIME_SPEC §十一 / §十二）。

只做三件事，全部是确定性函数，不碰数据库、不调模型、不解析规则语义：

- 战役 / 行动 / 待选择的状态机与合法迁移；
- 规则私有状态 patch 的校验与应用（`opaque_state` 对核心不透明，但结构必须可校验）；
- 稳定标识与对外投影。

落库、插件调用与联合提交在 `runtime/trpg.py`。
"""

from __future__ import annotations

import json
import secrets
from typing import Any


def _loads(text: Any, fallback: Any) -> Any:
    """宽松 JSON 解析：列里可能是 '' 或半截文本，解析不出来就给兜底。"""
    if isinstance(text, (dict, list)):
        return text
    try:
        return json.loads(str(text or ""))
    except (TypeError, ValueError):
        return fallback

# ---------------------------------------------------------------- 状态机

CAMPAIGN_STATES = ("preparing", "active", "waiting", "paused", "blocked", "archived")
CAMPAIGN_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "preparing": ("active", "archived"),
    "active": ("waiting", "paused", "blocked", "archived"),
    "waiting": ("active", "blocked", "archived"),
    "paused": ("active", "blocked", "archived"),
    "blocked": ("active", "archived"),
    "archived": (),
}

ACTION_STATES = (
    "received", "interpreted", "awaiting_confirmation", "confirmed", "modified", "abandoned",
    "snapshotting", "resolving", "reviewing", "awaiting_choice", "awaiting_gm_review",
    "committing", "committed", "transitioned", "rejected", "conflict", "stale",
    "plugin_failed", "interrupted",
)
ACTION_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "received": ("interpreted", "abandoned"),
    "interpreted": ("awaiting_confirmation", "confirmed", "abandoned"),
    "awaiting_confirmation": ("confirmed", "modified", "abandoned"),
    "modified": ("interpreted", "awaiting_confirmation", "abandoned"),
    "confirmed": ("snapshotting", "abandoned"),
    "snapshotting": ("resolving", "plugin_failed", "interrupted"),
    "resolving": ("reviewing", "plugin_failed", "interrupted"),
    "reviewing": ("awaiting_choice", "awaiting_gm_review", "committing", "rejected"),
    "awaiting_choice": ("committing", "rejected", "interrupted"),
    "awaiting_gm_review": ("committing", "rejected"),
    "committing": ("committed", "conflict", "stale", "rejected"),
    "committed": ("transitioned",),
    "transitioned": (),
    "abandoned": (),
    "rejected": (),
    "conflict": ("committing", "rejected"),
    "stale": ("committing", "rejected"),
    "plugin_failed": ("resolving", "rejected"),
    "interrupted": ("resolving", "rejected"),
}

CHOICE_STATES = ("open", "selected", "cancelled", "expired")
CHOICE_TRANSITIONS: dict[str, tuple[str, ...]] = {
    "open": ("selected", "cancelled", "expired"),
    "selected": (),
    "cancelled": (),
    "expired": (),
}

#: 会改变规则状态或世界状态、因而必须走确认与联合提交的行动状态
LIVE_ACTION_STATES = ("confirmed", "snapshotting", "resolving", "reviewing", "awaiting_choice",
                      "awaiting_gm_review", "committing")


class CampaignError(ValueError):
    """战役运行时的输入 / 状态错误（调用方翻成 invalid_input 之类的错误码）。"""


def transition(table: str, current: str, target: str, *, what: str = "") -> str:
    """校验一次状态迁移；不合法就抛错（带出合法去向，便于排查而不是猜）。"""
    tables = {
        "campaign": (CAMPAIGN_TRANSITIONS, CAMPAIGN_STATES),
        "action": (ACTION_TRANSITIONS, ACTION_STATES),
        "choice": (CHOICE_TRANSITIONS, CHOICE_STATES),
    }
    if table not in tables:
        raise CampaignError(f"未知的状态机：{table}")
    rules, states = tables[table]
    if current not in rules:
        raise CampaignError(f"未知的{what or table}状态：{current}（可用：{' / '.join(states)}）")
    if target == current:
        return target
    if target not in rules[current]:
        allowed = " / ".join(rules[current]) or "（终态）"
        raise CampaignError(f"{what or table}不能从 {current} 迁到 {target}；合法去向：{allowed}")
    return target


# ---------------------------------------------------------------- 规则状态 patch

PATCH_OPS = ("add", "replace", "remove", "increase", "decrease")


def _pointer(path: str) -> list[str]:
    if not isinstance(path, str) or not path.startswith("/"):
        raise CampaignError(f"规则状态路径必须是以 / 开头的指针：{path!r}")
    parts = [part for part in path.split("/")[1:]]
    if not parts or any(part == "" for part in parts):
        raise CampaignError(f"规则状态路径有空段：{path!r}")
    return parts


def validate_patch(patch: Any) -> list[str]:
    """检查一份 `rule_state_patch` 的形状（不解析语义：核心不认识 path 的业务含义）。"""
    errors: list[str] = []
    if patch in (None, {}, []):
        return errors
    if not isinstance(patch, dict):
        return ["rule_state_patch 必须是对象"]
    if not isinstance(patch.get("ruleset_id"), str) or not patch.get("ruleset_id"):
        errors.append("rule_state_patch.ruleset_id 缺失")
    base = patch.get("base_state_revision")
    if not isinstance(base, int) or isinstance(base, bool) or base < 0:
        errors.append("rule_state_patch.base_state_revision 必须是非负整数")
    operations = patch.get("operations")
    if not isinstance(operations, list) or not operations:
        errors.append("rule_state_patch.operations 必须是非空数组")
        return errors
    for index, item in enumerate(operations):
        if not isinstance(item, dict):
            errors.append(f"operations[{index}] 必须是对象")
            continue
        try:
            _pointer(str(item.get("path") or ""))
        except CampaignError as exc:
            errors.append(f"operations[{index}]: {exc}")
        op = str(item.get("op") or "")
        if op not in PATCH_OPS:
            errors.append(f"operations[{index}].op 必须是 {' / '.join(PATCH_OPS)} 之一")
        if op != "remove" and "value" not in item:
            errors.append(f"operations[{index}] 缺少 value")
        if op in ("increase", "decrease") and not isinstance(item.get("value"), (int, float)):
            errors.append(f"operations[{index}].value 在 {op} 时必须是数字")
    return errors


def apply_patch(opaque: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    """把 patch 应用到规则状态副本上；**整体成功或整体失败**（不落半份 patch）。

    核心只认识容器结构（对象 / 数字 / 增删），不认识 `path` 的规则语义。
    """
    if not isinstance(opaque, dict):
        raise CampaignError("规则状态必须是对象")
    state: dict[str, Any] = _deepcopy(opaque)
    for index, item in enumerate(operations or []):
        parts = _pointer(str(item.get("path") or ""))
        op = str(item.get("op"))
        node: Any = state
        for part in parts[:-1]:
            if not isinstance(node, dict):
                raise CampaignError(f"operations[{index}]: {'/'.join(parts[:-1])} 不是对象")
            node = node.setdefault(part, {})
        leaf = parts[-1]
        if not isinstance(node, dict):
            raise CampaignError(f"operations[{index}]: {'/'.join(parts[:-1])} 不是对象")
        if op == "remove":
            if leaf not in node:
                raise CampaignError(f"operations[{index}]: 待删除的路径不存在：{item.get('path')}")
            node.pop(leaf)
            continue
        if op in ("increase", "decrease"):
            current = node.get(leaf)
            if not isinstance(current, (int, float)) or isinstance(current, bool):
                raise CampaignError(f"operations[{index}]: {'/'.join(parts)} 不是数字，无法增减")
            step = item.get("value")
            node[leaf] = current + step if op == "increase" else current - step
            continue
        if op == "replace" and leaf not in node:
            raise CampaignError(f"operations[{index}]: 待替换的路径不存在：{item.get('path')}")
        node[leaf] = _deepcopy(item.get("value"))
    return state


def _deepcopy(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _deepcopy(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_deepcopy(item) for item in value]
    return value


# ---------------------------------------------------------------- 标识与投影

def new_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def public_campaign(row: dict[str, Any]) -> dict[str, Any]:
    """战役对外投影：只给管理元数据，不带规则私有状态正文。"""
    return {
        "campaign_id": str(row.get("campaign_id") or ""),
        "instance_id": str(row.get("instance_id") or ""),
        "timeline_id": str(row.get("timeline_id") or ""),
        "ruleset_id": str(row.get("ruleset_id") or ""),
        "ruleset_version": str(row.get("ruleset_version") or ""),
        "status": str(row.get("status") or ""),
        "state_revision": int(row.get("state_revision") or 0),
        "current_scene_id": str(row.get("current_scene_id") or ""),
        "note": str(row.get("note") or ""),
    }


#: 受众集合（TRPG_CAMPAIGN_RUNTIME_SPEC §十五）：两份固定项 + 三类带标识的
PUBLIC_PARTY = "public_party"
GM_ONLY = "gm_only"
_AUDIENCE_PREFIXES = ("player:", "character:", "npc:")


def audience_ok(value: Any) -> bool:
    """受众标识是否合法：闭集，别让「谁看得见」变成自由文本。"""
    text = str(value or "")
    if text in (PUBLIC_PARTY, GM_ONLY):
        return True
    return any(text.startswith(prefix) and len(text) > len(prefix) for prefix in _AUDIENCE_PREFIXES)


def audience_visible(material: str, viewer: str) -> bool:
    """材料对这位观看者可见吗：GM 全见，公开全见，其余只认精确匹配。

    同一用户控制多个角色**不自动合并** `character:<id>`（§十五）——这里刻意不做
    用户级归并，宁可让调用方显式再传一个受众。
    """
    material = str(material or PUBLIC_PARTY)
    viewer = str(viewer or PUBLIC_PARTY)
    if viewer == GM_ONLY:
        return True
    if material == PUBLIC_PARTY:
        return True
    return material == viewer


def scene_view(scene: dict[str, Any], *, audience: str = PUBLIC_PARTY) -> dict[str, Any]:
    """场景投影（§十五）：公共材料人人可见，私密材料只给对应受众。

    - `public_facts` / `active_risks` / `available_actions`：逐项按 `audience` 字段裁剪
      （没有该字段的项按公开处理——公共材料就是公共的）；
    - `private_views`：只给观看者自己那一条，GM 拿全份。
    """
    out = {
        **{key: value for key, value in scene.items() if not key.endswith("_world")},
        "world_snapshot": _loads(scene.get("world_snapshot"), {}),
        "location_refs": _loads(scene.get("location_refs"), []),
        "participants": _loads(scene.get("participants"), []),
        "turn_state": _loads(scene.get("turn_state"), {}),
    }
    for key in ("public_facts", "active_risks", "available_actions"):
        items = _loads(scene.get(key), [])
        out[key] = [
            item for item in items
            if not isinstance(item, dict) or audience_visible(str(item.get("audience") or PUBLIC_PARTY), audience)
        ]
    views = _loads(scene.get("private_views"), {})
    out["private_views"] = views if audience == GM_ONLY else {
        key: value for key, value in views.items() if audience_visible(str(key), audience)
    }
    return out


def action_view(row: dict[str, Any], *, audience: str = "public_party") -> dict[str, Any]:
    """行动投影：GM 私有材料（原始 resolution）只有 gm_only 受众拿得到。"""
    out: dict[str, Any] = {
        "action_id": str(row.get("action_id") or ""),
        "scene_id": str(row.get("scene_id") or ""),
        "actor_id": str(row.get("actor_id") or ""),
        "intent": str(row.get("intent") or ""),
        "target_refs": row.get("target_refs") or "[]",
        "method": str(row.get("method") or ""),
        "confirmation": str(row.get("confirmation") or ""),
        "action_revision": int(row.get("action_revision") or 1),
        "status": str(row.get("status") or ""),
        "failure_code": str(row.get("failure_code") or ""),
        "joint_commit_id": str(row.get("joint_commit_id") or ""),
        "audience": str(row.get("audience") or PUBLIC_PARTY),
    }
    if audience == "gm_only":
        out["resolution"] = row.get("resolution") or "{}"
    return out
