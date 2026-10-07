# 设计文档整体筛查：功能优化与效率 / 表现机会（2026-10-08）

> 筛查对象：`docs/` 下的规范类设计文档（WorldRuntime 总纲与各层 SPEC、Core Debugging、OC 故事层、Writing Assistant、TRPG 客户端与规则层、用户界面与首次配置）。
> 方法：逐篇通读 + 与 `isekai_core/`、`desktop/` 现实现交叉核对 + 关键路径实测量化（脚本 `scripts/_audit_perf_screen.py`）。
> 结论：**设计整体是自洽且可实现的**；本报告筛出的是「规范里写死的机制会在正常使用中必然浪费」的部分，而不是风格意见。
> 用法：`P0` 建议在本轮或下一轮修；`P1` 按模块排期；`P2` 与一致性项随下次文档对账一起处理。

---

## 一、结论摘要

### 1.1 P0：真实成本高、或与文档承诺直接冲突

| # | 问题 | 文档锚点 | 实测 / 证据 | 最小改动建议 | 预期收益 |
|---|---|---|---|---|---|
| P0-1 | **默认倍率上限远超时钟排水能力**：倍率上限 2,592,000 世界秒/现实秒，而 `_clock_tick`（5 s × 4 批）每秒只能排水 69,120 世界秒 —— 差 37.5 倍，积压只增不减 | `WORLD_RUNTIME_SPEC` §2.2 / §2.6；`config.py:117-120`；`app.py:402` | rate=2592000：每拍积压 **+146 世界日**，6 拍后 890 天且永远 `catching_up`；理论排水 69,120 | 把批次预算改成**墙钟时间盒**（跑到追平或约 200 ms 为止），或由排水预算反推 `rate_max` | 高倍率真正可用；「追赶受限」回到瞬态而不是常态；样例世界下追平仅占约 60% 单核 |
| P0-2 | **补算单批成本随累计历史增长**：单批内多次全量/无窗口查询（事件 id、有效后果、说法、400 行事件窗），补算总代价 O(天数 × 历史) | `WORLD_RUNTIME_SPEC` §2.6 / §12；`service.py:2602-2678`；`store.py:4659-4724` | 连推 240 世界日：单批 **2.4 ms → 27.5 ms（11.3×）**；profile 中 `fetchall` 占 38%，`effect_window` 每批 3 次、每次约 2,000 行 | 查询按 `(from_world, to_world]` 加界；候选集每批查一次往下传；`claim_list` 加水位 | 补算从 O(天数×历史) 降到 O(天数×批量)，长区间补算不再越跑越慢 |
| P0-3 | **记忆衰减每批逐行 UPDATE 且命中不了主键**：`UPDATE memory … WHERE id=?` 走全表扫描，`advance` 每个世界日批次都调一次 | `MEMORY_SPEC` §六 / §十；`store.py:4316-4339`；`service.py:2677` | 8000 条记忆：一个世界日批次 **1769 ms**（0 条时为 2.8 ms）；`EXPLAIN QUERY PLAN` 为 `SCAN memory` | 衰减改为**读时惰性计算**（纯函数，只记水位）；或按 `decay_world` 区间批量更新，并给 `memory(id)` 建索引 | 批成本回到毫秒级；记忆量不再把世界时钟拖死 |
| P0-4 | **召回无候选上限、无索引**：先取该角色全部记忆再在 Python 里排序，向量腿同样全量解包算余弦 | `MEMORY_SPEC` §5.1 / §十；`store.py:4058-4067, 4282-4314`；`service.py:1566-1601` | 8000 条：**召回 114 ms/轮**（0 条时 0.08 ms）；无 `(instance, timeline, character, learned_world)` 索引 | SQL 层候选上限（时间窗 + 强度阈值）+ 覆盖索引；向量腿设行数阈值或走近似检索 | 每轮对话的召回从 O(全部记忆) 变成有界；这是交互路径上的固定开销 |
| P0-5 | **提交快照全量物化**：diff 存储的设计被导出的「提交闭包」和导入回写打成全量 | `WORLD_RUNTIME_SPEC` §5.1/§6；`store.py:2661-2698`；`portable.py:121, 471` | 本机库 20.57 MB 中 **13 条快照 = 8.62 MB（42%）**，最大单条 3.4 MB——而库里只有 26 条提交、42 条消息 | 导出按**存储形态**（kind/base/body）导出并在导入时重建 delta 链；`commit_snapshot` 加 `base_commit_id` 列 | 容器与库体积从 O(提交数×历史) 回到 O(历史+变更)；256 MiB 上限不会因正常游玩逼近 |
| P0-6 | **每轮对话有两处固定模型开销**：输入分类串在生成之前（独立调用 + 8 s 超时），回复后审计再调一次、重试重发整段 prompt | `OC_STORY_LAYER_SPEC` §3.4/§十三；`NARRATIVE_LAYER_SPEC` §6.2；`session.py:335-337`；`story/service.py:361-371`；`service.py:3232-3236` | 分类只对 `HANDOFF_TARGETS` 短路，「你今天怎么样」也要一次调用；审计在含数字时被整体跳过 | 结构与确定性判定优先，只有模糊输入才调模型；审计只补差异、重试只发改动段 | 大幅降低每轮调用数与首字延迟，同时让「数字回复」不再绕过语义审计 |
| P0-7 | **桌面端从不宣告流式**：规范第 6.2 节还明文禁止流式正文，于是整块等待、超时升级、结果查询三件套都成了必需品 | `USER_INTERFACE_DESIGN` §6.2；`CHANNEL_PLUGIN_SPEC` §2.1；`desktop/src/ump.ts:107` | 内核与 CLI 已落地 `reply_delta`（`session.py` 按能力位分流），只有桌面壳的能力集里没有 `streaming` | 桌面 hello 里加 `streaming: true` 并渲染增量预览（固化帧仍是唯一事实）；同步改 §6.2 措辞 | 首字可见；用户不再因「像卡住」而重发，等待文案与查询路径的整条补丁失去必要性 |
| P0-8 | **记忆提取的两套时间轴**：产出按世界时间、预算按现实日，缺口是结构性的 | `MEMORY_SPEC` §5.2；`config.py:127`；`app.py:346-357` | 本机库：`memory_task` 待处理 **2753** / 丢弃 191 / 完成 179；`memory` 仅 45 条 | 把额度定义在世界时间（每角色每世界日 N 条，溢出顺延），现实日预算只作安全上限 | 积压不再随倍率无限增长，也避免「先产出再丢弃」的假记忆 |

### 1.2 P1：明确的浪费或正确性风险（详见 §三）

| # | 一句话 | 锚点 |
|---|---|---|
| P1-1 | 锁定设定 JSON 每次访问重新解析：单批 5–6 次、单轮对话 ≥4 次 | `WORLD_SETTING_SPEC` §3.1；`service.py:1734` |
| P1-2 | 生活线窗口每角色每世界日约 20 条经历永久落库（本机 7,103 条经历中 **7,098 条是 `life`**，真实行动只有 5 条） | `WORLD_RUNTIME_SPEC` §11；`service.py:3925-3953` |
| P1-3 | 死亡 / 归档靠最近 400 条事件窗推断，超窗即「复生」 | `WORLD_RUNTIME_SPEC` §六；`events.py:674-679` |
| P1-4 | **`knowledge_slice` 主题命中被末尾切片丢掉**（`sort` 后取 `[-limit:]`） | `WORLD_RUNTIME_SPEC` §13.1；`cognition.py:165-168` |
| P1-5 | 历史分页「先 LIMIT 后过滤」+ 说法表全量取 + 游标含边界（页重叠） | `WORLD_RUNTIME_INTERFACE_SPEC` §4.5；`service.py:2054-2095` |
| P1-6 | 规范声明的 `topics/entities/time_range` 读取选择器被接受后丢弃 | `WORLD_RUNTIME_INTERFACE_SPEC` §4.2/§4.3；`service.py:1916-1971` |
| P1-7 | `generation.check` 对「旧水位结果」返回 `valid`，`stale` 判不出来 | `WORLD_RUNTIME_INTERFACE_SPEC` §6.3；`service.py:2132-2138` |
| P1-8 | 规则插件默认每次裁定冷启子进程（`resident` 需显式声明） | `TRPG_RULE_PLUGIN_SPEC` §11/§168；`rules.py:374-381`；`trpg.py:1296` |
| P1-9 | 规则状态只有一份大文档（规范里的 `scope_ref` 未落地）→ 每次掷骰全量读写 + 冲突时回扫全部提交 | `TRPG_CAMPAIGN_RUNTIME_SPEC` §5.2；`store.py:766-778`；`trpg.py:798, 944, 1438-1477` |
| P1-10 | 战役路径的 `context` 没有通路，插件拿不到规则输入（`preconditions` 被当成 context 用） | `TRPG_RULE_PLUGIN_SPEC` §82/§183；`trpg.py:467-477, 532` |
| P1-11 | 局面投影无界：每次刷新取全部 `trpg_action` 再切尾 5 条；无索引；裁定结果同时写进 `event.detail` | `TRPG_CLIENT_SPEC` §137/§277；`trpg.py:282-295`；`store.py:3339-3344` |
| P1-12 | `recover` 每次进入按「实例+时间线」扫全部行动，且每个在途行动重列提交台账 | `TRPG_CLIENT_SPEC` §620-632；`trpg.py:1260-1282` |
| P1-13 | 导出把整容器序列化两遍（刚算完 digest 又验一遍）、导入再序列化一遍并重复校验两轮 | `WORLD_PACKAGE_APPENDIX` §38；`portable.py:155-165, 236`；`instances.py:80-86` |
| P1-14 | 导入时先为每条提交合成全量快照，随后又用容器内容整体覆盖（纯白做功） | `WORLD_SETTING_SPEC` §7.1；`instances.py:179-188`；`portable.py:241-256, 471` |
| P1-15 | `world.package.list` 每次调用对所有包做完整校验（列表与「[校验]」动作重复） | `DESKTOP_GENERATION_WORKSPACE_SPEC` §二；`ops.py:340` |
| P1-16 | 客户端渲染/取数浪费：消息列表每个事件全量重建；写作分区切换重复取数（`wa.state` 同参两次）；推进建议重跑整轮世界观察；战役列表按世界串行 N 次 IPC；生成进度轮询每秒携带约 6.7 KB prompt | `USER_INTERFACE_DESIGN` §6.1/§7.1/§7.3/§8.1；`DESKTOP_GENERATION_WORKSPACE_SPEC` §七；`contact.ts:660-680`；`writing.ts:178-300`；`main.ts:1949` |
| P1-17 | 两个永久定时器（1.5 s 通知 + 0.6 s 退出标志，约 6,000 次/小时 IPC），而壳里已有 `emit` | `ONBOARDING_AND_RECOVERY` §8；`desktop/src/user/app.ts:243, 773` |
| P1-18 | 退出契约写 30 s 等待与用户选择，实测 3 s 后强杀（写入中途被杀即损坏窗口） | `ONBOARDING_AND_RECOVERY` §7.2；`desktop/src-tauri/src/main.rs:23, 657-678` |
| P1-19 | 睡眠期等一个随机 30–120 s，而唤醒时刻已经从生活线窗口算出来了 | `CHANNEL_PROTOCOL_APPENDIX` §⑤；`session.py:725-729` |
| P1-20 | 自动备份是 7 份全量副本，且打包期间暂停世界推进 | `ONBOARDING_AND_RECOVERY` §9.1；`backup_pack.py:302-325` |

### 1.3 P2 与一致性项

P2（低风险 / 量级尚小，但应按设计意图收口）与跨文档一致性清单见 §四、§五。

---

## 二、筛查范围与实测基线

### 2.1 覆盖的文档

| 文档组 | 篇目 |
|---|---|
| WorldRuntime 内核 | `DESIGN.md`、`WORLD_RUNTIME_SPEC.md`、`WORLD_RUNTIME_INTERFACE_SPEC.md`、`SESSION_CORE_SPEC.md`、`EVENT_ENGINE_SPEC.md`、`MEMORY_SPEC.md`、`NARRATIVE_LAYER_SPEC.md` |
| WorldRuntime 支撑 | `WORLD_SETTING_SPEC.md`、`CHARACTER_CARD_SPEC.md`、`CHANNEL_PLUGIN_SPEC.md`、`CHANNEL_PROTOCOL_APPENDIX.md`、`WORLD_PACKAGE_APPENDIX.md`、`ANDROID_SPEC.md` |
| Core Debugging | `DESKTOP_SPEC.md`、`DESKTOP_GENERATION_WORKSPACE_SPEC.md` |
| 应用层 | `OC_STORY_LAYER_SPEC.md`、`WRITING_ASSISTANT_SPEC.md`、`TRPG_CLIENT_SPEC.md`、`TRPG_CAMPAIGN_RUNTIME_SPEC.md`、`TRPG_RULES_LAYER_SPEC.md`、`TRPG_RULE_COMMON_MODULE_SPEC.md`、`TRPG_RULE_PLUGIN_SPEC.md` |
| 用户界面 | `USER_INTERFACE_DESIGN.md`、`ONBOARDING_AND_RECOVERY.md`、`user-interface/README.md` |

未纳入（并说明理由）：`docs/worldruntime/archive/**`（历史存档，不是现行规范）；`TRPG_RUNTIME_GAP_AUDIT.md` 与三份 UI 评审报告（既有审计，本报告只在其结论之外找增量，并标注了重叠项）；`QUICKSTART.md` / `DEVELOPING.md`（操作指南，其中 `DEVELOPING.md:177` 的开发员参数被引用为证据）；评价草案类文档（`*_EVALUATION_DRAFT.md`，是需求材料而非规范）。

### 2.2 实测基线

复现命令（约 40 秒，全部在临时数据根内跑，不动 `data/`）：

```powershell
.venv/Scripts/python.exe scripts/_audit_perf_screen.py
```

**A. 世界时钟推进**（样例世界「灰潮纪」+ 1 张角色卡）

| 场景 | 结果 |
|---|---|
| 单个世界日批次成本（rate=1） | 中位 **约 2 ms**（多次运行 1.9–2.8 ms），最大 2.7 ms |
| rate=1（5 s × 4 批节拍） | 排水 1 世界秒/现实秒，状态 `current`，积压 0 |
| rate=86,400（1 世界日/现实秒） | 排水 **67,680**，每拍积压 **+1 世界日**，永远 `catching_up` |
| rate=2,592,000（设计默认上限） | 排水 **69,120**，每拍积压 **+146 世界日**，6 拍后 890 天 |
| 放开批次预算（max_batches=1e9） | 排水 **2,712,960**（约 31.4 世界日/现实秒），追平 `current`，CPU 约 **60% 单核** |

推论：**批数预算（4 批/5 s = 0.8 世界日/现实秒）而不是 CPU 才是当前瓶颈**；默认倍率上限是它的 37.5 倍。

**B. 长区间补算的规模效应**（连推 240 个世界日）

| 指标 | 结果 |
|---|---|
| 单批成本 | 头 2.4 ms → 中 14.9 ms → 尾 **27.5 ms**（**11.3×**） |
| 总耗时 | 3.7 s / 240 世界日 |
| 期间累计 | 事件 464、获知 460、经历 721 |
| cProfile（150 批） | `sqlite3.fetchall` 0.725 s / 1.9 s（38%）；`effect_window` 0.597 s（每批 3 次，共 312,285 行 dict 转换）；`claim_list` 0.139 s |

**C. 记忆规模**（0 / 500 / 2000 / 8000 条；单位毫秒）

| 记忆数 | 单批（含衰减） | 召回 | 单条写入 |
|---|---|---|---|
| 0 | 2.8 | 0.08 | 0.17 |
| 500 | 12.4 | 7.0 | 9.3 |
| 2000 | 123.7 | 27.1 | 36.9 |
| 8000 | **1769.2** | **114.1** | **148.9** |

**D. 本机库结构事实**（`data/isekai.db`，只读）

| 项 | 数值 |
|---|---|
| 库大小 | 20,570,112 B |
| `commit_snapshot` | 13 行 / 8,622,112 B（**42%**）/ 最大 3.4 MB；`commit_log` 26 行、`message` 42 行 |
| `experience` | 7,103 行，其中 `life` **7,098**、`action` 5 |
| `memory_task` | 3,123 行：待处理 2,753 / 丢弃 191 / 完成 179 |
| `memory` / `event` / `timeline` | 45 / 2,245 / 17 |

> 口径说明：本机库规模很小（17 条时间线、42 条消息），所以上表是**下界**；`P0-2/P0-3/P0-4` 的趋势随历史与记忆量单调增长，真实使用只会更差。

---

## 三、P1 发现明细

每条给出：文档锚点 → 机制 → 代码证据 → 最小改动 → 收益。已在本机实测的标注「实测」，其余为静态核对（读代码到行）。

### P1-1 设定 JSON 反复解析（实测 + 静态）
`WORLD_SETTING_SPEC` §3.1 规定实例设定创建后锁定不可编辑，但 `RuntimeService.setting()` 就是 `json.loads(instance["setting"])`，且 `calendar()`、`cards()`、`_institution_rows()`、`_environment_rows()` 各自再解析一次。`advance()` 每批调用 5–6 个这样的辅助函数，`character_snapshot()`/`system_prompt()` 每轮对话解析 ≥4 次，并重复 `institution_list`×2、`custom_list`×2、`effect_window`×2、`instance_get`×4。
**改动**：按 `(instance, revision)` 做请求内 memo，并把已解析的 `setting`/`cards`/`calendar` 往下传。
**收益**：锁定的不变量被当成每次都变的东西反复解析，纯属浪费；包越大越明显。

### P1-2 生活线经历全量落库（实测）
`WORLD_RUNTIME_SPEC` §11 只要求「每角色每世界日一份有效计划」，实现把每个日程窗口的结束都写成一条 `experience`（`service.py:3925-3953`）。本机 7,103 条经历里 7,098 条是这种派生行，而这些行会进入每份提交快照、认知窗口、记忆队列与衰减扫描。设计只在**入队**时做了降采样（`LIFE_SOURCE_PER_DAY=1`），存储层没有收口。
**改动**：按「每角色每世界日一段」存，或由 `life_plan` 读时派生；`experience` 只留真实行动 / 观察 / 获知。
**收益**：经历行数降约 95%，连带提交快照、补算插入、认知查询一起变轻。

### P1-3 归档状态靠事件窗推断（静态，正确性风险）
`WORLD_RUNTIME_SPEC` §六 把寿终后的归档写成**版本化角色状态**，但没有任何字段承载它：`events.is_dead()` 只在传入的事件行里找 `death:<id>` 模板。运行路径传入的都是有界窗口（400 行），所以死亡事件一旦被 400 条更新的事件挤出窗口，`is_dead` 就返回 False——计划与经历会为一个已归档角色继续生成，与「不再推进生活线、不再产生经历」直接冲突（本机最大时间线已有 1,160 条事件，按观测的约 4 事件/世界日，越窗是可达的）。
**改动**：把 `dead_world`/归档态并入版本化角色状态，在写死亡事件的同一批里置位；窗口查询只作为一致性断言。

### P1-4 `knowledge_slice` 丢弃主题命中（静态，正确性）
`WORLD_RUNTIME_SPEC` §13.1 要求「认知接口接收查询主题（只排序不过滤）」。`cognition.py:165-168` 先 `sort(key=命中?0:1)`（命中在前），再 `return out[-limit:]`——取走的是**尾部**，也就是命中项被优先丢弃，留下的是最不相关的最新说法。候选量超 `limit`（默认 24）时必然发生。
**改动**：`return sorted(out, key=...)[:limit]`（一行），并考虑把主题过滤下推到查询。

### P1-5 / P1-6 历史分页与读取选择器（静态）
`event_window(limit=count*3)` 之后才在 Python 里过滤 `kind/source/subject/time_range`，选择性强的过滤会返回远少于 `limit` 的条目（调用方只能反复翻页）；说法侧 `claim_list()` 无界；游标用 `world_seconds <= cursor`，下一页会重复边界行。同时接口声明的 `topics/entities/entity_refs/time_range` 在 `read_snapshot`/`cognition_project` 里被接受后直接忽略，固定返回「最新 50 经历 + 50 说法」。
**改动**：过滤、`since`、游标并列条件全部下推 SQL；实现选择器或在规范里删掉它们（不要让调用方以为有作用域读取）。

### P1-7 `generation.check` 判不出 stale（静态）
接口 §6.3 的 `stale` 就是为「异步高级模块拿旧水位结果来提交」准备的，但实现只在「请求水位 > 当前水位」时给 `conflict`，请求水位**落后**于当前水位（真正的过期）返回 `valid`。`snapshot_id` 也只是 `f"snap-{processed}"` 的字符串，没有持久化句柄。
**改动**：落后即 `stale`（除非调用方显式声明接受当前 revision）；快照句柄持久化（范围 + revision + TTL）。

### P1-8 / P1-9 / P1-10 TRPG 裁定路径（静态）
- **冷启子进程**：`rules.resolve()` 只在调用方给出常驻会话时走会话，而战役侧 `_rule_session()` 要求清单里显式写 `resident: true`；插件规范把「常驻」列为可选、把插件定义成「一次请求一个进程」。于是每一次检定 / 对抗都付一次解释器启动 + 导入 + stdio 握手。
- **单一规则状态文档**：规范里对象带 `scope_ref`，落库的 `trpg_rule_state` 主键是 `(instance, timeline, campaign, ruleset)`。每次掷骰整份角色表进、整份写回；不同角色的 patch 撞同一个 revision，只能走「路径不相交则合并」的兜底，而该兜底要回扫该战役全部提交并逐条 `json.loads`。代价 = O(整份表 × 角色数 × 掷骰次数)。
- **`context` 无通路**：插件请求里的 `context`（规则输入）在战役路径上没有入口，实际传的是被当作 context 的 `preconditions` 列表；需要属性 / 技能 / DC 的插件只能拿到 `{}`，于是「静默用默认值裁定」或调用方在插件外重算，正是设计想避免的分叉。
**改动**：`campaign_resolver` 模式默认常驻（显式 `resident: false` 才冷启）；把 `scope_ref` 落进主键；给 `resolve/declare` 加显式 `context: dict` 并让客户端能传。

### P1-11 / P1-12 TRPG 投影与恢复（静态）
局面投影要求「不显示全量事件日志」，实现是 `trpg_list("action")` 取该战役**全部**行动、在 Python 里过滤后再切 `[-5:]`；`trpg_action` 没有任何索引；裁定结果同时复制进 `event.detail`。恢复路径按 `(instance, timeline)` 扫全部行动，并在每个 `committing` 行动里重复 `trpg_list("commit")` 全表。
**改动**：加 `(instance, timeline, campaign, status)` 索引 + 数据库侧 `LIMIT`；恢复按 `campaign_id` 限定并一次性取在途集合；客户端「刷新局面」改调 `refresh`（不触发恢复）。

### P1-13 / P1-14 导出导入的重复做功（静态）
`write_export` 先 `build_container`（内部算一次 sha256 规范化序列化），再 `json.dumps(indent=2)` 整容器，然后 `verify_integrity(container)` **再算一遍同一 digest**；导入端把文件读成一个 `str`、`json.loads`、再规范化一遍算 digest，同一时刻持有文本 + 解析结果 + 规范化串。校验也做了两轮（`import_instance` 一轮，`create_instance` 又一轮，每轮 `validate_assembly` 逐卡校验并重建包级 id 集）。导入还先为每条提交合成全量快照（每条要 `runtime_dump` + 每会话 10k 条历史），随后被容器内容整体覆盖。
**改动**：导出不自我复验（让 `build_container` 返回 digest）；按原始字节做摘要、单趟流式写；`create_instance` 增加「已校验」快路径；导入跳过快照合成，只由 `_restore_commit_snapshots` 写。

### P1-15 `world.package.list` 全量校验（静态）
列表 op 对目录里每个 `*.json` 跑完整 `validate_package`，而规范本身把「列表」与显式的「[校验]」分成两个动作。仓库里已有现成范式：`backup_pack.list_packs` 用 `mtime:size` 缓存校验结果。
**改动**：复用同样的缓存（或列表只做轻量结构检查，硬校验留在 `validate`/`import`）。

### P1-16 客户端渲染与取数（静态）
- 联络页每个事件（新消息、通知、状态变化，共 12 处调用点）都 `fill()` 重建整个消息列表；`loadOlder` 只是多插 50 条也整体重建，`messages` 没有上限。设计只规定了「分页读取」和「返回保留位置」，没规定渲染集合的界。
- 写作工作区每次分区切换重取 `instance.info` + `wa.outline.list` + `wa.state`×2 + 候选，其中两次 `wa.state` 参数完全相同；挂载时又重复 `instanceInfo`/`waOutlines` 一次。
- 「给我推进建议」只传目标 / 观察者 / 大纲 / 数量，不传客户端刚读到的 revision，内核于是重跑整轮世界观察（快照 + 认知投影 + 包 + 卡 + 知识切片）。
- 未选战役时客户端按世界**串行**逐个 IPC 取战役列表。
- 生成进度轮询把上一次的完整提示词（实测 6.7 KB）随每秒回执一起传，而文档自己写着「不随每次回执推送，避免把回执撑大」。
**改动**：按 `messageId` 增量追加 + 虚拟列表；按 `(instance, timeline, outline)` 缓存并显式失效；建议请求带 `observed_revision` 复用投影；加一个跨世界聚合 op；把 prompt 拆成独立 op（或 `want_prompt` 开关）。

### P1-17 / P1-18 客户端轮询与退出契约（静态）
两个 `setInterval`（1.5 s `take_pending_notice`、0.6 s `exit_pending`）在可见状态下也一直跑，约 6,000 次/小时的 WebView↔Rust 往返，而壳里已经 `app.emit("notice-open")` / `core-status`；这两个轮询只是托盘隐藏场景的兜底。退出契约写的是「30 秒后给继续等待 / 查看诊断 / 强制退出」，壳里是 `FLUSH_WAIT_MS = 3000` 到点 `kill_core`，前端自己的超时反而是 10 s——**任何超过 3 s 的正常收尾都会被中途杀掉**，正是 30 s 契约想避免的损坏窗口。
**改动**：轮询按 `document.visibilityState` 门控 + 事件驱动；退出窗口与契约对齐（并给出进度 / 中止 UI）。

### P1-19 睡眠期随机等待（静态）
角色处于睡眠窗口时，已接受的消息被压在 `processing`，任务随机睡 30–120 s 才开始生成；而 `_sleeping_now` 已经解析出生活线窗口，**距窗口结束的时间是已知的**。平均凭空增加约 75 s 首字延迟，且用户侧只能看到「处理中」。
**改动**：`min(随机拍, 距窗口结束)`，或把回复挂到唤醒时刻由世界推进结算（「一批只取一拍」的语义不变）。

### P1-20 全量备份 + 暂停推进（静态）
自动备份写一份新的完整库快照 + 全部素材（DEFLATE），保留 7 份；打包全程置 `quiet()` 暂停世界推进，以取得一致读。
**改动**：对外仍是单文件，内部改内容寻址 / 增量（未变部分只存一次），并把静默窗口缩到只覆盖库快照那一刻。

---

## 四、P2 清单

| # | 问题 | 锚点 | 建议 |
|---|---|---|---|
| P2-1 | `unbase_dependents` 用 `payload LIKE '%"base": "cid"%'` 在全部快照大 JSON 上做子串扫描 | `WORLD_RUNTIME_SPEC` §8；`store.py:2746-2770` | `commit_snapshot` 加 `base_commit_id` 列 + 索引 |
| P2-2 | `narrative.map` 对已讲单元两两求交（O(n²)）且每节点查一次消息文本 | `NARRATIVE_LAYER_SPEC` §9.4；`narrative.py:356-406` | 建 `ref → [unit]` 索引；文本批量取 |
| P2-3 | 零世界后果的提交仍走完整世界管线（归一化草稿、制度、环境、写事件） | `TRPG_CAMPAIGN_RUNTIME_SPEC` §438/§614-616；`trpg.py:818-854, 1009-1023` | 空效果快路径 |
| P2-4 | 每次确认行动 6 个独立事务，其中 `snapshotting`/`resolving` 在客户端看起来完全一样 | `TRPG_CLIENT_SPEC` §368；`trpg.py:537-543, 595-605` | 合并为一个「在途」持久状态 |
| P2-5 | 确认卡在 draft 与 confirm 各解析一次（各带 8 s 超时上限） | `TRPG_CLIENT_SPEC` §337；`trpg_client/service.py:293-298` | 工作区保存解析结果并按 revision 复用 |
| P2-6 | 选择提交未校验场景 revision、幂等键只回显不回放（与规范不符） | `TRPG_CLIENT_SPEC` §525；`trpg.py:1046-1090` | 落实或改规范；剩余候选用聚合查询 |
| P2-7 | `_plugin_for` 走 `list_plugins()` 扫目录，为比较两个版本号读全部清单；`rule_view` 为拿一个 revision 传输整份 `opaque_state` | `TRPG_CLIENT_SPEC` §162-165；`trpg_client/service.py:244, 97-129` | revision-only 读取 + 从登记簿取版本 |
| P2-8 | 就地（Android）传输 10 ms 轮询取帧、每帧双向 JSON 编解码，且不套用帧 / 文本上限 | `ANDROID_SPEC` §3.1；`local_channel.py:36-42, 71-86, 158-165` | `asyncio.Event`/Future + 复用 `channel` 的限额 |
| P2-9 | 客户端接收队列 `asyncio.Queue()` 无 `maxsize`；流式增量无累计字节预算 | `CHANNEL_PROTOCOL_APPENDIX` §⑤；`client.py:44` | 设上限 + delta 累计预算（超限停发增量、等固化帧） |
| P2-10 | 创建期强制为所有选中条目生成「一句话」文本，而惰性展开路径已存在 | `WORLD_SETTING_SPEC` §3.6；`DESKTOP_SPEC` §3.3 | 骨架 / 说法照旧，文本改惰性展开 |
| P2-11 | 角色卡 `region` 是自由文本，但效果 `target` 必须在册且 `_all_ids` 无区域登记 → 区域效果写不出来 / 拼错即静默失效 | `CHARACTER_CARD_SPEC` §21/§5.2；`WORLD_PACKAGE_APPENDIX` §88；`validate.py:729-731` | 区域登记为在册对象并在卡校验里引用校验（或明确 region 仅供显示） |
| P2-12 | 卡片 / 草稿写入直接 `"w"` 截断，与「失败不覆盖、确认后原子替换」契约不符（包写入已是 tmp+fsync+replace） | `WORLD_SETTING_SPEC` §2.4；`instances.py:416-420`；`ops.py:747-749` | 复用原子写助手 |
| P2-13 | 试演每次都做完整分叉（含把对话历史重新导入到新时间线），失败也保留分支且无回收 | `USER_INTERFACE_DESIGN` §7.4；`service.py:700-773` | 懒分叉 / 覆盖层预览，采用才落线 |
| P2-14 | AI 连接测试固定显示三阶段文案、无已等待时间，且客户端超时 180 s > 规范 120 s | `ONBOARDING_AND_RECOVERY` §4.2/§7；`settings.ts:224`；`api.ts:150` | 渲染内核返回的 `stages` + 计时；超时对齐 |
| P2-15 | 世界观 / 角色卡生成（上限 900 s）在新用户界面没有阶段、无计时、不可中止（旧壳已有 1 s 快照轮询可复用） | `ONBOARDING_AND_RECOVERY` §7；`create.ts:1552-1554` | 复用 `world.generate.snapshot` + 已等待时间 + 停止等待 |

---

## 五、跨文档一致性 / 文档腐化（会造成防御性实现）

| # | 冲突 | 位置 | 处置 |
|---|---|---|---|
| C-1 | 「禁止流式正文」vs「流式已落地」（内核与 CLI 均已实现）vs 桌面客户端未宣告 `streaming` | `USER_INTERFACE_DESIGN` §6.2 ↔ `CHANNEL_PLUGIN_SPEC` §2.1/§七 ↔ `desktop/src/ump.ts:107` | 按 P0-7 统一为「增量预览、固化帧为准」 |
| C-2 | 退出「30 s 等待 + 用户选择」vs 实现 3 s 强杀 | `ONBOARDING_AND_RECOVERY` §7.2 ↔ `main.rs:23` | 按 P1-18 对齐 |
| C-3 | 「首版不提供低风险自动确认」vs 规则层 / 战役层的 `autonomous` 自动确认 | `TRPG_CLIENT_SPEC` §364 ↔ `TRPG_RULES_LAYER_SPEC` §149/§287-295 ↔ `TRPG_CAMPAIGN_RUNTIME_SPEC` §604-607 | 二者择一：客户端实现门控（带授权记录），或规范写明客户端恒为 `assisted` |
| C-4 | patch `op` 词表写 `add/replace/remove`，插件规范示例用 `increase/decrease`（代码只认后者，且只有相对量能在并发下安全合并） | `TRPG_CAMPAIGN_RUNTIME_SPEC` §5.2 ↔ `TRPG_RULE_PLUGIN_SPEC` §100-105 | 规范补 `increase/decrease` 并标注并发安全语义 |
| C-5 | 等待文案含倒计时、「最长约 2 分钟」与「可能在休息」推断，且 8 s 即升级到结果查询；规范禁止虚构进度，另一处规定 15 s / 60 s 分级 | `USER_INTERFACE_DESIGN` §6.2 ↔ `ONBOARDING_AND_RECOVERY` §7.2 ↔ `contact.ts:884, 921-929` | 明确哪些量由内核给出；阈值对齐 15 s / 60 s |
| C-6 | 生成工作区 §3.2 写「13 个计数」，同节表格与实现都是 15（27 旋钮）；§一.3 写「本方案不新增后端 op」，实际新增了 `world.generate.snapshot` | `DESKTOP_GENERATION_WORKSPACE_SPEC` §3.2/§一.3/§八 ↔ `world/generator.py:45-61`、`world/ops.py:727-732` | 更新计数与「新增 op」清单，让逐条对账脚本比对的契约是真的 |
| C-7 | 协议附录把 `client.py` 当作桌面内建客户端，实际壳用 `desktop/src/ump.ts`；能力位因此长期不同步 | `CHANNEL_PROTOCOL_APPENDIX` §一 ↔ `desktop/src/ump.ts` | 指明两份客户端各自适用范围，或由单一能力表生成 |
| C-8 | `MEMORY_SPEC` §5.1「限定可访问集合」没有规模上界，§十 把「索引布局」留作模块设计，落地结果是无上界加载 + 无索引 | `MEMORY_SPEC` §5.1/§十 ↔ `store.py:4058, 4286` + schema `memory` | 在规范层写明候选规模上界与索引要求（否则每次只是「实现问题」） |
| C-9 | `WORLD_RUNTIME_SPEC` §2.6「停止扩大未处理目标」在「目标 = 基准世界时间 + (现实时间−基准) × 倍率」下不可达：目标只由挂钟与倍率决定，实现只能记一个 `limited` 标记 | `WORLD_RUNTIME_SPEC` §2.2/§2.6 ↔ `service.py:2582, 2598, 2646`；`config.py:117-120` | 二选一：真正持久化「目标钉住」（并在降倍率 / 冻结时解除），或写明「受限 = 只读 + 提示」并让 `rate_max` 与排水预算自洽 |
| C-10 | `WORLD_PACKAGE_APPENDIX` §38 的完整性摘要口径是「setting + runtime 的规范化序列化」，与「导出 / 导入按存储形态走增量」的目标冲突（强制全量物化才可复算） | `WORLD_PACKAGE_APPENDIX` §38 ↔ `portable.py:36-37, 155, 213-219, 227` | 摘要改为按分节原始字节计算，允许分节独立校验 |

---

## 六、建议的处置顺序

**第 1 批（纯实现、不改语义，见效最快）**
1. P0-3 记忆衰减改惰性 + 索引；P0-4 召回候选上限 + 覆盖索引（同一张表，一起做）。
2. P1-4 `[-limit:]` 修正（一行）；P1-1 设定解析 memo（一个请求作用域）。
3. P0-2 窗口化查询（`effect_window` / `claim_list` / `event_ids` / `knowledge_ids`）——先用 `EXPLAIN QUERY PLAN` 对齐索引。
4. P1-16 / P1-17 客户端增量渲染、缓存与轮询门控；P2-12 原子写。
5. P1-18 退出窗口对齐（避免真实数据损坏）。

**第 2 批（参数与调度）**
6. P0-1 批次预算改墙钟时间盒（或由排水反推 `rate_max`）+ C-9 口径统一。
7. P1-19 睡眠等待取「距唤醒」；P0-8 记忆额度按世界时间定义。
8. P1-15 包列表校验缓存；P1-13 导出自我复验移除。

**第 3 批（契约与文档对齐）**
9. P1-8 / P1-9 / P1-10 TRPG 常驻、`scope_ref`、`context`（三项都要动协议，建议一次改完并同步审核脚本）。
10. C-1…C-10 一致性对账（其中 C-4/C-5/C-6 是纯文档修正）。

**第 4 批（结构）**
11. P0-5 / P1-14 提交快照的存储形态与导入路径（配合 C-10 摘要口径）。
12. P1-2 生活线经历的表示；P1-3 归档态进版本化状态；P1-20 增量备份；P2-13 懒分叉。

> 两处**不要**按「优化」处理：`WORLD_RUNTIME_SPEC` §2.6「暂不支持的区间必须继续分批处理并显示追赶中，不能跳过」与 `MEMORY_SPEC` §4.2「最小冲突规则首版即有」都是刻意的正确性约束，本报告的改动都在不改它们语义的前提下做。

---

## 七、本轮落实回执（2026-10-08 · 规范 + 实现）

规范侧：25 篇文档逐条修订并在文末留 `## 本轮修订（2026-10-08）`（原表述 → 新表述 → 发现编号）；两处机器对拍的实现级附录保持全绿（`_audit2_proto_doc.py` 5/5、`_audit2_pkg_doc.py` 20/20）。
实现侧：`pytest -q --junit-xml` = **608 项、0 失败、0 错误、0 跳过**（含本轮新增的归档态回归测试）；关键改动都用 `_audit_perf_screen.py` 复测（§7.2）。

### 7.1 逐条状态

| 编号 | 规范落点 | 实现落点 | 状态 |
|---|---|---|---|
| P0-1 | `WORLD_RUNTIME_SPEC` §2.2/§2.6、`DESIGN` §5.3 | `advance(budget_seconds)` 墙钟时间盒 + `catch_up_all`（滞后超阈值放宽到自愈预算、按线平分）；`config.catch_up_budget_seconds=1.0` / `catch_up_max_budget_seconds=4.0` / `catch_up_tick_batches=4096`；`app` 的 tick 与启动恢复改走该路径 | **已落实**（排水 69,120 → **2,632,320** 世界秒/现实秒 ≥ 默认上限 2,592,000；6 拍后积压 0、`state=current`、CPU 18%） |
| P0-2 | `WORLD_RUNTIME_SPEC` §2.6 有界读取条 | 单批共用一份有效后果并**跨批增量维护**；`claim_list(since, until)` + `ix_claim_window`；`event_window` 过滤/`since`/严格游标下推；`_LazyIdSet`（`event_exists`/`effect_active_exists` 走主键）取代全量 id 装载；归档态改读状态位后删掉 3 处 400 行事件窗 | **已落实**（240 天单批 27.5 → **7.9 ms**，总 3.7 → **1.26 s**） |
| P0-3 | `MEMORY_SPEC` §六 | `memory_decay` 只扫 `decay_world < to_world` + `ix_memory_id` / `ix_memory_timeline_decay` | **已落实**（8000 条单批 1769 → 54 ms） |
| P0-4 | `MEMORY_SPEC` §5.1 | 召回候选上界 `memory_candidate_limit=2000`、向量腿 `LIMIT`、`ix_memory_character`；写入侧 `memory_siblings`（窄列 + 有界扫描） | **已落实**（8000 条召回 114 → 33 ms、写入 149 → 85 ms） |
| P0-5 | `WORLD_RUNTIME_SPEC` §5.2、`WORLD_SETTING_SPEC` §7.1/§7.3 | `commit_snapshot.base_commit_id` 列 + 索引、`commit_snapshot_stored/put_stored`；导出按存储形态、导入重建 delta 链 | **已落实**（同容器「物化 vs 存储形态」2.30× 体量差；往返链深度逐条一致） |
| P0-6 | `OC_STORY_LAYER_SPEC` §3.4/§3.6、`SESSION_CORE_SPEC` §4.2/§4.3、`NARRATIVE_LAYER_SPEC` §6.2 | 分类词表定论 + `decide_instant` + 会话作用域缓存 + 超时 8 → 3 s、判定与生成准备并行；审计确定性优先（`needs_semantic_audit`）、差异重试 `retry_request`、数字不再免检 | **已落实** |
| P0-7 | `USER_INTERFACE_DESIGN` §6.2、`CHANNEL_PLUGIN_SPEC` §2.1/§2.4、`DESKTOP_SPEC` §3.1 | `ump.ts` 宣告 `streaming: true`；增量画在独立待定气泡、固化帧整段替换 | **已落实**（`tsc --noEmit`、`npm run build`、`cargo check` 全通过） |
| P0-8 | `MEMORY_SPEC` §5.2 第 0 条 | `extraction_allowance`（按待处理积压跨越的世界日数，下限 6 / 上限 24）+ `memory_extract_per_world_day=2`；`app._derived_pass` 使用 | **已落实** |
| P1-1 | `WORLD_RUNTIME_SPEC` §14、`SESSION_CORE_SPEC` §4.2 | `setting()`/`calendar()` 按 `(实例, 原文指纹)` 缓存（调用方只读） | **已落实** |
| P1-2 | `WORLD_RUNTIME_SPEC` §11 | `_harvest` 改「每角色每世界日**一段**」：一天里的日程窗口切片合成一条经历（时刻标签 + 去重活动列表），真实行动 / 观察 / 获知仍各自成条；记忆侧每世界日采样规则天然满足 | **已落实**（240 世界日的经历行 721 → **241**；单批 7.1 ms、总 1.2 s 同批见效） |
| P1-3 | `WORLD_RUNTIME_SPEC` §六、`EVENT_ENGINE_SPEC` §四 | 新增 `character_state` 表（归档位 / 归档世界时刻 / 依据 / 来源），与死亡事件**同一批同一事务**落位；`runtime_dump`/`runtime_load`、回滚清线、导出导入全部随件；`_migrate_character_state` 从既有死亡事件回填老库；所有运行判据（计划 / 经历 / 主动发言 / 打算 / 死亡登记）改读状态位 | **已落实**（`tests/test_death.py::test_archive_state_outlives_the_event_window`：灌 500 条更晚事件把寿终挤出 400 行窗口后，她仍不排计划、不产生经历、不会复生） |
| P1-4 | `WORLD_RUNTIME_SPEC` §13.1 | `knowledge_slice` 命中优先保留（`[:limit]`） | **已落实**（修掉取尾丢命中的 bug） |
| P1-5 | `WORLD_RUNTIME_INTERFACE_SPEC` §4.5/§3.3 | `event_window` 过滤/`since` 下推 + 严格游标（同秒按 `seq`）；`claim_list(at_least, audience)` | **已落实** |
| P1-6 | `WORLD_RUNTIME_INTERFACE_SPEC` §4.2/§4.3 | `cognition.project` 真实生效 `time_range`/`entity_refs`/`topics`；`snapshot.read` 对 `topics`/`entities` 显式 `rejected` | **已落实** |
| P1-7 | `WORLD_RUNTIME_INTERFACE_SPEC` §6.3 | `generation_check(require_current)` + `watermark_lag`；`ops` 透传 | **已落实**（默认仍返回 lag，保住 WA 的「重新预览」契约） |
| P1-8 | `TRPG_RULE_PLUGIN_SPEC` | `rules.resident_default`（`campaign_resolver` 缺省常驻）+ 复用前探活/重开语义 | **已落实** |
| P1-9 | `TRPG_CAMPAIGN_RUNTIME_SPEC` §3.4/§5.2 | `scope_ref` 进规则状态主键 + 老库迁移 + 分片读写/分片头 | **已落实** |
| P1-10 | 插件/战役/规则层/客户端四处 | `declare/resolve(context)`、`trpg_action.context` 列、`ops` 与客户端透传 | **已落实** |
| P1-11 | `TRPG_CLIENT_SPEC` §4.2/§5.2 | `trpg_action_window`（DB 侧 LIMIT + 走 `ix_trpg_action_campaign`）；`event.detail` 只留引用 | **已落实** |
| P1-12 | `TRPG_CLIENT_SPEC` §14.1 | `recover(campaign_id)` 一次取在途集合 + 台账内存配对；`ops` 透传 | **已落实** |
| P1-13 | `WORLD_SETTING_SPEC` §7.1 | `write_export` 不再自我复验；`build_container` 同趟产出摘要 | **已落实** |
| P1-14 | `WORLD_SETTING_SPEC` §7.3 | `validated=True` / `materialize_snapshots=False` 快路径；`_all_ids` 只算一次 | **已落实** |
| P1-15 | `DESKTOP_GENERATION_WORKSPACE_SPEC` §二 | `world.package.list` 按 `mtime:size` 缓存（`data/package-verify-cache.json`），列表标 `cached`/`fresh` | **已落实** |
| P1-16 | `USER_INTERFACE_DESIGN` §6.1/§7.1/§7.3/§8.1、`WRITING_ASSISTANT_SPEC` §6.1/§6.2 | 联络页按 id 增量追加 + 渲染集合有界；写作取数缓存 + `wa.state` 单次 + `observed_revision`；战役列表并行；生成进度拆 op；WA `observe` 缓存 + `suggest(observed_revision)` + `ops` 透传 | **已落实** |
| P1-17 | `ONBOARDING_AND_RECOVERY` §8、`DESKTOP_SPEC` §二 | 两个轮询改可见性门控 + 事件驱动（可见时不再常开） | **已落实** |
| P1-18 | `ONBOARDING_AND_RECOVERY` §7.2 | 壳：`FLUSH_WAIT_MS` 30 s + 到期置 `quit_timeout`；新命令 `quit_timeout_pending` / `quit_decision(choice)`；`begin_quit` 进「继续等待（最多 2 轮）/ 查看诊断 / 强制退出」循环。前端：保存期间 1 s 盯超时、弹三选一、诊断项跳帮助页 | **已落实**（`cargo check`、`tsc --noEmit` 通过） |
| P1-19 | `SESSION_CORE_SPEC` §4.5 | `_sleep_delay = min(随机拍, 距窗口结束)`，换算不可得退回随机拍 | **已落实** |
| P1-20 | `DESKTOP_SPEC` §五 | 备份静默窗口只覆盖「库快照 + 素材字节快照」，DEFLATE 全部移出窗口；格式与 7 份轮转不变 | **已落实**（内部内容寻址增量未做，属格式变更） |
| P2-1 | `WORLD_RUNTIME_SPEC` §8 | `base_commit_id` 列 + 索引；`unbase_dependents` 等值查询（去掉 `LIKE` 全表扫） | **已落实** |
| P2-2 | `NARRATIVE_LAYER_SPEC` §9.4 | `narrative.map_payload` 改 `ref → [unit]` 倒排索引建边（同 ref 组内连边、合并重复对）；`service.narrative_map` 按收集到的 `message_id` 一次批量取正文（`store.outbound_by_message_ids`） | **已落实** |
| P2-3 | `TRPG_CAMPAIGN_RUNTIME_SPEC` §12.4 | 零世界后果快路径（跳过草稿归一化与制度/环境构造） | **已落实** |
| P2-4 | `TRPG_CAMPAIGN_RUNTIME_SPEC` §11.2 | `snapshotting` 并入 `resolving`（老库读出兼容） | **已落实** |
| P2-5 | `TRPG_CLIENT_SPEC` §4.1/§6.1 | 工作区保存解析结果、按 revision 复用、确认不再调模型 | **已落实** |
| P2-6 | `TRPG_CLIENT_SPEC` §10.2 | 选择提交校验场景 revision 并支持幂等回放；剩余候选聚合计数 | **已落实** |
| P2-7 | `TRPG_CLIENT_SPEC` §4.2 | `rule_state_view`（revision-only）+ 版本从登记簿读 | **已落实** |
| P2-8 | `ANDROID_SPEC` §3.1 | `local_channel` 事件唤醒、dict 直传、双向帧上限 | **已落实** |
| P2-9 | `CHANNEL_PROTOCOL_APPENDIX` §⑤ | 客户端接收队列 `maxsize`（只丢增量、固化帧背压）+ 单条回复增量 64 KiB 预算 | **已落实** |
| P2-10 | `WORLD_SETTING_SPEC` §3.6 | 复核：创建流程本就没有 bulk 文本调用，惰性展开是唯一入口 | **已核对**（规范与实现的字面口径差待文档确认） |
| P2-11 | `WORLD_SETTING_SPEC` §2.2/§2.3、`CHARACTER_CARD_SPEC` §二/§5.2、`WORLD_PACKAGE_APPENDIX` ④ | `world.regions[]` 校验 + `_all_ids` 并入 + 卡片 region 引用校验 + 样例包登记；运行层新增 `cognition.region_label`（显示名解析） | **已落实** |
| P2-12 | `WORLD_SETTING_SPEC` §2.4 | `atomic_write_text`（tmp + fsync + replace + 失败清理）复用到包/卡片/草稿/导出 | **已落实** |
| P2-13 | `USER_INTERFACE_DESIGN` §7.4 | 试演改「先只读预览（`runtime.change.preview`，不建线、不动世界）→ 点『应用于试演线』才 `wa.branch` 建线并切换」；对话框文案与按钮同步改写（`api.changePreview` 新增） | **已落实**（`tsc --noEmit` 通过；失败预览不留分支） |
| P2-14 | `ONBOARDING_AND_RECOVERY` §4.2 | 连接测试显示内核真实阶段 + 已等待秒数；客户端超时 180 s → 120 s | **已落实** |
| P2-15 | `ONBOARDING_AND_RECOVERY` §7 | 生成阶段 + 已等待时间 + 「停止等待」；`world.generate.snapshot(want_prompt)` | **已落实** |

一致性项 C-1…C-10 全部在对应文档落文并同步实现（C-1 流式、C-2 退出窗口、C-3 客户端恒 `assisted` + 单次确认写入、C-4 patch 词表、C-5 等待文案、C-6 生成工作区计数与新增 op、C-7 两份客户端口径、C-8 召回上界、C-9 受限态口径、C-10 分节摘要）。

### 7.2 落实前后实测对比（`scripts/_audit_perf_screen.py`）

| 指标 | 落实前 | 落实后 |
|---|---|---|
| 默认倍率上限下的排水果位 | 69,120 世界秒/现实秒（每拍积压 +146 世界日） | **2,632,320**（≥ 默认上限 2,592,000；追平、积压 0、CPU 18%） |
| 连推 240 世界日的单批成本 | 2.4 → 27.5 ms（11.3×） | 1.2 → **7.9 ms**（6.7×），总 3.7 → **1.26 s** |
| 240 世界日产生的经历行 | 721 | **241**（每角色每世界日一段） |
| 8000 条记忆的单批（含衰减） | 1769 ms | **54 ms** |
| 8000 条记忆的召回 | 114 ms | **32 ms** |
| 8000 条记忆的单条写入 | 149 ms | **80 ms** |
| 导出容器（同内容，物化 vs 存储形态） | — | 2.30× 更小 |

### 7.3 未落实项与后续批次

本轮筛查的 43 条发现（P0×8、P1×20、P2×15）与 10 条一致性项（C-1…C-10）**全部有明确归宿**：除下表这一条外，其余都已按规范落文并落实实现。

| 编号 | 现状 | 下一步 |
|---|---|---|
| P1-20（增量容器部分） | 静默窗口已按规范收窄到「库快照 + 素材只读快照」两个瞬间；**按分节内容寻址的增量容器**会改 parts / 摘要口径，本轮明确写为「后续可选演进」，不再作为当前承诺（`WORLD_RUNTIME_SPEC` §5.2、`DESKTOP_SPEC` §五、`DESIGN` §5.3 三处已同步措辞） | 若要真做：先定分节寻址格式与向后兼容读法，再改 `backup_pack` 与摘要口径 |

> 验证口径：`pytest -q --junit-xml`（607 项、0 失败）；`_audit2_ws.py` 34/34、`_audit2_ocstory.py` 23/23、`_audit2_trpgclient.py` 89/89、`_audit2_proto_doc.py` 5/5、`_audit2_pkg_doc.py` 20/20、`_audit2_trpg_layer`/`_audit2_rulecommon`/`_audit2_chan`/`_audit2_mem`/`_audit2_sc` 与改前逐条一致（既有 FAIL 为先前状态，已在干净副本复核）。

---

## 八、方法与验证状态

- **代码引用约定**：为省版面，正文里的裸模块名相对仓库根补齐如下——`service.py`/`config.py`/`app.py`/`session.py`/`channel.py`/`client.py`/`local_channel.py`/`backup_pack.py`/`plugins.py`/`store.py` 指 `isekai_core/` 下的同名文件；`events.py`/`cognition.py`/`narrative.py`/`rules.py`/`trpg.py`/`budget.py` 指 `isekai_core/runtime/` 下；`trpg_client/service.py` 与 `world/*.py` 保留子目录；`contact.ts`/`writing.ts`/`settings.ts`/`api.ts`/`create.ts`/`trpg.ts` 与 `app.ts` 指 `desktop/src/user/` 下；`main.ts` 指旧壳 `desktop/src/main.ts`；`main.rs` 指 `desktop/src-tauri/src/main.rs`。
- **文档**：上表 25 篇规范逐篇通读（含 `user-interface/README.md` 的实施状态表）。
- **代码**：`isekai_core/` 的 `runtime/`、`world/`、`story/`、`writing/`、`trpg_client/` 与 `store.py`、`session.py`、`app.py`、`channel.py`、`client.py`、`local_channel.py`、`backup_pack.py`、`config.py`、`plugins.py`；`desktop/src/user/**`、`desktop/src/ump.ts`、`desktop/src-tauri/src/main.rs`。
- **实测**：`scripts/_audit_perf_screen.py`（A/B/C 三组，输出见 §2.2）；`data/isekai.db` 的只读结构统计；`EXPLAIN QUERY PLAN` 验证 `memory` 表的 `WHERE id=?` 全表扫描。
- **交叉验证**：P0-1 由实测与静态分析两条路径独立得出（一致）；P0-5 的体量以本机库实测复核；P1-4/P1-8/P1-10/P1-11/P1-12/P1-16/P1-17/P1-18 的关键行由筛查者逐条读代码确认。
- **不确定 / 未做**：未运行全量测试（`pytest -q`，当前基线 605 项）——本报告不改核心代码，只新增一个只读基准脚本；未在真实大库（数百世界日、上万条记忆、多角色）上端到端压测，所以 P0-2/P0-3/P0-4 的绝对值是下界，趋势可靠、常数待现场确认；TRPG 与 UI 相关条目为静态核对 + 少量运行时读取，未做真人操作计时；`memories` 规模表中的 8000 条为构造数据（文本同质），真实记忆的排序 / 去重分支可能略慢。
