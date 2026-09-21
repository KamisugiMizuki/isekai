"""倍率落库与回滚语义（BUG：结算后的倍率只活在视图投影里，快照固化陈旧倍率）。

四处断点的行为测试：
① 结算后的倍率必须落进 clock 行（advance / commit 都要收敛）；
② 冻结不得把结算前的旧倍率写回；
③ 快照 / 导出记的是「接下来按什么速度走」（待生效命令折进结果）；
④ 回滚后存储倍率 == 快照倍率，且世界水位不因陈旧倍率飙升。
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from isekai_core.runtime import versioning
from isekai_core.runtime.service import RuntimeService
from isekai_core.store import Store
from isekai_core.world.instances import create_instance
from isekai_core.world.portable import build_container
from samples import DAY, sample_card, sample_package

T = 1_700_000_000.0  # 固定现实时基：0.4 秒后取整秒生效点 → 1_700_000_001
START = DAY * 1500  # 初始世界时刻 129_600_000


@pytest.fixture
def store(tmp_path):
    handle = Store(tmp_path / "data" / "isekai.db")
    handle.ensure_schema()
    yield handle
    handle.close()


@pytest.fixture
def world(store):
    return RuntimeService(store)


def make_instance(store, world):
    package = sample_package(moment=START)
    card = sample_card(package)
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world.ensure_instance(info["id"], now_real=time.time())
    world.activate(info["id"], timeline_id, now_real=T)
    return info, timeline_id


def test_settled_rate_lands_in_clock_row(store, world) -> None:
    """①/② ：倍率调整结算后，clock 行的 rate 与状态机一致（不再只活在视图投影里）。"""
    info, timeline_id = make_instance(store, world)
    world.set_rate(info["id"], timeline_id, rate=2, now_real=T)
    assert store.clock_get(timeline_id)["rate"] == 1, "生效整秒之前仍属旧倍率段（§2.3.4）"

    world.advance(info["id"], timeline_id, now_real=T + 3)
    row = store.clock_get(timeline_id)
    assert row["rate"] == 2, "结算后的倍率必须落库"
    assert row["base_real"] == 1_700_000_001.0, "基准现实时间 = 生效整秒"
    assert row["base_world"] == START + 1, "旧段 1 秒 ×1 = 1"
    assert row["processed_world"] == START + 1 + 2 * 2, "新段 2 秒 ×2"
    assert store.rate_pending(timeline_id) == []

    world.set_rate(info["id"], timeline_id, rate=1, now_real=T + 3)
    world.advance(info["id"], timeline_id, now_real=T + 5)
    row = store.clock_get(timeline_id)
    assert row["rate"] == 1, "调回 1 之后存储值就是 1"
    assert row["base_real"] == 1_700_000_004.0
    assert row["base_world"] == START + 1 + 3 * 2, "旧段 3 秒 ×2"
    assert row["processed_world"] == START + 7 + 1, "新段 1 秒 ×1"


def test_commit_settles_and_records_effective_rate(store, world) -> None:
    """③ 陈旧窗口：已到期但没人结算时提交，快照/导出不得固化旧倍率。"""
    info, timeline_id = make_instance(store, world)
    world.set_rate(info["id"], timeline_id, rate=2592000, now_real=T)
    world.advance(info["id"], timeline_id, now_real=T + 2, max_batches=1)
    assert store.clock_get(timeline_id)["rate"] == 2592000

    world.set_rate(info["id"], timeline_id, rate=1, now_real=T + 3)  # 生效整秒 1_700_000_004
    assert store.clock_get(timeline_id)["rate"] == 2592000, "界面此刻只看到投影：行值仍是旧段"

    record = world.commit(info["id"], timeline_id, kind="manual", note="倍率改回之后")
    assert store.clock_get(timeline_id)["rate"] == 1, "写快照前先结算，行值不再陈旧"
    assert store.commit_snapshot_get(record["id"])["rate"] == 1, "快照记的是接下来要走的倍率"

    container = build_container(store, info["id"])
    assert container["runtime"]["state"][timeline_id]["rate"] == 1, "导出件同口径（§7.1）"


def test_freeze_keeps_settled_rate(store, world) -> None:
    """② 冻结先结算：不得用结算前的行值把刚生效的倍率覆盖掉。"""
    info, timeline_id = make_instance(store, world)
    world.set_rate(info["id"], timeline_id, rate=3, now_real=T)
    world.advance(info["id"], timeline_id, now_real=T + 2)
    assert store.clock_get(timeline_id)["rate"] == 3

    world.set_rate(info["id"], timeline_id, rate=1, now_real=T + 3)  # 生效整秒 1_700_000_004
    result = world.freeze(info["id"], timeline_id, now_real=T + 5)
    assert result["cancelled_commands"] == 0, "已到期命令先结算"
    row = store.clock_get(timeline_id)
    assert row["rate"] == 1, "冻结后存储倍率是结算结果，不是结算前的 3"
    assert row["base_world"] == START + 2 + 3 * 3, "旧段 3 秒 ×3"
    assert store.rate_pending(timeline_id) == []


def test_rollback_restores_snapshot_rate_without_exploding(store, world) -> None:
    """④ 回滚恢复快照倍率，不留会覆盖它的陈旧命令，世界水位按该倍率走。"""
    info, timeline_id = make_instance(store, world)
    world.set_rate(info["id"], timeline_id, rate=2, now_real=T)
    world.advance(info["id"], timeline_id, now_real=T + 3)
    before = store.clock_get(timeline_id)
    assert (before["rate"], before["processed_world"]) == (2, START + 5)

    record = world.commit(info["id"], timeline_id, kind="manual", note="回滚点")
    world.set_rate(info["id"], timeline_id, rate=2592000, now_real=T + 3)
    world.advance(info["id"], timeline_id, now_real=T + 4, max_batches=1)
    assert store.clock_get(timeline_id)["rate"] == 2592000, "回滚前现实确实在高倍率上"

    world.rollback(info["id"], timeline_id, commit_id=record["id"], now_real=T + 100)
    row = store.clock_get(timeline_id)
    assert row["rate"] == int(store.commit_snapshot_get(record["id"])["rate"]) == 2
    assert (row["base_real"], row["base_world"]) == (T + 100, START + 5), "以回滚时刻重锚（§七）"
    assert store.rate_pending(timeline_id) == [], "不残留待生效命令"
    with sqlite3.connect(store.path) as conn:  # 账本里不留任何倍率命令（pending/applied/cancelled）
        leftover = [row[0] for row in conn.execute(
            "SELECT state FROM rate_command WHERE timeline_id=?", (timeline_id,)
        )]
    assert leftover == [], "旧账本不得覆盖恢复的倍率"

    view = world.view(info["id"], timeline_id, now_real=T + 160)
    assert view["rate"] == 2
    assert view["world_seconds"] == START + 5 + 60 * 2, "60 秒现实 = 120 秒世界，没有 2592000 倍"
    again = world.advance(info["id"], timeline_id, now_real=T + 200)
    assert again["state"] == "current" and store.clock_get(timeline_id)["rate"] == 2, "再推进也不会被陈旧命令改回去"


def test_snapshot_rate_folds_not_yet_effective_change(store, world) -> None:
    """③ 生效整秒还没到就提交：快照仍记新倍率（待生效命令不进快照，丢弃它不能让旧倍率复活）。"""
    info, timeline_id = make_instance(store, world)
    world.set_rate(info["id"], timeline_id, rate=7, now_real=T + 100)  # 生效整秒 1_700_000_101，在未来
    assert store.clock_get(timeline_id)["rate"] == 1, "生效前仍是当前倍率段"
    assert versioning.recorded_rate(store, timeline_id) == 7, "快照口径：这条线接下来按 7 走"
    assert store.commit_snapshot_get(world.commit(info["id"], timeline_id)["id"])["rate"] == 7


def test_rollback_keeps_world_from_running_away(store, world) -> None:
    """④ 报告场景：调回倍率 1 之后回滚到当时的提交，倍率必须是 1，世界水位不许按旧倍率飙。

    界面此刻显示 1（投影），clock 行还停在 2592000 —— 快照若照行值固化，回滚就把 2592000 带回来。
    """
    info, timeline_id = make_instance(store, world)
    world.set_rate(info["id"], timeline_id, rate=2592000, now_real=T)
    world.advance(info["id"], timeline_id, now_real=T + 2, max_batches=1)
    assert store.clock_get(timeline_id)["rate"] == 2592000, "高倍率已结算落库"

    world.set_rate(info["id"], timeline_id, rate=1, now_real=T + 3)  # 生效整秒 1_700_000_004
    assert store.clock_get(timeline_id)["rate"] == 2592000, "行值落后于状态机（界面看的是投影）"

    record = world.commit(info["id"], timeline_id, kind="manual", note="调回 1 之后")
    snapshot = store.commit_snapshot_get(record["id"])
    assert snapshot["rate"] == 1, "快照不得固化陈旧倍率"
    assert snapshot["world"] == START + DAY, "水位仍按已完成批次记账"

    world.rollback(info["id"], timeline_id, commit_id=record["id"], now_real=T + 400)
    row = store.clock_get(timeline_id)
    assert row["rate"] == snapshot["rate"] == 1, "回滚后的存储倍率 == 快照倍率"
    assert (row["base_real"], row["base_world"]) == (T + 400, snapshot["world"])
    view = world.view(info["id"], timeline_id, now_real=T + 460)
    assert view["rate"] == 1
    assert view["world_seconds"] == snapshot["world"] + 60, "60 秒现实 = 60 秒世界（旧倍率会飙到上亿）"


def _ledger(store, timeline_id: str, state: str) -> int:
    """该线账本里某种状态的行数（applied / cancelled / pending）。"""
    with sqlite3.connect(store.path) as conn:
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM rate_command WHERE timeline_id=? AND state=?", (timeline_id, state)
            ).fetchone()[0]
        )


def test_commit_clears_settled_rate_ledger(store, world) -> None:
    """① 账本清理：提交清掉已结算 / 已取消行（不随历史无界积行），待生效行一动不动。"""
    info, timeline_id = make_instance(store, world)
    for step in range(5):
        at = T + 10 * step
        world.set_rate(info["id"], timeline_id, rate=2 + step, now_real=at)
        world.advance(info["id"], timeline_id, now_real=at + 2)
        assert _ledger(store, timeline_id, "applied") == 1, "推进结算后账本先记一笔"
        world.commit(info["id"], timeline_id, kind="manual", note=f"第 {step} 次")
        assert _ledger(store, timeline_id, "applied") == 0, "提交点清掉已结算行（否则 5 次变更积 5 行）"
        assert _ledger(store, timeline_id, "pending") == 0

    # 待生效行是控制状态，提交不许动它：提交按真实 now 结算，碰不到还没生效的整秒
    later = time.time() + 1000.0
    world.set_rate(info["id"], timeline_id, rate=9, now_real=later)
    world.commit(info["id"], timeline_id, kind="manual", note="有待生效命令")
    assert [item["rate"] for item in store.rate_pending(timeline_id)] == [9], "pending 行不受清理影响"
    assert versioning.recorded_rate(store, timeline_id) == 9, "快照仍按折进口径记新倍率"

    # 已取消行同理：冻结在生效整秒之前取消待生效请求 → 下一次提交清掉
    world.freeze(info["id"], timeline_id, now_real=int(later) + 1 - 120)
    assert _ledger(store, timeline_id, "cancelled") == 1, "冻结取消待生效请求（账本记一笔）"
    world.commit(info["id"], timeline_id, kind="manual", note="冻结之后")
    assert _ledger(store, timeline_id, "cancelled") == 0
    assert _ledger(store, timeline_id, "pending") == 0
