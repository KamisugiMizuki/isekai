"""审查用：**真正的 S-3 模型**——把死行移进同库的归档表（文件几乎不变大也不变小），
热表变小。若这样也更快，就证明收益来自「热表被扫描的行变少」，与「文件大小」无关。

三臂：
  A 原库                （effect_state 8,8xx 行）
  B 删除死行 + VACUUM    （文件 -15%，热表 -50%）
  C 移入归档表（同库）+ 不 VACUUM（**文件大小基本不变**，热表 -50%）← S-3 设计书原样

公平性：三臂的推进语句次数、返回行数、世界状态（active=1 的 effect_state 及其它表）都必须一致。

用法：python .hermes/s3r_s3_model_probe.py [src_root] [days] [rounds]
"""
from __future__ import annotations

import asyncio
import collections
import hashlib
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
BASE = src.parent

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402

TABLES = ('reaction', 'life_plan', 'claim', 'knowledge', 'event', 'experience', 'unit', 'intent',
          'environment_state', 'institution_state', 'custom_state')


class Acc:
    def __init__(self):
        self.calls = collections.Counter()
        self.rows = collections.Counter()
        self.ms = collections.Counter()


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
            for r in self._cur:
                self._acc.rows[self._key] += 1
                yield r
        finally:
            self._t(t0)

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
        t0 = time.perf_counter()
        cur = object.__getattribute__(self, '_c').execute(sql, params)
        a.ms[key] += (time.perf_counter() - t0) * 1000
        return Cur(cur, key, a)

    def __enter__(self):
        object.__getattribute__(self, '_c').__enter__(); return self

    def __exit__(self, *x):
        return object.__getattribute__(self, '_c').__exit__(*x)

    def set_trace_callback(self, cb):
        return object.__getattribute__(self, '_c').set_trace_callback(cb)

    def __getattr__(self, name):
        return getattr(object.__getattribute__(self, '_c'), name)


async def advance(root: pathlib.Path) -> tuple[Acc, float, int]:
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
    b = max(1, res.get('batches') or 1)
    await rt.service.shutdown()
    rt.store.close()
    return acc, spent, b


def make(name: str, mode: str) -> pathlib.Path:
    root = BASE / name
    if root.exists():
        shutil.rmtree(root)
    (root / 'data').mkdir(parents=True)
    db = root / 'data' / 'isekai.db'
    shutil.copy2(src / 'data' / 'isekai.db', db)
    if mode == 'none':
        return root
    c = sqlite3.connect(db)
    c.execute('PRAGMA journal_mode=DELETE')
    if mode == 'delete':
        c.execute('DELETE FROM effect_state WHERE active=0')
    else:
        c.execute('CREATE TABLE effect_state_archive AS SELECT * FROM effect_state WHERE active=0')
        c.execute('DELETE FROM effect_state WHERE active=0')
    c.commit()
    if mode == 'delete':
        c.execute('VACUUM')
    # 消除「journal_mode 不同」这个混淆项：所有臂都回到 WAL（与 ab 一致）
    c.execute('PRAGMA journal_mode=WAL')
    c.close()
    return root


def digest(root: pathlib.Path) -> tuple[dict, dict]:
    c = sqlite3.connect(root / 'data' / 'isekai.db')
    out = {}
    for t in TABLES:
        rows = c.execute(f'SELECT * FROM "{t}" ORDER BY 1, 2').fetchall()
        out[t] = hashlib.sha256(repr(rows).encode()).hexdigest()[:16]
    eff = c.execute('SELECT * FROM effect_state WHERE active=1 ORDER BY 1, 2').fetchall()
    out['effect_active_rows'] = (len(eff), hashlib.sha256(repr(eff).encode()).hexdigest()[:16])
    c.close()
    return out, {}


async def main() -> None:
    A = make('s3r_model_A_full', 'none')
    B = make('s3r_model_B_del', 'delete')
    C = make('s3r_model_C_archive', 'archive')
    for label, root in (('A 原库', A), ('B 删死行+VACUUM', B), ('C 移入同库归档表(不 VACUUM)', C)):
        db = root / 'data' / 'isekai.db'
        c = sqlite3.connect(db)
        n = c.execute('SELECT COUNT(*) FROM effect_state').fetchone()[0]
        c.close()
        print(f'  {label:<28} effect_state {n:>5} 行   文件 {db.stat().st_size/1024:>7.0f} KB')

    print('\n== 公平性：各推进 1 轮，比较语句次数/返回行数/状态 ==')
    res = {}
    for label, root in (('A', A), ('B', B), ('C', C)):
        acc, _, b = await advance(root)
        res[label] = acc
        print(f'  {label}: 语句 {sum(acc.calls.values())} 条，返回 {sum(acc.rows.values())} 行')
    keys = set(res['A'].calls) | set(res['B'].calls) | set(res['C'].calls)
    dc = [(k, res['A'].calls[k], res['B'].calls[k], res['C'].calls[k]) for k in keys
          if len({res[x].calls[k] for x in 'ABC'}) > 1]
    dr = [(k, res['A'].rows[k], res['B'].rows[k], res['C'].rows[k]) for k in keys
          if len({res[x].rows[k] for x in 'ABC'}) > 1]
    print(f'  调用次数不同的语句 {len(dc)} 种；返回行数不同的 {len(dr)} 种')
    for k, a, b, c in dc[:6]:
        print(f'    calls A={a} B={b} C={c}  {k[:90]}')
    for k, a, b, c in dr[:6]:
        print(f'    rows  A={a} B={b} C={c}  {k[:90]}')
    da, _ = digest(A); db_, _ = digest(B); dc_, _ = digest(C)
    print(f'  状态摘要 A==B: {da == db_}   A==C: {da == dc_}（effect_active_rows 应相同）')

    print(f'\n== 交替 {ROUNDS} 轮 × {DAYS} 天 ==')
    ms = {'A': [], 'B': [], 'C': []}
    upd = {'A': [], 'B': [], 'C': []}
    for i in range(ROUNDS):
        order = ['A', 'B', 'C'] if i % 2 == 0 else ['C', 'B', 'A']
        for tag in order:
            root = {'A': A, 'B': B, 'C': C}[tag]
            acc, spent, b = await advance(root)
            ms[tag].append(spent / b)
            for k, v in acc.ms.items():
                if k.startswith('UPDATE effect_state'):
                    upd[tag].append(v / acc.calls[k])
    for tag, label in (('A', 'A 原库'), ('B', 'B 删死行+VACUUM'), ('C', 'C 移入同库归档表')):
        print(f'  {label:<24} ms/日 {[round(v,2) for v in ms[tag]]} 中位数 {statistics.median(ms[tag]):.2f}')
    print(f'  ⇒ B/A = {statistics.median(ms["B"])/statistics.median(ms["A"]):.2f}×   '
          f'C/A = {statistics.median(ms["C"])/statistics.median(ms["A"]):.2f}×   '
          f'（<1 更快）')
    print(f'  成对 C/A: {[round(c/a,2) for c, a in zip(ms["C"], ms["A"])]}')
    print(f'\n  UPDATE effect_state 单条耗时中位数：'
          f'A {statistics.median(upd["A"]):.4f}  B {statistics.median(upd["B"]):.4f}  '
          f'C {statistics.median(upd["C"]):.4f} ms')

    print('\n== 结尾文件大小 ==')
    for tag, label in (('A', 'A 原库'), ('B', 'B 删死行+VACUUM'), ('C', 'C 移入同库归档表')):
        root = {'A': A, 'B': B, 'C': C}[tag]
        db = root / 'data' / 'isekai.db'
        print(f'  {label:<24} {db.stat().st_size/1024:>7.0f} KB')


if __name__ == '__main__':
    asyncio.run(main())
