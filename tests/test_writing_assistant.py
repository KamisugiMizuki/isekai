"""Writing Assistant 的行为验收（WRITING_ASSISTANT_SPEC §三 ~ §九；判据对应 §十二 场景表）。

真 WebSocket + 真 SQLite + 真实例，只有 LLM 换成 FakeLLM。判据落在可观察行为上：
条目状态、候选生命周期、世界事件 / 效果条数、快照水位与世代、观众层的键集合。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from conftest import open_mgmt, running_core
from isekai_core.ump import UmpError
from isekai_core.world.instances import create_instance
from samples import DAY, sample_card, sample_package


def _fast(tmp_path) -> None:
    """睡眠期等待压到 0.05s：测试不该为节拍等上分钟。"""
    folder = Path(tmp_path) / "config"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(
        "runtime:\n  sleep_wait_min_s: 0.05\n  sleep_wait_max_s: 0.05\n", encoding="utf-8"
    )


def _line(h, *, moment: int = DAY * 1500 + 30000) -> tuple[str, str, str]:
    """真实例 + 激活的时间线；返回 (instance_id, timeline_id, card_id)。"""
    package = sample_package(moment=moment)
    card = sample_card(package)
    info = create_instance(h.store, package, [card])
    timeline_id = h.store.timeline_list(info["id"])[0]["id"]
    h.runtime.world.ensure_instance(info["id"], now_real=time.time())
    h.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
    return info["id"], timeline_id, str(card["meta"]["card_id"])


def _counts(h, instance_id: str, timeline_id: str) -> dict[str, int]:
    return {
        "events": len(h.store.event_ids(instance_id, timeline_id)),
        "effects": len(h.store.effect_active_ids(instance_id, timeline_id)),
        "claims": len(h.store.claim_list(instance_id, timeline_id)),
    }


def _outline(h, instance_id: str, timeline_id: str, *, deadline: int = 0) -> dict:
    """一份三层大纲：必达（带世界引用）/ 禁止（引用世界里已有的事）/ 主题，外加一条到点未达的硬约束。"""
    events = sorted(h.store.event_ids(instance_id, timeline_id))
    return {
        "id": "ol-1",
        "name": "潮汐志·第一卷",
        "items": [
            {
                "id": "it-know", "layer": "required_node", "title": "她得知道告警",
                "statement": "堤禾在第一章结束前知道那份告警的存在", "scope": "timeline",
                "success_criteria": "她的认知里出现告警相关内容", "watch_refs": events[:1],
                "preconditions": [], "alternatives": ["由旁人转述"],
            },
            {
                "id": "it-forbid", "layer": "forbidden", "title": "不许再崩堤",
                "statement": "北堤不得再次崩塌", "scope": "world",
                "success_criteria": "世界里没有新的崩堤事件", "watch_refs": events[1:2],
            },
            {
                "id": "it-theme", "layer": "theme", "title": "盐味与旧账",
                "statement": "主题围绕记住与遗忘", "scope": "world",
                "success_criteria": "读者能说出这个主题",
            },
            {
                "id": "it-late", "layer": "required_node", "title": "到点没发生的节点",
                "statement": "第三章之前拿到旧账本", "scope": "chapter",
                "success_criteria": "账本出现在她的经历里", "watch_refs": ["ev-none"],
                "deadline_world": deadline or 1,
            },
        ],
    }


async def _setup(mgmt, h, **kwargs) -> tuple[str, str, str, dict]:
    instance_id, timeline_id, card_id = _line(h, **kwargs)
    outline = _outline(h, instance_id, timeline_id)
    await mgmt.call("wa.outline.save", outline=outline)
    await mgmt.call("wa.bind", instance_id=instance_id, timeline_id=timeline_id,
                    outline_id="ol-1", observers=[card_id], chapter="第一章")
    return instance_id, timeline_id, card_id, outline


# ------------------------------------------------------------------ §十二 新建大纲


async def test_outline_layers_and_required_fields(tmp_path) -> None:
    """§十二 第一行：条目可区分六个层级，每条有标识 / 范围 / 前置 / 判据；未通过校验不落盘。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            full = {
                "id": "ol-layers", "name": "六层",
                "items": [
                    {"id": f"it-{layer}", "layer": layer, "statement": f"{layer} 的约束",
                     "scope": "world", "success_criteria": "判据"}
                    for layer in ("theme", "required_node", "forbidden", "character_arc", "pacing",
                                  "variable_material")
                ],
            }
            saved = await mgmt.call("wa.outline.save", outline=full)
            fields = {"id", "layer", "strength", "scope", "preconditions", "success_criteria",
                      "alternatives", "status", "watch_refs", "deadline_world"}
            for item in saved["outline"]["items"]:
                assert fields <= set(item), item
            strengths = {item["layer"]: item["strength"] for item in saved["outline"]["items"]}
            assert strengths["required_node"] == "hard" and strengths["theme"] == "soft"
            assert strengths["character_arc"] == "medium"

            listed = await mgmt.call("wa.outline.list")
            assert [row["id"] for row in listed["outlines"]] == ["ol-layers"]

            bad = dict(full, id="ol-bad")
            bad["items"] = [*full["items"], {"id": "it-theme", "layer": "theme", "statement": "重复 id",
                                             "scope": "world", "success_criteria": "判据"}]
            with_error = None
            try:
                await mgmt.call("wa.outline.save", outline=bad)
            except UmpError as exc:  # noqa: PERF203 - 校验失败必须报错且不落盘
                with_error = str(exc)
            assert with_error and "重复" in with_error
            assert [row["id"] for row in (await mgmt.call("wa.outline.list"))["outlines"]] == ["ol-layers"], \
                "校验不过不得覆盖已存的大纲"

            forbidden = dict(full, id="ol-forbid", items=[{
                "id": "it-x", "layer": "forbidden", "statement": "不许", "scope": "world",
                "success_criteria": "判据", "status": "achieved",
            }])
            try:
                await mgmt.call("wa.outline.save", outline=forbidden)
                raise AssertionError("禁止事项不该允许标成达成")
            except UmpError as exc:
                assert "禁止事项" in str(exc)
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 只读观察


async def test_observation_is_scoped_and_read_only(tmp_path) -> None:
    """§十二 第二行：只能看到给定实例 / 时间线 / 观察者在一致快照上的合法投影。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            before = _counts(h, instance_id, timeline_id)
            observed = await mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                       observer_id=card_id, outline_id="ol-1", audience="author")
            assert observed["status"] == "ok" and observed["observed_revision"] == observed["world_time"]
            text = json.dumps(observed, ensure_ascii=False)
            assert "有碑刻提到崩堤当夜曾有人登堤敲钟" not in text, "未获知的说法不得出现在观察里"
            assert "她父亲的旧账本" not in text, "创作者背景（实情层）不得出现"
            assert "天罚" in text, "她获知过的说法要在合法视图里"
            assert _counts(h, instance_id, timeline_id) == before, "观察是只读的"

            h.runtime.world.freeze(instance_id, timeline_id)
            frozen = await mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                     observer_id=card_id, outline_id="ol-1", audience="author")
            assert frozen["status"] == "not_ready" and "frozen" in frozen["reason"], "冻结线不得拿旧状态冒充当前"
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 未达成硬约束


async def test_missing_required_node_reports_gap_only(tmp_path) -> None:
    """§十二 第三行：报告缺口，不自动制造世界事实，也不把候选标成已达成。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, outline = await _setup(mgmt, h)
            before = _counts(h, instance_id, timeline_id)
            report = await mgmt.call("wa.evaluate", instance_id=instance_id, timeline_id=timeline_id,
                                     outline_id="ol-1")
            kinds = {gap["kind"] for gap in report["gaps"]}
            assert "required_missing" in kinds, report["gaps"]
            assert {gap["item_id"] for gap in report["gaps"]} >= {"it-late"}
            item = [row for row in report["items"] if row["id"] == "it-late"][0]
            assert item["status"] == "unstarted", "评估不改状态：达成要靠决定或世界事实"
            assert "候选已经成了世界事实" == report["must_not_imply"]
            assert _counts(h, instance_id, timeline_id) == before
            assert report["evaluated_world"] == report["observed_revision"] > 0, "评估要记下用的水位"
            # 世界里已经有依据的必达节点：只报依据，不自动达成
            evidence_ids = {row["item_id"] for row in report["evidence"]}
            assert evidence_ids <= {"it-know", "it-forbid"}
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 候选批准


async def test_approval_is_not_world_change(tmp_path) -> None:
    """§十二 第四行：批准只进 approved 或草稿；在 commit 成功前仍标未提交。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            before = _counts(h, instance_id, timeline_id)
            text_candidate = await mgmt.call(
                "wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                outline_id="ol-1", ref="cd-text", kind="text", item_refs=["it-theme"],
                title="第一章开场", summary="盐滩上的清晨", instruction="她想先量水位再说话",
                unsolved=["她要不要先提告警"],
            )
            assert text_candidate["candidate"]["status"] == "proposed"
            approved = await mgmt.call(
                "wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                ref="cd-text", status="approved", reason="采用这段开场",
                text="退潮后的盐滩像一张没写完的账页，她先量水位，再开口。",
            )
            assert approved["candidate"]["status"] == "approved"
            assert approved["candidate"]["uncommitted"] is True, "批准不是已提交"
            assert approved["candidate"]["must_not_imply"] == "世界已经按它变了"
            assert _counts(h, instance_id, timeline_id) == before, "文本候选不改世界"

            change = [{"id": "c-1", "kind": "condition", "operation": "set", "certainty": "confirmed",
                       "target_refs": [card_id], "value": "封堤", "expiry": "until_cleared"}]
            proposed = await mgmt.call(
                "wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                outline_id="ol-1", ref="cd-change", kind="world_change", item_refs=["it-know"],
                title="封堤", summary="堤上挂了封堤的木牌", changes=change,
                basis={"fact": "她守着水位尺", "causality": "封堤先于通行牌停发", "outline": "it-know"},
            )
            assert proposed["status"] == "proposed" and proposed["candidate"]["preview_id"], proposed
            assert _counts(h, instance_id, timeline_id) == before, "预览不改世界"
            await mgmt.call("wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                            ref="cd-change", status="approved", reason="批准采用")
            mid = await mgmt.call("wa.state", instance_id=instance_id, timeline_id=timeline_id, outline_id="ol-1")
            assert [row for row in mid["public"]["candidates"] if row["id"] == "cd-change"][0]["uncommitted"] is True
            assert _counts(h, instance_id, timeline_id) == before

            committed = await mgmt.call("wa.candidate.commit", instance_id=instance_id, timeline_id=timeline_id,
                                        ref="cd-change", idempotency_key="wa-t1")
            assert committed["status"] == "ok" and committed["candidate"]["status"] == "committed"
            assert committed["candidate"]["uncommitted"] is False
            after = _counts(h, instance_id, timeline_id)
            assert after["events"] == before["events"] + 1, "提交成功才动世界"
            assert committed["event_refs"], committed
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 事实 / 因果冲突


async def test_conflicting_candidate_gets_reasons(tmp_path) -> None:
    """§十二 第五行：候选进 rejected / needs_review / conflict，并给出需要修改的依据。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            before = _counts(h, instance_id, timeline_id)
            refused = await mgmt.call(
                "wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                outline_id="ol-1", ref="cd-bad", kind="world_change",
                changes=[{"id": "c-9", "kind": "resource_change", "operation": "add",
                          "certainty": "confirmed", "value": 10}],
            )
            assert refused["status"] == "rejected" and "资源量" in refused["reason"], refused
            assert _counts(h, instance_id, timeline_id) == before
            assert refused["candidate"]["uncommitted"] is False, "被拒的候选不是「未提交」，而是这条提不出事实效果"

            pending = await mgmt.call(
                "wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                outline_id="ol-1", ref="cd-soft", kind="world_change",
                changes=[{"id": "c-10", "kind": "condition", "operation": "set", "certainty": "candidate",
                          "target_refs": [card_id], "value": "犹豫"}],
            )
            assert pending["status"] == "proposed" and "需要确认" in pending["reason"], pending
            await mgmt.call("wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                            ref="cd-soft", status="approved", reason="先批准，看提交时怎么说")
            blocked = await mgmt.call("wa.candidate.commit", instance_id=instance_id,
                                      timeline_id=timeline_id, ref="cd-soft")
            assert blocked["status"] == "needs_review", blocked
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 GM 直接变化


async def test_gm_declaration_goes_through_trpg_path(tmp_path) -> None:
    """§十二 第六行：只形成待批准结构；批准后经 TRPG GM 变化路径与联合提交落世界。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            campaign = await mgmt.call("trpg.campaign.create", instance_id=instance_id,
                                       timeline_id=timeline_id, ruleset_id="wa-probe", status="active")
            campaign_id = str(campaign["campaign_id"])
            before = _counts(h, instance_id, timeline_id)
            gm_changes = {
                "consequences": [{"kind": "institution_state", "target": "off-1", "value": "vacant",
                                  "expiry": "until_cleared", "certainty": "confirmed"}],
                "claims": [{"text": "堤长的位置空了出来", "source_id": "src-1", "audience": "公开"}],
            }
            declared = await mgmt.call(
                "wa.gm.declare", instance_id=instance_id, timeline_id=timeline_id,
                ref="gm-1", campaign=campaign_id, display_name="堤长去职",
                gm_changes=gm_changes, basis={"fact": "议席推举未定"},
            )
            assert declared["candidate"]["status"] == "proposed"
            assert declared["candidate"]["source_mode"] == "gm_declaration"
            assert declared["must_not_imply"] == "声明本身已经是世界事实"
            assert _counts(h, instance_id, timeline_id) == before, "声明本身不改世界"

            approved = await mgmt.call("wa.gm.approve", instance_id=instance_id, timeline_id=timeline_id,
                                       ref="gm-1", idempotency_key="wa-gm-1")
            assert approved["status"] == "committed", approved
            assert approved["joint_commit_id"], approved
            assert approved["candidate"]["status"] == "committed"
            after = _counts(h, instance_id, timeline_id)
            assert after["effects"] > before["effects"] or after["claims"] > before["claims"]
            ledger = h.store.trpg_commit_by_key(instance_id, timeline_id, "wa-gm-1")
            assert ledger is not None, "GM 直接变化要留联合提交账本"
            assert h.store.trpg_list("action", instance_id=instance_id, timeline_id=timeline_id) == [], \
                "GM 直接变化不制造行动行"
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 玩家行动结果


async def test_item_achieved_needs_committed_evidence(tmp_path) -> None:
    """§十二 第七行：读已固化结果后更新大纲状态；提交失败 / 依据对不上时不标记达成。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            change = [{"id": "c-1", "kind": "condition", "operation": "set", "certainty": "confirmed",
                       "target_refs": [card_id], "value": "守夜", "expiry": "until_cleared"}]
            await mgmt.call("wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                            outline_id="ol-1", ref="cd-1", kind="world_change", changes=change)
            await mgmt.call("wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                            ref="cd-1", status="approved", reason="采用")
            committed = await mgmt.call("wa.candidate.commit", instance_id=instance_id,
                                        timeline_id=timeline_id, ref="cd-1")
            event_refs = list(committed["event_refs"])

            # 依据对不上：不标记达成
            try:
                await mgmt.call("wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                                outline_id="ol-1", item="it-know", status="achieved",
                                reason="我觉得她知道了", evidence_refs=["ev-not-there"])
                raise AssertionError("依据对不上不该允许标记达成")
            except UmpError as exc:
                assert "对不上" in str(exc)
            # 硬约束没有依据：同样不标记达成
            try:
                await mgmt.call("wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                                outline_id="ol-1", item="it-late", status="achieved", reason="先记达成")
                raise AssertionError("硬约束标记达成必须带依据")
            except UmpError as exc:
                assert "依据" in str(exc)

            decided = await mgmt.call("wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                                      outline_id="ol-1", item="it-know", status="achieved",
                                      reason="封堤落进世界了", evidence_refs=event_refs)
            item = decided["item"]
            assert item["status"] == "achieved" and item["evidence_refs"] == sorted(event_refs)
            assert decided["must_not_imply"] == "草稿或模型判断已经让这条达成了"
            state = await mgmt.call("wa.state", instance_id=instance_id, timeline_id=timeline_id,
                                    outline_id="ol-1")
            assert [row for row in state["state"]["items"] if row["id"] == "it-know"][0]["status"] == "achieved"
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 分支试演


async def test_branch_trial_keeps_main_line_clean(tmp_path) -> None:
    """§十二 第八行：新线可观察候选后果，主线不被污染；不提供世界线合并。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            mark = await mgmt.call("runtime.commit", instance_id=instance_id, timeline_id=timeline_id,
                                   note="试演点")
            commit_id = mark["commit"]["id"]
            point = int(h.store.commit_snapshot_get(commit_id)["world"])
            branched = await mgmt.call("wa.branch", instance_id=instance_id, timeline_id=timeline_id,
                                       commit_id=commit_id, display_name="试演线", outline="ol-1")
            new_line = branched["timeline"]["id"]
            assert branched["timeline"]["state"] == "frozen"
            assert branched["note"] == "分支继承共同过去；主线不被污染，项目不提供世界线合并"
            assert branched["must_not_imply"] == "两条线可以合并回去"
            assert branched["state"]["items"], "新线也要有独立的大纲状态"
            assert all(item["status"] == "unstarted" for item in branched["state"]["items"]), \
                "新线的达成状态从零开始，独立记录"

            h.runtime.world.activate(instance_id, new_line, now_real=time.time())
            observed = await mgmt.call("wa.observe", instance_id=instance_id, timeline_id=new_line,
                                       observer_id=card_id, outline_id="ol-1", audience="author")
            assert observed["status"] == "ok" and observed["timeline_id"] == new_line

            h.runtime.world.activate(instance_id, timeline_id, now_real=time.time())
            h.runtime.world.consume_time(instance_id, timeline_id, seconds=3600, cause="主线继续",
                                         now_real=time.time(), max_batches=8)
            assert int(h.store.clock_get(timeline_id)["processed_world"]) > point
            assert int(h.store.clock_get(new_line)["processed_world"]) == point, "主线推进不回流到分支"
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 世界回滚


async def test_rollback_reevaluates_without_touching_text(tmp_path) -> None:
    """§十二 第九行：回滚后大纲状态重新评估；已导出的文本不被自动删除，文本操作不反向回滚世界。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            mark = await mgmt.call("runtime.commit", instance_id=instance_id, timeline_id=timeline_id,
                                   note="回滚点")
            commit_id = mark["commit"]["id"]
            change = [{"id": "c-1", "kind": "condition", "operation": "set", "certainty": "confirmed",
                       "target_refs": [card_id], "value": "封堤", "expiry": "until_cleared"}]
            await mgmt.call("wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                            outline_id="ol-1", ref="cd-1", kind="world_change", changes=change)
            await mgmt.call("wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                            ref="cd-1", status="approved", reason="采用")
            committed = await mgmt.call("wa.candidate.commit", instance_id=instance_id,
                                        timeline_id=timeline_id, ref="cd-1")
            await mgmt.call("wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                            outline_id="ol-1", item="it-know", status="achieved", reason="封堤落进世界",
                            evidence_refs=list(committed["event_refs"]))
            await mgmt.call("wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                            outline_id="ol-1", ref="cd-text", kind="text", title="草稿",
                            summary="锁定的段落")
            await mgmt.call("wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                            ref="cd-text", status="approved", reason="锁定", text="锁定的段落正文")

            h.runtime.world.rollback(instance_id, timeline_id, commit_id=commit_id, now_real=time.time())
            report = await mgmt.call("wa.evaluate", instance_id=instance_id, timeline_id=timeline_id,
                                     outline_id="ol-1")
            kinds = {item["kind"] for item in report["deviations"]}
            assert "evidence_lost" in kinds, report["deviations"]
            lost = [item for item in report["deviations"] if item["kind"] == "evidence_lost"][0]
            assert "重新评估" in lost["detail"]
            assert _counts(h, instance_id, timeline_id)["events"] <= 3, "回滚抹掉那段未来"
            kept = [row for row in h.store.wa_candidate_list(instance_id, timeline_id)
                    if row["id"] == "cd-text"][0]
            assert kept["text"] == "锁定的段落正文", "已锁定的文本不随世界回滚消失"
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 快照过期


async def test_stale_candidate_does_not_write_back(tmp_path) -> None:
    """§十二 第十行：候选在写入前被 generation.check 判 stale，不写回旧世界。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            change = [{"id": "c-1", "kind": "condition", "operation": "set", "certainty": "confirmed",
                       "target_refs": [card_id], "value": "封堤", "expiry": "until_cleared"}]
            await mgmt.call("wa.candidate.propose", instance_id=instance_id, timeline_id=timeline_id,
                            outline_id="ol-1", ref="cd-1", kind="world_change", changes=change)
            await mgmt.call("wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                            ref="cd-1", status="approved", reason="采用")
            before = _counts(h, instance_id, timeline_id)
            await mgmt.call("runtime.task.invalidate", instance_id=instance_id, timeline_id=timeline_id,
                            reason="场景重来")
            stale = await mgmt.call("wa.candidate.commit", instance_id=instance_id, timeline_id=timeline_id,
                                    ref="cd-1")
            assert stale["status"] == "stale" and stale["candidate"]["status"] == "stale", stale
            assert "旧世代" in stale["reason"] or "世代" in stale["reason"]
            assert _counts(h, instance_id, timeline_id) == before, "过期候选不得写回"
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 受众隔离


async def test_player_audience_hides_gm_basis(tmp_path) -> None:
    """§十二 第十一行：玩家观察不含 GM 私有依据、未获知事实或规则状态正文。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            player = await mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                     observer_id=card_id, outline_id="ol-1", audience="player")
            gm = await mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                 observer_id=card_id, outline_id="ol-1", audience="gm")
            assert "gm_basis" not in player, "玩家层不带主持依据"
            assert gm["gm_basis"]["outline_items"] and gm["gm_basis"]["evidence"] is not None
            assert "gm_basis" in gm and gm["gm_basis"]["snapshot_id"].startswith("snap-")
            assert set(player) | {"gm_basis"} == set(gm) | {"gm_basis"}
            player_text = json.dumps(player, ensure_ascii=False)
            assert "有碑刻提到崩堤当夜曾有人登堤敲钟" not in player_text
            assert "她父亲的旧账本" not in player_text
            assert "outline_items" not in player_text
            try:
                await mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                observer_id=card_id, audience="everyone")
                raise AssertionError("受众是闭集")
            except UmpError as exc:
                assert "受众" in str(exc)
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §六 模型提议


async def test_suggestions_are_candidates_not_facts(tmp_path) -> None:
    """§六：系统可以提出多条推进，但它们是未采用的候选，不写世界、不推状态。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        h.fake.judgements["情节提议"] = json.dumps({"candidates": [
            {"title": "碑文残片", "summary": "她在退潮的盐滩上看见半块碑文", "outline_ref": "it-know",
             "unsolved": ["碑文缺的那半写的是什么"]},
            {"title": "议会的信", "summary": "驿站转来一封没署名的信", "outline_ref": "", "unsolved": []},
        ]}, ensure_ascii=False)
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            before = _counts(h, instance_id, timeline_id)
            result = await mgmt.call("wa.suggest", instance_id=instance_id, timeline_id=timeline_id,
                                     outline="ol-1", observer=card_id, goal="让她开始怀疑告警",
                                     limit=2, timeout=60)
            assert result["status"] == "ok" and len(result["candidates"]) == 2
            assert all(item["status"] == "proposed" and item["uncommitted"] for item in result["candidates"])
            assert result["must_not_imply"] == "这些提议已经发生或已被采用"
            assert result["candidates"][0]["item_refs"] == ["it-know"]
            assert _counts(h, instance_id, timeline_id) == before, "提议不改世界"
            state = await mgmt.call("wa.state", instance_id=instance_id, timeline_id=timeline_id,
                                    outline_id="ol-1")
            assert [row for row in state["state"]["items"] if row["id"] == "it-know"][0]["status"] == "unstarted"
        finally:
            await mgmt.close()
