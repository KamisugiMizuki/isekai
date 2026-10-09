"""对交付总览里的声明做自审：每条都变成可验证断言，打印 PASS/FAIL。"""

from __future__ import annotations

import io
import re
import sqlite3
import sys

root = sys.argv[1] if len(sys.argv) > 1 else '.hermes/acceptF'
db = f'{root}/data/isekai.db'
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = '') -> None:
    results.append((name, ok, detail))


store_src = io.open('isekai_core/store.py', encoding='utf-8').read()
svc_src = io.open('isekai_core/runtime/service.py', encoding='utf-8').read()
cfg_src = io.open('isekai_core/config.py', encoding='utf-8').read()
dom_src = io.open('isekai_core/store_state_domains.py', encoding='utf-8').read()
life_src = io.open('isekai_core/runtime/life.py', encoding='utf-8').read()
pers_src = io.open('isekai_core/runtime/personality.py', encoding='utf-8').read()

# 1. A-1 记忆衰减：有界 + 惰性
check('A-1 memory_decay 支持 limit', 'limit: int = 0' in store_src and 'LIMIT ?' in store_src)
check('A-1 惰性结算入口存在', 'def effective_strength' in io.open(
    'isekai_core/runtime/memory.py', encoding='utf-8').read())

# 2. A-5 五处有界
check('A-5 说法侧只取本页事件的说法', 'event_ids=page_event_ids' in svc_src)
check('A-5 已知事件按 id 批量取', 'ids=known' in svc_src)
check('A-5 backfill 判空走 has_events', 'self.store.has_events(' in svc_src)
check('A-5 ops 单条说法走主键', 'store.claim_get(' in io.open(
    'isekai_core/world/ops.py', encoding='utf-8').read().replace('def claim_get', ''))
check('A-5 intent_list 阶段下推', 'stages=("adopted", "waiting", "deferred")' in svc_src)

# 3. A-7 注册表
check('A-7 注册表含 effect_superseded', '"effect_superseded"' in dom_src)
import re as _re
_m = _re.search(r'CLEARED_ON_ROLLBACK[^\n]*= \((.*?)\n\)', dom_src, _re.DOTALL)
check('A-7 回滚清单含 effect_superseded', bool(_m) and 'effect_superseded' in _m.group(1),
      '精确解析元组体（首次出现可能是说明文字，不能按字符串截断）')

# 4. A-8 确定性（世界事实路径不得用内置 hash / secrets 生成 id）
check('A-8 life 计划 id 用 stable_key', 'stable_key(instance_id, timeline_id, character_id' in life_src)
check('A-8 life 不再用 secrets', 'secrets' not in life_src.replace('`secrets.token_hex`', ''))
check('A-8 personality 不再用内置 hash', 'abs(hash(' not in pers_src)

# 5. A-10 读快照
check('A-10 追赶不再拒绝 + 显式水位', 'catching_up' in svc_src and 'lag_world_seconds' in svc_src)
check('A-10 冻结仍 not_ready', "时间线当前是" in svc_src)

# 6. A-11 默认值
check('A-11 默认 rate_max 已下调', 'rate_max: int = 864000' in cfg_src)

# 7. A-4 最小间隔
check('A-4 最小间隔闸存在', 'autocommit_min_gap_seconds' in svc_src and 'autocommit_min_gap_seconds' in cfg_src)

# 8. B-7 默认停用
check('B-7 默认停用（开关默认 0）', 'ISEKAI_EFFECT_RETIRE", "0"' in store_src.replace("'", '"'))

# 9. 页缓存
check('页缓存 mmap temp_store 已设', 'cache_size=-65536' in store_src and 'mmap_size=268435456' in store_src)

# 10. 方案①②：窄取数 + target 下推
check('方案② 窄取数方法存在', 'def effect_constraints' in store_src)
check('方案① target 下推接线', 'targets=tuple(sorted(constraint_targets))' in svc_src)

# 11. 数据库层：索引与表真实存在
try:
    conn = sqlite3.connect(db)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
    check('表 effect_superseded 存在', 'effect_superseded' in names)
    # S-4 索引瘦身之后，这里的断言必须反映**实测后的现实**，而不是旧声明：
    #   `ix_effect_target` 经 EXPLAIN QUERY PLAN 证明从未被命中 ⇒ 已删除（断言它**不在**）；
    #   `ix_effect_retire` / `ix_effect_superseded_by` 只服务 B-7，而 B-7 默认停用 ⇒ 默认不创建。
    for idx in ('ix_effect_expiry', 'ix_reaction_timeline_stage', 'ix_effect_active'):
        check(f'索引 {idx} 存在（实测在用）', idx in names)
    check('索引 ix_effect_target 已按实测删除', 'ix_effect_target' not in names)
    check('B-7 索引默认不创建（开关关）', 'ix_effect_retire' not in names)
    conn.close()
except Exception as exc:  # pragma: no cover
    check('数据库自审可执行', False, str(exc))

# 12. 文档与规格：原则约束与 B-8 条款存在
spec = io.open('docs/worldruntime/EVENT_ENGINE_SPEC.md', encoding='utf-8').read()
check('B-5 原则约束已进规格', '只接受枚举档位' in spec and '不得' in spec)
wrs = io.open('docs/worldruntime/WORLD_RUNTIME_SPEC.md', encoding='utf-8').read()
check('B-8 振荡器/棘轮条款已进规格', '振荡器' in wrs and '棘轮' in wrs)
iface = io.open('docs/worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md', encoding='utf-8').read()
check('A-10 规格已同步（追赶返回 ok）', '追赶中返回 `ok`' in iface)
check('A-10 规格 not_ready 已去掉追赶', '追赶、冻结' not in iface)

failed = [r for r in results if not r[1]]
for name, ok, detail in results:
    print(f'{"PASS" if ok else "FAIL"}  {name}' + (f'  [{detail}]' if detail else ''))
print(f'\n合计 {len(results)} 项：PASS {len(results) - len(failed)}，FAIL {len(failed)}')
