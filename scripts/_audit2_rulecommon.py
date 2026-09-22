"""TRPG 规则共用模块（TRPG_RULE_COMMON_MODULE_SPEC）行为级审计探针。

问题：公共层是「真边界」还是「文档加一层转发」？

判法（真 WebSocket + 真 SQLite + 真插件子进程，只把 LLM 换成 FakeLLM）：

- A 段：规范 ⇄ 常数双向对照（§5.1 类别表 / §5.2 首版映射 / 首版不支持项 / 三种状态）；
- B 段：逐条跑 §十一 行为验收表（11 行），读数来自 resolve / commit 状态、待审分账、
  规则状态 revision、世界事件与效果、时钟水位与场景待选择；
- C 段：跨层对照——三条路径（战役行动 / GM 直接变化 / B0 兼容）在同一个模块上过闸，
  来源与版本只由宿主给。

只读项目代码；数据写在临时目录；不需要联网（FakeLLM）。
用法：`.venv/Scripts/python.exe scripts/_audit2_rulecommon.py [--only 关键字] [--json]`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.client import MgmtClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402
from isekai_core.runtime import change as change_mod  # noqa: E402
from isekai_core.runtime import rule_common  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
#: 每个实例的真实角色卡标识：行动者 / 参与者 / 规则后果的主体都用它——
#: 用不存在的标识当后果目标是「目标未登记」，那是另一种用例（B04）
ACTORS: dict[str, str] = {}
TIDE = ROOT / "examples" / "tide_rules_plugin"
RESULTS: list[dict[str, Any]] = []
ONLY = ""

PLUGIN_SOURCE = '''
import json, sys

request = json.loads(sys.stdin.readline())
mode = str(request.get("intent") or "ok")
state = request.get("rule_state") or {}
base = int(state.get("state_revision") or 0)
patch = {
    "ruleset_id": state.get("ruleset_id"),
    "base_state_revision": base if mode != "bad_base" else base + 5,
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
elif mode == "bad_target":
    consequences = []
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
elif mode == "time_choice":
    consequences = [
        {"kind": "condition", "operation": "create", "target_refs": ["off-1"], "value": "封锁",
         "expiry": "with_cause", "certainty": "confirmed"},
        {"kind": "player_choice", "operation": "create", "choice_id": "ch-1",
         "value": ["追上去", "先撤退"], "audience": "public_party", "certainty": "confirmed"},
        {"kind": "time_advance", "operation": "advance",
         "value": {"seconds": 600, "cause": "追查去向"}, "certainty": "confirmed"},
    ]
elif mode == "unseen":
    consequences = []
answer = {
    "resolution": {"system": "audit-probe", "outcome": mode, "private": {"roll": 7}},
    "rule_state_patch": patch,
    "consequences": consequences,
    "scene_transition": transition,
    "claims": claims,
    "participants": ["pc-1"],
}
if mode == "bad_target":
    answer = {"resolution": {"system": "audit-probe", "outcome": mode},
              "effects": [{"kind": "institution_state", "target": "off-999", "value": "vacant",
                           "expiry": "until_cleared", "certainty": "confirmed"}]}
if mode == "half":
    answer = {"error": {"kind": "rejected", "message": "带半成品"},
              "consequences": consequences,
              "rule_state_patch": patch}
if mode == "unseen":
    answer = {"resolution": {"system": "audit-probe", "outcome": mode}, "effects": [
        {"kind": "institution_state", "target": "off-1", "value": "vacant",
         "expiry": "until_cleared", "certainty": "candidate"}]}
print(json.dumps(answer, ensure_ascii=False), flush=True)
'''


def spec_text(name: str) -> str:
    hits = [path for path in (ROOT / "docs").rglob(name)]
    return hits[0].read_text(encoding="utf-8") if hits else ""


def check(clause: str, expected: str, observed: str, status: str, evidence: str = "") -> None:
    RESULTS.append({"clause": clause, "status": status, "expected": expected, "observed": observed,
                    "evidence": evidence})
    print(f"[{status:8}] {clause} :: {observed[:170]}")


def only_matches(clause: str) -> bool:
    return not ONLY or ONLY in clause


def ok(clause: str, expected: str, evidence: str) -> None:
    if only_matches(clause):
        check(clause, expected, "PASS", "PASS", evidence)


def fail(clause: str, expected: str, observed: str, evidence: str = "") -> None:
    if only_matches(clause):
        check(clause, expected, observed, "FAIL", evidence)


def expect(clause: str, expected: str, condition: bool, observed: str, evidence: str = "") -> None:
    if not only_matches(clause):
        return
    check(clause, expected, "PASS" if condition else observed,
          "PASS" if condition else "FAIL", evidence)


@asynccontextmanager
async def core() -> AsyncIterator[Any]:
    """临时根目录里跑真核心（真 WS + 真 SQLite），只换 LLM。"""
    with tempfile.TemporaryDirectory(prefix="rulecommon-audit-") as tmp:
        folder = Path(tmp) / "config"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config.yaml").write_text("runtime:\n  sleep_wait_min_s: 0.05\n  sleep_wait_max_s: 0.05\n",
                                            encoding="utf-8")
        cfg = load_config(tmp)
        runtime = await build_runtime(cfg, llm=FakeLLM(["收到。"]))
        endpoint = await runtime.server.start()
        mgmt = MgmtClient(endpoint, runtime.server.mgmt_token)
        await mgmt.connect()
        try:
            yield SimpleNamespace(cfg=cfg, store=runtime.store, world=runtime.world,
                                  service=runtime.service, mgmt=mgmt, root=Path(tmp))
        finally:
            await mgmt.close()
            await runtime.service.shutdown()
            await runtime.server.close()
            runtime.store.close()


def make_plugin(root: Path, source: str = PLUGIN_SOURCE) -> Path:
    folder = root / "rules"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "main.py").write_text(source, encoding="utf-8")
    manifest = folder / "manifest.json"
    manifest.write_text(json.dumps({
        "id": "fake-rules", "name": "audit probe", "version": "1.0",
        "protocol": "isekai.trpg.rules/1", "entry": [sys.executable, "main.py"],
    }), encoding="utf-8")
    return manifest


async def line(h: Any, *, moment: int = DAY * 1500 + 30000) -> tuple[str, str, str]:
    """真实例 + 激活 + 返回 (instance_id, timeline_id, card_id)。"""
    package = example_package("灰潮纪", moment=moment)
    card = example_card(package, name="堤禾")
    info = create_instance(h.store, package, [card])
    instance_id = str(info["id"])
    timeline_id = str(h.store.timeline_list(instance_id)[0]["id"])
    h.world.ensure_instance(instance_id, now_real=time.time())
    h.world.activate(instance_id, timeline_id, now_real=time.time())
    asset = str(card["meta"]["card_id"])
    ACTORS[instance_id] = asset
    return instance_id, timeline_id, asset


async def campaign(h: Any, instance_id: str, timeline_id: str, manifest: Path, *,
                   ruleset_id: str = "fake-rules", version: str = "1.0") -> str:
    created = await h.mgmt.call(
        "trpg.campaign.create", instance_id=instance_id, timeline_id=timeline_id,
        ruleset_id=ruleset_id, ruleset_version=version, plugin_manifest=str(manifest),
        participants=[ACTORS.get(instance_id, "card-1")],
        scene={"kind": "conflict", "location_refs": ["rl-1"]},
    )
    return str(created["campaign_id"])


async def step(h: Any, instance_id: str, timeline_id: str, campaign_id: str, manifest: Path, *,
               intent: str, key: str, commit: bool = True) -> tuple[dict, dict | None]:
    """声明 → 裁定 →（可选）提交，返回 (resolve 读数, commit 读数)。"""
    actor = ACTORS.get(instance_id, "card-1")
    declared = await h.mgmt.call(
        "trpg.action.declare", instance_id=instance_id, timeline_id=timeline_id,
        campaign_id=campaign_id, actor_id=actor, intent=intent, raw_text=intent,
        auto_confirm=True, target_refs=["off-1"],
    )
    resolved = await h.mgmt.call(
        "trpg.action.resolve", instance_id=instance_id, timeline_id=timeline_id,
        campaign_id=campaign_id, plugin_manifest=str(manifest), action_id=str(declared["action_id"]),
        actor_id=actor, intent=intent, timeout=60.0,
    )
    if not commit:
        return resolved, None
    committed = await h.mgmt.call(
        "trpg.commit", instance_id=instance_id, timeline_id=timeline_id, campaign_id=campaign_id,
        action_id=str(declared["action_id"]), idempotency_key=key,
    )
    return resolved, committed


def world_facts(h: Any, instance_id: str, timeline_id: str) -> list[dict]:
    return [row for row in h.store.event_window(instance_id, timeline_id, until=10**15, limit=500)
            if str(row["source"]).startswith("trpg") or row["source"] == "gm_declaration"]


def rules_state(h: Any, instance_id: str, timeline_id: str, campaign_id: str) -> dict:
    return h.store.trpg_get("rule_state", instance_id=instance_id, timeline_id=timeline_id,
                            campaign_id=campaign_id, ruleset_id="fake-rules") or {}


# ---------------------------------------------------------------- A 段：规范 ⇄ 常数

def static_facts() -> None:
    text = spec_text("TRPG_RULE_COMMON_MODULE_SPEC.md")
    block = text.split("### 5.1 变化意图类别", 1)[-1].split("### 5.2", 1)[0]
    names = set(re.findall(r"^\|\s*`([a-z_]+)`\s*\|", block, flags=re.M))
    same = names == set(rule_common.CONSEQUENCE_KINDS)
    expect("A1 后果类别闭集", "§5.1 表的 10 类 == CONSEQUENCE_KINDS", same,
           f"规范 {sorted(names)} ≠ 实现 {sorted(rule_common.CONSEQUENCE_KINDS)}",
           f"规范 {sorted(names)}")

    mapping = text.split("### 5.2 当前可提交映射", 1)[-1].split("```text", 1)[-1].split("```", 1)[0]
    declared = {line.split("->")[0].strip() for line in mapping.strip().splitlines()}
    covered = set(change_mod.EFFECT_BY_KIND) | {"state_change", "knowledge_change", "world_event"}
    expect("A2 首版映射覆盖", "§5.2 左列 == 实现里可映射的 kind", declared <= covered,
           f"规范 {sorted(declared)} 里有实现没覆盖的：{sorted(declared - covered)}",
           f"EFFECT_BY_KIND={change_mod.EFFECT_BY_KIND} STATE_KIND_BY_TARGET={change_mod.STATE_KIND_BY_TARGET}")

    refused = set(rule_common.REFUSED)
    expect("A3 首版不支持项", "resource_change / relation_change / 无主 clock_progress 都拒绝且带替代路径",
           {"resource_change", "relation_change", "clock_progress"} <= refused
           and all(str(value).strip() for value in rule_common.REFUSED.values()),
           f"REFUSED={sorted(refused)}", f"REFUSED 表：{rule_common.REFUSED}")

    routed = rule_common.normalize(
        {"resolution": {"system": "probe"},
         "consequences": [{"kind": "time_advance", "operation": "advance",
                           "value": {"seconds": 60, "cause": "探针"}, "certainty": "confirmed"}]},
        origin=rule_common.origin_block("in-1", "tl-1", action_ref="act-1"),
    )
    expect("A3b 时间消耗走独立请求",
           "time_advance 不落世界效果、也不回 rejected，而是联合时间请求",
           routed["status"] == "ready" and (routed["world_time_request"] or {}).get("seconds") == 60
           and routed["changes"] == [] and routed["payload"]["effects"] == [],
           f"status={routed['status']} request={routed['world_time_request']} effects={routed['payload']['effects']}",
           "§5.1：时间消耗转 runtime.time.consume / 联合时间请求")

    expect("A4 状态闭集", "只有 ready / rejected / needs_review",
           tuple(rule_common.STATUSES) == ("ready", "rejected", "needs_review"),
           str(rule_common.STATUSES), "rule_common.STATUSES")


# ---------------------------------------------------------------- B 段：§十一 行为验收

async def acceptance_rows() -> None:
    async with core() as h:
        plugin = make_plugin(h.root)

        # B01 两个差异化插件：真实例里同一份核心跑两套规则
        instance_id, timeline_id, _card = await line(h)
        fake_id = await campaign(h, instance_id, timeline_id, plugin)
        fake_resolved, fake_commit = await step(h, instance_id, timeline_id, fake_id, plugin,
                                               intent="ok", key="b01-fake")
        tide_instance, tide_line, _ = await line(h)
        tide_id = await campaign(h, tide_instance, tide_line, TIDE / "manifest.json",
                                 ruleset_id="tide", version="0.1.0")
        tide_resolved, tide_commit = await step(h, tide_instance, tide_line, tide_id,
                                                TIDE / "manifest.json", intent="推进", key="b01-tide")
        fake_state = await h.mgmt.call("trpg.rule_state.read", instance_id=instance_id,
                                       timeline_id=timeline_id, campaign_id=fake_id)
        tide_state = await h.mgmt.call("trpg.rule_state.read", instance_id=tide_instance,
                                       timeline_id=tide_line, campaign_id=tide_id)
        expect("B01 两个差异化插件",
               "两套规则各回自己的 resolution 与规则状态，公共层不要求共享模型",
               fake_commit["status"] == "committed" and tide_commit["status"] == "committed"
               and "hp" in json.dumps(fake_state["opaque_state"])
               and "stress" in json.dumps(tide_state["opaque_state"])
               and "stress" not in json.dumps(fake_state["opaque_state"])
               and set(fake_resolved["resolution"]) != set(tide_resolved["resolution"]),
               f"fake commit={fake_commit['status']} tide commit={tide_commit['status']} "
               f"{tide_commit.get('errors') or ''}；"
               f"fake 规则状态={fake_state['opaque_state']} tide 规则状态={tide_state['opaque_state']}；"
               f"fake resolution 键={sorted(fake_resolved['resolution'])} "
               f"tide resolution 键={sorted(tide_resolved['resolution'])}",
               "两套插件真进程；公共层只搬运 resolution 与规则状态")

        tide_view = await h.mgmt.call("trpg.scene.view", instance_id=tide_instance,
                                     timeline_id=tide_line, campaign_id=tide_id)
        tide_beat = (tide_view.get("scene") or {}).get("turn_state", {}).get("rule_time")
        expect("B01b 规则节拍与世界秒分开",
               "插件申报的规则时间落在场景 turn_state，不改世界时钟",
               tide_beat == 1,
               f"turn_state.rule_time={tide_beat}",
               "scene_transition.rule_time_delta → _apply_transition；世界时间只认显式请求")

        # B02 成功 / 代价 / 失败：已确认部分落成结构化后果，候选只能待审
        candidate_resolved, _ = await step(h, instance_id, timeline_id, fake_id, plugin,
                                           intent="candidate", key="b02", commit=False)
        tide_consequences = tide_declared(h, tide_instance, tide_line, tide_id)
        expect("B02 裁定结果的三档去处",
               "已确认 → change_intent；候选 → 待审（不落世界）",
               candidate_resolved["status"] == "needs_review"
               and any("certainty=candidate" in item["reason"] for item in candidate_resolved["pending"])
               and all(item.get("certainty") == "confirmed" for item in tide_consequences
                       if isinstance(item, dict)),
               f"候选读数={candidate_resolved['status']}；tide 已确认后果="
               f"{[item.get('kind') for item in tide_consequences if isinstance(item, dict)]}",
               "候选条目进 pending 而不是 effects")

        # B03 资源 / 关系：明确拒绝，不伪装成别的效果
        resource_resolved, _ = await step(h, instance_id, timeline_id, fake_id, plugin,
                                          intent="resource", key="b03", commit=False)
        relation_resolved, _ = await step(h, instance_id, timeline_id, fake_id, plugin,
                                          intent="relation", key="b03b", commit=False)
        expect("B03 未映射结果诚实拒绝",
               "resource_change / relation_change → rejected 并给替代路径",
               resource_resolved["status"] == "rejected" and relation_resolved["status"] == "rejected"
               and "扩闭集" in json.dumps(relation_resolved["rejected"], ensure_ascii=False),
               f"{resource_resolved['status']} / {relation_resolved['status']}",
               json.dumps([resource_resolved["rejected"], relation_resolved["rejected"]],
                          ensure_ascii=False))

        # B04 目标错误：整批不落半条（规则状态与世界后果不分裂）
        before_events = len(world_facts(h, instance_id, timeline_id))
        _, bad_commit = await step(h, instance_id, timeline_id, fake_id, plugin,
                                   intent="bad_target", key="b04")
        expect("B04 目标 / 闭集错误整批拒绝",
               "提交边界拒整批，规则状态与世界一条都不落",
               bad_commit["status"] == "needs_review"
               and int(rules_state(h, instance_id, timeline_id, fake_id)["state_revision"]) == 1
               and len(world_facts(h, instance_id, timeline_id)) == before_events,
               f"commit={bad_commit['status']} state_revision="
               f"{int(rules_state(h, instance_id, timeline_id, fake_id)['state_revision'])}",
               f"事件数 {before_events} → {len(world_facts(h, instance_id, timeline_id))}")

        # B05 claims 与认知：说法要来源，不能反向造事实
        claim_bad, _ = await step(h, instance_id, timeline_id, fake_id, plugin,
                                  intent="claim_bad", key="b05", commit=False)
        resolved, committed = await step(h, instance_id, timeline_id, fake_id, plugin,
                                         intent="claim_ok", key="b05b")
        claims = h.store.claim_list(instance_id, timeline_id)
        landed = [row for row in claims if row["text"] == "岗位的封条换了新的"]
        expect("B05 claims 与认知边界",
               "未声明的说法不能让；声明过的保留渠道，且说法不进效果",
               claim_bad["status"] == "needs_review" and committed["status"] == "committed"
               and len(landed) == 1 and landed[0]["source_id"] == "src-1"
               and committed["effects"] == 1,
               f"未声明={claim_bad['status']}；落库说法={len(landed)} 渠道="
               f"{landed[0]['source_id'] if landed else '（无）'}；效果数={committed['effects']}",
               "knowledge_change 引用的说法必须在 claims[] 里")

        # B06 GM 直接变化：不伪造骰点、不造行动行，但过同一套检查
        gm_actions_before = len(h.store.trpg_list("action", instance_id=instance_id,
                                                  timeline_id=timeline_id))
        gm_ok = await h.mgmt.call("trpg.gm.change", instance_id=instance_id, timeline_id=timeline_id,
                                  campaign_id=fake_id, idempotency_key="b06",
                                  changes={"consequences": [{
                                      "kind": "state_change", "operation": "set",
                                      "target_refs": ["off-1"], "value": "occupied",
                                      "expiry": "until_cleared", "certainty": "confirmed"}]})
        gm_bad = await h.mgmt.call("trpg.gm.change", instance_id=instance_id, timeline_id=timeline_id,
                                   campaign_id=fake_id, idempotency_key="b06b",
                                   changes={"consequences": [{
                                       "kind": "relation_change", "operation": "change",
                                       "target_refs": ["off-1"], "value": "敌意",
                                       "certainty": "confirmed"}]})
        sources = {str(row["source"]) for row in world_facts(h, instance_id, timeline_id)}
        expect("B06 GM 直接变化",
               "来源 gm_declaration、无行动行、同一套后果检查",
               gm_ok["status"] == "committed" and gm_bad["status"] == "rejected"
               and "gm_declaration" in sources
               and len(h.store.trpg_list("action", instance_id=instance_id, timeline_id=timeline_id))
               == gm_actions_before,
               f"GM={gm_ok['status']} / 拒绝={gm_bad['status']}；事件来源={sorted(sources)}",
               "gm.change 与行动路径共用 _joint_apply")

        # B07 插件错误半成品：一字不采信
        half, _ = await step(h, instance_id, timeline_id, fake_id, plugin,
                             intent="half", key="b07", commit=False)
        expect("B07 错误响应夹带半成品",
               "状态进待审，规则状态与世界留原样",
               half["status"] == "awaiting_gm_review"
               and int(rules_state(h, instance_id, timeline_id, fake_id)["state_revision"]) == 2
               and "不采信" in json.dumps(half["errors"], ensure_ascii=False),
               f"状态={half['status']}；错误={half['errors']}",
               "ERROR_FORBIDDEN_KEYS 拦 rule_state_patch / consequences / effects")

        # B08 版本 / revision 冲突
        _, conflict = await step(h, instance_id, timeline_id, fake_id, plugin,
                                 intent="bad_base", key="b08")
        expect("B08 版本 / revision 冲突",
               "返回 conflict，规则状态与世界都不动",
               conflict["status"] == "conflict"
               and int(rules_state(h, instance_id, timeline_id, fake_id)["state_revision"]) == 2,
               f"{conflict['status']} / 当前 revision="
               f"{int(rules_state(h, instance_id, timeline_id, fake_id)['state_revision'])}",
               "base_state_revision 与快照不一致 → conflict")

        # B09 幂等重试
        again = await h.mgmt.call("trpg.commit", instance_id=instance_id, timeline_id=timeline_id,
                                   campaign_id=fake_id, action_id="", idempotency_key="b06")
        expect("B09 幂等重试", "同键返回原结果，不重复世界变化",
               again["status"] == "duplicate" and again["joint_commit_id"] == gm_ok["joint_commit_id"],
               f"{again['status']} / {again.get('joint_commit_id')}",
               "trpg_commit 账本按幂等键回放")

        # B10 时间与待选择
        clock_before = int((await h.mgmt.call("runtime.clock", instance_id=instance_id,
                                              timeline_id=timeline_id))["clock"]["processed_world"])
        _, timed = await step(h, instance_id, timeline_id, fake_id, plugin,
                              intent="time_choice", key="b10")
        after_clock = int((await h.mgmt.call("runtime.clock", instance_id=instance_id,
                                             timeline_id=timeline_id))["clock"]["processed_world"])
        facts = world_facts(h, instance_id, timeline_id)
        expect("B10 时间与待选择",
               "时间走独立请求并同批前移时钟；未选分支不进世界事实",
               timed["status"] == "committed" and timed["world_time_applied"] is True
               and timed["world_time_request"]["seconds"] == 600
               and after_clock >= clock_before + 600 and timed["open_choices"] == ["ch-1"]
               and not any("追上去" in json.dumps(row, ensure_ascii=False) for row in facts),
               f"时钟 {clock_before} → {after_clock}；待选择={timed['open_choices']}",
               "world_time_request 与规则状态同批；available_choices 只在战役侧")

        # B11 规则演进：私有字段留在插件结果里
        state = rules_state(h, instance_id, timeline_id, fake_id)
        stored = json.loads(str(state["opaque_state"]))
        expect("B11 规则演进", "插件私有 resolution / 规则状态原样保存，核心不解析",
               "actors" in stored and set(tide_resolved["resolution"]) >= {"system", "outcome", "pool"},
               f"规则状态键={sorted(stored)}；tide resolution 键={sorted(tide_resolved['resolution'])}",
               "公共层只搬运，不解释骰点与私有字段")

        # C 段：三条路径同一边界 + 来源与版本只由宿主给
        b0 = None
        try:
            await h.mgmt.call("trpg.action.resolve", instance_id=instance_id, timeline_id=timeline_id,
                              plugin_manifest=str(plugin), action_id="b0-candidate", actor_id="card-1",
                              intent="unseen")
            b0 = "accepted"
        except Exception as exc:  # noqa: BLE001 —— 就是要把拒绝文案读出来
            b0 = str(exc)
        expect("C01 三条路径共用同一个模块",
               "B0 兼容路径也过同一套检查（候选不再借兼容字段落成事实）",
               "certainty=candidate" in b0,
               f"B0 读数={b0[:120]}",
               "runtime/ops.py 的 B0 分支调 rule_common.normalize(campaign=False)")

        origin = rule_common.origin_block("in-1", "tl-1", campaign_id="cp-1", action_ref="act-1",
                                          source_mode="gm_declaration", expected_revision=5)
        normalized = rule_common.normalize(
            {"resolution": {"system": "x"}, "source_mode": "world_process", "campaign_id": "cp-evil",
             "consequences": [{"kind": "condition", "operation": "create", "target_refs": ["off-1"],
                               "value": "封锁", "expiry": "with_cause", "certainty": "confirmed"}]},
            origin=origin, package={})
        expect("C02 来源与版本只由宿主给",
               "插件自报的 source_mode / campaign_id 一概不采信",
               normalized["origin"]["campaign_id"] == "cp-1"
               and normalized["changes"][0]["source_mode"] == "gm_declaration"
               and normalized["changes"][0]["source_module"] == "trpg",
               json.dumps(normalized["origin"], ensure_ascii=False),
               "normalized.origin 来自 origin_block")


def tide_declared(h: Any, instance_id: str, timeline_id: str, campaign_id: str) -> list[Any]:
    """把潮汐插件这一步申报的后果从行动行里读回来（真进程声明，不是猜）。"""
    for row in h.store.trpg_list("action", instance_id=instance_id, timeline_id=timeline_id,
                                campaign_id=campaign_id):
        payload = json.loads(str(row.get("resolution") or "{}"))
        if payload.get("changes"):
            return list(payload["changes"])
    return []


def report() -> int:
    total = len([item for item in RESULTS if item["status"] != "SKIP"])
    passed = len([item for item in RESULTS if item["status"] == "PASS"])
    failed = [item for item in RESULTS if item["status"] == "FAIL"]
    print(f"\nTOTAL={total} PASS={passed} FAIL={len(failed)}")
    for item in failed:
        print(f"  FAIL {item['clause']}：期望 {item['expected']}；实测 {item['observed']}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    global ONLY
    parser = argparse.ArgumentParser(description="TRPG 规则共用模块审计探针")
    parser.add_argument("--only", default="", help="只跑包含该关键字的检查")
    parser.add_argument("--json", action="store_true", help="把读数写成 JSON")
    ns = parser.parse_args(argv)
    ONLY = ns.only

    static_facts()
    asyncio.run(acceptance_rows())
    code = report()

    audit_dir = ROOT / ".hermes/audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = audit_dir / f"rulecommon_{stamp}{'_json' if ns.json else ''}.txt"
    payload = json.dumps(RESULTS, ensure_ascii=False, indent=2) if ns.json else "\n".join(
        f"[{item['status']:8}] {item['clause']} :: {item['observed']}\n        期望：{item['expected']}\n"
        f"        判据：{item['evidence']}" for item in RESULTS
    )
    target.write_text(payload, encoding="utf-8")
    print(f"读数已存：{target.relative_to(ROOT).as_posix()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
