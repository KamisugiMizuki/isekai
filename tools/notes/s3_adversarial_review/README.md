# S-3 对抗式复核（证伪「归档无收益」的那份证据链）

## 为什么这份东西在仓库里

第 59 轮我（Lead）在做 S-3（冷数据归档）立项测量时，得出「归档不改善每批成本」的结论。
这份报告是一次**对抗式复核**的产物，它的任务是**证伪**那个结论——而且它成功了。

把它入库的唯一理由：**它同时给出了「我的结论错在哪」「我的探针错在哪」以及一条可复现的公平实验**。
只把它留在 `.hermes/`（未入库、随时可能丢）等于把一次真实的自我纠错也丢掉。

## 复核的最终判定（照抄报告 §1）

| # | 我原先的主张 | 判定 |
|---|---|---|
| 1 | 热点不在大而冷的表上（`claim` 0.14 / `knowledge` 0.07 ms/日） | **WEAKENED**（方向对、数不准，且由此得出的热点画像是**错的**） |
| 2 | 库体 −27% 无加速 ⇒ 归档机制不存在 | **REFUTED**（机制存在；我的归档清单恰好排除了 `effect_state`） |
| 3 | 第 45 轮那个 0.74× 读数不可信 | **SUPPORTED**（结论对，理由部分不准；且**不能**反推「归档无用」） |

## 真实结论（第 59 轮据此改写文档）

1. **归档机制是真的**：只移走推进路径永不读取的 `effect_state WHERE active=0` 行，
   用三重等式证明「同一份工作」（调用次数 0 差异、返回行数 0 差异、`active=1` 及其余表内容逐位一致），
   交替口径 **0.80× / 0.84×**；「移入**同库**归档表、文件大小不变」那一臂是 **0.88×**
   ⇒ 收益来自**热语句要扫的行数**，与文件大小无关。
2. **但 S-3 不值当**：同一条 `UPDATE effect_state` 的**根因是它用不上主键索引**
   （`WHERE … AND (? IS NULL OR instance_id=?)` ⇒ `SCAN effect_state`）。
   补上 `instance_id` 前缀后单条 **0.2994 → 0.0235 ms（12.7×）**；
   再给 `reaction` 加一条 `(timeline_id, source_ref)` 索引（单条 **22.3×**）。
   两处都是**第一档**（不改语义、不动快照/回滚、不需要规则版本），实测把老实例压到 **0.63×**，
   验收比值 **2.26× → 1.18×（首次达标）**。
3. 因此 **S-3 降级为不立项**——理由不是「机制不存在」，而是「有便宜一个数量级的等价手段」。

## 复现

```powershell
# 公平实验（只删死行，交替 5 轮）：预期 0.80× 左右
.venv\Scripts\python.exe tools\notes\s3_adversarial_review\s3r_fair_probe.py .hermes\ab 5 5

# S-3 原样模型（移入同库归档表，文件不变）：预期 0.83× / 0.88×
.venv\Scripts\python.exe tools\notes\s3_adversarial_review\s3r_s3_model_probe.py

# 那条 UPDATE 的计划与「成本 ∝ effect_state 行数」的四点缩放
.venv\Scripts\python.exe tools\notes\s3_adversarial_review\s3r_update_bench.py
.venv\Scripts\python.exe tools\notes\s3_adversarial_review\s3r_scan_scaling.py

# 三条路的单条成本对比（现状 vs 补主键前缀 vs 归档 vs reaction 加索引）
.venv\Scripts\python.exe tools\notes\s3_adversarial_review\s3r_fix_bench.py
```

**靶子依赖**：这些脚本需要一个「已老化的实例」（约 1,500 世界日）。仓库不附带实例；
可用 `acceptance_ab.py` / 既有夹具先造一份，或对任意 `root` 传入路径。
缺少靶子时脚本会失败，这是预期的——它们不是 CI 测试。

**它们不是测试**：不设退出码、不进 pytest（会因实例形态而变化）。pytest 里对应的**确定性**守卫是
`tests/test_query_plans.py`（锁 `EXPLAIN QUERY PLAN` 的 `SCAN`/`SEARCH`）。

## 脚本清单

| 脚本 | 作用 |
|---|---|
| `s3r_stmt_prof.py` | **正确口径**的逐语句计时（execute+fetch 内计时）+ 返回行数 + 表归因 |
| `s3r_plan_audit.py` / `s3r_attr_check.py` | 抓全部原始 SQL、逐条 `EXPLAIN QUERY PLAN`、核对归因正则 |
| `s3r_update_bench.py` / `s3r_scan_scaling.py` | 那条 UPDATE 的计划，与「成本 ∝ 行数」的四点缩放 |
| `s3r_fix_bench.py` | 现状 vs 补主键前缀 vs 归档 vs reaction 加索引 |
| `s3r_fair_probe.py` | **公平实验**：只删死行 + 三重等式证明 + 交替多轮 |
| `s3r_s3_model_probe.py` | **S-3 原样模型**：移入同库归档表（文件不变）三臂对照 |
| `s3r_determinism.py` | 双胞胎确定性 / 「死行以外状态逐表一致」证明 |
| `s3r_trace_vs_proxy.py` | trace 回调 vs 连接代理计数（证明绑定参数被展开、BEGIN/COMMIT 是额外 10 条） |
| `s3r_archive_probe_audit.py` | 第 45 轮旧探针到底删了多少**活行** |
| `s3r_stmt_truth.py` | 单条语句真值（8.4 µs vs 我口径下的 75 µs） |
| `s3r_index_plan.py` / `s3r_census.py` | 索引计划普查 / 两实例行数水位 |

日志：`s3r_fair_repl.txt`、`s3r_s3_model_result.txt`、`s3r_cache_rerun.txt`。

## 边界（诚实标注）

- 复核者算出的「S-3 可实现上限约 13–15%」是对**删死行 + VACUUM** 那一臂的估计；
  同库归档表模型实测 **0.88×**（约 12%）。两者都远低于两处第一档修法的 **0.63×**。
- 报告里若干绝对耗时（如 0.2994 ms/条）与我在 `tools/notes/s3_measurement_corrections/` 复现的
  0.313 ms 略有差异，属不同轮次/参数下的正常量级差；**结论不依赖这些绝对值**。
