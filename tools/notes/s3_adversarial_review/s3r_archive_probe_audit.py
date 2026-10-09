"""审查用：s3_archive_probe.py 到底删掉了多少「活行」（会被推进路径读到的行）？

PRUNE 的切割点：cut = low + (high-low)*ratio（ratio 见 s3_archive_probe.py:28-37），
删除 `< cut` 的行。这里对每张表算出：删掉多少行、其中多少是「活行」。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys

root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.hermes/ab')
c = sqlite3.connect(root / 'data' / 'isekai.db')
PRUNE = (
    ("knowledge", "world_seconds", 0.6),
    ("claim", "earliest_world", 0.6),
    ("event", "world_seconds", 0.6),
    ("experience", "world_seconds", 0.6),
    ("reaction", "started_world", 0.6),
    ("life_plan", "day_index", 0.6),
    ("effect_state", "from_world", 0.6),
    ("unit", "updated_world", 0.4),
)
cur_day = c.execute('SELECT day_index FROM life_plan ORDER BY day_index DESC LIMIT 1').fetchone()[0]
print(f'当前 life_plan 最新 day_index = {cur_day}；effect_state 活跃行 = active=1')
print(f'{"表":<14}{"总行":>7}{"删":>7}{"其中活行":>10}  活行定义')
for t, col, ratio in PRUNE:
    n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    lo, hi = c.execute(f'SELECT MIN({col}), MAX({col}) FROM "{t}"').fetchone()
    if lo is None:
        continue
    cut = int(lo + (hi - lo) * ratio)
    d = c.execute(f'SELECT COUNT(*) FROM "{t}" WHERE {col} < ?', (cut,)).fetchone()[0]
    if t == 'effect_state':
        live = c.execute('SELECT COUNT(*) FROM effect_state WHERE from_world < ? AND active=1', (cut,)).fetchone()[0]
        note = 'active=1（推进路径唯一会读的行）'
    elif t == 'reaction':
        live = c.execute("SELECT COUNT(*) FROM reaction WHERE started_world < ? AND stage IN ('active','fading')",
                         (cut,)).fetchone()[0]
        note = "stage IN ('active','fading')：批内 1.0 次/批的取数会**全部**取回"
    elif t == 'life_plan':
        live = c.execute('SELECT COUNT(*) FROM life_plan WHERE day_index < ? AND day_index >= ?',
                         (cut, cur_day - 5)).fetchone()[0]
        note = f'day_index 落在近 5 天窗口内（{cur_day-5}..{cur_day}）'
    elif t == 'unit':
        live = c.execute('SELECT COUNT(*) FROM unit WHERE updated_world < ?', (cut,)).fetchone()[0]
        note = 'unit 每批读 2 次（4 行全是活行）'
    elif t == 'claim':
        live = c.execute('SELECT COUNT(*) FROM claim WHERE earliest_world < ? AND earliest_world > ?',
                         (cut, cut - 86400 * 400)).fetchone()[0]
        note = '落在「读窗口」附近（近似，实际窗口按批）'
    else:
        live = 0
        note = '热路径不读（推进期间窗口查询结果不受影响）'
    print(f'{t:<14}{n:>7}{d:>7}{live:>10}  {note}')

print('\n== reaction stage 分布（全部 active ⇒ 删任何一行都减少批内取数的返回行）==')
for r in c.execute('SELECT stage, COUNT(*) FROM reaction GROUP BY stage'):
    print('  ', r)
print('\n== effect_state 的 active=1 行的 from_world 分布（前 4 分位）==')
rows = [r[0] for r in c.execute('SELECT from_world FROM effect_state WHERE active=1 ORDER BY from_world')]
if rows:
    for q in (0, 0.25, 0.5, 0.75, 1.0):
        i = min(len(rows) - 1, int(q * (len(rows) - 1)))
        print(f'   q{q:.2f} = {rows[i]}')
lo, hi = c.execute('SELECT MIN(from_world), MAX(from_world) FROM effect_state').fetchone()
cut = int(lo + (hi - lo) * 0.6)
print(f'  archive_probe 的切割点 cut={cut}；活跃行中 < cut 的 = '
      f'{c.execute("SELECT COUNT(*) FROM effect_state WHERE active=1 AND from_world < ?", (cut,)).fetchone()[0]} / {len(rows)}')
c.close()
