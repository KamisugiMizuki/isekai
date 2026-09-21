# 通道协议附录（实现级字段表 / 错误码 / 计数与上限）

> 上游：[`CHANNEL_PLUGIN_SPEC.md`](CHANNEL_PLUGIN_SPEC.md) §九「待模块设计项（残余）」第 1 条 —— **实现级字段表、错误码枚举、字符计数与帧上限**。
> 本文只写代码里已经存在的行为：每条给 `文件:行`（或 `文件::函数名`）——代码里没有的一律标「未实现 / 未定义」，不按设计意图补写。
> 基线：`isekai_core/{version,ump,channel,session,config,log,llm,client,store}.py` 与 `isekai_core/world/{ops,package}.py`。
> 引用约定：默认 `文件:行`（写作时代码定格）；`store.py` 与 `world/ops.py` 行号随近期提交漂移较大、函数名稳定，故按 `文件::函数名` 引用。
> 机器对拍：`scripts/_audit2_proto_doc.py`（比对本文数值与枚举，不一致即 FAIL 并打印差异）。

## ① 信封字段表

顶层字段（解析 `ump.parse` ump.py:238-301；构造 `ump.make` ump.py:304-328）：

| 信封字段 | 类型 | 必填 | 语义 / 校验 | 代码出处 |
|---|---|---|---|---|
| `ump` | str | 是 | 协议版本，须 `startswith("1.")`；构造值 `UMP_VERSION = "1.0"`，只认主版本 `UMP_MAJOR = "1"` | ump.py:255-257 · version.py:12-13 · ump.py:320 |
| `type` | str | 是 | 必须在 `CLIENT_TYPES ∪ SERVER_TYPES`（13 个）内，且属于当前方向允许集合 | ump.py:259-264 · ump.py:39-48 |
| `id` | str | 是 | 信封标识：非空、≤64 字符；入站去重键的第三段 | ump.py:266 · ump.py:125-133 |
| `ts` | number | 是 | 发送方现实时间戳，仅展示 / 诊断；int/float 皆可（bool 不算），解析后 `float()` | ump.py:267-269 · ump.py:121-122, 296 |
| `thread` | object | 条件 | 8 个 `THREAD_REQUIRED` 类型必须给 | ump.py:273-282 · ump.py:43-45 |
| `thread.id` | str | 条件 | 通道侧不透明 thread 标识：非空、≤128 字符 | ump.py:277 |
| `thread.binding_token` | str | 条件 | 不透明绑定令牌：非空、≤128 字符；3 个 `TOKEN_REQUIRED` 类型必须给 | ump.py:278-284 · ump.py:47 |
| `payload` | object | 否 | 缺省按 `{}` 处理；非对象报 protocol_error；字段按 `type` 分别校验（见 ②） | ump.py:286-291 |

- 未知顶层键不报错：解析只读上表字段，整帧原文留在 `Envelope.raw`（ump.py:110, 293-301）——`raw` 不是线上字段。
- 方向：核心以 `direction="c2s"` 解析通道来的帧（channel.py:151, 251-255）；客户端以 `direction="s2c"` 解析核心来的帧（client.py:79）。
- `id` 前缀（`e-` / `s-`）只是构造点习惯（`ump.new_id` ump.py:98-99），**不是方向判据**：服务端 `reply` / `system_notice` / `status` 走默认 `e-`（session.py:703-729, 781）。
- 握手期解析用**配置上限**（`cfg.max_text_len`，channel.py:151）；握手完成后改用**协商值**（channel.py:254）。
- 管理面帧 `{"mgmt":"1","op":…,"args":…}` 不是 UMP 信封（channel.py:586-593），只在 ③ 复用同一批 code 字符串。

## ② 消息类型表

| type | 方向 | 需 thread | 需 token | payload 字段（名 · 类型 · 必填 / 校验） | 语义 | 代码出处 |
|---|---|---|---|---|---|---|
| `hello` | c2s | 否 | 否 | `channel{id,name,version}` 必；`capabilities{segments,status,attachments,streaming,max_text_len,max_parts,max_attachments,max_attachment_bytes}` 否；`auth{bootstrap｜credential}` 必 | 首帧必须是 hello，否则 protocol_error + 关闭 1008；`channel.id` 非空 ≤64，`name`/`version` 缺省取 id / `"0"`；`auth` 须给 bootstrap 或 credential 之一，否则 auth_required；限额须为正整数，缺省 4000 / 10 / 3 / 524288 | ump.py:142-192, 177-179 · channel.py:163-168 |
| `hello_ack` | s2c | 否 | 否 | `channel_instance`、`name`、`negotiated{segments,status,attachments,streaming,max_text_len,max_parts,max_attachments,max_attachment_bytes}`、`protocol`、`state`∈CORE_STATES、`threads[{id,binding_version,binding_token}]`、`credential`（仅引导首连） | 回协商结果与既有 thread 令牌（重连不必再问管理面）；解析期只校验 `state` 属于 `CORE_STATES` | channel.py:198-218 · ump.py:227-229, 52 |
| `binding` | s2c | 是 | 否 | `thread_id`、`binding_version`、`binding_token`、`state`∈{active,revoked} | 管理面绑定 / 重绑后推给在线通道：**换代先发 `revoked`（旧版本号 + 旧令牌，发给旧绑定所在连接）再发 `active`**；只发 active 会让客户端拿着旧令牌直到下一次发送才吃 `binding_expired` | channel.py::_thread_bind · ump.py:230-232 |
| `user_message` | c2s | 是 | 是 | `text` 必：非空且非纯空白、≤协商 max_text_len；`attachments` 可选：数组，每项 `{name≤128, media_type(MIME), data(base64)}`，须协商过 `attachments` 且条数 ≤ `max_attachments`、解码后单件 ≤ `max_attachment_bytes` | 唯一用户输入入口；去重键 = 已认证通道 + thread + `id`（**换附件内容也算冲突**）；附件随消息落库、进历史，图像进模型时给 `image_url`，其余类型只给一行文字标注 | ump.py::_attachments_of · session.py::_user_content · store.py::inbound_put |
| `accepted` | s2c | 是 | 否 | `ref` ≤64 必；`state`∈ACCEPT_STATES（queued/processing/done/failed/cancelled）；`message_id`（未固化时为 null）；retry(outbound) 路径另带 `delivery` 汇总 | 「已持久接收」的确认，不等于已生成回复；重复输入返回同一逻辑轮次的最新状态 | ump.py:206-209, 51 · session.py:156-167, 252-260 |
| `reply` | s2c | 是 | 否 | `message_id` ≤64 必；`parts` 非空数组、每项 `text` 为 str；`batch_index` ≥0；`batch_count` ≥1；另有 `reply_to`（主动消息为 null）与 `covers[]` | 已固化最终回复；批次数与序号发送前确定，重试不重排、不换 `message_id` | ump.py:210-223 · session.py:716-729 |
| `reply_delta` | s2c | 是 | 否 | `message_id` ≤64 必；`index` 非负整数；`text` 非空 str | **增量预览**（只在通道协商 `streaming` 时发）：与最终 `reply` 同一个 `message_id`、按 `index` 有序；后验检查可能改字，客户端拿最终帧覆盖缓冲区 | ump.py:339-345 · session.py::_stream_reply · channel.py::deliver（能力位闸） |
| `system_notice` | s2c | 是 | 否 | `text` 必：非空字符串；`message_id` | 联络系统 / 管理机制的说明（归档、追赶提示），不是角色发言，不进角色上下文 | ump.py:233-235 · session.py:703-714, 463-474 |
| `delivery` | c2s | 是 | 是 | `message_id` ≤64 必；`batch_index` 非负整数（缺省 0）；`state`∈{accepted,failed,unknown} | 投递回执：只按原出站标识更新原投递记录，不代表用户已读 | ump.py:186-192, 50 · channel.py:348-369 |
| `retry` | c2s | 是 | 是 | `ref` ≤64 必；`kind`∈{input,outbound} 或缺省 | input：恢复同一逻辑轮次的新尝试；outbound：只重发固化结果；作废 / 已完成不可重放 | ump.py:193-196 · session.py:235-286 |
| `status` | s2c | 是 | 否 | `state`∈{thinking,idle,interrupted} | 只发给声明 `status` 能力的通道；`thinking` / `idle` 包住每一轮生成，`interrupted` 在轮次被打断时（回滚 / 重绑 / 冻结 / 删除期间作废）夹在中间发出 | ump.py:224-226 · channel.py:130-137 · session.py::_status（三处 drop 分支） |
| `error` | s2c | 否 | 否 | `code` ≤64 必；`message` str（可缺）；`retryable` bool（缺省 false）；`ref`；`stage`∈Stage（缺省 protocol） | 有限、脱敏的错误结果，见 ③ | ump.py:197-205, 331-340 |
| `ping` | 双向 | 否 | 否 | 无（payload 可省略） | 核心收到即回 `pong`（复用同 thread） | ump.py:39-42 · channel.py:272-276 |
| `pong` | 双向 | 否 | 否 | 无 | 核心收到即忽略；`UmpClient` 不发 UMP ping——连接心跳由 websockets 协议层 `ping_interval` 负责 | channel.py:278-279 · client.py:43 |

- `error` 的实现方向比设计窄：`error` 只在 `SERVER_TYPES` 里（ump.py:40-42），通道往核心发 `error` 会判方向越权 → `protocol_error`（ump.py:263-264）；CHANNEL_PLUGIN_SPEC §2.3 表里写的是「双向」，实现未放行 c2s 方向。
- `ping` / `pong` 在两侧集合里都有（ump.py:39-42），双向都收；但代码里只有核心发 `pong`（channel.py:275），没有发送 `ping` 的一方——测试 / 探针自己发（`scripts/_audit_ump.py:168`）。

## ③ 错误码表

`error` 信封的五个 payload 字段（`UmpError.to_payload` ump.py:88-95）：`code` / `message` / `retryable` / `ref` / `stage`；
`ref` 在发送处可被改写为触发帧的 `id`（ump.py:339；channel.py:296-299 用 `envelope.id`；session.py:784-786 用入站 `env_id`）。
`close=True`（ump.py:70-86）**不进线上 payload，代码里也没有读取方**——断连一律由调用点显式 `_safe_close()` 完成（channel.py:146, 156, 167, 188, 260, 608-612）。

阶段枚举（`Stage` ump.py:55-64；构造默认 `protocol` ump.py:78）：

| 阶段 | 取值 | 含义 | 代码出处 |
|---|---|---|---|
| `RECEIVE` | receive | 接收期：绑定校验、状态门、去重冲突、冻结 / 身故 | channel.py:301-346 · session.py:88-167 |
| `GENERATE` | generate | 生成期：模型调用失败、空回复、内部异常 | session.py:348-382 |
| `DELIVERY` | delivery | 投递期：回执目标 / 令牌不符、batch_index 越界、能力不兼容 | channel.py:348-369 · session.py:743-757 |
| `PROTOCOL` | protocol | 协议 / 解析期（默认值） | ump.py:78 · channel.py:165, 294 |
| `AUTH` | auth | 认证期：`hello.auth` 缺失 / 凭据不匹配 | ump.py:152, 156 · channel.py:230, 243 |

`Err` 枚举（18 项，值即线上 `error.code`；「retryable」列写代码实际取值）：

| 枚举名 | code | 语义 | retryable | 主要抛出点 |
|---|---|---|---|---|
| `PROTOCOL` | protocol_error | 信封 / 载荷不合规、方向越权、首帧不是 hello、核心不接受该类型、`delivery.batch_index` 越界 | 否 | ump.py:125-291（解析期全部校验）· channel.py:165, 294, 361 |
| `BAD_FRAME` | bad_frame | 帧不是合法 JSON、顶层不是对象 | 否 | ump.py:249, 253 |
| `UNSUPPORTED_TYPE` | unsupported_type | 未知信封 type；管理面未知操作 | 否 | ump.py:262 · channel.py:565 · world/ops.py::dispatch / ::dispatch_async |
| `AUTH_REQUIRED` | auth_required | hello 无 `auth` 段或既无 bootstrap 也无 credential；管理首帧不是 `auth` | 否 | ump.py:152, 156 · channel.py:375 |
| `AUTH_FAILED` | auth_failed | 引导凭据无效 / 已消费、持久凭据不匹配、管理令牌无效或已用 | 否 | channel.py:230, 243, 380 |
| `INVALID` | invalid_input | 管理面入参与产物校验失败（路径、角色卡、世界包、草稿、实例成员） | 否 | world/ops.py::resolve_path / ::_read_user_json / ::dispatch（入参与产物校验；读入前含字节限额） |
| `UNKNOWN_THREAD` | unknown_thread | thread 未绑定到任何会话 | 否 | channel.py:305, 328 |
| `BINDING_EXPIRED` | binding_expired | 消息 / 重试 / 回执携带的令牌与当前绑定不一致（换代或撤销后） | 否 | channel.py:308, 330, 356 |
| `CONFLICT` | conflict | 同键异文（同 `id` 换正文）；旧失败轮次之后已有新对话 | 否 | session.py:149, 278 |
| `VOIDED` | voided | 该输入已因回滚 / 重绑作废，不能重放 | 否 | session.py:96, 238, 273 |
| `NOT_FOUND` | not_found | 找不到对应的出站消息 / 输入或回复 / 会话 / 通道登记 | 否 | channel.py:352, 536, 539, 551 · session.py:244, 248 |
| `UNSUPPORTED_CAPABILITY` | unsupported_capability | 既有分段计划超出当前协商能力：只报投递能力不兼容，不重排 / 裁剪 / 重新生成 | 否 | session.py:743-757 |
| `STATE_BLOCKED` | state_blocked | 核心非 ready（此路可重试）、时间线冻结 / 归档、角色身故归档、实例兼容性阻断 | 依站点 | channel.py:282（true）· channel.py:514、session.py:110, 121, 131（false） |
| `GENERATION_FAILED` | generation_failed | 生成阶段失败归类：空回复、管理面生成类操作失败 | 依站点 | session.py:380（true）· world/ops.py::dispatch_async（跟随 LLMError） |
| `LLM_NOT_CONFIGURED` | llm_not_configured | 未配置 LLM API Key | 否 | world/ops.py::dispatch_async · llm.py:58 |
| `OVERLOADED` | overloaded | 容量闸：在线连接数达上限（拒新连接）、单会话排队入站达上限（拒新输入） | 是 | channel.py::_channel_handshake · session.py::accept |
| `RATE_LIMITED` | rate_limited | 速率闸：该连接本窗口入站帧数超限（持续超限即断这条连接） | 是 | channel.py::_rate_ok |
| `INTERNAL` | internal | 未预期的内部异常（脱敏，不回显细节） | 依站点 | session.py:371（true）· channel.py:418（false） |

生成阶段的 code 由 `LLMError` 直接产出（llm.py:26-31，构造 `retryable` 默认 **true**），经 session.py:353-364 原样作为 `error.code` 上线（`stage=generate`）：

| 生成码 | retryable | 语义 | 代码出处 |
|---|---|---|---|
| `llm_not_configured` | 否 | 未配置 API Key | llm.py:57-58 |
| `llm_unreachable` | 是 | 传输层异常（重试一次仍失败） | llm.py:79-85 |
| `llm_unavailable` | 是 | HTTP 429 / ≥500 | llm.py:87-93 |
| `llm_rejected` | 否 | HTTP 4xx | llm.py:95-96 |
| `llm_bad_response` | 是 | 响应缺 `choices[0].message.content` | llm.py:98-104 |
| `empty_completion` | 是 | 空文本（重试时预算翻倍） | llm.py:107-112 |
| `truncated_completion` | 是 | `finish_reason=length` 被截断 | llm.py:113-119 |
| `llm_failed` | 是 | 未知失败兜底 | llm.py:122 |

- 归类关系：管理面侧把 `llm_not_configured` 归到 `Err.LLM_NOT_CONFIGURED`，其余归到 `Err.GENERATION_FAILED`（world/ops.py::dispatch_async 的 except LLMError 分支）。
- 空回复的两个字段不是一回事：线上 code 是 `generation_failed`，落库的 `error_code` 是 `empty_completion`（session.py:375-381）。
- 客户端的错误处理：`s2c` 帧也走同一套校验，不合规直接丢弃（client.py:79-81）；握手期 error 转成 `UmpError` 抛出（client.py:64-70）；CLI 按 `ref` 匹配后抛出（cli.py:159-165）。

## ④ 字符计数方式

**UMP 协议路径上的长度校验一律用 Python `len(str)`：按 Unicode 码点计数——不是 UTF-8 字节数，也不是 UTF-16 码元数。**

| 校验点 | 计数对象 | 上限 | 代码出处 |
|---|---|---|---|
| 入站文本 | `payload["text"]` 原样（**未** strip） | 协商 `max_text_len`（缺省 4000） | ump.py:184-185 |
| 信封 / 载荷字符串字段 | 各字段值 | `id`/`ref`/`message_id`/`code` 64；`thread.id`/`binding_token` 128 | ump.py:125-133, 266, 277-280, 187, 194, 198, 207, 211 |
| 出站分段 | 规范化后的回复文本（`\r\n`→`\n`、strip 后按行贪心切） | 协商 `max_text_len` | session.py:37-63, 374 |
| 投递前复核 | 固化批次的每段 | 段长 ≤ `max_len`；批内段数 ≤ `max_parts` | session.py:697-702 |
| 附件字节 | 每件 base64 **解码后**的长度 | 协商 `max_attachment_bytes`（缺省 512 KiB）；条数 ≤ 协商 `max_attachments`（缺省 3） | ump.py::_attachments_of |

- CPython 的 `str` 是码点序列：一个 emoji / 辅助平面汉字算 **1**（UTF-8 下占 4 字节）。代码里没有 `encode()` 后的字节长度校验。
- 帧上限是**字节**，与上面分开：`MAX_FRAME_BYTES = 1048576` 交给 websockets 的 `max_size`（服务端 channel.py:95，客户端 client.py:43, 165），按帧重组后的消息字节数判（库文档：`max_size: Maximum size of incoming messages in bytes`；超限 `fail(CloseCode.MESSAGE_TOO_BIG=1009)`，websockets/protocol.py:625-627 · websockets/frames.py:68）→ 核心不进解析、不发 error 信封。
- 线上编码：`json.dumps(..., ensure_ascii=False)` + UTF-8 文本帧（channel.py:125, 176 · client.py:62, 141）。一个码点的 UTF-8 字节数 1–4，故「4000 码点」最坏约 16 KB 正文，默认限额下远小于 1 MiB；**但核心不校验协商 `max_text_len` 与 `MAX_FRAME_BYTES` 的关系**（`core.max_text_len` 可被配置放大，config.py:264），配置放大后超限帧会被库直接断开——属未定义边界。
- 入站长度校验用未 strip 的原文，落库时才 strip（session.py:92）；纯空白文本在解析期即被拒（ump.py:182-183）。
- 另一路（与 UMP 无关）：管理面读 JSON 件前按**文件字节数**拦，`MAX_PACKAGE_BYTES = 1 << 20`（world/package.py:140）在 `world/ops.py::_read_user_json` 内先 `stat` 再判、超限报 `invalid_input`——这是代码里唯一按字节拦的入参。

## ⑤ 帧 / 队列 / 日志上限

| 常量 | 值 | 出处 | 超限行为 |
|---|---|---|---|
| `MAX_FRAME_BYTES` | 1048576 | version.py:34 · channel.py:95 | 单条消息字节超限：websockets 断开连接（1009），核心不解析、不发错误信封 |
| `HANDSHAKE_TIMEOUT_S` | 10.0 | version.py:36 · channel.py:144-147 | 首帧未到：关闭 1008 `handshake timeout` |
| `PROTOCOL_ERROR_LIMIT` | 5 | version.py:35 · channel.py:257-261 | 解析期错误累计达 5：关闭 1008 `too many protocol errors`（计数只增不减，channel.py:49） |
| `DEFAULT_MAX_TEXT_LEN` | 4000 | version.py:31 · config.py:133 · ump.py:158, 242 | 入站超长：protocol_error，不落库、不进生成（ump.py:184-185） |
| `DEFAULT_MAX_PARTS` | 10 | version.py:32 · config.py:134 · ump.py:159 | 每批段数上限（取双方交集，channel.py:53-60） |
| `SEND_TIMEOUT_S` | 15.0 | session.py:27 · session.py:732-739 | 服务端发送超时：该批记 `unknown`，不假称成功 |
| `merge_batch_max` | 8 | config.py:111 · session.py:489-495 | 达到容量即封口，后来输入属下一批 |
| `pending_outbound limit` | 20 | store.py::pending_outbound · session.py:759-778 | 重连补投有界：一次最多补 20 条固化回复 |
| `core.log maxBytes` | 2097152 | log.py:27-28 | 写满 2 MiB 轮转 core.log |
| `core.log backupCount` | 3 | log.py:27-28 | 保留 3 份备份（最多 4 个文件 ≈ 8 MiB） |
| `ping_interval` | 20 | channel.py:96 · client.py:43, 165 | websockets 协议层心跳间隔（秒） |
| `ping_timeout` | 20 | channel.py:97 · client.py:43, 165 | 心跳超时即断连（秒） |
| `DEFAULT_MAX_CONNECTIONS` | 32 | version.py:40 · config.py:144 · channel.py::_channel_handshake | 在线连接数上限：第 N+1 条连接收到 `overloaded`（可重试）并关闭 1013 `connection limit`；在线的连接不受影响，同通道重连不算新增 |
| `DEFAULT_MAX_QUEUED_INBOUND` | 32 | version.py:41 · config.py:145 · session.py::accept | 单会话排队入站达上限：新输入收到 `overloaded`（可重试），已接受的照常处理；重复发送（同 env_id）不走这道闸 |
| `DEFAULT_RATE_LIMIT_MSGS` | 60 | version.py:42 · config.py:146 · channel.py::_rate_ok | 该连接本窗口入站帧超限：回 `rate_limited`（可重试）且不处理这一帧；超过 2 倍即断这条连接（1008 `rate limit`） |
| `DEFAULT_RATE_LIMIT_WINDOW_S` | 10.0 | version.py:43 · config.py:147 · channel.py::_rate_ok | 上面那个窗口的长度（秒）；固定窗口计数，不是令牌桶 |
| `DEFAULT_MAX_ATTACHMENTS` | 3 | version.py:46 · config.py:151 · ump.py::_attachments_of | 单条消息附件条数上限（协商取小）：超限 `unsupported_capability`，不落库 |
| `DEFAULT_MAX_ATTACHMENT_BYTES` | 524288 | version.py:47 · config.py:152 · ump.py::_attachments_of | 单个附件解码后字节上限（协商取小）：超限同样是 `unsupported_capability` |

同一类容量事实（未进上表，代码为据）：

- 协商限额 `core.max_text_len` / `core.max_parts` 默认取上面两个 `DEFAULT_*`（config.py:133-134）；握手取双方 min（channel.py:53-60），结果持久化到通道行（channel.py:185 · store.py::channel_set_handshake）；投递时按**当前**协商值复核（session.py:594-606, 697-702）。
- 上下文历史上限 `context_history_max = 20`（config.py:135 · session.py:664）。
- 睡眠期合并等待 `sleep_wait_min_s = 30.0` / `sleep_wait_max_s = 120.0` 秒（config.py:109-110 · session.py:528-532），一批只取一拍、不按条数叠加（session.py:323-329）。
- 管理面历史分页 `limit` 缺省 50（channel.py:552-556 · store.py::history_page）。
- 生成预算 `MAX_COMPLETION_BUDGET = 32768`（llm.py:23, 110）属模型调用侧，不是传输上限。

**未实现 / 未定义（代码里没有的东西，别当它有）：**

- 客户端接收队列上限：**未定义**（client.py:40 `asyncio.Queue()` 未给 `maxsize`）。
- 每连接发送队列上限 / 背压阈值：**未定义**（channel.py:50 只有串行化发送锁 `send_lock`，缓冲交给 websockets）。
- 入站文本的**字节**上限：未单独设（只有码点数上限与 1 MiB 帧上限）。
- 插件 stderr：**容量上限已实现**（`plugins.py:34-35` `STDERR_KEEP_LINES=200` 只留最近 200 行、`STDERR_LINE_CHARS=500` 单行截断；实测 `scripts/_audit2_chan.py` 刷 300 行 stderr 只留上限条数）；**脱敏未做**——只保证容量，不净化插件自行写出的内容（§六 已声明不给这个保证）。
- 附件与流式的尺寸上限：**附件已落地**（`max_attachments` / `max_attachment_bytes`，见上表与 ④）；流式的尺寸上限随该能力本身一起做（§七 仍后置）。
- 客户端重连退避（`desktop/src/main.ts:331` `RECONNECT_DELAYS_MS`）与生成 / 记忆预算类配额不属本文范围（§九 另条、各自 SPEC）。

**已实现（原先记在这一节，2026-09-22 落地，行为验收 `tests/test_channel_limits.py` 5 项 / `tests/test_attachments.py` 4 项）：**

- 入站队列容量上限 → `core.max_queued_inbound`（默认 32）：排队满了拒新输入（`overloaded` / 可重试），已接受的不丢。
- 在线连接数上限 → `core.max_connections`（默认 32）：超出只拒新连接（`overloaded` + 1013），不动在线的。
- 速率限制 → `core.rate_limit_msgs` / `core.rate_limit_window_s`（默认 60 帧 / 10 秒）：超限帧回 `rate_limited`，持续超限断这条连接。
- `binding.state=revoked` 产出方 → 换代时先给旧绑定发 revoked 再发 active（`channel.py::_thread_bind`）。
- `status.state=interrupted` 产出方 → 轮次被打断（回滚 / 重绑 / 冻结 / 删除期间作废）时夹在 thinking 与 idle 之间发出（`session.py` 三处 drop 分支）。
- 附件 / 富媒体（`attachments` 能力位 + 配额，`message.attachments` 列）→ 见 ② `user_message` 行与 ④；**流式仍未做**（`stream` 字段仍然显式拒绝）。

复跑对拍：`.venv/Scripts/python.exe scripts/_audit2_proto_doc.py`（比对本文 ②③⑤ 的集合与数值；不一致即 FAIL 并打印两侧差异）。
