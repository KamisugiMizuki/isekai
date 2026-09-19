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
