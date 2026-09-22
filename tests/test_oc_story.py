"""OC 故事层的行为验收（OC_STORY_LAYER_SPEC §二 ~ §八；判据对应 §十二 场景表）。

真 WebSocket + 真 SQLite + 真实例，只有 LLM 换成 FakeLLM；判据落在可观察行为上：
入站处理状态、出站的 `role` 分类、投递汇总、世界事件 / 效果 / 说法条数、提示词里带了什么、
以及管理面 `story.*` 的返回值。**grep 到符号不算实现**——每一条都读运行结果。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from conftest import bind_thread, open_mgmt, running_core
from isekai_core.llm import LLMError
from isekai_core.story import view
from isekai_core.world.instances import create_instance
from samples import DAY, sample_card, sample_package

#: 判断点脚本（分类那一问的答案）：FakeLLM 按提示词命中返回，不占回复脚本
CONTACT = '{"category": "contact_share", "why": "分享近况"}'
FOLLOWUP = '{"category": "followup", "why": "追问之前提过的事"}'
#: 未获知的说法 / 创作者背景（§6.2 实情层与未获知内容不得进回复）
UNKNOWN_CLAIM = "有碑刻提到崩堤当夜曾有人登堤敲钟。"
CREATOR_BACKGROUND = "她父亲的旧账本里有那份告警的一页抄件"


def _fast(tmp_path) -> None:
    """睡眠期等待压到 0.05s：测试不该为节拍等上分钟（样本角色此刻也不在睡眠块里）。"""
    folder = Path(tmp_path) / "config"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(
        "runtime:\n  sleep_wait_min_s: 0.05\n  sleep_wait_max_s: 0.05\n", encoding="utf-8"
    )


async def _room(
    h,
    mgmt,
    *,
    names: tuple[str, ...] = ("堤禾",),
    moment: int = DAY * 1500 + 30000,
    channel: str = "builtin",
    thread: str = "dm-1",
    activate: bool = True,
):
    """真实例 + 可选激活 + 绑定第一个角色的 thread。"""
    package = sample_package(moment=moment)
    cards = [sample_card(package, name=name) for name in names]
    info = create_instance(h.store, package, cards)
    timeline_id = h.store.timeline_list(info["id"])[0]["id"]
    h.runtime.world.ensure_instance(info["id"], now_real=time.time())
    if activate:
        h.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
    ids = [str(card["meta"]["card_id"]) for card in cards]
    client, bound = await bind_thread(
        h,
        mgmt,
        channel_id=channel,
        thread_id=thread,
        instance=info["id"],
        timeline=timeline_id,
        character=ids[0],
    )
    return client, bound, info["id"], timeline_id, ids


def _counts(h, instance_id: str, timeline_id: str) -> dict[str, int]:
    """世界公共事实的三个面：事件 / 有效效果 / 说法。"""
    return {
        "events": len(h.store.event_ids(instance_id, timeline_id)),
        "effects": len(h.store.effect_active_ids(instance_id, timeline_id)),
        "claims": len(h.store.claim_list(instance_id, timeline_id)),
    }


async def _say(client, thread: dict, text: str) -> str:
    ref = await client.send_user_message(
        thread_id=str(thread["thread_id"]), binding_token=str(thread["binding_token"]), text=text
    )
    await client.expect(lambda env: env.type == "accepted")
    return ref


def _prompt(h) -> str:
    """最后一次生成用的系统提示词（她的扮演定义 + 本层表达契约）。"""
    assert h.fake.calls, "没有发生生成调用"
    return str((h.fake.calls[-1][0] or {}).get("content") or "")


def _conversation(h) -> str:
    """最后一次生成用的整段输入（含本轮用户原话）：判断「看到了什么」要看这里。"""
    assert h.fake.calls, "没有发生生成调用"
    return "\n".join(str(item.get("content") or "") for item in h.fake.calls[-1])


# ------------------------------------------------------------------ §4.1 首次进入


async def test_first_entry_steps_are_product_language(tmp_path) -> None:
    """§4.1 / §十二 第一行：不必理解实例与时间线也能走到第一次联络；没就绪时停在准备状态。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            empty = await mgmt.call("story.enter")
            assert empty["product_state"] == "preparing" and empty["status"] == "waiting"
            assert [step["key"] for step in empty["steps"]] == [
                "describe", "review", "confirm", "create", "pick", "talk",
            ]
            assert not any(step["done"] for step in empty["steps"])
            for step in empty["steps"]:
                # 规范自己的流程里就有「创建锁定实例」；这里禁的才是真内部术语
                assert not [word for word in ("时间线", "认知", "提交", "绑定") if word in step["label"]], step
            assert "实例" not in empty["note"] and "时间线" not in empty["note"], "内部术语不该出现在给用户的说明里"
            assert view.internals_in(empty) == [], view.internals_in(empty)

            # 没就绪时读一轮：停在准备状态，不生成、也没有假角色回复
            pending = await mgmt.call("story.turn", instance_id="in-none", timeline_id="tl-none", character_id="cc-x")
            assert pending["product_state"] == "preparing" and pending["status"] == "waiting"
            assert not h.fake.calls, "准备状态不该花生成调用"

            # 创作目录里有一份可用世界包 → 前三步就绪
            folder = Path(h.cfg.paths.packages)
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "灰潮纪.json").write_text(
                json.dumps(sample_package(), ensure_ascii=False), encoding="utf-8"
            )
            ready = await mgmt.call("story.enter")
            done = {step["key"]: step["done"] for step in ready["steps"]}
            assert done["describe"] and done["review"] and done["confirm"]
            assert not done["create"] and not done["pick"]

            # 建实例 + 绑定 → 后三步就绪，场景进入可联络
            client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
            try:
                full = await mgmt.call(
                    "story.enter", instance_id=instance_id, timeline_id=timeline_id, character_id=cards[0]
                )
                assert full["product_state"] == "available"
                assert all(step["done"] for step in full["steps"])
                assert full["characters"] == [{"card_id": cards[0], "name": "堤禾"}]
            finally:
                await client.close()
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §十二 普通分享


async def test_plain_share_is_contact_not_world_fact(tmp_path) -> None:
    """§十二 第二行：分享固化为她收到的联络内容；世界事件 / 效果 / 说法不因分享自动增加。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["嗯，听着就累。"]) as h:
        h.fake.judgements["输入分类"] = CONTACT
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
        thread = bound["thread"]
        try:
            before = _counts(h, instance_id, timeline_id)
            ref = await _say(client, thread, "我今天加班到很晚，有点累")
            reply = await client.expect(lambda env: env.type == "reply")
            assert reply.payload["covers"] == [ref]
            assert _counts(h, instance_id, timeline_id) == before, "普通分享不自动成为世界事实"

            row = h.store.inbound_find(thread["channel_id"], thread["thread_id"], ref)
            assert row["state"] == "done" and row["text"] == "我今天加班到很晚，有点累"
            assert h.fake.judgement_calls, "分类走的是判断点通道（不占回复脚本）"

            turn = await mgmt.call(
                "story.turn", instance_id=instance_id, timeline_id=timeline_id, character_id=cards[0]
            )
            assert turn["status"] == "expressed" and turn["product_state"] == "expressed"
            assert turn["seq"] == int(row["seq"]) and turn["delivery"]["state"] == "sent"
            assert turn["delivery"]["delivered"] is False, "没有回执不算送达"
            assert turn["must_not_imply"] == "通道已经送达或世界事实已改变"
            assert view.internals_in(turn) == [], view.internals_in(turn)
        finally:
            await client.close()
            await mgmt.close()


# ------------------------------------------------------------------ §十二 近况询问


async def test_status_inquiry_stays_inside_legal_view(tmp_path) -> None:
    """§十二 第三行：回复只来自已完成水位与角色合法视图；未获知说法 / 创作者背景不进去。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["水位尺还是老样子。"]) as h:
        h.fake.judgements["输入分类"] = '{"category": "status_inquiry", "why": "问她近况"}'
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
        try:
            await _say(client, bound["thread"], "你最近怎么样？")
            await client.expect(lambda env: env.type == "reply")
            prompt = _prompt(h)
            assert UNKNOWN_CLAIM not in prompt, "未获知的说法不得进提示词"
            assert CREATOR_BACKGROUND not in prompt, "创作者背景（实情层）不得进提示词"
            assert "天罚" in prompt, "她获知过的说法要在合法视图里"
            assert "她这次说话的表达契约" in prompt, "§五 表达契约随生成进上下文"

            turn = await mgmt.call(
                "story.turn", instance_id=instance_id, timeline_id=timeline_id, character_id=cards[0]
            )
            scope = await mgmt.call("runtime.scope.inspect", instance_id=instance_id, timeline_id=timeline_id)
            assert turn["observed_revision"] == turn["processed_watermark"] == scope["processed_watermark"]
        finally:
            await client.close()
            await mgmt.close()


# ------------------------------------------------------------------ §十二 重复追问


async def test_repeat_followup_keeps_the_same_boundary(tmp_path) -> None:
    """§十二 第四行：同一话题追问不改边界——「没讲出口」的单元不因被再问而变成讲过的。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["……嗯。", "还是那句话。", "不想说这个。"]) as h:
        h.fake.judgements["输入分类"] = FOLLOWUP
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
        character_id = cards[0]
        session_id = bound["session"]["id"]
        # 造一条「没讲出口」的单元（她犹豫过的那件事）
        h.store.narrative_unit_put({
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "character_id": character_id,
            "id": "nu-deferred-1",
            "primary_ref": "xp-1",
            "refs": json.dumps(["xp-1"], ensure_ascii=False),
            "entry": "experience",
            "relation": "并列",
            "topic": "那封没有署名的信",
            "stage": "deferred",
            "message_id": "",
            "world_day": 1500,
            "created_world": DAY * 1500,
            "updated_world": DAY * 1500,
        })
        try:
            for index in range(3):
                await _say(client, bound["thread"], f"上次说的那封信后来呢？（第 {index + 1} 次问）")
                await client.expect(lambda env: env.type == "reply")
                prompt = _prompt(h)
                assert "她之前没讲出口的事（界限不变）：" in prompt, f"第 {index + 1} 次追问没带边界"
                assert "那封没有署名的信" in prompt
                assert "被再问一遍也不多给" in prompt
            unit = [row for row in h.store.narrative_unit_list(instance_id, timeline_id, character_id=character_id)
                    if str(row["id"]) == "nu-deferred-1"][0]
            assert unit["stage"] == "deferred", "重复追问不得把保留内容变成已讲述"
            assert unit["message_id"] == ""
            assert h.store.narrative_unit_list(instance_id, timeline_id, character_id=character_id).__len__() == 1, \
                "追问轮不在她材料上新增单元"
            assert h.store.last_inbound(session_id)["state"] == "done"
        finally:
            await client.close()
            await mgmt.close()


# ------------------------------------------------------------------ §十二 请求改变世界


async def test_world_change_request_hands_off_and_explicit_path_works(tmp_path) -> None:
    """§十二 第五行：普通轮次回 handoff、不生成假装执行过的回复；只有 preview → commit 才改世界。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["（她不该在这一轮说话）"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
        thread = bound["thread"]
        try:
            before = _counts(h, instance_id, timeline_id)
            ref = await _say(client, thread, "帮我把世界设定改成终年下雪")
            notice = await client.expect(lambda env: env.type == "system_notice")
            assert "创作" in notice.payload["text"] and "没有执行" in notice.payload["text"]
            assert not h.fake.calls, "转交轮不生成角色回复"

            row = h.store.inbound_find(thread["channel_id"], thread["thread_id"], ref)
            assert row["state"] == "cancelled" and row["error_code"] == "handoff:creation"
            assert _counts(h, instance_id, timeline_id) == before, "转交不写世界"
            turn = await mgmt.call(
                "story.turn", instance_id=instance_id, timeline_id=timeline_id, character_id=cards[0]
            )
            assert turn["status"] == "handoff" and turn["handoff"] == "creation"
            assert turn["must_not_imply"] == "普通聊天已执行了请求"

            # 显式路径：preview 不改世界，commit 才落事实
            changes = [{"id": "c-1", "kind": "condition", "operation": "set", "certainty": "confirmed",
                        "target_refs": [cards[0]], "value": "封堤", "expiry": "until_cleared"}]
            preview = await mgmt.call("runtime.change.preview", instance_id=instance_id,
                                      timeline_id=timeline_id, changes=changes)
            assert preview["status"] == "ok" and _counts(h, instance_id, timeline_id) == before
            committed = await mgmt.call(
                "runtime.change.commit", instance_id=instance_id, timeline_id=timeline_id,
                changes=changes, idempotency_key="oc-1", preview_id=preview["preview_id"], source_module="oc_story",
            )
            assert committed["status"] == "ok"
            assert _counts(h, instance_id, timeline_id)["events"] == before["events"] + 1
        finally:
            await client.close()
            await mgmt.close()


# ------------------------------------------------------------------ §十二 TRPG 行动


async def test_trpg_action_never_rolls_dice_in_oc_session(tmp_path) -> None:
    """§十二 第六行：行动文本转入 TRPG 入口，OC 会话里不生成骰点、成功或失败结论。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["（她不该替玩家掷骰）"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
        thread = bound["thread"]
        try:
            before = _counts(h, instance_id, timeline_id)
            ref = await _say(client, thread, "我先攻，掷骰攻击那个守卫")
            notice = await client.expect(lambda env: env.type == "system_notice")
            assert "TRPG" in notice.payload["text"] and "掷骰" in notice.payload["text"]
            assert not h.fake.calls, "OC 会话不该为行动生成结论"
            row = h.store.inbound_find(thread["channel_id"], thread["thread_id"], ref)
            assert row["error_code"] == "handoff:trpg"
            assert _counts(h, instance_id, timeline_id) == before
            turn = await mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                   character_id=cards[0])
            assert turn["status"] == "handoff" and turn["handoff"] == "trpg"
        finally:
            await client.close()
            await mgmt.close()


# ------------------------------------------------------------------ §十二 离线回访


async def test_offline_return_reports_catching_up_with_completed_time(tmp_path) -> None:
    """§十二 第七行：追赶中只把已完成水位当已发生，目标时刻不冒充事实。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["刚巡堤回来。"]) as h:
        h.fake.judgements["输入分类"] = CONTACT
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt, activate=False)
        try:
            h.runtime.world.activate(instance_id, timeline_id, now_real=time.time(), rate=3600)
            await asyncio.sleep(1.05)  # 1 秒 ≈ 1 世界小时：目标水位跑在已完成水位前面
            scene = await mgmt.call("story.scene", instance_id=instance_id, timeline_id=timeline_id,
                                    character_id=cards[0])
            assert scene["product_state"] == "catching_up" and scene["label"] == "世界追赶中"
            assert scene["can"] == ["wait", "read_history"]
            assert scene["must_not_imply"] == "目标时刻已经发生"

            home = await mgmt.call("story.home", instance_id=instance_id, timeline_id=timeline_id,
                                   character_id=cards[0])
            assert home["time"]["catching_up"] is True
            assert home["time"]["world_seconds"] == scene["processed_watermark"]
            assert home["time"]["world_seconds"] < home["time"]["target_watermark"]
            assert any("追赶" in note for note in home["notes"])

            await _say(client, bound["thread"], "在吗")
            await client.expect(lambda env: env.type == "reply")
            turn = await mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                   character_id=cards[0])
            assert turn["status"] == "expressed", "这一轮已经固化：轮次主状态是已表达"
            assert turn["world_time"] == home["time"]["world_seconds"], "回复只用已完成的那一刻"
            still = await mgmt.call("story.scene", instance_id=instance_id, timeline_id=timeline_id,
                                    character_id=cards[0])
            assert still["product_state"] == "catching_up", "场景级仍是世界追赶中"
        finally:
            await client.close()
            await mgmt.close()


# ------------------------------------------------------------------ §十二 叙事生成失败


async def test_generation_failure_degrades_without_losing_facts(tmp_path) -> None:
    """§十二 第八行：失败时经历与获知保留、回复降级为暂缓、不重复消费素材。"""
    _fast(tmp_path)
    async with running_core(tmp_path, fail_with=LLMError("llm_unavailable", "boom")) as h:
        h.fake.judgements["输入分类"] = CONTACT
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
        character_id = cards[0]
        try:
            far = 10**15
            before = _counts(h, instance_id, timeline_id)
            experiences = len(h.store.experience_window(instance_id, timeline_id, character_id, until=far))
            consumed = h.store.narrative_consumed_refs(instance_id, timeline_id, character_id)
            ref = await _say(client, bound["thread"], "我今天把账本翻了一遍")
            await client.expect(lambda env: env.type == "error")

            row = h.store.inbound_find(bound["thread"]["channel_id"], bound["thread"]["thread_id"], ref)
            assert row["state"] == "failed" and row["error_code"] == "llm_unavailable"
            page = h.store.history_page(bound["session"]["id"], limit=20)
            assert not [item for item in page["messages"] if item["role"] == "character"], "失败不得留下假回复"
            assert _counts(h, instance_id, timeline_id) == before
            assert len(h.store.experience_window(instance_id, timeline_id, character_id, until=far)) == experiences
            assert h.store.narrative_consumed_refs(instance_id, timeline_id, character_id) == consumed, \
                "失败不得重复消费素材"

            turn = await mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                   character_id=character_id)
            assert turn["status"] == "deferred" and turn["error_kind"] == "model"
            assert turn["must_not_imply"] == "系统丢失了一个必达剧情"
            assert turn["note"] == "这次没生成出可用的回应；已发生的经历与她听过的事都还在，可以再聊一次。"
        finally:
            await client.close()
            await mgmt.close()


# ------------------------------------------------------------------ §十二 投递失败 / unknown


async def test_delivery_state_is_separate_from_fixed_state(tmp_path) -> None:
    """§十二 第九行：固化与投递分开报；未确认送达不算成功，也不重新生成同一回复。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["知道了。", "路上小心。"]) as h:
        h.fake.judgements["输入分类"] = CONTACT
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
        thread = bound["thread"]
        try:
            ref = await _say(client, thread, "我出门了")
            reply = await client.expect(lambda env: env.type == "reply")
            message_id = reply.payload["message_id"]
            sent = await mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                   character_id=cards[0])
            assert sent["status"] == "expressed" and sent["delivery"]["state"] == "sent"
            assert sent["delivery"]["delivered"] is False

            await client.report_delivery(
                thread_id=str(thread["thread_id"]), binding_token=str(thread["binding_token"]),
                message_id=message_id, batch_index=0, state="accepted",
            )
            delivered = await mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                        character_id=cards[0])
            assert delivered["delivery"]["state"] == "delivered" and delivered["delivery"]["delivered"] is True

            # 第二条：发出去了但通道没有回执 → 未知，不算送达，也不重新生成
            await _say(client, thread, "晚点再说")
            second = await client.expect(lambda env: env.type == "reply")
            fixed = h.store.outbound_by_message_id(second.payload["message_id"])
            h.store.delivery_set(int(fixed["seq"]), 0, "unknown")
            unknown = await mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                      character_id=cards[0])
            assert unknown["delivery"]["state"] == "unknown" and unknown["delivery"]["delivered"] is False
            assert unknown["delivery"]["note"] == "投递结果未知：不当作已送达"
            assert unknown["message_id"] == second.payload["message_id"]
            fixed_rows = [item for item in h.store.history_page(bound["session"]["id"], limit=20)["messages"]
                          if item["role"] == "character"]
            assert len(fixed_rows) == 2, "固化内容不因投递未知而重做"
            assert len(h.fake.calls) == 2, "不重新生成"
            # 按轮次查：先确认过的那条仍是 delivered（迟到回执不倒退）
            first_seq = int(h.store.inbound_find(thread["channel_id"], thread["thread_id"], ref)["seq"])
            again = await mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                    character_id=cards[0], seq=first_seq)
            assert again["delivery"]["state"] == "delivered" and again["message_id"] == message_id
        finally:
            await client.close()
            await mgmt.close()


# ------------------------------------------------------------------ §十二 同世界不同角色


async def test_same_world_characters_stay_isolated(tmp_path) -> None:
    """§十二 第十行：同一世界不同角色的会话与视图互不自动共享。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["记下了。", "我也听说了。"]) as h:
        h.fake.judgements["输入分类"] = CONTACT
        mgmt = await open_mgmt(h)
        first, bound_a, instance_id, timeline_id, cards = await _room(
            h, mgmt, names=("堤禾", "芦生"), channel="builtin", thread="dm-1"
        )
        second, bound_b = await bind_thread(
            h, mgmt, channel_id="second", thread_id="dm-2",
            instance=instance_id, timeline=timeline_id, character=cards[1],
        )
        secret = "我在盐滩捡到了一枚刻字的铜扣"
        try:
            await _say(first, bound_a["thread"], secret)
            await first.expect(lambda env: env.type == "reply")
            talk_a = _conversation(h)
            await _say(second, bound_b["thread"], "今天风大")
            await second.expect(lambda env: env.type == "reply")
            talk_b = _conversation(h)
            assert secret in talk_a and secret not in talk_b, "第二个角色的输入不得带第一个角色说过的话"
            assert "今天风大" in talk_b and "今天风大" not in talk_a, "各自的对话只在各自的上下文里"

            home_a = await mgmt.call("story.home", instance_id=instance_id, timeline_id=timeline_id,
                                     character_id=cards[0])
            home_b = await mgmt.call("story.home", instance_id=instance_id, timeline_id=timeline_id,
                                     character_id=cards[1])
            texts_a = [item["text"] for item in home_a["messages"]]
            texts_b = [item["text"] for item in home_b["messages"]]
            assert secret in texts_a and secret not in texts_b, "另一个角色的会话不出现他人的话"
            assert home_a["character"]["name"] == "堤禾" and home_b["character"]["name"] == "芦生"
            assert home_a["session"]["id"] != home_b["session"]["id"]
            assert {item["seq"] for item in home_a["messages"]} != {item["seq"] for item in home_b["messages"]}
        finally:
            await first.close()
            await second.close()
            await mgmt.close()


# ------------------------------------------------------------------ §十二 分支与恢复


async def test_branch_and_restore_orchestration(tmp_path) -> None:
    """§十二 第十一行：分支后未来不回流；恢复前先说覆盖范围与保存路径；已投递内容不宣称可撤回。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["嗯。", "又是一天。"]) as h:
        h.fake.judgements["输入分类"] = CONTACT
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
        character_id = cards[0]
        try:
            await _say(client, bound["thread"], "今天先到这儿")
            await client.expect(lambda env: env.type == "reply")
            mark = await mgmt.call("runtime.commit", instance_id=instance_id, timeline_id=timeline_id, note="分支点")
            commit_id = mark["commit"]["id"]
            point_world = int(h.store.commit_snapshot_get(commit_id)["world"])
            # 场景时间往前跳一小时：原线继续生活，分支点仍是提交那一刻
            h.runtime.world.consume_time(instance_id, timeline_id, seconds=3600, cause="日常推进",
                                         now_real=time.time(), max_batches=8)
            line_world = int(h.store.clock_get(timeline_id)["processed_world"])
            assert line_world > point_world

            branched = await mgmt.call("story.branch", instance_id=instance_id, timeline_id=timeline_id,
                                       commit_id=commit_id, name="另一种可能")
            new_line = branched["timeline"]["id"]
            assert branched["status"] == "ok" and branched["timeline"]["state"] == "frozen"
            assert branched["must_not_imply"] == "原线已经被替换"
            # 分支继承共同过去，原线此后的推进不回流
            h.runtime.world.consume_time(instance_id, timeline_id, seconds=3600, cause="继续推进",
                                         now_real=time.time(), max_batches=8)
            branch_world = int(h.store.clock_get(new_line)["processed_world"])
            assert branch_world == point_world, "分支停在分叉点"
            assert int(h.store.clock_get(timeline_id)["processed_world"]) > line_world

            # 恢复前：只说覆盖范围与保存路径，世界一字不动
            preview = await mgmt.call("story.restore", instance_id=instance_id, timeline_id=timeline_id,
                                      commit_id=commit_id)
            assert preview["status"] == "waiting" and preview["coverage"]["commit_id"] == commit_id
            assert {item["action"] for item in preview["save_paths"]} == {"branch", "export"}
            assert preview["must_not_imply"] == "可以靠重试绕过闸门"
            assert int(h.store.clock_get(timeline_id)["processed_world"]) > 0
            generation = int(h.store.clock_get(timeline_id)["generation"])

            # 已确认但没先保存：仍停住
            held = await mgmt.call("story.restore", instance_id=instance_id, timeline_id=timeline_id,
                                   commit_id=commit_id, confirm=True)
            assert held["status"] == "waiting" and "先保存" in held["reason"]

            # 有已投递回复：覆盖提示不得宣称可撤回
            page = h.store.history_page(bound["session"]["id"], limit=10)
            her = [item for item in page["messages"] if item["role"] == "character"][0]
            h.store.delivery_set(int(her["seq"]), 0, "accepted")

            done = await mgmt.call("story.restore", instance_id=instance_id, timeline_id=timeline_id,
                                   commit_id=commit_id, confirm=True, saved=True)
            assert done["status"] == "ok" and int(done["result"]["generation"]) > generation
            assert "投递到外部平台" in done["warning"] and "不保证" in done["warning"]
            assert done["coverage"]["delivered_replies"] == 1, "覆盖范围要如实报已投递条数"
            assert done["must_not_imply"] == "已经投递出去的内容可以撤回"
            assert int(h.store.clock_get(timeline_id)["processed_world"]) == done["coverage"]["to_world"]
        finally:
            await client.close()
            await mgmt.close()


# ------------------------------------------------------------------ §十二 世界冻结 / 版本阻断


async def test_frozen_line_blocks_turn_and_cannot_be_bypassed(tmp_path) -> None:
    """§十二 第十二行：被阻断只读、给恢复入口；普通重试绕不过闸门。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["（冻结线不该说话）"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
        try:
            h.runtime.world.freeze(instance_id, timeline_id)
            scene = await mgmt.call("story.scene", instance_id=instance_id, timeline_id=timeline_id,
                                    character_id=cards[0])
            assert scene["product_state"] == "blocked" and scene["can"] == ["view_reason", "recover_or_export"]
            assert scene["must_not_imply"] == "可以靠重试绕过闸门"
            home = await mgmt.call("story.home", instance_id=instance_id, timeline_id=timeline_id,
                                    character_id=cards[0])
            assert any("冻结" in note for note in home["notes"])

            await client.send_user_message(
                thread_id=str(bound["thread"]["thread_id"]),
                binding_token=str(bound["thread"]["binding_token"]),
                text="在吗",
            )
            error = await client.expect(lambda env: env.type == "error", timeout=10)
            assert error.payload["code"] == "state_blocked" and "不可对话" in error.payload["message"]
            assert h.store.last_inbound(str(bound["session"]["id"])) is None, "被闸门拒绝的输入不落库"
            await asyncio.sleep(0.05)
            assert not h.fake.calls, "闸门挡在生成之前"
            page = h.store.history_page(bound["session"]["id"], limit=10)
            assert not [item for item in page["messages"] if item["role"] == "character"]

            turn = await mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                   character_id=cards[0])
            assert turn["status"] == "blocked" and turn["product_state"] == "blocked"
            assert turn["can"] == ["view_reason", "recover_or_export"]
        finally:
            await client.close()
            await mgmt.close()


# ------------------------------------------------------------------ §3.4 分类闭集


async def test_classify_closed_set_and_handoff_targets(tmp_path) -> None:
    """§3.4：六类闭集；三类结构性请求给出转交流程；分类只影响路由、不改变权限。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            scripted = {
                "contact_share": "",
                "status_inquiry": "",
                "followup": "",
                "world_change": "creation",
                "version_op": "version",
                "trpg_action": "trpg",
            }
            for category, target in scripted.items():
                h.fake.judgements["输入分类"] = json.dumps(
                    {"category": category, "why": "脚本"}, ensure_ascii=False
                )
                verdict = await mgmt.call("story.classify", text="这是一句测试输入")
                assert verdict["category"] == category and verdict["source"] == "model"
                assert verdict["handoff"] == target
                assert verdict["label"] in verdict["labels"].values()
                assert set(verdict["categories"]) == {
                    "contact_share", "status_inquiry", "followup", "world_change", "version_op", "trpg_action",
                }
            # 预筛硬命中（结构性请求）省一次调用；分不清时按联络处理
            before = len(h.fake.judgement_calls)
            hit = await mgmt.call("story.classify", text="回滚到上一个存档")
            assert hit["category"] == "version_op" and hit["source"] == "rule"
            assert len(h.fake.judgement_calls) == before, "结构性请求不再花判断点调用"
            h.fake.judgements.clear()
            fallback = await mgmt.call("story.classify", text="嗯……")
            assert fallback["category"] == "contact_share" and fallback["source"] == "default"
        finally:
            await mgmt.close()


# ------------------------------------------------------------------ §6 用户可见面黑箱


async def test_visible_surface_has_no_internals(tmp_path) -> None:
    """§6.2：产品面上不出现实情层、事件表、记忆表、评分、候选与提示词这类内部字段。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["嗯。"]) as h:
        h.fake.judgements["输入分类"] = CONTACT
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, cards = await _room(h, mgmt)
        try:
            await _say(client, bound["thread"], "今天怎么样")
            await client.expect(lambda env: env.type == "reply")
            payloads = [
                await mgmt.call("story.home", instance_id=instance_id, timeline_id=timeline_id,
                                character_id=cards[0]),
                await mgmt.call("story.turn", instance_id=instance_id, timeline_id=timeline_id,
                                character_id=cards[0]),
                await mgmt.call("story.scene", instance_id=instance_id, timeline_id=timeline_id,
                                character_id=cards[0]),
            ]
            for payload in payloads:
                assert view.internals_in(payload) == [], view.internals_in(payload)
            assert view.internals_in({"truth_layer": 1, "memory_row": {}, "prompt": "x"}) != [], \
                "自检本身要能抓出内部字段（否则这条断言是空跑）"
        finally:
            await client.close()
            await mgmt.close()
