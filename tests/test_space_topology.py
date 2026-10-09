"""B-4 v1：空间是**拓扑而非坐标**（邻接表 + 通行代价档）。

锁住三件事：① 邻接是引用闭集且只允许 `to / 通行 / 代价`；② 可达性判定是**确定性纯函数**、不涉及坐标；
③ **结构断言**：`isekai_core` 全库不得出现坐标 / 距离 / 寻路符号——这是堵「拓扑 → 路网 → 通行模拟」滑坡的唯一守卫。
"""

from __future__ import annotations

import io
from pathlib import Path

from isekai_core.runtime import space
from isekai_core.world.validate import validate_package
from samples import sample_package  # noqa: F401  仓库既有夹具


def _regions_package(regions: list[dict]) -> dict:
    package = sample_package()
    package["world"] = {**(package.get("world") or {}), "regions": regions}
    return package


def _chain_package(**over) -> dict:
    return _regions_package([
        {"id": "pl-1", "name": "北岸", "adjacent": [{"to": "pl-2", "通行": "可通行", "代价": 1, **over}]},
        {"id": "pl-2", "name": "河口", "adjacent": [{"to": "pl-3", "通行": "可通行", "代价": 2}]},
        {"id": "pl-3", "name": "内陆", "adjacent": [{"to": "pl-4", "通行": "受阻"}]},
        {"id": "pl-4", "name": "远山"},
    ])


def test_topology_declaration_is_valid() -> None:
    errors = validate_package(_chain_package())
    assert not [item for item in errors if "adjacent" in item or "regions" in item], errors


def test_adjacency_must_reference_declared_regions() -> None:
    """拓扑必须是**引用闭集**：指向未登记区域在创建期就失败。"""
    errors = validate_package(_regions_package([
        {"id": "pl-1", "name": "北岸", "adjacent": [{"to": "pl-不存在", "通行": "可通行", "代价": 1}]},
    ]))
    assert any("未登记区域" in item for item in errors), errors


def test_coordinate_like_field_is_rejected() -> None:
    """**核心守卫**：区域条目带坐标即拒绝——B-4 是拓扑，不是坐标系。"""
    errors = validate_package(_regions_package([
        {"id": "pl-1", "name": "北岸", "x": 12, "y": 34},
    ]))
    assert any("不允许坐标" in item for item in errors), errors


def test_edge_extra_fields_are_rejected() -> None:
    """边上多带 `distance` 就等于引入连续距离 ⇒ 拒绝。"""
    errors = validate_package(_chain_package(distance=3.5))
    assert any("只允许 to / 通行 / 代价" in item for item in errors), errors


def test_bad_cost_is_rejected() -> None:
    """代价是**档位**（1…3 整数），不是连续量。"""
    errors = validate_package(_chain_package(代价=2.5))
    assert any("必须是 1…3 的整数" in item for item in errors), errors


def test_reachability_is_deterministic_and_respects_blocked() -> None:
    """可达性判定：确定性、按代价预算、受阻的边不通；且**不涉及坐标**。"""
    regions = _chain_package()["world"]["regions"]
    first = space.reachable(regions, "pl-1", max_cost=2)
    second = space.reachable(regions, "pl-1", max_cost=2)
    assert first == second, "同一输入必须同一结果"
    assert first["pl-1"] == 0
    assert first["pl-2"] == 1, "相邻代价 1 必须可达"
    assert first.get("pl-3") is None, "预算 2 不足以覆盖 1+2"
    assert space.reachable(regions, "pl-1", max_cost=3)["pl-3"] == 3
    assert "pl-4" not in space.reachable(regions, "pl-2", max_cost=3), "受阻的边不得通过"
    assert space.hop_cost(regions, "pl-1", "pl-2") == 1
    assert space.hop_cost(regions, "pl-1", "pl-9") is None


def test_core_has_no_coordinate_or_pathfinding_symbols() -> None:
    """**结构断言（防滑坡唯一守卫）**：核心里不得出现坐标 / 距离 / 寻路符号。

    B-4 的提案验收 ③。允许的词是「可达 / 邻接 / 代价」；一旦有人写出 `distance`、`坐标`、`pathfind`
    这类符号，这条测试立刻红——那时必须回到规格层讨论，而不是在实现里悄悄扩。
    """
    # 只查**代码标识符与字符串字面量（不含文档字符串）**：
    # 第一版按整文件搜文本，结果全是假阳性——「坐标」出现在写着「**不**引入坐标」的注释与文档字符串里。
    # 一个把「反例说明」当成违规的断言，比没有断言更糟（它会诱使人删掉说明）。
    import ast

    cjk_forbidden = ("坐标", "经纬度", "距离", "寻路")
    ident_forbidden = {"pathfind", "shortest_path", "dijkstra", "a_star", "haversine", "geodesic", "route_plan"}
    hits: list[str] = []
    for path in Path('isekai_core').rglob('*.py'):
        # `validate.py` **故意排除**：它持有的是「拒绝坐标 / 距离」的词表与报错文案，
        # 命中的是**反对坐标的代码**而不是支持坐标的代码。对应的正向守卫在下一个测试里断言。
        if path.name == 'validate.py':
            continue
        tree = ast.parse(path.read_text(encoding='utf-8'))
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                body = getattr(node, "body", None)
                if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                        and isinstance(body[0].value.value, str):
                    docstrings.add(id(body[0].value))
        identifiers: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                identifiers.append(node.id)
            elif isinstance(node, ast.Attribute):
                identifiers.append(node.attr)
            elif isinstance(node, ast.arg):
                identifiers.append(node.arg)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                identifiers.append(node.name)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings:
                identifiers.append(node.value)
        for token in identifiers:
            if token in ident_forbidden:
                hits.append(f'{path.as_posix()}: {token}')
            for bad in cjk_forbidden:
                if bad in token:
                    hits.append(f'{path.as_posix()}: {bad}')
    assert not hits, f"核心里出现坐标 / 距离 / 寻路符号（仅查标识符与字面量）：{hits}"


def test_the_coordinate_rejection_guard_itself_exists() -> None:
    """反向断言：拒绝坐标的那条校验必须**在位**——不能为了通过上面的断言而把它删掉。

    这两条测试是一对：一条说「运行时里没有坐标」，另一条说「创建期仍然拒绝坐标」。
    只留前者会诱使人删掉守卫；只留后者则运行时可能悄悄长出坐标支持。
    """
    src = io.open('isekai_core/world/validate.py', encoding='utf-8').read()
    assert 'coordinate_like' in src, "区域校验里的坐标拒绝词表不见了"
    assert '不允许坐标' in src, "坐标拒绝的报错文案不见了"
    assert 'ADJACENCY_KINDS' in src, "通行档闭集不见了"
