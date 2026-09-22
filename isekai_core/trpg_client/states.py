"""TRPG 客户端的用户可见状态与文案（TRPG_CLIENT_SPEC §7）。

纯数据 + 纯函数：状态表就是规范 §7.1 那张表，客户端不创造新的行动状态、也不猜未知状态。
"""

from __future__ import annotations

from typing import Any

#: §7.1 用户可见状态表：核心状态 → 用户文案 / 可操作内容 / 禁止的客户端行为。
#: `received` 与 `interpreted` 合并显示、`snapshotting` 与 `resolving` 各自一行（规范原文如此）。
VISIBLE_STATES: dict[str, dict[str, Any]] = {
    "received": {"label": "正在理解行动", "actions": ["补充", "取消"], "forbidden": ["显示为已裁定"]},
    "interpreted": {"label": "正在理解行动", "actions": ["补充", "取消"], "forbidden": ["显示为已裁定"]},
    "awaiting_confirmation": {"label": "等待确认", "actions": ["确认", "修改", "放弃"],
                              "forbidden": ["调插件或提交世界"]},
    "confirmed": {"label": "已确认，等待裁定", "actions": ["查看", "取消（若状态仍允许）"],
                  "forbidden": ["修改成另一个 action_id"]},
    "snapshotting": {"label": "正在取得当前世界与规则状态", "actions": ["等待"],
                     "forbidden": ["重新发起随机裁定"]},
    "resolving": {"label": "规则裁定中", "actions": ["等待", "查看阶段说明"],
                  "forbidden": ["猜测骰点或结果"]},
    "reviewing": {"label": "裁定完成；玩家模式自动尝试提交，GM 模式等待主持操作",
                  "actions": ["查看摘要", "GM 可提交、拒绝或转待审"],
                  "forbidden": ["在提交成功前说成世界已经改变"]},
    "awaiting_choice": {"label": "等待玩家选择", "actions": ["只能处理对应 choice"],
                        "forbidden": ["声明下一关键行动"]},
    "awaiting_gm_review": {"label": "等待主持确认", "actions": ["主持批准、修改或拒绝"],
                           "forbidden": ["玩家端自行提交"]},
    "committing": {"label": "正在写入规则状态与世界后果", "actions": ["等待"],
                   "forbidden": ["分开重试其中一半"]},
    "committed": {"label": "结果已固化", "actions": ["查看结果、进入新局面"], "forbidden": ["再扣一次资源"]},
    "transitioned": {"label": "已进入新局面", "actions": ["声明下一行动"],
                     "forbidden": ["把旧局面继续当当前局面"]},
    "plugin_failed": {"label": "规则裁定失败", "actions": ["查看原因、显式重试 / 修改"],
                      "forbidden": ["自动重跑随机结果"]},
    "conflict": {"label": "当前状态已变化", "actions": ["重新读取、重新确认 / 裁定"],
                 "forbidden": ["套用旧 patch"]},
    "stale": {"label": "这次结果已过期", "actions": ["丢弃旧结果、重新读取场景"],
              "forbidden": ["写回旧世界"]},
    "rejected": {"label": "行动被规则或世界约束拒绝", "actions": ["修改、放弃、补充条件"],
                 "forbidden": ["改成成功叙述"]},
    "interrupted": {"label": "核心在裁定中断", "actions": ["显式恢复 / 重试"],
                    "forbidden": ["自动重跑不完整裁定"]},
}

#: 规范表没单列、但核心状态集合里有的两态：合并进相邻文案（不新造状态）
EXTRA_STATES: dict[str, dict[str, Any]] = {
    "modified": {"label": "等待确认（已修改，行动版本 +1）", "actions": ["确认", "继续修改", "放弃"],
                 "forbidden": ["用旧确认卡提交"]},
    "abandoned": {"label": "已放弃", "actions": ["重新声明行动"], "forbidden": ["把放弃当成失败骰点"]},
}

#: 空态与阻断文案（§5.1 / §5.2 / C0：waiting、blocked、paused、archived、空战役）
CAMPAIGN_STATES_COPY: dict[str, str] = {
    "preparing": "战役尚未开始：先建立首个场景或继续准备",
    "active": "战役进行中",
    "waiting": "等待选择 / 补充输入 / 主持确认——输入框不是失灵",
    "paused": "战役已暂停（主持人操作）",
    "blocked": "战役被阻断：先看原因，再等主持处理（导出或等待）",
    "archived": "战役已归档：只读",
}

#: 只读状态：不显示可提交按钮（C0 验收）
READ_ONLY_STATES = ("blocked", "paused", "archived")

#: 重试语义（§7.1 末）：两类必须分开——恢复原提交 vs 显式重新裁定
RETRY_KINDS: dict[str, str] = {
    "resume_submit": "带原幂等键，只查询和重放原提交结果",
    "reroll": "显式重新裁定：产生新的行动 revision / 新幂等身份",
}


def user_state(status: str) -> dict[str, Any]:
    """核心行动状态 → 用户可见文案。未知状态如实说未知，不猜、不套用邻近状态。"""
    key = str(status or "")
    entry = VISIBLE_STATES.get(key) or EXTRA_STATES.get(key)
    if entry is None:
        return {"status": key, "label": f"未知状态（{key or '（空）'}）——客户端版本可能落后",
                "actions": [], "forbidden": ["按已知状态推断可操作性"], "known": False}
    return {"status": key, **entry, "known": True}


def read_only(campaign_status: str) -> bool:
    """战斗状态的只读判定：blocked / paused / archived 不给提交入口（C0 验收）。"""
    return str(campaign_status or "") in READ_ONLY_STATES


def campaign_line(campaign_status: str) -> str:
    return CAMPAIGN_STATES_COPY.get(str(campaign_status or ""), f"未知战役状态：{campaign_status or '（空）'}")


def can_declare(campaign_status: str) -> bool:
    """当前能不能声明一个新的关键行动（waiting 是闸，§11.1 / C0 验收）。"""
    return str(campaign_status or "") == "active"


def next_step_copy(*, campaign_status: str, actions: list[dict[str, Any]], choices: list[dict[str, Any]]) -> dict[str, Any]:
    """当前该做什么（§5.1 / §5.2 首屏第三件事）：把闸说清楚，不靠用户猜输入框为什么没反应。"""
    pending_actions = [item for item in actions
                       if str(item.get("status") or "") not in ("transitioned", "abandoned", "rejected")]
    if choices:
        return {"next": "先处理待选择", "locks": ["新的关键行动"], "locks_reason": "战役在等待选择"}
    if str(campaign_status or "") == "waiting" and not choices:
        return {"next": "等待主持确认或补充输入", "locks": ["新的关键行动"],
                "locks_reason": "战役在 waiting"}
    if read_only(campaign_status):
        return {"next": "只读：看原因、导出或等主持处理", "locks": ["裁定", "提交", "选择"],
                "locks_reason": campaign_line(campaign_status)}
    if pending_actions:
        item = pending_actions[0]
        state = user_state(str(item.get("status") or ""))
        return {"next": f"处理未完成的行动：{state['label']}", "locks": [],
                "locks_reason": "", "action_id": str(item.get("action_id") or "")}
    return {"next": "可以声明下一行动", "locks": [], "locks_reason": ""}
