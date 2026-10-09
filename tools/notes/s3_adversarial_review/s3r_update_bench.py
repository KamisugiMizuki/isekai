"""审查用：UPDATE effect_state 的真实查询计划 + 微基准（不经过 runtime）。"""
from __future__ import annotations

import pathlib
import re
import sqlite3
import sys
import time

import shutil

src_root = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.hermes/ab')
# 只在副本上做写操作，绝不改被测实例
root = src_root.parent / 's3r_micro'
if not (root / 'data' / 'isekai.db').exists():
    root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src_root / 'data', root / 'data', dirs_exist_ok=True)
db = root / 'data' / 'isekai.db'
print('micro bench on copy:', root)
c = sqlite3.connect(db)
c.row_factory = sqlite3.Row

print('== effect_state schema ==')
for r in c.execute("SELECT sql FROM sqlite_master WHERE name='effect_state' AND type='table'"):
    print(r[0])
for r in c.execute("SELECT name, sql FROM sqlite_master WHERE tbl_name='effect_state'"):
    print(' ', r['name'], '|', r['sql'])
print(' table_info:', [(r[1], r[2], r[5]) for r in c.execute('PRAGMA table_info(effect_state)')])

inst = c.execute('SELECT id FROM instance LIMIT 1').fetchone()[0]
tl = c.execute('SELECT id FROM timeline LIMIT 1').fetchone()[0]
ids = [r[0] for r in c.execute(
    'SELECT id FROM effect_state WHERE timeline_id=? AND active=1 LIMIT 40', (tl,))]
print(f'\n取 {len(ids)} 条 active=1 的 id 做基准')

UPDATE = ("UPDATE effect_state SET active=0, cleared_at=? "
          "WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)")
print('\n== EXPLAIN QUERY PLAN（instance_id_ = None，即真实调用）==')
for r in c.execute('EXPLAIN QUERY PLAN ' + UPDATE, (12345, ids[0], tl, None, None)):
    print('  ', tuple(r))
print('== EXPLAIN QUERY PLAN（instance_id_ 非 None）==')
for r in c.execute('EXPLAIN QUERY PLAN ' + UPDATE, (12345, ids[0], tl, inst, inst)):
    print('  ', tuple(r))

print('\n== 微基准：每条 UPDATE 单独计时（一条一个事务会太慢，故分两种）==')
# (a) 全部放在一个事务里（最接近真实批事务）
t0 = time.perf_counter()
for i, eid in enumerate(ids):
    c.execute(UPDATE, (12345 + i, eid, tl, None, None))
t_all = (time.perf_counter() - t0) * 1000
c.rollback()
print(f'  40 条 UPDATE 同一事务（未提交）：{t_all:.2f} ms ⇒ {t_all/len(ids):.3f} ms/条（回滚）')

# (b) 每条单独提交（会含 fsync，不是热路径口径，仅供参考）
c2 = sqlite3.connect(db)
c2.execute('PRAGMA journal_mode=WAL')
t0 = time.perf_counter()
for i, eid in enumerate(ids[:15]):
    c2.execute(UPDATE, (22345 + i, eid, tl, None, None))
    c2.commit()
t_one = (time.perf_counter() - t0) * 1000
print(f'  15 条 UPDATE 各自提交：{t_one:.2f} ms ⇒ {t_one/15:.3f} ms/条')
c2.close()

# (c) 只读的等价 SELECT：按 id 定位一行需要多久
sel = 'SELECT id, active FROM effect_state WHERE id=? AND timeline_id=? AND active=1'
t0 = time.perf_counter()
for eid in ids:
    c.execute(sel, (eid, tl)).fetchall()
t_sel = (time.perf_counter() - t0) * 1000
print(f'  按 id 读 40 行的等价 SELECT：{t_sel:.2f} ms ⇒ {t_sel/len(ids):.3f} ms/条')

# (d) 按主键前缀 (instance_id, timeline_id, id) 定位
sel2 = 'SELECT id FROM effect_state WHERE instance_id=? AND timeline_id=? AND id=?'
for r in c.execute('EXPLAIN QUERY PLAN ' + sel2, (inst, tl, ids[0])):
    print('  plan(前缀完整):', tuple(r))
t0 = time.perf_counter()
for eid in ids:
    c.execute(sel2, (inst, tl, eid)).fetchall()
print(f'  前缀完整的 SELECT：{(time.perf_counter()-t0)*1000:.2f} ms ⇒ {(time.perf_counter()-t0)*1000/len(ids):.3f} ms/条')

# (e) 各表页数（page_count / 每表通过 dbstat 不可用则用 drop 后的差）
print('\n== 页/大小信息 ==')
print('  page_size', c.execute('PRAGMA page_size').fetchone()[0],
      'page_count', c.execute('PRAGMA page_count').fetchone()[0],
      'freelist', c.execute('PRAGMA freelist_count').fetchone()[0])
print('  journal_mode', c.execute('PRAGMA journal_mode').fetchone()[0])
try:
    n = c.execute("SELECT SUM(pgsize) FROM dbstat WHERE name='effect_state'").fetchone()[0]
    print('  dbstat effect_state bytes', n)
except Exception as e:
    print('  dbstat 不可用:', e)
c.close()
