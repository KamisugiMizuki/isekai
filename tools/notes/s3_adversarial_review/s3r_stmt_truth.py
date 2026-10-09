"""审查用：单条语句成本的「真值」对照——Lead 的区间口径 vs 代理计时。"""
from __future__ import annotations

import pathlib
import sqlite3
import statistics
import sys
import time

src = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.hermes/ab')
c = sqlite3.connect(src / 'data' / 'isekai.db')
c.execute('PRAGMA cache_size=-65536')
c.execute('PRAGMA mmap_size=268435456')
inst = c.execute('SELECT * FROM instance LIMIT 1').fetchone()
iid = inst[0]
tls = [r[0] for r in c.execute('SELECT timeline_id FROM timeline_clock')]
per = []
for _ in range(20):
    t0 = time.perf_counter()
    for _ in range(50):
        c.execute('SELECT * FROM instance WHERE id=?', (iid,)).fetchall()
    per.append((time.perf_counter() - t0) * 1000 / 50)
print(f'SELECT * FROM instance WHERE id=?  真值 {statistics.median(per)*1000:.1f} µs/条'
      f'（Lead 区间口径记 0.075 ms = 75 µs/条）')
per = []
for _ in range(20):
    t0 = time.perf_counter()
    for _ in range(50):
        c.execute('SELECT * FROM timeline WHERE id=?', (tls[0],)).fetchall()
    per.append((time.perf_counter() - t0) * 1000 / 50)
print(f'SELECT * FROM timeline WHERE id=?  真值 {statistics.median(per)*1000:.1f} µs/条')
c.close()
print('python', sys.version.split()[0])
