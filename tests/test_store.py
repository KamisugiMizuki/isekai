"""持久层行为：去重、冲突、绑定换代、投递状态单调性、作废记录。"""

from __future__ import annotations

import pytest

from isekai_core.store import EnvelopeConflict, Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "isekai.db")
    s.ensure_schema()
    yield s
    s.close()


def put_inbound(store: Store, *, channel="ci-1", thread="dm-1", env_id="e-1", text="hi"):
    return store.inbound_put(
        session_id="se-1",
        channel_id=channel,
        thread_id=thread,
        env_id=env_id,
        binding_version=1,
        text=text,
    )


def test_same_key_same_text_returns_existing_row(store):
    row, created = put_inbound(store)
    assert created is True
    again, created_again = put_inbound(store)
    assert created_again is False
    assert again["seq"] == row["seq"]
    assert store.counts()["messages"] == 1


def test_same_key_different_text_conflicts(store):
    put_inbound(store, text="hi")
    with pytest.raises(EnvelopeConflict) as excinfo:
        put_inbound(store, text="hello")
    assert excinfo.value.existing["text"] == "hi"


def test_same_env_id_on_different_channels_is_independent(store):
    first, _ = put_inbound(store, channel="ci-1")
    second, created = put_inbound(store, channel="ci-2")
    assert created is True
    assert first["seq"] != second["seq"]


def test_rebind_rotates_token_and_bumps_version(store):
    store.session_ensure("i", "t", "c")
    thread = store.thread_bind("ci-1", "dm-1", "se-1")
    assert thread["binding_version"] == 1
    rebound = store.thread_bind("ci-1", "dm-1", "se-1")
    assert rebound["binding_version"] == 2
    assert rebound["binding_token"] != thread["binding_token"]


def test_delivery_rollup_requires_all_batches_and_never_regresses(store):
    msg = store.outbound_put(
        session_id="se-1",
        message_id="m-1",
        reply_to="e-1",
        covers=["e-1"],
        batches=[["a"], ["b"]],
        target_channel="ci-1",
        target_thread="dm-1",
        binding_version=1,
        binding_token="bt-1",
    )
    assert store.delivery_rollup(msg["seq"]) == "pending"
    assert store.delivery_set(msg["seq"], 0, "sent") == "pending"  # 第二批还没发出
    assert store.delivery_set(msg["seq"], 1, "sent") == "sent"
    assert store.delivery_set(msg["seq"], 0, "accepted") == "sent"  # 未全部确认
    assert store.delivery_set(msg["seq"], 1, "accepted") == "delivered"
    # 迟到回执不能倒退已确认状态
    assert store.delivery_set(msg["seq"], 0, "unknown") == "delivered"
    assert store.delivery_set(msg["seq"], 1, "pending") == "delivered"


def test_pending_outbound_excludes_unknown_and_delivered(store):
    msg = store.outbound_put(
        session_id="se-1",
        message_id="m-1",
        reply_to=None,
        covers=[],
        batches=[["a"]],
        target_channel="ci-1",
        target_thread="dm-1",
        binding_version=1,
        binding_token="bt-1",
    )
    assert [row["message_id"] for row in store.pending_outbound("ci-1", "dm-1")] == ["m-1"]
    store.delivery_set(msg["seq"], 0, "unknown")
    assert store.pending_outbound("ci-1", "dm-1") == []
    store.delivery_set(msg["seq"], 0, "failed")
    assert [row["message_id"] for row in store.pending_outbound("ci-1", "dm-1")] == ["m-1"]


def test_voided_record_is_independent_of_message_rows(store):
    store.void_put("ci-1", "dm-1", "e-9", "rollback")
    assert store.void_has("ci-1", "dm-1", "e-9") is True
    assert store.void_has("ci-1", "dm-2", "e-9") is False


def test_history_page_walks_backwards(store):
    for index in range(5):
        put_inbound(store, env_id=f"e-{index}", text=f"hello {index}")
    first = store.history_page("se-1", limit=2)
    assert [row["text"] for row in first["messages"]] == ["hello 3", "hello 4"]
    assert first["has_more"] is True
    older = store.history_page("se-1", limit=2, before_seq=first["next_before_seq"])
    assert [row["text"] for row in older["messages"]] == ["hello 1", "hello 2"]


def test_legacy_db_without_base_commit_id_still_opens(tmp_path):
    """老数据根必须能重新打开：`base_commit_id` 由迁移补，不能写在 SCHEMA 的建表里就当老库也有。

    回归点（2026-10-08 真壳验收抓到）：列只写进 `CREATE TABLE` 时，老库的表已存在、
    `CREATE TABLE IF NOT EXISTS` 是空操作，紧随其后的 `CREATE INDEX ... (base_commit_id)`
    会以 `no such column` 中断整个 `executescript(SCHEMA)`——用户的数据根再也打不开。
    """
    path = tmp_path / "isekai.db"
    store = Store(path)
    store.ensure_schema()
    with store._lock, store._conn:
        store._conn.execute(
            """INSERT INTO commit_snapshot(commit_id, instance_id, payload, size, created_at, base_commit_id)
               VALUES('cid-legacy','in-1','{}',2,1.0,'')"""
        )
        # 退回旧形态：没有这一列、也没有它的索引
        store._conn.execute("DROP INDEX IF EXISTS ix_commit_snapshot_base")
        store._conn.execute("ALTER TABLE commit_snapshot DROP COLUMN base_commit_id")
    store.close()

    again = Store(path)
    again.ensure_schema()  # 不许抛
    columns = {str(row[1]) for row in again._conn.execute("PRAGMA table_info(commit_snapshot)")}
    assert "base_commit_id" in columns, "迁移要把列补回来"
    indexes = {str(row[1]) for row in again._conn.execute("PRAGMA index_list('commit_snapshot')")}
    assert "ix_commit_snapshot_base" in indexes, "迁移要把索引补上"
    assert again._conn.execute("SELECT count(*) FROM commit_snapshot").fetchone()[0] == 1, "老行不许丢"
    again.ensure_schema()  # 幂等：再跑一次不报错
    again.close()
