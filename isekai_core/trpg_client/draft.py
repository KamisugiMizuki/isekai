"""行动草稿（TRPG_CLIENT_SPEC §6.1）：把自然语言整理成行动者 / 目标 / 方法 / 意图 / 风险。

一次低成本判断调用，严格 JSON 输出，失败或字段不明确就**留空并报缺口**——不猜（§6.1 末）。
"""

from __future__ import annotations

import json
import re
from typing import Any

#: 确认卡上的字段（§6.1）：行动者 / 目标 / 方法 / 意图 / 风险 + 原文
CARD_FIELDS = ("actor", "target", "method", "intent", "expected_result")
#: 缺一个就打不开确认卡的字段（行动者缺省取当前角色）
KEY_FIELDS = ("target", "intent")

LABELS = {
    "actor": "行动者",
    "target": "目标",
    "method": "方法",
    "intent": "意图",
    "expected_result": "预期结果",
    "risks": "风险",
}

#: 模型输出里允许出现的键（多了不采信）
_JSON_RE = re.compile(r"\{.*\}", re.S)


def draft_request(*, text: str, actor: str, scene: dict[str, Any], known_targets: list[str]) -> list[dict[str, str]]:
    """提示词：只做字段整理，不做裁定、不编造现场没有的东西。"""
    scene_line = str(scene.get("title") or "") + ("：" + str(scene.get("description") or "")
                                                   if scene.get("description") else "")
    actions = "、".join(str(item) for item in (scene.get("available_actions") or [])[:8])
    targets = "、".join(str(item) for item in known_targets[:20])
    system = (
        "你在把玩家的自然语言行动整理成结构化声明。只输出 JSON，不要解释。\n"
        "字段：target（行动直接作用的人或物）、method（怎么做）、intent（想达成什么）、"
        "expected_result（期望结果）、risks（可预见的风险，数组）。\n"
        "规则：只写用户话里能支撑的内容；不确定或话里没提的字段留空字符串，不要补全猜测；"
        "不要替玩家骰点、不要写裁定结果。"
    )
    user = (
        f"当前角色：{actor or '（未指定）'}\n当前场面：{scene_line or '（未记录）'}\n"
        f"可行动作：{actions or '（未记录）'}\n已登记目标：{targets or '（未记录）'}\n\n"
        f"玩家输入：{text}\n\n只输出 JSON。"
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def parse_draft(raw: Any) -> dict[str, Any]:
    """容错解析（模型常见 ```json 围栏）：解析不出来就返回空字段，让缺口说话。"""
    match = _JSON_RE.search(str(raw or ""))
    if not match:
        return {}
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, Any] = {}
    for key in (*CARD_FIELDS, "risks"):
        value = data.get(key)
        if key == "risks":
            out[key] = [str(item) for item in value if str(item or "").strip()] if isinstance(value, list) else []
        else:
            out[key] = str(value or "").strip()
    return out


def decide(
    *,
    text: str,
    explicit: dict[str, Any] | None = None,
    model: dict[str, Any] | None = None,
    actor: str = "",
    scene: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """字段合并与缺口判定：用户给的优先，模型只补用户没给的，缺口如实列出。"""
    explicit = explicit if isinstance(explicit, dict) else {}
    model = model if isinstance(model, dict) else {}
    scene = scene if isinstance(scene, dict) else {}
    fields: dict[str, Any] = {key: "" for key in CARD_FIELDS}
    sources: dict[str, str] = {key: "unknown" for key in CARD_FIELDS}
    for key in CARD_FIELDS:
        given = str(explicit.get(key) or "").strip()
        if given:
            fields[key] = given
            sources[key] = "user"
        elif str(model.get(key) or "").strip():
            fields[key] = str(model[key]).strip()
            sources[key] = "model"
    if not fields["actor"]:
        fields["actor"] = str(actor or "")
        if fields["actor"]:
            sources["actor"] = "user"
    risks = [str(item) for item in (explicit.get("risks") or model.get("risks") or []) if str(item or "").strip()]
    targets = {str(item) for item in (scene.get("participants") or []) if str(item or "")}
    targets |= {str(item) for item in (scene.get("location_refs") or []) if str(item or "")}
    gaps: list[str] = []
    for key in KEY_FIELDS:
        if not fields[key]:
            gaps.append(f"{LABELS[key]}还不明确（{LABELS[key]}：未给出）")
    if sources["target"] == "model" and targets and fields["target"] not in targets:
        # 模型给的目标不在当前场面的已登记实体里：按不确定处理，不带着幻觉目标去声明
        gaps.append(f"目标「{fields['target']}」不在当前场面已登记的人/地点里，请确认或改写")
        sources["target"] = "uncertain"
    return {
        "raw_text": str(text or ""),
        "fields": fields,
        "sources": sources,
        "risks": risks,
        "gaps": gaps,
        "ready": not gaps,
    }
