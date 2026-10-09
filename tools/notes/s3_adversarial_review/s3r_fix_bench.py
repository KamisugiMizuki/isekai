"""审查用：把「热语句的成本来源」拆成可换的三种修法，量化每种能省多少 ms/世界日。

(1) UPDATE effect_state：现状 = SCAN effect_state（全表扫描，成本 ∝ 表行数）
    - 修法 A（补主键前缀）：WHERE instance_id=? AND timeline_id=? AND id=?
    - 修法 B（S-3 式归档）：把 active=0 的死行移走 ⇒ 扫描行数减半（不改语句）
(2) SELECT * FROM reaction ... stage IN ('active','fading') AND source_ref IN (...)：现状走
    ix_reaction_timeline_stage(timeline_id, stage) ⇒ 扫过全部 'active' 行（3,3xx）再按 source_ref 过滤，返回 0 行
    - 修法 C：加索引 (timeline_id, source_ref)
"""
from __future__ import annotations

import pathlib
import shutil
import sqlite3
import statistics
import sys
import time

src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.hermes/ab')
REPS = 7
BASE = src.parent / 's3r_fixbench'

UPD_SCAN = ("UPDATE effect_state SET active=0, cleared_at=? "
            "WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)")
UPD_PK = ("UPDATE effect_state SET active=0, cleared_at=? "
          "WHERE instance_id=? AND timeline_id=? AND id=? AND active=1")
UPD_NOPRED = "UPDATE effect_state SET active=0, cleared_at=? WHERE id=? AND timeline_id=?"

RX = ("SELECT * FROM reaction WHERE timeline_id=? AND stage IN ('active','fading') "
      "AND source_ref IN (?,?,?)")


def copy_variant(name: str, setup: list[str] | None = None) -> sqlite3.Connection:
    root = BASE / name
    if root.exists():
        shutil.rmtree(root)
    (root / 'data').mkdir(parents=True)
    db = root / 'data' / 'isekai.db'
    shutil.copy2(src / 'data' / 'isekai.db', db)
    c = sqlite3.connect(db)
    c.execute('PRAGMA journal_mode=DELETE')
    for sql in setup or []:
        c.execute(sql)
    c.commit()
    c.execute('VACUUM')
    c.execute('PRAGMA cache_size=-65536')
    c.execute('PRAGMA mmap_size=268435456')
    return c


def bench(c: sqlite3.Connection, sql: str, argsets: list[tuple], reps: int = REPS) -> float:
    per: list[float] = []
    for _ in range(reps):
        c.execute('BEGIN')
        t0 = time.perf_counter()
        for a in argsets:
            c.execute(sql, a)
        per.append((time.perf_counter() - t0) * 1000 / len(argsets))
        c.rollback()
    return statistics.median(per)


def main() -> None:
    tl, inst, until = None, None, None
    probe = sqlite3.connect(src / 'data' / 'isekai.db')
    inst = probe.execute('SELECT id FROM instance LIMIT 1').fetchone()[0]
    tl = probe.execute('SELECT id FROM timeline LIMIT 1').fetchone()[0]
    until = probe.execute('SELECT processed_world FROM timeline_clock LIMIT 1').fetchone()[0]
    ids = [r[0] for r in probe.execute(
        'SELECT id FROM effect_state WHERE timeline_id=? AND active=1 ORDER BY id LIMIT 80', (tl,))]
    probe.close()
    argsets = [(1000 + i, eid, tl, None, None) for i, eid in enumerate(ids)]
    argsets_pk = [(1000 + i, inst, tl, eid) for i, eid in enumerate(ids)]

    print(f'# 基准：每条语句 {len(ids)} 次执行 × {REPS} 轮取中位数')

    c = copy_variant('plain')
    argsets_np = [(1000 + i, eid, tl) for i, eid in enumerate(ids)]
    plans = ((UPD_SCAN, argsets[0], '现状（SCAN effect_state）'),
             (UPD_PK, argsets_pk[0], '修法 A：补主键前缀'),
             (UPD_NOPRED, argsets_np[0], '对照：只给 (id, timeline_id) 无实例谓词'))
    for sql, args, label in plans:
        for row in c.execute('EXPLAIN QUERY PLAN ' + sql, args):
            print(f'  {label:<26} PLAN: {row[3]}')
    benches = ((UPD_SCAN, argsets, '现状（SCAN effect_state）'),
               (UPD_PK, argsets_pk, '修法 A：补主键前缀'),
               (UPD_NOPRED, argsets_np, '对照：只给 (id, timeline_id) 无实例谓词'))
    for sql, args, label in benches:
        ms = bench(c, sql, args)
        print(f'  {label:<26} {ms:.4f} ms/条')
    c.close()

    c = copy_variant('no_dead', ['DELETE FROM effect_state WHERE active=0'])
    n = c.execute('SELECT COUNT(*) FROM effect_state').fetchone()[0]
    ms = bench(c, UPD_SCAN, argsets)
    print(f'  {"修法 B：S-3 归档死行（"+str(n)+" 行）":<30} {ms:.4f} ms/条')
    c.close()

    print('\n# reaction 的那三条取数')
    c = copy_variant('rx')
    a = (tl, 'x', 'y', 'z')
    for row in c.execute('EXPLAIN QUERY PLAN ' + RX, a):
        print(f'  现状 PLAN: {row[3]}')
    ms0 = bench(c, RX, [a])
    print(f'  现状                             {ms0:.4f} ms/条')
    c.close()
    c = copy_variant('rx_idx', ['CREATE INDEX ix_rx_srctmp ON reaction(timeline_id, source_ref)'])
    for row in c.execute('EXPLAIN QUERY PLAN ' + RX, a):
        print(f'  修法 C PLAN: {row[3]}')
    ms1 = bench(c, RX, [a])
    print(f'  修法 C：加 (timeline_id, source_ref) 索引 {ms1:.4f} ms/条  ⇒ {ms1/ms0:.2f}×')
    c.close()


if __name__ == '__main__':
    main()
