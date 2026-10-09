"""审查用：索引、查询计划、以及「哪些行是死的/活的」分布。"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.hermes/ab')
db = root / 'data' / 'isekai.db'
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

print('== 索引 ==')
for r in c.execute("SELECT name, tbl_name, sql FROM sqlite_master WHERE type='index' ORDER BY tbl_name, name"):
    print(f'  {r["tbl_name"]:<22}{r["name"]:<34}{(r["sql"] or "(auto)")[:150]}')

print('\n== effect_state 分布 ==')
for q, label in (
    ("SELECT active, COUNT(*) n, MIN(from_world) a, MAX(from_world) b FROM effect_state GROUP BY active", 'active'),
    ("SELECT expiry, COUNT(*) n FROM effect_state GROUP BY expiry", 'expiry'),
    ("SELECT active, expiry, COUNT(*) n FROM effect_state GROUP BY active, expiry", 'active x expiry'),
    ("SELECT COUNT(*) FROM effect_state WHERE active=1 AND target IN ('src-1')", 'active target=src-1'),
):
    print(' ', label)
    for r in c.execute(q):
        print('    ', dict(r))

print('\n== reaction 分布 ==')
for r in c.execute("SELECT stage, COUNT(*) n, MIN(started_world) a, MAX(started_world) b FROM reaction GROUP BY stage"):
    print('   ', dict(r))
print('   cols:', [r[1] for r in c.execute('PRAGMA table_info(reaction)')])

print('\n== life_plan 分布 ==')
print('   cols:', [r[1] for r in c.execute('PRAGMA table_info(life_plan)')])
for r in c.execute("SELECT COUNT(*) n, MIN(day_index) a, MAX(day_index) b FROM life_plan"):
    print('   ', dict(r))
for r in c.execute("SELECT COUNT(*) n FROM life_plan WHERE day_index < (SELECT MAX(day_index)-30 FROM life_plan)"):
    print('    day_index older than 30:', dict(r))

print('\n== claim / knowledge / event / experience ==')
for t, col in (('claim', 'earliest_world'), ('knowledge', 'world_seconds'), ('event', 'world_seconds'), ('experience', 'world_seconds')):
    cols = [r[1] for r in c.execute(f'PRAGMA table_info({t})')]
    print(f'   {t} cols={cols}')
    lo, hi = c.execute(f'SELECT MIN({col}), MAX({col}) FROM {t}').fetchone()
    n = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    print(f'      n={n} {col} in [{lo},{hi}]')
    for frac in (0.4, 0.5, 0.6, 0.7):
        cut = int(lo + (hi - lo) * frac)
        k = c.execute(f'SELECT COUNT(*) FROM {t} WHERE {col} < ?', (cut,)).fetchone()[0]
        print(f'      delete {col}<{cut} (keep newest {1-frac:.0%} of range) -> -{k} rows ({k/n:.0%})')

print('\n== 关键热语句的计划 ==')
inst = c.execute('SELECT id FROM instance LIMIT 1').fetchone()[0]
tl = c.execute('SELECT id FROM timeline LIMIT 1').fetchone()[0]
until = c.execute('SELECT processed_world FROM timeline_clock LIMIT 1').fetchone()[0]
plans = {
    'effect_constraints(方案①②)': (
        "SELECT id, event_id, target, kind, value, from_world FROM effect_state "
        "WHERE instance_id=? AND timeline_id=? AND active=1 AND from_world<=? AND target IN ('src-1','env-1') "
        "ORDER BY from_world, seq, id", (inst, tl, until)),
    'effects_due with_cause': (
        "SELECT * FROM effect_state WHERE instance_id=? AND timeline_id=? AND active=1 AND expiry='with_cause' "
        "AND from_world<=? ORDER BY from_world, seq, id", (inst, tl, until - 86400)),
    'effect_ids_active': ("SELECT id FROM effect_state WHERE instance_id=? AND timeline_id=? AND active=1", (inst, tl)),
    'effect_window(active)': ("SELECT * FROM effect_state WHERE instance_id=? AND timeline_id=? AND active=1", (inst, tl)),
    'reaction stage': ("SELECT * FROM reaction WHERE timeline_id=? AND stage='candidate'", (tl,)),
    'reaction stages in': ("SELECT * FROM reaction WHERE timeline_id=? AND stage IN ('active','fading')", (tl,)),
    'life_plan by day': ("SELECT * FROM life_plan WHERE instance_id=? AND timeline_id=? AND character_id=? AND day_index=?",
                         (inst, tl, 'char-1', 3173)),
    'knowledge by window': ("SELECT * FROM knowledge WHERE instance_id=? AND timeline_id=? AND world_seconds<=? "
                            "ORDER BY character_id, world_seconds", (inst, tl, until)),
    'claim hot?': ("SELECT * FROM claim WHERE instance_id=? AND timeline_id=? ORDER BY earliest_world", (inst, tl)),
}
for label, (sql, args) in plans.items():
    print(f'  -- {label}')
    try:
        for r in c.execute('EXPLAIN QUERY PLAN ' + sql, args):
            print('     ', tuple(r))
        t0 = __import__('time').perf_counter()
        n = len(c.execute(sql, args).fetchall())
        dt = (__import__('time').perf_counter() - t0) * 1000
        print(f'      rows={n}  first-call {dt:.3f} ms')
    except Exception as e:
        print('      ERR', e)

print('\n== claim 实际被怎么查 ==')
c.close()
