"""Lead 的独立复核：只归档 `effect_state` 的死行（`active=0`）是否真的更快。

被复核的结论（由对抗式审查者提出，见我上一版结论的反例）：
**公平归档 = 只删 `effect_state WHERE active=0`**（推进路径全部读取都带 `active=1` ⇒ 这些行永不被读），
7 轮交替测得 0.84×（原库 3.37 → 2.85 ms/日）。

为什么这与我先前那次「归档大而冷的表」不同：
- 我先前归档的是 `claim`/`knowledge`/`event`/`experience` —— 它们**只花 0.44 ms/日**，归档它们没有收益；
- 审查者归档的是**最热的那张表里的死行**（`effect_state` 8,846 → 4,425 行），
  于是 `UPDATE effect_state SET active=0 ...`（2.4 条/批）与 `effect_constraints` 取数同时变便宜。

本脚本独立复核三件事：
1. **公平性**：归档前后「逐语句调用次数 / 每批返回行数」是否完全相同（不同即说明改的不是同一件工作）；
2. **主对照**：同一实例、同一时间段、交替多轮，生产缓存（64 MB）；
3. **缓存对照**：同一轮内也在 2 MB 小缓存下交替，排除「只是缓存命中率变了」。

用法：python .hermes/lead_s3_deadrow_check.py [src_root] [days] [rounds]
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
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 5
DAY = 86400
FULL = Path('.hermes/s3l_full').resolve()
DEAD = Path('.hermes/s3l_dead').resolve()

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402

Norm = lambda s: re.sub(r'\s+', ' ', s).strip()  # noqa: E731


def clone(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns('*.db-wal', '*.db-shm'))


def strip_dead(root: Path) -> tuple[int, int]:
    """只删 `active=0` 的后果行（模拟「归档死行」）。**为公平起见不 VACUUM**（不改变页布局以外的东西）。"""
    db = root / 'data' / 'isekai.db'
    conn = sqlite3.connect(db)
    before = conn.execute('SELECT COUNT(*) FROM effect_state').fetchone()[0]
    conn.execute('DELETE FROM effect_state WHERE active=0')
    conn.commit()
    after = conn.execute('SELECT COUNT(*) FROM effect_state').fetchone()[0]
    # 只统计，不清理：让两组只差「行数」，不差「页是否被整理过」
    conn.close()
    return before, after


class Cur:
    def __init__(self, cur, key, stats):
        self._cur, self._key, self._stats = cur, key, stats

    def fetchall(self):
        t = time.perf_counter()
        r = self._cur.fetchall()
        self._stats['ms'][self._key] += (time.perf_counter() - t) * 1000
        self._stats['rows'][self._key] += len(r)
        return r

    def fetchone(self):
        t = time.perf_counter()
        r = self._cur.fetchone()
        self._stats['ms'][self._key] += (time.perf_counter() - t) * 1000
        self._stats['rows'][self._key] += 0 if r is None else 1
        return r

    def __iter__(self):
        t = time.perf_counter()
        for row in self._cur:
            self._stats['rows'][self._key] += 1
            yield row
        self._stats['ms'][self._key] += (time.perf_counter() - t) * 1000

    def __getattr__(self, name):
        return getattr(self._cur, name)


class Conn:
    def __init__(self, conn, stats, cache: str):
        object.__setattr__(self, '_c', conn)
        object.__setattr__(self, '_s', stats)
        object.__setattr__(self, '_cache', cache)
        object.__setattr__(self, '_applied', False)

    def _apply(self):
        if not object.__getattribute__(self, '_applied'):
            object.__setattr__(self, '_applied', True)
            c = object.__getattribute__(self, '_c')
            c.execute(f'PRAGMA cache_size={object.__getattribute__(self, "_cache")}')
            c.execute('PRAGMA mmap_size=0')

    def execute(self, sql, params=()):
        self._apply()
        key = Norm(sql)
        s = object.__getattribute__(self, '_s')
        s['calls'][key] += 1
        t = time.perf_counter()
        cur = object.__getattribute__(self, '_c').execute(sql, params)
        s['ms'][key] += (time.perf_counter() - t) * 1000
        return Cur(cur, key, s)

    def __enter__(self):
        object.__getattribute__(self, '_c').__enter__()
        return self

    def __exit__(self, *a):
        s = object.__getattribute__(self, '_s')
        t = time.perf_counter()
        r = object.__getattribute__(self, '_c').__exit__(*a)
        s['ms']['<txn>'] += (time.perf_counter() - t) * 1000
        s['calls']['<txn>'] += 1
        return r

    def set_trace_callback(self, cb):
        return object.__getattribute__(self, '_c').set_trace_callback(cb)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_c'), name)


async def one(root: Path, cache: str) -> tuple[dict, float]:
    stats = {'ms': collections.Counter(), 'calls': collections.Counter(), 'rows': collections.Counter()}
    rt = await build_runtime(load_config(root), llm=FakeLLM(['收到。']))
    world, store = rt.world, rt.store
    inst = store.instance_list()[0]['id']
    tl = store.timeline_list(inst)[0]['id']
    world.activate(inst, tl, now_real=time.time())
    store._local.conn = Conn(store._conn, stats, cache)
    clock = store.clock_get(tl)
    br, bw, pr = float(clock['base_real']), int(clock['base_world']), int(clock['processed_world'])
    t = time.perf_counter()
    res = world.advance(inst, tl, now_real=br + (pr + DAYS * DAY - bw) + 0.5, max_batches=DAYS)
    ms = (time.perf_counter() - t) * 1000 / max(1, res.get('batches') or 1)
    await rt.service.shutdown()
    rt.store.close()
    return stats, ms


async def main() -> None:
    clone(SRC, FULL)
    clone(SRC, DEAD)
    before, after = strip_dead(DEAD)
    print(f'# 只归档死行：effect_state {before} -> {after} 行（active=0 被移除）')
    for label, root in (('原库', FULL), ('归档死行', DEAD)):
        db = root / 'data' / 'isekai.db'
        print(f'  {label:<10} {db.stat().st_size / 1024:>8.0f} KB')

    for cache, clabel in (('-65536', '64 MB（生产）'), ('-2048', '2 MB（小缓存）')):
        full_ms: list[float] = []
        dead_ms: list[float] = []
        fs: dict = {'ms': collections.Counter(), 'calls': collections.Counter(), 'rows': collections.Counter()}
        ds: dict = {'ms': collections.Counter(), 'calls': collections.Counter(), 'rows': collections.Counter()}
        for i in range(ROUNDS):
            order = (DEAD, FULL) if i % 2 == 0 else (FULL, DEAD)
            for root in order:
                st, ms = await one(root, cache)
                if root == FULL:
                    full_ms.append(ms)
                else:
                    dead_ms.append(ms)
                tgt = fs if root == FULL else ds
                for k in ('ms', 'calls', 'rows'):
                    tgt[k].update(st[k])
        mf, md = statistics.median(full_ms), statistics.median(dead_ms)
        print(f'\n== 页缓存 {clabel}（{ROUNDS} 轮交替）==')
        print(f'  原库     ms/日 {[round(v,2) for v in full_ms]} 中位 {mf:.2f}')
        print(f'  归档死行 ms/日 {[round(v,2) for v in dead_ms]} 中位 {md:.2f}')
        print(f'  ⇒ 归档/原库 = {md/mf:.2f}×   （成对 {[round(d/f,2) for f,d in zip(full_ms,dead_ms)]}）')

    print('\n== 公平性检查（语句调用次数 / 返回行数，必须相同） ==')
    diff_calls = [k for k in set(fs['calls']) | set(ds['calls']) if fs['calls'][k] != ds['calls'][k]]
    diff_rows = [k for k in set(fs['rows']) | set(ds['rows']) if fs['rows'][k] != ds['rows'][k]]
    print(f'  语句形状 {len(set(fs["calls"]) | set(ds["calls"]))} 种；调用次数不同 {len(diff_calls)} 种；返回行数不同 {len(diff_rows)} 种')
    for k in diff_calls[:5]:
        print(f'    CALLS {k[:90]}\n      full={fs["calls"][k]} dead={ds["calls"][k]}')

    print('\n== 逐语句单条成本（中位口径不支持，这里给总 ms/日） ==')
    print(f'{"原库 ms/日":>11}{"归档 ms/日":>11}{"倍":>7}{"条/批":>8}  语句')
    for k, v in sorted(fs['ms'].items(), key=lambda kv: -kv[1])[:12]:
        f = v / ROUNDS
        d = ds['ms'][k] / ROUNDS
        print(f'{f:>11.3f}{d:>11.3f}{(d/f if f else 0):>7.2f}{fs["calls"][k]/ROUNDS/DAYS:>8.2f}  {k[:78]}')


if __name__ == '__main__':
    asyncio.run(main())
