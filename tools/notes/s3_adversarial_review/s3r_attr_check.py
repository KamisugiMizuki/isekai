"""审查用：核对 Lead 的表归因正则（首个 FROM/JOIN/INTO/UPDATE/DELETE FROM）是否会把语句归错表。

对每条真实语句：
- 列出正则找到的**全部**表名；
- 首个匹配（Lead 的口径）；
- 是否含 JOIN / WITH / 子查询 / schema 限定名 / 注释里的 FROM。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

TRE = re.compile(r'\b(?:FROM|JOIN|INTO|UPDATE|DELETE\s+FROM)\s+"?([a-zA-Z_][a-zA-Z0-9_]*)"?', re.I)
d = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
nb = d['batches']
multi, suspicious = [], []
for sql, v in d['sql'].items():
    tabs = TRE.findall(sql)
    first = tabs[0].lower() if tabs else '<none>'
    if len(set(t.lower() for t in tabs)) > 1:
        multi.append((first, tabs, v, sql))
    if re.search(r'\bJOIN\b', sql, re.I) or re.search(r'^\s*WITH\b', sql, re.I) \
       or re.search(r'\bmain\.|\btemp\.|/\*|--', sql):
        suspicious.append((first, v, sql))
print(f'语句种类 {len(d["sql"])}；正则命中多于一张表的 {len(multi)}；含 JOIN/WITH/schema/注释的 {len(suspicious)}')
for first, tabs, v, sql in multi:
    print(f'\n  首表={first}  全部={tabs}  {v["calls"]/nb:.2f}/批 {v["ms"]/nb:.3f} ms/日')
    print(f'   {sql[:220]}')
for first, v, sql in suspicious:
    print(f'\n  [可疑] 首表={first}  {v["calls"]/nb:.2f}/批  {sql[:220]}')
print('\n== 归到 <none> 的语句（无 FROM/JOIN/INTO/UPDATE）==')
for sql, v in d['sql'].items():
    if not TRE.search(sql):
        print(f'  {v["calls"]/nb:.2f}/批 {v["ms"]/nb:.3f} ms/日  {sql[:90]}')
