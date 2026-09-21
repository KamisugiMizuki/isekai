# TRPG 规则插件协议

状态：最小协议已落地，用于验证 WorldRuntime 的稳定调用边界。

## 定位

规则插件是独立进程。它属于 TRPG 产品层，不属于 WorldRuntime，也不属于 UMP 通道插件。插件可以实现 CoC、D&D、PBTA 或自定义规则；核心不解析骰子表达式、属性、难度或规则私有字段。

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
  "entry": ["python", "main.py"]
}
```

`entry` 是参数数组，不经过 shell。核心启动插件、发送一行 JSON、读取一行 JSON，然后退出；规则插件不持有 SQLite，不读核心凭据，也不直接改世界。

## 请求

```json
{
  "type": "resolve_action",
  "action_id": "act-001",
  "actor_id": "investigator-1",
  "intent": "调查废弃礼拜堂",
  "context": {}
}
```

`context` 由具体规则客户端决定。核心原样传递，不解释其字段。

## 响应

```json
{
  "resolution": {
    "system": "coc7",
    "outcome": "success",
    "degree": "regular",
    "rolls": [{"expression": "1D100", "result": 42}]
  },
  "effects": [
    {
      "kind": "public_notice",
      "target": "src-1",
      "value": "调查者发现旧祭文",
      "expiry": "until_cleared"
    }
  ],
  "claims": [
    {"text": "礼拜堂墙后有旧祭文", "source_id": "src-1", "audience": "public"}
  ],
  "participants": ["investigator-1"]
}
```

`resolution` 是规则结果记录，核心只保存它。`effects` 和 `claims` 会经过现有世界包目标、效果闭集、认知传播和原子事务校验；不合法则整次调用拒绝。`action_id` 在同一实例和时间线内提供幂等身份。

## 管理面入口

`trpg.action.resolve` 为异步管理操作，参数为：

- `instance_id`
- `timeline_id`
- `plugin_manifest`
- `action_id`
- `actor_id`
- `intent`
- `context`（可选对象）

它返回 `accepted`、`resolution`、事件标识、世界水位和写入效果数量。它不是骰点 API，也不是 GM 文本 API。

## 官方参考外壳

当前 UMP、Tauri 壳、桌面管理台是 WorldRuntime 的官方参考外壳：UMP 提供受信管理调用与通道承载，Tauri 负责核心进程与窗口生命周期，管理台负责世界创作和运行管理。三者不定义 TRPG 规则；未来 TRPG 客户端只需复用管理面语义或直接调用同一核心入口。

完整战斗循环、角色表编辑器、骰点 UI、规则书导入、GM 输出编排暂不属于这个最小协议；只有在第二个真实规则插件证明接口不足时再扩展。
