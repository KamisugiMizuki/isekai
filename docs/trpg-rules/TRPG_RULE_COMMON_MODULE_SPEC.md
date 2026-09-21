# TRPG 规则共用模块设计

> 状态：设计规范草案 v1.0，未实现声明。
> 上游：[`TRPG_RULE_PLUGIN_SPEC.md`](TRPG_RULE_PLUGIN_SPEC.md)、[`../worldruntime/DESIGN.md`](../worldruntime/DESIGN.md)。
> 使用方：[`TRPG_RULES_LAYER_SPEC.md`](TRPG_RULES_LAYER_SPEC.md)。

## 一、定位

TRPG 规则共用模块位于具体规则转接插件与 WorldRuntime 之间。它不试图把 CoC、D&D、PBTA、Forged in the Dark 或自定义规则转换成同一套规则，而是把不同规则产生的**已确认世界后果**包装成 WorldRuntime 可以验证、固化和传播的通用变化请求。

```text
玩家输入 / GM 规则输入
  -> 某套规则独占转接插件
  -> TRPG 规则共用模块
  -> WorldRuntime 变化校验与原子提交
  -> 角色认知 / 场景 / GM 表达
```

公共模块的最小承诺是：

- 不丢失规则私有裁定；
- 不让 WorldRuntime 理解规则私有字段；
- 不把规则文本或叙事文本直接当成世界事实；
- 不把“规则上成功”误认为“世界变化已提交”；
- 同一世界变化使用同一套事实、认知、时间线和版本语义；
- 不把规则私有状态变化与世界后果拆成两个可独立成功的写入。

公共模块处理四种结果材料：

```text
resolution_record
rule_state_patch
world_consequence_bundle
scene_transition
```

其中 `resolution_record` 和 `rule_state_patch` 由规则插件定义，公共模块只验证其命名空间、版本引用和提交边界；`world_consequence_bundle` 才进入 WorldRuntime 的世界变化校验；`scene_transition` 留在 TRPG Campaign Runtime。
## 二、为什么不能设计一个万能规则模型

不同规则的差异不只在骰子表达式：

| 差异 | 例子 | 公共层处理方式 |
|---|---|---|
| 行动单位 | 一次声明、一个动作点、一个场景承诺 | 保留 `action_ref` 与原始节拍，不统一成回合 |
| 结果结构 | 成功等级、位置 / 效果、进展时钟、资源交换 | 保存规则私有 `resolution`，只提取已确认后果 |
| 代价 | 伤害、压力、厄运、线索暴露、时间消耗 | 映射为可验证的通用变化意图 |
| 随机性 | d100、骰池、牌、无骰协商、GM 裁定 | 作为规则审计材料保留，不进入世界真值模型 |
| 角色能力 | 技能、标签、职业特性、叙事权限 | 由插件解释，不要求 WorldRuntime 认识名称 |
| 场景节奏 | 回合制、自由行动、钟表、阶段推进 | 以 `time_advance` 和 `phase_ref` 表达，不强加回合 |
| 结果确定性 | 确定、概率、候选、需玩家选择 | 用确认状态和不确定性标记区分 |

因此公共层不输出“所有规则都必须拥有”的属性、生命、技能、回合或骰点对象。它只处理世界交界面。

## 三、公共层的四层对象

### 3.1 原始裁定记录

原始裁定记录由规则插件提供，公共层原样保存，不解析规则私有字段：

- 规则系统标识与版本；
- 插件版本与转接器版本；
- 原始行动和行动者；
- 规则私有 resolution；
- rolls、牌面、资源消耗或 GM 裁定依据；
- 随机种子 / 重放信息（若该规则支持）；
- 规则私有状态变化。

这部分用于复盘和重放，不能直接成为 WorldRuntime 的事实。

### 3.2 规则状态 patch

规则插件可以返回自己的规则状态变化：

```text
ruleset_id
campaign_id
base_state_revision
operations[]
```

公共模块不解释 `operations[].path` 的规则语义，只检查：

- 命名空间属于当前插件；
- base revision 与读取快照一致；
- patch 没有写入 WorldRuntime 世界事实；
- patch 与必要世界后果进入同一联合提交；
- 规则版本不兼容时拒绝提交。

规则状态不是世界事实的第二份副本。世界地点、公共事件、认知和持续世界后果必须进入 WorldRuntime。

### 3.3 通用后果包

转接插件从原始裁定中明确声明本次已经成立的世界层后果。每个后果至少包含：

```text
consequence_id
kind                 # 公共效果闭集中的类型或待审批类型
subject / target     # 世界实体引用
operation            # create / set / change / add / remove / reveal / advance
value                # 结构化值，不接受未声明的自由事实文本
certainty            # confirmed / uncertain / candidate
visibility           # audience / observer scope
effective_time       # 发生时间、持续时间、过期条件
cause_refs           # action_ref / resolution_ref / event_ref
source               # rule adjudication / gm declaration / world process
```

`certainty=candidate` 的内容只能进入候选或待批准状态，不能直接提交为世界事实。`confirmed` 也必须通过 WorldRuntime 的目标、类型、权限、时间和因果校验。

### 3.4 认知与表达材料

规则插件可以声明哪些参与者因此获得、失去或更新了哪些认知，但不能直接写角色台词。公共层把它们整理为 WorldRuntime 可消费的 claims / perception / disclosure 请求；角色表达仍由 OC 或 GM 表达层生成。

同一个后果可以有不同观察者和不同说法：事实层只提交一次，认知层按角色视角传播。

### 3.5 提交请求

公共层最终生成 WorldRuntime 请求：

```text
origin
  -> action_ref
  -> source_plugin / source_mode
  -> raw_resolution_ref
  -> rule_state_patch?
  -> consequences[]
  -> knowledge_changes[]
  -> scene_transition?
  -> time_advance?
  -> expected_revision
  -> expected_state_revisions
  -> idempotency_key
```

提交协调器必须保证 `rule_state_patch` 与必要世界后果同批提交。
WorldRuntime 负责最后裁决。公共层不能自行补齐缺失目标、把候选升格为确认、修复不合法效果或绕过版本冲突。

## 四、规则独占转接插件

每套规则拥有自己的转接插件。插件不是简单字段映射，而是该规则与公共后果包之间的责任边界。

插件负责：

1. 接受用户 / GM 的规则语义输入；
2. 使用该规则自己的角色态、场景态和裁定方法；
3. 生成完整的原始裁定记录；
4. 明确声明哪些结果已经发生、哪些只是候选；
5. 将已成立的结果转换为公共层可验证的后果包；
6. 说明无法映射的规则后果及其所需人工确认。

插件不得：

- 直接写 WorldRuntime 数据；
- 把任意规则私有数值塞进公共字段；
- 用自由文本 claims 代替结构化目标、操作和时间；
- 在无法理解世界实体时自行创建同名实体；
- 把玩家尚未选择的分支作为已发生后果提交。

## 五、公共后果词汇

公共词汇必须足够小，能覆盖世界层变化，同时不绑定具体规则。首版只建议支持以下类别：

| 类别 | 含义 | 例子 |
|---|---|---|
| `state_change` | 实体已有状态的结构化改变 | 门锁定、角色受伤、关系变化 |
| `resource_change` | 世界资源的增减、转移或消耗 | 弹药减少、现金转移 |
| `relation_change` | 实体之间关系或立场改变 | 组织信任下降 |
| `knowledge_change` | 某观察者获得、失去或更新认知 | 角色知道墙后有通道 |
| `world_event` | 已发生的可追溯事件 | 警报被触发 |
| `condition` | 有起止或清除条件的持续效果 | 中毒、追踪、警戒 |
| `location_change` | 实体位置或场景归属改变 | 角色进入房间 |
| `time_advance` | 世界时间或局部允许的时间推进 | 消耗十分钟 |
| `clock_progress` | 具有明确所有者和刻度的世界进度 | 警戒时钟推进一格 |
| `player_choice` | 尚未决定的分支，不是世界变化 | 是否追踪逃犯 |

`player_choice` 只能作为新的场景入口返回，不得进入已发生效果。`clock_progress` 必须引用已有时钟，不允许插件凭空制造无主进度系统。

如果某个规则的后果无法落入这些类别，公共层返回 `needs_mapping`，由规则设计者明确扩展或转为 GM 审批；不使用一个万能 `custom_effect` 逃避设计。

## 六、校验与提交顺序

```text
原始裁定记录
  -> 转接插件声明通用后果
  -> 公共层检查结构、引用、确定性和可见性
  -> WorldRuntime 检查目标、效果闭集、版本和权限
  -> 原子提交世界事件 / 效果 / 认知 / 时间
  -> 返回 committed / rejected / needs_review
```

任何一个必要后果非法，整次提交拒绝或进入待审，不落半条状态。`needs_review` 不是失败骰点，而是表示规则与世界边界之间没有足够明确的映射。

提交结果至少区分：

- `committed`：世界变化已经固化；
- `rejected`：违反世界约束或版本条件，没有变化；
- `needs_review`：规则插件声明不完整或需要主持人确认；
- `duplicate`：同一幂等键已处理，返回原提交结果。

## 七、GM 直接变化与规则裁定的分离

GM 输入“守卫已经离开”“城门被毁”时，不应伪装成玩家行动，也不必强行调用骰点插件。公共层把它标记为 `source=gm_declaration`，经过同样的结构化后果、受众、时间、权限和版本校验后提交。

若 GM 输入的是“玩家尝试撬锁”，则仍走对应规则转接插件。若 GM 输入的是“玩家掷骰结果为成功，请应用后果”，可以走插件的复核 / 应用路径，但必须保留它是 GM 指定裁定的来源，不伪造随机记录。

## 八、跨规则兼容的真实边界

公共层保证的是“世界交界面兼容”，不是“规则语义互换”。因此：

- 可以把不同规则的伤害、资源消耗、位置变化和认知暴露提交到同一世界；
- 不能把 CoC 的技能百分比直接转换成 D&D 的技能加值；
- 不能期待 PBTA 的叙事结果自动拥有另一套规则的精确数值；
- 同一角色跨规则切换时，必须由新规则插件明确建立规则角色态；
- WorldRuntime 保存世界后果，规则插件保存本规则重放所需的私有状态。

## 九、版本与演进

公共层协议版本只在世界交界面发生破坏性变化时升级。新增规则私有字段留在插件自己的 `resolution` 或 `context` 内。

第二个真实规则插件是公共层的最低验证门槛。它必须证明：

- 不需要 WorldRuntime 理解它的骰点和角色属性；
- 能表达成功、失败、代价、部分结果和待选择分支；
- 能声明不同观察者的认知变化；
- 规则时间与世界时间不一致时不会强行覆盖；
- 无法映射的结果能安全进入 `needs_review`。

## 十、设计不变式

1. 规则私有裁定永远可回溯，但永远不是 WorldRuntime 事实本身。
2. 公共层只转换世界交界面，不统一不同 TRPG 的内部规则。
3. 每个后果都有结构化目标、操作、时间、受众和因果来源。
4. 候选、未知和需审批内容不能直接提交为事实。
5. GM 直接变化与规则裁定共享校验和提交，但保留不同来源。
6. 规则无法映射时进入 `needs_review`，不使用自由文本万能字段。
7. WorldRuntime 是唯一的世界变化提交者。
8. 新规则插件不能迫使已有规则插件共享属性、回合或资源模型。

## 十一、行为验收

- 两个真实规则插件能用完全不同的规则态返回各自的原始裁定。
- 两个插件都能将成功、失败、代价和待选择分支转换为公共后果包。
- WorldRuntime 不读取或依赖任一插件的规则私有字段。
- 规则私有结果完整保留，公共后果只提交结构化世界变化。
- 不合法目标、候选升格、版本冲突和未知自定义效果不会落库。
- 同一行动重试返回同一提交结果，不重复施加后果。
- GM 直接声明可在不伪造骰点的情况下提交结构化变化。
- GM 声明与玩家行动产生的世界效果经过同一套世界校验、认知传播和版本语义。
