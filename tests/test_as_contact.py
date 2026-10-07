"""「仅作为联络发送」（USER_INTERFACE_DESIGN §6.3）的行为验收。

真 WebSocket + 真 SQLite + 真实例，只有 LLM 换成 FakeLLM；判据落在可观察行为上：
入站行的 `intent` 与处理状态、有没有 `system_notice` / notice 行、有没有真的生成角色回复。

这一条受控入口的全部作用是**改变分类**：带上 `as_contact:true` 的轮次跳过 `story.classify`，
按普通联络正常生成；权限（认知与写入）不受影响，也不绕开分类去直接普通生成——
意图本身随新消息走 UMP 校验，核心只认落库的 `intent`。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from conftest import bind_thread, open_mgmt, running_core
from isekai_core import ump
from isekai_core.store import Store
from isekai_core.world.instances import create_instance
from samples import DAY, sample_card, sample_package

#: 预筛硬命中 world_change 的文本：不带意图时这一轮必定转交，用来做同文本对照
STRUCTURAL = "帮我把世界设定改成终年下雪"


def _fast(tmp_path) -> None:
    """睡眠期等待压到 0.05s：测试不该为节拍等上分钟（样本角色此刻也不在睡眠块里）。"""
    folder = Path(tmp_path) / "config"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "config.yaml").write_text(
        "runtime:\n  sleep_wait_min_s: 0.05\n  sleep_wait_max_s: 0.05\n", encoding="utf-8"
    )


async def _room(h, mgmt):
    """真实例 + 激活 + 绑定第一个角色的 thread（与 test_oc_story 的装配同口径）。"""
    package = sample_package(moment=DAY * 1500 + 30000)
    cards = [sample_card(package, name="堤禾")]
    info = create_instance(h.store, package, cards)
    timeline_id = h.store.timeline_list(info["id"])[0]["id"]
    h.runtime.world.ensure_instance(info["id"], now_real=time.time())
    h.runtime.world.activate(info["id"], timeline_id, now_real=time.time())
    client, bound = await bind_thread(
        h,
        mgmt,
        channel_id="builtin",
        thread_id="dm-1",
        instance=info["id"],
        timeline=timeline_id,
        character=str(cards[0]["meta"]["card_id"]),
    )
    return client, bound


async def _send(client, thread: dict, text: str, *, as_contact: bool = False) -> str:
    payload: dict[str, object] = {"text": text}
    if as_contact:
        payload["as_contact"] = True
    env = ump.make(
        "user_message",
        payload,
        thread_id=str(thread["thread_id"]),
        binding_token=str(thread["binding_token"]),
    )
    await client.send(env)
    return str(env["id"])


def _idle(env) -> bool:
    return env.type == "status" and str(env.payload.get("state")) == "idle"


async def test_as_contact_round_replies_without_handoff(tmp_path) -> None:
    """§6.3 正路：带 `as_contact:true` 的结构性请求按联络处理——出回复、无通知、行不 cancelled。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["我在，堤上风大。"]) as h:
        mgmt = await open_mgmt(h)
        client, bound = await _room(h, mgmt)
        thread = bound["thread"]
        try:
            ref = await _send(client, thread, STRUCTURAL, as_contact=True)
            collected: list = []
            reply = await client.expect(lambda env: env.type == "reply", collect=collected)
            await client.expect(_idle, collect=collected)  # 这一轮真的收尾了
            assert h.fake.calls, "按联络处理要真的生成角色回复"
            assert h.fake.judgement_calls == [], "带 as_contact 的轮次跳过分类调用（不花判断点）"
            assert not [env for env in collected if env.type == "system_notice"], "联络轮不该有转交通知"
            assert "".join(str(part.get("text") or "") for part in reply.payload["parts"]) == "我在，堤上风大。"

            row = h.store.inbound_find(
                str(thread["channel_id"]), str(thread["thread_id"]), ref
            )
            assert row is not None, "入站行要落库"
            assert row["intent"] == "as_contact", "意图标记要持久化"
            assert row["state"] == "done" and not row["error_code"], "这一轮正常收尾，不被 cancelled"
            page = h.store.history_page(str(bound["session"]["id"]))
            assert not [item for item in page["messages"] if item["role"] == "notice"], "不该落系统说明行"
        finally:
            await client.close()
            await mgmt.close()


async def test_same_text_without_as_contact_still_hands_off(tmp_path) -> None:
    """§6.3 对照：同一句话不带意图 → 照旧转交——出通知、入站 cancelled、不生成假装执行过的回复。"""
    _fast(tmp_path)
    async with running_core(tmp_path, replies=["（她不该在这一轮说话）"]) as h:
        mgmt = await open_mgmt(h)
        client, bound = await _room(h, mgmt)
        thread = bound["thread"]
        try:
            ref = await _send(client, thread, STRUCTURAL)
            collected: list = []
            notice = await client.expect(lambda env: env.type == "system_notice", collect=collected)
            await client.expect(_idle, collect=collected)
            assert "创作" in str(notice.payload["text"]), "转交说明照旧"
            assert not h.fake.calls, "转交轮不生成角色回复"
            assert not [env for env in collected if env.type == "reply"], "不该有角色回复"

            row = h.store.inbound_find(
                str(thread["channel_id"]), str(thread["thread_id"]), ref
            )
            assert row["state"] == "cancelled" and row["error_code"] == "handoff:creation"
            assert row["intent"] == "", "不带意图就不该有标记"
        finally:
            await client.close()
            await mgmt.close()


async def test_as_contact_accepts_only_true(tmp_path) -> None:
    """wire：`as_contact` 只允许 true 或省略；其它值一律 protocol_error，不静默放行。"""
    _fast(tmp_path)
    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        client, info = await bind_thread(h, mgmt, channel_id="ac-probe", thread_id="dm-ac")
        thread = info["thread"]
        try:
            for bad in ("yes", 1, False, 0):
                env = ump.make(
                    "user_message",
                    {"text": "随便一句", "as_contact": bad},
                    thread_id="dm-ac",
                    binding_token=str(thread["binding_token"]),
                )
                await client.send(env)
                error = await client.expect(lambda item: item.type == "error")
                assert error.payload["code"] == ump.Err.PROTOCOL, (bad, error.payload)
                assert "as_contact" in str(error.payload.get("message") or ""), (bad, error.payload)
                assert h.store.inbound_find("ac-probe", "dm-ac", str(env["id"])) is None, (
                    f"被拒的载荷不该落库：{bad!r}"
                )
        finally:
            await client.close()
            await mgmt.close()


def test_message_intent_column_migrates_idempotently(tmp_path) -> None:
    """旧库（无 `intent` 列）打开后列可用；同一库开两次不炸（幂等迁移）。"""
    db = tmp_path / "data" / "isekai.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    # 早先形态的 message 表：没有 intent 列（老库原样）
    conn = sqlite3.connect(db)
    conn.execute(
        """CREATE TABLE message(
             seq INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
             channel_id TEXT, thread_id TEXT, env_id TEXT, binding_version INTEGER, binding_token TEXT,
             text TEXT, parts TEXT, message_id TEXT, reply_message_id TEXT, reply_to TEXT,
             batch_id TEXT, batch_index INTEGER, batch_count INTEGER, covers TEXT DEFAULT '[]',
             wait_until REAL NOT NULL DEFAULT 0, model_fingerprint TEXT NOT NULL DEFAULT '',
             attachments TEXT NOT NULL DEFAULT '[]', state TEXT NOT NULL, error_code TEXT,
             created_at REAL NOT NULL)"""
    )
    conn.commit()
    conn.close()

    store = Store(db)
    try:
        store.ensure_schema()
        store.ensure_schema()  # 第二次打开同一库：迁移必须幂等
        columns = {row["name"] for row in store._conn.execute("PRAGMA table_info(message)")}
        assert "intent" in columns, "旧库打开后应补上 intent 列"
        row, created = store.inbound_put(
            session_id="s-1", channel_id="c-1", thread_id="t-1", env_id="e-as-contact",
            binding_version=1, text="带意图的入站", intent="as_contact",
        )
        assert created and store.message_get(row["seq"])["intent"] == "as_contact"
        plain, _ = store.inbound_put(
            session_id="s-1", channel_id="c-1", thread_id="t-1", env_id="e-plain",
            binding_version=1, text="普通入站",
        )
        assert store.message_get(plain["seq"])["intent"] == "", "默认空串：既有调用方零影响"
    finally:
        store.close()

    again = Store(db)
    try:
        again.ensure_schema()  # 新进程再开一次：仍然不炸、列仍在
        columns = {row["name"] for row in again._conn.execute("PRAGMA table_info(message)")}
        assert "intent" in columns
    finally:
        again.close()
