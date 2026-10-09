"""审查用：跨副本 A/B 是否合法？——先验证「同一起点 + 同一段世界时间」的两个副本是否**逐位一致**。

Lead 的三个探针（s3_archive_probe / s3_mechanism_probe / s3_cache_masking_probe）全部是
「同一源库的两个副本、交替推进、比较耗时」。这条方法论只有在**副本间行为确定一致**时才成立。
本脚本做两件事：
 A. 双胞胎测试：两个未改动的副本各推进同样天数 ⇒ 比较逐语句调用次数、返回行数、全表摘要；
 B. 公平归档 vs 原库：比较「除被删死行以外」的状态是否一致。

用法：python .hermes/s3r_determinism.py [src_root] [days]
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import pathlib
import shutil
import sqlite3
import sys
import time

src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.hermes/ab')
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
DAY = 86400
BASE = src.parent

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402

TABLES = ('effect_state', 'reaction', 'life_plan', 'claim', 'knowledge', 'event', 'experience',
          'unit', 'intent', 'environment_state', 'institution_state', 'custom_state', 'timeline_clock')


class Cur:
    def __init__(self, cur, key, acc):
        self._cur, self._key, self._acc = cur, key, acc

    def fetchall(self):
        r = self._cur.fetchall(); self._acc.rows[self._key] += len(r); return r

    def fetchone(self):
        r = self._cur.fetchone(); self._acc.rows[self._key] += 0 if r is None else 1; return r

    def __iter__(self):
        for r in self._cur:
            self._acc.rows[self._key] += 1
            yield r

    def __getattr__(self, name):
        return getattr(self._cur, name)


class Conn:
    def __init__(self, conn, acc):
        object.__setattr__(self, '_c', conn)
        object.__setattr__(self, '_a', acc)

    def execute(self, sql, params=()):
        key = ' '.join(sql.split())
        a = object.__getattribute__(self, '_a')
        a.calls[key] += 1
        return Cur(object.__getattribute__(self, '_c').execute(sql, params), key, a)

    def __enter__(self):
        object.__getattribute__(self, '_c').__enter__(); return self

    def __exit__(self, *x):
        return object.__getattribute__(self, '_c').__exit__(*x)

    def set_trace_callback(self, cb):
        return object.__getattribute__(self, '_c').set_trace_callback(cb)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_c'), name)


class Acc:
    def __init__(self):
        self.calls = collections.Counter()
        self.rows = collections.Counter()


async def advance(root: pathlib.Path) -> tuple[Acc, float]:
    acc = Acc()
    rt = await build_runtime(load_config(root), llm=FakeLLM(['收到。']))
    world, store = rt.world, rt.store
    inst = store.instance_list()[0]['id']
    tl = store.timeline_list(inst)[0]['id']
    world.activate(inst, tl, now_real=time.time())
    store._local.conn = Conn(store._conn, acc)
    clock = store.clock_get(tl)
    br, bw = float(clock['base_real']), int(clock['base_world'])
    processed = int(clock['processed_world'])
    t0 = time.perf_counter()
    world.advance(inst, tl, now_real=br + (processed + DAYS * DAY - bw) + 0.5, max_batches=DAYS)
    spent = (time.perf_counter() - t0) * 1000
    await rt.service.shutdown()
    rt.store.close()
    return acc, spent


def clone(name: str, delete_dead: bool = False) -> pathlib.Path:
    root = BASE / name
    if root.exists():
        shutil.rmtree(root)
    (root / 'data').mkdir(parents=True)
    shutil.copy2(src / 'data' / 'isekai.db', root / 'data' / 'isekai.db')
    if delete_dead:
        c = sqlite3.connect(root / 'data' / 'isekai.db')
        c.execute('PRAGMA journal_mode=DELETE')
        c.execute('DELETE FROM effect_state WHERE active=0')
        c.commit()
        c.execute('VACUUM')
        c.close()
    return root


def digest(root: pathlib.Path, where: dict[str, str] | None = None) -> dict:
    where = where or {}
    c = sqlite3.connect(root / 'data' / 'isekai.db')
    out = {}
    for t in TABLES:
        w = where.get(t, '')
        rows = c.execute(f'SELECT * FROM "{t}" {w} ORDER BY 1, 2').fetchall()
        out[t] = (len(rows), hashlib.sha256(repr(rows).encode()).hexdigest()[:16])
    c.close()
    return out


def cmp_report(a: Acc, b: Acc, la: str, lb: str) -> None:
    keys = set(a.calls) | set(b.calls)
    dc = {k: (a.calls[k], b.calls[k]) for k in keys if a.calls[k] != b.calls[k]}
    dr = {k: (a.rows[k], b.rows[k]) for k in keys if a.rows[k] != b.rows[k]}
    print(f'  调用次数: {la}={sum(a.calls.values())}  {lb}={sum(b.calls.values())}  不同形状 {len(dc)}')
    for k, v in list(dc.items())[:10]:
        print(f'    calls {la}={v[0]} {lb}={v[1]}  {k[:100]}')
    print(f'  返回行数: {la}={sum(a.rows.values())}  {lb}={sum(b.rows.values())}  不同形状 {len(dr)}')
    for k, v in list(dr.items())[:10]:
        print(f'    rows  {la}={v[0]} {lb}={v[1]}  {k[:100]}')


async def main() -> None:
    print(f'== A. 双胞胎测试（两个未改动副本，各推进 {DAYS} 天）==')
    t1 = clone('s3r_twin1')
    t2 = clone('s3r_twin2')
    a1, ms1 = await advance(t1)
    a2, ms2 = await advance(t2)
    print(f'  墙钟 {ms1:.1f} ms vs {ms2:.1f} ms')
    cmp_report(a1, a2, 'twin1', 'twin2')
    d1, d2 = digest(t1), digest(t2)
    same = d1 == d2
    print(f'  全表内容摘要一致: {same}')
    if not same:
        for k in d1:
            if d1[k] != d2[k]:
                print(f'    {k}: {d1[k]} vs {d2[k]}')

    print(f'\n== B. 公平归档副本 vs 原库副本（各推进 {DAYS} 天，比较「死行以外」的状态）==')
    f = clone('s3r_state_full')
    d = clone('s3r_state_dead', delete_dead=True)
    af, _ = await advance(f)
    ad, _ = await advance(d)
    cmp_report(af, ad, 'full', 'dead')
    wf = digest(f, {'effect_state': 'WHERE active=1'})
    wd = digest(d, {'effect_state': 'WHERE active=1'})
    print(f'  仅比较 active=1 的 effect_state 及其它全部表：一致 = {wf == wd}')
    if wf != wd:
        for k in wf:
            if wf[k] != wd[k]:
                print(f'    {k}: {wf[k]} vs {wd[k]}')
    allf = digest(f)
    alld = digest(d)
    print(f'  含死行的全表比较：一致 = {allf == alld}（应当 False，因为死行被移走）')
    for k in allf:
        if allf[k] != alld[k]:
            print(f'    差异表 {k}: full={allf[k]} dead={alld[k]}')


if __name__ == '__main__':
    asyncio.run(main())
