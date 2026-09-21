# TRPG 规则共用模块设计

> 状态：已定稿 v1.0（规则到世界的边界契约）；实现状态：独立公共模块仍属设计义务，当前部分校验由 Campaign Runtime / WorldRuntime 共同承接。
> 上游：[`TRPG_RULE_PLUGIN_SPEC.md`](TRPG_RULE_PLUGIN_SPEC.md)、[`TRPG_CAMPAIGN_RUNTIME_SPEC.md`](TRPG_CAMPAIGN_RUNTIME_SPEC.md)、[`../worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md`](../worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md)。
> 使用方：[`TRPG_RULES_LAYER_SPEC.md`](TRPG_RULES_LAYER_SPEC.md)。
>
> 本文是规则私有结果进入世界变化边界的唯一规范。它不拥有规则裁定、战役生命周期或世界提交；它只定义如何规范化、拒绝和转交。

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
kind                 # 变化意图闭集中的类型
subject_refs[]?
target_refs[]?
operation            # create / set / change / add / remove / reveal / advance
value                # 已登记类型允许的结构化值
certainty            # confirmed / uncertain / candidate
visibility           # 闭集受众 / observer scope
effective_time?      # 发生或生效时刻
expiry / clear_when?
cause_refs[]         # action / resolution / event / gm declaration
source_mode          # action / gm_declaration / world_process / npc_script
```

`certainty=candidate` 或 `uncertain` 的内容只能进入候选 / 待批准状态，不能直接提交为世界事实。`confirmed` 也必须通过 WorldRuntime 的目标、类型、权限、时间和因果校验。

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

### 3.6 规范化输出

公共层把插件响应收敛成一个供 Campaign Runtime / 联合提交协调器消费的结果，不直接提交数据库。规范化结果至少包含：

```text
normalized_result
  status: ready | rejected | needs_review
  origin: instance / timeline / campaign / action / source_mode
  raw_resolution: 原始 resolution 的引用或原样记录
  rule_state_patch?
  changes[]             # WorldRuntime change_intent
  claims[]              # 说法 / 获知材料，不是事实本身
  scene_transition?     # 只给 Campaign Runtime
  world_time_request?   # 通过独立时间消耗入口处理
  errors[] / warnings[]
```

规范化顺序固定为：

1. 识别结构化插件错误；错误响应不得携带可采信的 patch、effects 或 consequences；
2. 检查 `resolution`、`rule_state_patch`、`consequences` / B0 `effects`、`claims`、`scene_transition` 的形状；
3. 保留原始裁定，不解析其规则私有字段；
4. 将每个已确认后果转成带来源、受众、时间和目标引用的 `change_intent`；
5. 将候选 / 未确认 / 无法映射项分别放入 `needs_review`，不能静默丢弃或升格；
6. 把规则状态 patch、世界变化和场景转换交回同一联合提交边界。

`source_mode`、`action_ref`、`campaign_id`、`expected_revision` 和 `idempotency_key` 由宿主路径提供，插件不能靠响应自报来源或版本来取得写权限。公共层可以拒绝结构不完整的结果，但目标是否存在、效果是否属于当前世界包闭集、时间是否可推进，最终由 WorldRuntime 判断。

### 3.7 两种兼容输入

完整战役路径使用 `consequences`；B0 无状态 resolver 保留 `effects` 作为兼容输入。两者都必须经过同一套确定性、受众、来源和 WorldRuntime 校验。B0 没有规则状态 patch、战役场景转换或持续行动生命周期，不能借兼容字段伪装成完整战役结果。

### 3.8 字段与时间规则

规范化时不得依赖插件自由发挥。每个 `change_intent` 至少满足：

```text
id                 # 在本次提交中稳定，用于错误定位与审计
kind               # 变化意图闭集
subject_refs[]?
target_refs[]?
operation          # create / set / change / add / remove / reveal / advance
value              # 已声明结构化值
certainty          # confirmed / candidate / uncertain
visibility         # 闭集受众或观察者范围
effective_time?    # 发生 / 生效时刻
expiry / clear_when?
cause_refs[]        # action / resolution / event / gm declaration
source_mode         # action / gm_declaration / world_process / npc_script
source_module       # 产生该变化的上层模块
```

规则如下：

- `candidate`、`uncertain` 只能返回 `needs_review` / 预览材料；它们不能进入事实提交。
- `confirmed` 只表示插件或 GM 声明它已成立，不能跳过 WorldRuntime 的目标、效果、时间和因果校验。
- `visibility` 必须能映射到 Campaign Runtime / WorldRuntime 的受众闭集；自由字符串不作为“公开”处理。
- `effective_time` 不能晚于未完成的世界水位，也不能把未来计划伪装成已发生；预约或时间消耗走独立时间语义。
- `expiry` / `clear_when` 必须符合目标效果的失效方式；公共层不替后果补一个默认清除条件来让它通过。
- `cause_refs` 至少要能回到行动、裁定、GM 声明或世界过程之一；缺少因果来源的后果进入 `needs_review`。
- 一条后果的结构化 `value` 只能承载已登记类型允许的值，不能把自然语言背景塞进值字段成为隐式事实。

### 3.9 claims、认知与表达的边界

`claims` 是说法或获知材料，不等于它描述的事实。公共层可以保留说法文本、来源渠道、受众和确信信息，但不能用 claim 文本反向创建未登记实体、效果、因果或幕后动机。观察者是否真正获知、何时获知和如何呈现，由 WorldRuntime 的传播 / 认知链决定；角色台词由 OC / GM 表达层决定。

同一个世界后果只提交一次。不同观察者的 claims、受众和表达可以不同，但不能让每个观察者各自创建一份事实副本。

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

公共层区分“跨规则可以携带的意图类别”和“当前 WorldRuntime 可以提交的映射”。前者可以扩展，后者必须以 WorldRuntime 当前闭集为准；两者不能混写成一个假装全部可执行的清单。

### 5.1 变化意图类别

| 类别 | 公共层含义 | 首版处理 |
|---|---|---|
| `state_change` | 已登记主体的结构化状态变化 | 按目标类别映射为制度 / 惯例 / 环境状态；目标不明即拒绝 |
| `knowledge_change` | 观察者获得、失去或更新一条说法 / 认知 | 转为 claims / 获知请求；不能凭空制造事实 |
| `world_event` | 已发生的可追溯事件帧 | 进入 WorldRuntime 事件路径；事件正文不能代替结构化效果 |
| `condition` | 有起止或清除条件的持续约束 | 映射为已登记的活动约束等效果 |
| `location_change` | 实体位置或路线状态变化 | 仅在当前闭集有对应目标时映射；否则 `rejected` |
| `time_advance` | 明确的世界时间消耗 | 不作为普通效果提交，转 `runtime.time.consume` / 联合时间请求 |
| `clock_progress` | 有明确所有者的规则或世界进度 | 只有已有世界时钟并有对应提交路径时才可接受；无主时钟 `rejected` |
| `resource_change` | 资源增减、转移或消耗 | 当前闭集无通用映射，返回 `rejected` 并说明替代路径 |
| `relation_change` | 实体关系或立场变化 | 当前闭集无通用映射，返回 `rejected`，不得伪装成说法 |
| `player_choice` | 尚未决定的分支 | 只进入 `scene_transition.available_choices`，不进入世界变化 |

### 5.2 当前可提交映射

当前公共层可交给 WorldRuntime 的首版映射只有：

```text
condition       -> activity_constraint
location_change -> route_blocked
state_change    -> institution_state | custom_state | environment_state
knowledge_change -> claim / knowledge request
world_event     -> event frame
```

`institution_state`、`custom_state` 和 `environment_state` 的目标必须来自实例设定中已登记的结构；`resource_change`、`relation_change`、无主 `clock_progress` 和无法表达的地点变化必须诚实返回 `rejected` 或 `needs_review`。不引入万能 `custom_effect`，也不把自由文本塞进 `value` 逃避闭集。

`player_choice` 只能作为新的场景入口返回。公共层不得为未选择分支预写效果、claims、时间或认知。


## 六、校验与提交顺序

```text
原始裁定记录
  -> 结构化错误 / 响应形状检查
  -> 规则私有 patch 命名空间与 base revision 检查
  -> 通用后果规范化（来源 / 目标 / 受众 / 时间 / 确定性）
  -> WorldRuntime preview：闭集、目标、权限、因果和版本
  -> Campaign Runtime 联合 commit：规则状态 + 世界后果 + 场景转换
  -> 返回 committed / rejected / needs_review / duplicate / conflict / stale
```

公共层检查“能否表达”，WorldRuntime 检查“在这个实例、时间线和水位上能否成立”，Campaign Runtime 检查“这次提交是否属于当前战役和行动”。三个结果不能互相替代。

任何一个必要后果非法，整次提交拒绝或进入待审，不落半条状态。`needs_review` 不是失败骰点，而是表示规则与世界边界之间没有足够明确的映射。

提交结果至少区分：

- `ready`：公共层规范化成功，等待 preview / commit；
- `committed`：规则状态与世界变化已经固化；
- `rejected`：违反结构或世界约束，没有变化；
- `needs_review`：插件声明不完整、无法映射或需要主持人确认；
- `duplicate`：同一幂等键已处理，返回原提交结果；
- `conflict`：规则状态 / 世界 revision 不一致，需要重读后重新形成结果；
- `stale`：快照或运行世代已失效，结果不得写回。

## 七、GM 直接变化与规则裁定的分离

GM 输入“守卫已经离开”“城门被毁”时，不应伪装成玩家行动，也不必强行调用骰点插件。公共层把它标记为 `source=gm_declaration`，经过同样的结构化后果、受众、时间、权限和版本校验后提交。

若 GM 输入的是“玩家尝试撬锁”，则仍走对应规则转接插件。若 GM 输入的是“玩家掷骰结果为成功，请应用后果”，可以走插件的复核 / 应用路径，但必须保留它是 GM 指定裁定的来源，不伪造随机记录。

## 八、跨规则兼容的真实边界

公共层保证的是“世界交界面兼容”，不是“规则语义互换”。因此：

- 不同规则可以把各自的成功、失败、代价、位置变化或认知暴露交给同一世界接口，但只有当前世界闭集有对应映射时才会提交；
- 资源消耗、关系变化和规则专属数值可以保留在插件 / 战役状态中；没有 WorldRuntime 公共映射时，公共层返回 `rejected` 或 `needs_review`，不伪装成另一种效果；
- 不能把 CoC 的技能百分比直接转换成 D&D 的技能加值；
- 不能期待 PBTA 的叙事结果自动拥有另一套规则的精确数值；
- 同一角色跨规则切换时，必须由新规则插件明确建立规则角色态；
- WorldRuntime 保存已经成立的世界后果，规则插件保存本规则重放所需的私有状态。

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
6. 规则无法映射时进入 `needs_review` 或首版明确不支持的 `rejected`，不使用自由文本万能字段。
7. WorldRuntime 是唯一的世界变化提交者。
8. 新规则插件不能迫使已有规则插件共享属性、回合或资源模型。

## 十一、行为验收

| 场景 | 必须观察到的结果 |
|---|---|
| 两个差异化插件 | 可返回不同 `resolution` 与规则态；公共层不要求共享属性、骰点或资源模型 |
| 规则成功 / 失败 / 代价 | 已确认部分转成结构化后果；未确认部分进入候选 / 待审，不直接提交 |
| 资源 / 关系等未映射结果 | 返回明确 `rejected` 或 `needs_review`，不伪装成其他效果，不写万能自定义事实 |
| 目标 / 效果闭集错误 | WorldRuntime preview / commit 拒绝整批，规则状态和世界后果不分裂 |
| claims 与认知 | 说法保留来源和受众；不会由 claim 文本反向创造事实或越过角色认知 |
| GM 直接变化 | 不伪造骰点，使用 GM 来源，但与规则行动共享结构校验和原子提交边界 |
| 插件错误半成品 | 夹带 patch / effects / consequences 的错误响应一律不采信半成品 |
| 版本 / revision 冲突 | 返回 conflict / stale；不套用旧 patch，不静默覆盖新状态 |
| 幂等重试 | 相同幂等键返回原结果，不重复世界变化或规则状态 patch |
| 时间与待选择 | 时间消耗走独立请求；未选分支只留在场景转换，不进入事实效果 |
| 规则演进 | 私有字段留在插件结果；公共协议只在世界交界面破坏时升级 |

以上场景必须覆盖真实插件进程、真实 WorldRuntime preview / commit 和联合提交边界；公共模块规范本身不代表独立实现已经存在。
