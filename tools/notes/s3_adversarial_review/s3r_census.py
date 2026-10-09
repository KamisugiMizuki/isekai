"""审查用：ab / acceptH 的表行数、日历日长、时钟水位。"""
import json
import pathlib
import sqlite3
import sys

for root in sys.argv[1:] or ['.hermes/ab', '.hermes/acceptH']:
    p = pathlib.Path(root) / 'data' / 'isekai.db'
    print('==', root, 'db', round(p.stat().st_size / 1024), 'KB', 'dir',
          round(sum(f.stat().st_size for f in pathlib.Path(root).rglob('*') if f.is_file()) / 1024), 'KB')
    c = sqlite3.connect(p)
    c.row_factory = sqlite3.Row
    tabs = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    tot = 0
    for t in tabs:
        try:
            n = c.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except Exception as e:
            n = -1
        tot += n
        if n:
            print(f'   {t:<30}{n:>8}')
    print('   TOTAL rows', tot)
    for name in ('instance', 'instance_setting', 'world_setting', 'setting'):
        if name in tabs:
            row = c.execute(f'SELECT * FROM "{name}" LIMIT 1').fetchone()
            if row:
                print(f'   [{name}] cols', list(row.keys()))
                d = dict(row)
                for k, v in d.items():
                    s = str(v)
                    if 'calendar' in s or 'day_seconds' in s or k in ('world_package', 'package'):
                        print(f'      {k} = {s[:400]}')
    tl = c.execute('SELECT * FROM timeline').fetchall()
    for r in tl:
        print('   timeline:', dict(r))
    cl = c.execute('SELECT * FROM timeline_clock').fetchall()
    for r in cl:
        print('   clock:', dict(r))
    c.close()
