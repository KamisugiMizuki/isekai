"""TRPG 规则层（TRPG_RULES_LAYER_SPEC）行为级审计探针。

问题：规则层是「应用编排层」还是「一堆分散的 op」？三条入口、主持责任模式、推进节拍、
结果与失败语义、隐私 / 版本 / 幂等这些责任，落在真调用链上是什么行为？

判法（真 WebSocket + 真 SQLite + 真插件子进程，只把 LLM 换成 FakeLLM）：

- L1~L13：§十五 行为验收表逐行；
- M1~M3：§四 / §八 / §九 的最小数据对象与责任边界（主持模式、关键行动、推进节拍）；
- R1~R2：§五 / §七 的入口语义（明确无变化 / 放弃 / 无法裁定不阻塞改写）。

只读项目代码；数据写在临时目录；不需要联网（FakeLLM）。
用法：`.venv/Scripts/python.exe scripts/_audit2_trpglayer.py [--only 关键字] [--json]`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _audit2_rulecommon import (  # noqa: E402
    ACTORS, RESULTS, check, core, expect, fail, line, make_plugin, ok, only_matches, report, world_facts,
)

ROOT = Path(__file__).resolve().parent.parent
ONLY = ""

LAYER_PLUGIN = '''
import json, sys

request = json.loads(sys.stdin.readline())
mode = str(request.get("intent") or "ok")
state = request.get("rule_state") or {}
base = int(state.get("state_revision") or 0)
if mode == "crash":
    sys.exit(1)
if mode == "needs_choice":
    print(json.dumps({"error": {"code": "needs_choice", "message": "等玩家挑一个"}}, ensure_ascii=False))
    raise SystemExit(0)
if mode == "half":
    print(json.dumps({
        "error": {"code": "rejected", "message": "带半成品"},
        "consequences": [{"kind": "state_change", "operation": "set", "target_refs": ["off-1"],
                          "value": "vacant", "expiry": "until_cleared", "certainty": "confirmed"}],
    }, ensure_ascii=False))
    raise SystemExit(0)
if mode == "b0":
    print(json.dumps({
        "resolution": {"system": "layer-audit", "outcome": mode},
        "effects": [{"kind": "institution_state", "target": "off-1", "value": "vacant",
                     "expiry": "until_cleared", "certainty": "confirmed"}],
        "claims": [],
    }, ensure_ascii=False))
    raise SystemExit(0)

patch = {
    "ruleset_id": state.get("ruleset_id"),
    "base_state_revision": base if mode != "bad_base" else base + 5,
    "operations": [{"path": "/actors/pc-1/edge", "op": "add" if base == 0 else "increase", "value": 1}],
}
consequences = [{
    "kind": "state_change", "operation": "set", "target_refs": ["off-1"], "value": "vacant",
    "expiry": "until_cleared", "certainty": "confirmed",
}]
transition = {"status": "advanced"}
claims = []
if mode == "nochange":
    consequences = []
elif mode == "choice":
    consequences = [
        {"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "封锁",
         "expiry": "with_cause", "certainty": "confirmed"},
        {"kind": "player_choice", "operation": "create", "choice_id": "ch-1",
         "value": ["追上去", "先撤退"], "audience": "public_party", "certainty": "confirmed"},
        {"kind": "time_advance", "operation": "advance",
         "value": {"seconds": 600, "cause": "追查去向"}, "certainty": "confirmed"},
    ]
    transition = {"status": "waiting_choice", "rule_time_delta": 2}
elif mode == "candidate":
    consequences = [{"kind": "condition", "operation": "create", "target_refs": ["off-1"],
                     "value": "也许有人盯梢", "expiry": "with_cause", "certainty": "candidate"}]
print(json.dumps({
    "resolution": {"system": "layer-audit", "outcome": mode, "gm_notes": "只有 GM 该看到的依据"},
    "rule_state_patch": patch,
    "consequences": consequences,
    "scene_transition": transition,
    "claims": claims,
    "participants": ["pc-1"],
}, ensure_ascii=False))
'''


async def new_campaign(h, instance_id: str, timeline_id: str, manifest: Path, *, host_mode="assisted"):
    created = await h.mgmt.call(
        "trpg.campaign.create", instance_id=instance_id, timeline_id=timeline_id,
        ruleset_id="fake-rules", ruleset_version="1.0", plugin_manifest=str(manifest),
        participants=[ACTORS.get(instance_id, "card-1")], host_mode=host_mode,
        scene={"kind": "conflict", "location_refs": ["rl-1"]},
    )
    return str(created["campaign_id"])


async def declare(h, instance_id: str, timeline_id: str, campaign_id: str, *, intent: str,
                  auto: bool = True, critical: bool = False, actor: str | None = None) -> dict:
    return await h.mgmt.call(
        "trpg.action.declare", instance_id=instance_id, timeline_id=timeline_id,
        campaign_id=campaign_id, actor_id=actor or ACTORS.get(instance_id, "card-1"),
        intent=intent, raw_text=intent, auto_confirm=auto, require_confirmation=critical,
        target_refs=["off-1"],
    )


async def resolve(h, instance_id: str, timeline_id: str, campaign_id: str, manifest: Path, action_id: str,
                  *, intent: str) -> dict:
    return await h.mgmt.call(
        "trpg.action.resolve", instance_id=instance_id, timeline_id=timeline_id,
        campaign_id=campaign_id, plugin_manifest=str(manifest), action_id=action_id,
        actor_id=ACTORS.get(instance_id, "card-1"), intent=intent, timeout=60.0,
    )


async def commit(h, instance_id: str, timeline_id: str, campaign_id: str, action_id: str, *, key: str,
                 **extra) -> dict:
    return await h.mgmt.call(
        "trpg.commit", instance_id=instance_id, timeline_id=timeline_id, campaign_id=campaign_id,
        action_id=action_id, idempotency_key=key, **extra,
    )


async def run_all() -> None:
    async with core() as h:
        manifest = make_plugin(h.root, source=LAYER_PLUGIN)
        instance_id, timeline_id, _card = await line(h)
        campaign_id = await new_campaign(h, instance_id, timeline_id, manifest, host_mode="autonomous")

        # L1 B0 无状态 resolver：不留战役痕迹（§5.5）
        b0 = await h.mgmt.call("trpg.action.resolve", instance_id=instance_id, timeline_id=timeline_id,
                               plugin_manifest=str(manifest), action_id="b0-1", actor_id="card-1",
                               intent="b0")
        expect("L1 B0 入口",
               "不带 campaign_id 时只走兼容单次裁定：不产生行动行 / 规则状态 / 战役痕迹",
               b0["accepted"] is True
               and h.store.trpg_list("action", instance_id=instance_id, timeline_id=timeline_id) == []
               and h.store.trpg_list("rule_state", instance_id=instance_id, timeline_id=timeline_id) == [],
               f"accepted={b0['accepted']} 行动行="
               f"{len(h.store.trpg_list('action', instance_id=instance_id, timeline_id=timeline_id))}",
               "world/ops.py::_trpg_action_resolve 的 B0 分支")

        # L2 战役行动：未确认不入裁定链；关键行动不自动确认
        assisted_id = await new_campaign(h, instance_id, timeline_id, manifest, host_mode="assisted")
        pending = await declare(h, instance_id, timeline_id, assisted_id, intent="ok")
        refused = ""
        try:
            await resolve(h, instance_id, timeline_id, assisted_id, manifest, str(pending["action_id"]),
                          intent="ok")
        except Exception as exc:  # noqa: BLE001 —— 就是要把拒绝文案读出来
            refused = str(exc)
        critical = await declare(h, instance_id, timeline_id, campaign_id, intent="撬锁", critical=True)
        expect("L2 未确认 / 关键行动",
               "辅助裁定不自动确认；关键行动在自动主持下也要玩家确认",
               pending["status"] == "awaiting_confirmation" and "未确认" in refused
               and critical["status"] == "awaiting_confirmation",
               f"辅助裁定声明={pending['status']}；裁定被拒={refused[:60]}；关键行动={critical['status']}",
               "§5.2 / §八：declare 的主持模式闸")

        # L3 确认版本：修改涨版本，旧版本不能确认
        asked_id = await declare(h, instance_id, timeline_id, campaign_id, intent="撬锁", auto=False)
        wrong = ""
        try:
            await h.mgmt.call("trpg.action.confirm", instance_id=instance_id, timeline_id=timeline_id,
                              campaign_id=campaign_id, action_id=str(asked_id["action_id"]),
                              action_revision=int(asked_id["action_revision"]) + 1)
        except Exception as exc:  # noqa: BLE001
            wrong = str(exc)
        confirmed = await h.mgmt.call(
            "trpg.action.confirm", instance_id=instance_id, timeline_id=timeline_id,
            campaign_id=campaign_id, action_id=str(asked_id["action_id"]),
            action_revision=int(asked_id["action_revision"]), changes={"intent": "改撬窗户"},
        )
        expect("L3 确认与行动版本",
               "版本不一致拒绝；修改增加 action_revision",
               "版本不一致" in wrong and confirmed["action_revision"] == int(asked_id["action_revision"]) + 1,
               f"错版本={wrong[:60]}；修改后 revision={confirmed['action_revision']}",
               "confirm() 的 action_revision 闸（§4.2 / §十五）")

        # L4 裁定与提交分开：resolve 只保存，commit 才改世界
        before_facts = len(world_facts(h, instance_id, timeline_id))
        action = await declare(h, instance_id, timeline_id, campaign_id, intent="ok")
        resolved = await resolve(h, instance_id, timeline_id, campaign_id, manifest,
                                 str(action["action_id"]), intent="ok")
        after_resolve = len(world_facts(h, instance_id, timeline_id))
        committed = await commit(h, instance_id, timeline_id, campaign_id, str(action["action_id"]),
                                 key="l4")
        expect("L4 裁定 ≠ 提交",
               "resolve 停在 reviewing 且世界零变化；commit 成功后才落事件与规则状态",
               resolved["status"] == "reviewing" and after_resolve == before_facts
               and committed["status"] == "committed" and len(world_facts(h, instance_id, timeline_id))
               == before_facts + 1,
               f"resolve={resolved['status']}；提交前事件={after_resolve}"
               f"；提交后={len(world_facts(h, instance_id, timeline_id))}",
               "§5.4：只有提交成功才能把结果表达为已发生")

        # L5 GM 直接变化：不调插件、不造行动行、不伪造骰点
        actions_before = len(h.store.trpg_list("action", instance_id=instance_id, timeline_id=timeline_id))
        gm = await h.mgmt.call(
            "trpg.gm.change", instance_id=instance_id, timeline_id=timeline_id, campaign_id=campaign_id,
            idempotency_key="l5",
            changes={"consequences": [{"kind": "state_change", "operation": "set", "target_refs": ["off-1"],
                                       "value": "occupied", "expiry": "until_cleared",
                                       "certainty": "confirmed"}]},
        )
        gm_events = [row for row in world_facts(h, instance_id, timeline_id)
                     if str(row["source"]) == "gm_declaration"]
        expect("L5 GM 直接变化",
               "来源 gm_declaration、不造行动行、不伪造骰点",
               gm["status"] == "committed"
               and len(h.store.trpg_list("action", instance_id=instance_id, timeline_id=timeline_id))
               == actions_before
               and gm_events and "rolls" not in json.dumps(gm_events[-1].get("detail") or "", ensure_ascii=False),
               f"GM={gm['status']}；行动行={actions_before} → "
               f"{len(h.store.trpg_list('action', instance_id=instance_id, timeline_id=timeline_id))}；"
               f"来源={gm_events[-1]['source'] if gm_events else '（无）'}",
               "§六 / §七 + TRPG_RULE_COMMON_MODULE_SPEC §七")

        # L6 联合提交失败：冲突 / 待审都不落半条
        bad = await declare(h, instance_id, timeline_id, campaign_id, intent="bad_base")
        await resolve(h, instance_id, timeline_id, campaign_id, manifest, str(bad["action_id"]),
                      intent="bad_base")
        state_before = h.store.trpg_get("rule_state", instance_id=instance_id, timeline_id=timeline_id,
                                        campaign_id=campaign_id, ruleset_id="fake-rules")
        conflicted = await commit(h, instance_id, timeline_id, campaign_id, str(bad["action_id"]), key="l6")
        state_after = h.store.trpg_get("rule_state", instance_id=instance_id, timeline_id=timeline_id,
                                       campaign_id=campaign_id, ruleset_id="fake-rules")
        expect("L6 联合提交冲突",
               "返回 conflict；规则状态与世界都不动",
               conflicted["status"] == "conflict"
               and int(state_before["state_revision"]) == int(state_after["state_revision"]),
               f"{conflicted['status']}；规则状态 revision={state_after['state_revision']}",
               "base_state_revision 与快照不一致 → conflict")

        # L7 插件错误：结构化错误映射状态；夹带半成品不采信
        asked = await declare(h, instance_id, timeline_id, campaign_id, intent="needs_choice")
        choice_pending = await resolve(h, instance_id, timeline_id, campaign_id, manifest,
                                       str(asked["action_id"]), intent="needs_choice")
        half = await declare(h, instance_id, timeline_id, campaign_id, intent="half")
        half_result = await resolve(h, instance_id, timeline_id, campaign_id, manifest,
                                    str(half["action_id"]), intent="half")
        expect("L7 插件错误",
               "needs_choice → awaiting_choice；半成品不采信（进待审）",
               choice_pending["status"] == "awaiting_choice"
               and half_result["status"] == "awaiting_gm_review"
               and "不采信" in json.dumps(half_result["errors"], ensure_ascii=False),
               f"{choice_pending['status']} / {half_result['status']}；{half_result['errors']}",
               "§5.8 结构化错误 + ERROR_FORBIDDEN_KEYS")

        # L8 插件死亡：不重跑、不猜结果
        dying = await declare(h, instance_id, timeline_id, campaign_id, intent="crash")
        crash_note = ""
        try:
            await resolve(h, instance_id, timeline_id, campaign_id, manifest, str(dying["action_id"]),
                          intent="crash")
        except Exception as exc:  # noqa: BLE001
            crash_note = str(exc)
        row = h.store.trpg_get("action", instance_id=instance_id, timeline_id=timeline_id,
                               campaign_id=campaign_id, action_id=str(dying["action_id"]))
        expect("L8 插件死亡 / 超时",
               "行动标 plugin_failed、没留半条裁定（核心重启恢复见测试）",
               crash_note and str(row["status"]) == "plugin_failed" and not (row.get("resolution") or ""),
               f"{str(row['status'])}；错误={crash_note[:70]}",
               "tests/test_trpg_campaign.py::test_recover_reports_inflight_without_guessing 覆盖重启恢复")

        # L9 规则版本变化：不兼容阻断 → 人工接受放行
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        manifest_data["ruleset_version"] = "2.0"
        manifest.write_text(json.dumps(manifest_data), encoding="utf-8")
        gated = await declare(h, instance_id, timeline_id, campaign_id, intent="ok")
        blocked_note = ""
        try:
            await resolve(h, instance_id, timeline_id, campaign_id, manifest, str(gated["action_id"]),
                          intent="ok")
        except Exception as exc:  # noqa: BLE001
            blocked_note = str(exc)
        campaign_row = h.store.trpg_get("campaign", instance_id=instance_id, timeline_id=timeline_id,
                                        campaign_id=campaign_id)
        accepted = await h.mgmt.call("trpg.campaign.status", instance_id=instance_id, timeline_id=timeline_id,
                                     campaign_id=campaign_id, status="active",
                                     accept_ruleset_version="2.0")
        # 清单保持 2.0：后面还要在这条战役上继续走链路，版本闸不能又炸一次（那才是真升级场景）
        expect("L9 规则版本变化",
               "不兼容 → 战役 blocked + 拒绝裁定；人工接受后放行",
               "规则版本不兼容" in blocked_note and str(campaign_row["status"]) == "blocked"
               and accepted["status"] == "active" and accepted["ruleset_version"] == "2.0",
               f"阻断={blocked_note[:40]}；战役={campaign_row['status']} → 接受后 {accepted['status']}",
               "§十六 第 2 层版本闸 + status(accept_ruleset_version=…)")

        # L10 幂等重试
        facts_before = len(world_facts(h, instance_id, timeline_id))
        again = await commit(h, instance_id, timeline_id, campaign_id, str(action["action_id"]), key="l4")
        expect("L10 幂等重试",
               "同键返回原结果，不再写世界、不再动规则状态",
               again["status"] == "duplicate" and again["joint_commit_id"] == committed["joint_commit_id"]
               and len(world_facts(h, instance_id, timeline_id)) == facts_before,
               f"{again['status']}；事件数不变={facts_before}",
               "trpg_commit 账本按幂等键回放")

        # L11 受众隔离：GM 私有 resolution 不进公开面
        public_view = await h.mgmt.call("trpg.scene.view", instance_id=instance_id,
                                        timeline_id=timeline_id, campaign_id=campaign_id,
                                        audience="public_party")
        gm_view = await h.mgmt.call("trpg.scene.view", instance_id=instance_id, timeline_id=timeline_id,
                                    campaign_id=campaign_id, audience="gm_only")
        public_text = json.dumps(public_view, ensure_ascii=False)
        expect("L11 受众隔离",
               "公开面没有 GM 私有 resolution（gm_only 面才有）",
               "gm_notes" not in public_text and "gm_notes" in json.dumps(gm_view, ensure_ascii=False),
               f"公开面含 gm_notes={'gm_notes' in public_text}；GM 面含="
               f"{'gm_notes' in json.dumps(gm_view, ensure_ascii=False)}",
               "scene_view / action_view 的受众裁剪（§十 / §十五）")

        # L12 待选择：只进场景转换，且 waiting 时锁住无关关键行动
        choose = await declare(h, instance_id, timeline_id, campaign_id, intent="choice")
        await resolve(h, instance_id, timeline_id, campaign_id, manifest, str(choose["action_id"]),
                      intent="choice")
        waited = await commit(h, instance_id, timeline_id, campaign_id, str(choose["action_id"]),
                              key="l12")
        locked = ""
        try:
            await declare(h, instance_id, timeline_id, campaign_id, intent="ok")
        except Exception as exc:  # noqa: BLE001
            locked = str(exc)
        facts = world_facts(h, instance_id, timeline_id)
        expect("L12 待选择",
               "未选分支只在战役侧；战役 waiting 时拒绝新的关键行动",
               waited["status"] == "committed" and waited["open_choices"] == ["ch-1"]
               and "等待选择" in locked
               and not any("追上去" in json.dumps(row, ensure_ascii=False) for row in facts),
               f"待选择={waited['open_choices']}；锁={locked[:50]}",
               "§11.3 + §5.4：未选分支不进世界事实")

        picked = await h.mgmt.call("trpg.choice.select", instance_id=instance_id, timeline_id=timeline_id,
                                   campaign_id=campaign_id, choice_id="ch-1", selection="追上去")
        expect("L12b 选择后回到可行动",
               "选完战役回 active（无其他 open 选择）",
               picked["status"] == "selected" and str(
                   h.store.trpg_get("campaign", instance_id=instance_id, timeline_id=timeline_id,
                                    campaign_id=campaign_id)["status"]) == "active",
               f"选择={picked['status']}",
               "select_choice 的战役状态折算")

        # L13 分支与回滚：规则状态与场景都退回提交那一刻
        mark = await h.mgmt.call("runtime.commit", instance_id=instance_id, timeline_id=timeline_id)
        extra = await declare(h, instance_id, timeline_id, campaign_id, intent="ok")
        await resolve(h, instance_id, timeline_id, campaign_id, manifest, str(extra["action_id"]),
                      intent="ok")
        await commit(h, instance_id, timeline_id, campaign_id, str(extra["action_id"]), key="l13")
        before_rollback = (await h.mgmt.call("trpg.rule_state.read", instance_id=instance_id,
                                             timeline_id=timeline_id,
                                             campaign_id=campaign_id))["state_revision"]
        await h.mgmt.call("runtime.rollback", instance_id=instance_id, timeline_id=timeline_id,
                          commit_id=str(mark["commit"]["id"]), confirm=True)
        after_rollback = (await h.mgmt.call("trpg.rule_state.read", instance_id=instance_id,
                                            timeline_id=timeline_id,
                                            campaign_id=campaign_id))["state_revision"]
        expect("L13 分支与回滚",
               "回滚把规则状态退回提交那一刻（行动派生不跨线回流）",
               int(after_rollback) < int(before_rollback),
               f"规则状态 {before_rollback} → {after_rollback}",
               "§13 场景恢复与重建 / runtime.rollback")

        # M1 主持责任模式：三档 + 关键行动（§八）
        modes = {}
        for mode in ("assisted", "autonomous", "cohost"):
            cid = await new_campaign(h, instance_id, timeline_id, manifest, host_mode=mode)
            declared = await declare(h, instance_id, timeline_id, cid, intent="ok")
            modes[mode] = declared["status"]
        expect("M1 主持责任模式",
               "辅助裁定 / 共同主持不替玩家确认；自动主持才直接确认",
               modes["assisted"] == "awaiting_confirmation" and modes["cohost"] == "awaiting_confirmation"
               and modes["autonomous"] == "confirmed",
               json.dumps(modes, ensure_ascii=False),
               "§八 表 + campaign.host_mode 闭集")

        # M2 推进节拍：声明而不是回合（§4.1 / §九）
        beat_campaign = await new_campaign(h, instance_id, timeline_id, manifest, host_mode="autonomous")
        beats = (await h.mgmt.call("trpg.scene.view", instance_id=instance_id, timeline_id=timeline_id,
                                   campaign_id=beat_campaign))["scene"]["advance_mode"]
        opened = await h.mgmt.call("trpg.scene.open", instance_id=instance_id, timeline_id=timeline_id,
                                   campaign_id=beat_campaign, kind="conflict", advance_mode="opposed")
        rejected = ""
        try:
            await h.mgmt.call("trpg.scene.open", instance_id=instance_id, timeline_id=timeline_id,
                              campaign_id=beat_campaign, advance_mode="回合制")
        except Exception as exc:  # noqa: BLE001
            rejected = str(exc)
        expect("M2 场景推进节拍",
               "节拍是闭集声明（缺省连续），非法值直接拒；核心不硬套回合",
               beats == "continuous" and opened["advance_mode"] == "opposed" and "推进节拍" in rejected,
               f"缺省={beats}；显式={opened['advance_mode']}；非法={rejected[:50]}",
               "campaign.SCENE_BEATS + _scene_row")

        # R1 明确无变化 / 失败带代价：都要能落账（§七）
        quiet = await new_campaign(h, instance_id, timeline_id, manifest, host_mode="autonomous")
        nochange = await declare(h, instance_id, timeline_id, quiet, intent="nochange")
        await resolve(h, instance_id, timeline_id, quiet, manifest, str(nochange["action_id"]),
                      intent="nochange")
        settled = await commit(h, instance_id, timeline_id, quiet, str(nochange["action_id"]), key="r1")
        quiet_state = await h.mgmt.call("trpg.rule_state.read", instance_id=instance_id,
                                        timeline_id=timeline_id, campaign_id=quiet)
        expect("R1 明确无变化",
               "没有世界后果的裁定照样提交：规则状态落地、事件零效果",
               settled["status"] == "committed" and settled["effects"] == 0
               and int(quiet_state["state_revision"]) == 1
               and quiet_state["opaque_state"]["actors"]["pc-1"]["edge"] == 1,
               f"{settled['status']}；效果={settled['effects']}；规则状态 revision="
               f"{quiet_state['state_revision']}",
               "drafts.normalize_draft(require_effects=False) + _joint_apply")

        # R2 无法裁定 / 放弃：不推进世界，也不阻止改写
        stuck = await declare(h, instance_id, timeline_id, quiet, intent="needs_choice")
        stuck_result = await resolve(h, instance_id, timeline_id, quiet, manifest,
                                     str(stuck["action_id"]), intent="needs_choice")
        rewritten = await declare(h, instance_id, timeline_id, quiet, intent="ok")
        dropped = await declare(h, instance_id, timeline_id, quiet, intent="ok")
        abandoned = await h.mgmt.call("trpg.action.abandon", instance_id=instance_id, timeline_id=timeline_id,
                                      campaign_id=quiet, action_id=str(dropped["action_id"]),
                                      reason="换条路")
        expect("R2 无法裁定 / 放弃",
               "待补充不推进世界、不阻止改写；放弃只终结行动",
               stuck_result["status"] == "awaiting_choice" and rewritten["status"] == "confirmed"
               and abandoned["status"] == "abandoned",
               f"待补充={stuck_result['status']}；改写={rewritten['status']}；放弃={abandoned['status']}",
               "§七：无法裁定不是失败；放弃不写世界")


def main(argv: list[str] | None = None) -> int:
    global ONLY
    parser = argparse.ArgumentParser(description="TRPG 规则层审计探针")
    parser.add_argument("--only", default="", help="只跑包含该关键字的检查")
    parser.add_argument("--json", action="store_true", help="把读数写成 JSON")
    ns = parser.parse_args(argv)
    ONLY = ns.only
    import _audit2_rulecommon as common

    common.ONLY = ONLY

    asyncio.run(run_all())
    code = report()

    audit_dir = ROOT / ".hermes/audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = audit_dir / f"trpglayer_{stamp}{'_json' if ns.json else ''}.txt"
    payload = json.dumps(RESULTS, ensure_ascii=False, indent=2) if ns.json else "\n".join(
        f"[{item['status']:8}] {item['clause']} :: {item['observed']}\n        期望：{item['expected']}\n"
        f"        判据：{item['evidence']}" for item in RESULTS
    )
    target.write_text(payload, encoding="utf-8")
    print(f"读数已存：{target.relative_to(ROOT).as_posix()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
