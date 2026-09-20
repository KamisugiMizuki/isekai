"""睡眠期等待与合并（SESSION_CORE_SPEC §4.5）行为验收：真核心 + 真 WS，只换 LLM。"""

from __future__ import annotations

import asyncio
import time

import pytest

from conftest import bind_thread, open_mgmt, running_core
from isekai_core import ump
from isekai_core.client import UmpClient
from isekai_core.runtime import life
from isekai_core.session import SLEEP_REPLY_HINT, WAKE_REPLY_HINT
from isekai_core.world.instances import create_instance
from samples import DAY, sample_card, sample_package

#: 样本角色的睡眠块在世界日首 [0, 25200)：DAY*1500 落在睡眠块内，+30000 已是她白天
SLEEP_AT = DAY * 1500
AWAKE_AT = DAY * 1500 + 30000


def use_wait(tmp_path, *, wait_s: float, capacity: int = 4) -> None:
    """把等待参数写进配置：上下界相同 = 一拍就是 wait_s（测试里要确定值）。"""
    folder = tmp_path / "config"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(
        "runtime:\n"
        f"  sleep_wait_min_s: {wait_s}\n"
        f"  sleep_wait_max_s: {wait_s}\n"
        f"  merge_batch_max: {capacity}\n",
        encoding="utf-8",
    )


async def room(h, mgmt, *, moment: int = SLEEP_AT, thread_id: str = "dm-1"):
    """真实实例 + 已激活时间线 + 绑定 thread（默认世界时刻落在她的睡眠块里）。"""
    package = sample_package(moment=moment)
    card = sample_card(package)
    info = create_instance(h.store, package, [card])
    timeline_id = h.store.timeline_list(info["id"])[0]["id"]
    character_id = str(card["meta"]["card_id"])
    h.runtime.world.ensure_instance(info["id"], now_real=time.time())
    h.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
    client, bound = await bind_thread(
        h,
        mgmt,
        channel_id="builtin",
        thread_id=thread_id,
        instance=info["id"],
        timeline=timeline_id,
        character=character_id,
    )
    return client, bound, info["id"], timeline_id, character_id


async def test_sleep_inputs_merge_into_one_fixed_reply(tmp_path):
    """睡眠期两条连续输入 → 一份固化回复：covers 含两条、reply_to 是最后一条，不吞输入。"""
    use_wait(tmp_path, wait_s=0.25)
    async with running_core(tmp_path, replies=["……唔。听见了。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await room(h, mgmt)
        thread, session_id = bound["thread"], bound["session"]["id"]
        token, channel_id = thread["binding_token"], thread["channel_id"]
        plan_before = h.store.plan_get(instance_id, timeline_id, character_id, 1500)
        try:
            first = await client.send_user_message(thread_id="dm-1", binding_token=token, text="睡了吗")
            await client.expect(lambda e: e.type == "accepted")
            await asyncio.sleep(0.08)  # 第二条在她还没到点前到达
            second = await client.send_user_message(thread_id="dm-1", binding_token=token, text="明天几点上堤")
            queued = await client.expect(lambda e: e.type == "accepted")
            assert queued.payload == {"ref": second, "state": "queued", "message_id": None}

            reply = await client.expect(lambda e: e.type == "reply", timeout=5)
            assert reply.payload["covers"] == [first, second], "批内保留接受顺序与各自标识"
            assert reply.payload["reply_to"] == second, "reply_to 关联批内最后一条入站"
            assert reply.thread_id == "dm-1"
            assert len(h.fake.calls) == 1, "一份固化回复：只生成一次"

            rows = [h.store.inbound_find(channel_id, "dm-1", ref) for ref in (first, second)]
            assert [row["text"] for row in rows] == ["睡了吗", "明天几点上堤"], "各输入保留自己的原文与顺序"
            assert [row["state"] for row in rows] == ["done", "done"]
            assert {row["reply_message_id"] for row in rows} == {reply.payload["message_id"]}
            assert rows[0]["wait_until"] > 0 and rows[1]["wait_until"] == 0, "截止点一次确定，不按条数叠加"

            call = h.fake.calls[0]
            assert SLEEP_REPLY_HINT in call[0]["content"], "仍睡：短暂朦胧，不声称起身做事"
            assert WAKE_REPLY_HINT not in call[0]["content"]
            assert [item["content"] for item in call if item["role"] == "user"] == ["睡了吗", "明天几点上堤"]

            plan_after = h.store.plan_get(instance_id, timeline_id, character_id, 1500)
            assert plan_after["windows"] == plan_before["windows"], "应答不改生活安排"
            moment = h.runtime.world.world_moment(instance_id, timeline_id)
            window = life.current_window(plan_after, moment)
            assert window is not None and window["activity"] == "sleep", "睡眠块没被截断：应答后她仍在同一块里"

            tasks = h.store.memory_tasks(instance_id, timeline_id)
            assert sorted(item["source_ref"] for item in tasks) == sorted(
                [f"user:{first}", f"user:{second}", f"reply:{reply.payload['message_id']}"]
            ), "各输入各登记一次，回复只登记一次：不重复提取同一来源"
        finally:
            await client.close()
            await mgmt.close()


async def test_any_covered_input_returns_the_same_batch_result(tmp_path):
    """查询或重试批内任一输入：同一批状态、同一 message_id，不逐条补发、不重新生成。"""
    use_wait(tmp_path, wait_s=0.2)
    async with running_core(tmp_path, replies=["……唔。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await room(h, mgmt)
        token = bound["thread"]["binding_token"]
        session_id = bound["session"]["id"]
        try:
            first = await client.send_user_message(thread_id="dm-1", binding_token=token, text="睡了吗")
            await client.expect(lambda e: e.type == "accepted")
            second = await client.send_user_message(thread_id="dm-1", binding_token=token, text="明天几点上堤")
            await client.expect(lambda e: e.type == "accepted")
            reply = await client.expect(lambda e: e.type == "reply", timeout=5)
            message_id = reply.payload["message_id"]

            # 重发批内第一条：返回同批状态与同一 message_id（不重新生成）
            duplicate = ump.make(
                "user_message", {"text": "睡了吗"}, thread_id="dm-1", binding_token=token, id=first
            )
            await client.send(duplicate)
            again = await client.expect(lambda e: e.type == "accepted")
            assert again.payload == {"ref": first, "state": "done", "message_id": message_id}

            # 显式重试批内另一条：同样落到这一批
            await client.request_retry(thread_id="dm-1", binding_token=token, ref=second)
            restored = await client.expect(lambda e: e.type == "accepted")
            assert restored.payload == {"ref": second, "state": "done", "message_id": message_id}
            assert len(h.fake.calls) == 1, "查询 / 重试都不重新生成"
            assert h.store.counts()["messages"] == 3, "两条入站 + 一份回复：不逐条补发"
        finally:
            await client.close()
            await mgmt.close()


async def test_deadline_is_not_reset_by_later_input(tmp_path):
    """截止点以首次接受该批输入的现实时间为基准：第二条输入不重置、不叠加。"""
    use_wait(tmp_path, wait_s=0.8)
    async with running_core(tmp_path, replies=["……唔。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await room(h, mgmt)
        token, channel_id = bound["thread"]["binding_token"], bound["thread"]["channel_id"]
        try:
            started = time.monotonic()
            first = await client.send_user_message(thread_id="dm-1", binding_token=token, text="睡了吗")
            await client.expect(lambda e: e.type == "accepted")
            await asyncio.sleep(0.4)
            second = await client.send_user_message(thread_id="dm-1", binding_token=token, text="还在吗")
            await client.expect(lambda e: e.type == "accepted")
            reply = await client.expect(lambda e: e.type == "reply", timeout=5)
            elapsed = time.monotonic() - started

            assert reply.payload["covers"] == [first, second], "仍是同一批"
            assert elapsed < 1.0, f"到点是首条的一拍（0.8s），不是第二条之后重算：{elapsed:.2f}s"
            assert h.store.inbound_find(channel_id, "dm-1", second)["wait_until"] == 0
        finally:
            await client.close()
            await mgmt.close()


async def test_capacity_seals_batch_and_next_input_starts_a_new_one(tmp_path):
    """达到容量即封口：批容量 2 时第三条属下一批，各批各一份固化回复。"""
    use_wait(tmp_path, wait_s=0.2, capacity=2)
    async with running_core(tmp_path, replies=["……唔。", "……又怎么了。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await room(h, mgmt)
        token = bound["thread"]["binding_token"]
        try:
            refs = []
            for text in ("睡了吗", "明天几点上堤", "还有件事"):
                refs.append(await client.send_user_message(thread_id="dm-1", binding_token=token, text=text))
            first = await client.expect(lambda e: e.type == "reply", timeout=5)
            second = await client.expect(lambda e: e.type == "reply", timeout=5)
            assert first.payload["covers"] == refs[:2] and first.payload["reply_to"] == refs[1]
            assert second.payload["covers"] == refs[2:] and second.payload["reply_to"] == refs[2]
            assert len(h.fake.calls) == 2
        finally:
            await client.close()
            await mgmt.close()


async def test_inputs_from_other_threads_are_not_merged(tmp_path):
    """不跨通道 / thread 合并：同会话的另一个 thread 属下一批，不并进这一份回复。"""
    use_wait(tmp_path, wait_s=0.4)
    async with running_core(tmp_path, replies=["……唔。", "……这边的。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await room(h, mgmt)
        session_id = bound["session"]["id"]
        channel_id = bound["thread"]["channel_id"]
        other = (
            await mgmt.call(
                "thread.bind",
                channel="builtin",
                thread_id="dm-2",
                session_id=session_id,
            )
        )["thread"]
        try:
            first = await client.send_user_message(
                thread_id="dm-1", binding_token=bound["thread"]["binding_token"], text="睡了吗"
            )
            second = await client.send_user_message(
                thread_id="dm-2", binding_token=other["binding_token"], text="这边也发一条"
            )
            one = await client.expect(
                lambda e: e.type == "reply" and e.thread_id == "dm-1", timeout=5
            )
            first_done = time.monotonic()
            two = await client.expect(
                lambda e: e.type == "reply" and e.thread_id == "dm-2", timeout=5
            )
            gap = time.monotonic() - first_done
            assert one.payload["covers"] == [first], "另一个来源不算同批"
            assert two.payload["covers"] == [second]
            assert len(h.fake.calls) == 2
            assert h.store.inbound_find(channel_id, "dm-2", second)["wait_until"] > 0
            assert gap < 0.25, f"截止点按各自接受时刻算，排队已耗去的时间计入等待：{gap:.2f}s"
        finally:
            await client.close()
            await mgmt.close()


async def test_freeze_or_rebind_invalidates_waiting_turn(tmp_path):
    """等待中被重绑 / 冻结：旧任务失效，批内输入结算为取消，不生成、不投递。"""
    use_wait(tmp_path, wait_s=0.6)
    async with running_core(tmp_path, replies=["不该出现。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await room(h, mgmt)
        token, channel_id = bound["thread"]["binding_token"], bound["thread"]["channel_id"]
        session_id = bound["session"]["id"]
        try:
            # 重绑
            first = await client.send_user_message(thread_id="dm-1", binding_token=token, text="睡了吗")
            await client.expect(lambda e: e.type == "accepted")
            await asyncio.sleep(0.15)
            await mgmt.call("thread.bind", channel="builtin", thread_id="dm-1", session_id=session_id)
            await asyncio.sleep(0.7)
            row = h.store.inbound_find(channel_id, "dm-1", first)
            assert (row["state"], row["error_code"]) == ("cancelled", ump.Err.BINDING_EXPIRED)
            with pytest.raises(TimeoutError):
                await client.expect(lambda e: e.type == "reply", timeout=0.4)

            # 冻结（同一时间线：另一个 thread 的等待任务也失效）
            other = (
                await mgmt.call("thread.bind", channel="builtin", thread_id="dm-2", session_id=session_id)
            )["thread"]
            second = await client.send_user_message(
                thread_id="dm-2", binding_token=other["binding_token"], text="那边还醒着吗"
            )
            await client.expect(lambda e: e.type == "accepted")
            await asyncio.sleep(0.15)
            h.runtime.world.freeze(instance_id, timeline_id, now_real=time.time())
            await asyncio.sleep(0.7)
            frozen = h.store.inbound_find(channel_id, "dm-2", second)
            assert (frozen["state"], frozen["error_code"]) == ("cancelled", ump.Err.STATE_BLOCKED)
            assert h.fake.calls == [], "失效的等待任务不生成回复"
        finally:
            await client.close()
            await mgmt.close()


async def test_waking_up_during_the_wait_replies_from_the_real_state(tmp_path):
    """到期按真实状态表达：等待期间世界推进到她醒了，回复不得仍按睡意说话。"""
    use_wait(tmp_path, wait_s=0.6)
    async with running_core(tmp_path, replies=["嗯……我刚醒。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await room(h, mgmt)
        token = bound["thread"]["binding_token"]
        try:
            first = await client.send_user_message(thread_id="dm-1", binding_token=token, text="睡了吗")
            await client.expect(lambda e: e.type == "accepted")
            await asyncio.sleep(0.2)
            # 世界自然推进：睡眠块结束（世界时钟走 30000 秒到她白天）
            h.runtime.world.advance(instance_id, timeline_id, now_real=time.time() + 30000)
            reply = await client.expect(lambda e: e.type == "reply", timeout=5)

            assert reply.payload["covers"] == [first]
            call = h.fake.calls[0]
            assert WAKE_REPLY_HINT in call[0]["content"], "已醒来：按清醒状态回复"
            assert SLEEP_REPLY_HINT not in call[0]["content"], "不为套睡意否认世界推进"
            plan = h.store.plan_latest(instance_id, timeline_id, character_id)
            moment = h.runtime.world.world_moment(instance_id, timeline_id)
            window = life.current_window(plan, moment)
            assert window is not None and window["activity"] == "duty", "此刻她确实已经不在睡眠块里"
        finally:
            await client.close()
            await mgmt.close()


async def test_awake_and_unreadable_life_lines_do_not_wait(tmp_path):
    """非睡眠时段不额外等待（照旧逐条）；生活线不可读（占位会话）同样不等待、不凭现实钟猜。"""
    use_wait(tmp_path, wait_s=0.6)
    async with running_core(tmp_path, replies=["一。", "二。", "三。", "四。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await room(h, mgmt, moment=AWAKE_AT)
        token, channel_id = bound["thread"]["binding_token"], bound["thread"]["channel_id"]
        try:
            started = time.monotonic()
            first = await client.send_user_message(thread_id="dm-1", binding_token=token, text="在吗")
            one = await client.expect(lambda e: e.type == "reply", timeout=5)
            quick = time.monotonic() - started
            second = await client.send_user_message(thread_id="dm-1", binding_token=token, text="忙吗")
            two = await client.expect(lambda e: e.type == "reply", timeout=5)
            assert one.payload["covers"] == [first] and two.payload["covers"] == [second], "清醒时逐条回复"
            assert quick < 0.3, f"清醒时段不额外等待（等待拍是 0.6 秒）：{quick:.2f}s"
            assert h.store.inbound_find(channel_id, "dm-1", first)["wait_until"] == 0

            # 占位会话：没有生活线可读
            ph_client, ph_bound = await bind_thread(h, mgmt, channel_id="ph", thread_id="dm-9")
            try:
                third = await ph_client.send_user_message(
                    thread_id="dm-9", binding_token=ph_bound["thread"]["binding_token"], text="在吗"
                )
                fourth = await ph_client.send_user_message(
                    thread_id="dm-9", binding_token=ph_bound["thread"]["binding_token"], text="在吗在吗"
                )
                ph_one = await ph_client.expect(lambda e: e.type == "reply", timeout=5)
                ph_two = await ph_client.expect(lambda e: e.type == "reply", timeout=5)
                assert ph_one.payload["covers"] == [third] and ph_two.payload["covers"] == [fourth]
                assert (
                    h.store.inbound_find(
                        ph_bound["thread"]["channel_id"], "dm-9", third
                    )["wait_until"]
                    == 0
                ), "生活线不可读按未就绪处理：不等待"
            finally:
                await ph_client.close()
        finally:
            await client.close()
            await mgmt.close()


async def test_interrupted_wait_resumes_on_retry_without_a_fresh_beat(tmp_path):
    """进程中断：沿「标记中断 → 显式重试」恢复，不重抽等待、不重新等待完整节拍。"""
    use_wait(tmp_path, wait_s=0.6)
    async with running_core(tmp_path, replies=["不该出现。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await room(h, mgmt)
        token, channel_id = bound["thread"]["binding_token"], bound["thread"]["channel_id"]
        credential = bound["credential"]
        ref = await client.send_user_message(thread_id="dm-1", binding_token=token, text="睡了吗")
        await client.expect(lambda e: e.type == "accepted")
        await asyncio.sleep(0.2)  # 中断发生在到点之前
        deadline = h.store.inbound_find(channel_id, "dm-1", ref)["wait_until"]
        assert deadline > 0, "中断前截止点已经固化"
        await client.close()
        await mgmt.close()

    async with running_core(tmp_path, replies=["刚醒了一会儿。"]) as h2:
        row = h2.store.inbound_find(channel_id, "dm-1", ref)
        assert (row["state"], row["error_code"]) == ("failed", "interrupted"), "沿既有中断标记，不静默漏单"
        assert row["wait_until"] == deadline, "不重抽等待：沿用既有的截止点"
        client = UmpClient(
            endpoint=h2.endpoint, channel_id="builtin", name="builtin", credential=credential
        )
        await client.connect()
        try:
            started = time.monotonic()
            await client.request_retry(thread_id="dm-1", binding_token=token, ref=ref)
            reply = await client.expect(lambda e: e.type == "reply", timeout=5)
            waited = time.monotonic() - started
            assert reply.payload["covers"] == [ref] and reply.payload["reply_to"] == ref
            assert h2.store.inbound_find(channel_id, "dm-1", ref)["state"] == "done"
            assert waited < 0.45, f"只等到原截止点，不重新等待完整节拍（一拍 0.6 秒）：{waited:.2f}s"
        finally:
            await client.close()


async def test_failed_batch_settles_every_covered_input(tmp_path):
    """生成失败同样结算到批内各输入：各自失败状态，不留「处理中」。"""
    from isekai_core.llm import LLMError

    use_wait(tmp_path, wait_s=0.2)
    failure = LLMError("llm_unreachable", "网络不可达", retryable=True)
    async with running_core(tmp_path, fail_with=failure) as h:
        mgmt = await open_mgmt(h)
        client, bound, instance_id, timeline_id, character_id = await room(h, mgmt)
        token, channel_id = bound["thread"]["binding_token"], bound["thread"]["channel_id"]
        try:
            first = await client.send_user_message(thread_id="dm-1", binding_token=token, text="睡了吗")
            second = await client.send_user_message(thread_id="dm-1", binding_token=token, text="明天几点上堤")
            error = await client.expect(lambda e: e.type == "error", timeout=5)
            assert error.payload["code"] == "llm_unreachable"
            for ref in (first, second):
                row = h.store.inbound_find(channel_id, "dm-1", ref)
                assert (row["state"], row["error_code"]) == ("failed", "llm_unreachable"), ref
        finally:
            await client.close()
            await mgmt.close()
