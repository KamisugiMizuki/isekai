"""审查用：`UPDATE effect_state ... WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)`
的真实计划是 SCAN effect_state（全表扫描）。这里测「扫描成本 vs 行数」的线性关系——
这是 S-3「把行移出热表 ⇒ 每批更便宜」最直接的可证伪/可证实的确定性观测。

做法：在 ab 的副本上构造不同 effect_state 行数的变体（只删 active=0 的行 = 推进路径永不读取的行，
不改变世界演化），对每条变体重复 N 次「同一批真实 UPDATE」计时，取中位数。

用法：python .hermes/s3r_scan_scaling.py [src_root] [reps]
"""
from __future__ import annotations

import pathlib
import shutil
import sqlite3
import statistics
import sys
import time

src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.hermes/ab')
REPS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
work = src.parent / 's3r_scaling'
UPDATE = ("UPDATE effect_state SET active=0, cleared_at=? "
          "WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)")
SELECT_IDS = "SELECT id FROM effect_state WHERE timeline_id=? AND active=1 ORDER BY id LIMIT 60"


def build(name: str, delete_sql: str | None) -> pathlib.Path:
    root = work / name
    if root.exists():
        shutil.rmtree(root)
    (root / 'data').mkdir(parents=True)
    db = root / 'data' / 'isekai.db'
    shutil.copy2(src / 'data' / 'isekai.db', db)
    if delete_sql:
        c = sqlite3.connect(db)
        c.execute('PRAGMA journal_mode=DELETE')
        c.execute(delete_sql)
        c.commit()
        c.execute('VACUUM')
        c.close()
    return db


VARIANTS = (
    ('full', None),
    ('no_dead', 'DELETE FROM effect_state WHERE active=0'),                      # 只移出「推进路径永不读」的行
    ('half', 'DELETE FROM effect_state WHERE active=0 OR (id IN (SELECT id FROM effect_state WHERE active=0))'),  # 占位（见下）
    ('tiny', 'DELETE FROM effect_state WHERE id NOT IN (SELECT id FROM effect_state WHERE active=1 LIMIT 200)'),
)

print('== 变体 ==')
built: list[tuple[str, pathlib.Path, int, int, int]] = []
for name, sql in VARIANTS:
    if name == 'half':
        # 保留最近 50% 的 active=1 行 + 全部 active=0 删除
        sql = ('DELETE FROM effect_state WHERE active=0 OR id NOT IN '
               '(SELECT id FROM effect_state WHERE active=1 ORDER BY from_world DESC, seq, id LIMIT 2200)')
    db = build(name, sql)
    c = sqlite3.connect(db)
    n_all = c.execute('SELECT COUNT(*) FROM effect_state').fetchone()[0]
    n_act = c.execute('SELECT COUNT(*) FROM effect_state WHERE active=1').fetchone()[0]
    c.close()
    built.append((name, db, n_all, n_act, db.stat().st_size))
    print(f'  {name:<9} rows={n_all:<6} active={n_act:<6} db={db.stat().st_size/1024:.0f} KB')

print(f'\n== 同一语句的真实耗时（每变体 {REPS} 次，取中位数）==')
print(f'  {"变体":<9}{"effect_state 行":>14}{"库 KB":>9}{"ms/条":>9}{"相对 full":>11}')
base = None
for name, db, n_all, n_act, size in built:
    c = sqlite3.connect(db)
    c.execute('PRAGMA cache_size=-65536')
    c.execute('PRAGMA mmap_size=268435456')
    tl = c.execute('SELECT id FROM timeline LIMIT 1').fetchone()[0]
    ids = [r[0] for r in c.execute(SELECT_IDS, (tl,))]
    per: list[float] = []
    for _ in range(REPS):
        c.execute('BEGIN')
        t0 = time.perf_counter()
        for i, eid in enumerate(ids):
            c.execute(UPDATE, (12345 + i, eid, tl, None, None))
        per.append((time.perf_counter() - t0) * 1000 / len(ids))
        c.rollback()
    med = statistics.median(per)
    base = base if base is not None else med
    print(f'  {name:<9}{n_all:>14}{size/1024:>9.0f}{med:>9.4f}{med/base:>11.2f}')
    c.close()

print('\n== 结论用：full 的一次 UPDATE = 全表扫描；把 active=0 行移出后成本应随行数下降 ==')
