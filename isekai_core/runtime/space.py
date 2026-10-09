"""空间的最小形态：**拓扑而非坐标**（B-4，人类裁决 2026-10-10）。

只提供三件东西：邻接关系、通行档、代价档。
**明确不做**：坐标、连续距离、寻路、渲染、通行模拟——`reachable` 是**可达集合**判定，
不是路径搜索（没有「怎么走」的问题，只有「能不能到、要几步」）。

用途（按裁决**只限三处**；v1 先把纯函数备好，消费者接入见任务清单 B-4 的 v2）：
① 说法传播延迟；② 事件影响范围；③ 生活线活动的可达性判定。
"""

from __future__ import annotations

from typing import Any

#: 被阻断的通行档：不参与可达集合
BLOCKED = "受阻"


def _edges(regions: list[dict[str, Any]] | None) -> dict[str, dict[str, int]]:
    """邻接表 → `{区域: {相邻区域: 代价}}`。**双向**：世界包声明单向即可（路是通的）。"""
    out: dict[str, dict[str, int]] = {}
    for region in regions or []:
        if not isinstance(region, dict):
            continue
        ident = str(region.get("id") or "")
        if not ident:
            continue
        bucket = out.setdefault(ident, {})
        for edge in region.get("adjacent") or []:
            if not isinstance(edge, dict):
                continue
            to_id = str(edge.get("to") or "")
            if not to_id or str(edge.get("通行") or "可通行") == BLOCKED:
                continue
            cost = edge.get("代价") if edge.get("代价") is not None else edge.get("cost")
            cost = int(cost) if isinstance(cost, int) and not isinstance(cost, bool) else 1
            # 同一对区域出现多条边时取**最小代价**（确定性：不随声明顺序变）
            bucket[to_id] = min(bucket.get(to_id, cost), max(1, cost))
            reverse = out.setdefault(to_id, {})
            reverse[ident] = min(reverse.get(ident, cost), max(1, cost))
    return out


def hop_cost(regions: list[dict[str, Any]] | None, a: str, b: str) -> int | None:
    """相邻两区域的**代价档**；不相邻或受阻时返回 `None`。"""
    return _edges(regions).get(str(a), {}).get(str(b))


def reachable(
    regions: list[dict[str, Any]] | None, start: str, *, max_cost: int = 3
) -> dict[str, int]:
    """从 `start` 出发、在代价预算内**可达**的区域 → 最小代价。

    确定性：邻居按标识排序遍历，同一输入同一结果；不涉及坐标、距离或路径。
    """
    graph = _edges(regions)
    start = str(start)
    if start not in graph:
        return {}
    best: dict[str, int] = {start: 0}
    frontier: list[str] = [start]
    while frontier:
        node = frontier.pop(0)
        for neighbour in sorted(graph.get(node, {})):
            cost = best[node] + graph[node][neighbour]
            if cost > max(0, int(max_cost)):
                continue
            if neighbour not in best or cost < best[neighbour]:
                best[neighbour] = cost
                frontier.append(neighbour)
    return best
