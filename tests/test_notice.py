"""管理面通知（CHANNEL_PLUGIN_SPEC §2.5 末条 / DESKTOP_SPEC §3.1、A17）。

判据：通知只引用已固化消息；重复登记幂等；目标失效只报管理错误——不改投、不激活冻结线。
"""

from __future__ import annotations

from isekai_core.config import load_config
from isekai_core.world import ops as world_ops
from test_runtime import make_instance, store, world  # noqa: F401  夹具定义在那边


def _materialize(store, info, timeline_id, *, message_id="m-notice-1"):
    session_id = f"s-{info['id']}-{timeline_id}-probe"
    with store._lock, store._conn:
        store._conn.execute(
            """INSERT OR IGNORE INTO session(id, instance_id, timeline_id, character_id, created_at)
               VALUES(?, ?, ?, ?, 0)""",
            (session_id, info["id"], timeline_id, "ch-probe"),
        )
        store._conn.execute(
            """INSERT OR IGNORE INTO message(session_id, role, channel_id, thread_id, text, parts,
                                            message_id, state, created_at)
               VALUES(?, 'character', 'builtin', 'th-1', '她主动说了句话', '[]', ?, 'done', 0)""",
            (session_id, message_id),
        )
    return session_id


def _create(cfg, store, info, timeline_id, session_id, message_id="m-notice-1"):
    return world_ops.dispatch(
        cfg,
        store,
        "notice.create",
        {
            "instance_id": info["id"],
            "timeline_id": timeline_id,
            "session_id": session_id,
            "message_id": message_id,
            "revision": 3,
        },
    )["notice"]


def _resolve(cfg, store, notice_id):
    return world_ops.dispatch(cfg, store, "notice.resolve", {"id": notice_id})["target"]


def test_notice_is_idempotent_and_resolves_to_original_target(store, world, tmp_path) -> None:
    cfg = load_config(tmp_path)
    info, timeline_id, _character = make_instance(store, world)
    session_id = _materialize(store, info, timeline_id)
    first = _create(cfg, store, info, timeline_id, session_id)
    again = _create(cfg, store, info, timeline_id, session_id)
    assert first["id"] == again["id"], "同一条固化消息只登记一次"

    target = _resolve(cfg, store, first["id"])
    assert target["valid"] is True, target
    assert target["session_id"] == session_id and target["message_id"] == "m-notice-1"
    assert target["revision"] == 3, "固定引用原会话版本"

    by_message = world_ops.dispatch(cfg, store, "notice.resolve", {"message_id": "m-notice-1"})["target"]
    assert by_message["session_id"] == session_id
    assert world_ops.dispatch(cfg, store, "notice.list", {"instance_id": info["id"]})["notices"]


def test_missing_message_reports_management_error_without_rerouting(store, world, tmp_path) -> None:
    cfg = load_config(tmp_path)
    info, timeline_id, _character = make_instance(store, world)
    session_id = _materialize(store, info, timeline_id)
    notice = _create(cfg, store, info, timeline_id, session_id)
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM message WHERE session_id=? AND message_id=?", (session_id, "m-notice-1"))
    target = _resolve(cfg, store, notice["id"])
    assert target["valid"] is False and "消息已不存在" in target["reason"], target
    assert target["session_id"] == session_id, "不改投：报的还是原来那个会话"


def test_archived_timeline_and_rebound_session_invalidate(store, world, tmp_path) -> None:
    cfg = load_config(tmp_path)
    info, timeline_id, _character = make_instance(store, world)
    session_id = _materialize(store, info, timeline_id)
    notice = _create(cfg, store, info, timeline_id, session_id)

    store.timeline_set_state(timeline_id, "archived")
    target = _resolve(cfg, store, notice["id"])
    assert target["valid"] is False and "已归档" in target["reason"], target

    store.timeline_set_state(timeline_id, "active")
    with store._lock, store._conn:
        store._conn.execute("UPDATE session SET timeline_id='tl-elsewhere' WHERE id=?", (session_id,))
    rebound = _resolve(cfg, store, notice["id"])
    assert rebound["valid"] is False and "重绑" in rebound["reason"], rebound
