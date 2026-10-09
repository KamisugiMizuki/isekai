"""审查用 A：两个副本推进相同天数后，**除被删的 active=0 死行外**状态是否逐表一致。
   审查用 B：同一次推进里「trace 回调计数」与「代理计数」的差异（Lead 用前者，我用后者）。

用法：python .hermes/s3r_trace_vs_proxy.py [src_root] [days]
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
TV = src.parent / 's3r_tv'

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402


def norm(sql: str) -> str:
    return ' '.join(sql.split())


class Proxy:
    def __init__(self, conn):
        object.__setattr__(self, '_c', conn)
        object.__setattr__(self, 'calls', collections.Counter())
        object.__setattr__(self, 'rows', collections.Counter())

    def execute(self, sql, params=()):
        self.calls[norm(sql)] += 1
        cur = object.__getattribute__(self, '_c').execute(sql, params)
        key = norm(sql)
        rows = object.__getattribute__(self, 'rows')

        class C:
            def fetchall(self_inner):
                r = cur.fetchall(); rows[key] += len(r); return r

            def fetchone(self_inner):
                r = cur.fetchone(); rows[key] += 0 if r is None else 1; return r

            def __iter__(self_inner):
                for r in cur:
                    rows[key] += 1
                    yield r

            def __getattr__(self_inner, name):
                return getattr(cur, name)

        return C()

    def __enter__(self):
        object.__getattribute__(self, '_c').__enter__(); return self

    def __exit__(self, *a):
        return object.__getattribute__(self, '_c').__exit__(*a)

    def set_trace_callback(self, cb):
        return object.__getattribute__(self, '_c').set_trace_callback(cb)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_c'), name)


async def main() -> None:
    if TV.exists():
        shutil.rmtree(TV)
    (TV / 'data').mkdir(parents=True)
    shutil.copy2(src / 'data' / 'isekai.db', TV / 'data' / 'isekai.db')

    rt = await build_runtime(load_config(TV), llm=FakeLLM(['收到。']))
    world, store = rt.world, rt.store
    inst = store.instance_list()[0]['id']
    tl = store.timeline_list(inst)[0]['id']
    world.activate(inst, tl, now_real=time.time())
    proxy = Proxy(store._conn)
    store._local.conn = proxy
    trace_calls: collections.Counter = collections.Counter()

    def cb(sql: str) -> None:
        trace_calls[norm(sql)] += 1

    real = object.__getattribute__(proxy, '_c')
    real.set_trace_callback(cb)
    clock = store.clock_get(tl)
    br, bw = float(clock['base_real']), int(clock['base_world'])
    processed = int(clock['processed_world'])
    t0 = time.perf_counter()
    res = world.advance(inst, tl, now_real=br + (processed + DAYS * DAY - bw) + 0.5, max_batches=DAYS)
    spent = (time.perf_counter() - t0) * 1000
    real.set_trace_callback(None)
    b = max(1, res.get('batches') or 1)
    await rt.service.shutdown()
    rt.store.close()

    pc, tc = sum(proxy.calls.values()), sum(trace_calls.values())
    print(f'# 代理计数 {pc} 条（{pc/b:.1f}/批）  trace 回调计数 {tc} 条（{tc/b:.1f}/批）  差 {pc - tc}')
    print(f'# 墙钟 {spent:.1f} ms（{spent/b:.2f} ms/日）——注意 trace 回调本身有开销')
    keys = set(proxy.calls) | set(trace_calls)
    diff = sorted(((proxy.calls[k] - trace_calls[k], k) for k in keys), key=lambda r: -abs(r[0]))
    print('\n== 计数差异最大的语句（正=代理多，负=trace 多）==')
    for d, k in diff[:14]:
        if d:
            print(f'  {d:>5}  代理 {proxy.calls[k]:>4}  trace {trace_calls[k]:>4}  返回行 {proxy.rows[k]:>4}  {k[:100]}')
    print('\n== 返回行数最多的语句 ==')
    for k, n in proxy.rows.most_common(12):
        print(f'  {n:>5} 行 / {proxy.calls[k]:>4} 次   {k[:100]}')
    print('\n== trace 里出现而代理里没有的（多半是隐式事务语句）==')
    for k in sorted(set(trace_calls) - set(proxy.calls)):
        print(f'  trace {trace_calls[k]:>4} 次   {k[:100]}')


if __name__ == '__main__':
    asyncio.run(main())
