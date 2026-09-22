"""故事表达契约（OC_STORY_LAYER_SPEC §五）：三维可分辨 + 讲述边界不因追问扩大。

这是 OC 故事层**唯一**给生成器的额外约束，而且是纯口径、不含事实：

- 本层向会话核心提供「场景意图 + 角色视角」，不提供事实文本（§五 首段）；
- 角色表达至少要让人分得清：**来源**（亲历 / 观察 / 听说 / 猜测）、**时间**（刚发生 /
  持续 / 尚未发生）、**意愿**（完整讲 / 只讲一部分 / 暂缓 / 不想谈）（§五 三条）；
- **重复追问不自动解锁保留内容**：上次没讲出口的事，被再问一遍也不多给——要改口
  必须有新的经历、获知、处境或对话依据（§五 末段）。

材料本身仍由会话核心按合法视图给（`runtime.turn_context` / 叙事中介），本层不搬材料。
"""

from __future__ import annotations

from typing import Any, Iterable

#: 一次能带进提示词的「没讲出口」条目上限：只影响提示词长度，不影响她的边界
BOUNDARY_TOPICS_MAX = 3

#: 三维表达契约（§五 三条），只约束怎么说
CONTRACT_HEADER = "她这次说话的表达契约（只约束怎么说，不改变她知道什么）："
CONTRACT_LINES: tuple[str, ...] = (
    "- 说清这件事是亲历、亲眼看到、听说还是猜的；不要把听来的说成亲眼所见。",
    "- 说清是刚发生、还在继续，还是还没发生；还没做成的事只能说是打算。",
    "- 愿意完整讲、只讲一部分、先不说都可以；没讲出口的不能当成已经讲过。",
)

#: 追问轮的边界行：同一件事的界限停在原来那里
BOUNDARY_HEADER = "她之前没讲出口的事（界限不变）："
BOUNDARY_LINES: tuple[str, ...] = (
    "被再问一遍也不多给：除非她这期间有了新的经历或新听来的说法，界限就停在原来那里；"
    "不想谈就直说不想谈，但不要编一段去填。",
)


def unit_topics(rows: Iterable[dict[str, Any]] | None) -> list[str]:
    """从叙事单元行里取「没讲出口」的话题文本（只进提示词，不给用户看）。"""
    out: list[str] = []
    for row in rows or ():
        if str(row.get("stage") or "") != "deferred":
            continue
        topic = str(row.get("topic") or "").strip()
        if not topic or topic in out:
            continue
        out.append(topic[:40])
        if len(out) >= BOUNDARY_TOPICS_MAX:
            break
    return out


def turn_block(*, deferred_topics: Iterable[str] = (), followup: bool = False) -> str:
    """组装本轮的表达契约块（会话层追加到扮演定义后面）。

    `followup=True` 时把没讲出口的界限带上：追问不扩权这件事必须有可核对的约束行，
    不能指望模型自己记得上一轮的取舍。
    """
    lines = [CONTRACT_HEADER, *CONTRACT_LINES]
    topics = [str(item).strip() for item in deferred_topics if str(item).strip()]
    if followup and topics:
        lines.append(BOUNDARY_HEADER)
        lines.extend(f"- {topic}" for topic in topics)
        lines.extend(BOUNDARY_LINES)
    return "\n".join(lines)
