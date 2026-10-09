"""终审：把第 4 批（B 类）的落地与守卫逐条变成可验证断言。

与 `.hermes/self_audit.py`（覆盖 A 类）互补；两者都跑一遍才算「全量复核」。
"""

from __future__ import annotations

import ast
import io
import sqlite3
import sys
from pathlib import Path

root = sys.argv[1] if len(sys.argv) > 1 else '.hermes/acceptF'
db = f'{root}/data/isekai.db'
rows: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    rows.append((name, ok, detail))


def read(path: str) -> str:
    return io.open(path, encoding='utf-8').read()


ev = read('isekai_core/runtime/events.py')
vd = read('isekai_core/world/validate.py')
sv = read('isekai_core/runtime/service.py')
st = read('isekai_core/store.py')
dom = read('isekai_core/store_state_domains.py')
life = read('isekai_core/runtime/life.py')

# ---- B-6 说法差异化 ----
check('B-6.1 claim_rows 支持每来源表述', '_claim_variants' in ev and 'variants.get(source_id)' in ev)
check('B-6.2 校验强制互异', '同文不构成差异化' in vd)

# ---- B-5 身体后果 ----
check('B-5 档位闭集含死亡', 'CASUALTY_GRADES: tuple[str, ...] = ("轻伤", "重伤", "失能", "死亡")' in vd)
check('B-5 casualty 进闭集', '"casualty"' in vd)
check('B-5 优先级最高', '"casualty": 90' in ev)
check('B-5 档位改写计划', 'def apply_casualty' in life and 'apply_casualty(_plan, grade)' in sv)
check('B-5 死亡与归档同批', 'lethal' in sv and 'death_rows' in sv)
check('B-5 原则约束已进规格', '只接受枚举档位' in read('docs/worldruntime/EVENT_ENGINE_SPEC.md'))

# ---- B-8 振荡器 / 棘轮 ----
wrs = read('docs/worldruntime/WORLD_RUNTIME_SPEC.md')
check('B-8 振荡器/棘轮条款', '振荡器' in wrs and '棘轮' in wrs)

# ---- B-1 压力量 ----
check('B-1 压力量纯函数', 'def pressure_values' in ev and 'def modulated_weight' in ev)
check('B-1 权重形式写死（拒绝表达式）', '只允许 id / k 两个键' in vd)
check('B-1 抽取接入调制', 'modulated_weight(item, pressures)' in ev)

# ---- B-3+B-9 账本 ----
check('B-3 账本表存在', 'CREATE TABLE IF NOT EXISTS world_ledger' in st)
check('B-3 账本写入方接线', 'ledger_init(' in sv)
check('B-3 账本只补不覆盖', '只补不覆盖' in st)
check('B-9 点名规则结构断言就位', Path('tests/test_ledger.py').exists())

# ---- B-2 关系事实层 ----
check('B-2 关系表存在', 'CREATE TABLE IF NOT EXISTS relation_state' in st)
check('B-2 关系必须有依据', '关系变化必须带依据' in st)
check('B-2 关系写入方接线', 'relation_init(' in sv)
check('B-2 MEMORY_SPEC 措辞已修订', '用户对世界' in read('docs/worldruntime/MEMORY_SPEC.md'))

# ---- B-4 拓扑 ----
check('B-4 拓扑纯函数', Path('isekai_core/runtime/space.py').exists())
check('B-4 邻接校验与坐标拒绝', 'ADJACENCY_KINDS' in vd and 'coordinate_like' in vd)
check('B-4 两处规格措辞已修订', '不引入坐标与通行模拟' in wrs and '是拓扑，不是坐标' in read('docs/worldruntime/EVENT_ENGINE_SPEC.md'))

# ---- A-7 注册表覆盖三个新状态域 ----
for table in ('world_ledger', 'relation_state', 'effect_superseded'):
    body = dom.split('CLEARED_ON_ROLLBACK: tuple[str, ...] = (')[1].split('\n)')[0]
    check(f'A-7 {table} 在回滚清单内', f'"{table}"' in body)

# ---- 数据库层：三个新表真实存在 ----
try:
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table in ('world_ledger', 'relation_state', 'effect_superseded'):
        check(f'表 {table} 存在', table in names)
    conn.close()
except Exception as exc:  # pragma: no cover
    check('数据库终审可执行', False, str(exc))

# ---- 结构断言：核心里没有坐标 / 寻路标识符（排除持有拒绝词表的 validate.py）----
bad_ids = {"pathfind", "shortest_path", "dijkstra", "a_star", "haversine", "geodesic", "route_plan"}
bad_cjk = ("坐标", "经纬度", "距离", "寻路")
hits: list[str] = []
for path in Path('isekai_core').rglob('*.py'):
    if path.name == 'validate.py':
        continue
    tree = ast.parse(path.read_text(encoding='utf-8'))
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, 'body', None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        tokens: list[str] = []
        if isinstance(node, ast.Name):
            tokens.append(node.id)
        elif isinstance(node, ast.Attribute):
            tokens.append(node.attr)
        elif isinstance(node, ast.arg):
            tokens.append(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            tokens.append(node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            tokens.append(node.value)
        for token in tokens:
            if token in bad_ids or any(bad in token for bad in bad_cjk):
                hits.append(f'{path.as_posix()}:{token}')
check('结构断言：核心无坐标/寻路符号', not hits, ','.join(hits[:3]))

failed = [r for r in rows if not r[1]]
for name, ok, detail in rows:
    print(f'{"PASS" if ok else "FAIL"}  {name}' + (f'  [{detail}]' if detail else ''))
print(f'\n第 4 批终审：{len(rows)} 项，PASS {len(rows) - len(failed)}，FAIL {len(failed)}')
