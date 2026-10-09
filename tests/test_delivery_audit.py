"""交付契约审计（进 `pytest` 的版本）。

此前这些断言只存在于未入库的 `.hermes/*_audit.py`——**而它们锁住的是交付正确性**，
不该只活在临时目录里。这里把最关键的部分固化进测试套：

- **源码级**：S-1（计划查询合并）、S-4（索引瘦身与开关）、P0①/④（快照复用与链长计数）、页缓存；
- **数据库级**：新状态域已登记且已建表、索引已收敛。

写在注释里的**理由**与断言同等重要：它们解释「为什么必须有这条」，避免后来者按「看起来更整齐」改回去。
"""

from __future__ import annotations

import io

from isekai_core import store_state_domains as domains
from test_runtime import store, world  # noqa: F401  夹具在那边


def _src(path: str) -> str:
    return io.open(path, encoding='utf-8').read()


def _segment(text: str, header: str) -> str:
    """取某个方法/函数的正文段（断言要收窄到对象本身，避免全文件级假阳性）。"""
    start = text.find(header)
    assert start >= 0, f'未找到 {header}'
    end = text.find('\n    def ', start + 10)
    return text[start:end if end > 0 else len(text)]


# ---------------- S-1：计划查询合并 ----------------

def test_s1_plan_queries_are_merged_in_the_hot_path() -> None:
    store_src = _src('isekai_core/store.py')
    service_src = _src('isekai_core/runtime/service.py')
    assert 'def plan_list(' in store_src
    collect = _segment(service_src, '    def _collect_batch(')
    assert 'self.store.plan_list(' in collect
    assert 'self.store.plan_get(' not in collect, '热路径不得退回按日查询（S-1 的收益来源）'


# ---------------- S-4：索引瘦身与开关 ----------------

def test_s4_unused_index_is_gone_and_scoped_indexes_are_gated() -> None:
    store_src = _src('isekai_core/store.py')
    assert 'CREATE INDEX IF NOT EXISTS ix_effect_target' not in store_src, '该索引实测无人使用，不得再加回'
    assert 'DROP INDEX IF EXISTS ix_effect_target' in store_src, '既有库也要能清掉它'
    assert 'ISEKAI_EFFECT_RETIRE' in store_src
    assert 'DROP INDEX IF EXISTS ix_effect_retire' in store_src, '关闭时必须连旧库里的也删掉'


def test_s4_effect_state_indexes_are_converged(store) -> None:  # noqa: ANN001
    names = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='effect_state'"
        )
    }
    assert 'ix_effect_target' not in names
    assert 'ix_effect_retire' not in names, 'B-7 默认停用 ⇒ 其索引不得存在'
    assert {'ix_effect_active', 'ix_effect_expiry'} <= names, '实测在用的索引必须保留'


# ---------------- 页缓存 ----------------

def test_page_cache_pragmas_are_set(store) -> None:  # noqa: ANN001
    cache = store._conn.execute("PRAGMA cache_size").fetchone()[0]
    mmap = store._conn.execute("PRAGMA mmap_size").fetchone()[0]
    assert cache == -65536 and mmap == 268435456


# ---------------- P0① / ④：提交路径的两处不变量 ----------------

def test_p0_snapshot_reuse_and_chain_counter_exist() -> None:
    store_src = _src('isekai_core/store.py')
    assert '_last_snapshot' in store_src, 'P0①：上一条快照的内存副本'
    assert 'base = self.commit_snapshot_get(base_id) if base_id else None' in store_src, '取不到缓存必须回落'
    assert '_delta_chain' in store_src, '④：链长计数'
    assert 'depth > versioning_mod.MAX_DELTA_CHAIN' in store_src, '④：只在超阈值时才走链压缩'
    assert 'kind, _base = versioning_mod.snapshot_kind(row["payload"])' not in store_src, \
        '④：形态判断不得再解析整段 payload JSON'


def test_rollback_invalidates_both_caches() -> None:
    """回滚后两个缓存都必须作废，否则下一次提交会拿**未来**的快照当基准 / 误判链长。"""
    store_src = _src('isekai_core/store.py')
    clear = _segment(store_src, '    def timeline_clear_state(')
    assert '_last_snapshot.pop(key, None)' in clear
    assert '_delta_chain.pop(key, None)' in clear


# ---------------- 新状态域：已登记且已建表 ----------------

def test_new_state_domains_are_registered_and_created(store) -> None:  # noqa: ANN001
    for table in ('world_ledger', 'relation_state', 'effect_superseded'):
        assert table in domains.cleared_tables(), f'{table} 必须登记进回滚清单（A-7 纪律）'
        row = store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        assert row is not None, f'{table} 未建表'


def test_writes_have_consumers_for_new_domains() -> None:
    """新状态域必须有**写入方**，否则就是「建了表没人写」的空状态域（A-9b 的教训）。"""
    service_src = _src('isekai_core/runtime/service.py')
    assert 'ledger_init(' in service_src, '账本缺写入方'
    assert 'relation_init(' in service_src, '关系缺写入方'


# ---------------- 结构断言：核心不得长出坐标 / 寻路 ----------------

def test_core_has_no_coordinate_symbols() -> None:
    import ast
    from pathlib import Path

    bad_ids = {'pathfind', 'shortest_path', 'dijkstra', 'a_star', 'haversine', 'geodesic', 'route_plan'}
    for path in Path('isekai_core').rglob('*.py'):
        if path.name == 'validate.py':
            continue  # 它持有的是「拒绝坐标」的词表
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
                assert token not in bad_ids, f'{path.as_posix()} 出现寻路标识符：{token}'
                for bad in ('坐标', '经纬度', '寻路'):
                    assert bad not in token, f'{path.as_posix()} 出现坐标类标识符：{token}'
