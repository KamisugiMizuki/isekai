"""惰性展开的覆盖状态（EVENT_ENGINE_SPEC §3.4 / 附录 B #10）。

判据：尚未生成 ≠ 已确认缺载；缺载 ≠ 删改（原记载与历史维持原样）。
"""

from __future__ import annotations

import json

from isekai_core.config import load_config
from isekai_core.world import ops as world_ops
from test_render import FakeRenderLLM, _prepared
from test_runtime import make_instance, store, world  # noqa: F401  夹具定义在那边


async def _render_one(tmp_path, store, world):
    cfg = load_config(tmp_path)
    info, timeline_id, character_id, event = _prepared(store, world)
    skeleton = str(event["summary"])
    llm = FakeRenderLLM(
        [
            json.dumps(
                {"detail": f"（信报抄存）{skeleton}", "claims": {"src-1": f"驿站传：{skeleton}"}},
                ensure_ascii=False,
            )
        ]
    )
    await world_ops.dispatch_async(
        cfg,
        llm,
        "event.render",
        {"instance_id": info["id"], "timeline_id": timeline_id, "event_id": event["id"]},
        store=store,
    )
    claims = store.claim_list(info["id"], timeline_id)
    assert claims, "渲染之后要有说法条目"
    return cfg, info, timeline_id, claims[0]


def _coverage(cfg, store, info, timeline_id, claim_id) -> dict:
    out = world_ops.dispatch(
        cfg,
        store,
        "claim.coverage",
        {"instance_id": info["id"], "timeline_id": timeline_id, "claim_id": claim_id},
    )
    return out["coverage"]


async def test_pending_is_not_absence(store, world, tmp_path) -> None:
    cfg, info, timeline_id, claim = await _render_one(tmp_path, store, world)
    before = str(claim["text"])
    cov = _coverage(cfg, store, info, timeline_id, str(claim["id"]))
    assert cov["state"] == "pending", cov
    assert "尚未生成" in str(cov["note"])
    assert str(claim["text"]) == before, "读覆盖状态不改动记载"


async def test_grounded_absence_is_lacuna_not_tampering(store, world, tmp_path) -> None:
    cfg, info, timeline_id, claim = await _render_one(tmp_path, store, world)
    before = str(claim["text"])
    result = await world_ops.dispatch_async(
        cfg,
        FakeRenderLLM(["此事不可考，没有记下更多。"]),
        "event.expand",
        {"instance_id": info["id"], "timeline_id": timeline_id, "claim_id": claim["id"]},
        store=store,
    )
    assert result["state"] == "absent" and result["derived"] and result["text"], result
    cov = _coverage(cfg, store, info, timeline_id, str(claim["id"]))
    assert cov["state"] == "absent" and "缺载≠删改" in str(cov["note"])
    rows = store.claim_list(info["id"], timeline_id)
    original = [item for item in rows if str(item["id"]) == str(claim["id"])][0]
    assert str(original["text"]) == before, "缺载只是没写下：原记载一字未动"
    derived = [item for item in rows if str(item.get("derived_from") or "") == str(claim["id"])]
    assert derived and str(derived[0]["id"]) == str(result["derived"]), "展开产物是新增派生记录"


async def test_rejected_expansion_stays_pending(store, world, tmp_path) -> None:
    cfg, info, timeline_id, claim = await _render_one(tmp_path, store, world)
    result = await world_ops.dispatch_async(
        cfg,
        FakeRenderLLM(["那年死了 777 个人。"]),  # 塞进了原记载没有的数字 → 被护栏丢弃
        "event.expand",
        {"instance_id": info["id"], "timeline_id": timeline_id, "claim_id": claim["id"]},
        store=store,
    )
    assert result["state"] == "pending", result
    assert "已确认缺载" in str(result.get("note") or ""), result
    cov = _coverage(cfg, store, info, timeline_id, str(claim["id"]))
    assert cov["state"] == "pending", "被丢弃的展开不是「已确认缺载」"
