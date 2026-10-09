"""S-3 判别实验：把历史行移出热表后，收益到底来自「热表行变少」还是「整库变小」。

背景（Lead 实测，见 s3_table_cost.py 输出）：
- `claim` 9,804 行只花 0.14 ms/日、`knowledge` 6,534 行只花 0.07 ms/日 ⇒ **大而冷的表几乎不花热路径成本**；
- 真正贵的是**小而热的状态表**：`effect_state`（9.2 条/批）1.31 ms/日、`reaction`（11 条/批）0.56 ms/日、
  外加 `COMMIT` 0.486 ms/日。

因此必须区分两种机制：
- **M1 直接效应**：归档那张表 ⇒ 该表自己的语句变便宜（但只有 `claim`/`knowledge` 这类表会被归档，
  而它们本来只花 0.14 / 0.07 ⇒ 直接效应上限很小）；
- **M2 间接效应**：整库变小 ⇒ **其它表**的语句也变便宜（B 树更浅、页更少）⇒ 这才是 s3_archive_probe 里 26% 的来源。

做法：复制老实例两份（A = 原库，B = 归档历史前缀），然后**交替**推进；
同时**逐表归因**，重点看**没有被归档的表**（`effect_state` / `reaction` / `unit` / `life_plan`）
在 B 上是否变便宜——这才能把 M1 与 M2 分开。

用法：python .hermes/s3_mechanism_probe.py <aged_root> [days] [rounds]
"""

from __future__ import annotations

import asyncio
import collections
import re
import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path

SRC = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path('.hermes/ab').resolve()
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 3
DAY = 86400
FULL = Path('.hermes/s3m_full').resolve()
ARCH = Path('.hermes/s3m_arch').resolve()

#: 可归档的历史表（时间列, 保留比例）。**只归档纯粹的历史事实**，
#: 不碰任何「活跃状态」表（effect_state / reaction / life_plan / character_state / *_state / ledger / relation）。
ARCHIVE = (
    ("knowledge", "world_seconds", 0.4),
    ("claim", "earliest_world", 0.4),
    ("event", "world_seconds", 0.4),
    ("experience", "world_seconds", 0.4),
)

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402

_TABLE_RE = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?',
    re.IGNORECASE,
)


def table_of(sql: str) -> str:
    m = _TABLE_RE.search(sql)
    return m.group(1).lower() if m else '<none>'


def clone(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('*.db-wal', '*.db-shm'))


def archive(root: Path) -> dict[str, tuple[int, int]]:
    db = root / 'data' / 'isekai.db'
    conn = sqlite3.connect(db)
    conn.execute('PRAGMA journal_mode=DELETE')
    stats: dict[str, tuple[int, int]] = {}
    for table, column, keep in ARCHIVE:
        before = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        bounds = conn.execute(f'SELECT MIN({column}), MAX({column}) FROM "{table}"').fetchone()
        if bounds and bounds[0] is not None:
            low, high = bounds
            cut = int(low + (high - low) * (1 - keep))
            conn.execute(f'DELETE FROM "{table}" WHERE {column} < ?', (cut,))
        conn.commit()
        after = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        stats[table] = (before, after)
    conn.execute('VACUUM')
    conn.close()
    return stats


async def one_round(root: Path) -> tuple[collections.Counter, collections.Counter, float]:
    rt = await build_runtime(load_config(root), llm=FakeLLM(['收到。']))
    world, store = rt.world, rt.store
    inst = store.instance_list()[0]['id']
    tl = store.timeline_list(inst)[0]['id']
    world.activate(inst, tl, now_real=time.time())
    clock = store.clock_get(tl)
    base_real, base_world = float(clock['base_real']), int(clock['base_world'])
    processed = int(clock['processed_world'])

    calls: collections.Counter = collections.Counter()
    cost: collections.Counter = collections.Counter()
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
    if last[0] is not None:
        cost[last[0]] += (time.perf_counter() - last[1]) * 1000
    batches = max(1, res.get('batches') or 1)
    await rt.service.shutdown()
    rt.store.close()
    return calls, cost, spent * 1000 / batches


async def main() -> None:
    clone(SRC, FULL)
    clone(SRC, ARCH)
    stats = archive(ARCH)
    print('# 归档（移出热表的历史前缀，保留最近 40%）')
    for t, (b, a) in stats.items():
        print(f'  {t:<12} {b:>6} -> {a:>6}  (-{b - a})')
    for label, root in (('原库', FULL), ('归档', ARCH)):
        db = root / 'data' / 'isekai.db'
        print(f'  {label} 库大小 {db.stat().st_size / 1024:.0f} KB')

    full_ms: list[float] = []
    arch_ms: list[float] = []
    full_tab: collections.Counter = collections.Counter()
    arch_tab: collections.Counter = collections.Counter()
    full_cost: collections.Counter = collections.Counter()
    arch_cost: collections.Counter = collections.Counter()
    for i in range(ROUNDS):
        if i % 2 == 0:
            c, k, ms = await one_round(ARCH)
            arch_ms.append(ms)
            c2, k2, ms2 = await one_round(FULL)
            full_ms.append(ms2)
        else:
            c2, k2, ms2 = await one_round(FULL)
            full_ms.append(ms2)
            c, k, ms = await one_round(ARCH)
            arch_ms.append(ms)
        for sql, n in c.items():
            arch_tab[table_of(sql)] += n
        for sql, n in c2.items():
            full_tab[table_of(sql)] += n
        for sql, v in k.items():
            arch_cost[table_of(sql)] += v
        for sql, v in k2.items():
            full_cost[table_of(sql)] += v
    rounds_each = ROUNDS
    batches_each = ROUNDS * DAYS

    print(f'\n# 每世界日 ms 原库   : {[round(v, 2) for v in full_ms]} 中位数 {statistics.median(full_ms):.2f}')
    print(f'# 每世界日 ms 归档后 : {[round(v, 2) for v in arch_ms]} 中位数 {statistics.median(arch_ms):.2f}')
    ratio = statistics.median(arch_ms) / statistics.median(full_ms)
    print(f'# ⇒ 归档/原库 = {ratio:.2f}×（<1 表示归档更快）')

    print('\n# 逐表 ms/日：重点看**没有被归档的表**是否也变便宜（= M2 整库变小的间接效应）')
    print(f'{"表":<20}{"原库 ms/日":>12}{"归档 ms/日":>12}{"倍":>7}')
    for t in sorted(set(full_cost) | set(arch_cost), key=lambda x: -max(full_cost[x], arch_cost[x])):
        f = full_cost[t] / batches_each
        a = arch_cost[t] / batches_each
        if max(f, a) < 0.02:
            continue
        print(f'{t:<20}{f:>12.2f}{a:>12.2f}{(a / f if f else 0):>7.2f}')

    print('\n# 按表语句条数（/批）：确认归档表少碰了、其它表工作量不变')
    print(f'{"表":<20}{"原库":>9}{"归档":>9}')
    for t in sorted(set(full_tab) | set(arch_tab), key=lambda x: -max(full_tab[x], arch_tab[x])):
        if max(full_tab[t], arch_tab[t]) < 0.5 * batches_each:
            continue
        print(f'{t:<20}{full_tab[t] / batches_each:>9.1f}{arch_tab[t] / batches_each:>9.1f}')


if __name__ == '__main__':
    asyncio.run(main())
