"""归档后的会话行为（SESSION_CORE_SPEC §5.7）：不再对话、一次性通告、历史保留。"""

from __future__ import annotations

import asyncio
import time

from isekai_core.runtime.service import RuntimeService
from isekai_core.ump import UmpError
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _dead_line(store, world) -> tuple[dict, str, str, str]:
    """造一条「该角色已有身故记录」的线（直接写事件，等价于寿终事件已落库）。"""
    service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world)
    service.activate(info["id"], timeline_id, now_real=time.time())
    world_seconds = int(service.clock_row(timeline_id)["processed_world"]) + 100
    store.apply_runtime_batch(
        timeline_id=timeline_id,
        generation=int(service.clock_row(timeline_id)["generation"]),
        processed_world=world_seconds,
        catching_up=False,
        limited=False,
        events=[
            {
                "instance_id": info["id"],
                "timeline_id": timeline_id,
                "id": "ev-death-test",
                "world_seconds": world_seconds,
                "seq": 0,
                "kind": "character",
                "family": "",
                "template": f"death:{character_id}",
                "source": "engine",
                "summary": "堤禾身故，享年 44",
                "detail": "堤禾身故，享年 44",
                "text_source": "template",
                "effects": "[]",
                "share_value": 0,
                "importance": 0.8,
                "created_real": time.time(),
            }
        ],
    )
    return info, timeline_id, character_id, "ev-death-test"


class _Session:
    def __init__(self, store_, world_) -> None:  # noqa: ANN001
        self.store = store_
        self.world = world_
        self.sent: list[str] = []

    async def _send_batches(self, msg):  # noqa: ANN001, ANN202
        self.sent.append(str(msg["message_id"]))


def test_session_accept_refuses_after_archive_and_notices_once(store, world) -> None:
    """身故后：首条给一次归档说明并拒绝；再发只拒绝，不重复出通告；历史仍在。"""
    from isekai_core.session import SessionService

    info, timeline_id, character_id, _event = _dead_line(store, world)
    session = store.session_ensure(info["id"], timeline_id, character_id)
    channel = store.channel_register(
        name="builtin", display_name="b", version="0", protocol="1", capabilities={}
    )[0]
    thread = store.thread_bind(channel["id"], "dm-1", session["id"])

    probe = _Session(store, world)
    probe.store.thread_for_session = store.thread_for_session  # type: ignore[assignment]

    async def fake_flush(_session_id: str) -> int:
        return 0

    service = SessionService.__new__(SessionService)
    service.store = store
    service._flush_proactive = fake_flush  # type: ignore[assignment]
    service._send_batches = probe._send_batches  # type: ignore[assignment]

    from isekai_core.ump import Envelope

    env = Envelope(
        type="user_message",
        id="e-1",
        ts=time.time(),
        payload={"text": "在吗"},
        thread_id="dm-1",
        binding_token=str(thread["binding_token"]),
    )
    try:
        asyncio.run(
            service.accept(channel_id=channel["id"], thread_row=thread, env=env)
        )
    except UmpError as exc:
        assert "归档" in str(exc)
    else:
        raise AssertionError("归档角色仍接受了新消息")

    notice = store.session_notice_get(str(session["id"]), "archive")
    assert notice is not None and notice["message_id"], "首次要给一次归档说明"
    first_notice_id = str(notice["message_id"])

    env2 = Envelope(
        type="user_message",
        id="e-2",
        ts=time.time(),
        payload={"text": "还在吗"},
        thread_id="dm-1",
        binding_token=str(thread["binding_token"]),
    )
    try:
        asyncio.run(service.accept(channel_id=channel["id"], thread_row=thread, env=env2))
    except UmpError:
        pass
    assert str(store.session_notice_get(str(session["id"]), "archive")["message_id"]) == first_notice_id

    # 历史与既有消息保留
    rows = store.instance_messages(info["id"])
    assert any(str(row.get("message_id") or "") == first_notice_id for row in rows)
