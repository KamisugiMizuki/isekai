# `effect_superseded` 该不该进 `runtime_dump` / `SNAPSHOT_SECTIONS`？

- 结论文件（**只出结论，不实施**）：任务 C3 欠账②，文档 `docs/worldruntime/KERNEL_OPTIMIZATION_TASKS_2026-10-10.md:570` 登记。
- 依据：对 `isekai_core/store.py`、`isekai_core/store_state_domains.py`、`isekai_core/runtime/service.py`、
  `isekai_core/world/portable.py` 的**原文阅读**，加一次一次性 `python -c` 探针（未落任何脚本文件）。
- 本轮代码状态：`ISEKAI_EFFECT_RETIRE` **默认停用**；写入钩子只有**一处**（推进路径），
  `NOT EXISTS` 过滤也只有**一处**（`effect_window`）。详见 §5 的不一致清单。

---

## 1. 结论（明确建议）

**不进。现在不做。**

理由（按权重）：

1. **默认停用 ⇒ 收益为零、成本非零。** 开关默认 `0`（`store.py:1224`，文档第 1260-1261 行裁决），
   此时全流程既不写也不读 `effect_superseded`。加一个快照分节意味着**每一次提交快照 / 每一次便携包导出**
   都要多一条查询、并在 payload 里多一个恒为 `[]` 的键。项目自己的纪律（S-4 索引瘦身，`store.py:1872-1878`）
   就是「没有查询使用的结构是纯成本」——同一个理由在这里成立，而且是每次提交都付。
2. **这个改动的正确触发条件是「B-7 转正」，不是「补文档欠账」。** B-7 是**语义改动**
   （「哪条后果算当前生效」变了 ⇒ 文档第 489 行要求递增规则版本），而受控 A/B 已判定净负
   （文档第 1252-1258 行：启用 6.95 vs 停用 5.28 ms/世界日）⇒ 默认停用，且文档第 1266 行明确
   「不需要为它递增规则版本」。所以恢复侧这一刀应当与「B-7 转正」同批做，而不是单独做。
3. **文档「只损失热路径优化、不影响正确性」这句话**（第 570 行）在**开关打开时**并不准确，
   见 §2：回滚后「至多一条活跃」不变量确实会短期失效（下一次同族写入自愈）。
   也就是说，这条欠账的性质是「B-7 转正时的**前置阻塞项**」，不是「可以永远搁置的脚注」。
   这与上面的结论不矛盾：**默认停用下**它不影响正确性；**启用后**它是阻塞项。

一句话给 Lead：**保持不进；把「B-7 若转正，必须同批补恢复侧」写进 B-7 的准入条件。**

---

## 2. 读代码得到的依据

### 2.1 它随回滚清空，且回滚后不会被恢复

| 事实 | 代码位置 |
|---|---|
| `effect_superseded` 已登记为 `CLEARED_ON_ROLLBACK`（附理由：后果 id 稳定，旧记录会挡住回滚后重建的同 id 后果） | `store_state_domains.py:58-61` |
| `timeline_clear_state` 按注册表**逐表** `DELETE FROM {table} WHERE timeline_id=?` | `store.py:3281-3282` |
| `runtime_dump` 共 26 个分节，**没有** `effect_superseded` | `store.py:3815-3995` |
| `runtime_load` 写回 `payload["effects"]`，**没有**写回 `effect_superseded`，也**没有**重放 `_supersede_prior_effects` | `store.py:4106-4114`；对照 `store.py:3582-3583`（唯一调用点） |
| `runtime_load(clear=True)` 正是回滚的落点 | `service.py:869` |

`_relabel_payload`（`store.py:982-999`）对**任意 list 分节**做实例 / 线重标，所以若新增分节，
它这一层**不需要改**；`world/portable.py:130` 直接 `**store.runtime_dump(...)` 展开，导出侧**也不需要改**。

### 2.2 语义：它是「仍然有效、只是离开热路径」，不是 `active=0`

`_supersede_prior_effects`（`store.py:5301-5334`）只 `INSERT INTO effect_superseded`，
**不改 `active`、不删行**；`effect_superseded` 表 DDL 的注释也把两套语义写开了（`store.py:600-611`）。
所以：

- 快照**已有** `effects` 分节（`store.py:3902-3908`）会把被取代的行**原样**带回去（`active` 仍为 1）；
  丢的只是「谁取代了谁」这层记账。
- 因此缺失 **不是**「读少了一行事实」，而是「热路径过滤条件在恢复后失效」。

### 2.3 回滚后的实际状态（探针实测，一次性 `python -c`，未落盘）

开关 `ISEKAI_EFFECT_RETIRE=1`，同一 `(target, kind, family)` = `env-1 / environment_state / ef-1`
写两条 `until_cleared` 设值后果（`fx-a@100`、`fx-b@200`）：

```
ledger after a  []                                  # 第一条无可取代前项
ledger after b  [('fx-a', 'fx-b', 200)]              # 记账真的在写（推进路径）
effect_window   ['fx-b']                             # 至多一条活跃
raw effect_state [('fx-a', 1), ('fx-b', 1)]          # 不删事实、active 仍为 1
runtime_dump 分节数 26，含 effect_superseded? False
timeline_clear_state 后 ledger 0
runtime_load(dump) 后：effects 2 行、ledger 0 行、effect_window ['fx-a', 'fx-b']   ← 不变量未恢复
```

即：**回滚把后果行整体写回，但记账没跟着回来**，`effect_window` 的 `NOT EXISTS` 找不到任何行，
于是同族历史后果全部重新可见，直到该族下一次写入时 `_supersede_prior_effects`
按 `active=1` 重扫该组并补记（自愈，但**不是原子恢复**）。

### 2.4 「至多一条活跃」在别的失效方式上本来就不成立

`_supersede_prior_effects` 的前项条件是 `active=1 AND expiry='until_cleared'`（`store.py:5321`），
所以同族里 `with_cause` / `natural_recovery` 的后果按设计**不**被取代。
这解释了为什么 `effects_due`（`store.py:5623-5658`）**不需要**过滤：它只取
`expiry='with_cause'` 与 `expiry='natural_recovery'`，与退休的取值域不相交。
本文件与 `tests/test_effect_retire.py` 的断言口径都写成「同族**设值型 `until_cleared`** 至多一条」。

### 2.5 一个实现层注意点（做恢复侧时必须知道）

`_supersede_prior_effects` 的规则实现是「**最后写入者胜**」，不是「`from_world` 最大者胜」：
它标记同组**所有**其它 `active=1` 的 `until_cleared` 行，不看 `from_world` 大小。
正常推进按时间递增写，两者等价；但任何**恢复 / 重放**实现都必须保证重放顺序
= 推进时的写入顺序（后果 `from_world` 升序；同刻用 `seq`，而 `seq` 由
`_same_instant_order`（`store.py:1108-1129`）按「优先档位 + 稳定 id」定死），
否则重建出的 `superseded_by` 可能与原库不同。

---

## 3. 若将来要做（B-7 转正时），必须同时改的地方

### 方案 A（按文档字面：进快照分节）

| # | 位置 | 改什么 | 漏掉的后果 |
|---|---|---|---|
| 1 | `store.py:3815-3995` `runtime_dump` | 新增 `"effect_superseded"` 分节：`WHERE instance_id=? AND timeline_id=? AND at_world<=?`，`ORDER BY at_world, effect_id` | 不加就仍是本次欠账 |
| 2 | `store.py:3997+` `runtime_load` | 新增插入循环 `payload.get("effect_superseded")`，`ON CONFLICT(instance_id,timeline_id,effect_id) DO NOTHING` | 快照带了也白带：回滚后照样丢 |
| 3 | `store_state_domains.py:110-137` `SNAPSHOT_SECTIONS` | 加 `"effect_superseded"` | `tests/test_state_domains.py:62-77` 守卫立刻变红（**这是设计好的拦截**，不是可选步骤） |
| 4 | `_relabel_payload`（`store.py:982-999`）/ `portable.build_container`（`portable.py:130`）/ `known_tables`（已在 `CLEARED_ON_ROLLBACK`） | **不需要改**（泛型 list 处理 + 展开 + 已登记） | — |
| 5 | 快照回归面 | 重跑 `tests/test_snapshot_delta.py`、`tests/test_snapshot_equivalence.py`、`tests/test_state_domains.py`、`tests/test_delivery_audit.py` | 快照字节变了而没人核对 |
| 6 | `DATA_FORMAT_VERSION`（`isekai_core/version.py`） | **显式决定**（建议不升主版本：老件缺该分节 → `payload.get(...)` 取空即可；新件被老读端忽略未知分节）。若决定升，则同步 `test_version_fingerprint.py` | 「没决定」本身是欠账；项目纪律要求写明理由 |

`at_world` 就是**取代者**的 `from_world`（`store.py:5326`），与 `runtime_dump` 其它分节的
水位截断语义一致：`watermark` 落在 A 与 B 之间时，A 在、记账行不在，
恰好表示「那一刻 A 仍是最新」——截断键选得对。

### 方案 B（更省，推荐在真的要恢复一致性时采用）

**不进 `runtime_dump`**，改在 `runtime_load` 装载完 `payload["effects"]` 之后，
按 `from_world, seq, id` 顺序对每条「设值型 + `until_cleared`」行重放一次
`_supersede_prior_effects`（仅在 `self.effect_retire_enabled` 时）。

- 优点：**低频路径**（只在回滚 / 分叉 / 导入时跑）、不加分节 ⇒ 不必改 `SNAPSHOT_SECTIONS` 守卫、
  不增加每次提交的快照体积、老件照常可读。
- 代价（必须写进注释）：它不是**逐字节**保留原记账——「当时被取代、后来又被解除（`active=0`）」
  的行在重放里不会被补记（重放条件要求 `active=1`），审计口径会少这几行；
  热路径行为不受影响（`active=0` 本来也不进 `effect_window`）。
- 前提：重放顺序必须按 §2.5 的结论固定，否则 `superseded_by` 可能与原库不同。

### 无论选哪个方案，都必须一起做（否则开关打开也不产生收益）

| 位置 | 问题 |
|---|---|
| `effect_constraints`（`store.py:5591-5621`） | **推进路径真正的窄取数**（`service.py:2847`），SQL 里**没有** `NOT EXISTS` 过滤；启用开关后 `advance` 仍会取回同族全部历史后果。`effect_window` 的过滤只作用于 `active_effects is None` 的回退调用（`service.py:3110-3113`）等读路径 ⇒ 这正是实测「热路径 3,900 → 4 条却没改善」的一个结构原因 |
| `effect_active_exists`（`store.py:5293-5299`） | 方法文档字符串写着「B-7：被取代的后果不再算参与热路径」，但 SQL 只有 `active=1`，**没有**过滤，也不受开关控制 → 文档字符串与实现不一致 |
| `RULES_VERSION` | B-7 是语义改动（文档第 489 行），转正必须递增；默认停用期间按文档第 1266 行不递增 |

---

## 4. 不做的后果（现状下的准确表述）

1. **默认停用：零正确性后果。** 表不写不读，快照 / 回滚 / 导出导入全都不受影响；
   代价只是「将来 B-7 转正时要补一刀」。
2. **开关打开（当前代码即可做到 `ISEKAI_EFFECT_RETIRE=1`）：**
   - 回滚 / 分叉 / 导入后 `effect_superseded` 为空，而 `effects` 分节把同族历史后果全写回 ⇒
     `effect_window` 的「至多一条」不变量失效，直到该族下一次写入才自愈
     （由 `tests/test_effect_retire.py::test_gap_rollback_reloads_effects_but_not_the_supersede_ledger`
     特征化锁住；补上恢复侧后该用例必须同步改成断言 1 条）；
   - 由于 §3 结尾那条，**推进路径本来也没被过滤**，所以开关打开的真实收益仍待重新测量
     （这也与受控 A/B 的净负结论一致）。
3. 因此「缺它只损失热路径优化、不影响正确性」（文档第 570 行）应修正为：
   **默认停用下成立；开关打开后是「不变量短期失效 + 推进路径无收益」**。

---

## 5. 发现的文档 / 代码不一致（以代码为准）

| # | 文档说法 | 代码事实 | 影响 |
|---|---|---|---|
| 1 | 第 499-500 行（第十九轮）：「`_supersede_prior_effects` 在**两处**写入点（`apply_runtime_batch` + `runtime_load`）」「`effect_window` / `effects_due`（两条）/ `effect_active_exists` **四处** `NOT EXISTS`」 | 只有 **1 处**写入点（`store.py:3582-3583`，受开关控制）与 **1 处**过滤（`store.py:5577-5581`，`effect_window`）；`effects_due`、`effect_active_exists`、`effect_constraints` 都没有过滤 | 文档是**历史轮次记录**（第二十三轮选了方案 B 撤掉四处过滤，第二十四轮只按开关恢复「写入钩子 + `effect_window` 过滤」）。第 570 行的欠账描述需按此修订，否则会据「四处已过滤」做错误判断 |
| 2 | 第 570 行：「缺它只损失热路径优化，不影响正确性」 | 开关打开时回滚后「至多一条」不变量失效（§2.3 实测） | 欠账性质应从「脚注」升级为「B-7 转正的阻塞项」 |
| 3 | `store.py:5294`（`effect_active_exists` 文档字符串）：「B-7：被取代的后果不再算参与热路径」 | 该 SQL 无任何 B-7 过滤 | 读代码的人会误以为这里已退休；§3 表里列了修法 |
| 4 | 第 1195 行（第二十二轮）把 B-7 一整套描述为「新表 + 两处写入钩子 + 四处查询过滤」，第 1261 行又说「写入钩子与 `effect_window` 过滤受开关控制」 | 现码 = 第 1261 行的口径（1 写入点 + 1 过滤） | 同上；引用时应只引第 1244-1277 行（最终处置轮） |
| 5 | 第 471 行累加型名单：`activity_constraint` / `route_blocked` / `source_delay` / `rumor_spread` / `public_notice` | `SETTING_EFFECT_KINDS = ("environment_state","custom_state","institution_state")`（`store.py:30`），未归类的按累加型处理 | **一致**，无差异；`tests/test_effect_retire.py` 已逐字锁住 |
| 6 | 第 508-514 / 1252-1258 行的性能数字 | 未复核（本轮不做性能测量） | 只作引用，不作为本结论依据 |

---

## 6. 若 `effect_superseded` 明细进入运行时导出面（附）

`runtime_dump` 同时是**提交快照**与**便携导出件**的载荷（`portable.py:130`）。
方案 A 会让每次提交快照都带该分节；方案 B 不会。若将来改用「离线 / 归档通道」处置 B-7
（文档第 1267 行的下一步），那么**导出面需要的可能正是这份冷数据归档**——
届时再决定它进 `runtime_dump` 还是单独进导出清单。本文件不预设该决定，只登记判据。
