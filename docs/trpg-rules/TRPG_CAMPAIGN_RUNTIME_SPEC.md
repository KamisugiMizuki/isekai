# TRPG 战役运行时模块设计

> 状态：设计规范 v1.0，未实现声明。
> 定位：位于 TRPG 规则层与 WorldRuntime 之间的战役编排层。
> 相关文档：[`TRPG_RULE_PLUGIN_SPEC.md`](TRPG_RULE_PLUGIN_SPEC.md)、[`TRPG_RULE_COMMON_MODULE_SPEC.md`](TRPG_RULE_COMMON_MODULE_SPEC.md)、[`../worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md`](../worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md)。

## 一、定位

TRPG Campaign Runtime 负责把玩家行动、规则裁定、规则私有状态和 WorldRuntime 世界后果组织成可恢复的战役闭环。

它不解释 CoC、D&D、Fate、行于泰拉或其他规则的属性、骰点、职业、资源和状态语义；这些仍属于具体规则插件。它也不拥有世界事实；世界事实仍只能通过 WorldRuntime 提交。

```text
玩家 / GM 输入
  -> 战役运行时：场景、行动、受众、节拍
  -> 规则插件：规则状态与私有裁定
  -> 规则共用模块：状态 patch + 世界后果
  -> 联合提交协调
      ├─ 规则状态版本
      └─ WorldRuntime 世界变化
  -> 新场景 / 新行动窗口 / GM 表达
```

## 二、职责边界

| 能力 | Campaign Runtime | 规则插件 | WorldRuntime |
|---|---|---|---|
| 战役、场景、遭遇 | 持有 | 可提供规则视图 | 不持有 |
| 玩家角色 / NPC 参与关系 | 编排 | 使用 | 只持有世界实体引用 |
| 行动声明与确认 | 持有 | 消费已确认输入 | 不负责 |
| 属性、技能、职业、骰点 | 不解释 | 唯一所有者 | 不理解 |
| 规则私有状态 | 挂载版本、协调读写 | 解释和修改 | 不解析 opaque 内容 |
| 世界事实与世界后果 | 提交协调 | 声明候选 | 唯一所有者 |
| 认知与披露 | 请求投影 / 提交 | 声明规则相关观察 | 唯一所有者 |
| 回滚、分叉、世代 | 编排请求 | 丢弃旧状态结果 | 执行世界版本语义 |
| GM / 玩家表达 | 提供结构化新局面 | 提供裁定摘要 | 提供合法世界视图 |

## 三、核心对象

### 3.1 战役

```text
campaign_id
instance_id
timeline_id
ruleset_id
ruleset_version
plugin_manifest
participants[]
current_scene_id
state_revision
status: preparing / active / waiting / paused / blocked / archived
```

战役绑定一条 WorldRuntime 时间线，但战役的规则时间、场景和规则状态不是 WorldRuntime 世界状态的第二份副本。战役删除或归档不能删除已经提交的世界历史。

### 3.2 场景

场景是可行动局面的投影，至少包含：

```text
scene_id
kind: exploration / social / conflict / travel / downtime
location_refs
world_snapshot_id
participants[]
public_facts[]
private_views[]
active_risks[]
available_actions[]
turn_state
pending_choice?
```

场景事实必须能追溯到 WorldRuntime 投影、已提交后果、合法规则状态或 GM 已确认的结构化变化。场景摘要可重建，已提交的行动与裁定不可被摘要覆盖。

### 3.3 行动声明

```text
action_id
campaign_id
scene_id
actor_id
raw_text
actor_ref
target_refs
method
intent
expected_result
preconditions
visible_risks
confirmation: pending / confirmed / modified / abandoned
```

行动声明不是事实。未确认的关键行动不得调用会改变规则状态或世界状态的裁定链。

### 3.4 规则状态附件

规则状态由插件定义字段，由核心托管版本边界：

```text
ruleset_id
ruleset_version
campaign_id
scope_ref
state_revision
opaque_state
```

核心不解析 `opaque_state`，但必须保证：读取带版本、写入带 base revision、重复提交幂等、分叉 / 回滚可恢复、规则版本不兼容时阻断。

## 四、行动生命周期

```text
1. receive
2. interpret
3. confirm
4. snapshot
5. resolve
6. review
7. commit
8. transition
9. express
```

- `receive`：保存用户或 GM 原始输入。
- `interpret`：形成行动者、目标、方法、意图和风险。
- `confirm`：玩家确认、修改或放弃；低风险自动主持必须记录授权边界。
- `snapshot`：取得 WorldRuntime 世界快照和规则状态快照。
- `resolve`：调用规则插件。
- `review`：检查状态 patch、世界后果、受众、版本和待选择。
- `commit`：规则状态与世界后果必须作为一个联合提交单元成功或失败。
- `transition`：更新场景、行动窗口和待选择状态。
- `express`：只表达已提交结果，不替玩家选择关键行动。

插件成功返回不等于行动已经发生。只有联合提交成功，Campaign Runtime 才能把行动结果标记为 `committed`。

## 五、裁定结果

规则插件返回四个互相独立的部分：

```text
resolution_record
  规则私有裁定和重放材料

rule_state_patch
  规则私有状态变化

world_consequences
  交给 WorldRuntime 的结构化世界后果

scene_transition
  新场景、待选择、行动窗口和规则节拍变化
```

### 5.1 `resolution_record`

核心原样保存，不解释字段。它可以包含骰点、牌面、难度、随机种子、规则解释和 GM 指定依据。

### 5.2 `rule_state_patch`

```text
base_state_revision
operations[]
```

每个操作至少包含：

```text
path
op: add / replace / remove
value?
```

patch 只允许写入该插件自己的命名空间。不能借规则状态 patch 写 WorldRuntime 世界事实，也不能把世界事实复制成规则状态的隐藏真值。

### 5.3 `world_consequences`

公共后果必须具有结构化目标、操作、确定性、受众、有效时间和因果来源，沿 `TRPG_RULE_COMMON_MODULE_SPEC` 进入 WorldRuntime preview / commit。

### 5.4 `scene_transition`

场景转换可以返回待选择，但待选择不是事实：

```text
status: unchanged / advanced / waiting_choice / blocked
available_choices[]
next_actor?
next_phase?
rule_time_delta?
world_time_request?
```

玩家尚未选择的分支不得进入世界提交。

## 六、规则时间

Campaign Runtime 单独保存规则节拍，例如即时、连续、对抗、回合、轮次、阶段或进度时钟。规则时间不自动等于 WorldRuntime 世界秒。

只有明确声明并通过 WorldRuntime 校验的 `world_time_request` 才能推进世界时间。离线补算不得替玩家消耗尚未作出的关键行动；世界自身的 NPC / 环境推进与玩家行动必须有不同来源标识。

## 七、失败与待审

以下状态必须区分：

```text
needs_input       缺少行动或规则条件
needs_choice      等待玩家选择
needs_review      规则结果无法映射或需要 GM 确认
rejected          违反规则状态 / 世界约束 / 版本约束
plugin_failed     插件崩溃、超时或协议错误
stale             快照或世代已失效
committed         规则状态与世界后果均已固化
```

失败不得伪造成功；待审不得落世界事实；规则状态 patch 与世界后果任一失败时整次联合提交失败。

## 八、版本、分叉与恢复

- 同一 `action_id` 重试返回原裁定和原联合提交结果。
- 插件重启后必须能根据核心提供的规则状态快照继续裁定。
- 规则状态快照随战役所属时间线提交、分叉、回滚和导入导出。
- 规则版本不兼容时阻断战役继续裁定，不静默替换规则。
- 回滚会使目标点之后的规则状态、场景派生、待选择和异步裁定失效。
- 已投递给玩家的表达不保证撤回，但不得在核心状态中继续作为当前结果使用。

## 九、行为验收

- 未确认的关键行动不调用会改变状态的裁定链。
- 玩家修改或放弃行动后，旧 `action_id` 不会被继续提交。
- 两套规则插件可以返回不同结构的 `opaque_state`，Campaign Runtime 不解析其字段。
- 同一 `action_id` 重试不重复扣除规则资源或施加世界后果。
- 规则状态 patch 与世界后果必须同批成功或同批失败。
- 插件重启后能从规则状态快照恢复。
- 规则时间推进不会偷偷替玩家完成关键行动。
- 待选择分支不会被当作世界事实。
- 回滚 / 分叉不会串入另一条线的规则状态或场景。
- 玩家只看到自己受众范围内的场景、认知和表达。

## 十、持久化归属

Campaign Runtime 自己持有战役编排状态；WorldRuntime 持有世界真值；规则插件状态由核心托管、由插件解释。三者不得互相复制真值。

| 数据 | 权威所有者 | 是否随 WorldRuntime 时间线回滚 |
|---|---|---|
| 世界事件、地点、环境、认知、世界时间 | WorldRuntime | 是 |
| 战役定义、玩家绑定、场景、行动声明、待选择、规则节拍 | Campaign Runtime | 是，按战役提交点恢复 |
| 规则私有 `opaque_state` | 核心托管的规则状态附件 | 是，按 `state_revision` 恢复 |
| 原始裁定、骰点、插件版本、重放材料 | Campaign Runtime 的裁定记录 | 是 |
| GM / 玩家表达文本 | 会话 / 消息层 | 按既有会话与外部投递语义处理 |
| 插件进程、缓存、临时生成文件 | 插件运行环境 | 否；重启后按状态快照恢复 |

战役记录引用 WorldRuntime 的 `commit_id`、`revision` 和事件标识，不复制世界事实正文。场景可以缓存事实摘要，但恢复时必须从引用的世界快照重建；摘要与事实冲突时以 WorldRuntime 为准。

## 十一、状态机

### 11.1 战役状态

```text
preparing -> active
preparing -> archived
active -> waiting
active -> paused
active -> blocked
waiting -> active
paused -> active
blocked -> active       # 兼容性 / 持久化恢复后
active -> archived
waiting -> archived
paused -> archived
blocked -> archived
```

- `preparing`：角色、规则版本或首场景尚未准备完毕。
- `active`：允许创建和确认行动。
- `waiting`：等待玩家选择、补充输入或 GM 审批；只允许处理对应的输入（实现上由战役状态闸门拦下声明 / 确认 / 裁定 / 提交，`choice.select` 是放行的那一条；还有别的待选择未处理就继续停在 `waiting`）。
- `paused`：主持人暂停；不接受会改变状态的行动。
- `blocked`：规则版本、状态恢复、WorldRuntime 或持久化不可用；只读并显示原因。
- `archived`：终态；只能读取和导出。

任何状态转换都记录原因、来源、操作者、旧 revision 和新 revision。不能通过修改场景摘要绕过战役状态闸门。

### 11.2 行动状态

```text
received -> interpreted
interpreted -> awaiting_confirmation
interpreted -> confirmed       # 自动主持且满足授权边界
awaiting_confirmation -> confirmed
awaiting_confirmation -> modified -> interpreted
awaiting_confirmation -> abandoned
confirmed -> snapshotting -> resolving
resolving -> reviewing
resolving -> plugin_failed
reviewing -> awaiting_choice
reviewing -> awaiting_gm_review
reviewing -> committing
reviewing -> rejected
committing -> committed
committing -> conflict
committing -> stale
committed -> transitioned
```

同一个 `action_id` 只能有一个确认版本。修改行动必须生成新的 `action_revision`；旧版本只能作为历史记录，不能再次提交。

### 11.3 待选择

`pending_choice` 是 Campaign Runtime 状态，不是 WorldRuntime 事实。它必须包含：

```text
choice_id
campaign_id
scene_id
action_id
prompt_ref
choices[]
audience
expires_at?
created_revision
status: open / selected / cancelled / expired
```

选择提交必须带 `choice_id`、当前场景 revision 和幂等键。过期或已选择的 choice 重试返回原结果，不重新裁定。

## 十二、联合提交协议

联合提交由 Campaign Runtime 发起，由核心事务协调器执行；不是插件自己写库，也不是先写规则状态再写 WorldRuntime。

### 12.1 提交请求

```text
trpg.commit
  scope: instance_id, timeline_id, campaign_id
  action_id
  action_revision
  base_campaign_revision
  base_world_revision
  base_state_revisions[]
  resolution_ref
  rule_state_patches[]
  world_changes[]
  knowledge_changes[]
  world_time_request?
  scene_transition
  idempotency_key
```

### 12.2 校验顺序

```text
1. 校验 campaign / scene / action 仍属于该实例和时间线
2. 校验战役状态允许提交
3. 校验 runtime_generation、WorldRuntime revision 和规则 state revision
4. 校验 action_id / action_revision / idempotency_key
5. 校验规则 patch 只写所属命名空间
6. 预览并校验 world_changes、knowledge、受众、时间和因果
7. 校验 scene_transition 不把待选择升格为事实
8. 原子写入规则状态附件、WorldRuntime 变化和 Campaign Runtime 提交记录
9. 返回一个联合 commit_id 与各自的新 revision
```

任一步失败，三类状态都不改变：规则状态、WorldRuntime 世界状态、Campaign Runtime 场景状态。提交记录可以保存失败原因，但失败记录不能伪装成事实提交。

### 12.3 提交结果

```text
committed
  joint_commit_id
  campaign_revision
  world_commit_id?
  world_revision
  state_revisions[]
  scene_id
  scene_revision

duplicate
  原 joint_commit_id 与原结果

conflict
  当前各 revision，不返回可直接套用的 patch

stale
  runtime_generation / snapshot 已失效

needs_review
  记录待审引用，不写规则状态和世界事实
```

`joint_commit_id` 是跨层关联号；WorldRuntime 的 `commit_id` 仍只代表世界提交，不把 Campaign Runtime 私有对象提升为世界事实。

## 十三、场景恢复与重建

核心重启、导入、回滚和分叉后按以下顺序恢复：

```text
1. 读取战役提交头与当前 campaign_revision
2. 检查 ruleset_id / ruleset_version 是否可用
3. 读取对应规则状态附件
4. 读取当前 WorldRuntime revision 和世界快照
5. 校验 scene.world_snapshot_ref 是否仍可达
6. 重建场景投影、可行动作和 pending_choice
7. 未完成 action 按状态恢复：
   - received / interpreted / awaiting_confirmation：继续等待
   - resolving：标记 interrupted，允许显式 retry
   - reviewing：重新做 review，不重跑插件
   - committing：按 idempotency_key 查询原提交结果
   - committed：只恢复 transition / expression
8. 不自动重跑随机裁定，不自动替玩家选择
```

规则插件进程退出不等于规则行动失败：核心依据裁定记录和提交状态判断。没有完整裁定记录的在途调用只能标记 `plugin_failed` 或 `interrupted`，不得猜测结果。

## 十四、规则时间与世界时间的最终语义

Campaign Runtime 持有：

```text
rule_clock
phase
round?
turn?
initiative_order?
local_progress_clocks[]
```

这些值只对当前战役和场景有效。规则插件可以返回 `rule_time_delta`，Campaign Runtime 负责应用；它不会自动改变 WorldRuntime。

只有 `world_time_request` 通过核心校验后，才改变世界时间。请求形态：

```text
cause                             # 必填：这次时间消耗的理由（记进提交说明）
seconds                           # 必填：正秒数
source?                           # world_process / player_action / gm_declaration
                                  # 缺省按路径推：行动提交 = player_action，GM 直接变化 = gm_declaration
```

落地方式：**它和规则状态 patch、世界后果在同一个批次里**——一次性前移时钟锚点（`base_world`），而不是改水位（`processed_world`）；随后按正常批次结算这段时间（`advance`）。所以：

- 玩家关键行动不能被后台自动推进消耗；
- 世界过程推进与玩家行动分开记账（`source` 进提交说明，回滚与审计按它区分）；
- 世界时间推进失败时，规则状态和场景也不能假装已完成——同批落盘，任一步非法则整批回到批前；
- 规则回合结束不默认等于世界时间推进；
- 休息、旅行和调查耗时只有规则插件 / GM 明确产生结构化请求时才推进世界时间；
- 非法请求（非正秒数 / 非对象 / 未知 source）进 `needs_review`，**不动时钟**，不静默忽略。

通用的 `runtime.time.consume(duration, cause, source)` 是同一原语的对外入口（受信调用方用）：`cause` 必填、只许前进、留下 `time_consume` 提交点；回滚越过它会把世界时间拉回提交那一刻。

## 十五、受众与信息隔离

每个战役材料使用以下受众集合之一：

```text
public_party
player:<player_id>
character:<character_id>
gm_only
npc:<character_id>
```

受众只控制 Campaign Runtime 对该材料的呈现范围；世界事实本身仍由 WorldRuntime 的认知投影决定。

落地：受众是**闭集**（上表 + `public_party` / `gm_only`），写入口（提交、GM 直接变化、待选择的受众）一律校验；`trpg.scene.view(audience=…)` 按受众裁剪——场景公共材料逐项看 `audience` 字段，`private_views` 只给对应受众（GM 拿全份），行动材料带自己的受众列。规则插件的 `resolution` 默认 `gm_only`，除非规则层明确生成玩家可见摘要。玩家可见摘要不能包含未获知世界事实、其他角色私密信息或规则插件私有状态中不应公开的字段。

同一用户控制多个角色不自动合并 `character:<id>` 受众。队伍公开信息必须显式声明为 `public_party`。

## 十六、规则版本与状态迁移

规则兼容检查分三层：

1. 数据格式版本：核心能否读取附件容器；
2. 规则版本：插件能否解释 `opaque_state`；
3. 战役协议版本：Campaign Runtime 能否解释场景、行动和提交记录。

规则版本不兼容时战役进入 `blocked`，不静默清空、降级或替换状态。

第 2 层的比对基准是**插件声明的规则版本**（清单 `ruleset_version`；没声明就退回插件 `version`，只有 `opaque_state` 格式变化才该改它），比的是它和**规则状态写入时记录的版本**（`trpg_rule_state.ruleset_version`）。触发点是插件升级后继续拿旧状态裁定——这是最危险的一种，因为插件看不懂的状态会被它当成合法输入。

没有转换器时的合法出口只有人工确认：`trpg.campaign.status(status="active", accept_ruleset_version="…")`，同一批把状态行的版本重铸到新值并把这次接受写进战役记录（`note`）。它是"人工接受"，不是"自动迁移"；转换器存在时才允许自动改写状态正文。

若插件声明转换器，转换必须是独立、可审计、幂等的：

```text
old_state_revision
old_ruleset_version
converter_id
converter_version
new_state_revision
losses[]
```

有信息损失、未确认字段或转换失败时必须停在 `needs_review`，原状态保持可恢复。转换不是 WorldRuntime 的自动推断。

落地：转换器由清单 `converters[]` 声明（`converter_id` / `from_version` / `to_version` / 可选 `converter_version` 与 `entry`，`entry` 缺省用插件主入口），核心经 `trpg.campaign.migrate` 调用它，只搬运与记账：请求带 `from_version` / `to_version` / `state_revision` / `opaque_state`，响应必须回 `opaque_state` 对象与 `losses[]`。幂等键是 `converter|from>to`，同一转换重放返回原记录（版本已经一致时也先认重放，不报「不需要转换」）。

## 十七、分叉与回滚的战役语义

- 从 WorldRuntime 提交分叉时，同时建立 Campaign Runtime 的初始提交和规则状态快照引用。
- 源战役之后的场景、行动、规则 patch、待选择和表达不回流新战役。
- WorldRuntime 回滚时，Campaign Runtime 清除目标点之后的场景派生、pending choice、未提交行动和规则状态附件。
- 回滚跨过战役创建点时，战役标记为 `orphaned` / `blocked`，不能继续使用旧战役状态；用户必须从可达提交恢复或新建战役。
- 回滚后运行世代提升，旧插件裁定、旧 patch 和旧 transition 全部 `stale`。
- 外部已投递表达不撤回，但核心不会把它作为当前场景内容再次生成。

## 十八、审计与可观测性

每次行动至少记录：

```text
action_id
action_revision
campaign_revision
world_revision
state_revisions
plugin_manifest
ruleset_version
resolution_ref
joint_commit_id?
status
failure_code?
created_at / updated_at
```

正文、骰点和 GM 私有材料按受众保护；管理审计可以读取结构化状态和引用，但普通玩家不能浏览完整裁定日志。审计记录是恢复和争议依据，不是新的世界事实来源。

## 十九、最小实现顺序

1. 战役 / 场景 / 行动 / choice 的持久化和状态机；
2. 无规则语义的规则状态附件容器与 revision；
3. `trpg.commit` 联合提交协调器；
4. 重启、幂等、冲突、回滚和分叉恢复；
5. 一个简单规则插件接入 `rule_state_patch`；
6. 再加入 Terra 的战斗场景和规则状态；
7. 以 CoC 或 Wilderfeast 作为第二个差异化验证插件。

不得先实现 Terra 专属战斗表，再回头补联合提交；那会把规则私有状态写死进核心。

## 二十、行为验收

### 战役状态

- 未准备完成的战役不能接受关键行动。
- `waiting` 战役只接受对应的 choice / 补充输入。
- `blocked` 战役只能读取和导出。
- 归档战役不能写入。

### 行动状态

- 同一 action 只能有一个确认版本。
- 修改会生成新 action revision，旧版本不能提交。
- 未确认行动不会调用有状态裁定。
- 插件超时不会猜测结果。

### 联合提交

- 规则 patch、WorldRuntime 变化和场景转换要么全部生效，要么全部不生效。
- 任意 revision 冲突都不落半条状态。
- 同一幂等键返回原 joint commit。
- 迟到世代结果不能写回。

### 恢复与版本

- 重启不重跑随机裁定。
- 在途 `committing` 按幂等记录恢复。
- 回滚跨过提交点会撤销场景派生和规则状态附件。
- 规则版本不兼容会阻断，不静默替换。
- 转换器失败保留原状态。

### 信息与时间

- GM 私有材料不进入玩家摘要。
- 玩家未选分支不进入世界事实。
- 规则回合不自动推进世界时间。
- 玩家关键行动不被离线补算消耗。

## 二十一、实施状态

**已实现并通过行为验证**（`tests/test_trpg_campaign.py` 23 项 + `tests/test_time_consume.py` 3 项：真 WebSocket + 真 SQLite + 真插件子进程；CLI 端到端 `scripts/_probe_trpg_cli.py` 走完战役 → 行动 → 裁定 → 联合提交 → 时间消耗 → GM 直接变化 → 规则版本迁移）：

- 战役 / 场景 / 行动 / 待选择的持久化与状态机（非法迁移给出合法去向，不静默纠正）；
- 战役状态闸门：`blocked` / `paused` / `archived` 拒绝一切改变状态的调用；**`waiting` 只接受对应的输入**（有待选择未处理时不许再声明 / 确认 / 裁定 / 提交；选择完回 `active`，还有别的待选择就继续停在 `waiting`）；
- 规则状态附件（`trpg_rule_state`）：字段不透明、只托管版本与并发；记下**写这份状态时插件声明的规则版本**；
- 规则版本闸（§十六 第 2 层）：比对「状态写入时的版本」与「当前插件声明的版本（清单 `ruleset_version`，缺省退回 `version`）」，不一致→战役 `blocked` + 明确原因；
- 规则版本**转换器**（§十六）：清单 `converters` 声明的转换器由插件执行、核心只搬运与记账；失败 / 输出非法 / 有信息损失（未显式接受）一律停在 `needs_review` 且**原状态可恢复**；记录（old_state_revision / old_ruleset_version / converter_id / converter_version / new_state_revision / losses）写进 `trpg_commit` 账本（`status=converted`），幂等键 `converter|from>to`；无转换器时的人工出口是 `trpg.campaign.status(accept_ruleset_version=…)`（同批重铸状态版本 + 留记录）；
- 联合提交 `trpg.commit`：规则状态 patch + 世界后果 + 场景转换在**同一个 `apply_runtime_batch` 事务**里落地，任一步非法则三处都不落盘；**行动路径与 GM 直接变化共用同一条管线**（`_joint_apply`）；
- **GM 直接变化**（§十五）：`trpg.gm.change` 不过行动、不过插件，直接提交后果；不制造行动行，规则状态与世界后果照旧同批；非法来源直接拒；
- 场景内时间消耗（§十四）：`transition.world_time_request = {seconds, cause}` 校验通过后，**与世界后果、规则状态同批前移时钟锚点**（`clock_shift_seconds`），随后按正常批次结算这段时间；`runtime.time.consume` 是同一原语的通用入口（`cause` 必填、只许前进、留下 `time_consume` 提交点、回滚会把世界时间一起拉回）；非法请求进待审且不动时钟；
- **来源细分**（§二十一 残余第 1 条，2026-09-22）：来源不再是两种——`source_mode` / 事件 `source` 现为
  `action→trpg_action` / `gm_declaration→gm_declaration` / `world_process→trpg_world_process`（世界自身的 NPC 与环境推进）/
  `npc_script→trpg_npc_script`（剧本推进）；`trpg.gm.change` 收 `source`（CLI `--source`），非法来源拒并列出合法集；
  世界过程与玩家行动从此在事件流里分得开（RULE_COMMON §200 的要求）；
- **规则状态 patch 分片合并**（§二十一 残余第 4 条，2026-09-22）：`base_state_revision` 落后但**触及路径（JSON 指针）**
  与 `base..current` 之间每一次提交记下的 `patch_paths` 完全不相交时，patch 并入当前 revision（记 `merged_from`）；
  有交集、或中间任何一次提交没留路径记录（老数据 / 直改状态）→ 照旧 `conflict` 且不落半条（不确定就别猜）；
- 受众与信息隔离（§十五）：受众闭集（`public_party` / `gm_only` / `player:` / `character:` / `npc:`）校验；
  **上层可显式传一串受众**（`audience=["character:pc-1","character:pc-2"]`）取并集——这就是「用户级归并」的位置：
  核心不把 `user:` 猜成角色（§十五 拍板），`user:` 仍非法；`trpg.scene.view(audience=…)` 按受众裁剪场景材料（`private_views` 只给对应受众，GM 拿全份）与行动（行动材料带自己的受众列）；插件原始 `resolution` 默认 `gm_only`，玩家面拿不到；
- 幂等重放（`trpg_commit` 账本，同键返回原 `joint_commit_id`）、版本冲突（`base_state_revision` 不符→`conflict`）、世代失效（→`stale`）；
- 回滚 / 分叉 / 导出导入随件：六张 `trpg_*` 表进 `runtime_dump` / `runtime_load` / `timeline_clear_state` / `instance_delete` / `portable`，回滚按提交快照精确恢复规则状态；
- 重启恢复 `trpg.recover`：在途 `snapshotting`/`resolving` → `interrupted`，`committing` 按幂等账本判定，不重跑随机裁定；
- **规则插件常驻形态**（§五）：清单 `resident: true` 时一个插件进程服务多次裁定（心跳 `ping`/`pong`，闲置超时就地收掉，核心退出统一关闭）；
  **跨核心复用**（§二十一 残余第 3 条，2026-09-22）：再声明 `share: true` 时改走**共享承载**——核心连本机回环上的「中继桥」（`isekai_core/runtime/plugin_bridge.py`），桥以 stdio 托管真插件并把端口/令牌写进插件目录的 `.isekai-plugin-share.json`；新核心照这份文件**接上同一个插件进程**，插件代码与线协议都不动；核心退出只断开连接（插件留着），静置 `ISEKAI_PLUGIN_IDLE_EXIT` 秒（缺省 600）桥自退不留孤儿；进程死在「还没发请求」时重开一个，**死在半路不重发**（不重跑裁定）；常驻只省启动与加载——状态仍只经快照进出；
- 管理面 op（16 个：`trpg.campaign.create|list|info|status|migrate`、`trpg.scene.open|view`、`trpg.action.declare|confirm|abandon|resolve`、`trpg.choice.select`、`trpg.rule_state.read`、`trpg.commit`、`trpg.gm.change`、`trpg.recover`，外加 `runtime.time.consume`）与 CLI 同名命令组；
- **主持责任模式与关键行动闸**（TRPG_RULES_LAYER_SPEC §八，2026-09-22）：战役带 `host_mode`
  （`assisted` 缺省 / `autonomous` / `cohost`，闭集校验）；声明的 `require_confirmation`（§4.2「是否需要玩家确认」）
  让**关键行动**在任何模式下都要玩家确认；只有 `autonomous` 对非关键行动直接确认，`assisted` / `cohost`
  一律停在 `awaiting_confirmation`——核心不替玩家确认；
- **场景推进节拍**（TRPG_RULES_LAYER_SPEC §4.1 / §九，2026-09-22）：场景带 `advance_mode`
  （`instant` / `continuous` 缺省 / `opposed` / `world`，闭集校验）；节拍是声明，核心不硬套回合；
- **提交闭包里的战役版本**（§12.1，2026-09-22）：`trpg.commit` 收可选的 `campaign_revision`
  （Python API `expected_campaign_revision`，CLI `--campaign-revision`，取自 `trpg.scene.view` 的
  `campaign.state_revision`）；与当前 `state_revision` 不一致 → `conflict`，不套用旧材料
  （规则状态 / 世界 / 场景一处不动）；
- **零世界后果的提交**（§12.1 `world_changes[]` 可以是空的，2026-09-22）：只有规则状态 patch / 说法 /
  场景转换 / 时间请求，或「明确无变化」的裁定，都能提交落账（`drafts.normalize_draft(require_effects=False)`）——
  事件行记录这次行动本身，效果数为 0；`world_event` 事件帧并进事件正文（叙述材料），**不产生效果**；
- **主持拒绝**（TRPG_CLIENT_SPEC §7.1 待审工作区，2026-09-22）：`trpg.action.reject`（CLI `trpg reject`）
  把待提交 / 待审的裁定推成 `rejected`（终态）——裁定载荷原样保留供主持复核，规则状态与世界一律不写；
  拒绝原因只回执给调用方，不往规范化载荷里塞额外字段；
- **显式重试裁定**（TRPG_CLIENT_SPEC §C2，2026-09-22）：`interrupted` / `plugin_failed` 的行动重新
  `trpg.action.resolve` 时**直接进 `resolving`**（不经过 `snapshotting`——那两个状态到不了它），
  这就是「显式重试」的合法路径，仍然不自动重跑不带用户意图的随机裁定；
- B0 兼容：不带 `campaign_id` 的 `trpg.action.resolve` 语义不变。

**尚未实现（记为设计义务，不充数）**：

（本节原列四条残余已于 2026-09-22 全部落地：来源细分 / 受众显式并集 / 跨核心复用 / patch 分片合并。）

