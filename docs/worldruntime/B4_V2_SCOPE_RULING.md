# B-4 v2 范围裁决书（S1–S3 已实现；S4 缓做）

**状态**：**S1–S3 已实现并验收**（2026-10-10，第 63 轮实施记录）；**S4 缓做**（理由见下）。
人类裁决（本轮确认）：**接受递增 `RULES_VERSION`（0.1 → 0.2）及其兼容性阻断**，按最小形态 S1–S5 实施。

| 步 | 内容 | 状态 |
|---|---|---|
| **S1** | `sources[]` 新增**可选** `region`（必须是 `world.regions[].id`，未登记即拒） | ✅ |
| **S2** | `claim_rows` 接入拓扑：`earliest_world = 时刻 + delay_seconds + 跳数 × hop_delay_seconds` | ✅ |
| **S3** | `grants` 的亲历判定由零跳扩展为「区域在效果 target 区域的可达集合内」 | ✅ |
| **S4** | 生活线活动窗口加可选 `place`（地点绑定） | ⏸ **缓做**（见 §4） |
| **S5** | 结构断言（不得出现寻路 / 坐标 / 距离符号） | ✅ 沿用 v1 的 AST 断言，`space` 的调用点已纳入 |

**规则版本**：`RULES_VERSION` 已由 `0.1` → `0.2`（`isekai_core/version.py`），并在 `WORLD_RUNTIME_SPEC` §十一
写明用途、判定边界与纯增量判据。**未声明 `regions` / `hop_delay_seconds` / 来源 `region` 的世界包行为逐字节不变。**

**测试**：`tests/test_space_topology.py` 8 → **15 项**；全量 `pytest -q` = **769 passed / 0 failed**。

**上游裁决**（`KERNEL_OPTIMIZATION_TASKS_2026-10-10.md` 第 388 行）：
用途**只限三处**：① 说法传播延迟；② 事件影响范围；③ 生活线活动可达性；并要求 ④ 结构断言测试。

v1 已落地（第三十七轮）：`world.regions[].adjacent` 声明 + 校验（引用闭集 / 不得自指 / 代价必须是 1…3 整数 /
**拒绝任何坐标·距离类字段**）+ `runtime/space.py`（`hop_cost` / `reachable` 纯函数）+ 两条结构断言。
v1 明确标注：**这三处消费者都缺空间绑定，接入列为 v2**。

## 1. 核实结果：三处消费者各自缺什么

| # | 消费者 | 现状 | 缺的绑定 |
|---|---|---|---|
| ① | 说法传播延迟 | `events.claim_rows` 的 `earliest_world = world_seconds + max(0, delay)`，`delay` 只来自 `sources[].delay_seconds` | **来源没有区域**：`sources[]` 只校验 `id` / `reach` / `delay_seconds` / `audience`，**没有 `region`** ⇒ 无从计算「隔几跳」 |
| ② | 事件影响范围 | `events.grants` 的 `involved = role in targets or region_of(card) in targets` | 这是**零跳**判定：只在「角色的区域恰好等于效果 target」时算受影响；`reachable` **完全没被调用** |
| ③ | 生活线可达性 | `life_plan.windows` 只有活动与时间，**没有地点** | 窗口缺 `place` ⇒ 「这个角色到得了吗」无从问起 |

**结论：三处确实都缺绑定**（v1 的诚实标注是准确的）。

## 2. 关键发现：① 一旦接入就是**第二档（改语义）**

`del` 说明为什么这件事不能顺手做：

- `earliest_world` 决定的是**角色什么时候获知**（`events.grants` 只在 `earliest_world <= world_seconds` 时才成立；
  `service` 的传播按它下推）。
- 把「来源区域到事件区域要几跳」折进延迟，就是**同一锁定设定 + 种子 + 前序状态下，角色不同的时刻知道不同的事**
  ⇒ 世界走上不同的路。
- 因此它**不能作为纯增量悄悄合入**，必须：
  1. **递增 `RULES_VERSION`**（`version.py`，现为 `"0.1"`）——这会让所有既有实例进入兼容性阻断路径
     （`world/instances.py:213-218`：`data_format`/`rules_version` 不匹配即要求「在副本上转换后使用」）；
  2. 在 `WORLD_RUNTIME_SPEC` 里写明「说法传播延迟 = 来源声明的延迟 + 跳数 × 每跳延迟」这条口径；
  3. 明确「未声明 `regions` / 来源未声明 `region` 的世界包 ⇒ 逐字节不变」作为回归判据。

**这是本轮唯一需要裁决的点。**

## 3. 若要实施，建议的最小形态（供裁决用，尚未开工）

| 步 | 内容 | 性质 |
|---|---|---|
| S1 | `sources[]` 新增**可选** `region`（必须是 `world.regions[].id`；未声明 ⇒ 零跳，行为不变） | 纯增量 |
| S2 | `events.claim_rows` 增加 `regions` 与 `hop_delay` 入参：`delay = delay_seconds + hops × hop_delay`，`hops` 取「事件效果 target 里的区域」到「来源 region」的**最小 `reachable` 代价**；没有可用区域 ⇒ 0 | **改语义（需规则版本）** |
| S3 | `events.grants` ②：把 `region_of(card) in targets` 扩展为「`region_of(card)` 在**效果 target 区域的 `reachable` 集合**内」 | **改语义（需规则版本）** |
| S4 | ③ 生活线可达性：给活动窗口加可选 `place`，用 `reachable` 判定角色能否到达 | 改语义（需规则版本） |
| S5 | 结构断言扩展：核心里**不得出现**寻路 / 坐标 / 距离符号（沿用 v1 的 AST 断言，并把 `reachable` 的调用点纳入） | 纯守卫 |

**风险**：S2/S3 都会改变「谁知道什么、什么时候知道」。S3 尤其要注意
「效果 target 是区域」与「target 是角色 / 职位 / 环境」混在同一字段里 ⇒ 必须只对**区域标识**做可达扩展，
其余 target 保持零跳（否则会把角色 id 当区域查，静默得到空集合 = 另一种静默丢数据）。

## 4. 实施结果与剩下一项

1. **S1–S3 已落地**（改动见第 63 轮实施记录）：`sources[].region` 校验、`claim_rows` 的跳数折算、
   `grants` 的可达范围判定；并把 `world.regions` / `events.hop_delay_seconds` 接进 `service` 的四个调用点。
2. **S4（生活线地点绑定）缓做**，理由：它是**三类绑定里唯一需要新增数据字段**的一项
   （活动窗口要有 `place`），而当前 `life_plan.windows` 由生活线模板生成、**没有任何地点来源**可绑——
   要做就得先决定「地点从哪来」（角色卡的常驻区域？活动类型 → 区域的映射？包的日常结构？）。
   那是**内容供给 + 语义**两层的问题，不该顺手塞进本次拓扑接入。**它与 S1–S3 无依赖关系**，
   因此缓做不影响已落地部分的自洽性。
3. **未做的事**：没有把拓扑用于寻路 / 连续距离 / 渲染 / 通行模拟（结构断言守着这条线）。
