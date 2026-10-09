"""S-3 立项后的第一件事：把「每批成本」按**表**归因（先测量到可归因的粒度）。

为什么按表归因：S-3 的手段是「把历史行移出热表」⇒ 只有当成本确实落在**可归档的表**上时才成立。
语句条数是**确定性指标**（无噪声），作为主口径；耗时用 trace 回调的区间口径作同向佐证，
并按已固化的方法论（同实例、交替、多轮取中位数）执行。

用法：python .hermes/s3_table_cost.py <root> [days] [rounds]
"""

from __future__ import annotations

import asyncio
import collections
import re
import sqlite3
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 3
DAY = 86400

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402

_TABLE_RE = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?',
    re.IGNORECASE,
)


def shape(sql: str) -> str:
    s = re.sub(r'\s+', ' ', sql).strip()
    s = re.sub(r'\([^()]*\)', '(…)', s)
    s = re.sub(r"'[^']*'", "'…'", s)
    return s[:100]


def table_of(sql: str) -> str:
    m = _TABLE_RE.search(sql)
    return m.group(1).lower() if m else '<none>'


async def one_round() -> tuple[collections.Counter, collections.Counter, float, int]:
    rt = await build_runtime(load_config(ROOT), llm=FakeLLM(['收到。']))
    world, store = rt.world, rt.store
    inst = store.instance_list()[0]['id']
    tl = store.timeline_list(inst)[0]['id']
    world.activate(inst, tl, now_real=time.time())
    clock = store.clock_get(tl)
    base_real, base_world = float(clock['base_real']), int(clock['base_world'])
    processed = int(clock['processed_world'])

    calls: collections.Counter = collections.Counter()
    cost: collections.Counter = collections.Counter()  # ms，记在**上一条**语句上（区间口径）
    last = [None, time.perf_counter()]

    def cb(sql: str) -> None:
        now = time.perf_counter()
        calls[sql] += 1
        if last[0] is not None:
            cost[last[0]] += (now - last[1]) * 1000
        last[0], last[1] = sql, now

    store._conn.set_trace_callback(cb)
    start = time.perf_counter()
    res = world.advance(inst, tl, now_real=base_real + (processed + DAYS * DAY - base_world) + 0.5,
                        max_batches=DAYS)
    spent = time.perf_counter() - start
    store._conn.set_trace_callback(None)

    # 最后一条语句的等待也计入
    if last[0] is not None:
        cost[last[0]] += (time.perf_counter() - last[1]) * 1000

    batches = max(1, res.get('batches') or 1)
    await rt.service.shutdown()
    rt.store.close()
    return calls, cost, spent * 1000 / batches, batches


def census(db: Path) -> dict[str, int]:
    conn = sqlite3.connect(db)
    out: dict[str, int] = {}
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
        try:
            out[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except Exception:
            out[t] = -1
    conn.close()
    return out


async def main() -> None:
    db = ROOT / 'data' / 'isekai.db'
    rows = census(db)
    print(f'# 靶子 {db}  库 {db.stat().st_size / 1024:.0f} KB')

    per_day_ms: list[float] = []
    agg_calls: collections.Counter = collections.Counter()
    agg_cost: collections.Counter = collections.Counter()
    total_batches = 0
    for _ in range(ROUNDS):
        calls, cost, ms, batches = await one_round()
        per_day_ms.append(ms)
        total_batches += batches
        agg_calls.update(calls)
        agg_cost.update(cost)
    med = statistics.median(per_day_ms)
    print(f'# 每世界日 ms（{ROUNDS} 轮）: {[round(v, 2) for v in per_day_ms]}  中位数 {med:.2f}')
    print(f'# 语句总数 {sum(agg_calls.values())} 批数 {total_batches} ⇒ {sum(agg_calls.values()) / max(1,total_batches):.1f} 条/批')

    print('\n== 按表归因（语句条数为确定性口径；ms 为区间口径同向佐证） ==')
    print(f'{"表":<22}{"条/批":>9}{"ms/日":>10}{"表内行数":>10}{"ms/条":>9}')
    by_table: collections.Counter = collections.Counter()
    for sql, n in agg_calls.items():
        by_table[table_of(sql)] += n
    cost_by_table: collections.Counter = collections.Counter()
    for sql, ms in agg_cost.items():
        cost_by_table[table_of(sql)] += ms
    for t, n in by_table.most_common():
        n_per_batch = n / max(1, total_batches)
        ms_day = cost_by_table[t] / max(1, total_batches)
        ms_stmt = (cost_by_table[t] / n) if n else 0.0
        print(f'{t:<22}{n_per_batch:>9.1f}{ms_day:>10.2f}{rows.get(t, -1):>10}{ms_stmt:>9.3f}')

    print('\n== 单条最贵的语句形状（前 12，区间口径） ==')
    for sql, ms in agg_cost.most_common(12):
        print(f'{ms / max(1,total_batches):>8.3f} ms/日  x{agg_calls[sql]/max(1,total_batches):<6.1f}  {shape(sql)}')


if __name__ == '__main__':
    asyncio.run(main())
