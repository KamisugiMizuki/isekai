"""WRITING_ASSISTANT_SPEC（编剧层）行为级审计探针。

问题：规范里那份「行为验收」表（§十二 十一行）与 §四 / §十 的分层约束，在代码里是**真行为**
还是只有文档与符号？

判法（真 WebSocket + 真 SQLite + 真实例，只把 LLM 换成 FakeLLM）：

- A 段：规范 ⇄ 常数双向对照（层级 / 状态 / 受众 / CLI 同名命令 / 分层归属 / 表主键）；
- B 段：逐条跑 §十二 的十一行场景，读数来自条目状态、候选生命周期、事件与效果条数、
  快照水位与世代、观众层的键集合；
- C 段：消费方事实——编剧层是不是走 WorldRuntime 的**公共接口**读世界、经 TRPG 规则层
  提交 GM 直接变化，而不是绕过边界自己写库。

只读项目代码；数据写在临时目录；不需要联网（FakeLLM）。
用法：`.venv/Scripts/python.exe scripts/_audit2_wa.py [--only 关键字] [--json]`
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.client import MgmtClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402
from isekai_core.ump import Err, UmpError  # noqa: E402
from isekai_core.world import ops as world_ops  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402
from isekai_core.world_cli import OP_BY_COMMAND  # noqa: E402
from isekai_core.writing import candidates as cand  # noqa: E402
from isekai_core.writing import outline as outline_mod  # noqa: E402
from isekai_core.writing.service import AUDIENCES, TRPG_AUDIENCE  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[dict[str, Any]] = []
ONLY = ""
SUGGESTION = json.dumps({"candidates": [
    {"title": "碑文残片", "summary": "退潮的盐滩上露出半块碑文", "outline_ref": "it-know",
     "unsolved": ["缺的那半写的是什么"]},
    {"title": "议会的信", "summary": "驿站转来一封没署名的信", "outline_ref": "", "unsolved": []},
]}, ensure_ascii=False)


def spec_text() -> str:
    hits = [path for path in (ROOT / "docs").rglob("WRITING_ASSISTANT_SPEC.md")]
    return hits[0].read_text(encoding="utf-8") if hits else ""


def check(clause: str, expected: str, observed: str, status: str, evidence: str = "", code_ref: str = "") -> None:
    RESULTS.append({"clause": clause, "status": status, "expected": expected, "observed": observed,
                    "evidence": evidence, "code_ref": code_ref})
    print(f"[{status:8}] {clause} :: {observed[:170]}")


def only_matches(clause: str) -> bool:
    return not ONLY or ONLY in clause


async def core() -> AsyncIterator[Any]:
    """临时根目录里跑真核心（真 WS + 真 SQLite），只换 LLM。"""
    with tempfile.TemporaryDirectory(prefix="wa-audit-") as tmp:
        folder = Path(tmp) / "config"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "config.yaml").write_text(
            "runtime:\n  sleep_wait_min_s: 0.05\n  sleep_wait_max_s: 0.05\n", encoding="utf-8"
        )
        cfg = load_config(tmp)
        fake = FakeLLM(["收到。"])
        fake.judgements["情节提议"] = SUGGESTION
        runtime = await build_runtime(cfg, llm=fake)
        endpoint = await runtime.server.start()
        mgmt = MgmtClient(endpoint, runtime.server.mgmt_token)
        await mgmt.connect()
        try:
            yield SimpleNamespace(cfg=cfg, store=runtime.store, world=runtime.world,
                                  service=runtime.service, fake=fake, mgmt=mgmt, root=Path(tmp))
        finally:
            await mgmt.close()
            await runtime.service.shutdown()
            await runtime.server.close()
            runtime.store.close()


async def line(h: Any, *, moment: int = DAY * 1500 + 30000) -> tuple[str, str, str, dict]:
    """真实例 + 激活 + 一份三层大纲 + 绑定。"""
    package = example_package("灰潮纪", moment=moment)
    card = example_card(package, name="堤禾")
    info = create_instance(h.store, package, [card])
    instance_id = info["id"]
    timeline_id = h.store.timeline_list(instance_id)[0]["id"]
    h.world.ensure_instance(instance_id, now_real=time.time())
    h.world.activate(instance_id, timeline_id, now_real=time.time())
    card_id = str(card["meta"]["card_id"])
    events = sorted(h.store.event_ids(instance_id, timeline_id))
    outline = {
        "id": "ol-1", "name": "潮汐志·第一卷",
        "items": [
            {"id": "it-know", "layer": "required_node", "title": "她得知道告警",
             "statement": "堤禾在第一章结束前知道那份告警的存在", "scope": "timeline",
             "success_criteria": "她的认知里出现告警相关内容", "watch_refs": events[:1],
             "alternatives": ["由旁人转述"]},
            {"id": "it-forbid", "layer": "forbidden", "title": "不许再崩堤",
             "statement": "北堤不得再次崩塌", "scope": "world",
             "success_criteria": "世界里没有新的崩堤事件", "watch_refs": events[1:2]},
            {"id": "it-theme", "layer": "theme", "title": "盐味与旧账",
             "statement": "主题围绕记住与遗忘", "scope": "world", "success_criteria": "读者能说出主题"},
            {"id": "it-late", "layer": "required_node", "title": "到点没发生的节点",
             "statement": "第三章之前拿到旧账本", "scope": "chapter",
             "success_criteria": "账本出现在她的经历里", "watch_refs": ["ev-none"], "deadline_world": 1},
        ],
    }
    await h.mgmt.call("wa.outline.save", outline=outline)
    await h.mgmt.call("wa.bind", instance_id=instance_id, timeline_id=timeline_id,
                      outline_id="ol-1", observers=[card_id], chapter="第一章")
    return instance_id, timeline_id, card_id, outline


def counts(h: Any, instance_id: str, timeline_id: str) -> dict[str, int]:
    return {
        "events": len(h.store.event_ids(instance_id, timeline_id)),
        "effects": len(h.store.effect_active_ids(instance_id, timeline_id)),
        "claims": len(h.store.claim_list(instance_id, timeline_id)),
    }


def change_intent(card_id: str, value: str = "封堤") -> list[dict[str, Any]]:
    return [{"id": "c-1", "kind": "condition", "operation": "set", "certainty": "confirmed",
             "target_refs": [card_id], "value": value, "expiry": "until_cleared"}]


# ------------------------------------------------------------------ A 段：规范 ⇄ 常数


def static_facts() -> None:
    spec = spec_text()
    if not spec:
        check("A0 规范在库", "docs/**/WRITING_ASSISTANT_SPEC.md 存在", "找不到规范文件", "FAIL")
        return

    if only_matches("A1 六个层级"):
        wanted = {"theme": "主题", "required_node": "必达", "forbidden": "禁止",
                  "character_arc": "弧线", "pacing": "节奏", "variable_material": "可变素材"}
        missing_spec = [label for label in wanted.values() if label not in spec]
        missing_code = [name for name in wanted if name not in outline_mod.LAYERS]
        check("A1 六个层级", "规范点名的六个层级与 outline.LAYERS 一致",
              f"规范缺 {missing_spec or '（无）'}；代码缺 {missing_code or '（无）'}",
              "PASS" if not missing_spec and not missing_code else "FAIL",
              evidence="层级是闭集，条目必须落在其中一层（§三）", code_ref="isekai_core/writing/outline.py LAYERS")

    if only_matches("A2 条目状态机"):
        wanted = {"unstarted", "in_progress", "achieved", "deviated", "abandoned"}
        closed = set(outline_mod.STATUSES) == wanted
        no_back = "unstarted" not in outline_mod.TRANSITIONS["achieved"]
        no_revive = not outline_mod.TRANSITIONS["abandoned"]
        check("A2 条目状态机", "五个状态是闭集，回退与复活被挡住（达成后不能改回未开始）",
              f"状态={list(outline_mod.STATUSES)}；achieved→{list(outline_mod.TRANSITIONS['achieved'])}；"
              f"abandoned→{list(outline_mod.TRANSITIONS['abandoned'])}",
              "PASS" if closed and no_back and no_revive else "FAIL",
              evidence="状态迁移是产品语义，不是随便写的字符串", code_ref="isekai_core/writing/outline.py TRANSITIONS")

    if only_matches("A3 候选生命周期"):
        wanted = ("proposed", "selected", "approved", "committed", "rejected", "deferred", "stale")
        code_ok = set(cand.STATES) == set(wanted)
        uncommitted = set(cand.UNCOMMITTED)
        check("A3 候选生命周期", "候选状态是闭集，approved 属于「未提交」",
              f"状态={list(cand.STATES)}；未提交={sorted(uncommitted)}",
              "PASS" if code_ok and "approved" in uncommitted and "committed" not in uncommitted else "FAIL",
              evidence="「批准采用」不等于「世界已经改变」（§4.3）",
              code_ref="isekai_core/writing/candidates.py STATES / UNCOMMITTED")

    if only_matches("A4 受众闭集"):
        spec_ok = all(word in spec for word in ("玩家观察", "主持依据", "下一步编排"))
        check("A4 受众闭集", "受众是闭集；三层输出（玩家观察 / 主持依据 / 下一步编排）都在",
              f"代码受众={list(AUDIENCES)}；TRPG 受众翻译={TRPG_AUDIENCE}；规范三处提法齐={spec_ok}",
              "PASS" if set(AUDIENCES) == {"player", "gm", "author"} and spec_ok else "FAIL",
              evidence="两套受众词表不同名，翻译必须显式（曾经把 gm 直接塞给规则层被拒）",
              code_ref="isekai_core/writing/service.py AUDIENCES / TRPG_AUDIENCE")

    if only_matches("A5 CLI 同名命令"):
        ops = [op for (_group, _cmd), op in OP_BY_COMMAND.items() if op.startswith("wa.")]
        registered = {name for name in dir(world_ops) if name.startswith("WA_")}
        expected = [name for name in (
            "wa.outline.save", "wa.outline.list", "wa.outline.get", "wa.bind", "wa.state",
            "wa.evaluate", "wa.item.decide", "wa.observe", "wa.suggest", "wa.candidate.propose",
            "wa.candidate.decide", "wa.candidate.commit", "wa.gm.declare", "wa.gm.approve", "wa.branch",
        )]
        missing = [name for name in expected if name not in ops]
        # 解析器真的能建起来：新增参数撞名（如两个 --observer）在映射表里看不出来，
        # 只有 argparse 构造时才炸——`--help` 是这条链路最小的真检查。
        parsed = subprocess.run(
            [sys.executable, "-m", "isekai_core.world_cli", "--help"],
            cwd=str(ROOT), capture_output=True, text=True, encoding="utf-8",
        )
        parser_ok = parsed.returncode == 0 and "wa" in (parsed.stdout or "")
        check("A5 CLI 同名命令", "十五个管理面 op 都有同名 CLI 命令，且解析器能真的建起来",
              f"CLI 里 {len(ops)} 条；缺 {missing or '（无）'}；ops 里 WA_* 记号 {sorted(registered)}；"
              f"`--help` 可跑={parser_ok}",
              "PASS" if not missing and parser_ok else "FAIL",
              evidence="CLI 只是第二入口，op 才是产品面；参数撞名只有真建解析器才会暴露",
              code_ref="isekai_core/world_cli.py OP_BY_COMMAND")

    if only_matches("A6 分层归属"):
        world_writers = ("event_put", "claim_put", "effect_put", "knowledge_put", "experience_put",
                         "commit_put", "clock_set", "apply_runtime_batch", "knowledge_add")
        offenders: list[str] = []
        for path in sorted((ROOT / "isekai_core/writing").glob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            for name in world_writers:
                if name in text:
                    offenders.append(f"{path.name}:{name}")
        allowed = ("wa_outline_put", "wa_state_put", "wa_candidate_put", "wa_outline_get", "wa_state_get",
                   "wa_candidate_get", "wa_outline_list", "wa_candidate_list", "wa_state_list")
        write_calls = [line.strip() for line in
                       (ROOT / "isekai_core/writing/service.py").read_text(encoding="utf-8",
                                                                          errors="replace").splitlines()
                       if re.search(r"store\.\w+\(", line)]
        foreign = [line for line in write_calls
                   if not any(name in line for name in allowed)
                   and re.search(r"store\.(\w+)\(", line).group(1) not in (
                       "instance_get", "timeline_get", "claim_list", "event_ids", "effect_active_ids",
                       "commit_snapshot_get", "clock_get", "card_of", "trpg_commit_by_key")]
        check("A6 分层归属", "编剧层不写世界库：世界写入只经 WorldRuntime / 规则层的公共入口",
              f"越界写方法={offenders or '（无）'}；可疑 store 调用={foreign or '（无）'}",
              "PASS" if not offenders and not foreign else "FAIL",
              evidence="大纲与候选都不是事实，不能自己动世界表（§十-1/2）",
              code_ref="isekai_core/writing/service.py")

    if only_matches("A7 表归属"):
        src = (ROOT / "isekai_core/store.py").read_text(encoding="utf-8", errors="replace")
        state_pk = "PRIMARY KEY(instance_id, timeline_id, outline_id)" in src
        cand_pk = "PRIMARY KEY(instance_id, timeline_id, id)" in src
        outline_pk = ("CREATE TABLE IF NOT EXISTS wa_outline" in src
                      and "id TEXT PRIMARY KEY" in src)
        delete_wired = all(name in src for name in ('"wa_state"', '"wa_candidate"'))
        check("A7 表归属", "大纲定义是作者资产；达成状态与候选按实例 + 时间线独立",
              f"wa_state 主键={state_pk}；wa_candidate 主键={cand_pk}；wa_outline 定义表={outline_pk}；"
              f"随实例删除={delete_wired}",
              "PASS" if state_pk and cand_pk and outline_pk and delete_wired else "FAIL",
              evidence="同一份大纲在新分支上要有独立的达成与偏离记录（§4.1）",
              code_ref="isekai_core/store.py wa_* 三张表")

    if only_matches("A8 不做世界线合并"):
        svc = (ROOT / "isekai_core/writing/service.py").read_text(encoding="utf-8", errors="replace")
        ok = "不提供世界线合并" in svc and "不提供世界线合并" in spec
        check("A8 不做世界线合并", "分支试演只读不合并，文案与规范一致",
              f"代码与规范同时出现={ok}", "PASS" if ok else "FAIL",
              evidence="试演的价值是看后果，不是把两条线拼起来", code_ref="isekai_core/writing/service.py trial_branch")


# ------------------------------------------------------------------ B 段：§十二 十一行


async def acceptance_rows() -> None:
    async for h in core():
        instance_id, timeline_id, card_id, _outline = await line(h)

        # --- §十二 第一行：新建大纲
        if only_matches("B1 新建大纲"):
            saved = await h.mgmt.call("wa.outline.get", outline_id="ol-1")
            fields = {"id", "layer", "strength", "scope", "preconditions", "success_criteria",
                       "alternatives", "status", "watch_refs", "deadline_world"}
            ok_all = all(fields <= set(item) for item in saved["outline"]["items"])
            strengths = {item["layer"]: item["strength"] for item in saved["outline"]["items"]}
            try:
                await h.mgmt.call("wa.outline.save", outline={
                    "id": "ol-bad", "name": "坏",
                    "items": [{"id": "i1", "layer": "nowhere", "statement": "x",
                               "scope": "world", "success_criteria": "y"}]})
                rejected = "没有拒绝"
            except UmpError as exc:
                rejected = str(exc)
            check("B1 新建大纲", "条目字段齐全、层级闭集、非法大纲不落盘",
                  f"字段齐={ok_all}；强度默认={strengths.get('required_node')}/{strengths.get('theme')}；"
                  f"越界层级被拒={('层级不在闭集' in rejected)}",
                  "PASS" if ok_all and "层级不在闭集" in rejected
                  and strengths.get("required_node") == "hard" else "FAIL",
                  evidence=f"拒绝文案：{rejected[:80]}", code_ref="wa.outline.save")

        # --- §十二 第二行：只读观察
        if only_matches("B2 只读观察"):
            before = counts(h, instance_id, timeline_id)
            seen = await h.mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                     observer_id=card_id, outline_id="ol-1", audience="author")
            text = json.dumps(seen, ensure_ascii=False)
            materials = (seen.get("player_view") or {}).get("materials") or []
            h.world.freeze(instance_id, timeline_id)
            frozen = await h.mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                       observer_id=card_id, outline_id="ol-1", audience="author")
            check("B2 只读观察", "只给该视角能获得的材料；冻结线不拿旧状态冒充当前；观察是只读的",
                  f"材料 {len(materials)} 条；未获知说法泄露={'崩堤当夜曾有人登堤敲钟' in text}；"
                  f"冻结返回={frozen['status']}({frozen.get('reason', '')[:24]})；世界条数不变={counts(h, instance_id, timeline_id) == before}",
                  "PASS" if materials and "崩堤当夜曾有人登堤敲钟" not in text
                  and frozen["status"] == "not_ready" and counts(h, instance_id, timeline_id) == before else "FAIL",
                  evidence=f"材料带来源与态度：{materials[0].get('source', '')} / {materials[0].get('stance', '')}"
                  if materials else "没有材料",
                  code_ref="wa.observe + cognition.knowledge_slice")
            h.world.activate(instance_id, timeline_id, now_real=time.time())

        # --- §十二 第三行：未达成硬约束
        if only_matches("B3 未达成硬约束"):
            before = counts(h, instance_id, timeline_id)
            report = await h.mgmt.call("wa.evaluate", instance_id=instance_id, timeline_id=timeline_id,
                                       outline_id="ol-1")
            kinds = sorted({gap["kind"] for gap in report["gaps"]})
            late = [item for item in report["items"] if item["id"] == "it-late"][0]
            check("B3 未达成硬约束", "报告缺口；不自动制造世界事实；不把条目标成达成",
                  f"缺口={kinds}；it-late 状态={late['status']}；世界条数不变={counts(h, instance_id, timeline_id) == before}",
                  "PASS" if "required_missing" in kinds and late["status"] != "achieved"
                  and counts(h, instance_id, timeline_id) == before else "FAIL",
                  evidence=f"评估水位 {report['evaluated_world']}（= 当前 processed_world）",
                  code_ref="wa.evaluate")

        # --- §十二 第四行 / 第十行：候选批准与快照过期
        if only_matches("B4 候选批准不是世界变化"):
            before = counts(h, instance_id, timeline_id)
            await h.mgmt.call("wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                              outline_id="ol-1", ref="cd-text", kind="text", item_refs=["it-theme"],
                              title="开场", summary="盐滩上的清晨", unsolved=["先不先提告警"])
            approved = await h.mgmt.call("wa.candidate.decide", instance_id=instance_id,
                                         timeline_id=timeline_id, ref="cd-text", status="approved",
                                         reason="采用这段开场", text="退潮后的盐滩像一张没写完的账页。")
            await h.mgmt.call("wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                              outline_id="ol-1", ref="cd-change", kind="world_change", changes=change_intent(card_id))
            await h.mgmt.call("wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                              ref="cd-change", status="approved", reason="批准采用")
            mid = counts(h, instance_id, timeline_id)
            committed = await h.mgmt.call("wa.candidate.commit", instance_id=instance_id,
                                          timeline_id=timeline_id, ref="cd-change", idempotency_key="wa-b4")
            after = counts(h, instance_id, timeline_id)
            check("B4a 批准不是已提交", "approved 的候选仍标未提交，世界条数不变",
                  f"approved.uncommitted={approved['candidate']['uncommitted']}；"
                  f"承诺文案={approved['candidate']['must_not_imply']}；批准后世界不变={mid == before}",
                  "PASS" if approved["candidate"]["uncommitted"] and mid == before
                  and approved["candidate"]["must_not_imply"] == "世界已经按它变了" else "FAIL",
                  evidence="文本候选只形成草稿，不推世界", code_ref="wa.candidate.decide")
            check("B4b 提交成功才动世界", "commit 返回 ok 且事件 +1，候选转 committed",
                  f"提交返回={committed['status']}；事件 {before['events']}→{after['events']}；"
                  f"候选={committed['candidate']['status']}；事件引用={committed['event_refs']}",
                  "PASS" if committed["status"] == "ok" and after["events"] == before["events"] + 1
                  and committed["candidate"]["status"] == "committed" else "FAIL",
                  evidence="候选里的世界变化只能由 change.commit 变成事实（§4.3）",
                  code_ref="wa.candidate.commit")

        # --- §十二 第五行：事实 / 因果冲突
        if only_matches("B5 事实与因果冲突"):
            refused = await h.mgmt.call("wa.candidate.propose", instance_id=instance_id,
                                        timeline_id=timeline_id, outline_id="ol-1", ref="cd-bad",
                                        kind="world_change",
                                        changes=[{"id": "c-9", "kind": "resource_change", "operation": "add",
                                                  "certainty": "confirmed", "value": 10}])
            soft = await h.mgmt.call("wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                                     outline_id="ol-1", ref="cd-soft", kind="world_change",
                                     changes=[{"id": "c-10", "kind": "condition", "operation": "set",
                                               "certainty": "candidate", "target_refs": [card_id], "value": "犹豫"}])
            await h.mgmt.call("wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                              ref="cd-soft", status="approved", reason="看提交时怎么说")
            blocked = await h.mgmt.call("wa.candidate.commit", instance_id=instance_id,
                                        timeline_id=timeline_id, ref="cd-soft")
            check("B5 事实与因果冲突", "翻不成事实效果的进 rejected / needs_review，并给出要改的依据",
                  f"越界效果→{refused['status']}（{refused['reason'][:40]}）；"
                  f"待确认→{soft['status']}（{soft['reason'][:40]}）；提交时={blocked['status']}",
                  "PASS" if refused["status"] == "rejected" and soft["status"] == "proposed"
                  and blocked["status"] == "needs_review" else "FAIL",
                  evidence="待确认内容不能直接提交为世界事实（§5.2）", code_ref="wa.candidate.propose / commit")

        # --- §十二 第六行：GM 直接变化
        if only_matches("B6 GM 直接变化"):
            campaign = await h.mgmt.call("trpg.campaign.create", instance_id=instance_id,
                                         timeline_id=timeline_id, ruleset_id="wa-probe", status="active")
            before = counts(h, instance_id, timeline_id)
            # 声明用**变化意图**形态（§3.7 / §5.1：`consequences` 收 state_change 一类意图，
            # 世界效果名只能进 `effects` 兼容字段）——旧夹具把效果名写进 consequences，会被拒。
            declared = await h.mgmt.call(
                "wa.gm.declare", instance_id=instance_id, timeline_id=timeline_id, ref="gm-1",
                campaign=str(campaign["campaign_id"]), display_name="堤长去职",
                gm_changes={"consequences": [{"kind": "state_change", "operation": "set",
                                              "target_refs": ["off-1"], "value": "vacant",
                                              "expiry": "until_cleared", "certainty": "confirmed"}],
                            "claims": [{"text": "堤长的位置空了出来", "source_id": "src-1", "audience": "公开"}]},
                basis={"fact": "议席推举未定"})
            mid = counts(h, instance_id, timeline_id)
            done = await h.mgmt.call("wa.gm.approve", instance_id=instance_id, timeline_id=timeline_id,
                                     ref="gm-1", idempotency_key="wa-gm-1")
            after = counts(h, instance_id, timeline_id)
            ledger = h.store.trpg_commit_by_key(instance_id, timeline_id, "wa-gm-1")
            # 负例：世界效果名写进 `consequences` 要被拒并指出去处（判据跟着规则共用模块的新契约走）
            old_shape = ""
            try:
                await h.mgmt.call(
                    "wa.gm.declare", instance_id=instance_id, timeline_id=timeline_id, ref="gm-old",
                    campaign=str(campaign["campaign_id"]), display_name="旧形态",
                    gm_changes={"consequences": [{"kind": "institution_state", "target": "off-1",
                                                  "value": "vacant", "expiry": "until_cleared",
                                                  "certainty": "confirmed"}]},
                    basis={"fact": "旧形态"})
                old_done = await h.mgmt.call("wa.gm.approve", instance_id=instance_id, timeline_id=timeline_id,
                                             ref="gm-old", idempotency_key="wa-gm-old")
                old_shape = json.dumps(old_done, ensure_ascii=False)
            except UmpError as exc:
                old_shape = str(exc)
            check("B6 GM 直接变化", "声明只是待批准结构；批准后经规则层联合提交落世界，且不制造行动行",
                  f"声明状态={declared['candidate']['status']}（未提交={declared['candidate']['uncommitted']}）；"
                  f"声明不改世界={mid == before}；批准→{done['status']}；联合提交账本={bool(ledger)}；"
                  f"效果 {before['effects']}→{after['effects']}；行动行={len(h.store.trpg_list('action', instance_id=instance_id, timeline_id=timeline_id))}；"
                  f"旧形态被拒={'rejected' in old_shape and 'effects' in old_shape}（{old_shape[:80]}）",
                  "PASS" if declared["candidate"]["status"] == "proposed" and mid == before
                  and done["status"] == "committed" and ledger and after != before
                  and "rejected" in old_shape and "effects" in old_shape else "FAIL",
                  evidence=f"承诺文案：{declared['must_not_imply']}", code_ref="wa.gm.declare / wa.gm.approve")

        # --- §十二 第七行：玩家行动结果
        if only_matches("B7 玩家行动结果"):
            await h.mgmt.call("wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                              outline_id="ol-1", ref="cd-2", kind="world_change",
                              changes=change_intent(card_id, "守夜"))
            await h.mgmt.call("wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                              ref="cd-2", status="approved", reason="采用")
            committed = await h.mgmt.call("wa.candidate.commit", instance_id=instance_id,
                                          timeline_id=timeline_id, ref="cd-2")
            refs = list(committed["event_refs"])
            bad, no_evidence = "", ""
            try:
                await h.mgmt.call("wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                                  outline_id="ol-1", item="it-know", status="achieved",
                                  reason="我觉得她知道了", evidence_refs=["ev-not-there"])
            except UmpError as exc:
                bad = str(exc)
            try:
                await h.mgmt.call("wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                                  outline_id="ol-1", item="it-late", status="achieved", reason="先记达成")
            except UmpError as exc:
                no_evidence = str(exc)
            decided = await h.mgmt.call("wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                                        outline_id="ol-1", item="it-know", status="achieved",
                                        reason="封堤落进世界了", evidence_refs=refs)
            check("B7 玩家行动结果", "依据对得上才更新状态；提交失败 / 没有依据时不标记达成",
                  f"假依据被拒={bool(bad)}；无依据被拒={bool(no_evidence)}；"
                  f"it-know={decided['item']['status']}，依据={decided['item']['evidence_refs']}",
                  "PASS" if bad and no_evidence and decided["item"]["status"] == "achieved"
                  and decided["item"]["evidence_refs"] == sorted(refs) else "FAIL",
                  evidence="「规则成功但世界提交失败」无法在这里凑成达成",
                  code_ref="wa.item.decide")

        # --- §十二 第八行：分支试演
        if only_matches("B8 分支试演"):
            mark = await h.mgmt.call("runtime.commit", instance_id=instance_id, timeline_id=timeline_id,
                                     note="试演点")
            point = int(h.store.commit_snapshot_get(mark["commit"]["id"])["world"])
            branched = await h.mgmt.call("wa.branch", instance_id=instance_id, timeline_id=timeline_id,
                                         commit_id=mark["commit"]["id"], display_name="试演线", outline="ol-1")
            new_line = branched["timeline"]["id"]
            h.world.activate(instance_id, new_line, now_real=time.time())
            seen = await h.mgmt.call("wa.observe", instance_id=instance_id, timeline_id=new_line,
                                     observer_id=card_id, outline_id="ol-1", audience="author")
            h.world.activate(instance_id, timeline_id, now_real=time.time())
            h.world.consume_time(instance_id, timeline_id, seconds=3600, cause="主线继续",
                                 now_real=time.time(), max_batches=8)
            main_moved = int(h.store.clock_get(timeline_id)["processed_world"]) > point
            branch_still = int(h.store.clock_get(new_line)["processed_world"]) == point
            check("B8 分支试演", "新线可观察、主线不被污染，不提供世界线合并",
                  f"新线状态={branched['timeline']['state']}；新线观察={seen['status']}；"
                  f"新线条目全未开始={all(i['status'] == 'unstarted' for i in branched['state']['items'])}；"
                  f"主线推进={main_moved} 且分支不动={branch_still}",
                  "PASS" if seen["status"] == "ok" and main_moved and branch_still
                  and branched["source_commit"] == mark["commit"]["id"] else "FAIL",
                  evidence=branched["note"], code_ref="wa.branch")

        # --- §十二 第九行：世界回滚
        if only_matches("B9 世界回滚"):
            before_rollback = h.store.wa_candidate_list(instance_id, timeline_id)
            text_kept = [row for row in before_rollback if row["id"] == "cd-text"][0]["text"]
            report = await h.mgmt.call("wa.evaluate", instance_id=instance_id, timeline_id=timeline_id,
                                       outline_id="ol-1")
            # 回滚点先立：下面那次提交才会落在「被丢弃的未来」里
            mark2 = await h.mgmt.call("runtime.commit", instance_id=instance_id, timeline_id=timeline_id,
                                      note="回滚点")
            await h.mgmt.call("wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                              outline_id="ol-1", ref="cd-3", kind="world_change",
                              changes=change_intent(card_id, "连夜修补"))
            await h.mgmt.call("wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                              ref="cd-3", status="approved", reason="采用")
            fresh = await h.mgmt.call("wa.candidate.commit", instance_id=instance_id,
                                      timeline_id=timeline_id, ref="cd-3")
            await h.mgmt.call("wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                              outline_id="ol-1", item="it-late", status="achieved",
                              reason="凭这次提交达成", evidence_refs=list(fresh["event_refs"]))
            h.world.rollback(instance_id, timeline_id, commit_id=mark2["commit"]["id"], now_real=time.time())
            after = await h.mgmt.call("wa.evaluate", instance_id=instance_id, timeline_id=timeline_id,
                                      outline_id="ol-1")
            kinds = sorted({item["kind"] for item in after["deviations"]})
            kept = [row for row in h.store.wa_candidate_list(instance_id, timeline_id) if row["id"] == "cd-text"]
            check("B9 世界回滚", "回滚后按目标时间线重新评估；已锁定的文本不随世界回滚消失",
                  f"回滚前偏离={sorted({i['kind'] for i in report['deviations']})}；"
                  f"回滚后偏离={kinds}；被回滚掉的事件={sorted(fresh['event_refs'])}；"
                  f"锁定文本仍在={bool(kept) and kept[0]['text'] == text_kept}",
                  "PASS" if "evidence_lost" in kinds and kept and kept[0]["text"] == text_kept else "FAIL",
                  evidence="回滚删掉那段未来，达成所需的引用随之消失 —— 这正是要重新评估的信号",
                  code_ref="wa.evaluate + outline.deviations")

        # --- §十二 第十一行：受众隔离
        if only_matches("B11 受众隔离"):
            player = await h.mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                       observer_id=card_id, outline_id="ol-1", audience="player")
            gm = await h.mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                   observer_id=card_id, outline_id="ol-1", audience="gm")
            text = json.dumps(player, ensure_ascii=False)
            try:
                await h.mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                  observer_id=card_id, audience="everyone")
                closed = "没有拒绝"
            except UmpError as exc:
                closed = str(exc)
            check("B11 受众隔离", "玩家观察层不带主持依据 / 未获知事实；受众是闭集",
                  f"玩家层键={sorted(player)}；gm 层有 gm_basis={'gm_basis' in gm}；"
                  f"未获知说法泄露={'崩堤当夜曾有人登堤敲钟' in text}；越界受众被拒={bool(closed)}",
                  "PASS" if "gm_basis" not in player and "gm_basis" in gm
                  and "崩堤当夜曾有人登堤敲钟" not in text and closed else "FAIL",
                  evidence="三层输出按受众分流（§5.2）", code_ref="wa.observe")

        # --- §六 模型提议
        if only_matches("B12 模型提议"):
            before = counts(h, instance_id, timeline_id)
            result = await h.mgmt.call("wa.suggest", instance_id=instance_id, timeline_id=timeline_id,
                                       outline="ol-1", observer=card_id, goal="让她开始怀疑告警",
                                       limit=2, timeout=60)
            check("B12 模型提议", "提议是未采用的候选，不写世界、不推状态",
                  f"返回={result['status']}，提议 {len(result['candidates'])} 条，"
                  f"全为 proposed/未提交={all(i['status'] == 'proposed' and i['uncommitted'] for i in result['candidates'])}；"
                  f"世界不变={counts(h, instance_id, timeline_id) == before}",
                  "PASS" if result["status"] == "ok" and len(result["candidates"]) == 2
                  and counts(h, instance_id, timeline_id) == before else "FAIL",
                  evidence=f"承诺文案：{result['must_not_imply']}", code_ref="wa.suggest")


# ------------------------------------------------------------------ C 段：消费方事实


async def consumer_facts() -> None:
    if only_matches("C1 走公共接口"):
        iface = (ROOT / "docs/worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md").read_text(
            encoding="utf-8", errors="replace")
        svc = (ROOT / "isekai_core/writing/service.py").read_text(encoding="utf-8", errors="replace")
        used = sorted({name for name in re.findall(r"(?:runtime|world)\.([a-z_]+)\(", svc)
                       + re.findall(r"self\._runtime\(\)\.([a-z_]+)\(", svc)
                       + re.findall(r"campaign\.([a-z_]+)\(", svc)
                       + re.findall(r"cognition\.([a-z_]+)\(", svc)})
        # 公共面 = 运行时门面自己的公开方法 + 规则层 / 认知层的两个纯入口；
        # 手抄名单会漂移，直接从类里取。
        from isekai_core.runtime.service import RuntimeService
        facade = {name for name in dir(RuntimeService) if not name.startswith("_")}
        foreign = [name for name in used
                   if name not in facade and name not in {"gm_change", "knowledge_slice"}]
        check("C1 走公共接口", "编剧层只经世界接口 / 规则层读世界与提交，不自己写库",
              f"用到的接口面={used}；不在公共清单里的={foreign or '（无）'}；"
              f"接口规范里出现过 read_snapshot/change_preview/change_commit={'read_snapshot' in iface}",
              "PASS" if not foreign else "FAIL",
              evidence="这也是 `_audit2_iface.py` §7「消费方」那一项的收口：编剧层是第二类真实调用方",
              code_ref="isekai_core/writing/service.py")

    if only_matches("C2 GM 路径归属"):
        svc = (ROOT / "isekai_core/writing/service.py").read_text(encoding="utf-8", errors="replace")
        ok = "campaign.gm_change(" in svc and "source=\"gm_declaration\"" in svc
        audiences = all(name in svc for name in TRPG_AUDIENCE.values())
        check("C2 GM 路径归属", "GM 直接变化走 TRPG 规则层的联合提交，受众显式翻译成规则层闭集",
              f"调用 gm_change={ok}；受众翻译值齐={audiences}（{sorted(set(TRPG_AUDIENCE.values()))}）",
              "PASS" if ok and audiences else "FAIL",
              evidence="编剧层不另造一套 GM 写世界的路径（§九）",
              code_ref="isekai_core/writing/service.py gm_approve")


def report() -> int:
    total = len(RESULTS)
    passed = sum(1 for item in RESULTS if item["status"] == "PASS")
    failed = sum(1 for item in RESULTS if item["status"] == "FAIL")
    deferred = sum(1 for item in RESULTS if item["status"] == "DEFERRED")
    print(f"\nTOTAL={total} PASS={passed} FAIL={failed} DEFERRED={deferred}")
    if failed:
        print("FAIL 明细：")
        for item in RESULTS:
            if item["status"] == "FAIL":
                print(f"  - {item['clause']}：{item['observed'][:220]}")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    global ONLY
    parser = argparse.ArgumentParser(description="编剧层行为级审计探针")
    parser.add_argument("--only", default="", help="只跑包含该关键字的检查")
    parser.add_argument("--json", action="store_true", help="把读数写成 JSON")
    ns = parser.parse_args(argv)
    ONLY = ns.only

    static_facts()
    asyncio.run(acceptance_rows())
    asyncio.run(consumer_facts())
    code = report()

    audit_dir = ROOT / ".hermes/audits"
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = audit_dir / f"wa_{stamp}{'_json' if ns.json else ''}.txt"
    payload = json.dumps(RESULTS, ensure_ascii=False, indent=2) if ns.json else "\n".join(
        f"[{item['status']:8}] {item['clause']} :: {item['observed']}\n        期望：{item['expected']}\n"
        f"        判据：{item['evidence']}（{item['code_ref']}）"
        for item in RESULTS
    )
    target.write_text(payload, encoding="utf-8")
    print(f"读数已存：{target.relative_to(ROOT).as_posix()}")
    return code


if __name__ == "__main__":
    sys.exit(main())
