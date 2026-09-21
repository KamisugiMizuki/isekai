"""用户引入事件（EVENT_ENGINE_SPEC §八 + 附录 B #7/#17）。

这是**显式管理操作**，不是聊天命令的隐式副作用；用户仍不成为世界内人物。流程：
草案（只展示用户意图与可确认部分）→ 校验（锁定公理、可执行效果、来源点状态）→
确认（原子创建新线并注入；预约事件只写待执行状态）→ 到点复核后施加或记为未执行。

纯函数层：草案的规范化与校验，不碰数据库、不调模型。
"""

from __future__ import annotations

import json
import re
from typing import Any

from .events import SUPPORTED_EFFECTS, stable_key

#: 用户可在草案里选的行动族（与引擎的模板族对齐，但由用户显式给出而非抽样）
TIME_KINDS = ("now", "scheduled")


def normalize_draft(
    package: dict[str, Any],
    payload: dict[str, Any],
    *,
    known_targets: set[str],
    world_seconds: int,
    default_channels: list[str] | None = None,
) -> dict[str, Any]:
    """把用户意图规范成草案：明确拒绝无法表达的效果，不把自由文本当成已执行。

    `known_targets` 是来源点已登记的对象（角色 + 登记实体 + 渠道 + 环境类型）；
    指向未登记对象的效果一律拒绝——「可以改变当前局势，不能改写过去或公理」。
    """
    intent = str(payload.get("intent") or "").strip()
    if not intent:
        raise ValueError("草案缺少修改意图")
    when = str(payload.get("when") or "now")
    if when not in TIME_KINDS:
        raise ValueError(f"不支持的时间类型：{when}")
    at_world = world_seconds if when == "now" else int(payload.get("at_world") or 0)
    if when == "scheduled" and at_world <= world_seconds:
        raise ValueError("预约事件的目标时刻必须晚于来源点水位")

    effects: list[dict[str, Any]] = []
    for item in payload.get("effects") or []:
        if not isinstance(item, dict):
            raise ValueError("效果必须是结构化条目")
        kind = str(item.get("kind") or "")
        if kind not in SUPPORTED_EFFECTS:
            raise ValueError(f"不支持的效果类型：{kind or '（空）'}")
        target = str(item.get("target") or "")
        if target not in known_targets:
            raise ValueError(f"效果目标未登记：{target or '（空）'}")
        # 制度 / 惯例效果只能指向世界包登记过的职位 / 惯例：目标对了才不会被「接受然后静默失效」
        world = package.get("world") if isinstance(package.get("world"), dict) else {}
        if kind == "institution_state":
            offices = {
                str(office.get("id"))
                for entry in world.get("institutions") or [] if isinstance(entry, dict)
                for office in entry.get("offices") or [] if isinstance(office, dict)
            }
            if target not in offices:
                raise ValueError(f"制度效果只能指向世界包登记的职位：{target}")
        elif kind == "custom_state":
            customs = {str(entry.get("id")) for entry in world.get("customs") or [] if isinstance(entry, dict)}
            if target not in customs:
                raise ValueError(f"惯例效果只能指向世界包登记的惯例：{target}")
        effect = {"kind": kind, "target": target, "expiry": str(item.get("expiry") or "with_cause")}
        if item.get("value") is not None:
            effect["value"] = str(item["value"])
        if item.get("recovery"):
            effect["recovery"] = str(item["recovery"])
        effects.append(effect)
    if not effects:
        raise ValueError("草案至少要有一个受支持的事实效果（否则请改世界包后新建实例）")

    claims: list[dict[str, Any]] = []
    for item in payload.get("claims") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        channel = str(item.get("source_id") or "")
        if not text:
            continue
        if channel and channel not in known_targets:
            raise ValueError(f"说法渠道未登记：{channel}")
        claims.append({
            "text": text,
            "source_id": channel,
            "audience": str(item.get("audience") or "public"),
            "credibility": float(item.get("credibility") or 0.6),
        })
    if not claims and default_channels:
        claims.append({
            "text": intent,
            "source_id": default_channels[0],
            "audience": "public",
            "credibility": 0.6,
        })

    return {
        "intent": intent,
        "when": when,
        "at_world": at_world,
        "effects": effects,
        "claims": claims,
        "participants": [str(item) for item in payload.get("participants") or []],
        "note": str(payload.get("note") or ""),
    }


def draft_id_for(instance_id: str, timeline_id: str, intent: str, at_world: int) -> str:
    """草案标识按内容稳定：重试同一意图不会重复造线（§八 第 8 条）。"""
    return f"dr-{stable_key(instance_id, timeline_id, intent, at_world)[:12]}"


def public_draft(draft: dict[str, Any]) -> dict[str, Any]:
    """草案展示面：只给用户提供的意图与可确认部分，不展示既有隐藏事实（§八 第 2/5 条）。"""
    return {
        "draft_id": draft.get("id"),
        "intent": draft.get("intent"),
        "when": draft.get("when"),
        "at_world": draft.get("at_world"),
        "effects": draft.get("effects") or [],
        "claims": [
            {"text": item.get("text"), "source_id": item.get("source_id"), "audience": item.get("audience")}
            for item in draft.get("claims") or []
        ],
        "participants": draft.get("participants") or [],
        "source": draft.get("source") or {},
        "confirmed": bool(draft.get("confirmed")),
        "timeline_id": draft.get("timeline_id") or "",
    }


def parse_proposal(text: str, known_targets: set[str]) -> dict[str, Any]:
    """把模型给出的意图草案解析成结构化形状；不合规的部分整条丢弃（与 §八 第 4 条一致）。"""
    raw = str(text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = raw.rstrip("`")
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        payload = json.loads(raw[start : end + 1])
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    out: dict[str, Any] = {"intent": str(payload.get("intent") or "")}
    effects = []
    for item in payload.get("effects") or []:
        if not isinstance(item, dict):
            continue
        kind, target = str(item.get("kind") or ""), str(item.get("target") or "")
        if kind in SUPPORTED_EFFECTS and target in known_targets:
            effects.append({"kind": kind, "target": target, "expiry": str(item.get("expiry") or "with_cause")})
    out["effects"] = effects
    claims = []
    for item in payload.get("claims") or []:
        if isinstance(item, dict) and str(item.get("text") or "").strip():
            claims.append({
                "text": str(item["text"]).strip(),
                "source_id": str(item.get("source_id") or ""),
                "audience": str(item.get("audience") or "public"),
                "credibility": 0.6,
            })
    out["claims"] = claims
    if payload.get("when") in TIME_KINDS:
        out["when"] = str(payload["when"])
    if isinstance(payload.get("at_world"), int):
        out["at_world"] = int(payload["at_world"])
    return out


def proposal_prompt(
    *,
    intent: str,
    world_label: str,
    allowed: dict[str, list[str]],
    channels: list[str],
) -> list[dict[str, str]]:
    """让模型把管理意图翻译成受支持的效果（闭集 + 已登记目标），不做别的判断。

    格式与允许清单放 system（同一个世界包内稳定），这次要翻译的意图与世界时刻放 user：
    system 段能吃到前缀缓存，意图每次都不同则只重算 user 段。
    """
    rules = [
        "玩家要对世界做一次显式修改（不是角色说话）。把他给的修改意图翻译成受支持的事实效果，",
        "只输出 JSON：",
        '{"intent":"一句话","when":"now|scheduled","at_world":0,"effects":[{"kind":"…","target":"…"}],'
        '"claims":[{"text":"说法原文","source_id":"…","audience":"public"}]}',
        "硬约束：",
        "1) kind 只能从下面列出的类型里选；target 必须在对应类型允许的目标里；",
        "2) 不能改写过去或公理：只写这次修改带来什么新局面；",
        "3) claims 是这次事件之后被人知道时流传的说法，可以是片面的；",
        "4) 表达不出来就输出 {}（宁可拒绝，不要编造）。",
        "允许的效果与目标：",
    ]
    for kind, targets in allowed.items():
        if targets:
            rules.append(f"- {kind}: {', '.join(targets)}")
    if channels:
        rules.append("可用渠道：" + ", ".join(channels))
    ask = [f"当前世界时刻：{world_label}", f"修改意图：{intent}"]
    return [
        {"role": "system", "content": "\n".join(rules)},
        {"role": "user", "content": "\n".join(ask)},
    ]
