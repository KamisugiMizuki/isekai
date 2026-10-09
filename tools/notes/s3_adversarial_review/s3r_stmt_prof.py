"""审查用：**正确处理**的逐语句计时（execute+fetch 内计时，不是区间）与返回行数归因。

与 Lead 的 s3_table_cost.py 的区别：
- 时间用「从 execute 开始到最后一次 fetch 结束」的真实语句耗时，不把语句之间的 Python 时间算进去；
- 同时记录每条语句实际**返回的行数**（这是「工作量」的确定性指标之一）；
- 表归因同时给出「首个表」（Lead 的口径）与「语句里出现的全部表」；
- 报出每批是多少次 execute（确定性），以及 DML/COMMIT 的耗时。

用法：python .hermes/s3r_stmt_prof.py <root> [days] [label]
"""
from __future__ import annotations

import asyncio
import collections
import json
import re
import statistics
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve()
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
LABEL = sys.argv[3] if len(sys.argv) > 3 else ROOT.name
DAY = 86400

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402

_TABLE_RE = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?',
    re.IGNORECASE,
)
_ALL_TABLES_RE = re.compile(
    r'\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?',
    re.IGNORECASE,
)


def shape(sql: str) -> str:
    s = re.sub(r'\s+', ' ', sql).strip()
    s = re.sub(r'\([^()]*\)', '(…)', s)
    s = re.sub(r"'[^']*'", "'…'", s)
    return s[:110]


def table_of(sql: str) -> str:
    m = _TABLE_RE.search(sql)
    return m.group(1).lower() if m else '<none>'


def tables_of(sql: str) -> tuple[str, ...]:
    return tuple(sorted({m.lower() for m in _ALL_TABLES_RE.findall(sql)}))


class Acc:
    def __init__(self) -> None:
        self.ms: collections.Counter = collections.Counter()
        self.calls: collections.Counter = collections.Counter()
        self.rows: collections.Counter = collections.Counter()
        self.dml_ms: collections.Counter = collections.Counter()
        self.commit_ms = 0.0
        self.commit_n = 0
        self.other_ms = 0.0
        self.other_n = 0


class Cur:
    def __init__(self, cur, key, acc: Acc) -> None:
        self._cur = cur
        self._key = key
        self._acc = acc
        self._n = 0

    def _add(self, t0: float) -> None:
        self._acc.ms[self._key] += (time.perf_counter() - t0) * 1000

    def fetchall(self):
        t0 = time.perf_counter()
        r = self._cur.fetchall()
        self._add(t0)
        self._n += len(r)
        self._acc.rows[self._key] += len(r)
        return r

    def fetchone(self):
        t0 = time.perf_counter()
        r = self._cur.fetchone()
        self._add(t0)
        if r is not None:
            self._n += 1
            self._acc.rows[self._key] += 1
        return r

    def fetchmany(self, size=1):
        t0 = time.perf_counter()
        r = self._cur.fetchmany(size)
        self._add(t0)
        self._n += len(r)
        self._acc.rows[self._key] += len(r)
        return r

    def __iter__(self):
        t0 = time.perf_counter()
        try:
            for row in self._cur:
                self._n += 1
                self._acc.rows[self._key] += 1
                yield row
        finally:
            self._add(t0)

    def __getattr__(self, name):
        return getattr(self._cur, name)


class Conn:
    def __init__(self, conn, acc: Acc) -> None:
        object.__setattr__(self, '_conn', conn)
        object.__setattr__(self, '_acc', acc)

    def execute(self, sql, params=()):
        key = shape(sql)
        self._acc.calls[key] += 1
        if key.startswith(('COMMIT', 'BEGIN', 'ROLLBACK', 'END')):
            t0 = time.perf_counter()
            cur = self._conn.execute(sql, params)
            self._acc.commit_ms += (time.perf_counter() - t0) * 1000
            self._acc.commit_n += 1
            return cur
        t0 = time.perf_counter()
        cur = self._conn.execute(sql, params)
        dt = (time.perf_counter() - t0) * 1000
        self._acc.ms[key] += dt
        if sql.lstrip()[:6].upper() in ('INSERT', 'UPDATE', 'DELETE', 'CREATE', 'ALTER ', 'DROP T', 'VACUUM', 'PRAGMA'):
            self._acc.dml_ms[key] += dt
        return Cur(cur, key, self._acc)

    def executemany(self, sql, seq):
        key = shape(sql)
        self._acc.calls[key] += 1
        t0 = time.perf_counter()
        cur = self._conn.executemany(sql, seq)
        self._acc.ms[key] += (time.perf_counter() - t0) * 1000
        return cur

    def executescript(self, sql):
        t0 = time.perf_counter()
        cur = self._conn.executescript(sql)
        self._acc.other_ms += (time.perf_counter() - t0) * 1000
        self._acc.other_n += 1
        return cur

    def commit(self):
        t0 = time.perf_counter()
        self._conn.commit()
        self._acc.commit_ms += (time.perf_counter() - t0) * 1000
        self._acc.commit_n += 1

    def rollback(self):
        t0 = time.perf_counter()
        self._conn.rollback()
        self._acc.commit_ms += (time.perf_counter() - t0) * 1000
        self._acc.commit_n += 1

    def cursor(self):
        return Cur(self._conn.cursor(), '<cursor>', self._acc)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *a):
        t0 = time.perf_counter()
        r = self._conn.__exit__(*a)
        self._acc.commit_ms += (time.perf_counter() - t0) * 1000
        return r

    def set_trace_callback(self, cb):
        return self._conn.set_trace_callback(cb)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)


async def one_round(root: Path) -> tuple[Acc, float, int]:
    acc = Acc()
    rt = await build_runtime(load_config(root), llm=FakeLLM(['收到。']))
    world, store = rt.world, rt.store
    inst = store.instance_list()[0]['id']
    tl = store.timeline_list(inst)[0]['id']
    world.activate(inst, tl, now_real=time.time())
    store._local.conn = Conn(store._conn, acc)
    clock = store.clock_get(tl)
    base_real, base_world = float(clock['base_real']), int(clock['base_world'])
    processed = int(clock['processed_world'])
    start = time.perf_counter()
    res = world.advance(inst, tl, now_real=base_real + (processed + DAYS * DAY - base_world) + 0.5,
                        max_batches=DAYS)
    spent = time.perf_counter() - start
    batches = max(1, res.get('batches') or 1)
    await rt.service.shutdown()
    rt.store.close()
    return acc, spent * 1000, batches


async def main() -> None:
    db = ROOT / 'data' / 'isekai.db'
    rows = {}
    conn = sqlite3.connect(db)
    for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"):
        try:
            rows[t] = conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except Exception:
            rows[t] = -1
    conn.close()
    print(f'### {LABEL}  {db}  {db.stat().st_size/1024:.0f} KB  days={DAYS}')

    ms_list = []
    total = Acc()
    for _ in range(3):
        acc, ms, batches = await one_round(ROOT)
        ms_list.append(ms / batches)
        total.calls.update(acc.calls)
        total.ms.update(acc.ms)
        total.rows.update(acc.rows)
        total.dml_ms.update(acc.dml_ms)
        total.commit_ms += acc.commit_ms
        total.commit_n += acc.commit_n
        total.other_ms += acc.other_ms
        total.other_n += acc.other_n
    nb = 3 * DAYS
    print(f'   整批墙钟 ms/日(3 轮): {[round(v,2) for v in ms_list]}  中位数 {statistics.median(ms_list):.2f}  批数/轮={DAY}')
    print(f'   语句 execute 次数合计 {sum(total.calls.values())} ⇒ {sum(total.calls.values())/nb:.1f} 条/批')
    print(f'   execute+fetch 计时合计 {sum(total.ms.values()) + total.commit_ms:.2f} ms ⇒ '
          f'{(sum(total.ms.values()) + total.commit_ms)/nb:.2f} ms/日（墙钟 {statistics.median(ms_list):.2f}）')
    print(f'   COMMIT/BEGIN/ROLLBACK 类：{total.commit_n} 次 {total.commit_ms:.2f} ms ⇒ {total.commit_ms/nb:.2f} ms/日')

    by_t_ms: collections.Counter = collections.Counter()
    by_t_calls: collections.Counter = collections.Counter()
    by_t_rows: collections.Counter = collections.Counter()
    for k, v in total.ms.items():
        by_t_ms[table_of(k)] += v
    for k, v in total.calls.items():
        by_t_calls[table_of(k)] += v
    for k, v in total.rows.items():
        by_t_rows[table_of(k)] += v
    print(f'\n   {"表":<20}{"条/批":>8}{"行/批":>8}{"ms/日":>8}{"ms/条":>8}{"ms/行":>8}{"表内行数":>9}')
    for t, c in by_t_calls.most_common():
        n = c / nb
        m = by_t_ms[t] / nb
        rr = by_t_rows[t] / nb
        print(f'   {t:<20}{n:>8.1f}{rr:>8.1f}{m:>8.3f}{(by_t_ms[t]/c if c else 0):>8.4f}'
              f'{(by_t_ms[t]/by_t_rows[t] if by_t_rows[t] else 0):>8.4f}{rows.get(t, -1):>9}')

    print('\n   最贵语句形状（真实 execute+fetch 计时，前 15）')
    for k, v in total.ms.most_common(15):
        print(f'     {v/nb:>7.3f} ms/日 x{total.calls[k]/nb:<6.1f} rows/次 {total.rows[k]/total.calls[k]:>6.1f}'
              f'  {k[:96]}')

    print('\n   单条最贵（真实计时，前 12）')
    for k, v in sorted(total.ms.items(), key=lambda kv: -kv[1] / max(1, total.calls[kv[0]]))[:12]:
        print(f'     {v/total.calls[k]:>7.4f} ms/条  x{total.calls[k]/nb:<6.1f}/批  ms/日 {v/nb:>6.3f}'
              f'  {k[:90]}')

    out = ROOT.parent / f'{LABEL}_shapes.json'
    out.write_text(json.dumps({
        'label': LABEL, 'ms_med': statistics.median(ms_list), 'batches': nb,
        'shapes': {k: {'calls': total.calls[k], 'ms': total.ms[k], 'rows': total.rows[k]} for k in total.calls},
        'commit_ms': total.commit_ms, 'commit_n': total.commit_n,
    }, ensure_ascii=False, indent=1), encoding='utf-8')
    print(f'\n   明细已写入 {out}')


if __name__ == '__main__':
    asyncio.run(main())
