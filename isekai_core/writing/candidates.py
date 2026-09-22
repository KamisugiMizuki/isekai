"""候选与草稿的生命周期（WRITING_ASSISTANT_SPEC §4.3 / §十-4）：未提交就是未提交。

纯逻辑。三条要紧的：

- `approved` 只表示创作者同意采用该方案，**不表示世界已经改变**；只有
  `runtime.change.commit` 返回 `committed` / `duplicate`，候选里的世界变化才可被描述为已发生；
- 文本候选 `approved` 后可以直接形成草稿，但仍保留「未提交世界」的标记；
- 试演优先走分支，项目不提供世界线合并（§八）。
"""

from __future__ import annotations

import json
from typing import Any


def _json(value: Any, default: Any) -> Any:
    """库里的 JSON 列读出来是文本（也能直接喂字典）：读侧统一在这里解。"""
    if isinstance(value, type(default)):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except json.JSONDecodeError:
        return default
    return parsed if isinstance(parsed, type(default)) else default

#: 候选类型：世界变化 / 文本 / 场景 / 观察材料
KINDS: tuple[str, ...] = ("world_change", "text", "scene", "observation")
#: 生命周期（§4.3）
STATES: tuple[str, ...] = ("proposed", "selected", "approved", "committed", "rejected", "deferred", "stale")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "proposed": ("selected", "approved", "rejected", "deferred", "stale"),
    "selected": ("approved", "rejected", "deferred", "stale"),
    "approved": ("committed", "stale"),          # 批准 ≠ 已提交：还能过期
    "deferred": ("selected", "approved", "rejected", "stale"),
    "committed": (),
    "rejected": (),
    "stale": (),
}
#: 还没进世界的状态：产品面上必须这么标注
UNCOMMITTED: tuple[str, ...] = ("proposed", "selected", "approved", "deferred")

MUST_NOT_IMPLY: dict[str, str] = {
    "proposed": "这已经是决定",
    "selected": "世界已经按它变了",
    "approved": "世界已经按它变了",
    "deferred": "这条被丢掉了",
    "committed": "文本草稿也一起进世界了",
    "rejected": "大纲条目自动放弃了",
    "stale": "可以写回旧世界",
}


def transition(current: str, target: str) -> str:
    allowed = TRANSITIONS.get(str(current), ())
    if str(target) not in allowed:
        raise ValueError(f"候选状态不能从 {current} 变为 {target}；合法去向：{'、'.join(allowed) or '（终态）'}")
    return str(target)


def normalize_candidate(row: dict[str, Any]) -> dict[str, Any]:
    basis = _json(row.get("basis"), {})
    return {
        "id": str(row.get("id") or ""),
        "kind": str(row.get("kind") or "world_change"),
        "item_refs": [str(name) for name in _json(row.get("item_refs"), [])],
        "title": str(row.get("title") or ""),
        "summary": str(row.get("summary") or ""),
        #: 依据分三类（§七）：事实 / 因果 / 大纲——缺哪一类都要如实说
        "basis": {
            "fact": str(basis.get("fact") or ""),
            "causality": str(basis.get("causality") or ""),
            "outline": str(basis.get("outline") or ""),
        },
        "audience": str(row.get("audience") or "author"),
        "base_world": int(row.get("base_world") or 0),
        "base_generation": int(row.get("base_generation") or 0),
        #: 世界变化意图（走 change.preview → change.commit；文本候选为空）
        "changes": list(_json(row.get("changes"), [])),
        #: GM 直接变化的原生载荷（走 trpg.gm.change 的联合提交）
        "gm_changes": _json(row.get("gm_changes"), {}),
        "campaign_id": str(row.get("campaign_id") or ""),
        "source_mode": str(row.get("source_mode") or ""),
        "unsolved": [str(name) for name in _json(row.get("unsolved"), [])],
        "text": str(row.get("text") or ""),
        "status": str(row.get("status") or "proposed"),
        "reason": str(row.get("reason") or ""),
        "preview_id": str(row.get("preview_id") or ""),
        "joint_commit_id": str(row.get("joint_commit_id") or ""),
    }


def public_candidate(row: dict[str, Any]) -> dict[str, Any]:
    """给创作者 / GM 看的候选面：带未提交标记，绝不说成已经发生。"""
    item = normalize_candidate(row)
    state = item["status"]
    return {
        **item,
        "uncommitted": state in UNCOMMITTED,
        "must_not_imply": MUST_NOT_IMPLY.get(state, ""),
        "has_world_change": bool(item["changes"] or item["gm_changes"]),
    }


def is_text(row: dict[str, Any]) -> bool:
    return str(row.get("kind") or "") == "text"
