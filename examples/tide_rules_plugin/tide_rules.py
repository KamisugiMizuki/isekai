#!/usr/bin/env python3
"""潮汐骰池（示例规则）：成功数制的第二套规则插件。

它刻意和「行于泰拉」示例不共享任何模型：没有属性 + 技能 + 骰点步进，只有骰池、
压力与际遇；规则状态不是 hp / sp，世界后果按**变化意图类别**申报，不是世界效果名。
存在它的意义是当公共层的最低验证门槛（TRPG_RULE_COMMON_MODULE_SPEC §九）：证明
公共层不要求两套规则共享属性、骰点或资源模型。

线协议：一行一个 JSON（TRPG_RULE_PLUGIN_SPEC）。状态只能经快照进出——进程内存不算数，
所以每次裁定都从请求里的 `rule_state.opaque_state` 重读，不靠上次调用留下的东西。
"""

from __future__ import annotations

import hashlib
import json
import random
import sys
from typing import Any

FACES = 6
SUCCESS_FACE = 5          # 5–6 算成功
BANE_FACE = 1             # 1 是代价
BASE_POOL = 3
DEFAULT_CHANNEL = "src-1"  # 说法渠道缺省值（实例里登记过的渠道）
STAKES_SECONDS = 900      # 一次对峙拖掉的世界时间


def _int(value: Any, name: str, *, minimum: int = 0) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须是整数") from exc
    if result < minimum:
        raise ValueError(f"{name} 不能小于 {minimum}")
    return result


def _seed(request: dict[str, Any]) -> int:
    raw = str(request.get("seed") or request.get("action_id") or "tide")
    return int.from_bytes(hashlib.sha256(raw.encode("utf-8")).digest()[:8], "big")


def _actor(request: dict[str, Any], opaque: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    actor = str(request.get("actor_id") or "")
    actors = opaque.get("actors") if isinstance(opaque.get("actors"), dict) else {}
    record = actors.get(actor) if isinstance(actors.get(actor), dict) else {}
    return actor, record


def _roll(request: dict[str, Any], opaque: dict[str, Any]) -> dict[str, Any]:
    """骰池：一次声明掷一把，5–6 算成功、1 算代价；种子固定所以重放一致。"""
    actor, record = _actor(request, opaque)
    context = request.get("context") if isinstance(request.get("context"), dict) else {}
    size = _int(record.get("pool", BASE_POOL), "pool", minimum=1)
    size += _int(context.get("bonus", 0), "bonus") - _int(context.get("penalty", 0), "penalty")
    size = max(1, size)
    rng = random.Random(_seed(request))
    rolls = [rng.randint(1, FACES) for _ in range(size)]
    successes = sum(1 for face in rolls if face >= SUCCESS_FACE)
    banes = sum(1 for face in rolls if face == BANE_FACE)
    if not successes:
        outcome = "失败"
    elif banes:
        outcome = "代价成功"
    else:
        outcome = "成功"
    return {
        "actor": actor,
        "size": size,
        "rolls": rolls,
        "successes": successes,
        "banes": banes,
        "outcome": outcome,
        "stress": _int(record.get("stress", 0), "stress"),
    }


def _target(request: dict[str, Any], actor: str) -> str:
    refs = [str(item) for item in request.get("target_refs") or [] if str(item or "").strip()]
    return refs[0] if refs else actor


def _channel(request: dict[str, Any], opaque: dict[str, Any]) -> str:
    context = request.get("context") if isinstance(request.get("context"), dict) else {}
    return str(context.get("channel") or opaque.get("channel") or DEFAULT_CHANNEL)


def _consequences(
    request: dict[str, Any], roll: dict[str, Any], opaque: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """把这次裁定翻成变化意图类别（§5.1）＋ 说法 ＋ 场景转换；插件不自造世界效果名。"""
    actor = str(roll["actor"])
    target = _target(request, actor)
    channel = _channel(request, opaque)
    intent = str(request.get("intent") or "")
    consequences: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    transition: dict[str, Any] = {
        "status": "advanced",
        "next_phase": "对峙" if roll["banes"] else "推进",
        # 规则节拍：一次裁定走一个规则时间单位，**不动世界秒**（只有显式 time_advance 才动）
        "rule_time_delta": 1,
    }

    if roll["outcome"] == "失败":
        # 失败不是「没有变化」：被压制是持续约束，还留一个待选分支与一段世界时间
        consequences.append(
            {
                "id": "tide-pressure",
                "kind": "condition",
                "operation": "create",
                "subject_refs": [actor],
                "target_refs": [actor],
                "value": "被压制",
                "expiry": "with_cause",
                "certainty": "confirmed",
                "cause_refs": [str(request.get("action_id") or "")],
            }
        )
        consequences.append(
            {
                "id": "tide-stakes",
                "kind": "time_advance",
                "operation": "advance",
                "value": {"seconds": STAKES_SECONDS, "cause": "潮线对峙僵持"},
                "certainty": "confirmed",
            }
        )
        consequences.append(
            {
                "id": "tide-branch",
                "kind": "player_choice",
                "operation": "create",
                "value": ["硬撑一把", "顺势退开"],
                "audience": "public_party",
                "certainty": "confirmed",
            }
        )
    else:
        consequences.append(
            {
                "id": "tide-edge",
                "kind": "condition",
                "operation": "create",
                "subject_refs": [actor],
                "target_refs": [target],
                "value": "占着上风" if roll["outcome"] == "成功" else "代价换来的进展",
                "expiry": "with_cause",
                "certainty": "confirmed",
                "cause_refs": [str(request.get("action_id") or "")],
            }
        )
        claims.append(
            {
                "id": "tide-notice",
                "text": f"潮线记下：{actor} 在 {target} {roll['outcome']}",
                "source_id": channel,
            }
        )
        consequences.append(
            {
                "id": "tide-notice-change",
                "kind": "knowledge_change",
                "operation": "reveal",
                "subject_refs": [actor],
                "target_refs": [target],
                "value": {"claim_ref": "tide-notice"},
                "visibility": "public_party",
                "certainty": "confirmed",
                "cause_refs": [str(request.get("action_id") or "")],
            }
        )

    if "耗尽" in intent:
        # 资源量在首版事实效果闭集里没有对应项：如实申报，让公共层拒绝并给替代路径（§5.2）
        consequences.append(
            {
                "id": "tide-drain",
                "kind": "resource_change",
                "operation": "change",
                "subject_refs": [actor],
                "value": 2,
                "certainty": "confirmed",
            }
        )
    if "也许" in intent:
        # 还只是候选：公共层只能把它放进取景 / 待审，不能当事实（§3.8）
        consequences.append(
            {
                "id": "tide-maybe",
                "kind": "location_change",
                "operation": "change",
                "target_refs": [target],
                "value": "也许被堵住了",
                "certainty": "candidate",
            }
        )
    return consequences, claims, transition


def resolve(request: dict[str, Any]) -> dict[str, Any]:
    if request.get("type") != "resolve_action":
        raise ValueError("只支持 type=resolve_action")
    state = request.get("rule_state") if isinstance(request.get("rule_state"), dict) else {}
    opaque = state.get("opaque_state") if isinstance(state.get("opaque_state"), dict) else {}
    actor, record = _actor(request, opaque)
    if not actor:
        return {"error": {"code": "needs_input", "message": "潮汐裁定需要行动者"}}
    roll = _roll(request, opaque)
    consequences, claims, transition = _consequences(request, roll, opaque)
    stress = roll["stress"] + roll["banes"]
    patch = {
        "ruleset_id": state.get("ruleset_id"),
        "base_state_revision": int(state.get("state_revision") or 0),
        "operations": [
            {
                "path": f"/actors/{actor}/stress",
                # 已经有这个键就用增减（路径写错不会被静默当 0），首次落值才用 add
                "op": "increase" if "stress" in record else "add",
                "value": roll["banes"],
            },
            {"path": f"/actors/{actor}/pool", "op": "add", "value": max(1, roll["size"] - roll["banes"])},
        ],
    }
    return {
        "resolution": {
            "system": "tide",
            "version": "0.1.0",
            "outcome": roll["outcome"],
            "pool": {"size": roll["size"], "faces": FACES, "rolls": roll["rolls"],
                     "successes": roll["successes"], "banes": roll["banes"]},
            "stress_after": stress,
            "seed": str(request.get("action_id") or ""),
        },
        "rule_state_patch": patch,
        "consequences": consequences,
        "scene_transition": transition,
        "claims": claims,
        "participants": [actor],
    }


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            print(json.dumps(resolve(json.loads(line)), ensure_ascii=False), flush=True)
        except (ValueError, json.JSONDecodeError) as exc:
            print(json.dumps({"error": {"code": "rejected", "message": str(exc)}}, ensure_ascii=False),
                  flush=True)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
