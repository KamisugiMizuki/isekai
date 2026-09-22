"""U3 上线前的写作侧业务边界（USER_INTERFACE_DESIGN §11.1 归属于 U3 的六条）。

每条都是「先前实跑发现的洞」，所以判据写成反例先过的形式：先造出那个坏行为会发生的局面，
再断言它现在被挡住 / 被如实报告。真 WebSocket + 真 SQLite + 真实例，只有 LLM 换成假模型。
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
    folder = Path(tmp_path) / "config"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(
        "runtime:\n  sleep_wait_min_s: 0.05\n  sleep_wait_max_s: 0.05\n", encoding="utf-8"
    )


def _line(h, *, moment: int = DAY * 1500 + 30000) -> tuple[str, str, str]:
    package = sample_package(moment=moment)
    card = sample_card(package)
    info = create_instance(h.store, package, [card])
    timeline_id = h.store.timeline_list(info["id"])[0]["id"]
    h.runtime.world.ensure_instance(info["id"], now_real=time.time())
    h.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
    return info["id"], timeline_id, str(card["meta"]["card_id"])


def _outline(h, instance_id: str, timeline_id: str) -> dict:
    events = sorted(h.store.event_ids(instance_id, timeline_id))
    return {
        "id": "ol-b",
        "name": "边界卷",
        "items": [
            {
                "id": "it-hard", "layer": "required_node", "title": "她得知道告警",
                "statement": "堤禾在本章结束前知道那份告警", "scope": "timeline",
                # 故意给一个世界里不存在的引用：这条硬约束**没有**可追溯依据
                "success_criteria": "她的认知里出现告警", "watch_refs": ["ev-none"],
            },
            {
                "id": "it-theme", "layer": "theme", "title": "记住与遗忘",
                "statement": "主题围绕记住与遗忘", "scope": "world",
                "success_criteria": "读者能说出主题",
            },
        ],
    }


async def _setup(mgmt, h) -> tuple[str, str, str, dict]:
    instance_id, timeline_id, card_id = _line(h)
    outline = _outline(h, instance_id, timeline_id)
    await mgmt.call("wa.outline.save", outline=outline)
    await mgmt.call(
        "wa.bind", instance_id=instance_id, timeline_id=timeline_id, outline_id="ol-b",
        observers=[card_id], chapter="第一章",
    )
    return instance_id, timeline_id, card_id, outline


# ---------------------------------------------------------------- ① 受众隔离


async def test_player_observation_hides_host_material(tmp_path) -> None:
    """玩家观察不带主持候选正文 / 依据 / 大纲缺口（§5.2 第 1 层）。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            # 一条主持候选（带依据与变化意图）+ 一条玩家候选
            host = await mgmt.call(
                "wa.candidate.propose",
                instance_id=instance_id, timeline_id=timeline_id, candidate_id="cand-host",
                kind="text", title="主持私货", summary="主持看的推进",
                basis={"fact": "事实依据", "causality": "因果依据", "outline": "it-hard"},
                text="主持候选正文", audience="author",
            )
            assert host["candidate"]["status"] == "proposed"
            await mgmt.call(
                "wa.candidate.propose",
                instance_id=instance_id, timeline_id=timeline_id, candidate_id="cand-open",
                kind="text", title="玩家可见", summary="玩家也能看",
                basis={"fact": "玩家依据"}, text="玩家候选正文", audience="player",
            )

            player = await mgmt.call(
                "wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                observer_id=card_id, audience="player",
            )
            author = await mgmt.call(
                "wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                observer_id=card_id, audience="author",
            )

            ids = [item["id"] for item in player["next_step"]["candidates"]]
            assert ids == ["cand-open"], f"玩家面不该出现主持候选：{ids}"
            shown = player["next_step"]["candidates"][0]
            for key in ("basis", "changes", "gm_changes", "item_refs", "preview_id"):
                assert key not in shown, f"玩家面漏了主持材料：{key}"
            assert "主持候选正文" not in json.dumps(player, ensure_ascii=False)
            assert player["next_step"]["gaps"] == [] and player["next_step"]["deviations"] == []
            assert "主持依据" in player["next_step"]["withheld"]

            # 作者面照旧：依据与缺口都在，主持候选也在
            author_ids = [item["id"] for item in author["next_step"]["candidates"]]
            assert set(author_ids) == {"cand-host", "cand-open"}
            assert "gm_basis" in author and author["next_step"]["candidates"][0]["basis"]["fact"] == "事实依据"
        finally:
            await mgmt.close()


# ---------------------------------------------------------------- ② 候选身份


async def test_proposals_never_overwrite_adopted_work(tmp_path) -> None:
    """每次生成独立身份；已采用的候选不许被同名提案覆盖（§4.3 / §11.1）。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            first = await mgmt.call(
                "wa.candidate.propose",
                instance_id=instance_id, timeline_id=timeline_id, candidate_id="cand-a",
                kind="text", title="初稿", summary="第一版", text="第一版正文",
            )
            assert first["candidate"]["status"] == "proposed"
            await mgmt.call(
                "wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                candidate_id="cand-a", status="approved", reason="采用这版",
            )
            # 同名再来一遍：必须被挡住，且原来那条一个字都不变
            refused = None
            try:
                await mgmt.call(
                    "wa.candidate.propose",
                    instance_id=instance_id, timeline_id=timeline_id, candidate_id="cand-a",
                    kind="text", title="覆盖稿", summary="想覆盖", text="覆盖正文",
                )
            except UmpError as exc:
                refused = str(exc)
            assert refused and "新标识" in refused, refused
            # 模型提议：两次生成的标识不同，上一轮的还在（各自独立身份）
            h.fake.judgements["情节提议"] = json.dumps(
                {"candidates": [{"title": "提议一", "summary": "推进"}]}, ensure_ascii=False
            )
            one = await mgmt.call(
                "wa.suggest", instance_id=instance_id, timeline_id=timeline_id,
                outline="ol-b", observer=card_id, limit=2, timeout=60,
            )
            h.fake.judgements["情节提议"] = json.dumps(
                {"candidates": [{"title": "提议二", "summary": "另一版"}]}, ensure_ascii=False
            )
            two = await mgmt.call(
                "wa.suggest", instance_id=instance_id, timeline_id=timeline_id,
                outline="ol-b", observer=card_id, limit=2, timeout=60,
            )
            ids_one = [item["id"] for item in one["candidates"]]
            ids_two = [item["id"] for item in two["candidates"]]
            assert ids_one and ids_two and not set(ids_one) & set(ids_two), (ids_one, ids_two)
            stored = await mgmt.call("wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                                     observer_id=card_id, audience="author")
            titles = {item["title"] for item in stored["next_step"]["candidates"]}
            assert {"提议一", "提议二"} <= titles, titles
        finally:
            await mgmt.close()


# ---------------------------------------------------------------- ③ 已生效要有依据


async def test_effective_only_after_real_commit(tmp_path) -> None:
    """「已生效」只能来自真实提交结果，且带得住提交依据（§11.1）。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            await mgmt.call(
                "wa.candidate.propose",
                instance_id=instance_id, timeline_id=timeline_id, candidate_id="cand-w",
                kind="world_change", title="加一条环境状态", summary="想让北堤封路",
                changes=[{"id": "c-1", "kind": "condition", "operation": "set", "certainty": "confirmed",
                          "target_refs": [card_id], "value": "封堤", "expiry": "until_cleared"}],
            )
            await mgmt.call(
                "wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                candidate_id="cand-w", status="approved", reason="同意",
            )
            # 状态决定不能把候选标成已生效
            refused = None
            try:
                await mgmt.call(
                    "wa.candidate.decide", instance_id=instance_id, timeline_id=timeline_id,
                    candidate_id="cand-w", status="committed", reason="我觉得生效了",
                )
            except UmpError as exc:
                refused = str(exc)
            assert refused and "已生效" in refused, refused

            approved = await mgmt.call(
                "wa.observe", instance_id=instance_id, timeline_id=timeline_id,
                observer_id=card_id, audience="author",
            )
            row = [item for item in approved["next_step"]["candidates"] if item["id"] == "cand-w"][0]
            assert row["effective"] is False, "还没提交就不算已生效"
            assert row["uncommitted"] is True and row["effective_basis"] == ""

            # 真提交：状态与依据一起出现
            events_before = len(h.store.event_ids(instance_id, timeline_id))
            committed = await mgmt.call(
                "wa.candidate.commit", instance_id=instance_id, timeline_id=timeline_id,
                candidate_id="cand-w",
            )
            assert committed["status"] in ("ok", "duplicate"), committed
            events_after = len(h.store.event_ids(instance_id, timeline_id))
            assert events_after > events_before, "提交要真的在世界里留下东西"
            after = committed["candidate"]
            assert after["status"] == "committed" and after["effective"] is True
            assert after["effective_basis"], "已生效必须指得出是哪一次提交"
        finally:
            await mgmt.close()


# ---------------------------------------------------------------- ④ 元数据修改不清进度


async def test_rebind_keeps_progress_and_template_state_is_ignored(tmp_path) -> None:
    """改观察者 / 章节是元数据修改：进度原样保留；模板里的达成状态不随绑定混进来（§11.1）。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            started = await mgmt.call(
                "wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                item_id="it-theme", status="in_progress", reason="开始写这条主题",
            )
            assert [item for item in started["state"]["items"] if item["id"] == "it-theme"][0]["status"] == "in_progress"

            again = await mgmt.call(
                "wa.bind", instance_id=instance_id, timeline_id=timeline_id, outline_id="ol-b",
                observers=[], chapter="第二章",
            )
            theme = [item for item in again["state"]["items"] if item["id"] == "it-theme"][0]
            assert theme["status"] == "in_progress", f"重绑不该重置进度：{theme}"
            assert theme["reason"] == "开始写这条主题"
            assert again["state"]["chapter"] == "第二章" and again["state"]["observers"] == []
            assert again.get("updated") is True

            # 模板里写着 achieved 的条目：绑定时压成未开始
            tricked = {
                "id": "ol-trick", "name": "带状态的模板",
                "items": [{
                    "id": "it-trick", "layer": "required_node", "statement": "看起来早就达成了",
                    "scope": "timeline", "success_criteria": "判据", "status": "achieved",
                    "evidence_refs": ["ev-lie"],
                }],
            }
            await mgmt.call("wa.outline.save", outline=tricked)
            bound = await mgmt.call(
                "wa.bind", instance_id=instance_id, timeline_id=timeline_id, outline_id="ol-trick",
                observers=[card_id], chapter="",
            )
            item = bound["state"]["items"][0]
            assert item["status"] == "unstarted" and item["evidence_refs"] == [] and item["reason"] == ""
        finally:
            await mgmt.close()


# ---------------------------------------------------------------- ⑤ 严格目标可操作


async def test_strict_item_can_start_without_evidence_but_not_claim_achieved(tmp_path) -> None:
    """只对「标记达成」要依据：开始一条硬约束不该被同一道闸挡住（§11.1）。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)
            started = await mgmt.call(
                "wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                item_id="it-hard", status="in_progress", reason="开始推进这条必达节点",
            )
            item = [row for row in started["state"]["items"] if row["id"] == "it-hard"][0]
            assert item["status"] == "in_progress", "开始严格目标不该被依据检查挡住"

            blocked = None
            try:
                await mgmt.call(
                    "wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                    item_id="it-hard", status="achieved", reason="我觉得到了",
                )
            except UmpError as exc:
                blocked = str(exc)
            assert blocked and "依据" in blocked, blocked

            # 偏离去向同样不被依据闸挡住（只对「达成」要依据）
            deviated = await mgmt.call(
                "wa.item.decide", instance_id=instance_id, timeline_id=timeline_id,
                item_id="it-hard", status="deviated", reason="改成让旁人转述",
            )
            assert [row for row in deviated["state"]["items"] if row["id"] == "it-hard"][0]["status"] == "deviated"
        finally:
            await mgmt.close()


# ---------------------------------------------------------------- ⑥ 无效输出可排错


async def test_unusable_model_output_is_reported_not_silently_empty(tmp_path) -> None:
    """「没有建议」和「没拿到可用输出」分开报，并给出原文摘要（§11.1）。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            instance_id, timeline_id, card_id, _ = await _setup(mgmt, h)

            h.fake.judgements["情节提议"] = "抱歉，我建议你多喝水。"
            broken = await mgmt.call(
                "wa.suggest", instance_id=instance_id, timeline_id=timeline_id,
                outline="ol-b", observer=card_id, limit=2, timeout=60,
            )
            assert broken["status"] == "unparsable", broken
            assert broken["candidates"] == [] and "没有 JSON" in broken["reason"]
            assert "多喝水" in broken["raw_excerpt"]

            h.fake.judgements["情节提议"] = ""
            empty = await mgmt.call(
                "wa.suggest", instance_id=instance_id, timeline_id=timeline_id,
                outline="ol-b", observer=card_id, limit=2, timeout=60,
            )
            assert empty["status"] == "unparsable" and "空内容" in empty["reason"]

            h.fake.judgements["情节提议"] = json.dumps({"candidates": []}, ensure_ascii=False)
            none = await mgmt.call(
                "wa.suggest", instance_id=instance_id, timeline_id=timeline_id,
                outline="ol-b", observer=card_id, limit=2, timeout=60,
            )
            assert none["status"] == "ok" and none["candidates"] == []
            assert "没有给出可用的候选" in none["reason"]

            h.fake.judgements["情节提议"] = json.dumps(
                {"candidates": [{"title": "可用提议", "summary": "推进"}]}, ensure_ascii=False
            )
            good = await mgmt.call(
                "wa.suggest", instance_id=instance_id, timeline_id=timeline_id,
                outline="ol-b", observer=card_id, limit=2, timeout=60,
            )
            assert good["status"] == "ok" and len(good["candidates"]) == 1
            assert good["candidates"][0]["title"] == "可用提议"
            assert "-" in good["candidates"][0]["id"] and len(good["candidates"][0]["id"].split("-")) >= 3
        finally:
            await mgmt.close()
