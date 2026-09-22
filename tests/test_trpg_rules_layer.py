"""TRPG 规则层（TRPG_RULES_LAYER_SPEC）：真 WS + 真 SQLite + 真插件子进程。

规则层是应用编排层：三条入口（B0 兼容 / 战役行动 / GM 直接变化）各有固定语义，
主持责任模式（§八）决定谁能替玩家确认行动，场景推进节拍（§4.1 / §九）是声明而不是回合，
「明确无变化」的裁定也要能落账（§七）。

盯的正是这些：§十五 行为验收里属于本层编排的行，加上 §四 / §八 / §九 的最小数据对象与责任边界。
"""

from __future__ import annotations

import json
import sys

import pytest

from conftest import open_mgmt, running_core
from isekai_core.ump import UmpError
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401
from test_trpg_campaign import make_plugin
from test_trpg_rule_common import _commit, _declare, _resolve, _trpg_events

LAYER_PLUGIN_SOURCE = '''
import json, sys

request = json.loads(sys.stdin.readline())
mode = str(request.get("intent") or "ok")
state = request.get("rule_state") or {}
base = int(state.get("state_revision") or 0)
patch = {
    "ruleset_id": state.get("ruleset_id"),
    "base_state_revision": base,
    "operations": [{"path": "/actors/pc-1/edge", "op": "add" if base == 0 else "increase", "value": 1}],
}
consequences = [{
    "kind": "state_change", "operation": "set", "target_refs": ["off-1"], "value": "vacant",
    "expiry": "until_cleared", "certainty": "confirmed",
}]
if mode == "nochange":
    # 明确无变化：只有规则状态动，世界一条后果都没有
    consequences = []
elif mode == "cost":
    # 失败并产生代价：代价走规则状态（edge 消耗），世界只留下持续约束
    consequences = [{
        "kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "失手留下痕迹",
        "expiry": "with_cause", "certainty": "confirmed",
    }]
elif mode == "b0":
    # B0 无状态 resolver 形态：只回世界效果，没有规则状态 / 场景转换（§5.5 / §3.7）
    print(json.dumps({
        "resolution": {"system": "layer-probe", "outcome": mode},
        "effects": [{"kind": "institution_state", "target": "off-1", "value": "vacant",
                     "expiry": "until_cleared", "certainty": "confirmed"}],
        "claims": [],
    }, ensure_ascii=False))
    raise SystemExit(0)
elif mode == "needs_input":
    print(json.dumps({"error": {"code": "needs_input", "message": "缺目标"}}, ensure_ascii=False),
          flush=True)
    raise SystemExit(0)
print(json.dumps({
    "resolution": {"system": "layer-probe", "outcome": mode},
    "rule_state_patch": patch,
    "consequences": consequences,
    "scene_transition": {"status": "advanced"},
    "claims": [],
    "participants": ["pc-1"],
}, ensure_ascii=False))
'''


async def _campaign_with(mgmt, info, timeline_id, plugin, *, host_mode=None, **extra):
    args = {
        "instance_id": info["id"], "timeline_id": timeline_id,
        "ruleset_id": "fake-rules", "ruleset_version": "1.0", "plugin_manifest": plugin,
        "participants": ["card-1"],
        "scene": {"kind": "conflict", "location_refs": ["rl-1"]},
    }
    if host_mode is not None:
        args["host_mode"] = host_mode
    args.update(extra)
    created = await mgmt.call("trpg.campaign.create", **args)
    return str(created["campaign_id"])


@pytest.mark.asyncio
async def test_host_modes_gate_auto_confirmation(tmp_path) -> None:
    """§八：辅助裁定 / 共同主持不替玩家确认；自动主持也只对非关键行动直接确认。"""
    plugin = make_plugin(tmp_path, source=LAYER_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            cases = {"assisted": "awaiting_confirmation", "cohost": "awaiting_confirmation",
                     "autonomous": "confirmed"}
            for mode, expected in cases.items():
                campaign_id = await _campaign_with(mgmt, info, timeline_id, plugin, host_mode=mode)
                declared = await mgmt.call(
                    "trpg.action.declare", instance_id=info["id"], timeline_id=timeline_id,
                    campaign_id=campaign_id, actor_id="card-1", intent="ok", raw_text="ok",
                    auto_confirm=True, target_refs=["off-1"],
                )
                assert declared["status"] == expected, (mode, declared)
                if mode != "autonomous":
                    with pytest.raises(UmpError) as err:
                        await _resolve(mgmt, info, timeline_id, campaign_id, plugin,
                                       declared["action_id"], intent="ok")
                    assert "未确认" in str(err.value), err.value

            # 关键行动（声明里写明需要玩家确认）在任何主持模式下都不自动确认
            autonomous_id = await _campaign_with(mgmt, info, timeline_id, plugin, host_mode="autonomous")
            critical = await mgmt.call(
                "trpg.action.declare", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=autonomous_id, actor_id="card-1", intent="撬锁", raw_text="撬锁",
                auto_confirm=True, require_confirmation=True, target_refs=["off-1"],
            )
            assert critical["status"] == "awaiting_confirmation", critical

            view = await mgmt.call("trpg.scene.view", instance_id=info["id"], timeline_id=timeline_id,
                                   campaign_id=autonomous_id)
            assert view["campaign"]["host_mode"] == "autonomous", "主持模式要可见"
            with pytest.raises(UmpError) as bad:
                await _campaign_with(mgmt, info, timeline_id, plugin, host_mode="host")
            assert "主持模式" in str(bad.value)
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_confirmation_revision_and_stale_version(tmp_path) -> None:
    """§十五：修改增加 action_revision；旧版本不能确认（也就进不了裁定与提交）。"""
    plugin = make_plugin(tmp_path, source=LAYER_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign_with(mgmt, info, timeline_id, plugin)
            declared = await mgmt.call(
                "trpg.action.declare", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id, actor_id="card-1", intent="撬锁", raw_text="撬锁",
                target_refs=["off-1"],
            )
            action_id = str(declared["action_id"])
            with pytest.raises(UmpError) as wrong:
                await mgmt.call(
                    "trpg.action.confirm", instance_id=info["id"], timeline_id=timeline_id,
                    campaign_id=campaign_id, action_id=action_id,
                    action_revision=int(declared["action_revision"]) + 1,
                )
            assert "版本不一致" in str(wrong.value), wrong.value
            first = await mgmt.call(
                "trpg.action.confirm", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id, action_id=action_id,
                action_revision=declared["action_revision"], changes={"intent": "改撬窗户"},
            )
            assert first["action_revision"] == declared["action_revision"] + 1, first
            with pytest.raises(UmpError) as old:
                await mgmt.call(
                    "trpg.action.confirm", instance_id=info["id"], timeline_id=timeline_id,
                    campaign_id=campaign_id, action_id=action_id,
                    action_revision=declared["action_revision"],
                )
            assert "不能确认" in str(old.value), old.value
            row = harness.store.trpg_get("action", instance_id=info["id"], timeline_id=timeline_id,
                                        campaign_id=campaign_id, action_id=action_id)
            assert str(row["intent"]) == "改撬窗户" and str(row["status"]) == "confirmed"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_result_without_world_change_still_commits(tmp_path) -> None:
    """§七：失败可以是「明确无变化」；代价也可以只落在规则状态上——都要能提交落账。"""
    plugin = make_plugin(tmp_path, source=LAYER_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign_with(mgmt, info, timeline_id, plugin, host_mode="autonomous")

            action = await _declare(mgmt, info, timeline_id, campaign_id, intent="nochange")
            await _resolve(mgmt, info, timeline_id, campaign_id, plugin,
                           action["action_id"], intent="nochange")
            result = await _commit(mgmt, info, timeline_id, campaign_id, action["action_id"], key="nochange")
            assert result["status"] == "committed", result
            assert result["effects"] == 0, "世界没有变化，也不能编一个出来"
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 1 and state["opaque_state"]["actors"]["pc-1"]["edge"] == 1, \
                "规则状态照样要落（同批）"
            events = _trpg_events(harness, info, timeline_id)
            assert len(events) == 1 and len(json.loads(events[0]["effects"])) == 0

            # 代价型失败：规则状态与世界约束在同一批里
            cost = await _declare(mgmt, info, timeline_id, campaign_id, intent="cost")
            await _resolve(mgmt, info, timeline_id, campaign_id, plugin, cost["action_id"], intent="cost")
            paid = await _commit(mgmt, info, timeline_id, campaign_id, cost["action_id"], key="cost-1")
            assert paid["status"] == "committed" and paid["effects"] == 1, paid
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id))["state_revision"] == 2
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"], timeline_id=timeline_id,
                                   campaign_id=campaign_id)
            assert [item["status"] for item in view["recent"]] == ["transitioned", "transitioned"]
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_unresolved_and_abandoned_actions_do_not_block_rewriting(tmp_path) -> None:
    """§七：无法裁定不是失败——不推进世界、不消费行动，也不阻止玩家改写 / 另起一个行动。"""
    plugin = make_plugin(tmp_path, source=LAYER_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign_with(mgmt, info, timeline_id, plugin, host_mode="autonomous")

            blocked = await _declare(mgmt, info, timeline_id, campaign_id, intent="needs_input")
            resolved = await _resolve(mgmt, info, timeline_id, campaign_id, plugin,
                                      blocked["action_id"], intent="needs_input")
            assert resolved["status"] == "awaiting_gm_review", resolved
            world_rows = _trpg_events(harness, info, timeline_id)
            assert world_rows == [] and (await mgmt.call(
                "trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id))["state_revision"] == 0

            # 改写：另起一个行动照常走完整链路（campaign 没有被卡住）
            again = await _declare(mgmt, info, timeline_id, campaign_id, intent="ok")
            assert again["status"] == "confirmed"
            await _resolve(mgmt, info, timeline_id, campaign_id, plugin, again["action_id"], intent="ok")
            assert (await _commit(mgmt, info, timeline_id, campaign_id, again["action_id"],
                                  key="again-1"))["status"] == "committed"

            # 玩家放弃：行动终结，世界不受影响
            dropped = await _declare(mgmt, info, timeline_id, campaign_id, intent="ok")
            abandoned = await mgmt.call(
                "trpg.action.abandon", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id, action_id=dropped["action_id"], reason="换条路",
            )
            assert abandoned["status"] == "abandoned"
            assert len(_trpg_events(harness, info, timeline_id)) == 1, "放弃不写世界"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_scene_beat_is_declared_not_enforced(tmp_path) -> None:
    """§4.1 / §九：推进节拍是场景声明（即时 / 连续 / 对抗 / 世界推进），核心不硬套回合。"""
    plugin = make_plugin(tmp_path, source=LAYER_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign_with(mgmt, info, timeline_id, plugin, host_mode="autonomous")
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"], timeline_id=timeline_id,
                                   campaign_id=campaign_id)
            assert view["scene"]["advance_mode"] == "continuous", view["scene"]

            scene = await mgmt.call(
                "trpg.scene.open", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id, kind="conflict", advance_mode="opposed",
                location_refs=["rl-1"],
            )
            assert scene["advance_mode"] == "opposed", scene
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"], timeline_id=timeline_id,
                                   campaign_id=campaign_id)
            assert view["scene"]["advance_mode"] == "opposed"
            with pytest.raises(UmpError) as bad:
                await mgmt.call("trpg.scene.open", instance_id=info["id"], timeline_id=timeline_id,
                                campaign_id=campaign_id, advance_mode="回合制")
            assert "推进节拍" in str(bad.value)
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_commit_closure_rejects_stale_campaign_revision(tmp_path) -> None:
    """§5.6 / §12.1：提交闭包里的战役版本过期 → conflict，不套用旧材料。"""
    plugin = make_plugin(tmp_path, source=LAYER_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign_with(mgmt, info, timeline_id, plugin, host_mode="autonomous")

            first = await _declare(mgmt, info, timeline_id, campaign_id, intent="ok")
            await _resolve(mgmt, info, timeline_id, campaign_id, plugin, first["action_id"], intent="ok")
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"], timeline_id=timeline_id,
                                   campaign_id=campaign_id)
            revision = int(view["campaign"]["state_revision"])
            committed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=first["action_id"], idempotency_key="rev-1", campaign_revision=revision,
            )
            assert committed["status"] == "committed", committed

            second = await _declare(mgmt, info, timeline_id, campaign_id, intent="ok")
            await _resolve(mgmt, info, timeline_id, campaign_id, plugin, second["action_id"], intent="ok")
            stale = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=second["action_id"], idempotency_key="rev-2", campaign_revision=revision,
            )
            assert stale["status"] == "conflict", stale
            assert stale["campaign_revision"] > stale["expected_campaign_revision"]
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id))["state_revision"] == 1, "冲突不落半条"
            assert len(_trpg_events(harness, info, timeline_id)) == 1
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_three_entrances_keep_their_own_semantics(tmp_path) -> None:
    """§5.5：三条入口各走各的——B0 不留战役痕迹、战役行动停在待提交、GM 不造行动行。"""
    plugin = make_plugin(tmp_path, source=LAYER_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime,
                                                     moment=DAY * 1500)
            campaign_id = await _campaign_with(mgmt, info, timeline_id, plugin, host_mode="autonomous")

            seeded = {str(row["id"]) for row in
                      harness.store.event_window(info["id"], timeline_id, until=10**15, limit=500)}

            def all_events() -> list[dict]:
                """实例自带事件之外，这次用例新写进去的（世界本身就是有历史的）。"""
                return [row for row in
                        harness.store.event_window(info["id"], timeline_id, until=10**15, limit=500)
                        if str(row["id"]) not in seeded]

            b0 = await mgmt.call("trpg.action.resolve", instance_id=info["id"], timeline_id=timeline_id,
                                 plugin_manifest=plugin, action_id="b0-1", actor_id="card-1", intent="b0")
            assert b0["accepted"] is True and "campaign" not in json.dumps(b0, ensure_ascii=False)
            assert harness.store.trpg_list("action", instance_id=info["id"], timeline_id=timeline_id) == [], \
                "B0 不产生行动行"
            assert harness.store.trpg_list("rule_state", instance_id=info["id"],
                                           timeline_id=timeline_id) == [], "B0 不产生规则状态"

            action = await _declare(mgmt, info, timeline_id, campaign_id, intent="ok")
            resolved = await _resolve(mgmt, info, timeline_id, campaign_id, plugin,
                                      action["action_id"], intent="ok")
            assert resolved["status"] == "reviewing"
            assert len(all_events()) == 1, "世界只有 B0 那一条"

            gm = await mgmt.call(
                "trpg.gm.change", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                idempotency_key="gm-1",
                changes={"consequences": [{"kind": "state_change", "operation": "set",
                                           "target_refs": ["off-1"], "value": "occupied",
                                           "expiry": "until_cleared", "certainty": "confirmed"}]},
            )
            assert gm["status"] == "committed", gm
            actions = harness.store.trpg_list("action", instance_id=info["id"], timeline_id=timeline_id)
            assert len(actions) == 1, "GM 直接变化不制造行动行"
            assert [str(row["source"]) for row in all_events()] == ["player_action", "gm_declaration"], \
                [row["source"] for row in all_events()]
        finally:
            await mgmt.close()


def test_old_database_gets_the_new_columns(tmp_path) -> None:
    """旧库升级路径：缺列用 ALTER 补，不重建表、不丢已落库的战役（§八 / §4.1）。"""
    import sqlite3

    from isekai_core.store import Store

    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE trpg_campaign(
          instance_id TEXT NOT NULL, timeline_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
          ruleset_id TEXT NOT NULL DEFAULT '', ruleset_version TEXT NOT NULL DEFAULT '',
          plugin_manifest TEXT NOT NULL DEFAULT '', participants TEXT NOT NULL DEFAULT '[]',
          current_scene_id TEXT NOT NULL DEFAULT '', state_revision INTEGER NOT NULL DEFAULT 1,
          status TEXT NOT NULL DEFAULT 'preparing', note TEXT NOT NULL DEFAULT '',
          created_world INTEGER NOT NULL DEFAULT 0, updated_world INTEGER NOT NULL DEFAULT 0,
          created_real REAL NOT NULL DEFAULT 0, updated_real REAL NOT NULL DEFAULT 0,
          PRIMARY KEY(instance_id, timeline_id, campaign_id)
        );
        CREATE TABLE trpg_scene(
          instance_id TEXT NOT NULL, timeline_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
          scene_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'exploration',
          location_refs TEXT NOT NULL DEFAULT '[]', world_snapshot TEXT NOT NULL DEFAULT '{}',
          participants TEXT NOT NULL DEFAULT '[]', public_facts TEXT NOT NULL DEFAULT '[]',
          private_views TEXT NOT NULL DEFAULT '{}', active_risks TEXT NOT NULL DEFAULT '[]',
          available_actions TEXT NOT NULL DEFAULT '[]', turn_state TEXT NOT NULL DEFAULT '{}',
          status TEXT NOT NULL DEFAULT 'open', revision INTEGER NOT NULL DEFAULT 1,
          created_world INTEGER NOT NULL DEFAULT 0, updated_world INTEGER NOT NULL DEFAULT 0,
          PRIMARY KEY(instance_id, timeline_id, campaign_id, scene_id)
        );
        INSERT INTO trpg_campaign(instance_id, timeline_id, campaign_id, ruleset_id, status)
        VALUES('in-old', 'tl-old', 'cp-old', 'fake-rules', 'active');
        """
    )
    conn.commit()
    conn.close()

    store = Store(str(db))
    try:
        store.ensure_schema()
        campaign = store.trpg_get("campaign", instance_id="in-old", timeline_id="tl-old",
                                  campaign_id="cp-old")
        assert campaign is not None, "旧库里的战役不能被重建表弄丢"
        assert campaign["host_mode"] == "assisted", "旧战役按缺省辅助裁定解释"
        store.trpg_upserts({"campaign": [{**campaign, "host_mode": "autonomous"}]})
        assert store.trpg_get("campaign", instance_id="in-old", timeline_id="tl-old",
                              campaign_id="cp-old")["host_mode"] == "autonomous"
        scene_columns = {row[1] for row in store._conn.execute("PRAGMA table_info(trpg_scene)")}
        assert "advance_mode" in scene_columns
    finally:
        store.close()
