"""公平实验（审查方）：把**推进路径永不读取**的行移出热表，验证「同样的工作量，更少的行」是否更快。

为什么要这样切：
- `effect_state` 的**全部**推进路径读取都带 `active=1`（store.py:5296/5561/5575/5640/5651，
  以及 e服务端 effect_constraints 5611-5620）⇒ `active=0` 的 4,3xx 行在推进期间**一次都不会被读到**。
- 那条最贵的语句 `UPDATE effect_state ... WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)`
  的查询计划是 **SCAN effect_state**（全表扫描）⇒ 它的成本正比于**表内行数**（包括永不读取的 active=0 行）。
  这正是「归档**能**降低每批成本」的可证伪形式。

公平性证明（本脚本强制输出）：
1. 逐语句**调用次数**两边相同；
2. 逐语句**返回行数**两边相同（⇒ 移出的行确实不被读）；
3. 推进结束后**世界状态摘要**相同（⇒ 演化轨迹一致，删的不是「工作」而是「死重量」）。

用法：python .hermes/s3r_fair_probe.py [src_root] [days] [rounds]
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
import json
import pathlib
import shutil
import sqlite3
import statistics
import sys
import time

src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.hermes/ab')
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 5
DAY = 86400
FULL = src.parent / 's3r_full'
DEAD = src.parent / 's3r_dead'

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402

DIGEST_TABLES = ('effect_state', 'reaction', 'life_plan', 'claim', 'knowledge', 'event',
                 'experience', 'unit', 'intent', 'memory')


def clone(dst: pathlib.Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    (dst / 'data').mkdir(parents=True)
    shutil.copy2(src / 'data' / 'isekai.db', dst / 'data' / 'isekai.db')


def archive_dead(root: pathlib.Path) -> dict[str, int]:
    """只删推进路径永不读取的行（active=0 的后果）。"""
    db = root / 'data' / 'isekai.db'
    c = sqlite3.connect(db)
    c.execute('PRAGMA journal_mode=DELETE')
    before = c.execute('SELECT COUNT(*) FROM effect_state').fetchone()[0]
    c.execute('DELETE FROM effect_state WHERE active=0')
    c.commit()
    after = c.execute('SELECT COUNT(*) FROM effect_state').fetchone()[0]
    c.execute('VACUUM')
    c.close()
    return {'before': before, 'after': after}


class Acc:
    def __init__(self) -> None:
        self.ms: collections.Counter = collections.Counter()
        self.calls: collections.Counter = collections.Counter()
        self.rows: collections.Counter = collections.Counter()
        self.txn_ms = 0.0
        self.txn_n = 0


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
        key = ' '.join(sql.split())
        acc = object.__getattribute__(self, '_acc')
        acc.calls[key] += 1
        t0 = time.perf_counter()
        cur = object.__getattribute__(self, '_conn').execute(sql, params)
        acc.ms[key] += (time.perf_counter() - t0) * 1000
        return Cur(cur, key, acc)

    def __enter__(self):
        object.__getattribute__(self, '_conn').__enter__(); return self

    def __exit__(self, *a):
        acc = object.__getattribute__(self, '_acc')
        t0 = time.perf_counter()
        r = object.__getattribute__(self, '_conn').__exit__(*a)
        acc.txn_ms += (time.perf_counter() - t0) * 1000
        acc.txn_n += 1
        return r

    def set_trace_callback(self, cb):
        return object.__getattribute__(self, '_conn').set_trace_callback(cb)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_conn'), name)


def digest(root: pathlib.Path) -> dict[str, object]:
    c = sqlite3.connect(root / 'data' / 'isekai.db')
    out: dict[str, object] = {}
    for t in DIGEST_TABLES:
        try:
            n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
            rows = c.execute(f'SELECT * FROM "{t}" ORDER BY 1, 2').fetchall()
            out[t] = [n, hashlib.sha256(repr(rows).encode()).hexdigest()[:16]]
        except Exception as e:  # noqa: BLE001
            out[t] = [f'err {e}']
    out['clock'] = c.execute('SELECT processed_world, generation FROM timeline_clock').fetchall()
    out['effect_active'] = c.execute('SELECT active, expiry, COUNT(*) FROM effect_state GROUP BY active, expiry').fetchall()
    c.close()
    return out


async def run(root: pathlib.Path, days: int) -> tuple[Acc, float, int]:
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
    res = world.advance(inst, tl, now_real=br + (processed + days * DAY - bw) + 0.5, max_batches=days)
    spent = (time.perf_counter() - t0) * 1000
    batches = max(1, res.get('batches') or 1)
    await rt.service.shutdown()
    rt.store.close()
    return acc, spent, batches


async def main() -> None:
    clone(FULL)
    clone(DEAD)
    st = archive_dead(DEAD)
    print(f'# 公平归档：只删 `effect_state WHERE active=0`（推进路径全部读取都带 active=1）')
    print(f'  effect_state {st["before"]} -> {st["after"]} 行')
    for label, root in (('原库', FULL), ('归档(死行)', DEAD)):
        db = root / 'data' / 'isekai.db'
        print(f'  {label:<12} 文件 {db.stat().st_size/1024:>7.0f} KB')

    # ---------- 1. 公平性证明：同样的语句次数、同样的返回行数、同样的世界状态 ----------
    print('\n== 公平性检查：各推进 1 轮，比较逐语句调用次数/返回行数/状态摘要 ==')
    a_full, _, b_full = await run(FULL, DAYS)
    a_dead, _, b_dead = await run(DEAD, DAYS)
    n_shape = len(set(a_full.calls) | set(a_dead.calls))
    diff_calls = {k: (a_full.calls[k], a_dead.calls[k]) for k in set(a_full.calls) | set(a_dead.calls)
                  if a_full.calls[k] != a_dead.calls[k]}
    diff_rows = {k: (a_full.rows[k], a_dead.rows[k]) for k in set(a_full.rows) | set(a_dead.rows)
                 if a_full.rows[k] != a_dead.rows[k]}
    print(f'  语句形状 {n_shape} 种；调用次数不同的 {len(diff_calls)} 种；返回行数不同的 {len(diff_rows)} 种')
    for k, v in list(diff_calls.items())[:8]:
        print(f'    calls diff {v}  {k[:100]}')
    for k, v in list(diff_rows.items())[:8]:
        print(f'    rows  diff {v}  {k[:100]}')
    d_full, d_dead = digest(FULL), digest(DEAD)
    key = next(iter(d_full))
    print(f'  状态摘要（{len(d_full)} 项）相同: {d_full == d_dead}')
    if d_full != d_dead:
        for k in d_full:
            if d_full[k] != d_dead[k]:
                print(f'    {k}: full={str(d_full[k])[:110]} dead={str(d_dead[k])[:110]}')

    # ---------- 2. 交替多轮 ----------
    print(f'\n== 交替 {ROUNDS} 轮 × {DAYS} 天（同一实例、同一时间段）==')
    full_ms: list[float] = []
    dead_ms: list[float] = []
    per_stmt: dict[str, dict[str, list[float]]] = collections.defaultdict(lambda: collections.defaultdict(list))
    calls_full: collections.Counter = collections.Counter()
    calls_dead: collections.Counter = collections.Counter()
    rows_full: collections.Counter = collections.Counter()
    rows_dead: collections.Counter = collections.Counter()
    for i in range(ROUNDS):
        order = ((DEAD, dead_ms, 'dead', calls_dead, rows_dead), (FULL, full_ms, 'full', calls_full, rows_full))
        if i % 2:
            order = tuple(reversed(order))
        for root, store_ms, tag, calls, rows in order:
            acc, ms, b = await run(root, DAYS)
            store_ms.append(ms / b)
            calls.update(acc.calls)
            rows.update(acc.rows)
            for k, v in acc.ms.items():
                if v / b >= 0.02:
                    per_stmt[k][tag].append(v / acc.calls[k])
            per_stmt['<txn>'][tag].append(acc.txn_ms / max(1, acc.txn_n))

    mf, md = statistics.median(full_ms), statistics.median(dead_ms)
    print(f'  原库      ms/日 {[round(v,2) for v in full_ms]}  中位数 {mf:.2f}  （最小 {min(full_ms):.2f}）')
    print(f'  归档(死行) ms/日 {[round(v,2) for v in dead_ms]}  中位数 {md:.2f}  （最小 {min(dead_ms):.2f}）')
    print(f'  ⇒ 归档/原库 = {md/mf:.2f}×  （按最小二乘式「成对」看：'
          f'{[round(d/f,2) for d, f in zip(dead_ms, full_ms)]}）')

    nb = ROUNDS * DAYS
    print(f'\n== 逐语句「单条真实耗时」中位数（同一交替轮次内，最可信口径）==')
    print(f'  {"原库 ms/条":>11}{"归档 ms/条":>11}{"倍":>7}{"原库 条/批":>11}{"归档 条/批":>11}  语句')
    rows_out = []
    for k, d in per_stmt.items():
        if not d['full'] or not d['dead']:
            continue
        rows_out.append((statistics.median(d['full']), statistics.median(d['dead']), k,
                         calls_full[k] / nb, calls_dead[k] / nb))
    for f, dd, k, cf, cd in sorted(rows_out, key=lambda r: -r[0] * max(r[3], 1e-9)):
        print(f'  {f:>11.4f}{dd:>11.4f}{dd/f if f else 0:>7.2f}{cf:>11.2f}{cd:>11.2f}  {k[:88]}')

    print(f'\n== 返回行数（/批）对比（公平性在真实测量窗口内是否保持）==')
    tot_f = sum(rows_full.values()) / nb
    tot_d = sum(rows_dead.values()) / nb
    print(f'  原库 {tot_f:.1f} 行/批   归档 {tot_d:.1f} 行/批   差 {tot_f - tot_d:.1f}')
    diffs = {k: (rows_full[k], rows_dead[k]) for k in set(rows_full) | set(rows_dead)
             if rows_full[k] != rows_dead[k]}
    for k, v in list(diffs.items())[:10]:
        print(f'    rows diff full={v[0]/nb:.2f}/批 dead={v[1]/nb:.2f}/批  {k[:90]}')

    json.dump({'full_ms': full_ms, 'dead_ms': dead_ms, 'median_full': mf, 'median_dead': md,
               'per_stmt': {k: {t: v for t, v in d.items()} for k, d in per_stmt.items()}},
              open(src.parent / 's3r_fair_result.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)


if __name__ == '__main__':
    asyncio.run(main())
