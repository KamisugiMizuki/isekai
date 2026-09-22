"""TRPG 客户端（TRPG_CLIENT_SPEC C0–C5）：真 WS + 真 SQLite + 真插件子进程。

客户端层是**产品语义**（四个面 / 行动闭环 / 用户可见状态 / 结果表达 / 显示闸门 /
多角色与主持创作），不是把管理面操作换个名字：这些用例盯 §十九 行为验收里属于客户端的行。

完整分段审计（A~H，共 90 项读数）在 `scripts/_audit2_trpgclient.py`；这里留最小回归网。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

from conftest import open_mgmt, running_core
from isekai_core.trpg_client import draft as draft_mod
from isekai_core.trpg_client import states, views
from isekai_core.ump import UmpError
from isekai_core.world.instances import create_instance
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401
from test_trpg_campaign import make_plugin

CLIENT_PLUGIN_SOURCE = '''
import json, sys

request = json.loads(sys.stdin.readline())
mode = str(request.get("intent") or "ok")
state = request.get("rule_state") or {}
base = int(state.get("state_revision") or 0)
patch = {"ruleset_id": state.get("ruleset_id"), "base_state_revision": base,
         "operations": [{"path": "/actors/pc-1/edge", "op": "add" if base == 0 else "increase", "value": 1}]}
consequences = [{"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "封条已揭",
                 "expiry": "with_cause", "certainty": "confirmed"}]
claims = [{"id": "cl-1", "text": "岗位的封条是新的", "source_id": "src-1", "audience": "public_party"}]
transition = {"status": "advanced"}
if mode == "private":
    consequences = [{"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "密探已布防",
                     "expiry": "with_cause", "visibility": "gm_only", "certainty": "confirmed"}]
    claims = []
elif mode == "candidate":
    consequences = [{"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "戒备",
                     "expiry": "with_cause", "certainty": "candidate"}]
    claims = []
elif mode == "choice":
    consequences = [
        {"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "封锁",
         "expiry": "with_cause", "certainty": "confirmed"},
        {"kind": "player_choice", "operation": "create", "choice_id": "ch-1",
         "value": ["追上去", "先撤退"], "audience": "public_party", "certainty": "confirmed"},
        {"kind": "time_advance", "operation": "advance",
         "value": {"seconds": 600, "cause": "追查去向"}, "certainty": "confirmed"},
    ]
    claims = []
print(json.dumps({
    "resolution": {"system": "client-test", "outcome": mode, "degree": "regular", "rolls": [{"d10": 7}]},
    "rule_state_patch": patch, "consequences": consequences, "claims": claims,
    "scene_transition": transition, "participants": ["pc-1"],
}, ensure_ascii=False))
'''

DRAFT_REPLY = json.dumps(
    {"target": "off-1", "method": "徒手", "intent": "揭开封条", "expected_result": "", "risks": ["被抓住"]},
    ensure_ascii=False,
)


async def _call(mgmt, op: str, **args):
    """管理面调用：真插件子进程第一次拉起可能超过默认 30s，统一给足超时。"""
    return await mgmt.call(op, timeout=120.0, **args)


async def _campaign(mgmt, info, timeline_id, plugin, *, host_mode: str = "autonomous") -> str:
    created = await _call(mgmt, 
        "trpg.campaign.create", instance_id=info["id"], timeline_id=timeline_id,
        ruleset_id="fake-rules", ruleset_version="1.0", plugin_manifest=plugin,
        host_mode=host_mode, participants=["card-1"],
        scene={"kind": "conflict", "location_refs": ["rl-1"], "public_facts": [{"text": "岗位的门半掩着"}],
               "active_risks": [{"text": "夜里有人巡岗"}], "available_actions": ["查岗", "问人"]},
    )
    return str(created["campaign_id"])


def _scope(info, timeline_id, campaign_id, *, mode: str = "player", audience: str = "public_party"):
    return {"instance_id": info["id"], "timeline_id": timeline_id, "campaign_id": campaign_id,
            "mode": mode, "audience": audience}


def _events(harness, info, timeline_id) -> list[dict]:
    return [row for row in harness.store.event_window(info["id"], timeline_id, until=10**15, limit=200)
            if str(row["source"]).startswith("trpg") or row["source"] == "gm_declaration"]


def test_visible_state_table_covers_the_spec_and_never_guesses() -> None:
    """§7.1：状态表是闭集，未知状态如实说未知；受众默认严（未标记 = 仅 GM）。"""
    assert len(states.VISIBLE_STATES) == 17
    assert states.user_state("committed")["label"] == "结果已固化"
    unknown = states.user_state("travelling")
    assert unknown["known"] is False and not unknown["actions"] and "未知状态" in unknown["label"]
    assert views.audience_allows("character:a", "character:a") is True
    assert views.audience_allows("character:a", "character:b") is False
    assert views.audience_allows("", "public_party") is False, "未标记的材料不按公开处理"
    assert views.audience_valid("npc:guard-1") and not views.audience_valid("everyone")


def test_display_gate_catches_leaks() -> None:
    """§十二：闸门是检查器——玩家面出现 gm_only 材料或私有字段要报违规，GM 面不报。"""
    bundle = {"faces": {"scene": {"public_facts": [{"text": "密探已布防", "audience": "gm_only"}]}}}
    assert len(views.display_gate(bundle, "public_party")) == 1
    assert views.display_gate(bundle, "gm_only") == []
    assert views.display_gate({"reasons": {"resolution": {"rolls": [7]}}}, "public_party")


def test_draft_card_reports_gaps_and_rejects_hallucinated_targets() -> None:
    """§6.1：字段整理不猜；模型给的目标不在当前场面已登记实体里就报缺口。"""
    scene = {"participants": ["off-1", "off-2"], "location_refs": ["rl-1"]}
    empty = draft_mod.decide(text="我去看看", explicit={}, model={}, actor="card-1", scene=scene)
    assert not empty["ready"] and any("目标" in gap for gap in empty["gaps"])
    guessed = draft_mod.decide(text="我去看看", explicit={}, model={"target": "off-77", "intent": "看看"},
                               actor="card-1", scene=scene)
    assert not guessed["ready"] and any("off-77" in gap for gap in guessed["gaps"])
    ok = draft_mod.decide(text="我去看看", explicit={"target": "off-1"}, model={"intent": "看看"},
                          actor="card-1", scene=scene)
    assert ok["ready"] and ok["sources"]["intent"] == "model"
    assert draft_mod.parse_draft("```json\n{\"intent\": \"看看\"}\n```")["intent"] == "看看"


@pytest.mark.asyncio
async def test_faces_are_audience_trimmed(tmp_path) -> None:
    """C0：玩家面拿不到 resolution / GM 层；主持面拿得到（§十二 / §7.2）。"""
    plugin = make_plugin(tmp_path, source=CLIENT_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.world,
                                                     moment=DAY * 1500)
            harness.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            player = await _call(mgmt, "trpg.client.enter", **_scope(info, timeline_id, campaign_id),
                                     character_id="card-1")
            assert player["faces"]["gates"]["violations"] == []
            assert "gm" not in player["faces"]
            assert "resolution" not in json.dumps(player["faces"], ensure_ascii=False)
            assert player["faces"]["scene"]["public_facts"], "场景公开事实要来自真实投影"
            assert player["faces"]["next"]["next"] == "可以声明下一行动"
            gm = await _call(mgmt, "trpg.client.enter", **_scope(info, timeline_id, campaign_id, mode="gm",
                                                               audience="gm_only"), character_id="card-1")
            assert gm["faces"]["gm"]["rule_state"]["ruleset"] == "fake-rules"
            assert gm["faces"]["gm"]["direct_change_form"]["fields"]
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_action_card_then_player_auto_commit(tmp_path) -> None:
    """C1：草稿不落库 → 缺口不放行 → 确认后真插件裁定 → 玩家模式自动提交（§16.1 / §20.2）。"""
    plugin = make_plugin(tmp_path, source=CLIENT_PLUGIN_SOURCE)
    async with running_core(tmp_path, replies=[DRAFT_REPLY]) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.world,
                                                     moment=DAY * 1500)
            harness.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            scope = _scope(info, timeline_id, campaign_id)
            entered = await _call(mgmt, "trpg.client.enter", **scope, character_id="card-1")
            assert entered["faces"]["gates"]["violations"] == []
            # 工作区由客户端持有并传进传出（§4.1）：闭环里的每次调用都带着它
            ws = entered["workspace"]

            card = await _call(mgmt, "trpg.client.act", **scope, workspace=ws, text="我揭开封条",
                               fields={}, confirm=False)
            assert card["stage"] == "draft" and card["draft"]["sources"]["intent"] == "model"
            assert not harness.store.trpg_list("action", instance_id=info["id"], timeline_id=timeline_id), \
                "草稿阶段不能落行动行（§18.1）"
            gaps = await _call(mgmt, "trpg.client.act", **scope, workspace=ws, text="我去看看",
                               fields={"method": "徒手"}, confirm=True)
            assert gaps["stage"] == "draft_gaps" and gaps["draft"]["gaps"]

            done = await _call(mgmt, "trpg.client.act", **scope, workspace=ws, text="我揭开封条",
                               confirm=True,
                               fields={"target": "off-1", "intent": "揭开封条", "method": "徒手"})
            assert done["resolved_status"] == "reviewing" and done["committed"] is True
            assert "封条已揭" in json.dumps(done["result"]["result"], ensure_ascii=False)
            assert "岗位的封条是新的" in done["result"]["reasons"]["public_summary"]
            assert "audit" not in done and "raw_resolution" not in done, "玩家模式不给 GM 层"
            written = _events(harness, info, timeline_id)
            assert written and str(written[-1]["source"]) == "trpg_action"
            assert done["faces"]["gates"]["violations"] == []
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_gm_mode_stops_at_reviewing_and_private_stays_private(tmp_path) -> None:
    """C3：GM 模式不自动提交；gm_only 后果不进玩家面但进 GM 审计层。"""
    plugin = make_plugin(tmp_path, source=CLIENT_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.world,
                                                     moment=DAY * 1500)
            harness.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            player = _scope(info, timeline_id, campaign_id)
            gm = _scope(info, timeline_id, campaign_id, mode="gm", audience="gm_only")
            await _call(mgmt, "trpg.client.enter", **player, character_id="card-1")
            before = len(_events(harness, info, timeline_id))
            stopped = await _call(mgmt, 
                "trpg.client.act", **gm, text="GM 试一次", confirm=True, character_id="card-1",
                fields={"target": "off-1", "intent": "ok", "method": "暗中观察"},
            )
            assert stopped["resolved_status"] == "reviewing" and stopped["committed"] is False
            assert len(_events(harness, info, timeline_id)) == before, "GM 模式不许自动写世界"
            approved = await _call(mgmt, "trpg.client.review", **gm, action_id=stopped["action_id"],
                                       decision="approve")
            assert approved["committed"] is True

            private = await _call(mgmt, 
                "trpg.client.act", **player, text="私下试探", confirm=True, character_id="card-1",
                fields={"target": "off-1", "intent": "private", "method": "试探"},
            )
            assert private["committed"] is True
            assert "密探已布防" not in json.dumps(private["result"]["result"], ensure_ascii=False)
            assert private["faces"]["gates"]["violations"] == []
            replay = await _call(mgmt, "trpg.client.retry", **gm, kind="resume_submit",
                                     action_id=private["action_id"])
            audit = replay.get("audit") or {}
            assert any("密探已布防" in json.dumps(item, ensure_ascii=False)
                       for item in audit.get("effects") or []), "受众允许时 GM 审计层要拿得到"
            assert replay["raw_resolution"], "第四层原始材料只给主持"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_waiting_locks_and_choice_round_trip(tmp_path) -> None:
    """C2：待选择置首 + waiting 闸（关键行动锁死）；选择后回到 active，重复选择返回原结果。"""
    plugin = make_plugin(tmp_path, source=CLIENT_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.world,
                                                     moment=DAY * 1500)
            harness.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            scope = _scope(info, timeline_id, campaign_id)
            await _call(mgmt, "trpg.client.enter", **scope, character_id="card-1")
            clock_before = int(harness.runtime.service.runtime.clock_row(timeline_id)["processed_world"])
            done = await _call(mgmt, "trpg.client.act", **scope, text="追上去", confirm=True,
                                   character_id="card-1",
                                   fields={"target": "off-1", "intent": "choice", "method": "追上去"})
            assert done["choice"] and [item["text"] for item in done["choice"]["options"]] == ["追上去", "先撤退"]
            assert "已前进 600 秒" in done["time"]["world_time"]
            assert int(harness.runtime.service.runtime.clock_row(timeline_id)["processed_world"]) \
                - clock_before == 600
            locked = await _call(mgmt, "trpg.client.act", **scope, text="抢在前面", confirm=True,
                                     character_id="card-1",
                                     fields={"target": "off-1", "intent": "追上去", "method": "跑"})
            assert locked["stage"] == "blocked" and "等待选择" in locked["blocked"]
            picked = await _call(mgmt, "trpg.client.choice", **scope, choice_id="ch-1", option_id="追上去")
            assert picked["faces"]["campaign"]["status"] == "active"
            again = await _call(mgmt, "trpg.client.choice", **scope, choice_id="ch-1", option_id="先撤退")
            assert again["duplicate"] is True
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_interrupted_action_can_be_explicitly_rerun(tmp_path) -> None:
    """C2：重启恢复把在途行动推成 interrupted；显式重试走真实裁定，不藏普通重试里。"""
    plugin = make_plugin(tmp_path, source=CLIENT_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.world,
                                                     moment=DAY * 1500)
            harness.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            scope = _scope(info, timeline_id, campaign_id)
            declared = await _call(mgmt, 
                "trpg.action.declare", instance_id=info["id"], timeline_id=timeline_id,
                campaign_id=campaign_id, actor_id="card-1", intent="ok", raw_text="ok",
                auto_confirm=True, target_refs=["off-1"],
            )
            action_id = str(declared["action_id"])
            row = harness.store.trpg_get("action", instance_id=info["id"], timeline_id=timeline_id,
                                         campaign_id=campaign_id, action_id=action_id)
            harness.store.trpg_upserts({"action": [{**row, "status": "resolving"}]})
            recovered = await _call(mgmt, "trpg.client.enter", **scope, character_id="card-1")
            assert recovered["recovery"]["interrupted"] >= 1
            stuck = harness.store.trpg_get("action", instance_id=info["id"], timeline_id=timeline_id,
                                           campaign_id=campaign_id, action_id=action_id)
            assert str(stuck["status"]) == "interrupted"
            retried = await _call(mgmt, "trpg.client.retry", **scope, kind="retry_resolve",
                                      action_id=action_id)
            assert retried["resolved_status"] == "reviewing" and retried["committed"] is True
            assert "interrupted" not in str(retried.get("errors") or ""), retried.get("errors")
            with pytest.raises(UmpError) as err:
                await _call(mgmt, "trpg.client.retry", **scope, kind="resume_submit",
                                action_id="act-404")
            assert "act-404" in str(err.value) or "没有" in str(err.value)
        finally:
            await mgmt.close()

async def _multi_campaign(mgmt, info, timeline_id, plugin, actors) -> str:
    """一个实例、一份战役、两个角色：多角色切换要在这上面验。"""
    actor_a, actor_b = actors
    created = await _call(
        mgmt, "trpg.campaign.create", instance_id=info["id"], timeline_id=timeline_id,
        ruleset_id="fake-rules", ruleset_version="1.0", plugin_manifest=plugin,
        host_mode="autonomous", participants=[actor_a, actor_b],
        scene={"kind": "conflict", "location_refs": ["rl-1"],
               "public_facts": [{"text": "岗位的门半掩着"}],
               "private_views": {f"character:{actor_a}": [{"text": "堤禾认得墙上的划痕"}],
                                 f"character:{actor_b}": [{"text": "渡舟听见水声"}]},
               "available_actions": ["查岗"]},
    )
    return str(created["campaign_id"])


@pytest.mark.asyncio
async def test_switch_actor_keeps_private_views_apart(tmp_path) -> None:
    """C4：同一用户控制多个角色时，切换只换 actor / audience，私密认知不合并。"""
    plugin = make_plugin(tmp_path, source=CLIENT_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            package = sample_package(moment=DAY * 1500)
            card_a = sample_card(package, name="堤禾")
            card_b = sample_card(package, name="渡舟")
            info = create_instance(harness.store, package, [card_a, card_b])
            timeline_id = str(harness.store.timeline_list(info["id"])[0]["id"])
            harness.runtime.world.ensure_instance(info["id"], now_real=time.time())
            harness.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
            actor_a = str(card_a["meta"]["card_id"])
            actor_b = str(card_b["meta"]["card_id"])
            campaign_id = await _multi_campaign(mgmt, info, timeline_id, plugin, (actor_a, actor_b))
            scope = _scope(info, timeline_id, campaign_id)
            entered = await _call(mgmt, "trpg.client.enter", **scope, character_id=actor_a)
            # 切换只给作用域与角色：受众由客户端从角色推导（不给就是 character:<id>）
            base = {key: value for key, value in scope.items() if key not in ("mode", "audience")}
            first = await _call(mgmt, "trpg.client.switch", **base, workspace=entered["workspace"],
                                character_id=actor_a)
            second = await _call(mgmt, "trpg.client.switch", **base, workspace=first["workspace"],
                                 character_id=actor_b)
            seen_a = json.dumps(first["faces"]["party"], ensure_ascii=False)
            seen_b = json.dumps(second["faces"]["party"], ensure_ascii=False)
            assert "堤禾认得墙上的划痕" in seen_a and "渡舟听见水声" not in seen_a
            assert "渡舟听见水声" in seen_b and "堤禾认得墙上的划痕" not in seen_b
            assert second["workspace"]["audience"] == f"character:{actor_b}"
            assert second["faces"]["gates"]["violations"] == []
            with pytest.raises(UmpError) as err:
                await _call(mgmt, "trpg.client.switch", **base, character_id=actor_b,
                            audience="gm_only")
            assert "gm_only" in str(err.value)
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_second_plugin_runs_the_same_client_flow(tmp_path) -> None:
    """C4：换第二个真实规则插件（潮汐骰池）跑同一套客户端流程——不读第一套规则的字段。"""
    tide = Path(__file__).resolve().parents[1] / "examples" / "tide_rules_plugin" / "manifest.json"
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, card_id = make_instance(harness.store, harness.runtime.world,
                                                      moment=DAY * 1500)
            harness.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
            created = await _call(
                mgmt, "trpg.campaign.create", instance_id=info["id"], timeline_id=timeline_id,
                ruleset_id="tide", ruleset_version="0.1.0", plugin_manifest=str(tide),
                host_mode="autonomous", participants=[card_id],
                scene={"kind": "conflict", "location_refs": ["rl-1"]},
            )
            scope = _scope(info, timeline_id, str(created["campaign_id"]))
            await _call(mgmt, "trpg.client.enter", **scope, character_id=card_id)
            # 潮汐插件会把「被压制」压在行动者身上：行动者必须是已登记目标（真实角色卡标识）
            done = await _call(
                mgmt, "trpg.client.act", **scope, character_id=card_id,
                text="沿墙摸过去查岗", confirm=True,
                fields={"target": "off-1", "intent": "沿墙摸过去查岗", "method": "沿墙摸过去"},
            )
            surface = json.dumps({"result": done.get("result"), "faces": done.get("faces")},
                                 ensure_ascii=False)
            assert done["committed"] is True, (done.get("resolved_status"), done.get("errors"))
            for name in ('"hp"', '"sp"', "edge", '"pool"', '"stress"'):
                assert name not in surface, f"客户端面不该出现第一套 / 第二套规则的私有字段：{name}"
            gm_scope = _scope(info, timeline_id, str(created["campaign_id"]), mode="gm",
                              audience="gm_only")
            gm = await _call(mgmt, "trpg.client.retry", **gm_scope, kind="resume_submit",
                             action_id=str(done["action_id"]))
            assert (gm.get("audit") or {}).get("level", {}).get("outcome"), gm.get("audit")
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_branch_and_rollback_need_explicit_confirmation(tmp_path) -> None:
    """C5：分支与回滚先给风险说明；确认后才执行；回滚清空本地状态并让规则状态退回去。"""
    plugin = make_plugin(tmp_path, source=CLIENT_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.world,
                                                     moment=DAY * 1500)
            harness.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            scope = _scope(info, timeline_id, campaign_id)
            gm = _scope(info, timeline_id, campaign_id, mode="gm", audience="gm_only")
            entered = await _call(mgmt, "trpg.client.enter", **scope, character_id="card-1")
            await _call(mgmt, "trpg.client.act", **scope, workspace=entered["workspace"],
                        text="揭开封条", confirm=True,
                        fields={"target": "off-1", "intent": "ok", "method": "徒手"})
            commit_id = str(harness.runtime.world.commit(info["id"], timeline_id, kind="manual",
                                                          note="分支点")["id"])
            lines_before = len(harness.store.timeline_list(info["id"]))
            brief = await _call(mgmt, "trpg.client.branch", **gm, commit_id=commit_id)
            assert brief["stage"] == "branch_confirm" and len(brief["brief"]["lines"]) >= 4
            assert len(harness.store.timeline_list(info["id"])) == lines_before, "未确认不建线"
            made = await _call(mgmt, "trpg.client.branch", **gm, workspace=brief["workspace"],
                               commit_id=commit_id, name="试演线", confirm=True)
            assert made["new_timeline_id"] and made["original_timeline_kept"] is True
            assert len(harness.store.timeline_list(info["id"])) == lines_before + 1
            await _call(mgmt, "trpg.client.act", **scope, text="再来一次", confirm=True,
                        character_id="card-1",
                        fields={"target": "off-1", "intent": "ok", "method": "徒手"})
            risky = await _call(mgmt, "trpg.client.rollback", **(gm | {"timeline_id": timeline_id}),
                                workspace={**made["workspace"], "timeline_id": timeline_id},
                                commit_id=commit_id)
            assert risky["stage"] == "rollback_confirm" and len(risky["brief"]["lines"]) >= 5
            rolled = await _call(mgmt, "trpg.client.rollback", **gm,
                                 workspace=risky["workspace"], commit_id=commit_id, confirm=True,
                                 saved=True)
            state = harness.store.trpg_get("rule_state", instance_id=info["id"],
                                           timeline_id=timeline_id, campaign_id=campaign_id,
                                           ruleset_id="fake-rules") or {}
            assert int(state.get("state_revision") or 0) == 1, state.get("state_revision")
            assert len(rolled["discarded"]) >= 4 and rolled["workspace"]["draft_text"] == ""
            assert rolled["faces"]["gates"]["violations"] == []
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_express_writes_an_audience_tagged_claim(tmp_path) -> None:
    """C5：主持写的人工表达落成带受众的说法；事件帧只许公开材料。"""
    plugin = make_plugin(tmp_path, source=CLIENT_PLUGIN_SOURCE)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info, timeline_id, _card = make_instance(harness.store, harness.runtime.world,
                                                     moment=DAY * 1500)
            harness.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
            campaign_id = await _campaign(mgmt, info, timeline_id, plugin)
            gm = _scope(info, timeline_id, campaign_id, mode="gm", audience="gm_only")
            entered = await _call(mgmt, "trpg.client.enter", **gm, character_id="card-1")
            tell_scope = {k: v for k, v in gm.items() if k != "audience"}
            told = await _call(mgmt, "trpg.client.express", **tell_scope,
                               workspace=entered["workspace"], text="主持补叙：夜里换了岗",
                               audience="public_party")
            assert told["committed"] is True
            claims = [item for item in harness.store.claim_list(info["id"], timeline_id)
                      if "补叙" in str(item.get("text") or "")]
            assert claims and str(claims[0]["audience"]) == "public_party"
            with pytest.raises(UmpError) as err:
                await _call(mgmt, "trpg.client.express", **tell_scope, text="私下安排",
                            audience="gm_only", as_frame=True)
            assert "事件正文" in str(err.value)
        finally:
            await mgmt.close()
