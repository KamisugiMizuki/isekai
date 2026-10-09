"""审查用：真实推进一遍，记录**原始 SQL**（不 shape）的次数/耗时，再对每条热语句跑 EXPLAIN QUERY PLAN，
把「热语句是不是全表扫描 / 扫描行数是否随表行数增长」一次性列出来。

用法：python .hermes/s3r_plan_audit.py [src_root] [days]
"""
from __future__ import annotations

import asyncio
import collections
import pathlib
import re
import shutil
import sqlite3
import statistics
import sys
import time

src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.hermes/ab')
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 3
DAY = 86400
WORK = src.parent / 's3r_work'

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402


def norm(sql: str) -> str:
    return re.sub(r'\s+', ' ', sql).strip()


class Acc:
    def __init__(self) -> None:
        self.ms: collections.Counter = collections.Counter()
        self.calls: collections.Counter = collections.Counter()
        self.rows: collections.Counter = collections.Counter()
        self.raw: dict[str, str] = {}


class Cur:
    def __init__(self, cur, key, acc):
        self._cur, self._key, self._acc = cur, key, acc

    def _t(self, t0):
        self._acc.ms[self._key] += (time.perf_counter() - t0) * 1000

    def fetchall(self):
        t0 = time.perf_counter(); r = self._cur.fetchall(); self._t(t0)
        self._acc.rows[self._key] += len(r); return r

    def fetchone(self):
        t0 = time.perf_counter(); r = self._cur.fetchone(); self._t(t0)
        self._acc.rows[self._key] += 0 if r is None else 1; return r

    def __iter__(self):
        t0 = time.perf_counter()
        try:
            for row in self._cur:
                self._acc.rows[self._key] += 1
                yield row
        finally:
            self._t(t0)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class Conn:
    def __init__(self, conn, acc):
        object.__setattr__(self, '_conn', conn)
        object.__setattr__(self, '_acc', acc)

    def execute(self, sql, params=()):
        key = norm(sql)
        acc = object.__getattribute__(self, '_acc')
        acc.calls[key] += 1
        acc.raw[key] = sql
        t0 = time.perf_counter()
        cur = object.__getattribute__(self, '_conn').execute(sql, params)
        acc.ms[key] += (time.perf_counter() - t0) * 1000
        return Cur(cur, key, acc)

    def executemany(self, sql, seq):
        key = norm(sql)
        acc = object.__getattribute__(self, '_acc')
        acc.calls[key] += 1
        acc.raw[key] = sql
        t0 = time.perf_counter()
        cur = object.__getattribute__(self, '_conn').executemany(sql, seq)
        acc.ms[key] += (time.perf_counter() - t0) * 1000
        return cur

    def __enter__(self):
        object.__getattribute__(self, '_conn').__enter__(); return self

    def __exit__(self, *a):
        acc = object.__getattribute__(self, '_acc')
        t0 = time.perf_counter()
        r = object.__getattribute__(self, '_conn').__exit__(*a)
        acc.ms['<txn __exit__>'] += (time.perf_counter() - t0) * 1000
        acc.calls['<txn __exit__>'] += 1
        acc.raw['<txn __exit__>'] = ''
        return r

    def set_trace_callback(self, cb):
        return object.__getattribute__(self, '_conn').set_trace_callback(cb)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)


async def run(root: pathlib.Path) -> tuple[Acc, float, int]:
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
    res = world.advance(inst, tl, now_real=br + (processed + DAYS * DAY - bw) + 0.5, max_batches=DAYS)
    spent = (time.perf_counter() - t0) * 1000
    await rt.service.shutdown()
    rt.store.close()
    return acc, spent, max(1, res.get('batches') or 1)


async def main() -> None:
    if WORK.exists():
        shutil.rmtree(WORK)
    (WORK / 'data').mkdir(parents=True)
    shutil.copy2(src / 'data' / 'isekai.db', WORK / 'data' / 'isekai.db')
    acc, ms, nb = await run(WORK)
    print(f'# {WORK}  {DAYS} 天，墙钟 {ms:.1f} ms ⇒ {ms/nb:.2f} ms/日，语句 {sum(acc.calls.values())} 条'
          f' ⇒ {sum(acc.calls.values())/nb:.1f} 条/批')
    tot = sum(v for k, v in acc.ms.items() if k != '<txn __exit__>')
    print(f'# execute+fetch 合计 {tot:.2f} ms ⇒ {tot/nb:.2f} ms/日；事务上下文 __exit__ {acc.ms["<txn __exit__>"]/nb:.3f} ms/日')
    import json as _json
    (src.parent / 's3r_raw_sql.json').write_text(
        _json.dumps({'batches': nb, 'sql': {k: {'calls': acc.calls[k], 'ms': acc.ms[k], 'rows': acc.rows[k]}
                                            for k in acc.calls}}, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'# 原始 SQL 明细 -> {src.parent / "s3r_raw_sql.json"}')

    plain = sqlite3.connect(src / 'data' / 'isekai.db')
    inst = plain.execute('SELECT id FROM instance LIMIT 1').fetchone()[0]
    tl = plain.execute('SELECT id FROM timeline LIMIT 1').fetchone()[0]
    until = plain.execute('SELECT processed_world FROM timeline_clock LIMIT 1').fetchone()[0]

    print('\n== 热语句（≥0.05 ms/日）的计划 ==')
    for k, v in sorted(acc.ms.items(), key=lambda kv: -kv[1]):
        if k == '<txn __exit__>' or v / nb < 0.05:
            continue
        calls = acc.calls[k]
        rpc = acc.rows[k] / calls if calls else 0
        print(f'\n  {v/nb:7.3f} ms/日  x{calls/nb:5.2f}/批  rows/call={rpc:6.1f}  {v/calls:7.4f} ms/条')
        print(f'      SQL: {k[:200]}')
        sql = acc.raw[k]
        # 用真实参数跑计划：先用 sqlite3 的 ? 占位数量匹配
        n_par = sql.count('?')
        params = _guess_params(sql, n_par, inst, tl, until)
        try:
            for row in plain.execute('EXPLAIN QUERY PLAN ' + sql, params):
                print(f'      PLAN: {row[3]}')
        except Exception as e:
            print(f'      PLAN: (无法执行: {e})')
    plain.close()


def _guess_params(sql: str, n: int, inst: str, tl: str, until: int) -> tuple:
    """计划形状对参数不敏感（实测 `? IS NULL OR col=?` 两种取值都是 SCAN）；给一组类型合适的值即可。"""
    out: list[object] = []
    for _ in range(n):
        out.append(None)
    if n >= 1:
        out[0] = inst
    if n >= 2:
        out[1] = tl
    for i in range(n):
        if out[i] is None:
            out[i] = until if i + 1 < n else 'x'
    return tuple(out)


if __name__ == '__main__':
    asyncio.run(main())
