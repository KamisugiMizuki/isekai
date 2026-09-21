# TRPG 规则层与 WorldRuntime 适配审查

> 状态：初审报告，2026-09-22
> 范围：`D:\TRPG` 规则资料、当前 WorldRuntime 设计、TRPG 规则层设计、Terra v1.2 示例插件与实际代码。
> 性质：审查记录，不是兼容性承诺，也不代表后续能力已经实现。

## 结论

当前 WorldRuntime 可以作为 TRPG 的世界事实、时间、认知、事件后果和版本底座，但不能完全满足 CoC、D&D、Fate、行于泰拉、Wilderfeast 等规则的运行需求。

核心缺口不是缺少某几个效果类型，而是当前分层缺少一层正式的 **TRPG Campaign Runtime**，以及规则私有状态的持久化、版本化和原子提交边界。

建议目标结构：

```text
WorldRuntime
  世界事实、时间、认知、版本、通用世界后果

TRPG Campaign Runtime
  战役、场景、遭遇、参与者、行动生命周期、回合、受众、待选择

具体规则插件
  属性、技能、骰点、资源、职业、规则状态、战斗与成长裁定

GM / Narrative 表达层
  把已提交结果表达为玩家可理解的新局面
```

当前结构更接近：

```text
WorldRuntime + 一次性规则骰点插件
```

它能验证“规则插件返回结果并写入世界事件”，还不能验证“完整规则战役持续运行”。

## 一、审查依据

### 规则资料

`D:\TRPG` 下已盘点：

- CoC 调查员手册、守秘人规则书；
- D&D 5E 玩家手册、城主指南、怪物图鉴；
- Fate Atrous Grail 核心规则、英灵、魔术师、圣杯战争、监督者手册及角色表；
- 行于泰拉 v1.2 规则书、角色创建指南、角色卡、战斗职业表、源石技艺表；
- Ventangle；
- Wilderfeast；
- 多份角色卡与 Excel 资料。

DOCX、PDF、XLSX 均已完成本地文本或结构盘点。抽取中间文件位于 `.hermes/trpg_extract/`，属于临时审查产物。

### 项目依据

- `docs/worldruntime/DESIGN.md`
- `docs/worldruntime/WORLD_RUNTIME_SPEC.md`
- `docs/worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md`
- `docs/trpg-rules/TRPG_RULE_PLUGIN_SPEC.md`
- `docs/trpg-rules/TRPG_RULES_LAYER_SPEC.md`
- `docs/trpg-rules/TRPG_RULE_COMMON_MODULE_SPEC.md`
- `isekai_core/runtime/rules.py`
- `isekai_core/world/ops.py`
- `isekai_core/runtime/service.py`
- `isekai_core/world/validate.py`
- `examples/terra_v12_rules_plugin/terra_v12_rules.py`
- `tests/test_terra_v12_plugin.py`
- `tests/test_trpg_rules.py`

## 二、已经满足的部分

现有 WorldRuntime 已具备以下适合 TRPG 的底座能力：

- 世界时钟、时间线、倍率和离线补算；
- 事件、事实效果、说法和认知传播；
- 角色经历、记忆和认知过滤；
- 提交、分叉、回滚和运行世代；
- 环境状态、制度状态和文化惯例状态；
- 外部事件注入；
- 规则插件独立进程边界；
- 原始裁定结果的保存；
- 基于 `action_id` 的幂等提交。

实际代码中的外部规则路径为：

```text
trpg.action.resolve
  -> runtime.rules.resolve
  -> 外部插件进程
  -> drafts.normalize_draft
  -> RuntimeService.apply_external_event
  -> Store.apply_runtime_batch
```

`RuntimeService.apply_external_event()` 会将事件、claims、knowledge、effects、环境和制度状态放入一次运行层批提交。它可以承载“规则结果造成世界后果”的尾部。

项目 venv 实测：

```text
4 passed in 0.26s
```

覆盖了 Terra 插件进程调用、管理面调用、真实 SQLite 写入和重复 `action_id` 幂等。该结果只证明最小外部事件链路，不证明完整规则运行时。

## 三、当前接口与代码的错位

当前工作树中的规则协议文档已经使用 `consequences`，但实际代码和测试仍消费 `effects`：

- `isekai_core/world/ops.py` 从插件结果读取 `resolution["effects"]`；
- `isekai_core/runtime/rules.py` 要求返回对象包含 `effects` 数组；
- Terra 示例插件返回 `effects`；
- `tests/test_trpg_rules.py` 也按 `effects` 验收。

因此文档契约与真实调用契约尚未收敛。这不是简单的字段重命名，因为它反映出“规则公共后果包”尚未成为稳定边界。

当前实现还把原始规则结果放进事件 `detail`：

```python
rows["event"]["detail"] = json.dumps({"resolution": resolution})
```

规则结果中的资源消耗、参与者关系、行动节拍、规则状态变化、当前场景和待处理反应没有独立的持久化归属。

## 四、Terra 暴露出的真实缺口

### 1. 规则角色状态没有正式归属

行于泰拉的角色创建和运行状态包括：

- 七项基础属性；
- 技能和技能流派；
- 特质、修正因子、绑定和互斥；
- 特质点和社交点数；
- 经济评级；
- 感染状态与感染值；
- 生命值、技力、体力等战斗资源；
- 战斗职业；
- 源石技艺；
- 装备与配件；
- 成长与幕间调整。

这些字段会影响后续规则裁定，并且会在战役中变化。它们不属于 WorldRuntime 的性格单元、生活线或普通世界效果，也不能只作为一次调用的 `context` 传入后丢弃。

当前 Terra 插件实际采用的是一次性纯计算：调用方把 `skill`、`attribute`、`face`、`resistance`、`cost` 等数值塞进 `context`，插件计算后返回结果。它没有持久化角色规则状态、战役状态、成长、装备或状态触发。

### 2. Terra 战斗需要场景和遭遇运行时

规则书定义了战术格、坐标、高度、地形、透明性、障碍性、可通行性、单位、召唤物、地形单位、阵营、回合、轮次、动作、资源消耗、反应和状态。

典型动作流程是：

```text
选择动作
→ 选择目标
→ 检查条件
→ 消耗资源
→ 宣言动作
→ 触发反应
→ 结算效果
```

当前 WorldRuntime 的事件提交只能表达“某个世界效果已经发生”，不能表达行动在第几轮、哪个行动窗口、哪个战术格、受哪些地形和反应影响。继续增加几个 `effect kind` 不能替代场景、遭遇和回合模型。

### 3. 当前效果闭集不足，但不应直接把 Terra 字段塞进核心

现有事实效果闭集主要是：

```text
source_delay
route_blocked
activity_constraint
public_notice
rumor_spread
institution_state
custom_state
environment_state
```

Terra 还需要伤害、资源消耗、位置变化、状态施加 / 解除、地形破坏、召唤物、装备变化、感染变化、死亡和条件触发。

这些确实说明当前公共变化能力不足，但解决方式不是把 HP、SP、感染、职业和 Terra 状态加入 WorldRuntime。正确拆分是：

```text
规则私有状态：规则插件持有并由核心托管版本
世界后果：结构化提交给 WorldRuntime
```

例如 Terra 法术可以同时产生：

```text
rule_state_patch:
  角色消耗 2 点 SP

world_consequence:
  仓库进入着火状态
```

WorldRuntime 不需要知道 SP 的语义，但必须保证规则状态变化和世界后果在同一提交边界内成功或失败。

## 五、跨规则共性

对 CoC、D&D、Fate、Terra、Wilderfeast 和 Ventangle 的资料进行对照后，稳定的共性不是“都有 HP / 属性 / 回合”，而是以下六类边界。

### 1. 规则角色状态

每套规则都有自己的基础数值、衍生数值、能力、资源、装备、条件和成长。字段不同，但都需要在战役中被读取、修改、恢复和版本化。

WorldRuntime 不应理解这些字段，但系统必须给规则插件一个可靠的状态归属。

### 2. 行动生命周期

不同规则都可抽象为：

```text
action declared
→ action confirmed
→ preconditions checked
→ resources reserved / consumed
→ resolution
→ reactions / triggers
→ consequences
→ state commit
→ next legal action
```

当前 `resolve_action` 把这些阶段压成一次调用，无法表达用户修改 / 放弃行动、等待玩家选择、反应窗口、跨回合行动和部分提交失败。

### 3. 资源与消耗

资源可以是 HP、MP、SP、Stamina、Luck、Fate Point、令咒、法术位、行动次数、特质点、经济资源或进度时钟。它们都有当前值、最大值、消耗来源、恢复方式和持续范围。

当前 WorldRuntime 没有规则资源模型。`resource_change` 只出现在接口草案中，尚未成为实际可提交能力。

### 4. 条件与状态

规则状态通常需要持有者、施加者、开始时刻、有效期、触发条件、清除条件、叠加策略、抗性和可见范围。Terra 的引导、飞行、流血、隐匿、眩晕、禁疗等不能只用世界事件的 `expiry` 表示。

### 5. 场景和行动空间

场景是当前参与者、地点、公开信息、私密信息、危险、可行动作和等待选择的可行动投影，不是 WorldRuntime 世界事实的第二份副本。现有设计有快照和认知投影，但没有 TRPG 场景 / 遭遇契约。

### 6. 规则时间与世界时间

战斗轮、回合、阶段、调查时间、休息和旅行不应自动等于世界秒。需要独立的规则节拍，并明确哪些规则结果会映射成世界时间推进。

## 六、Fate、CoC、D&D 和 Wilderfeast 的补充证据

Fate Atrous Grail 进一步证明，规则状态不仅是数值：令咒、御主与从者契约、职阶、宝具准备、宝具点数、MP 上限、资格和信息暴露都需要持续状态。角色是否知道真名、职阶和宝具信息则属于规则状态与认知状态的交界，不能只靠普通 claims 文本解决。

CoC 会增加 SAN、临时 / 不定性疯狂、线索、伤口和心理状态。D&D 会增加等级、职业资源、法术位、反应、Bonus Action、专注、临时生命值、条件、休息和死亡豁免。Wilderfeast 则说明规则运行不应被设计成只服务战斗，它还涉及狩猎、旅途、食材、烹饪、Stamina、Durability 和环境探索。

共同结论是：规则层的核心不是“把骰子接到世界事件”，而是持续维护“角色行为、规则资源、场景条件和结果后果”的闭环。

## 七、建议的分层

### WorldRuntime 负责

- 世界地点、环境和实体；
- 世界事实、事件和可持续后果；
- 世界时间与水位；
- 角色在世界中的位置；
- 认知、说法和传播；
- 版本、分叉、回滚和世代失效；
- 规则状态附件的版本边界与原子提交协调。

### TRPG Campaign Runtime 负责

- 战役；
- 玩家、玩家角色、NPC 和召唤物的参与关系；
- 场景和遭遇；
- 当前规则节拍、回合和阶段；
- 行动声明、确认、放弃和待选择；
- 受众和 GM 私有 / 玩家可见信息；
- 规则裁定记录；
- 规则状态快照句柄；
- 规则状态变化与世界后果的联合提交。

### 具体规则插件负责

- 属性、技能、职业和等级；
- 骰点、难度、优势 / 劣势和结果分级；
- 规则资源、装备、能力和状态；
- 规则角色成长；
- 规则专属战斗、调查、狩猎或旅途裁定；
- 规则私有重放数据。

### GM / Narrative 表达层负责

- 解释行动如何被理解；
- 表达裁定依据和结果；
- 描述已经提交的世界变化；
- 给出新的可行动局面；
- 遵守角色认知、玩家视角和 GM 私有信息边界。

## 八、建议的接口修订

### 1. 规则状态附件

需要在 `WORLD_RUNTIME_INTERFACE_SPEC` 中增加规则状态命名空间和生命周期：

```text
instance_id
 timeline_id
 campaign_id
 ruleset_id
 ruleset_version
 state_revision
 opaque_state
```

WorldRuntime 不解析 `opaque_state`，但负责它的读取、持久化、版本、分叉、回滚、导出、恢复、幂等和世代检查。

### 2. 变化提交拆分

规则结果至少应分成：

```text
resolution_record
  规则私有裁定原文

rule_state_patch
  规则插件自己的状态变化

world_consequence_bundle
  交给 WorldRuntime 的世界后果

scene_transition
  当前场景、待选择和行动窗口变化
```

规则私有状态不能伪装成 WorldRuntime effect，世界后果也不能只存在规则插件进程内。

### 3. 插件形态分级

当前“一行 JSON 请求 → 一行 JSON 响应 → 进程退出”的模型适合无状态 resolver。完整战役还需要定义状态快照 / patch 协议，是否长期驻留进程可以后置，但状态生命周期不能后置。

### 4. 新增独立规范

原审查曾提出新增 `TRPG_CAMPAIGN_RUNTIME_SPEC.md`，并明确不把战役、场景、回合和规则状态直接塞进 `WORLD_RUNTIME_SPEC` 或硬编码到 Terra 插件。该建议已在本轮落实为独立规范。

## 九、差距表

| 能力 | 当前状态 | 判断 |
|---|---|---|
| 世界时间 | 已有 | 基本满足 |
| 世界事件与认知 | 已有 | 基本满足 |
| 世界后果闭集 | 题材世界效果为主 | 不足 |
| 规则角色状态挂载 | 无正式契约 | 核心缺口 |
| 规则资源 | 无实际通用实现 | 核心缺口 |
| 规则状态触发 | 无规则级生命周期 | 核心缺口 |
| 场景 / 遭遇 | 无 | Campaign Runtime 缺口 |
| 行动生命周期 | 单次 resolve | 不足 |
| 规则时间 | 无独立模型 | 缺口 |
| 规则状态恢复 | 无 | 核心缺口 |
| 规则状态与世界后果原子提交 | 无正式契约 | 核心缺口 |
| 插件进程边界 | 已有 | 可保留 |
| 原始裁定保存 | 已有，但落在事件 detail | 部分满足 |
| action_id 幂等 | 已有并实测 | 基本满足 |
| 认知隔离 | 已有 | 可复用 |
| GM / 玩家受众 | 有基础，但无战役契约 | 部分满足 |

## 十、最终判断

Terra 插件无法实现的部分，确实有相当一部分来自当前 WorldRuntime 设计缺能力；但解决方案不是让 WorldRuntime 直接增加 Terra 的属性、技能、战斗和状态。

真正需要补齐的是：

1. 规则私有状态的正式托管与版本生命周期；
2. TRPG Campaign Runtime；
3. 规则状态与世界后果的原子协调；
4. 稳定的 `resolution / state_patch / consequences / scene_transition` 边界。

在这四项明确之前，继续扩充 `SUPPORTED_EFFECTS` 或继续堆叠 `trpg.action.resolve`，只能得到一个“把规则结果包装成世界事件”的外壳，不能得到可持续运行的 TRPG 战役。

## 十一、本轮设计收敛结果

本轮已新增并完成 [`TRPG_CAMPAIGN_RUNTIME_SPEC.md`](TRPG_CAMPAIGN_RUNTIME_SPEC.md)，原审查中“Campaign Runtime 尚未完成设计”的缺口已关闭为设计项，具体覆盖：

- 持久化归属与三类真值边界；
- 战役、行动和待选择状态机；
- 规则状态附件和 revision；
- `trpg.commit` 联合提交请求、校验顺序、结果和幂等；
- 重启、导入、分叉、回滚后的场景恢复；
- 规则时间与世界时间的分离；
- GM / 玩家 / 角色受众隔离；
- 规则版本兼容和状态转换器；
- 审计字段、最小实现顺序与行为验收。

因此当前账目应区分为：

| 项目 | 设计状态 | 实现状态 |
|---|---|---|
| Campaign Runtime 边界 | 已完成设计 | **已实现**（战役 / 场景 / 行动 / 待选择 + 状态机 + `trpg.commit` 联合提交，`tests/test_trpg_campaign.py` 9 项 + CLI 探针） |
| 规则状态附件 | 已完成接口设计 | **已实现**（`trpg_rule_state`，不透明字段 + revision 并发；随提交 / 回滚 / 分叉 / 导出导入） |
| 规则状态与世界后果联合提交 | 已完成协议设计 | **已实现**（同一个 `apply_runtime_batch` 事务，任一步非法整批不落盘） |
| 场景 / 行动 / choice 状态机 | 已完成状态设计 | **已实现**（非法迁移报出合法去向） |
| Terra 完整规则插件 | 仍待具体实现 | 当前只有 B0 无状态 resolver + 假插件验证；真 Terra 属性 / 技能 / 感染 / 战术格未落 |
| 规则版本转换器 / 世界时间请求执行 / 待选择自动续接 | 设计已定型 | 未实现（见 `TRPG_CAMPAIGN_RUNTIME_SPEC.md` §二十一） |

本报告仍保留“初审”性质；它记录问题如何被发现和归因，正式设计以各 SPEC 的当前版本为准。
