"""为什么 `UPDATE effect_state SET active=0 ...` 这么贵（7.26 ms/日，约 2.6 条/批）？

它出现在 `store.apply_runtime_batch`（`service.py` 的后果解除路径）。单条 7.26/2.6 ≈ **2.8 ms/条**，
这远超一次主键更新的量级 ⇒ 先看它的 WHERE 与查询计划。

判据：若计划是 **SCAN effect_state**（而非 SEARCH ... USING PRIMARY KEY），则成本 ∝ 表行数，
把死行移出热表只是**回避**问题；真正的修法是给它可用的索引 / 或者把 `instance_id` 加进 WHERE。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DB = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path('.hermes/ab/data/isekai.db').resolve()

conn = sqlite3.connect(DB)
inst = conn.execute('SELECT id FROM instance LIMIT 1').fetchone()[0]
tl = conn.execute('SELECT id FROM timeline LIMIT 1').fetchone()[0]

sql = ('UPDATE effect_state SET active=0, cleared_at=? '
       'WHERE id=? AND timeline_id=? AND active=1')
print('== 语句 ==')
print(' ', sql)

for label, s, p in (
    ('原样（id, timeline_id, active）', sql, (1, 'fx', tl)),
    ('加 instance_id（即主键全列）', sql.replace('WHERE id=?', 'WHERE instance_id=? AND id=?'), (1, inst, 'fx', tl)),
    ('只按 timeline_id（无 id 精确项）',
     'UPDATE effect_state SET active=0, cleared_at=? WHERE timeline_id=? AND active=1', (1, tl)),
):
    print(f'\n-- {label} --')
    try:
        for row in conn.execute('EXPLAIN QUERY PLAN ' + s, p):
            print('   PLAN:', row[3])
    except Exception as e:
        print('   PLAN 无法执行:', e)

print('\n== 相关索引 ==')
for row in conn.execute("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='effect_state'"):
    print('  ', row[0], '::', (row[1] or '(auto)')[:120])

print('\n== 表规模 ==')
print('   effect_state 总行数 ', conn.execute('SELECT COUNT(*) FROM effect_state').fetchone()[0])
print('   active=1         ', conn.execute('SELECT COUNT(*) FROM effect_state WHERE active=1').fetchone()[0])
print('   active=0         ', conn.execute('SELECT COUNT(*) FROM effect_state WHERE active=0').fetchone()[0])

print('\n== 实测：同一条 UPDATE 在「保留死行」与「移除死行」两张表上的单条耗时（交替 200 轮）==')
import subprocess, tempfile, shutil, time  # noqa: E402

work = Path('.hermes/s3l_update_bench').resolve()
if work.exists():
    shutil.rmtree(work)
(work / 'data').mkdir(parents=True)
shutil.copy2(DB, work / 'data' / 'isekai.db')
dead = work / 'data' / 'dead.db'
shutil.copy2(DB, dead)
c2 = sqlite3.connect(dead)
c2.execute('DELETE FROM effect_state WHERE active=0')
c2.commit()
c2.close()


def bench(path: Path, rounds: int = 200) -> float:
    c = sqlite3.connect(path)
    c.execute('PRAGMA cache_size=-65536')
    c.execute('PRAGMA mmap_size=0')
    row = c.execute('SELECT id, timeline_id FROM effect_state WHERE active=1 LIMIT 1').fetchone()
    if row is None:
        c.close()
        return 0.0
    ident, line = row
    t = time.perf_counter()
    for _ in range(rounds):
        c.execute('UPDATE effect_state SET active=0, cleared_at=1 WHERE id=? AND timeline_id=? AND active=1',
                  (ident, line))
        c.rollback()
    cost = (time.perf_counter() - t) * 1000 / rounds
    c.close()
    return cost


full_ms: list[float] = []
dead_ms: list[float] = []
for i in range(3):
    if i % 2 == 0:
        dead_ms.append(bench(dead))
        full_ms.append(bench(work / 'data' / 'isekai.db'))
    else:
        full_ms.append(bench(work / 'data' / 'isekai.db'))
        dead_ms.append(bench(dead))
print('  保留死行 ms/条:', [round(v, 3) for v in full_ms])
print('  移除死行 ms/条:', [round(v, 3) for v in dead_ms])
