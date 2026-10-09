"""S-4 步骤 1–2：索引使用盘点。

思路：**每个索引都要为每次写入付维护成本**，所以「没有任何查询使用」的索引是纯成本。
做法：① 列出库里所有用户索引；② 对**真实热查询集**（取自语句普查的实际形状）逐个跑
`EXPLAIN QUERY PLAN`；③ 报告从未被任何查询命中的索引。

用法：python .hermes/index_audit.py <root>
"""

from __future__ import annotations

import re
import sqlite3
import sys

db = f'{sys.argv[1]}/data/isekai.db'
conn = sqlite3.connect(db)

# 真实热查询集（占位符用 ?；EXPLAIN 不看值，只看计划形状）
PROBES: dict[str, str] = {
    'effect 窄取数（方案①②）': (
        "SELECT id, event_id, target, kind, value, from_world FROM effect_state "
        "WHERE instance_id=? AND timeline_id=? AND active=1 AND from_world<=? AND target IN (?,?)"
    ),
    'effect_window（管理面/快照）': (
        "SELECT * FROM effect_state WHERE instance_id=? AND timeline_id=? AND active=1 AND from_world<=?"
    ),
    'effects_due（with_cause）': (
        "SELECT * FROM effect_state WHERE instance_id=? AND timeline_id=? AND active=1 "
        "AND expiry='with_cause' AND from_world<=?"
    ),
    'reaction 阶段推进（candidate）': (
        "SELECT * FROM reaction WHERE timeline_id=? AND stage='candidate' AND started_world<=?"
    ),
    'reaction 阶段推进（adopted）': "SELECT * FROM reaction WHERE timeline_id=? AND stage='adopted'",
    'reaction 阶段推进（active/fading 解除）': (
        "SELECT * FROM reaction WHERE timeline_id=? AND stage IN ('active','fading') AND source_ref IN (?,?)"
    ),
    '生活线计划（S-1 合并后）': (
        "SELECT * FROM life_plan WHERE instance_id=? AND timeline_id=? AND character_id=? AND day_index IN (?,?)"
    ),
    '生活线计划（单日）': (
        "SELECT * FROM life_plan WHERE instance_id=? AND timeline_id=? AND character_id=? AND day_index=?"
    ),
    '性格单元': (
        "SELECT * FROM unit WHERE instance_id=? AND timeline_id=? AND character_id=? ORDER BY archived, id"
    ),
    '归档态': (
        "SELECT character_id FROM character_state WHERE instance_id=? AND timeline_id=? AND archived=1"
    ),
    '打算': (
        "SELECT * FROM intent WHERE instance_id=? AND timeline_id=? AND character_id=? AND stage IN (?,?)"
    ),
    '经历窗口': (
        "SELECT * FROM experience WHERE instance_id=? AND timeline_id=? AND character_id=? AND world_seconds<=?"
    ),
    '获知窗口': (
        "SELECT * FROM knowledge WHERE instance_id=? AND timeline_id=? AND character_id=? AND world_seconds<=?"
    ),
    '记忆候选（按角色）': (
        "SELECT * FROM memory WHERE instance_id=? AND timeline_id=? AND character_id=? ORDER BY decay_world"
    ),
    '记忆衰减清扫': (
        "SELECT * FROM memory WHERE timeline_id=? AND decay_world<=? ORDER BY decay_world, id LIMIT ?"
    ),
    '事件窗': "SELECT * FROM event WHERE instance_id=? AND timeline_id=? AND world_seconds<=?",
    '说法按事件': "SELECT * FROM claim WHERE instance_id=? AND timeline_id=? AND event_id IN (?,?)",
    '账本': "SELECT * FROM world_ledger WHERE instance_id=? AND timeline_id=? AND scope_kind=? AND scope_id=?",
    '关系': "SELECT * FROM relation_state WHERE instance_id=? AND timeline_id=? AND from_id=?",
    '预约事件': "SELECT * FROM pending_event WHERE instance_id=? AND timeline_id=? AND state=?",
    '叙事单元': "SELECT * FROM narrative_unit WHERE instance_id=? AND timeline_id=? AND stage=?",
    '披露授权': "SELECT * FROM disclosure WHERE instance_id=? AND timeline_id=? AND character_id=?",
    '写入：effect 插入': (
        "INSERT INTO effect_state(instance_id, timeline_id, id, event_id, target, kind, family, value, "
        "from_world, expiry, recovery, active, cleared_at, priority, seq) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(instance_id, timeline_id, id) DO NOTHING"
    ),
    '写入：claim 插入': (
        "INSERT INTO claim(instance_id, timeline_id, id, event_id, source_id, text, audience, earliest_world, "
        "credibility) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(instance_id, timeline_id, id) DO NOTHING"
    ),
    '写入：knowledge 插入': (
        "INSERT INTO knowledge(instance_id, timeline_id, character_id, id, world_seconds, kind, target, "
        "source, stance, text) VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING"
    ),
}

used: dict[str, set[str]] = {}
for label, sql in PROBES.items():
    try:
        plan = conn.execute("EXPLAIN QUERY PLAN " + sql, tuple([None] * sql.count('?'))).fetchall()
    except sqlite3.Error as exc:
        print(f'  [跳过] {label}: {exc}')
        continue
    hits = set()
    for row in plan:
        detail = str(row[3])
        for match in re.findall(r'USING (?:COVERING )?INDEX ([A-Za-z0-9_]+)', detail):
            hits.add(match)
    used[label] = hits

all_indexes = sorted(
    name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
)
# 主键自动索引（sqlite_autoindex_*）不在此列；显式索引才是可评估对象
seen: set[str] = set()
for hits in used.values():
    seen |= hits

print(f'# 显式索引 {len(all_indexes)} 个；被探针查询命中的有 {len(seen)} 个\n')
for label, hits in used.items():
    print(f'  {label}: {sorted(hits) if hits else "（无索引，或用主键/自动索引）"}')
print('\n# 从未被任何探针命中的索引（候选：纯维护成本）：')
idle = [name for name in all_indexes if name not in seen]
for name in idle:
    table = conn.execute(
        "SELECT tbl_name FROM sqlite_master WHERE type='index' AND name=?", (name,)
    ).fetchone()[0]
    count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    print(f'  {name}  (表 {table}，{count} 行)')
if not idle:
    print('  （无——每个索引都有查询在用）')
conn.close()
