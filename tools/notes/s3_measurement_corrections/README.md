# 第 59 轮的测量脚本（含**已被证伪的探针**——请不要直接引用它们的结论）

这里放的是 Lead 在第 59 轮测 S-3 时用的脚本。分两类，**必须区别对待**：

## A. 结论已被证伪的探针（保留作为「错误长什么样」的证据）

| 脚本 | 它错在哪 |
|---|---|
| `s3_table_cost.py` | 用 **trace 回调展开后的 SQL** 当 Counter 键 ⇒ 参数逐次不同的语句被拆成几十个一次性形状 ⇒ **全内核最贵的那条 UPDATE 少报约 7×**；另把语句间的 Python 时间记到上一条语句头上（`ms/条` 对廉价语句膨胀 5–9×） |
| `s3_effect_callsites.py` | 基于上一行的错误画像去追「`effect_state` 的 4 条 SELECT」，而它们只值 ≈0.31 ms/日；真正 1.0 ms/日 的是那条 UPDATE |
| `s3_mechanism_probe.py` | 归档清单（`knowledge`/`claim`/`event`/`experience`）**恰好排除了 `effect_state`** ⇒ 按设计不可能测到机制；且归档臂被切成 `journal_mode=DELETE`、对照臂仍是 WAL |
| `s3_cache_masking_probe.py` | 缓存臂强制 `mmap_size=0`，不是生产配置（生产 256 MB） |
| `s3_dump_cost.py` | 本身没错，但用它支撑「归档会缩小 `runtime_dump`」**不成立**——设计书要求归档行进快照 |
| `lead_s3_ab.py` | 第一版拿 `git show HEAD:store.py` 当基线，而 HEAD 早于 A-5/A-7（连 `has_events` 都没有）⇒ 跨了太多改动，不是单变量对照。**已改为「当前 store.py 只还原待测的那一处」**（见 `_store_before*.py` 的生成逻辑） |

**裁定：这些脚本的结论一律不得引用。** 需要数据请用 `tools/notes/s3_adversarial_review/` 里的正确口径脚本。

## B. 结论仍然成立、且被采纳的脚本

| 脚本 | 作用 | 关键读数 |
|---|---|---|
| `lead_s3_deadrow_check.py` | 独立复现「只归档死行」 | **0.92×（64 MB）/ 0.94×（2 MB）**，并与对照逐语句调用次数/返回行数 0 差异 |
| `lead_s3_ab2.py` | 两处第一档修法的合并 A/B（基线 = 当前 store.py 只还原那两处） | **0.63×**，成对 7/7（0.57–0.65） |
| `lead_accept_ab.py` | 验收比值的新旧同轮对照 | 1.72× → **1.26×** |
| `lead_s3_update_why.py` | 那条 UPDATE 为什么贵 | `SCAN effect_state` → `SEARCH USING PRIMARY KEY`；单条 0.313 → 0.167 ms |
| `lead_s3_reaction_idx.py` | `reaction` 索引收益 | 单条 0.4172 → **0.0187 ms（22.3×）** |

## 方法论教训（写在这里，免得下次再犯）

1. **trace 回调给的是展开后的 SQL** —— 拿它做聚合键必然把「同一形状」拆碎。要聚合先归一化
   （去字面量 / 用连接代理按原始 SQL 计数）。
2. **口径要选对**：`execute()` 返回后 `fetchall()` 才是取数成本；区间计时会把语句间的 Python 时间摊派进去。
3. **探针的对照臂必须真的只差一个变量**（journal_mode、mmap_size、缓存大小都要对齐生产）。
4. **窗口太短不能下「无效果」的定论**：3 轮 × 5 天 ≈ 每轮十几毫秒，而本机运行间噪声 ±20%。
   正确表述是「在此窗口内不可分辨，待重测」。
5. **归档清单必须覆盖「真正热的那张表」**：只归档大而冷的表，测不到任何机制。
