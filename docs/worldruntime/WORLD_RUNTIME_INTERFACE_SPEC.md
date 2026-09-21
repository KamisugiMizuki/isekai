# WorldRuntime 对外接口设计

> 状态：接口设计草案 v1.0，未实现声明。
> 定位：WorldRuntime 与 OC 故事层、TRPG 规则层、Writing Assistant 之间的解耦契约。
> 相关总纲：[`DESIGN.md`](DESIGN.md)。
> 相关底层模块：[`WORLD_RUNTIME_SPEC.md`](WORLD_RUNTIME_SPEC.md)、[`SESSION_CORE_SPEC.md`](SESSION_CORE_SPEC.md)、[`NARRATIVE_LAYER_SPEC.md`](NARRATIVE_LAYER_SPEC.md)。

## 一、接口目标

WorldRuntime 是世界事实、时间、认知、版本和原子变化的唯一持有者。高级模块通过本接口读取受约束的世界投影，提交结构化的变化意图，并根据提交后的新水位继续编排自己的体验。

```text
OC 故事层          ─┐
TRPG 规则共用模块   ─┼─> WorldRuntime 对外接口 ─> 世界事实 / 时间 / 认知 / 版本
Writing Assistant  ─┘
```

本接口保证：

- 所有调用带有实例、时间线和运行世代作用域；
- 所有读取来自一个明确的一致水位；
- 所有世界变化经过同一个校验和原子提交入口；
- 所有角色可见内容经过角色认知投影；
- 所有异步结果在提交前检查水位、世代和成员资格；
- 重试、分支、回滚和核心重启不会重复或串线地改变世界。

本接口不保证：

- 角色文本质量；
- 叙事候选质量；
- 规则插件的裁定正确性；
- 故事大纲一定达成；
- 通道消息一定送达。

### 2.1 TRPG 规则状态边界

TRPG 规则状态由具体规则插件定义字段，由 TRPG Campaign Runtime 组织，由核心托管版本边界。WorldRuntime：

- 可以保存和恢复带 `ruleset_id`、`campaign_id`、`state_revision` 的 opaque 状态附件；
- 校验实例、时间线、战役、规则版本、世代、权限和并发版本；
- 让规则状态随提交、分叉、回滚、导入导出和幂等语义生效；
- 不解析属性、技能、职业、资源、状态、回合或其他规则私有字段。

规则私有状态变化与必要的世界后果必须经过同一联合提交边界。规则状态不是 WorldRuntime 世界事实；规则插件也不能把世界事实复制到自己的状态中作为第二份真值。

## 二、分层边界

| 能力 | WorldRuntime | 高级模块 |
|---|---|---|
| 世界实体、事件、效果、时间和历史 | 唯一所有者 | 只能通过接口读取或提交 |
| 角色知道什么 | 提供认知投影 | 决定如何表达 |
| 用户消息与会话 | 不负责 | OC / 会话核心负责 |
| 规则骰点与规则状态 | 不理解；托管受版本约束的规则状态附件，不解析其字段 | TRPG Campaign Runtime / 规则插件负责 |
| 规则结果到世界后果的映射 | 校验并固化 | TRPG 规则共用模块负责 |
| 故事大纲与偏离检测 | 不拥有 | Writing Assistant 负责 |
| 角色叙事候选与表达取舍 | 提供材料与边界 | 叙事中介 / 会话核心负责 |
| 玩家观察、GM 文案、小说草稿 | 不生成 | 对应高级模块负责 |

高级模块可以有自己的派生状态，但派生状态不能伪装成 WorldRuntime 事实，也不能绕过本接口写入底层。

## 三、通用作用域

### 3.1 请求作用域

每次调用必须明确：

```text
instance_id       # 世界实例
 timeline_id      # 世界线
 actor_scope?      # 角色或其他观察者；读取接口必填，管理接口按权限决定
 observed_at       # 请求使用的已完成世界水位或一致快照标识
 expected_revision # 写入方基于的版本 / 水位
 runtime_generation# 异步任务世代
 idempotency_key   # 会改变状态的调用必填
 caller            # 高级模块标识与能力范围
```

接口拒绝隐含的“当前世界”“当前角色”“当前线”。当前 UI 选择、通道默认绑定和调用方本地缓存都不能替代作用域字段。

### 3.2 返回信封

所有响应至少带有：

```text
status             # ok / not_ready / conflict / rejected / stale / failed
instance_id
timeline_id
observed_revision  # 本响应实际读取或写入的版本
world_time         # 该版本对应的世界时刻
processed_watermark
runtime_generation
```

失败响应只提供可操作的错误类别、对象引用和恢复入口，不向普通 OC 层泄露实情、内部表、规则私有字段或模型内部内容。管理级调用可以获得更详细的结构化诊断，但仍不返回不属于调用方视角的秘密。

## 四、读取接口

### 4.1 `runtime.scope.inspect`

读取实例 / 时间线的公开运行状态和管理元数据：

```text
输入：instance_id, timeline_id
输出：
  - timeline_state: active / frozen / catching_up / persistence_blocked / archived
  - world_time
  - processed_watermark
  - target_watermark
  - revision
  - runtime_generation
  - ruleset_version
  - available_actions
```

用途：所有高级模块在开始读取或提交前确认底层是否 ready。它不返回世界实情、全量角色状态或内部任务正文。

### 4.2 `runtime.snapshot.read`

取得一个一致的、可复用的世界快照句柄：

```text
输入：scope + snapshot_request
snapshot_request:
  - characters: [character_id]
  - entities: [entity_id]
  - topics: [topic_ref]
  - include: time / current_activity / active_effects / experiences / claims / plans
  - audience: caller_defined_view
输出：snapshot_id, revision, world_time, payload, expires_at
```

规则：

- `snapshot_id` 固定其读取版本；后续世界推进不改变本快照；
- `include` 只选择调用方有权读取的投影；不能用 topics 读取实情层；
- 未完成追赶、持久化阻断、冻结线发起写入等状态返回明确不可用，不用旧状态冒充当前状态；
- 快照用于生成和候选计算，不等于写入许可。

### 4.3 `runtime.cognition.project`

按角色或其他观察者取得合法可知投影：

```text
输入：scope + observer_id + query + at_revision
query:
  - topics / entity_refs / time_range
  - purpose: dialogue / player_observation / narrative_candidate / audit
输出：
  - observer_id
  - observed_revision / world_time
  - observations[]
  - claims[]
  - known_unknowns[]
  - source_refs[]
```

每条 observation / claim 至少保留：来源、发生时刻、获知时刻、说法身份、主观确信、受众和有效期。返回“未知”是合法结果；推测不会被包装成事实。该接口禁止返回角色尚未获知的事件、未接触的史料、其他角色未披露的私聊和实情层幕后字段。

这是 OC 故事层、叙事中介和 Writing Assistant 生成角色视角材料的唯一底层入口。它不是聊天接口，也不生成自然语言回复。

### 4.4 `runtime.subject.state.read`

读取指定世界主体的结构化运行状态投影：

```text
输入：scope + subject_id + fields + audience
输出：
  - subject_id
  - state_at_revision
  - active_effects
  - current_activity
  - membership / archive_state
  - source_refs
```

只返回调用方在该 audience 下合法的字段。规则属性、骰点结果、叙事候选、大纲状态和会话历史不属于此接口。

### 4.5 `runtime.history.read`

读取已经固化、可对当前调用方公开的世界事件 / 效果 / 说法历史：

```text
输入：scope + cursor + limit + filters
filters: event_kind / subject / source / time_range / audience
输出：items[], next_cursor, observed_revision
```

历史项必须区分实际发生、记录固化、传播和角色获知时刻。它不能返回未来计划作为已发生事件，也不能把语言生成产物提升为世界历史。

## 五、变化接口

### 5.1 变化意图的公共形态

高级模块提交的是结构化 `change_intent`，不是自由文本事实：

```text
change_intent:
  id
  kind                 # world_event / state_change / resource_change /
                       # relation_change / knowledge_change / condition /
                       # location_change / time_advance / clock_progress
  subject_refs
  target_refs
  operation            # create / set / change / add / remove / reveal / advance
  value                # 已声明结构化值
  certainty            # confirmed / candidate / uncertain
  visibility           # audience / observer scope
  effective_time
  expiry / clear_when
  cause_refs           # action / rule_resolution / gm_declaration / source_event
  source_mode          # oc_management / trpg_rule / gm_declaration / world_process
  source_module
```

`candidate` 和 `uncertain` 不能直接改变世界；它们只能进入预览或待确认状态。`source_mode` 是审计来源，不是权限替代品。权限、目标、效果闭集、时间、因果和认知传播仍由 WorldRuntime 决定。

`player_choice`、故事候选、大纲目标、角色台词和小说草稿不是变化意图。它们必须留在高级模块的派生状态中。

### 5.2 `runtime.change.preview`

在不改变世界的情况下检查一组变化意图：

```text
输入：scope + base_snapshot_id + changes[] + rule_state_patches?
输出：
  - preview_id
  - accepted_candidates[]
  - rejected_candidates[]
  - needs_review[]
  - projected_effects
  - projected_observations
  - projected_rule_state_revisions
  - conflicts
  - base_revision
```

预览结果不是提交承诺。提交前若版本、世代、目标或权限改变，预览自动失效。

用途：

- OC 管理面在用户确认显式世界修改前展示影响；
- TRPG 规则共用模块检查规则后果是否能进入世界；
- Writing Assistant 比较不同大纲候选和分支后果。

### 5.3 `runtime.change.commit`

原子提交一组已经确认且通过预览的变化：

```text
输入：scope + preview_id? + changes[] + rule_state_patches[] +
      expected_revision + expected_state_revisions + idempotency_key
输出：
  - committed / duplicate / rejected / conflict / needs_review
  - commit_id
  - new_revision
  - world_time
  - event_refs
  - effect_refs
  - knowledge_refs
  - rule_state_refs
  - rule_state_revisions
  - scene_transition_ref?
  - invalidated_tasks
```

提交规则：

1. 校验作用域、运行世代、权限、版本、实体引用、效果闭集、时间和因果；
2. 任何必要变化不合法则整批拒绝，不落半条状态；
3. 成功后同一提交同时发布事件、效果、认知变化、世界水位和来源引用；
4. 相同 `idempotency_key` 返回原提交结果，不重复施加效果；
5. 提交成功才可被高级模块表达为“已经发生”；
6. 提交失败不得被语言层润色为成功。

`runtime.change.commit` 是唯一的世界事实写入口。任何高级模块不得通过数据库、缓存、事件文本、记忆或通道旁路写世界。

### 5.4 `runtime.rule_state.read`

读取某个规则插件命名空间下的版本化状态附件。WorldRuntime 只验证作用域、规则版本、权限和水位，不解析 `opaque_state`。

```text
输入：instance_id, timeline_id, campaign_id, ruleset_id, at_revision
输出：state_revision, ruleset_version, opaque_state, observed_revision
```

规则状态不属于世界事实，但必须随战役所属时间线接受分叉、回滚、导入、世代和幂等语义。插件不能直接读取数据库。

### 5.5 `runtime.change.commit` 的规则附件

TRPG 规则提交可以携带受命名空间约束的规则状态 patch：

```text
输入：scope + preview_id? + changes[] + rule_state_patches[] +
      expected_revision + expected_state_revisions + idempotency_key
```

规则状态 patch 必须满足：

1. 只能写入声明该命名空间的规则插件状态；
2. 以 `base_state_revision` 为并发条件；
3. 与必要的世界事件、效果、claims、knowledge 在同一提交边界内成功或失败；
4. 不把规则私有字段解释为 WorldRuntime 世界事实；
5. 回滚、分叉和世代失效后，迟到 patch 不得写回旧状态。

返回增加：

```text
rule_state_refs
rule_state_revisions
scene_transition_ref?
```

`runtime.change.commit` 仍是唯一的世界事实写入口；规则状态附件是受控的外部状态提交，不改变 WorldRuntime 不理解规则语义的边界。

### 5.6 `runtime.knowledge.grant`

显式提交一个经过用户 / 管理者确认的信息披露：

```text
输入：scope + from_observer + to_observer + source_refs + disclosure_scope
输出：commit result + knowledge_refs
```

披露只改变接收者的可知范围，不改变世界事实，也不把来源角色的经历变成接收者亲历。披露本身必须版本化，回滚和分支遵守普通世界状态语义。

### 5.7 `runtime.time.advance`

请求对当前时间线推进世界时间：

```text
输入：scope + requested_until / duration + reason + idempotency_key
输出：accepted / catching_up / rejected + processed_watermark + new_revision
```

通常由 WorldRuntime 自己根据激活状态、现实锚点和倍率推进；高级模块只在设计明确允许的场景请求局部时间消耗。请求不能跳过事实转移、补算、事件效果或认知获得。

## 六、版本与异步接口

### 6.1 `runtime.timeline.fork`

从不可变提交创建新时间线。若该线存在 TRPG 战役，分叉请求必须同时携带战役标识；核心复制战役提交头和规则状态附件引用，不复制正在进行的插件进程、未提交 action 或开放 choice。

```text
输入：instance_id + source_commit_id + name + activate? + campaign_id?
输出：new_timeline_id + source_commit_id + initial_revision + campaign_commit_id?
```

分叉继承共同过去；源线后续变化不回流。Writing Assistant 的候选试演和小说情节比较优先在分支中进行。OC 和 TRPG 也只能通过此接口保存另一条生活线 / 战役线。

### 6.2 `runtime.timeline.rollback`

将当前线覆盖到指定可达提交：

```text
输入：scope + target_commit_id + confirm + idempotency_key
输出：new_revision + new_runtime_generation + invalidated_tasks + delivered_external_count
```

回滚是破坏性操作：当前线目标点之后的世界增量、认知、对话派生、叙事消费、TRPG 场景派生、开放 choice、未提交 action 和规则状态附件失效；外部平台已经显示的文本不保证撤回。回滚不会恢复历史中的本机通道绑定、待发送任务和其他控制状态。

若回滚跨过战役创建点，相关战役进入 `orphaned` / `blocked`，必须从可达提交恢复或新建战役；迟到的规则 patch、scene transition 和插件结果按新世代拒绝。

### 6.3 `runtime.generation.check`

异步高级模块在固化结果前调用：

```text
输入：scope + snapshot_id + runtime_generation + source_refs
输出：valid / stale / conflict / member_archived / persistence_blocked
```

生成、规则裁定后处理、叙事候选和 Writing Assistant 候选都必须在写入或提交前检查。`stale` 结果可以被保存为本地失败记录，但不能写入 WorldRuntime。

### 6.4 `runtime.task.invalidate`

仅供受信管理面或核心内部使指定世代的派生任务失效。高级模块不能用它删除事实，只能取消尚未提交的候选、生成或投递工作；已固化世界历史不可通过任务失效撤销，必须走回滚。

## 七、三类高级模块的使用方式

### 7.1 OC 故事层

```text
1. scope.inspect
2. snapshot.read + cognition.project(character_id)
3. 会话核心生成角色表达
4. 会话核心将回复 / 合法披露作为受控增量提交
5. generation.check
6. 读取提交后的新投影，交给会话固化与投递
```

OC 故事层：

- 读取角色视角，而不是实情；
- 把用户分享记录为角色听到的内容，不直接写公共事实；
- 普通聊天不调用 `change.commit` 创建世界事件；
- 显式世界修改才使用 `change.preview` → `change.commit`；
- 使用 timeline fork / rollback 管理生活线，不自行复制状态。

WorldRuntime 不知道“这是一次 OC 对话”，只看到受作用域和来源约束的会话增量或显式变化请求。

### 7.2 TRPG 规则层

```text
1. Campaign Runtime 读取 scope.inspect
2. Campaign Runtime 取得 snapshot.read + cognition.project
3. Campaign Runtime 读取规则状态附件
4. 规则独占转接插件完成裁定
5. 规则共用模块生成 rule_state_patch + change_intent[] + scene_transition
6. runtime.change.preview
7. runtime.change.commit（世界后果与规则状态联合提交）
8. Campaign Runtime 更新场景和行动窗口
9. 读取各玩家角色的 cognition.project
10. 由 GM / 客户端表达新场景
```

TRPG 规则层：

- WorldRuntime 不理解骰点、属性、职业、回合或规则私有 `resolution`；
- Campaign Runtime 不复制世界真值，只持有战役、场景、行动和规则状态引用；
- 规则共用模块必须把成功、失败、代价和待选择分支区分为可提交变化或候选；
- 规则私有状态 patch 只能写入对应插件命名空间，并与必要世界后果同批提交；
- 无法映射的规则结果停在 `needs_review`；
- GM 直接变化使用 `source_mode=gm_declaration`，不伪造规则骰点，但经过同一预览 / 提交校验；
- action_id 与 idempotency_key 分开：前者是规则行动身份，后者是底层提交幂等身份。

### 7.3 Writing Assistant

```text
1. 读取 outline 自己维护的约束状态
2. snapshot.read + cognition.project(observer / character)
3. 检查大纲达成、事实冲突、因果缺口和偏离
4. 生成候选，不写 WorldRuntime
5. 需要试演时 timeline.fork
6. 候选确认后 change.preview → change.commit
7. 读取新水位并更新大纲派生状态
```

Writing Assistant：

- 大纲、章节目标、候选和草稿全部是上层派生状态；
- “大纲要求发生”不构成世界变化授权；
- 小说模式默认只读或在草稿分支提交；
- GM 辅助是应用方式：GM 直接变化使用 `gm_declaration`，玩家行动结果则经 TRPG 规则层进入本接口；
- 偏离可以被创作者明确接受，但不能由 WorldRuntime 静默改写成达成。

## 八、错误与降级

| 错误 | 含义 | 高级模块处理 |
|---|---|---|
| `not_ready` | 追赶、冻结、持久化阻断或未完成初始化 | 显示状态或只读，不能使用旧快照冒充当前 |
| `conflict` | 预期版本与当前版本不同 | 丢弃预览，重新读取快照并重新计算 |
| `stale` | 异步结果来自旧世代 / 旧水位 | 不提交，可保存为失败原因 |
| `rejected` | 目标、权限、效果、时间或因果非法 | 向用户显示可理解原因，不局部重试同一非法请求 |
| `needs_review` | 高级模块没有完成规则 / 故事到世界的映射 | 留在待确认，不创建事实 |
| `persistence_blocked` | 无法安全发布状态 | 停止产生新事实和派生写入，等待恢复 |
| `duplicate` | 幂等键已经处理 | 使用原提交结果，不重复生成或表达 |

错误降级不能把“没有发生”改写成“发生了但没说出来”。

## 九、解耦规则

1. 高级模块只能依赖本文件定义的稳定语义，不读取 WorldRuntime 数据库、内部表或实现类。
2. WorldRuntime 不导入 OC、TRPG 或 Writing Assistant 的包、规则字段和提示词。
3. 上层自有状态通过 `source_module`、`source_refs` 和作用域关联，但不进入底层的规则私有模型。
4. 新增上层模块优先复用 snapshot / cognition / change / version 接口，不新增旁路写入口。
5. 接口升级只能增加可选字段或提升协议版本；破坏性变化必须保留迁移 / 兼容阻断，不静默解释旧请求。
6. 高级模块可以替换模型、规则插件、客户端和文本风格，不改变已提交世界事实的语义。
7. WorldRuntime 的行为测试必须使用至少一个无上层产品偏好的调用方验证；高级模块的行为测试必须覆盖真实接口边界。

## 十、设计不变式

1. WorldRuntime 是世界事实、时间、认知、版本和原子提交的唯一所有者。
2. 读取永远绑定实例、时间线、观察者和一致水位。
3. 角色视图永远经过认知投影；实情层不作为高级模块默认输入。
4. 变化意图、规则裁定、大纲候选、角色台词和小说草稿不等价。
5. 只有 `runtime.change.commit` 成功返回后，才允许上层表达“已经发生”。
6. 预览不是提交，候选不是事实，未知不是失败事实。
7. 回滚 / 分叉 / 世代失效不会让迟到任务、缓存或旁路写回旧历史。
8. 同一事实不因 OC、TRPG、GM 和小说模式分别提交而重复产生。
9. WorldRuntime 不需要知道调用方使用哪套 TRPG 规则或哪种写作方法。
10. 所有新上层模块都能通过本接口解耦替换，而不复制世界真值。

## 十一、行为验收

- OC、TRPG、Writing Assistant 使用同一个实例 / 时间线时，读取到相同水位的世界事实。
- 不同角色通过 `cognition.project` 得到不同且合法的观察，不返回未获知事件或他人私密内容。
- 三类上层模块都能在同一 `snapshot_id` 上生成，世界推进后旧结果被 `generation.check` 拒绝写回。
- 任意变化都必须经过 preview / commit；非法目标、非法效果、版本冲突和未确认候选不会落库。
- 同一幂等键重试返回原提交结果，不重复事件、效果、认知传播或时间推进。
- OC 普通聊天不会静默成为世界事件；显式世界修改可审计且可回滚。
- 两个不同规则插件的公共后果能经过同一提交边界，WorldRuntime 不读取规则私有字段。
- GM 直接声明可以提交结构化变化，但不会伪造骰点，也不会绕过认知与版本校验。
- Writing Assistant 的大纲偏离可以被报告、接受或改写，但不会由底层自动制造必达节点。
- 分支试演不污染主线，回滚不恢复已失效的异步任务，外部已投递文本仍按不可撤回语义处理。

## 十二、与现有文档的关系

- `WORLD_RUNTIME_SPEC.md` 定义底层世界时钟、状态、认知和版本语义；本文件定义它们如何被外部消费。
- `SESSION_CORE_SPEC.md` 定义会话、消息、生成和投递；本文件只提供其所需的世界快照、认知投影和受控提交边界。
- `NARRATIVE_LAYER_SPEC.md` 定义叙事候选与表达约束；本文件只提供合法材料和版本边界。
- `TRPG_RULE_COMMON_MODULE_SPEC.md` 定义规则私有裁定到通用后果包的转换；本文件接收转换后的变化意图并最终固化。
- `WRITING_ASSISTANT_SPEC.md` 定义大纲、候选、偏离和写作模式；本文件不保存大纲，也不判断故事是否好看。
- 本文件不替代上述模块的产品设计，也不把它们的内部对象提升为 WorldRuntime 公共数据模型。
