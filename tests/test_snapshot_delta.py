"""快照的 diff 存储与压缩（WORLD_RUNTIME_SPEC §6 / §8）。

判据：diff 与全量等价（物化逐字段相同、删除不复活）；链太长自动物化；祖先可读性不被删除动作带走。
"""

from __future__ import annotations

import json

from isekai_core.runtime import versioning
from isekai_core.runtime.service import RuntimeService
from samples import DAY
from test_runtime import make_instance, store, world  # noqa: F401  夹具定义在那边


def test_delta_roundtrip_including_deletes() -> None:
    base = {
        "watermark": 100,
        "events": [{"id": "ev-1", "summary": "旧"}, {"id": "ev-2", "summary": "留着"}],
        "units": [{"id": "u-1", "confidence": 0.4}],
    }
    target = {
        "watermark": 200,
        "events": [{"id": "ev-2", "summary": "留着"}, {"id": "ev-3", "summary": "新"}],
        "units": [{"id": "u-1", "confidence": 0.6}],
    }
    delta = versioning.encode_delta(base, target)
    materialized = versioning.apply_delta(base, delta)
    assert materialized == target, materialized
    assert [row["id"] for row in materialized["events"]] == ["ev-2", "ev-3"], "行序照目标"
    assert materialized["watermark"] == 200

    # 删除标记优先：祖先层还有 ev-1 也不复活
    again = versioning.apply_delta(base, delta)
    assert all(row["id"] != "ev-1" for row in again["events"])
    assert json.loads(versioning.dump_snapshot(delta, kind="delta", base="cm-x"))["base"] == "cm-x"


def test_commits_use_delta_then_materialize_equivalently(store, world) -> None:
    info, timeline_id, _character = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1.7e9)
    world.advance(info["id"], timeline_id, now_real=1.7e9 + 6 * DAY)
    history = world.commits(info["id"], timeline_id)
    assert history, "创建期就有一条初始提交"
    oldest = str(history[-1]["id"])
    first = world.commit(info["id"], timeline_id, kind="manual", note="一")["id"]
    world.advance(info["id"], timeline_id, now_real=1.7e9 + 10 * DAY)
    second = world.commit(info["id"], timeline_id, kind="manual", note="二")["id"]

    def _kind(commit_id: str) -> str:
        row = store._conn.execute(
            "SELECT payload FROM commit_snapshot WHERE commit_id=?", (commit_id,)
        ).fetchone()
        return versioning.snapshot_kind(row["payload"])[0]

    assert _kind(oldest) == "full", "链的起点是全量"
    assert _kind(first) == "delta" and _kind(second) == "delta", "接得上就存 diff"

    # 物化结果 = 全量快照（等价性：读的人看不出差别）
    payload = store.commit_snapshot_get(second)
    assert payload and int(payload["world"]) == int(store.clock_get(timeline_id)["processed_world"])
    snapshot_now = versioning.snapshot_of(store, info["id"], timeline_id, note="当场")
    assert payload["runtime"] == snapshot_now["runtime"], "runtime 段逐字段相同（容器也按行 diff）"
    assert payload["dialog"] == snapshot_now["dialog"]
    assert payload["rules_version"] == snapshot_now["rules_version"]


def test_long_chain_is_compressed_and_stays_readable(store, world) -> None:
    info, timeline_id, _character = make_instance(store, world)
    world.activate(info["id"], timeline_id, now_real=1.7e9)
    commits = []
    for step in range(versioning.MAX_DELTA_CHAIN + 3):
        world.advance(info["id"], timeline_id, now_real=1.7e9 + (6 + step) * DAY)
        commits.append(world.commit(info["id"], timeline_id, kind="manual", note=f"第{step}次")["id"])
        head = store._conn.execute(
            "SELECT payload FROM commit_snapshot WHERE commit_id=?", (commits[-1],)
        ).fetchone()
        kind = versioning.snapshot_kind(head["payload"])[0]
        depth = store.commit_snapshot_depth(commits[-1])
        assert depth <= versioning.MAX_DELTA_CHAIN or kind == "full", (kind, depth)
    kinds = []
    for commit_id in ([str(item["id"]) for item in world.commits(info["id"], timeline_id)]):
        row = store._conn.execute(
            "SELECT payload FROM commit_snapshot WHERE commit_id=?", (commit_id,)
        ).fetchone()
        if row is not None:
            kinds.append(versioning.snapshot_kind(row["payload"])[0])
    assert kinds.count("full") >= 2, f"链到上限被物化成全量，链从它重新开始：{kinds}"
    # 每个提交仍可物化，且越晚的水位不早于越早的
    waters = []
    for commit_id in commits:
        payload = store.commit_snapshot_get(commit_id)
        assert payload is not None, commit_id
        waters.append(int(payload.get("world") or 0))
    assert waters == sorted(waters)
