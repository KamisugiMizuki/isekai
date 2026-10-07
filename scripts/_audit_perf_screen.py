"""设计效率筛查 · 可复现基线（只读仓库；全部在临时数据根里跑，不动 `data/`）。

对应报告：`docs/DESIGN_EFFICIENCY_SCREEN_2026-10-08.md`。
三组测量：

A. 世界时钟推进：单个世界日批次的墙钟成本、`app.py::_clock_tick` 节拍下（5 s / 4 批）
   三条倍率的实际排水速率与积压增长，以及放开批次预算后的纯 CPU 上限。
B. 长区间补算：连推 240 个世界日，看单批成本是否随累计历史增长（头 / 中 / 尾）。
C. 记忆规模：0 / 500 / 2000 / 8000 条记忆下，单批（含衰减）、召回、单条写入的墙钟成本。

用法：
    .venv/Scripts/python.exe scripts/_audit_perf_screen.py
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

# 中文 Windows（cp936）下直接跑本脚本时把输出钉在 UTF-8，避免报告数字可读、标题乱码
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from isekai_core.runtime.service import RuntimeService  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402

from samples import DAY, sample_card, sample_package  # noqa: E402  # tests/ 已在 path 上

NOW = 1_700_000_000.0
DAY_SECONDS = DAY
TICK_INTERVAL = 5.0  # app.py::_clock_tick 默认
TICK_BATCHES = 4     # app.py::catch_up_all(max_batches=4)


class World:
    """一个隔离的临时世界（新库 + 新实例 + 激活 + 设定倍率）。"""

    def __init__(self, *, rate: int = 1) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="isekai-perf-"))
        self.store = Store(self.tmp / "data" / "isekai.db")
        self.store.ensure_schema()
        self.world = RuntimeService(self.store)
        package = sample_package()
        self.card = sample_card(package)
        self.info = create_instance(self.store, package, [self.card])
        self.instance = self.info["id"]
        self.timeline = self.store.timeline_list(self.instance)[0]["id"]
        self.character = str(self.card["meta"]["card_id"])
        self.world.ensure_instance(self.instance, now_real=NOW)
        self.world.activate(self.instance, self.timeline, now_real=NOW)
        if rate != 1:
            self.set_rate(rate)

    def set_rate(self, rate: int) -> None:
        self.world.set_rate(self.instance, self.timeline, rate=rate, now_real=NOW)
        self.world.advance(self.instance, self.timeline, now_real=NOW + 1.5, max_batches=1)

    def close(self) -> None:
        self.store.close()


def per_day_batch_cost() -> dict:
    """rate=1 下逐日推进，量单个世界日批次的墙钟成本。"""
    w = World()
    try:
        w.set_rate(1)
        base_world = int(w.world.clock_row(w.timeline)["base_world"])
        base_real = float(w.world.clock_row(w.timeline)["base_real"])
        costs = []
        for k in range(1, 11):
            t0 = time.perf_counter()
            res = w.world.advance(w.instance, w.timeline, now_real=base_real + DAY_SECONDS * k, max_batches=1)
            costs.append((time.perf_counter() - t0) * 1000)
            if res.get("batches") == 0:
                break
        return {
            "base_world_days": round(base_world / DAY_SECONDS, 1),
            "days": len(costs),
            "per_day_ms_median": round(statistics.median(costs), 2),
            "per_day_ms_max": round(max(costs), 2),
        }
    finally:
        w.close()


def tick_cadence(rate: int, *, ticks: int = 6, mode: str = "timebox") -> dict:
    """模拟 `_clock_tick`：每 5 s 推进一次激活线。

    - `timebox`（现行实现）：批数硬闸 + 墙钟时间盒（`catch_up_budget_seconds`，默认 1 s）
    - `fixed4`（改前实现）：固定 4 批 / 拍，用来对照「时间盒之前排不动默认倍率上限」
    """
    w = World(rate=rate)
    try:
        start = int(w.world.clock_row(w.timeline)["processed_world"])
        rows, calls = [], []
        for i in range(ticks):
            now = NOW + 1.5 + TICK_INTERVAL * (i + 1)
            t0 = time.perf_counter()
            if mode == "fixed4":
                result = w.world.advance(w.instance, w.timeline, now_real=now, max_batches=4, budget_seconds=0)
                backlog = (int(result["target"]) - int(result["processed_world"])) / DAY_SECONDS
                batches = result.get("batches")
                state = result.get("state")
            else:
                result = w.world.catch_up_all(
                    now_real=now,
                    max_batches=w.world.catch_up_tick_batches,
                    budget_seconds=w.world.catch_up_budget_seconds,
                )
                item = result.get(w.timeline) or {}
                backlog = (int(item.get("target", 0)) - int(item.get("processed_world", 0))) / DAY_SECONDS
                batches = item.get("batches")
                state = item.get("state")
            calls.append(time.perf_counter() - t0)
            rows.append({"tick": i + 1, "batches": batches, "state": state, "backlog_days": round(backlog, 1)})
        end = int(w.world.clock_row(w.timeline)["processed_world"])
        wall = TICK_INTERVAL * ticks
        return {
            "rate": rate,
            "mode": mode,
            "processed_days": round((end - start) / DAY_SECONDS, 1),
            "achieved_world_s_per_real_s": round((end - start) / wall, 1),
            "ticks": rows,
            "cpu_s_total": round(sum(calls), 3),
        }
    finally:
        w.close()


def unbounded_control(rate: int = 2_592_000, *, ticks: int = 2) -> dict:
    """同样的节拍、预算放到 1e9：测纯 CPU 排水上限（即「节拍不是瓶颈」时的能力）。"""
    w = World(rate=rate)
    try:
        start = int(w.world.clock_row(w.timeline)["processed_world"])
        spent = 0.0
        last = None
        for i in range(ticks):
            now = NOW + 1.5 + TICK_INTERVAL * (i + 1)
            t0 = time.perf_counter()
            last = w.world.advance(w.instance, w.timeline, now_real=now, max_batches=10**9)
            spent += time.perf_counter() - t0
        end = int(w.world.clock_row(w.timeline)["processed_world"])
        wall = TICK_INTERVAL * ticks
        return {
            "rate": rate,
            "processed_days": round((end - start) / DAY_SECONDS, 1),
            "achieved_world_s_per_real_s": round((end - start) / wall, 1),
            "cpu_s_total": round(spent, 3),
            "cpu_share_pct": round(100 * spent / wall, 1),
            "final_state": last.get("state") if last else None,
        }
    finally:
        w.close()


def history_scaling(*, batches: int = 240) -> dict:
    """连推 batches 个世界日（rate=1），比较头 / 中 / 尾单批成本。"""
    w = World()
    try:
        w.set_rate(1)
        base_real = float(w.world.clock_row(w.timeline)["base_real"])
        costs = []
        for k in range(1, batches + 1):
            t0 = time.perf_counter()
            res = w.world.advance(w.instance, w.timeline, now_real=base_real + DAY_SECONDS * (k + 1), max_batches=1)
            costs.append((time.perf_counter() - t0) * 1000)
            if res.get("batches") == 0:
                break
        n = len(costs)
        head = statistics.median(costs[:10])
        tail = statistics.median(costs[-10:])
        rows = {
            "event": w.store._conn.execute("SELECT COUNT(*) FROM event").fetchone()[0],
            "knowledge": w.store._conn.execute("SELECT COUNT(*) FROM knowledge").fetchone()[0],
            "experience": w.store._conn.execute("SELECT COUNT(*) FROM experience").fetchone()[0],
        }
        return {
            "batches": n,
            "head_ms": round(head, 2),
            "mid_ms": round(statistics.median(costs[n // 2 - 5 : n // 2 + 5]), 2),
            "tail_ms": round(tail, 2),
            "tail_over_head": round(tail / head, 2) if head else None,
            "total_s": round(sum(costs) / 1000, 2),
            "rows": rows,
        }
    finally:
        w.close()


def _seed_memories(store: Store, instance: str, timeline: str, character: str, count: int) -> None:
    import json

    rows = [
        (
            f"mem-{i:06d}", instance, timeline, character,
            f"第{i}条可召回素材：堤禾记得的一件小事与人物名。", "fact",
            json.dumps([], ensure_ascii=False), None, 0, 0, 0, 0.6, 0.7, "active", 1, None, None,
            f"seed-{i}", 0,
        )
        for i in range(count)
    ]
    with store._lock, store._conn:
        store._conn.executemany(
            """INSERT INTO memory(id, instance_id, timeline_id, character_id, text, kind, sources,
                                  happened_world, learned_world, recorded_world, semantic_watermark,
                                  strength, confidence, state, version, supersedes, superseded_by,
                                  source_key, decay_world)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            rows,
        )


def memory_scaling() -> list[dict]:
    """记忆条数 → 单批（含衰减）/ 召回 / 单条写入的墙钟成本。

    注意：这三个量都是**有状态**的（衰减水位会被推平、召回不写、写入会去重），
    所以只取首次调用的耗时。
    """
    w = World()
    day = {"k": 0}
    out = []
    try:
        for target in (0, 500, 2000, 8000):
            w.store._conn.execute("DELETE FROM memory")
            w.store._conn.commit()
            if target:
                _seed_memories(w.store, w.instance, w.timeline, w.character, target)
            cursor = int(w.world.clock_row(w.timeline)["processed_world"])

            day["k"] += 1
            base_real = float(w.world.clock_row(w.timeline)["base_real"])
            t0 = time.perf_counter()
            w.world.advance(w.instance, w.timeline, now_real=base_real + DAY_SECONDS * (day["k"] + 1), max_batches=1)
            batch_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            w.world.recall(w.instance, w.timeline, w.character, topic="堤禾 小事", world_seconds=cursor + DAY_SECONDS)
            recall_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            w.store.memory_add(
                {
                    "id": f"probe-{time.perf_counter_ns()}",
                    "instance_id": w.instance,
                    "timeline_id": w.timeline,
                    "character_id": w.character,
                    "text": "第 99999 条新增素材：与既有条目明显不同的一句话。",
                    "kind": "fact",
                    "learned_world": 0,
                    "recorded_world": 0,
                    "semantic_watermark": 0,
                    "strength": 0.6,
                    "confidence": 0.7,
                    "source_key": f"probe-{time.perf_counter_ns()}",
                }
            )
            add_ms = (time.perf_counter() - t0) * 1000
            out.append(
                {
                    "memories": target,
                    "batch_ms": round(batch_ms, 2),
                    "recall_ms": round(recall_ms, 2),
                    "memory_add_ms": round(add_ms, 2),
                }
            )
        return out
    finally:
        w.close()


def main() -> None:
    print("== A1 单个世界日批次成本（rate=1）")
    print(per_day_batch_cost())
    print("\n== A2 _clock_tick 节拍（5 s/拍）下的排水果位：现行时间盒 vs 改前固定 4 批")
    for mode in ("fixed4", "timebox"):
        for rate in (1, 86_400, 2_592_000):
            result = tick_cadence(rate, mode=mode)
            print(
                f"[{mode:>7}] rate={rate:>8}  排水={result['achieved_world_s_per_real_s']:>10.0f} 世界秒/现实秒"
                f"  6 拍处理 {result['processed_days']} 世界日  cpu={result['cpu_s_total']}s"
            )
            for row in result["ticks"][-2:]:
                print(f"      {row}")
    print("\n== A3 放开批次预算（max_batches=1e9，rate=2592000，2 拍）")
    print(unbounded_control())
    print("\n== B 长区间补算：单批成本随历史增长（240 世界日）")
    print(history_scaling())
    print("\n== C 记忆规模（每格 = 首次调用墙钟毫秒）")
    print("memories | batch_ms | recall_ms | memory_add_ms")
    for row in memory_scaling():
        print(f"{row['memories']:>8} | {row['batch_ms']:>8} | {row['recall_ms']:>9} | {row['memory_add_ms']:>13}")


if __name__ == "__main__":
    main()
