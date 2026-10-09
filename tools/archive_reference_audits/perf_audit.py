"""性能线终审：把 S-1 / S-4 / P0① / ④ / 页缓存 的落地与不变量逐条变成断言。"""

from __future__ import annotations

import io
import sqlite3
import sys
from pathlib import Path

root = sys.argv[1] if len(sys.argv) > 1 else '.hermes/acceptF'
db = f'{root}/data/isekai.db'
rows: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    rows.append((name, ok, detail))


st = io.open('isekai_core/store.py', encoding='utf-8').read()
sv = io.open('isekai_core/runtime/service.py', encoding='utf-8').read()

# ---- S-1：计划查询合并 ----
check('S-1 store.plan_list 存在', 'def plan_list(' in st)
check('S-1 _collect_batch 用 plan_list', 'self.store.plan_list(' in sv)
# 断言要**收窄到热路径所在的 `_collect_batch`**：别处（补卡 / 初始化）单日查询是合法的，
# 全文件断言会把合法调用误判为遗漏——第一版就是这么错的。
_start = sv.find('    def _collect_batch(')
_end = sv.find('\n    def ', _start + 10)
_collect = sv[_start:_end if _end > 0 else len(sv)]
check('S-1 _collect_batch 内已无按日 plan_get',
      'self.store.plan_get(' not in _collect,
      f"段内剩余 {_collect.count('self.store.plan_get(')} 处")
check('S-1 _collect_batch 内改用 plan_list', 'self.store.plan_list(' in _collect)

# ---- S-4：索引瘦身 ----
check('S-4 ix_effect_target 已从 SCHEMA 移除', 'CREATE INDEX IF NOT EXISTS ix_effect_target' not in st)
check('S-4 有幂等删除语句', 'DROP INDEX IF EXISTS ix_effect_target' in st)
check('S-4 B-7 索引按开关创建', 'ISEKAI_EFFECT_RETIRE' in st and 'ix_effect_retire' in st)
check('S-4 关闭时删除既有 B-7 索引', 'DROP INDEX IF EXISTS ix_effect_retire' in st)

# ---- P0 ①：复用上一条快照 ----
check('P0① 内存快照缓存存在', '_last_snapshot' in st)
check('P0① 取不到缓存时回落物化', 'base = self.commit_snapshot_get(base_id) if base_id else None' in st)
check('P0① 回滚时清缓存', '_last_snapshot.pop(key, None)' in st)

# ---- ④：链长计数 + 形态看列 ----
check('④ 链长计数存在', '_delta_chain' in st)
check('④ 只在超阈值时才压缩', 'depth > versioning_mod.MAX_DELTA_CHAIN' in st)
check('④ 回滚时清计数', '_delta_chain.pop(key, None)' in st)
check('④ 形态判断不再解析 payload JSON',
      'kind, _base = versioning_mod.snapshot_kind(row["payload"])' not in st)

# ---- 页缓存 ----
check('页缓存/mmap/temp_store 已设',
      'cache_size=-65536' in st and 'mmap_size=268435456' in st and 'temp_store=MEMORY' in st)

# ---- 数据库层复核 ----
try:
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    check('effect_state 索引已收敛（无 ix_effect_target / ix_effect_retire）',
          'ix_effect_target' not in names and 'ix_effect_retire' not in names)
    check('ix_effect_active / ix_effect_expiry 仍在', 'ix_effect_active' in names and 'ix_effect_expiry' in names)
    conn.close()
except Exception as exc:  # pragma: no cover
    check('数据库复核可执行', False, str(exc))

failed = [r for r in rows if not r[1]]
for name, ok, detail in rows:
    print(f'{"PASS" if ok else "FAIL"}  {name}' + (f'  [{detail}]' if detail else ''))
print(f'\n性能线终审：{len(rows)} 项，PASS {len(rows) - len(failed)}，FAIL {len(failed)}')
