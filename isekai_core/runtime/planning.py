"""角色自主生成打算（WORLD_RUNTIME_SPEC §11.3 / EVENT_ENGINE_SPEC §六）。

后台让模型替角色想一步：给定她**已知的**东西（说法、观察、仍在的后果、未竟之事），
提出一个受支持的行动候选。模型只能从闭集里选效果类型、只能指向她可知的目标；
运行层照旧做前置校验与原子提交——「引擎不得为了完成故事链替角色生成未获知的目标」。
"""

from __future__ import annotations

import json
import re
from typing import Any

#: 角色行动可用的效果闭集：世界级效果（渠道延迟 / 环境改值）不由角色直接选
ACTION_KINDS: tuple[str, ...] = (
    "activity_constraint",
    "route_blocked",
    "public_notice",
    "rumor_spread",
    "institution_state",
)

#: 在世角色同时保留的未竟之事上限（不逐轮重做计划、不堆事项）
MAX_LIVE_INTENTS = 3


def allowed_targets(
    package: dict[str, Any],
    card: dict[str, Any],
    *,
    knowledge: list[dict[str, Any]],
    observations: list[dict[str, Any]],
) -> dict[str, list[str]]:
    """她可知的目标集合：自己的角色 / 地区 / 组织、她掌握的渠道、她观察到的环境类型。"""
    identity = card.get("identity") if isinstance(card.get("identity"), dict) else {}
    role = str(card.get("role_id") or "")
    region = str(identity.get("region") or "")
    roles = {str(item.get("id")) for item in card.get("roles") or []}
    channels = {str(item.get("source_id")) for item in card.get("channels") or [] if item.get("source_id")}
    env_types = {str(item.get("type_id")) for item in observations if item.get("type_id")}
    institutions = {
        str(item.get("id"))
        for item in (package.get("world") or {}).get("institutions") or []
        if isinstance(item, dict)
    } if isinstance(package.get("world"), dict) else set()
    targets: dict[str, list[str]] = {
        "activity_constraint": sorted({role, region} - {""}),
        "route_blocked": sorted({region} - {""}),
        "public_notice": sorted(channels),
        "rumor_spread": sorted(channels),
        "institution_state": sorted(institutions),
    }
    roles.clear()
    _ = knowledge
    return targets


def prompt(
    *,
    name: str,
    occupation: str,
    world_label: str,
    aims: list[dict[str, Any]],
    knowledge: list[dict[str, Any]],
    effects: list[dict[str, Any]],
    observations: list[dict[str, Any]],
    allowed: dict[str, list[str]],
) -> list[dict[str, str]]:
    """让她自己决定要不要动手：只给可知信息，答案必须落在允许的类型与目标上。"""
    lines = [
        f"你在扮演「{name}」（{occupation}），现在是 {world_label}。",
        "请判断她自己会不会想做点什么（一件具体、办得到的小事），并只输出 JSON。",
        "硬约束：",
        "1) 只能从下面的 action_kind 里选一个；target 必须在该类型允许的目标里；",
        "2) 动机必须来自下面列出的她已知信息，不能引用没给她的东西；",
        "3) 不写对话、不写心理描写，只给她要不要动手与动手的理由；",
        '4) 输出：{"object":"…","basis":"…","strength":0.0-1.0,"action_kind":"…","target":"…"}；'
        '若她自己不会动手，输出 {"object":""}。',
        "允许的行动类型与目标：",
    ]
    for kind, targets in allowed.items():
        if targets:
            lines.append(f"- {kind}: {', '.join(targets)}")
    if aims:
        lines.append("她手上还没办完的事：" + "；".join(f"{item['object']}（{item['stage']}）" for item in aims[:3]))
    if observations:
        lines.append(
            "她能观察到的环境：" + "；".join(f"{item['name']}={item['value']}{item['unit']}" for item in observations[:4])
        )
    if effects:
        lines.append(
            "仍在生效的后果：" + "；".join(f"{item['kind']}（目标 {item['target']}）" for item in effects[:4])
        )
    if knowledge:
        lines.append("她已知的消息：")
        for item in knowledge[:8]:
            lines.append(f"- [{item.get('source')}] {item.get('text')}")
    return [{"role": "system", "content": "\n".join(lines)}]


def parse(text: str, allowed: dict[str, list[str]]) -> dict[str, Any] | None:
    """解析并校验：类型在闭集内、目标在她可知集合里、动机与描述非空。"""
    raw = text.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = raw.rstrip("`")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    obj = str(payload.get("object") or "").strip()
    if not obj:
        return None  # 她自己不动手，是合法结果
    kind = str(payload.get("action_kind") or "")
    if kind not in ACTION_KINDS:
        return None
    target = str(payload.get("target") or "")
    if target not in allowed.get(kind, []):
        return None  # 指向她不可知的目标 = 引擎替她生成目标，不允许
    basis = str(payload.get("basis") or "").strip()
    if not basis:
        return None
    strength = payload.get("strength")
    value = float(strength) if isinstance(strength, (int, float)) else 0.5
    return {
        "object": obj,
        "basis": basis,
        "strength": max(0.0, min(1.0, value)),
        "effect": {"kind": kind, "target": target, "expiry": "with_cause"},
    }
