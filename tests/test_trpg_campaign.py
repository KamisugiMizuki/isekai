"""TRPG 战役运行时（TRPG_CAMPAIGN_RUNTIME_SPEC）：真 WS + 真 SQLite + 真插件子进程。

覆盖设计里的硬约束：未确认不裁定、规则状态与世界后果同批成功或同批失败、
幂等重放返回原结果、版本冲突不落半条、回滚恢复规则状态、插件崩溃不猜结果、
待选择不进世界事实。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from conftest import open_mgmt, running_core
from isekai_core.ump import UmpError
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401

PLUGIN_SOURCE = '''
import json, sys

request = json.loads(sys.stdin.readline())
mode = str(request.get("intent") or "ok")
state = request.get("rule_state") or {}
base = int(state.get("state_revision") or 0)
if mode == "crash":
    sys.exit(1)
patch = {
    "ruleset_id": state.get("ruleset_id"),
    "base_state_revision": base,
    # 首次落值用 add，之后用 increase：数值增减要求目标已存在（避免路径写错被静默当 0）
    "operations": [{
        "path": "/actors/pc-1/hp",
        "op": "add" if base == 0 else "increase",
        "value": 2,
    }],
}
if mode == "bad_base":
    patch["base_state_revision"] = base + 5
consequences = [{
    "kind": "institution_state", "target": "off-1", "value": "vacant",
    "expiry": "until_cleared", "certainty": "confirmed",
}]
if mode == "bad_world":
    consequences = [{
        "kind": "institution_state", "target": "off-999", "value": "vacant",
        "expiry": "until_cleared", "certainty": "confirmed",
    }]
transition = {"status": "advanced"}
if mode == "waiting":
    transition = {
        "status": "waiting_choice",
        "available_choices": [{"choice_id": "ch-1", "options": ["追上去", "先撤退"]}],
    }
print(json.dumps({
    "resolution": {"system": "fake-rules", "outcome": "success", "mode": mode},
    "rule_state_patch": patch,
    "consequences": consequences,
    "scene_transition": transition,
    "claims": [{"text": "职位出现变动", "source_id": "src-1", "audience": "public"}],
    "participants": ["pc-1"],
}, ensure_ascii=False))
'''


def make_plugin(tmp_path) -> str:
    folder = tmp_path / "rules"
    folder.mkdir(exist_ok=True)
    (folder / "main.py").write_text(PLUGIN_SOURCE, encoding="utf-8")
    manifest = folder / "manifest.json"
    manifest.write_text(
        json.dumps({
            "id": "fake-rules", "name": "fake", "version": "1.0",
            "protocol": "isekai.trpg.rules/1", "entry": [sys.executable, "main.py"],
        }),
        encoding="utf-8",
    )
    return str(manifest)


async def _campaign(mgmt, info, timeline_id, plugin, **overrides) -> str:
    created = await mgmt.call(
        "trpg.campaign.create",
        instance_id=info["id"], timeline_id=timeline_id,
        ruleset_id="fake-rules", ruleset_version="1.0", plugin_manifest=plugin,
        participants=["card-1"],
        scene={"kind": "conflict", "location_refs": ["rl-1"], "participants": ["card-1"]},
        **overrides,
    )
    assert created["status"] == "active"
    assert created["scene_id"], "开局面场景要落在战役上"
    return str(created["campaign_id"])


async def _run_action(mgmt, info, timeline_id, campaign_id, *, plugin, intent="ok", auto=True):
    declared = await mgmt.call(
        "trpg.action.declare",
        instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
        actor_id="card-1", intent=intent, raw_text=intent, auto_confirm=auto,
        target_refs=["off-1"],
    )
    action_id = str(declared["action_id"])
    if not auto:
        confirmed = await mgmt.call(
            "trpg.action.confirm",
            instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
            action_id=action_id, action_revision=declared["action_revision"],
        )
        assert confirmed["status"] == "confirmed"
    resolved = await mgmt.call(
        "trpg.action.resolve", timeout=60.0,
        instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
        plugin_manifest=plugin, action_id=action_id, actor_id="card-1", intent=intent,
    )
    return action_id, resolved


@pytest.mark.asyncio
async def test_commits_rule_state_and_world_consequence_together(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, resolved = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="ok")
            assert resolved["status"] == "reviewing", "裁定结果先停在待提交，不写世界"
            assert resolved["rule_state_patch"]["base_state_revision"] == 0

            # 裁定阶段不落世界、不落规则状态：提交前世界事件为零
            assert [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                    if row["source"] == "trpg_action"] == []
            assert (await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id))["state_revision"] == 0

            committed = await mgmt.call(
                "trpg.commit",
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="k-1",
            )
            assert committed["status"] == "committed", committed
            assert committed["state_revisions"] == {"fake-rules": 1}

            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["opaque_state"] == {"actors": {"pc-1": {"hp": 2}}}, "规则状态按 patch 落盘"
            events = [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                      if row["source"] == "trpg_action"]
            assert len(events) == 1 and json.loads(events[0]["effects"])[0]["target"] == "off-1"
            assert harness.store.institution_list(info["id"], timeline_id), "制度效果落成真状态"

            view = await mgmt.call("trpg.scene.view", instance_id=info["id"],
                                   timeline_id=timeline_id, campaign_id=campaign_id)
            assert view["actions"] == [], "已提交的行动不再挂在可行动局面上"
            assert [item["status"] for item in view["recent"]] == ["transitioned"]
            assert view["rule_state"]["state_revision"] == 1
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_unconfirmed_action_never_reaches_resolver(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            declared = await mgmt.call(
                "trpg.action.declare",
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                actor_id="card-1", intent="ok", raw_text="ok",
            )
            assert declared["status"] == "awaiting_confirmation"
            with pytest.raises(UmpError):
                await mgmt.call(
                    "trpg.action.resolve", timeout=60.0,
                    instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                    plugin_manifest=plugin, action_id=declared["action_id"],
                    actor_id="card-1", intent="ok",
                )
            assert [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                    if row["source"] == "trpg_action"] == []
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_invalid_world_consequence_writes_nothing(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _resolved = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="bad_world")
            result = await mgmt.call(
                "trpg.commit",
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="k-bad",
            )
            assert result["status"] == "needs_review"
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 0, "世界后果非法则规则状态也不落（同批）"
            assert [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                    if row["source"] == "trpg_action"] == []
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_stale_base_revision_conflicts_without_writes(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            first, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="ok")
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=first, idempotency_key="k-a")
            second, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="bad_base")
            result = await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                                     campaign_id=campaign_id, action_id=second, idempotency_key="k-b")
            assert result["status"] == "conflict"
            # 插件在「当前 revision + 5」上做版本，与真实 revision 不同 → 冲突
            assert result["rule_state_revision"] == 1 and result["requested_base"] == 6
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 1, "冲突不推进规则状态"
            events = [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                      if row["source"] == "trpg_action"]
            assert len(events) == 1, "冲突不落第二条世界事件"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_duplicate_idempotency_key_replays_original(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="ok")
            first = await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id, action_id=action_id, idempotency_key="k-dup")
            again = await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id, action_id=action_id, idempotency_key="k-dup")
            assert again["status"] == "duplicate"
            assert again["joint_commit_id"] == first["joint_commit_id"]
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 1, "重放不重复扣资源"
            events = [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15)
                      if row["source"] == "trpg_action"]
            assert len(events) == 1
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_pending_choice_is_not_a_world_fact(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="waiting")
            committed = await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                                        campaign_id=campaign_id, action_id=action_id, idempotency_key="k-w")
            assert committed["open_choices"] == ["ch-1"]
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"],
                                   timeline_id=timeline_id, campaign_id=campaign_id)
            assert len(view["pending_choices"]) == 1
            assert view["scene"]["status"] == "waiting_choice"
            # 未选择的分支不能出现在世界事实里（只落了一条已提交的行动事件）
            assert not any("追上去" in str(row["detail"]) for row in
                           harness.store.event_window(info["id"], timeline_id, until=10**15))
            picked = await mgmt.call("trpg.choice.select", instance_id=info["id"], timeline_id=timeline_id,
                                     campaign_id=campaign_id, choice_id="ch-1", selection="追上去")
            assert picked["status"] == "selected" and picked["duplicate"] is False
            again = await mgmt.call("trpg.choice.select", instance_id=info["id"], timeline_id=timeline_id,
                                    campaign_id=campaign_id, choice_id="ch-1", selection="追上去")
            assert again["duplicate"] is True, "已选择的待选择重放原结果"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_plugin_crash_marks_failed_and_guesses_nothing(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            declared = await mgmt.call(
                "trpg.action.declare",
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                actor_id="card-1", intent="crash", raw_text="crash", auto_confirm=True,
            )
            with pytest.raises(UmpError):
                await mgmt.call(
                    "trpg.action.resolve", timeout=60.0,
                    instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                    plugin_manifest=plugin, action_id=declared["action_id"],
                    actor_id="card-1", intent="crash",
                )
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"],
                                   timeline_id=timeline_id, campaign_id=campaign_id)
            assert view["actions"][0]["status"] == "plugin_failed"
            assert view["rule_state"]["state_revision"] == 0
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_rollback_restores_rule_state(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            first, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="ok")
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=first, idempotency_key="k-1")
            mark = await mgmt.call("runtime.commit", instance_id=info["id"], timeline_id=timeline_id)
            second, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="ok")
            await mgmt.call("trpg.commit", instance_id=info["id"], timeline_id=timeline_id,
                            campaign_id=campaign_id, action_id=second, idempotency_key="k-2")
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 2 and state["opaque_state"]["actors"]["pc-1"]["hp"] == 4

            await mgmt.call("runtime.rollback", instance_id=info["id"], timeline_id=timeline_id,
                            commit_id=str(mark["commit"]["id"]), confirm=True)
            rolled = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                     timeline_id=timeline_id, campaign_id=campaign_id)
            assert rolled["state_revision"] == 1, "回滚把规则状态退回提交那一刻"
            assert rolled["opaque_state"]["actors"]["pc-1"]["hp"] == 2
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"],
                                   timeline_id=timeline_id, campaign_id=campaign_id)
            assert [item["action_id"] for item in view["recent"]] == [first], "被截去的行动退出当前线"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_recover_reports_inflight_without_guessing(tmp_path) -> None:
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            declared = await mgmt.call(
                "trpg.action.declare",
                instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                actor_id="card-1", intent="ok", raw_text="ok", auto_confirm=True,
            )
            # 模拟「进程在裁定中途被杀」：库里留下 resolving 状态，重启后必须被归位
            harness.store.trpg_upserts({"action": [{
                "instance_id": info["id"], "timeline_id": timeline_id, "campaign_id": campaign_id,
                "action_id": declared["action_id"], "status": "resolving",
            }]})
            result = await mgmt.call("trpg.recover", instance_id=info["id"], timeline_id=timeline_id)
            assert result["interrupted"] == 1
            view = await mgmt.call("trpg.scene.view", instance_id=info["id"],
                                   timeline_id=timeline_id, campaign_id=campaign_id)
            assert view["actions"][0]["status"] == "interrupted"
            assert view["rule_state"]["state_revision"] == 0, "中断不猜结果、不扣资源"
        finally:
            await mgmt.close()

@pytest.mark.asyncio
async def test_ruleset_version_change_blocks_then_manual_accept_unblocks(tmp_path) -> None:
    """§十六：插件声明的规则版本变了 → 战役 blocked；人工确认接受是唯一出口。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _resolved = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            committed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="ver-1",
            )
            assert committed["status"] == "committed", committed
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_ruleset_version"] == "1.0", "状态要记下写它时插件声明的规则版本"

            # 插件升级：清单里声明的规则版本变了（只有 opaque_state 格式变化才该这么干）
            manifest_path = Path(plugin)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["ruleset_version"] = "2.0"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

            # 闸门在裁定入口就生效：拿旧状态去问新插件 = 最危险的情形（§十六）
            with pytest.raises(UmpError) as err:
                await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            assert "规则版本不兼容" in str(err.value)

            row = await mgmt.call("trpg.campaign.info", instance_id=info["id"],
                                  timeline_id=timeline_id, campaign_id=campaign_id)
            assert row["status"] == "blocked", "版本不兼容要阻断，不静默替换状态"
            assert "规则版本不兼容" in row["note"]

            # 出口：人工确认接受新版本（同一批把状态版本重铸，并留下记录）
            accepted = await mgmt.call(
                "trpg.campaign.status", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id, status="active", accept_ruleset_version="2.0",
            )
            assert accepted["status"] == "active"
            assert "人工确认" in accepted["note"]
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_ruleset_version"] == "2.0"
            assert state["state_revision"] == 1, "接受版本不改写状态本身"
            assert state["opaque_state"] == {"actors": {"pc-1": {"hp": 2}}}

            action_id2, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            committed2 = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id2, idempotency_key="ver-2",
            )
            assert committed2["status"] == "committed", committed2
            state = await mgmt.call("trpg.rule_state.read", instance_id=info["id"],
                                    timeline_id=timeline_id, campaign_id=campaign_id)
            assert state["state_revision"] == 2 and state["opaque_state"]["actors"]["pc-1"]["hp"] == 4
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_waiting_campaign_takes_only_the_choice(tmp_path) -> None:
    """§11.1：有待选择时战役进 waiting，只接受对应的输入；选完就能继续声明行动。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin, intent="waiting")
            committed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="wait-1",
            )
            assert committed["status"] == "committed", committed
            choice_id = committed["open_choices"][0]
            row = await mgmt.call("trpg.campaign.info", instance_id=info["id"],
                                  timeline_id=timeline_id, campaign_id=campaign_id)
            assert row["status"] == "waiting"

            with pytest.raises(UmpError) as err:
                await mgmt.call(
                    "trpg.action.declare", instance_id=info["id"], timeline_id=timeline_id,
                    campaign_id=campaign_id, actor_id="card-1", intent="硬来", raw_text="硬来",
                )
            assert "等待选择" in str(err.value)

            picked = await mgmt.call(
                "trpg.choice.select", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id, choice_id=choice_id, selection="追上去",
            )
            assert picked["status"] == "selected" and picked["duplicate"] is False
            row = await mgmt.call("trpg.campaign.info", instance_id=info["id"],
                                  timeline_id=timeline_id, campaign_id=campaign_id)
            assert row["status"] == "active"

            # 继续本来就靠「再声明一个行动」完成，不需要额外的自动续接机制
            action_id2, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            after = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id2, idempotency_key="wait-2",
            )
            assert after["status"] == "committed", after
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_gm_declaration_source_is_distinguishable(tmp_path) -> None:
    """§十五：GM 直接裁定的后果与角色行动的后果不能混成同一个来源。"""
    plugin = make_plugin(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.service.runtime, moment=DAY * 1500)
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            action_id, _ = await _run_action(mgmt, info, timeline_id, campaign_id, plugin=plugin)
            with pytest.raises(UmpError) as err:
                await mgmt.call(
                    "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                    action_id=action_id, idempotency_key="gm-bad", source_mode="whatever",
                )
            assert "来源" in str(err.value)

            committed = await mgmt.call(
                "trpg.commit", instance_id=info["id"], timeline_id=timeline_id, campaign_id=campaign_id,
                action_id=action_id, idempotency_key="gm-ok", source_mode="gm_declaration",
            )
            assert committed["status"] == "committed", committed
            events = harness.store.event_window(info["id"], timeline_id, until=10**15)
            sources = {str(row["source"]) for row in events}
            assert "gm_declaration" in sources, "GM 直接裁定要落成自己的来源"
            assert "trpg_action" not in sources
        finally:
            await mgmt.close()
