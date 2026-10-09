"""B-3+B-9 v2：账本**推导式**（声明式纯函数，**没有表达式语言**）。

裁决书：`docs/worldruntime/B3_B9_V2_RULING_LEDGER_DERIVED.md`

三条纪律（决定了这个模块这么小）：

1. **算子闭集，只有四种**。可自由编写的表达式等于在核心里开一个脚本引擎入口——
   与「不做 HP 系统」「不给 `weight_expr`」「不引入坐标」是同一条滑坡，一律不开口。
2. **推导值不落库**。账本数字只能经 `ledger_put` 变更（**单一事实源**）；
   把推导值写回 `world_ledger` 会制造第二份可被写坏的事实。
   因此这个模块是**纯函数**：进的是已存储的键值，出的是推导键值，不碰存储。
3. **单趟、无环、确定性**。校验只允许操作数引用「存储键」或「**在它之前声明的**推导键」，
   于是不存在环，也不需要拓扑排序或迭代收敛。
"""

from __future__ import annotations

from typing import Any

#: 算子闭集。**多一个都不加**（这是「不给表达式语言」这条纪律的具体边界）。
DERIVED_OPS: tuple[str, ...] = ("和", "差", "积", "比")

#: `积` 的缺省千分比分母。
DEFAULT_SCALE = 1000


def operation(spec: dict[str, Any]) -> str:
    return str(spec.get("op") or "")


def operands(spec: dict[str, Any]) -> tuple[str, str]:
    return str(spec.get("a") or ""), str(spec.get("b") or "")


def apply_op(op: str, a: int, b: int, *, scale: int = DEFAULT_SCALE) -> int:
    """执行一个算子。**除零回 0**（不抛错）：账本要能被稳定读取，0 是「无意义值」的保守表达。

    整数语义（不用浮点）：`积` 用千分比 `// scale`，`比` 用 `a × 1000 // b`——
    账本是域外计数器，浮点会引入不可解释的漂移。
    """
    if op == "和":
        return int(a) + int(b)
    if op == "差":
        return int(a) - int(b)
    if op == "积":
        divisor = int(scale) or DEFAULT_SCALE
        return int(a) * int(b) // divisor
    if op == "比":
        if int(b) == 0:
            return 0
        return int(a) * DEFAULT_SCALE // int(b)
    raise ValueError(f"未支持的推导算子：{op!r}（闭集只有 {'/'.join(DERIVED_OPS)}）")


def evaluate(derived: list[dict[str, Any]], stored: dict[str, int]) -> dict[str, int]:
    """单趟求值：按声明顺序算，每个推导键可引用**存储键**或**更早声明的**推导键。

    `stored` 只读、不被改动（返回新字典）。未在 `stored` 里的操作数按 **0** 处理——
    运行时保守兜底；创建期校验会先拒绝「引用未声明键」的包。
    """
    values: dict[str, int] = {str(key): int(value) for key, value in stored.items()}
    out: dict[str, int] = {}
    for spec in derived or []:
        if not isinstance(spec, dict):
            continue
        ident = str(spec.get("id") or "")
        if not ident:
            continue
        left, right = operands(spec)
        a = values.get(left, 0)
        b = values.get(right, 0)
        scale = spec.get("scale")
        result = apply_op(
            operation(spec), a, b,
            scale=int(scale) if isinstance(scale, int) and not isinstance(scale, bool) else DEFAULT_SCALE,
        )
        values[ident] = result   # 后续推导键可以引用它（这就是「链式推导」）
        out[ident] = result
    return out
