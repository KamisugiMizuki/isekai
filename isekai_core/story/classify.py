"""输入分类（OC_STORY_LAYER_SPEC §二 / §3.4）：六类主类别，分类不改变权限。

本文件是纯逻辑 + 一次便宜判断的问法，不碰数据库、不写世界：

- **一次请求只能有一个主类别**（§3.4），主类别一旦确定就只走该类别的处理路径；
- **分类结果不改变权限**：它只决定这一轮走哪条路。改世界仍必须由用户显式走
  `preview → commit`（§3.5 / §八），分类永远不给出写入许可；
- **无法确定时按「联络分享」处理**，原文照旧作为她听到的内容（§3.4）——分不清就当她听见了；
- **确定性判定优先**（SESSION_CORE_SPEC §4.2 / NARRATIVE_LAYER_SPEC §6.2 同一口径）：
  结构性请求、绑定 / 版本类显式命令、以及词表能定论的探询与追问，一律**在本地定论、不调模型**；
  只有词表给不出结论的模糊输入才调一次模型（`decide_instant` 返回 None 即「无法结论」）。
  词表因此从「省一次调用的小聪明」升级为**第一判据**——但它仍然不是唯一判据：
  词表定不了就交给模型，模型也不可用才退回「分不清就当她听见了」。
"""

from __future__ import annotations

import json
import re
from typing import Any

#: 主类别闭集（§3.4 表格左列）
CATEGORIES: tuple[str, ...] = (
    "contact_share",
    "status_inquiry",
    "followup",
    "world_change",
    "version_op",
    "trpg_action",
)

#: 产品语言名称（客户端与探针共用一份，不在两处各写一遍）
LABELS: dict[str, str] = {
    "contact_share": "联络分享",
    "status_inquiry": "近况询问",
    "followup": "旧话题追问",
    "world_change": "请求改变世界",
    "version_op": "版本操作",
    "trpg_action": "TRPG 行动",
}

#: 需要转入独立流程的主类别 → 目标流程（§3.4「允许的写入」一列）
HANDOFF_TARGETS: dict[str, str] = {
    "world_change": "creation",
    "version_op": "version",
    "trpg_action": "trpg",
}

#: 转交说明（上线的 system_notice 文本）：说清「普通对话没有执行它」，不冒充她的回复
HANDOFF_NOTICES: dict[str, str] = {
    "creation": "这条是创作请求，普通对话没有执行它。要改世界请走创作流程：先看影响预览，确认之后才生效。",
    "version": "这条属于版本操作（保存分支 / 恢复版本 / 导入导出），已经交给版本流程。普通对话不会替你回滚或分叉。",
    "trpg": "这条属于 TRPG 行动：行动要经过规则裁定，普通对话不会替你掷骰，也不给成功或失败结论。",
}

#: 入口前缀：标记这是一次「判断点」调用（$ 见 llm.JUDGEMENT_MARK）。
#: 真实模型按提示词正常作答；测试替身据此把判断点与回复生成分开，不占用回复脚本。
JUDGEMENT_MARK = "【判断点】输入分类"

#: 分不清时的默认类别：原文照旧作为她听到的内容（§3.4）
DEFAULT_CATEGORY = "contact_share"

SYSTEM = (
    f"{JUDGEMENT_MARK}\n"
    "给「用户对世界里的角色说的一句话」定一个主类别，只输出 JSON，不要解释。\n"
    "可选类别（只能选一个）：\n"
    "- contact_share：用户分享自己的情况、感受、想法、愿望或闲聊。"
    "注意：说「我希望这里下一场雨」是表达愿望，仍然算 contact_share。\n"
    "- status_inquiry：用户在问角色最近 / 眼下怎么样、在做什么。\n"
    "- followup：用户在回接之前聊过、或她讲过的事，追问后续。\n"
    "- world_change：用户明确要求替他改写世界、设定、背景，或要求凭空制造一件世界事件（不是愿望）。\n"
    "- version_op：用户要求存档 / 回滚 / 恢复到某个版本 / 分支 / 导入导出创作状态。\n"
    "- trpg_action：用户以玩家身份声明行动并要一个裁定（掷骰、检定、攻击、回合、进入战斗）。\n"
    '输出：{"category": "<上述之一>", "why": "不超过 20 字的理由"}\n'
    "分不清时输出 contact_share。"
)

#: 确定性词表：**命中即定论、跳过模型调用**（SESSION_CORE_SPEC §4.2「确定性判定优先」）。
#: 三类结构性请求命中的是「转独立流程」的类目，宁可交给流程也不在普通轮次里静默执行；
#: 追问 / 询问两类是显式问法，词表能定论就不必每轮再花一次判断调用。
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "version_op",
        re.compile(
            r"(回滚|恢复到|回到(上一个)?存档|读档|存档|分叉|开一条(新|另一条)线|另起一条线"
            r"|导出(这个)?(存档|实例|世界)|导入(存档|实例|备份))"
        ),
    ),
    (
        "trpg_action",
        re.compile(
            r"(掷骰|骰子|骰点|检定|先攻|进入战斗|发起攻击|攻击检定|过个?判定|投个?d\d+|d20|d100|hp|血量|回合)",
            re.IGNORECASE,
        ),
    ),
    (
        "world_change",
        re.compile(
            r"((帮我|我要|我想|请你?)(把|将).{0,12}(世界|设定|背景|世界观).{0,6}(改|换|加|删|写)"
            r"|(加|添)(一个|一条|一场)(事件|灾难|变故|剧情)|凭空(制造|生成)|改一下(世界|设定|背景)"
            r"|写进(世界)?(设定|正史))"
        ),
    ),
    ("followup", re.compile(r"(上次|之前|刚才|前面|先前).{0,8}(说|提|讲|聊)|后来(呢|怎样|怎么|如何)|接着说|继续讲")),
    (
        "status_inquiry",
        re.compile(r"(最近|这几天|今天|现在).{0,6}(怎么样|如何|过得好|在做什么|忙什么|干嘛)|你在(做什么|干嘛|忙什么)"),
    ),
)


def prefilter(text: str) -> str | None:
    """确定性词表：命中返回类别，否则 None。**是第一判据，但仍不是唯一判据**——
    它给不出结论的模糊输入由调用方交给模型判定（`decide_instant`）。"""
    body = str(text or "")
    if not body.strip():
        return None
    for category, pattern in _RULES:
        if pattern.search(body):
            return category
    return None


def handoff_of(category: str) -> str:
    """该类别要转入的流程（不需要转交时返回空串）。"""
    return HANDOFF_TARGETS.get(str(category), "")


def notice_for(target: str) -> str:
    """转交说明文本；未知目标回一句通用说明，不猜流程。"""
    return HANDOFF_NOTICES.get(str(target), "这条请求不在普通联络里处理，已经交给对应流程。")


def classify_request(text: str) -> list[dict[str, Any]]:
    """分类判断的问法（服务层发起一次便宜调用）。"""
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": f"用户这句话：{str(text or '').strip()}"},
    ]


def parse_classify(raw: str) -> tuple[str, str] | None:
    """解析分类结果；闭合外 / 解析不出来回 None（调用方走预筛与默认兜底，不误判）。"""
    body = str(raw or "").strip()
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(body[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    category = str(payload.get("category") or "").strip()
    if category not in CATEGORIES:
        return None
    return category, str(payload.get("why") or "")


def decide(text: str, *, model: tuple[str, str] | None = None, model_failed: bool = False) -> dict[str, Any]:
    """合并确定性判定与模型判断，得到这一轮的唯一主类别。

    合并口径（§3.4 / SESSION_CORE_SPEC §4.2「确定性判定优先」）：
    1. 词表命中（结构 / 绑定 / 显式命令 / 追问 / 询问）→ 直接用，**不再调模型**；
    2. 词表给不出结论时，以模型判断为准（模型是兜底，不是每轮固定动作）；
    3. 模型不可用 / 判不出来 → 退到词表（若命中则标 `rule_fallback`）；再没有就按联络分享。
    """
    rule = prefilter(text)
    if rule is not None:
        return {
            "category": rule,
            "label": LABELS[rule],
            "source": "rule_fallback" if model_failed else "rule",
            "why": "判断点不可用，按确定性词表降级" if model_failed else "命中确定性词表（结构 / 绑定 / 显式命令）",
            "handoff": HANDOFF_TARGETS.get(rule, ""),
        }
    if model is not None:
        category, why = model
        return {
            "category": category,
            "label": LABELS[category],
            "source": "model",
            "why": why,
            "handoff": HANDOFF_TARGETS.get(category, ""),
        }
    return {
        "category": DEFAULT_CATEGORY,
        "label": LABELS[DEFAULT_CATEGORY],
        "source": "default",
        "why": "分不清就当她听见了",
        "handoff": "",
    }


def decide_instant(text: str) -> dict[str, Any] | None:
    """确定性判定：词表能定论就给结论（**不调模型**），给不出结论回 None。

    调用方据此决定「就此定论」还是「起一次模型判定」；`None` 表示确定性规则无法结论，
    不是「判成联络分享」——兜底仍由 `decide` 在模型不可用时给出。
    """
    if prefilter(str(text or "")) is None:
        return None
    return decide(text)
