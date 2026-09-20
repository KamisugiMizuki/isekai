"""2026-09-21 审计收口的回归断言：补卡留痕 / 成员资格门、归档说明分类。

每条都对着「实现与 SPEC 之间的真实不匹配」——锁的是可观察行为，不是读代码复述。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from isekai_core.runtime.service import RuntimeService, RuntimeStateError
from isekai_core.session import SessionService
from samples import DAY, sample_card, sample_package
from test_runtime import make_instance, store, world  # noqa: F401  夹具在那边


def _service(store, **over) -> RuntimeService:
    params = {
        "instance_tokens_per_day": 400_000,
        "timeline_tokens_per_day": 150_000,
        "task_tokens_per_day": 60_000,
        "autocommit_enabled": False,
    }
    params.update(over)
    return RuntimeService(store, **params)


def _ready(store, world_service, *, moment=DAY * 1500):
    info, timeline_id, character_id = make_instance(store, world_service, moment=moment)
    world_service.activate(info["id"], timeline_id, now_real=1.7e9)
    world_service.advance(info["id"], timeline_id, now_real=1.7e9 + 2 * DAY)
    return info, timeline_id, character_id


def _newcomer(package, name: str = "桑叶", card_id: str = "cc-桑叶") -> dict[str, Any]:
    card = sample_card(package, name=name)
    card["meta"]["card_id"] = card_id
    return card


def test_join_writes_definition_commit_and_is_idempotent(store) -> None:  # noqa: F811
    """§3.7：补卡三处留痕（实例定义 / 成员资格 / 加入提交）；同一请求标识重试不重复登记。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    commits_before = len(store.commit_list(info["id"]))
    spec = dict(
        instance_id=info["id"],
        timeline_id=timeline_id,
        card=_newcomer(sample_package()),
        now_real=1.7e9 + 3 * DAY,
    )

    first = world_service.add_character(request_id="req-1", **spec)
    setting = json.loads(store.instance_get(info["id"])["setting"])
    joined = [c for c in setting["cards"] if (c.get("meta") or {}).get("card_id") == "cc-桑叶"]
    assert joined, "定义没写进实例设定快照（定义与成员资格必须分开留痕）"
    assert len(store.commit_list(info["id"])) == commits_before + 1, "补卡没有创建加入提交"
    assert first["commit_id"], "成员资格行没记加入提交"

    units = len(store.unit_list(info["id"], timeline_id, "cc-桑叶"))
    second = world_service.add_character(request_id="req-1", **spec)
    assert second.get("reused") is True and second["character"] == first["character"]
    assert len(store.character_join_list(info["id"], timeline_id)) == 1
    assert len(store.unit_list(info["id"], timeline_id, "cc-桑叶")) == units


def test_join_definition_is_immutable_across_lines(store) -> None:  # noqa: F811
    """§3.7：同一 card_id 只有一份不可变定义；补卡只作用于目标线（兄弟线不静默获得）。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    anchor = world_service.commit(info["id"], timeline_id, kind="manual", note="分叉点")
    branch = world_service.fork(info["id"], timeline_id, commit_id=anchor["id"], name="兄弟线")["timeline"]["id"]
    card = _newcomer(sample_package())
    world_service.add_character(info["id"], timeline_id, card, now_real=1.7e9 + 3 * DAY)

    instance = store.instance_get(info["id"])
    on_branch = [
        str((c.get("meta") or {}).get("card_id"))
        for c in world_service.cards(
            instance, timeline_id=branch, world_seconds=world_service.world_moment(info["id"], branch)
        )
    ]
    assert "cc-桑叶" not in on_branch, f"兄弟线被静默写入：{on_branch}"

    impostor = json.loads(json.dumps(card, ensure_ascii=False))
    impostor["identity"]["occupation"] = "冒充者"
    with pytest.raises(RuntimeStateError):
        world_service.add_character(info["id"], branch, impostor, now_real=1.7e9 + 4 * DAY)


def test_join_revokes_on_rollback_and_blocks_membership(store) -> None:  # noqa: F811
    """§3.7：回滚跨过加入点 → 成员资格转撤销（记录留档），成员资格检查随之拒绝。"""
    world_service = _service(store)
    info, timeline_id, _ = _ready(store, world_service)
    anchor = world_service.commit(info["id"], timeline_id, kind="manual", note="加入前")
    world_service.add_character(info["id"], timeline_id, _newcomer(sample_package()), now_real=1.7e9 + 3 * DAY)
    world_service.rollback(info["id"], timeline_id, commit_id=anchor["id"], now_real=1.7e9 + 4 * DAY)

    assert store.character_join_list(info["id"], timeline_id) == [], "撤销后本线不该再有有效成员资格"
    revoked = store.character_join_list(info["id"], timeline_id, state="revoked")
    assert revoked and revoked[0]["state"] == "revoked", "撤销记录没留档（不能复活旧成员资格）"

    instance = store.instance_get(info["id"])
    with pytest.raises(RuntimeStateError):
        world_service.assert_member(instance, timeline_id, "cc-桑叶")
    with pytest.raises(RuntimeStateError):
        world_service.assert_member(instance, timeline_id, "cc-从未装配")


def test_archive_notice_is_classified_as_notice(store, tmp_path) -> None:  # noqa: F811
    """§5.7：归档说明以 role=notice 落库（不是角色发言），也不进角色上下文。"""
    from isekai_core.config import load_config

    world_service = _service(store)
    info, timeline_id, character_id = _ready(store, world_service)
    session = store.session_ensure(info["id"], timeline_id, character_id)
    channel = store.channel_register(
        name="ch-notice", display_name="审计通道", version="1.0", protocol="1.0", capabilities={}
    )[0]
    store.thread_bind(channel["id"], "t-notice", session["id"])

    service = SessionService(
        store=store,
        cfg=load_config(tmp_path),
        llm=None,
        deliver=lambda *a, **k: asyncio.sleep(0, result=True),
    )
    service.runtime = world_service
    asyncio.run(service._archive_notice(session, character_id))

    notice = store.session_notice_get(str(session["id"]), "archive")
    assert notice is not None, "归档说明没有落"
    row = store.outbound_by_message_id(str(notice["message_id"]))
    assert row is not None and str(row["role"]) == "notice", f"归档说明不是 notice 分类：{row and row['role']}"
    assert character_id not in str(store.message_text(row)), "归档说明带内部角色标识"
    context = store.context_window(str(session["id"]), 20)
    assert all(str(item["role"]) != "notice" for item in context), "通知被当成角色发言送进上下文"
    asyncio.run(service._archive_notice(session, character_id))
    again = store.session_notice_get(str(session["id"]), "archive")
    assert again is not None and str(again["message_id"]) == str(notice["message_id"]), "归档说明重复固化"
