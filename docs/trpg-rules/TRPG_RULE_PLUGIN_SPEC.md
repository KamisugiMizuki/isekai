# TRPG 规则插件协议

状态：最小协议已落地，用于验证 WorldRuntime 的稳定调用边界。

## 定位

规则插件是独立进程。它属于 TRPG 产品层，不属于 WorldRuntime，也不属于 UMP 通道插件。插件可以实现 CoC、D&D、PBTA 或自定义规则；核心不解析骰子表达式、属性、难度或规则私有字段。

插件协议分两种使用形态：

- **无状态 resolver**：一次请求、一次裁定、一次响应，适合当前 B0 验证和纯计算规则插件；
- **战役裁定器**：由 TRPG Campaign Runtime 提供规则状态快照，插件返回原始裁定、规则状态 patch、世界后果和场景转换。

两种形态共享进程边界和 JSON 信封，但不能把无状态 resolver 当成完整战役运行时。

```text
TRPG 客户端
  -> trpg.action.resolve
  -> 规则插件（独立进程）
  -> 通用裁定结果 + WorldRuntime 效果
  -> 当前时间线事件 / 认知 / 叙事素材
```

## 清单

```json
{
  "id": "example-coc",
  "name": "Example CoC rules",
  "version": "0.1.0",
  "protocol": "isekai.trpg.rules/1",
  "entry": ["python", "main.py"],
  "modes": ["stateless_resolver", "campaign_resolver"],
  "state_schema": "coc7.state/1",
  "converters": []
}
```

`entry` 是参数数组，不经过 shell。无状态 resolver 可以由核心启动、发送一行 JSON、读取一行 JSON，然后退出；战役裁定器同样不得持有 SQLite 或直接改世界，但必须能消费核心提供的规则状态快照，并返回带 `base_state_revision` 的 patch。规则插件不读核心凭据，也不直接写 WorldRuntime。

## 请求

请求分为 B0 无状态 resolver 和战役裁定器两种形态。

### B0 无状态 resolver

```json
{
  "type": "resolve_action",
  "protocol": "isekai.trpg.rules/1",
  "action_id": "act-001",
  "actor_id": "investigator-1",
  "intent": "调查废弃礼拜堂",
  "context": {}
}
```

### 战役裁定器

```json
{
  "type": "resolve_action",
  "protocol": "isekai.trpg.rules/1",
  "campaign_id": "camp-001",
  "scene_id": "scene-004",
  "action_id": "act-001",
  "action_revision": 2,
  "actor_id": "investigator-1",
  "intent": "调查废弃礼拜堂",
  "world_snapshot": {
    "snapshot_id": "ws-12",
    "revision": "wr-18"
  },
  "rule_state": {
    "ruleset_id": "coc7",
    "ruleset_version": "0.1.0",
    "state_revision": "rs-12",
    "opaque_state": {}
  },
  "context": {}
}
```

插件不得把客户端传入的 `opaque_state` 或世界快照当作可信写入结果；它们只是本次裁定的输入，提交时必须使用原 revision 做并发校验。

## 响应

插件响应分为四部分：规则私有的原始裁定、规则状态变化、交给规则共用模块的世界后果，以及战役场景转换。

```json
{
  "resolution": {
    "system": "coc7",
    "outcome": "success",
    "degree": "regular",
    "rolls": [{"expression": "1D100", "result": 42}]
  },
  "rule_state_patch": {
    "ruleset_id": "coc7",
    "base_state_revision": "rs-12",
    "operations": [
      {"path": "/actors/investigator-1/san", "op": "decrease", "value": 3}
    ]
  },
  "consequences": [
    {
      "id": "c-001",
      "kind": "knowledge_change",
      "subject": "investigator-1",
      "target": "src-1",
      "operation": "reveal",
      "value": {"claim_ref": "claim-001"},
      "certainty": "confirmed",
      "visibility": {"audience": ["investigator-1"]},
      "cause_ref": "act-001"
    }
  ],
  "scene_transition": {
    "status": "advanced",
    "available_choices": []
  },
  "claims": [
    {"id": "claim-001", "text": "礼拜堂墙后有旧祭文", "source_id": "src-1"}
  ],
  "participants": ["investigator-1"]
}
```

`resolution` 是规则结果记录，核心原样保存，不解析规则私有字段。`rule_state_patch` 只能写入该插件自己的规则状态命名空间，并以 `base_state_revision` 做并发校验。`consequences` 是规则插件声明的世界后果意图，先由 TRPG 规则共用模块检查，再由 WorldRuntime 校验目标、效果闭集、认知传播、版本和原子事务。`scene_transition` 只改变战役侧场景与待选择，不把未选择分支写成世界事实。规则状态 patch 与必要世界后果必须联合提交。

## 错误响应

插件错误必须是结构化对象，不能用看似成功的 `resolution` 伪装失败：

```json
{
  "error": {
    "code": "needs_input | needs_choice | needs_review | plugin_failed | rejected",
    "retryable": false,
    "action_id": "act-001",
    "detail_ref": "err-001"
  }
}
```

- `needs_input`：缺少规则所需输入；
- `needs_choice`：规则结果等待玩家选择；
- `needs_review`：需要 GM 或公共模块确认；
- `plugin_failed`：插件崩溃、超时或输出非法；
- `rejected`：行动不满足规则前置条件。

错误响应不得包含可被当作规则状态 patch 或世界后果的半成品。
## 当前实现状态

- **B0 无状态 resolver（已实现）**：`trpg.action.resolve` 不带 `campaign_id` 时保持旧语义——调插件、校验 `effects`、直接落世界事件。
- **战役裁定器（已实现）**：带 `campaign_id` 时读规则状态快照 → 调插件 → 把 `resolution` / `rule_state_patch` / `consequences` / `scene_transition` 存进行动并停在 `reviewing`，**不写世界**；世界与规则状态由 `trpg.commit` 联合提交（见 `TRPG_CAMPAIGN_RUNTIME_SPEC.md`）。
- 插件响应的世界后果清单 `effects` 与 `consequences` 现在都接受（`runtime/rules.py` 的边界检查同时认两者）；`rule_state_patch`、结构化错误响应和常驻进程形态仍按协议后续项处理。
- 清单里 `resident` 是**可选**字段：`true` = 核心保持一个插件进程服务多次裁定（一行一 JSON，`{"type":"ping"}` 必须回 `{"type":"pong"}`，读到 stdin EOF 必须自己退出）。常驻只省进程启动与模块加载——**状态仍然只能经快照进出**，插件不许把状态藏在进程内存里（否则回滚 / 分叉会带着不该有的记忆）。进程死在「还没发请求」时核心会重开一个；**死在半路不重发**（不重跑裁定）。
- 清单里 `converters[]` 是**可选**字段：声明状态转换器（`converter_id` / `from_version` / `to_version` / 可选 `converter_version`、`entry`），供 `trpg.campaign.migrate` 调用；响应必须回 `opaque_state` 对象与 `losses[]`。
- 清单里 `ruleset_version` 是**可选**字段：声明它 = 只有 `opaque_state` 格式变化时才改；不声明则退回 `version`。核心用它做 §十六 第 2 层的版本闸比对（`runtime/rules.py::manifest_identity`）。

如果具体规则的后果无法映射为结构化 `consequences`，插件必须返回待审状态，不使用万能 `custom_effect` 绕过公共层。

`trpg.action.resolve` 为 B0 无状态 resolver 的异步管理操作。完整战役裁定必须由 Campaign Runtime 编排，并使用规则状态快照 / patch 和场景转换：

- `instance_id`
- `timeline_id`
- `campaign_id`
- `plugin_manifest`
- `action_id`
- `actor_id`
- `intent`
- `context`（可选对象）
- `rule_state_ref`（战役裁定器需要时）
- `scene_ref`（战役裁定器需要时）

当前实现只返回旧 B0 结果：`accepted`、`resolution`、事件标识、世界水位和写入效果数量。未来战役路径还必须返回状态版本、规则状态引用、场景转换和联合提交结果。

## 官方参考外壳

当前 UMP、Tauri 壳、桌面管理台是 WorldRuntime 的官方参考外壳：UMP 提供受信管理调用与通道承载，Tauri 负责核心进程与窗口生命周期，管理台负责世界创作和运行管理。三者不定义 TRPG 规则；未来 TRPG 客户端只需复用管理面语义或直接调用同一核心入口。

完整战斗循环、角色表编辑器、骰点 UI、规则书导入、GM 输出编排和 Campaign Runtime 不属于当前最小实现；无状态 resolver 只用于 B0 验证。规则状态快照 / patch、联合提交和场景转换属于后续协议能力，必须先完成设计与兼容策略，再声明实现。
