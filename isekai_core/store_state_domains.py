"""运行状态域注册表（P0：把「新增一类状态要手工改四处」收敛成一份声明）。

背景：新增一类世界状态原先要手工改 **四处**——`runtime_dump`（`store.py`）、`runtime_load`、
`timeline_clear_state` 的表清单、TRPG 的 `TRPG_COLUMNS`。其中漏掉第三处的后果最严重：
不是「没恢复到」，而是**回滚后脏数据残留**。

本模块是这份声明的**第一片**：先覆盖 `timeline_clear_state`，并用 `tests/test_state_domains.py`
锁住「新表带 `timeline_id` 就必须登记」——漏登记会让测试失败，而不是让脏数据悄悄留下。

三档登记（都要求写理由，不接受「还没想」当理由）：

- `CLEARED_ON_ROLLBACK`：回滚 / 分叉前必须清空，清空后由快照回写恢复「提交那一刻」的值。
- `KEPT_ON_ROLLBACK`：**故意不清**，并写明依据（控制状态、身份、账本等）。
- `NEEDS_DECISION`：实测存在 `timeline_id` 列、但当前**没有**任何文档说明它该清还是该留。
  这一档是**待办清单**，不是结论；每一条都要在后续修订里给出归属（并落进上面两档之一）。

维护约定：本文件与 `store.py` 的表结构同源；新增带 `timeline_id` 的表若不登记，测试直接失败。
"""

from __future__ import annotations

#: 回滚 / 分叉前清空（由快照回写恢复当时的值）
CLEARED_ON_ROLLBACK: tuple[str, ...] = (
    "unit",
    "life_plan",
    "experience",
    "claim",
    "knowledge",
    "effect_state",
    "intent",
    "event",
    "environment_state",
    "institution_state",
    "custom_state",
    # 回滚撤销还没投出去的主动消息与素材消费（SESSION_CORE §5.3 末条）
    "proactive_log",
    # 回滚撤销通告资格
    "session_notice",
    "memory",
    "memory_task",
    "memory_citation",
    # 历史里的待生效倍率不是现时控制命令（WORLD_RUNTIME_SPEC §七）
    "rate_command",
    # 回滚撤销待执行状态及其后果（EVENT_ENGINE_SPEC §八 末条）
    "pending_event",
    # 回滚同时撤销授权与依赖它的派生（§7.2）
    "disclosure",
    # 短期反应随线版本化：回滚撤销派生状态（§11.1 / 附录B#17）
    "reaction",
    # 归档态随线版本化（§六 / P1-3）：回滚后由快照回写恢复当时的值
    "character_state",
    # B-3+B-9 域外账本（2026-10-10 新增）：**必须随回滚清空**——账本数字若留着，
    # 回滚后重新推进会从「未来的账」继续，而不是从快照那一刻的账继续。
    "world_ledger",
    # B-2 关系事实层（2026-10-10 新增）：**必须随回滚清空**——关系是随线版本化的派生状态，
    # 留着会让回滚后的关系停在「未来」，与快照那一刻不一致。
    "relation_state",
    # B-1 v2 压力量增量（2026-10-10 新增）：**必须随回滚清空**——Δ 是「本批后果」的纯函数，
    # 而后果行本身随回滚清空；Δ 若留着，回滚后重新推进会把已撤销事件的增量继续算进去。
    "pressure_state",
    # B-7 取代式退休的记账（2026-10-10 新增）：**必须随回滚清空**——后果 id 是稳定标识，
    # 若旧的「被取代」记录留着，回滚后重建的同 id 后果会被它挡住（表现为后果读了却看不见）。
    # 注意它记的是「仍然有效、只是已离开热路径」的后果，与 `effect_state.active=0`（已解除）语义不同。
    "effect_superseded",
    # 叙事单元 / 暂缓标记 / 审计结果同样是派生状态（NARRATIVE_LAYER §7.2）
    "narrative_unit",
    # 战役运行时：场景 / 行动 / 选择是派生编排状态；战役与规则状态由快照回写给出提交那一刻的值
    # （TRPG_CAMPAIGN_RUNTIME_SPEC §十七）
    "trpg_scene",
    "trpg_action",
    "trpg_choice",
    "trpg_campaign",
    "trpg_rule_state",
    "trpg_commit",
)

#: 回滚**故意不清**，附带依据；「依据」字段不允许为空
KEPT_ON_ROLLBACK: dict[str, str] = {
    # 「初见已完成」记号属控制状态，不随回滚倒退（SESSION_CORE §5.6）
    "first_contact": "控制状态：初见记号不随回滚倒退（SESSION_CORE §5.6）",
    # 成员资格记录要留着并转为 revoked；回滚后再次补入必须用新的加入版本，
    # 不能复活被撤销的旧记录——撤销由回滚流程显式执行（WORLD_SETTING_SPEC §3.7 末条）
    "character_join": "身份/成员资格：撤销由回滚流程显式执行，不能静默复活旧记录（§3.7）",
    # 时钟不是被版本化的状态：回滚后重新锚定现实时间，不能复用历史基准追赶（§2.6）
    "timeline_clock": "时钟：回滚后重新锚定，不复用历史基准（§2.6）",
    # 提交历史本身：回滚要保留「曾经提交过什么」
    "commit_log": "提交历史：回滚的输入，不是被回滚的状态",
    "commit_auto_state": "自动提交节流状态：控制状态（§5.3）",
    # 调用预算账本按**现实日**记账，属运行控制而非世界状态（WORLD_RUNTIME_SPEC §2.8）
    "call_ledger": "调用预算账本：按现实日记账，属控制状态（§2.8）",
    "budget_reserve": "调用预算预占：同上（§2.8）",
    "session": "会话身份：绑定 (实例, 线, 角色)，不是派生状态",
    "event_draft": "管理面草稿：用户引入事件的待确认草案，属控制状态（EVENT_ENGINE_SPEC §八）",
}

#: 实测带 `timeline_id` 但**当前无文档依据**判断该清还是该留 —— 待办，不是结论
NEEDS_DECISION: tuple[str, ...] = (
    # 说法覆盖/引用记录（claim.coverage）：看起来是派生，但没有任何 SPEC 说明它随不随回滚
    "claim_coverage",
    # 通告队列：与已清的 `session_notice` 名字相近，但用途待核
    "notice",
    # Writing Assistant 的候选与状态：WA 明确「不拥有世界事实」，但它按线版本化到什么程度待核
    "wa_candidate",
    "wa_state",
)

#: 按线清空的两张向量表（`memory_embedding` 有「按线」与「按 memory 归属」两种历史形态）
VECTOR_TABLES_CLEARED_BY_LINE: tuple[str, ...] = ("memory_embedding",)

#: `Store.runtime_dump` 实际产出的分节名（2026-10-10 对真实例调用核对；`watermark` 是水位标量，不是域）。
#: **守卫用法**：`tests/test_state_domains.py` 断言它等于运行时真实产出的键集合——
#: 于是「加了一个快照分节却没在这里登记」会立刻变红，而不是等到导出 / 导入才发现少了一段。
SNAPSHOT_SECTIONS: tuple[str, ...] = (
    "character_states",
    "characters",
    "citations",
    "claims",
    "customs",
    "disclosure",
    "effects",
    "environment",
    "events",
    "experiences",
    "institution",
    "intents",
    "knowledge",
    "memories",
    "memory_tasks",
    "narrative",
    "plans",
    "reactions",
    "trpg_actions",
    "trpg_campaigns",
    "trpg_choices",
    "trpg_commits",
    "trpg_rule_states",
    "trpg_scenes",
    "units",
    "watermark",
)

#: 快照分节里**不是状态域**的标量（水位）；守卫比较时会把它一起算进来，便于逐字比对
NON_DOMAIN_SECTIONS: frozenset[str] = frozenset({"watermark"})


def cleared_tables() -> tuple[str, ...]:
    """回滚要清的表（顺序即执行顺序，保持与历史行为一致）。"""
    return CLEARED_ON_ROLLBACK


def known_tables() -> set[str]:
    """已登记的、带 `timeline_id` 的表全集（测试用：新表不登记即失败）。"""
    return (
        set(CLEARED_ON_ROLLBACK)
        | set(KEPT_ON_ROLLBACK)
        | set(NEEDS_DECISION)
        | set(VECTOR_TABLES_CLEARED_BY_LINE)
    )
