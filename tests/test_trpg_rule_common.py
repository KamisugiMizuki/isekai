"""TRPG 规则共用模块（TRPG_RULE_COMMON_MODULE_SPEC）：真 WS + 真 SQLite + 真插件子进程。

公共层是规则私有结果进入世界的唯一规范化边界。这份测试盯的是 §十一 行为验收里属于它的那几行：

- 两个差异化插件（`examples/tide_rules_plugin` 是第二个真实插件）：不共享属性 / 骰点 / 资源模型；
- 规则成功 / 代价 / 失败：已确认部分落成结构化后果，候选只能待审；
- 资源 / 关系等未映射结果：明确 rejected 并给替代路径，不伪装成别的效果；
- 目标 / 效果闭集错误与 claims：不会由说法文本反向创造事实；
- GM 直接变化：与规则行动共用同一套校验，来源仍是 gm_declaration；
- 时间与待选择：时间走独立请求，未选分支只留在场景转换；
- 兼容输入：B0 的 `effects` 与战役的 `consequences` 走同一套确定性 / 受众 / 因果检查。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from conftest import open_mgmt, running_core
from isekai_core.runtime import rule_common
from isekai_core.ump import UmpError
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401
from test_trpg_campaign import make_plugin

ROOT = Path(__file__).resolve().parent.parent
TIDE_DIR = ROOT / "examples" / "tide_rules_plugin"
TIDE_MANIFEST = TIDE_DIR / "manifest.json"


COMMON_PLUGIN_SOURCE = '''
import json, sys

request = json.loads(sys.stdin.readline())
mode = str(request.get("intent") or "ok")
state = request.get("rule_state") or {}
base = int(state.get("state_revision") or 0)
patch = {
    "ruleset_id": state.get("ruleset_id"),
    "base_state_revision": base,
    "operations": [{"path": "/actors/pc-1/hp", "op": "add" if base == 0 else "increase", "value": 1}],
}
consequences = [{
    "kind": "state_change", "operation": "set", "target_refs": ["off-1"], "value": "vacant",
    "expiry": "until_cleared", "certainty": "confirmed",
}]
claims = []
transition = {"status": "advanced"}
if mode == "candidate":
    consequences = [{"kind": "condition", "operation": "create", "target_refs": ["off-1"],
                     "value": "戒备", "expiry": "with_cause", "certainty": "candidate"}]
elif mode == "resource":
    consequences = [{"kind": "resource_change", "operation": "change", "subject_refs": ["pc-1"],
                     "value": 2, "certainty": "confirmed"}]
elif mode == "relation":
    consequences = [{"kind": "relation_change", "operation": "change", "target_refs": ["off-1"],
                     "value": "敌意", "certainty": "confirmed"}]
elif mode == "frame":
    consequences = [{"kind": "world_event", "operation": "create", "value": "钟声在夜里响过",
                     "certainty": "confirmed"}]
elif mode == "choice":
    consequences = [
        {"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "封锁",
         "expiry": "with_cause", "certainty": "confirmed"},
        {"kind": "player_choice", "operation": "create", "choice_id": "ch-1",
         "value": ["追上去", "先撤退"], "audience": "public_party", "certainty": "confirmed"},
        {"kind": "time_advance", "operation": "advance",
         "value": {"seconds": 1800, "cause": "追查去向"}, "certainty": "confirmed"},
    ]
elif mode == "claim_bad":
    consequences = [{"kind": "knowledge_change", "operation": "reveal", "subject_refs": ["pc-1"],
                     "value": {"claim_ref": "cl-404"}, "certainty": "confirmed"}]
elif mode == "claim_ok":
    consequences = [
        {"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "封锁",
         "expiry": "with_cause", "certainty": "confirmed"},
        {"kind": "knowledge_change", "operation": "reveal", "subject_refs": ["pc-1"],
         "value": {"claim_ref": "cl-1"}, "certainty": "confirmed"},
    ]
    claims = [{"id": "cl-1", "text": "岗位的封条换了新的", "source_id": "src-1"}]
elif mode == "audience":
    consequences = [{"kind": "condition", "operation": "create", "target_refs": ["off-1"],
                     "value": "封锁", "visibility": "所有人", "certainty": "confirmed"}]
elif mode in ("b0_ok", "b0_candidate", "b0_patch"):
    consequences = []
answer = {
    "resolution": {"system": "common-probe", "outcome": mode, "private": {"roll": 7}},
    "rule_state_patch": patch,
    "consequences": consequences,
    "scene_transition": transition,
    "claims": claims,
    "participants": ["pc-1"],
}
b0_effect = {"kind": "institution_state", "target": "off-1", "value": "vacant",
             "expiry": "until_cleared", "certainty": "confirmed"}
if mode == "b0_ok":
    answer = {"resolution": {"system": "common-probe", "outcome": mode}, "effects": [b0_effect]}
if mode == "b0_candidate":
    answer = {"resolution": {"system": "common-probe", "outcome": mode},
              "effects": [{**b0_effect, "certainty": "candidate"}]}
if mode == "b0_patch":
    answer = {"resolution": {"system": "common-probe", "outcome": mode},
              "rule_state_patch": patch, "effects": [b0_effect]}
print(json.dumps(answer, ensure_ascii=False), flush=True)
'''


_TIDE_CACHE: dict[str, object] = {}


def _tide_module():
    """进程内加载示例插件：期望值由插件自己的规则函数算出，不把骰子的运气写进断言。"""
    if "module" not in _TIDE_CACHE:
        spec = importlib.util.spec_from_file_location("tide_rules_probe", TIDE_DIR / "tide_rules.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _TIDE_CACHE["module"] = module
    return _TIDE_CACHE["module"]


def _tide_expect(action_id: str, *, intent: str = "推进", actor: str = "card-1", target: str = "off-1"):
    """插件在同一条请求下的裁定（种子 = action_id，所以核心那侧算出来必须是同一个）。"""
    return _tide_module().resolve({
        "type": "resolve_action", "action_id": action_id, "actor_id": actor, "intent": intent,
        "target_refs": [target],
        "rule_state": {"ruleset_id": "tide", "ruleset_version": "0.1.0", "state_revision": 0,
                       "opaque_state": {}},
    })


async def _campaign(mgmt, info, timeline_id, plugin, *, ruleset_id: str = "fake-rules", version: str = "1.0"):
    created = await mgmt.call(
        "trpg.campaign.create",
        instance_id=info["id"], timeline_id=timeline_id,
        ruleset_id=ruleset_id, ruleset_version=version, plugin_manifest=plugin,
        participants=["card-1"], host_mode="autonomous",
        scene={"kind": "conflict", "location_refs": ["rl-1"]},
    )
    assert created["status"] == "active" and created["scene_id"]
    return str(created["campaign_id"])


async def _declare(mgmt, info, timeline_id, campaign_id, *, intent: str, actor: str = "card-1") -> dict:
    """默认行动者 `card-1` 只用于「后果不指向行动者」的用例；指向行动者的用例要传真卡标识。"""
    declared = await mgmt.call(
        "trpg.action.declare",
        instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
        actor_id=actor, intent=intent, raw_text=intent, auto_confirm=True, target_refs=["off-1"],
    )
    assert declared["status"] == "confirmed"
    return declared


async def _resolve(mgmt, info, timeline_id, campaign_id, plugin, action_id, *, intent: str, actor: str = "card-1"):
    return await mgmt.call(
        "trpg.action.resolve",
        instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
        plugin_manifest=plugin, action_id=action_id, actor_id=actor, intent=intent, timeout=60.0,
    )


async def _commit(mgmt, info, timeline_id, campaign_id, action_id, *, key: str = "k-1"):
    return await mgmt.call(
        "trpg.commit",
        instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
        action_id=action_id, idempotency_key=key,
    )


def _trpg_events(harness, info, timeline_id) -> list[dict]:
    return [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15, limit=500)
            if str(row["source"]).startswith("trpg") or row["source"] == "gm_declaration"]


@pytest.mark.asyncio
async def test_two_differentiated_plugins_share_only_the_boundary(tmp_path) -> None:
    """§九：第二个真实插件（潮汐骰池）不需要 WorldRuntime 理解它的骰池、压力或规则状态。"""
    fake = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, card_id = make_instance(harness.store, harness.runtime.service.runtime,
                                                      moment=DAY * 1500)

            # 第一套规则：无状态 resolver 形态的假插件
            fake_campaign = await _campaign(mgmt, info, timeline_id, fake)
            fake_action = await _declare(mgmt, info, timeline_id, fake_campaign, intent="ok")
            await _resolve(mgmt, info, timeline_id, fake_campaign, fake, fake_action["action_id"], intent="ok")
            await _commit(mgmt, info, timeline_id, fake_campaign, fake_action["action_id"], key="fake-1")

            # 第二套规则：潮汐骰池（真实示例插件，骰池 / 压力 / 际遇，与前一套不共享模型）
            await mgmt.call("runtime.activate", instance_id=info["id"], timeline_id=timeline_id)
            tide_campaign = await _campaign(
                mgmt, info, timeline_id, str(TIDE_MANIFEST), ruleset_id="tide", version="0.1.0"
            )
            action = await _declare(mgmt, info, timeline_id, tide_campaign, intent="推进", actor=card_id)
            expected = _tide_expect(action["action_id"], actor=card_id)
            resolved = await _resolve(mgmt, info, timeline_id, tide_campaign, str(TIDE_MANIFEST),
                                      action["action_id"], intent="推进", actor=card_id)
            assert resolved["status"] == "reviewing", resolved
            assert resolved["resolution"]["pool"]["rolls"] == expected["resolution"]["pool"]["rolls"]
            assert resolved["resolution"]["stress_after"] == expected["resolution"]["stress_after"]
            committed = await _commit(mgmt, info, timeline_id, tide_campaign, action["action_id"], key="tide-1")
            assert committed["status"] == "committed", committed

            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=tide_campaign)
            actor_state = state["opaque_state"]["actors"][card_id]
            assert actor_state["stress"] == expected["resolution"]["pool"]["banes"]
            assert actor_state["pool"] == max(1, expected["resolution"]["pool"]["size"]
                                              - expected["resolution"]["pool"]["banes"])
            fake_state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                         campaign_id=fake_campaign)
            assert set(fake_state["opaque_state"]["actors"]["pc-1"]) == {"hp"}
            assert "stress" not in json.dumps(fake_state["opaque_state"]), "两套规则不共享规则状态模型"

            # 已确认的后果落成世界事实；未选分支与世界时间留在这里不动（下面单独验）
            effects = [json.loads(row["effects"]) for row in _trpg_events(harness, info, timeline_id)
                       if str(row["source"]) == "trpg_action"]
            kinds = [str(item["kind"]) for batch in effects for item in batch]
            assert "activity_constraint" in kinds, effects
            # 规则节拍留在场景的 turn_state 里，不冒充世界秒（§九：规则时间与世界时间不一致不强行覆盖）
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"], timeline_id=timeline_id,
                                   campaign_id=tide_campaign)
            assert view["scene"]["turn_state"]["rule_time"] == 1, view["scene"]["turn_state"]
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_unconfirmed_and_unmapped_outcomes_never_reach_the_world(tmp_path) -> None:
    """候选只能待审；资源 / 关系在首版闭集里没有映射 → rejected 并给替代路径（§5.1 / §5.2）。"""
    plugin = make_plugin(tmp_path, source=COMMON_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            cases = {
                "candidate": ("needs_review", "certainty=candidate"),
                "resource": ("rejected", "首版事实效果闭集里没有资源量"),
                "relation": ("rejected", "关系变化没有对应的事实效果"),
            }
            for mode, (status, needle) in cases.items():
                action = await _declare(mgmt, info, timeline_id, campaign_id, intent=mode)
                resolved = await _resolve(mgmt, info, timeline_id, campaign_id, plugin,
                                          action["action_id"], intent=mode)
                assert resolved["status"] == status, (mode, resolved)
                reasons = json.dumps(resolved["pending"] if status == "needs_review" else resolved["rejected"],
                                     ensure_ascii=False)
                assert needle in reasons, (mode, reasons)
                # 公共层判死 / 待审 → 规则状态与世界都没动过
                state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                        timeline_id=timeline_id, campaign_id=campaign_id)
                assert int(state["state_revision"]) == 0, mode
                assert _trpg_events(harness, info, timeline_id) == [], mode
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_b0_compat_path_goes_through_the_same_checks(tmp_path) -> None:
    """§3.7：B0 的 `effects` 兼容输入过同一套检查，且不接受战役专属材料。"""
    plugin = make_plugin(tmp_path, source=COMMON_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            ok = await mgmt.call(
                "trpg.action.resolve", instance_id=info["id"], timeline_id=timeline_id,
                plugin_manifest=plugin, action_id="b0-ok", actor_id="card-1", intent="b0_ok",
            )
            assert ok["accepted"] is True and ok["resolution"]["system"] == "common-probe"

            with pytest.raises(UmpError) as bad_candidate:
                await mgmt.call(
                    "trpg.action.resolve", instance_id=info["id"], timeline_id=timeline_id,
                    plugin_manifest=plugin, action_id="b0-candidate", actor_id="card-1",
                    intent="b0_candidate",
                )
            assert "certainty=candidate" in str(bad_candidate.value)

            with pytest.raises(UmpError) as bad_patch:
                await mgmt.call(
                    "trpg.action.resolve", instance_id=info["id"], timeline_id=timeline_id,
                    plugin_manifest=plugin, action_id="b0-patch", actor_id="card-1", intent="b0_patch",
                )
            assert "B0" in str(bad_patch.value) and "rule_state_patch" in str(bad_patch.value)

            assert harness.store.trpg_list("rule_state", instance_id=info["id"],
                                            timeline_id=timeline_id) == [], "B0 路径不许落规则状态"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_event_frame_alone_is_not_a_world_fact(tmp_path) -> None:
    """§5.1：事件帧是叙述材料——进事件路径，但正文不能代替结构化效果（零效果提交）。"""
    plugin = make_plugin(tmp_path, source=COMMON_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action = await _declare(mgmt, info, timeline_id, campaign_id, intent="frame")
            resolved = await _resolve(mgmt, info, timeline_id, campaign_id, plugin,
                                      action["action_id"], intent="frame")
            assert resolved["status"] == "reviewing"
            assert [item["kind"] for item in resolved["changes"]] == ["world_event"]
            assert resolved["consequences"] == [], "事件帧不产生事实效果"
            committed = await _commit(mgmt, info, timeline_id, campaign_id, action["action_id"], key="frame-1")
            assert committed["status"] == "committed", committed
            assert committed["effects"] == 0, "帧不产生效果"
            rows = _trpg_events(harness, info, timeline_id)
            assert len(rows) == 1 and "钟声在夜里响过" in str(rows[0]["summary"]), \
                "事件帧要进事件正文（叙述材料），但不能造效果"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_time_and_pending_choice_stay_out_of_world_facts(tmp_path) -> None:
    """§5.1：时间走独立请求、未选分支只进场景转换；两者都不写成世界事实。"""
    plugin = make_plugin(tmp_path, source=COMMON_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            await mgmt.call("runtime.activate", instance_id=info["id"], timeline_id=timeline_id)
            before = int((await mgmt.call("runtime.clock", instance_id=info["id"],
                                          timeline_id=timeline_id))["clock"]["processed_world"])
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action = await _declare(mgmt, info, timeline_id, campaign_id, intent="choice")
            resolved = await _resolve(mgmt, info, timeline_id, campaign_id, plugin,
                                      action["action_id"], intent="choice")
            assert resolved["status"] == "reviewing", resolved
            assert resolved["world_time_request"]["seconds"] == 1800
            assert any("player_choice" in line for line in resolved["warnings"]), resolved["warnings"]

            committed = await _commit(mgmt, info, timeline_id, campaign_id, action["action_id"], key="choice-1")
            assert committed["status"] == "committed", committed
            assert committed["open_choices"] == ["ch-1"]
            assert committed["world_time_applied"] is True
            assert committed["world_time_settled"]["processed_world"] == before + 1800
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"], timeline_id=timeline_id,
                                   campaign_id=campaign_id)
            assert [item["choice_id"] for item in view["pending_choices"]] == ["ch-1"]
            rows = _trpg_events(harness, info, timeline_id)
            assert not any("追上去" in json.dumps(row, ensure_ascii=False) for row in rows), \
                "未选分支不能出现在世界事实里"
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id)
            assert int(state["state_revision"]) == 1, "时间请求与规则状态同批"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_claim_reference_must_resolve_and_claims_keep_their_source(tmp_path) -> None:
    """§3.9：说法保留来源渠道，不能用没声明的说法，也不能由文本反向造事实。"""
    plugin = make_plugin(tmp_path, source=COMMON_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            bad = await _declare(mgmt, info, timeline_id, campaign_id, intent="claim_bad")
            refused = await _resolve(mgmt, info, timeline_id, campaign_id, plugin,
                                     bad["action_id"], intent="claim_bad")
            assert refused["status"] == "needs_review", refused
            assert "cl-404" in json.dumps(refused["pending"], ensure_ascii=False)
            baseline = harness.store.claim_list(info["id"], timeline_id)
            assert baseline, "实例自带的世界说法不该被这条用例挡住"
            assert not any("封条" in str(row["text"]) for row in baseline)

            good = await _declare(mgmt, info, timeline_id, campaign_id, intent="claim_ok")
            resolved = await _resolve(mgmt, info, timeline_id, campaign_id, plugin,
                                      good["action_id"], intent="claim_ok")
            assert resolved["status"] == "reviewing", resolved
            assert await _commit(mgmt, info, timeline_id, campaign_id, good["action_id"], key="claim-1")
            claims = [row for row in harness.store.claim_list(info["id"], timeline_id)
                      if row["text"] == "岗位的封条换了新的"]
            assert len(claims) == 1 and claims[0]["source_id"] == "src-1"
            effects = [json.loads(row["effects"]) for row in _trpg_events(harness, info, timeline_id)]
            kinds = [str(item["kind"]) for batch in effects for item in batch]
            assert kinds == ["activity_constraint"], "说法不进效果：文本没有变成事实"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_free_string_visibility_is_not_public(tmp_path) -> None:
    """§3.8：受众是闭集，自由字符串不当作「公开」。"""
    plugin = make_plugin(tmp_path, source=COMMON_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action = await _declare(mgmt, info, timeline_id, campaign_id, intent="audience")
            resolved = await _resolve(mgmt, info, timeline_id, campaign_id, plugin,
                                      action["action_id"], intent="audience")
            assert resolved["status"] == "rejected", resolved
            assert any("visibility" in item for item in resolved["errors"]), resolved["errors"]
            assert _trpg_events(harness, info, timeline_id) == []
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_gm_direct_change_uses_the_same_boundary(tmp_path) -> None:
    """§七：GM 直接变化不走骰点、不造行动行，但过同一套后果检查与联合提交。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)

            pending = {"consequences": [{"kind": "condition", "operation": "create",
                                         "target_refs": ["off-1"], "value": "戒备",
                                         "expiry": "with_cause", "certainty": "candidate"}]}
            reviewed = await mgmt.call("trpg.gm.change", instance_id=info["id"], timeline_id=timeline_id,
                                       campaign_id=campaign_id, changes=pending, idempotency_key="gm-cand")
            assert reviewed["status"] == "needs_review" and reviewed["action_id"] == ""
            assert _trpg_events(harness, info, timeline_id) == []

            unmapped = {"consequences": [{"kind": "relation_change", "operation": "change",
                                          "target_refs": ["off-1"], "value": "敌意",
                                          "certainty": "confirmed"}]}
            refused = await mgmt.call("trpg.gm.change", instance_id=info["id"], timeline_id=timeline_id,
                                      campaign_id=campaign_id, changes=unmapped, idempotency_key="gm-rel")
            assert refused["status"] == "rejected", refused

            branching = {"consequences": [
                {"kind": "state_change", "operation": "set", "target_refs": ["off-1"], "value": "vacant",
                 "expiry": "until_cleared", "certainty": "confirmed"},
                {"kind": "player_choice", "operation": "create", "choice_id": "gm-ch-1",
                 "value": ["把门堵上", "留一条缝"], "audience": "public_party", "certainty": "confirmed"},
            ]}
            committed = await mgmt.call("trpg.gm.change", instance_id=info["id"], timeline_id=timeline_id,
                                        campaign_id=campaign_id, changes=branching, idempotency_key="gm-ok")
            assert committed["status"] == "committed", committed
            assert committed["open_choices"] == ["gm-ch-1"]
            assert harness.store.trpg_list("action", instance_id=info["id"], timeline_id=timeline_id) == [], \
                "GM 直接变化不制造行动行"
            events = [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                      if row["source"] == "gm_declaration"]
            assert len(events) == 1
            assert not any("把门堵上" in json.dumps(row, ensure_ascii=False) for row in events)
        finally:
            await mgmt.close()


def test_normalized_result_keeps_host_origin_and_private_resolution() -> None:
    """§3.6：规范化结果的字段就这些；来源与版本由宿主给，插件自报不算数。"""
    origin = rule_common.origin_block(
        "ins-1", "tl-1", campaign_id="cp-1", action_ref="act-1", source_mode="action",
        source_plugin="tide", expected_revision=42,
    )
    plugin_result = {
        "resolution": {"system": "tide", "private": {"rolls": [6, 1, 5]}},
        "consequences": [{
            "id": "c-1", "kind": "condition", "operation": "create", "target_refs": ["off-1"],
            "value": "封锁", "expiry": "with_cause", "certainty": "confirmed", "cause_refs": ["act-1"],
        }],
        "claims": [{"id": "cl-1", "text": "封条换了新的", "source_id": "src-1"}],
        # 插件自报的来源 / 版本 / 版次：不该被采信（§3.6 末段）
        "source_mode": "world_process", "campaign_id": "cp-evil", "expected_revision": 999,
    }
    normalized = rule_common.normalize(
        plugin_result, origin=origin,
        package={"world": {"institutions": []}, "environment": {"types": []}},
    )
    assert normalized["status"] == "ready", normalized["errors"]
    assert normalized["origin"]["campaign_id"] == "cp-1"
    assert normalized["origin"]["source_mode"] == "action"
    assert normalized["origin"]["expected_revision"] == 42
    assert normalized["raw_resolution"] == plugin_result["resolution"], "规则私有裁定原样保留"
    assert set(normalized) >= {
        "status", "origin", "raw_resolution", "rule_state_patch", "changes", "claims",
        "scene_transition", "world_time_request", "errors", "warnings",
    }
    change = normalized["changes"][0]
    assert set(change) >= {
        "id", "kind", "target_refs", "operation", "value", "certainty", "visibility",
        "effective_time", "cause_refs", "source_mode", "source_module",
    }
    assert change["source_mode"] == "trpg_rule" and change["source_module"] == "trpg"
    assert change["effective_time"] == 42, "生效时刻缺省 = 宿主这次的基准水位"
    assert change["visibility"] == ["public_party"], "没声明受众就按宿主这次提交的受众"
    assert [item["kind"] for item in normalized["payload"]["effects"]] == ["activity_constraint"]
    assert normalized["payload"]["claims"][0]["source_id"] == "src-1"

def test_tide_plugin_three_outcomes_all_reach_the_boundary() -> None:
    """§十一「规则成功 / 失败 / 代价」：真插件的三种结果各自都能过公共层（结果形态不同）。"""
    from isekai_core.world.example import example_package

    module = _tide_module()
    seen: dict[str, dict] = {}
    for index in range(400):
        request = {
            "type": "resolve_action", "action_id": f"act-{index}", "actor_id": "cc-探针",
            "intent": "推进", "target_refs": ["off-1"],
            "rule_state": {"ruleset_id": "tide", "state_revision": 0, "opaque_state": {}},
        }
        result = module.resolve(request)
        seen.setdefault(str(result["resolution"]["outcome"]), result)
    assert set(seen) == {"成功", "代价成功", "失败"}, sorted(seen)

    package = example_package("探针", moment=DAY * 1500)
    for outcome, result in sorted(seen.items()):
        normalized = rule_common.normalize(
            result,
            origin=rule_common.origin_block("in-1", "tl-1", action_ref="act-1", source_mode="action"),
            package=package,
        )
        assert normalized["status"] == "ready", (outcome, normalized["errors"], normalized["pending"])
        kinds = [item["kind"] for item in normalized["changes"]]
        assert kinds, outcome
        if outcome == "失败":
            assert [item for item in normalized["payload"]["effects"]] and normalized["world_time_request"], \
                "失败也要有结构化后果与世界时间消耗，不能是「没有变化」"
        else:
            assert normalized["claims"], "成功 / 代价成功带说法材料"
