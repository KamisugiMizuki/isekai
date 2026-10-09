# S-3（冷数据归档）对抗性复核报告

复核对象：Lead 的结论「**S-3 按设计不能改善每批成本，不应立项**」
复核方式：读他的 4 个探针脚本 + 仓库方法论（`KERNEL_OPTIMIZATION_TASKS_2026-10-10.md`），
然后**自己重测**（用独立的口径：代理计时 / 原始 SQL 计数 / `EXPLAIN QUERY PLAN` / 行数缩放 / 同库归档表模型）。
所有新脚本在 `.hermes/s3r_*.py`，未改 `isekai_core/**`、`tests/**`、`docs/**`、`tools/**`，未跑任何 git 命令。

---

## 0. 三行结论

1. **他的两个探针测错了对象。** 全内核最贵的一条语句是
   `UPDATE effect_state SET active=0, cleared_at=? WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)`
   （`isekai_core/store.py:3660-3664`），`EXPLAIN QUERY PLAN` 是 **`SCAN effect_state`（全表扫描）**，
   **成本正比于 `effect_state` 的行数**。他的 `s3_mechanism_probe.py` 的归档清单
   （`.hermes/s3_mechanism_probe.py:41-46`）**恰好把 `effect_state` 排除了**，因此它按设计**不可能**
   测出 S-3 的机制。
2. **公平实验做出来了：归档确实更快。** 只把推进路径**永不读取**的 `active=0` 行移出 `effect_state`
   （语句次数、返回行数、世界状态逐表逐位一致 ⇒ 同一份工作），交替口径：
   **0.80× / 0.84× / 0.83×（三轮独立复现，成对 19/19 全部 <1）**；
   其中「移入**同库**归档表、文件大小不变（15,880 → 15,924 KB）」这一臂是 **0.88×**——
   收益**与文件大小无关**，来自热表被扫描的行变少。
3. **但 S-3 不该按原计划上。** 同一份钱有一条便宜得多的路：给那条 UPDATE 补上主键前缀，
   单条 **0.2994 → 0.0235 ms（12.7×）**，约省 **0.7 ms/日（≈18%）**，
   不改语义、不动快照/回滚/版本。S-3 用几十倍的工作量只能拿到 ~15%。

---

## 1. 逐条判定

| # | 他的主张 | 判定 |
|---|---|---|
| 1 | 归因：热点不在大而冷的表上（claim 0.14 / knowledge 0.07 / effect_state 1.3 of 3.4 ms/日） | **WEAKENED**（方向对、数不准、且由此得出的"effect_state 是尺寸无关热点"是**错的**） |
| 2 | 机制：文件 −27% 无加速（1.05×），2 MB 页缓存也只有 0.93× | **REFUTED** |
| 3 | 旧证据：`s3_archive_probe.py` 的 0.74× 无效，因为它删了 effect_state/reaction/life_plan 的**活行** | **SUPPORTED**（结论对，理由部分不准；且不能反推"归档没用"） |

---

## 2. 主张 1（归因）——WEAKENED

### 2.1 单位 / 除数：**没有问题**（我检查了）
- 1 批 = 1 世界日（`service.py:2871-2873`：`day = day_index(processed); stop = min(target, (day+1)*day_seconds)`），
  且 `day_seconds=86400`（`ab` 的 world_package），目标取 `processed + DAYS*86400` ⇒ 3 轮 × 5 天 = **15 批**（实测打印 `批数 15`）。
- `s3_table_cost.py:127` 的 `cost_by_table[t]/total_batches` 与 `:83` 的 `spent*1000/batches` 都是 ms/批 = ms/日，**除数正确**。
- 我按他的脚本原样跑：`中位数 3.01 ms/日`，逐表 ms/日 之和 ≈ 3.12（区间口径按定义覆盖全部墙钟），自洽。

### 2.2 区间计时口径：**不是语句成本**（方向对，量级错）
`sqlite3` 的 trace 回调在语句**开始执行前**触发，所以 `cost[last[0]] += now - last[1]`（`:62-67`）把
「上一条语句的执行 + **两条语句之间的 Python 时间**」都记在上一条语句头上。方向是对的（注释也写了），
但这不是 SQL 成本：
- `SELECT * FROM instance WHERE id=?`：他的口径 **0.075 ms/条**，真值 **8.4 µs**（9×）；
- `SELECT * FROM timeline WHERE id=?`：**0.016 ms** vs 真值 **3.3 µs**（5×）。
- 我用连接代理（execute+fetch 内计时）实测：一股推进 = 墙钟 4.20 ms/日，其中 sqlite 调用 2.63 ms/日、
  事务上下文 `__exit__` 0.41 ms/日 ⇒ 约 **1.1 ms/日（≈28%）根本不在任何语句里**，
  而区间口径把这 1.1 ms/日**摊派给了各表**。因此**表级份额可用、`ms/条` 列不可用**。

### 2.3 表归因正则：**这次没被打破**（我试了）
我把一次真实推进的 43 种原始 SQL 全抓下来逐条核对（`.hermes/s3r_attr_check.py`）：
- 43/43 都是**单表语句**，没有 JOIN、没有 `WITH`、没有 `main.`/`temp.` 限定、没有注释；
- 唯一"命中多表"的是 `INSERT INTO unit(...) ... ON CONFLICT(...) DO UPDATE SET ...`
  ——`DO UPDATE SET` 被正则当成「表名 = set」；因为 `INTO` 在前，`table_of` 仍取到 `unit`，**没有实际误归因**。
- `<none>` 桶里装的其实是 `BEGIN`/`COMMIT`（2.0 条/批，0.41–0.46 ms/日，占总墙钟 **11–13%**），
  他和他的读者都容易把它当成"杂项"而不是"事务开销"。

### 2.4 真正的错：用错误的热点画像去决定"什么杠杆有用"
- 真值（代理计时 + `EXPLAIN QUERY PLAN`，同一次推进）：

  | 语句 | 计划 | ms/条 | 条/批 | **ms/日** |
  |---|---|---|---|---|
  | `UPDATE effect_state SET active=0 …`（store.py:3660） | **SCAN effect_state** | 0.372 | 2.5–2.9 | **0.99–1.02（24%）** |
  | `SELECT id,event_id,target,kind,value,from_world FROM effect_state … active=1 AND from_world<=? AND target IN (…)`（store.py:5611） | SEARCH `ix_effect_active` + TEMP B-TREE | 1.24–1.35（每次 advance 一次） | 0.2 | 0.25–0.41 |
  | `SELECT * FROM reaction … stage IN ('active','fading') AND source_ref IN (…)` ×4 形状 | SEARCH `ix_reaction_timeline_stage`（**扫过全部 3,406 条 active 再按 source_ref 过滤，返回 0 行**） | 0.30–0.47 | 合计 ≈0.9 | 0.32–0.41 |
  | 事务 `__exit__`/COMMIT | — | — | 1.0 | 0.41–0.46 |
  | claim / knowledge 全部语句 | 只 INSERT + 窗口 SELECT（返回 **0 行/批**） | — | 5.9 / 3.9 | **0.086 / 0.041** |

- 他的结论里 `claim 0.14`、`knowledge 0.07` 比真值高 ~1.6×（Python 时间摊派），方向仍对；
- 但他在 `.hermes/s3_effect_callsites.py:3-5` 写下的前提——「`effect_state` 每批 4 条 SELECT 合计约 1.3 ms/日（36%）」——
  **是错的**：那 4 条 SELECT 只值 **≈0.3 ms/日**，1.0 ms/日 是那条 **UPDATE**。
- 他因此把 `effect_state` 判成「与库变大无关的确定性热点（S-3 解决不了）」，而事实**正好相反**：
  它的成本是 **effect_state 行数的线性函数**（见 3.2）。这个误判就是主张 2 的直接来源。

---

## 3. 主张 2（机制）——REFUTED

### 3.1 他的探针为什么测不到
`.hermes/s3_mechanism_probe.py:41-46` 的归档清单是
`knowledge / claim / event / experience`（各保留最近 40% 时间区间），注释明确写「不碰任何活跃状态表」，
把 `effect_state`、`reaction`、`life_plan` 全部排除。而：
- `effect_state` 的全部推进路径读取都带 `active=1`（`store.py:5296/5321/5561/5575/5612/5641/5652`），
  它被排除 ⇒ 那条**唯一**「成本 ∝ 热表行数」的语句在 A/B 两边**完全一样**；
- `reaction`（3,406 行 stage='active'）与 `life_plan` 被排除 ⇒ 批内取数的返回行数也不变。
- 实测确认：他的探针打印的「按表语句条数」两列**逐表完全相同**（`effect_state 11.0/11.0`、`reaction 11.0/11.0`…）
  ⇒ 它只测了**文件变小 26%** 这一个变量。我原样重跑：原库 3.70 vs 归档 3.92 = **1.06×**，与他一致。

### 3.2 决定性观测：那条 UPDATE 的成本 ∝ effect_state 行数（确定性口径）
`EXPLAIN QUERY PLAN` = **`SCAN effect_state`**。原因是主键是 `(instance_id, timeline_id, id)`，
而 WHERE 里 `instance_id` 被包在 `(? IS NULL OR instance_id=?)` 中（调用方 `instance_id_=None`，
该谓词恒真），SQLite 用不上主键前缀 ⇒ 每解除 1 条后果就**整表扫一遍**。

在 `ab` 的副本上只改行数，同一语句重复 7 轮取中位数（`.hermes/s3r_scan_scaling.py`）：

| effect_state 行数 | 库大小 | UPDATE 单条 | 相对 |
|---|---|---|---|
| 8,846（原样） | 15,880 KB | **0.2784 ms** | 1.00 |
| 4,425（只删 `active=0`） | 13,548 KB | **0.1563 ms** | 0.56 |
| 2,200 | 12,924 KB | 0.0867 ms | 0.31 |
| 200 | 12,348 KB | 0.0158 ms | 0.06 |

线性、单调、无噪声歧义。**这就是 S-3 机制的确定性证据**（方法论规则 c 的首选口径），
而他的两个探针都恰好绕开了它。

### 3.3 公平实验（我造并跑了）：同一实例、同一时段、交替、多轮取中位数
设计：只删/移**推进路径永不读取**的行（`effect_state WHERE active=0`），并用三重等式证明"同一份工作"：
1. 逐语句**调用次数**相同（320/320/320，43 种形状 0 差异）；
2. 逐语句**返回行数**相同（126/126/126，0 差异）；
3. 推进后**世界状态**相同（`active=1` 的 effect_state 及其它 11 张表内容哈希逐表相等；唯一差异就是被移走的死行）。
   另有双胞胎对照：两个未改动副本各推进 5 天，**语句次数、返回行数、全表内容摘要逐位一致**
   ⇒ 跨副本 A/B 这条方法论本身**有效**（`.hermes/s3r_determinism.py`）。

结果（`.hermes/s3r_fair_probe.py`、`.hermes/s3r_s3_model_probe.py`；日志 `s3r_fair_repl.txt`、`s3r_s3_model_result.txt`）：

| 实验 | 臂 | 文件 | ms/日 中位数 | 比值 | 成对 |
|---|---|---|---|---|---|
| 5 轮×5 天 | 原库 / 删死行+VACUUM | 15,880 / 13,548 KB | 3.63 / 2.92 | **0.80×** | [0.92,0.69,0.85,0.85,0.83] |
| 7 轮×5 天（复现） | 原库 / 删死行+VACUUM | 同上 | 3.37 / 2.85 | **0.84×** | [0.86,0.84,0.86,0.82,0.80,0.83,0.93] |
| 7 轮×5 天（模型） | A 原库 | 15,880 KB | 3.48 | 1.00 | — |
| | B 删死行+VACUUM | 13,548 KB | 2.88 | **0.83×** | 7/7 <1 |
| | **C 移入同库归档表、不 VACUUM** | **15,924 KB（文件不变）** | 3.07 | **0.88×** | [0.82,0.90,0.91,0.94,0.87,0.93,0.89] |

**C 臂就是 S-3 设计书的原样**（`退休 ≠ 删除`：移入同库归档表、`effect_state` 只留热行）——
**文件一点没小，却快了 12%**。这条直接杀死他的 M1/M2 二分
（`s3_mechanism_probe.py:8-11`：「M2 间接效应 = 整库变小 ⇒ 其它表也变便宜」）；

单条语句口径（同一交替轮次内）：`UPDATE effect_state` 中位数 **0.3703 → 0.1834（B）/ 0.2422（C）ms**。

一个反讽：他的探针文件减 **27%** 换来 **1.06×（更慢）**；我的 C 臂文件 **+0.3%** 换来 **0.88×（更快）**。
⇒ 「文件大小」不是自变量，「**热语句要扫的行数**」才是。

### 3.4 探针本身的其它缺陷（不影响上面的判定，但值得记）
- 窗口太短：3 轮 × 5 天 ≈ 每轮 15–20 ms 的工作量，而他第 28 轮自己固化了「同实例同代码运行间噪声 ±20%」
  （文档 `:1411-1416`）。用这个分辨率去下"没有效果"的**定论**，违反他自己写的规则
  （`docs/worldruntime/KERNEL_OPTIMIZATION_TASKS_2026-10-10.md:1468-1475`：小于 ±20% 的差异一律标注"待重测"）。
  正确表述应是"在此窗口内不可分辨"，而不是"证明无效"。
- 缓存对照臂不是生产配置：`.hermes/s3_cache_masking_probe.py:52-53` 强制 `PRAGMA mmap_size=0`，
  而生产是 `mmap_size=268435456`（文档 `:2564`）。我原样重跑：2 MB **0.97×**、64 MB **0.99×**
  （他报的 0.93× / 1.05× 都在噪声内）——这只能说明"冷表变小的文件没影响"，同样没触及机制。
- 归档臂额外被切成 `journal_mode=DELETE`（`:72`），而对照臂仍是 WAL ⇒ 两个臂的日志模式不同，
  是混淆项（方向对归档不利，所以我的结论偏保守；模型实验里我已把三臂统一回 WAL，结果不变）。

---

## 4. 主张 3（旧证据）——SUPPORTED（理由部分不准）

`.hermes/s3_archive_probe.py:27-37` 的 PRUNE 含 `reaction / life_plan / effect_state`，`:58-59` 按时间前缀删 60%。
我在 `ab` 上算出它实际删了什么（`.hermes/s3r_archive_probe_audit.py`）：

| 表 | 总行 | 删掉 | 其中**活行** | 说明 |
|---|---|---|---|---|
| reaction | 3,408 | 2,046 | **2,046（100%）** | 全是 `stage='active'`；批内那条取数按 `stage` seek，**扫过的就是这些行**（返回 0 行）⇒ **确实删掉了工作** |
| effect_state | 8,846 | 5,394 | **2,696** | 活行 = `active=1`（4,425 中的 61%），既减少读取集也缩短扫描 ⇒ **确实删掉了工作** |
| claim | 10,083 | 2,580 | 窗口外（批内窗口查询返回 0 行） | 基本无工作变化 |
| knowledge / event / experience | 6,720 / 3,367 / 3,408 | 4,086 / 866 / 2,046 | 0 | 推进路径不读 |
| **life_plan** | 3,408 | 2,042 | **0** | 热取数是 `day_index IN (当前日窗口)`（`store.py:5984-5999`），删旧日**不减少任何工作** |
| unit | 4 | 0 | 0 | 没删到 |

**判定**：他的定性论证成立——那次 0.74× 里混了「每天少读 2,046 条 reaction + 2,696 条活 effect_state」，
不能当作"文件变小 ⇒ 更快"的证据。
但两处要更正：
1. 他对 **life_plan** 的指控不成立（2,042 行删了但零工作变化），对 **unit** 也不成立（删了 0 行）；
2. ——**更重要的是**：推翻这个探针**不能**推出"归档无用"。一个**只删死行**（工作量为零变化）的归档
   仍然测到 0.80–0.88×（第 3.3 节）。他把"那次实验不干净"错误地升级成了对 S-3 的死刑判决。

---

## 5. Lead 探针里的 bug（含更正后的数字）

**B1（最严重）trace 文本里的绑定参数已被展开 ⇒ 语句"形状"归并失效。**
`s3_table_cost.py:64` / `s3_mechanism_probe.py:105` 用 `calls[sql] += 1`、`cost[sql]` 直接以 **trace 传入的字符串**做键。
CPython 的 trace 回调给的是**展开后**的 SQL：实测 trace 里是
`SELECT * FROM instance WHERE id='in-c48a5cbcc64d'`，而源码里是 `WHERE id=?`（`.hermes/s3r_trace_vs_proxy.py`）。
后果：参数逐次不同的语句被拆成几十个"一次性的形状"。
- 表现：他的「单条最贵的语句形状（前 12）」里，那条全内核最贵的 `UPDATE effect_state … cleared_at=<字面量> …`
  以 3 行 × 0.045–0.050 ms/日 出现（合计 0.14 ms/日），**真值 0.99–1.02 ms/日（≈7×少报）**，
  于是它从未进入热点名单——这正是 `.hermes/s3_effect_callsites.py` 去追 4 条 SELECT 的根源。
- 更正后（代理计时 + 原始 SQL 归并，`ab`，5 天 × 3 轮）：
  `effect_state 1.38` / `reaction 0.50` / `claim 0.086` / `knowledge 0.041` / `COMMIT+事务 0.46` /
  `effect_state 的 4 条 SELECT 合计 ≈0.31`（不是 1.3）/ 总墙钟 4.22 ms/日。
- 修法：在代理里按原始 SQL 计数，或先做 `re.sub(r"'[^']*'", "'…'", sql)` + 把数字字面量归一化**再计数**
  （他的 `shape()`（`:36-40`）其实就是这个函数，但只用于**打印**，没有用于**计数**）。

**B2 区间口径把语句间的 Python 时间记到语句上**（`:62-67`）：不是单位错误，是口径错误；
`ms/条` 列对廉价语句膨胀 5–9×（见 2.2）。**更正**：真值 `instance` 8.4 µs、`timeline` 3.3 µs/条。

**B3 最后一条语句的尾巴记在回调关闭之后**（`:74-78`）：`set_trace_callback(None)` 与 `spent` 取样之后
才补 `cost[last[0]] += perf_counter() - last[1]`，把收尾的 Python 时间（含停用回调）算给了"最后一条语句"。
量级小（µs 级），但方法上应当把最后的取样放在 `advance` 返回之后、且明确排除收尾。

**B4 `batches_each = ROUNDS * DAYS` 是假设而不是实测**（`s3_mechanism_probe.py:160-161`）：
他自己的 `s3_table_cost.py:110` 用的是 `total_batches += batches`（正确）。本例恰好 15/15 相等，
但一旦目标提前到达或 `rate` 被抬高，除数就会静默偏大 ⇒ ms/日 偏小。两个脚本口径不一致，应以 `res['batches']` 为准。

**B5 `<none>` 吞掉 BEGIN/COMMIT**（`:43-45`）：2.0 条/批、0.41–0.46 ms/日（11–13%）被记成表名 `<none>`，
与"无 FROM 的其它语句"混在一起；建议单列 `<txn>`。

**B6 缓存臂不是生产配置**（`s3_cache_masking_probe.py:52-53` 强制 `mmap_size=0`）；且代理只在**第一次
`execute`** 时下发 PRAGMA，任何不走 `execute` 的路径（`executescript`/`backup`）会绕过它。

**B7 归档臂偷换了 `journal_mode`**（`s3_mechanism_probe.py:72` → DELETE，对照臂仍是 WAL）。

（**没有**发现的 bug：除数/单位（1 批 = 1 世界日，实测 15 批）、表归因正则（43/43 单表、无误归因）。
这两项他的实现对。）

---

## 6. 能不能构造公平实验？——能，已跑（这就是第 3.3 节），并给出替代修法

同一批"钱"有三条路，量化对比（`.hermes/s3r_fix_bench.py`，每条 80 次执行 × 7 轮取中位数）：

| 修法 | 计划 | ms/条 | 相对现状 | 折算 ms/日（按 2.4–2.9 条/批） | 占墙钟 |
|---|---|---|---|---|---|
| 现状 | `SCAN effect_state` | 0.2994 | 1.00 | +0.72 ~ +0.87 | 18–24% |
| **A：补主键前缀**（`WHERE instance_id=? AND timeline_id=? AND id=? AND active=1`） | `SEARCH … sqlite_autoindex_effect_state_1` | **0.0235** | **0.08（12.7×）** | 省 **≈0.70** | ≈18% |
| B：S-3 归档死行（热表 8,846 → 4,425 行） | 仍是 SCAN，行数减半 | 0.1557 | 0.52 | 省 ≈0.45 | ≈12% |

| 第二条 | 计划 | ms/条 | 相对 | 折算 |
|---|---|---|---|---|
| `reaction … stage IN (…) AND source_ref IN (…)` 现状 | SEARCH `ix_reaction_timeline_stage`（扫 3,406 行、返回 0） | 0.3453 | 1.00 | +0.32 ~ +0.41 ms/日 |
| C：加索引 `(timeline_id, source_ref)` | SEARCH 新索引 | **0.0094** | **0.03（37×）** | 省 ≈0.35 ms/日（≈9%） |

两条修法都在**第一档**（不改语义、不碰版本/快照/回滚），合计约 **1.0–1.1 ms/日**，
是本实例墙钟（3.4–4.2 ms/日）的 **≈25–30%**；而 S-3 在本实例上的可实现份额约 **0.5 ms/日（≈13–15%）**，
且要付设计书里那三处（A-7 注册表 / `SNAPSHOT_SECTIONS` / 回滚清空，文档 `:2132`）+ 规则版本的成本。

S-3 的**上限**也很清楚：可归档的只有 `effect_state` 的 4,425 条死行。
`effect_constraints` 那 0.25–0.41 ms/日 读的是 **`active=1`** 的行（不可归档）、
`reaction` 三连 0.32–0.41 ms/日 读的是**活跃**反应、事务 0.41–0.46 ms/日 和 ~1.1 ms/日 的 Python 都不受归档影响。

**顺带一个对他有利的反向提醒**：第 45/54/56 轮把 S-3 的剩余价值记在「缩小 `runtime_dump`（124 ms = 提交的 52–83%）」上
（文档 `:2443`、`:2522`）。但设计书要求归档表**必须进快照、必须随回滚恢复**（`:2132`）——
若归档行仍要出现在快照里，`runtime_dump` 的行数不会减少，这个收益就是不成立的。
立项前必须先测「归档后 `runtime_dump` 的行数/耗时」，否则第 54 轮的论点会重演第 55 轮那次
「把 62.7 ms 对应错了对象」的更正。

---

## 7. 底线：S-3 —— 不立项（照原设计）；先做两条一档修法，之后按残余重新评估

- 他的**结论**（"S-3 不改善每批成本"）**站不住**：机制实测存在且可复现（0.80–0.88×，成对 19/19）。
- 但**该不该立项**要按"每单位工作量的收益"判：同样 ~15% 的每批收益，
  补主键前缀是**一处 SQL + 一个参数**（12.7× on 那条语句，≈18%），加一条索引再拿 ≈9%，
  而 S-3 要动状态域注册、快照分节、回滚一致性、规则版本，且**在同一实例上只能拿到 ≈13–15%**。
- 因此建议：
  1. **先做**（第一档，不改语义）：① `store.apply_runtime_batch` 加 `instance_id` 参数，
     把 `clear_effects` 的 UPDATE 改成主键前缀等值（`service.py:2941` 的调用方本就有 `instance_id`）；
     ② 给 `reaction` 加 `(timeline_id, source_ref)` 索引（S-4 已有先例）。做完用交替口径复测（预期 0.7–0.75×）。
  2. **再判 S-3**：只有当① ② 落地后残余的"行数相关成本"仍显著、且**先测出**归档能缩小 `runtime_dump`
     的行数时，才按原设计立项；否则**降级为不立项，把第 1 批验收条款正式改判为"由已完成的两处修法承接"**。
  3. 无论是否立项：**他的两个探针脚本应当废掉**（它们按设计测不到机制），
     `s3_table_cost.py` 的计数键与计时口径必须按 B1/B2 修，否则它给出的热点名单会继续误导下一步。

---

## 8. 复现用脚本（全在 `.hermes/`）

| 脚本 | 作用 |
|---|---|
| `s3r_census.py` | 两实例行数/日历/水位 |
| `s3r_stmt_prof.py` | **正确口径**的逐语句计时（execute+fetch）+ 返回行数 + 表归因 |
| `s3r_plan_audit.py` + `s3r_attr_check.py` | 抓全部原始 SQL、对每条热语句跑 `EXPLAIN QUERY PLAN`、核对归因正则 |
| `s3r_update_bench.py` / `s3r_scan_scaling.py` | 那条 UPDATE 的计划与「成本 ∝ effect_state 行数」的 4 点缩放 |
| `s3r_fix_bench.py` | 现状 vs 补主键前缀 vs 归档 vs reaction 加索引 |
| `s3r_fair_probe.py` | **公平实验**：只删死行，三重等式证明 + 交替 5/7 轮 |
| `s3r_s3_model_probe.py` | **S-3 原样模型**：移入同库归档表（文件不变）三臂对照 |
| `s3r_determinism.py` | 双胞胎确定性 + 「死行以外状态逐表一致」证明 |
| `s3r_trace_vs_proxy.py` | trace 回调 vs 代理计数（证明参数被展开、BEGIN/COMMIT 是额外 10 条） |
| `s3r_archive_probe_audit.py` | 旧探针到底删了多少**活行** |
| `s3r_stmt_truth.py` | 单条语句真值（8.4 µs vs 75 µs） |
| 日志 | `s3r_fair_repl.txt`、`s3r_s3_model_result.txt`、`s3r_cache_rerun.txt` |

关键命令：`.venv\Scripts\python.exe .hermes\s3r_fair_probe.py .hermes/ab 5 7`
