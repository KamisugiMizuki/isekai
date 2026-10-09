## B-1 v2 裁决书（先写后改）：由事件效果改变压力量

**状态**：**已实现并验收**（2026-10-10，第 60 轮实施记录）。
改动落点：`store.py`（新表 + `pressure_list` / `pressure_apply`）、`store_state_domains.py`（A-7 登记）、
`runtime/service.py`（`_pressure_delta` + 写入方 + 把 Δ 传给规划 + **压力量并进 `target` 下推集合**）、
`runtime/events.py`（`pressure_values` / `draw_slot` / `plan_day` 接受 `pressure_delta`）、
`world/validate.py`（闭集 + 优先级 + `{kind, target, value}` 白名单校验）。
**测试**：`tests/test_pressures.py` 9 → **23 项**；全量 `pytest -q` = **747 passed / 0 failed**。
**规则版本**：**不递增**（现有世界包与现有实例行为逐字节不变）。
**实现中抓到的一个真缺陷**（见第 60 轮 §3）：`effect_constraints` 的 `target` 下推原先不含压力量，
导致写入方拿不到数据——由**端到端测试**（而非单元测试）暴露。

**上游裁决**（`KERNEL_OPTIMIZATION_TASKS_2026-10-10.md` 第 401 行）：
> **B-1** 压力标量 → 候选权重 | **只做声明式压力量 + 写死的线性权重形式** | 压力量声明式（`{id, 取值域, 初始值, 来源, 作用域}`）；
> 权重只允许**写死形式**（如 `base × (1 + k × pressure)`），**不给表达式语言**——理由：自由 `weight_expr` 等于在核心里开一个脚本引擎入口。
> 守卫：**禁止**由用户行为 / 体验指标 / 现实时间推导（裁决原则 3）。

### 1. v1 现状（已落地，不改动其行为）

| 落点 | 内容 |
|---|---|
| `runtime/events.py` | `pressure_values(package, *, day_index)`：`value = clamp(初始值 + drift × 世界日, 下限, 上限)`——**世界时间的纯函数**；`modulated_weight(template, pressures)`：`weight = base × (1000 + k × pressure) // 1000`，`k` 是千分比整数 |
| `world/validate.py` | `pressures` 段校验；模板 `pressure` 只允许 `{id, k}` 两个键（多一个即拒） |
| 性质 | **不新增状态域、不持久化、不需要规则版本**（未声明 `pressures` 的包行为逐字节不变） |

`events.py:123` 明确写着：「由事件效果改变压力量**需要持久状态与状态域登记，属 v2**」。本裁决书就是 v2。

### 2. 问题：v1 的压力量是纯函数，事件无法影响它

于是「局势紧张 → 更容易出事」只表达**随时间单调漂移**，无法表达「一场灾难之后局势真的变紧张了」。
这正是设计文档 §3.2 B-1 要的东西：压力量要能被**世界内的事件**推动。

### 3. 裁决：累积增量 + 写死线性形式，**不改权重公式**

**3.1 存储形态（新状态域 `pressure_state`）**

| 项 | 决定 | 理由 |
|---|---|---|
| 表 | `pressure_state(instance_id, timeline_id, pressure_id, world_seconds, delta)`，主键 `(instance_id, timeline_id, pressure_id)` | 与 `world_ledger` / `relation_state` **同一形态**（B-3+B-9、B-2 v1 的先例），最省心 |
| 语义 | 行 = **累积增量** `Δ`（整数），不是绝对压力值 | 绝对值得从「纯函数基线 + Δ」算出 ⇒ **基线公式仍是单一真源**，v1 行为不被复制成第二份实现 |
| 生效值 | `effective = clamp(基线 + Δ, 下限, 上限)`；`drift` 基线照旧由世界时间算 | 夹取用包内声明的取值域，保证 `modulated_weight` 的输入仍在声明域内 |
| 写入 | 由**后果**（`effect_state` 里 `kind='pressure_change'` 的行）确定性地求出，`world_seconds <= 水位` | **不是新的事实来源**——`Δ` 是既有后果的纯函数 ⇒ 可复算、可回滚、无第二事实源 |
| 幂等 | `_write` 用 `ON CONFLICT DO UPDATE`，同一 `(id, 水位)` 重算得同一值 | 分批与整批等价 |

**关键决定：`Δ` 是既有后果的纯函数，而不是独立累加的事实。**
这样「同一条后果被算两次」不可能发生（幂等），回滚也不需要特殊处理（后果行随回滚清空 ⇒ Δ 自然回到当时值）。

**3.2 权重形式：一个字都不改**

沿用 v1 的 `base × (1000 + k × pressure) // 1000`。v2 **只改 pressure 的来源**（基线 + Δ），
**不引入任何新的表达式能力**，也不新增 `k` 之外的键。

**3.3 效果 schema：`pressure_change` 只允许 `{target, value}`**

| 键 | 约束 |
|---|---|
| `target` | **必须**是已声明的 `pressures[].id`（未声明即创建期拒绝） |
| `value` | **必须是整数**（增量，可为负）；且 `|value|` 不得超过该压力量声明域的宽度（防止一条效果把世界掀翻） |
| 其余键 | **一律拒绝**（沿用 B-6.2 / B-1 的「多一个键即拒」纪律：不给表达式入口） |

**为什么不做「表达式」或「按比例」**：`value` 是整数增量，语义可校验、可枚举、可审计；
`expr` / `formula` / `ratio` 一律拒绝。这与「不做 HP 系统」「不引入坐标」是同一条纪律。

### 4. 兼容性与规则版本

| 项 | 决定 | 依据 |
|---|---|---|
| 未声明 `pressures` 的包 | **逐字节不变**（不建行、不读表、不改权重） | 与 B-6.1 / B-1 v1 / B-3+B-9 / B-2 v1 同理 |
| 声明了 `pressures` 但**没有任何 `pressure_change` 后果**的包 | **逐字节不变**（Δ 恒为 0） | 「纯增量」的判据就是这一条 |
| 声明了 `pressure_change` 的新包 | 世界演化会不同 | **这是新内容的能力，不是既有世界的改变** |
| **规则版本** | **不递增** | 判据：**现有世界包与现有实例的行为逐字节不变**。v2 只让「新包可以表达事件推动压力」。（若将来发现某个既有样例包用了它，才需要重新裁决。） |

### 5. 必须同时改的地方（缺一即「建了表没人写」）

1. `store.py`：新表 DDL + `pressure_list`（按线取 Δ）+ `pressure_apply`（把后果折算成 Δ，幂等）；
2. `store_state_domains.py`：登记进 `CLEARED_ON_ROLLBACK`（**A-7 守卫会拦**，漏登记直接红灯）；
3. `runtime/service.py`：`advance` 接上**写入方**（每批一次，由已固化的后果折算）；
   并把 Δ 传给事件规划（`plan_day` → `draw_slot`）；
4. `runtime/events.py`：`draw_slot` / `plan_day` 接受可选的 `pressure_delta`，并入基线与夹取；
   **默认 `None` ⇒ 行为与 v1 逐字相同**；
5. `world/validate.py`：`pressure_change` 进 `SUPPORTED_EFFECTS` + `EFFECT_PRIORITY`，
   并校验 `{target, value}` 白名单、声明的压力量存在、整数增量、域宽上限。

### 6. 验收（可反证）

| # | 断言 |
|---|---|
| ① | 未声明 `pressures` / 无 `pressure_change` 后果 ⇒ 权重与 v1 **逐值相同**（回归） |
| ② | 有一条 `pressure_change` 后果时，Δ 真的被写入 `pressure_state` 且 `effective` 落在声明域内 |
| ③ | 同族/同水位重算**幂等**（不重复累加） |
| ④ | 回滚后 `pressure_state` 被清空（A-7 登记生效） |
| ⑤ | 校验：未声明的 `target` 被拒 / 非整数 `value` 被拒 / 多余键被拒 |
| ⑥ | `draw_slot` 在给定 Δ 下**确定性**，且 Δ 改变时**逐日序列发生变化**（机制真的生效） |

### 7. 风险

| 风险 | 缓解 |
|---|---|
| 压力量漂移到域外使所有权重归零 | 夹取到声明域；`modulated_weight` 已有 `max(0, …)` 下限；取值域必须显式声明 |
| 一条后果把世界掀翻 | `|value|` 不得超过域宽 |
| 与 v1 基线重复实现 | Δ 是增量、基线仍是 `pressure_values` 单一真源 |
| 「又建了一个没人写的状态域」 | 写入方与表**同批**落地，并有测试断言 `pressure_state` 真的出现行 |
