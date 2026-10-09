"""测量 `reaction … stage IN ('active','fading') AND source_ref IN (…)` 加索引 `(timeline_id, source_ref)` 的收益。

现状索引 `ix_reaction_timeline_stage` 是 `(timeline_id, stage, started_world)` ⇒ 能定位到 stage，
但 `source_ref` 只能逐行过滤；而该线 `active` 阶段有 3,400+ 行、查询**返回 0 行** ⇒ 纯浪费。
"""

from __future__ import annotations

import shutil
import sqlite3
import statistics
import sys
import time
from pathlib import Path

SRC = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path('.hermes/ab/data/isekai.db').resolve()
WORK = Path('.hermes/s3l_reaction_idx').resolve()

if WORK.exists():
    shutil.rmtree(WORK)
WORK.mkdir(parents=True)
before = WORK / 'before.db'
after = WORK / 'after.db'
shutil.copy2(SRC, before)
shutil.copy2(SRC, after)

conn = sqlite3.connect(after)
conn.execute('CREATE INDEX IF NOT EXISTS ix_reaction_source_ref ON reaction(timeline_id, source_ref)')
conn.commit()
conn.close()

conn = sqlite3.connect(before)
row = conn.execute("SELECT timeline_id FROM reaction WHERE stage='active' LIMIT 1").fetchone()
tl = row[0]
refs = [r[0] for r in conn.execute(
    "SELECT source_ref FROM reaction WHERE stage IN ('active','fading') LIMIT 6")]
if not refs:
    refs = [r[0] for r in conn.execute("SELECT source_ref FROM reaction LIMIT 6")]
marks = ','.join('?' for _ in refs)
sql = (f"SELECT * FROM reaction WHERE timeline_id=? AND stage IN ('active','fading')"
       f" AND source_ref IN ({marks})")
args = [tl, *refs]
print('== 语句 ==')
print(' ', sql)
print(' 参数 source_ref =', refs)

for label, db in (('现状（无新索引）', before), ('加 ix_reaction_source_ref', after)):
    c = sqlite3.connect(db)
    print(f'\n-- {label}')
    print('   active+fading 行数:',
          c.execute("SELECT COUNT(*) FROM reaction WHERE stage IN ('active','fading')").fetchone()[0])
    print('   返回行数:', len(c.execute(sql, args).fetchall()))
    for r in c.execute('EXPLAIN QUERY PLAN ' + sql, args):
        print('   PLAN:', r[3])
    c.close()

print('\n== 单条耗时（同一进程内交替 7 轮 × 100 次）==')
def bench(db: Path, rounds: int = 100) -> float:
    c = sqlite3.connect(db)
    c.execute('PRAGMA cache_size=-65536')
    c.execute('PRAGMA mmap_size=268435456')
    t = time.perf_counter()
    for _ in range(rounds):
        c.execute(sql, args).fetchall()
    cost = (time.perf_counter() - t) * 1000 / rounds
    c.close()
    return cost

b: list[float] = []
a: list[float] = []
for i in range(7):
    if i % 2 == 0:
        a.append(bench(after)); b.append(bench(before))
    else:
        b.append(bench(before)); a.append(bench(after))
print(f'  现状      ms/条 {[round(v,4) for v in b]} 中位 {statistics.median(b):.4f}')
print(f'  加索引后  ms/条 {[round(v,4) for v in a]} 中位 {statistics.median(a):.4f}')
print(f'  ⇒ 加速 {statistics.median(b)/statistics.median(a):.1f}×')
