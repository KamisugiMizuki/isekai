# 参考审计存档（原样归档，**只读**）

本目录把此前只存在于**未入库目录 `.hermes/`** 的三套终审脚本搬进仓库，让它们在未来
`.hermes/` 被清理 / 丢失后仍可复核。归档原则是**逐字节原样**（不是「整理后重写」）：

- 归档件与 `.hermes/` 原件 SHA256 一致，见 `MANIFEST.sha256`；
- 归档件里的**已知缺陷与假阳性风险也一并保留**，只在下面的表格里注明，不做静默修补
  （修补会掩盖历史结论「当时是怎么算出来的」）。

三套审计合计 78 项：`self_audit.py` A 类 30 项、`final_audit.py` B 类 30 项、
`perf_audit.py` 性能线 18 项。

---

## 1. 文件清单与角色

| 文件 | 角色 | 能不能进 `pytest` | 说明 |
| --- | --- | --- | --- |
| `self_audit.py` | **审计（A 类 30 项）** | 断言可迁移，脚本本身不建议直接进 | 见 §3 |
| `final_audit.py` | **审计（B 类 30 项）** | 同上 | 与 `self_audit.py` 互补，两者都跑才算「全量复核」 |
| `perf_audit.py` | **审计（性能线 18 项）** | 同上 | 断言全是**结构 / 模式**检查，不含计时 |
| `index_audit.py` | **诊断工具（不是审计）** | 否 | S-4「索引瘦身」的依据：对真实热查询跑 `EXPLAIN QUERY PLAN`，报告从未被命中的索引。它**只打印、不断言、恒退出 0**，天然不能当门禁 |
| `fix_audit.py` | **历史一次性补丁** | 否 | 往 `self_audit.py` 里打「A-7 精确解析元组体」补丁 |
| `fix_self_audit.py` | **历史一次性补丁** | 否 | 往 `self_audit.py` 里打「S-4 之后索引断言翻转」补丁 |
| `fix_perf_audit.py` | **历史一次性补丁** | 否 | 往 `perf_audit.py` 里打「S-1 断言收窄到 `_collect_batch`」补丁 |

### 依赖关系（重要）

**三套审计彼此不 import，也不 import 同目录任何文件。** 它们只依赖：

1. **运行目录必须是仓库根**——脚本里写死了相对路径 `isekai_core/store.py`、
   `docs/worldruntime/EVENT_ENGINE_SPEC.md` 等。换目录跑会直接 `FileNotFoundError`。
2. **一个可用实例的 `sqlite3` 库**——默认是 `sys.argv[1] + '/data/isekai.db'`，
   即默认靶子 `.hermes/acceptF`。它们只读 `sqlite_master`（表 / 索引清单），
   **不读任何业务数据行**，所以任意一个「结构完整」的实例都能跑。
3. `fix_*.py` **不是**三套审计的依赖，而是**修改审计脚本本身**的历史补丁：它们对
   `.hermes/self_audit.py` / `.hermes/perf_audit.py` 做字符串替换，替换不到就 `assert` 失败。
   因此它们是**一次性、非幂等、会写 `.hermes/`**的，**严禁**接入 `run_audits.py`；
   归档在这里只为还原「断言为什么长现在这样」。`index_audit.py` 同理只是诊断证据，
   不在运行链上。

---

## 2. 原本跑在什么靶子上（前置条件）

| 项 | 值 |
| --- | --- |
| 命令 | `.venv\Scripts\python.exe .hermes\self_audit.py .hermes/acceptF`（三者同形，参数是**根目录**，不是 db 文件） |
| 默认靶子 | `.hermes/acceptF`（`data/isekai.db`，约 14.7 MB，61 张表） |
| 实例要求 | 由**含 S-4 索引收敛的代码**建出的实例；脚本只查 `sqlite_master`，不依赖世界日数 / 角色 / 事件规模 |
| 期望结果 | 78/78（30 + 30 + 18） |

**实测复核（2026-10-10，本次归档时）：**

| 靶子 | self_audit | final_audit | perf_audit | 合计 |
| --- | --- | --- | --- | --- |
| `.hermes/acceptF`（脚本默认靶子） | 28/30 | 30/30 | 17/18 | **75/78** |
| `.hermes/acceptH` | 30/30 | 30/30 | 18/18 | **78/78** |
| `.hermes/s3_full` | 30/30 | 30/30 | 18/18 | **78/78** |
| `.hermes/a4_bench` | 30/30 | 30/30 | 18/18 | **78/78** |
| `.hermes/chain_A` | 30/30 | 30/30 | 18/18 | **78/78** |
| `.hermes/ab` | 30/30 | 30/30 | 18/18 | **78/78** |

**结论（这是本存档最该被记住的一条）**：`acceptF` 是 **S-4 之前**建的实例，库里仍留着
`ix_effect_target` 与 `ix_effect_retire`，所以脚本默认靶子上**永远**会有 3 条 FAIL：

- `self_audit`：`索引 ix_effect_target 已按实测删除`、`B-7 索引默认不创建（开关关）`；
- `perf_audit`：`effect_state 索引已收敛（无 ix_effect_target / ix_effect_retire）`。

即「78/78」这个结论**只在 post-S-4 实例上成立**；拿默认靶子复跑会看到 75/78，属**预期**，
不是回归。`tools/run_audits.py` 因此不写死任何靶子，并把每一条 FAIL 原样透出。

---

## 3. 为什么不能直接进 `pytest`

不是「不想」，是三条硬理由：

1. **没有退出码。** 三套脚本结尾都只 `print` 汇总，**从不 `sys.exit(非零)`**——FAIL 2 条时
   退出码依然是 0。直接挂进 CI 会「红字绿门」。所以必须有一个**外层**解析汇总行并决定退出码的
   运行器（`tools/run_audits.py` 就是干这个的）。
2. **靶子是外部状态。** 断言里的数据库部分需要一个「当前代码建出的」实例；`.hermes/` 未入库，
   pytest 不能依赖它，也不该去连一个 14 MB 的临时库。可迁移的部分必须改成用**夹具里新建的库**
   （`tests/test_audit_archive.py` 用 `test_runtime.py` 的 `store` 夹具，它调 `ensure_schema()`）。
3. **断言形式是「文本 grep」，不是行为。** 例如 `'ids=known' in service_src` 只能证明这串字符
   在某处出现过；`perf_audit.py` 自己就记录过一次由此产生的**假阳性**（S-1 的全文件断言把
   合法调用误判为遗漏，见 `fix_perf_audit.py`）。迁移到 pytest 时这些断言被**收窄到所属函数段**
   或替换为**行为断言**（如 `validate.CASUALTY_GRADES == (...)`、`EFFECT_PRIORITY['casualty']`、
   `store_state_domains.cleared_tables()`、`load_config()` 的默认值），见
   `tests/test_audit_archive.py` 的映射表。

---

## 4. 已知缺陷清单（归档时发现，**未修改归档件**）

| # | 位置 | 问题 | 影响 |
| --- | --- | --- | --- |
| D-1 | 三套脚本全部 | 不设退出码，恒为 0 | 不能作门禁；必须由外部运行器解析 |
| D-2 | `self_audit.py:40` | `'"effect_superseded"' in dom_src` 是**全文件子串**检查，而 `CLEARED_ON_ROLLBACK` 在文件里被**说明文字**提到过；`fix_audit.py` 的注释明确记着「首次出现可能是说明文字」 | 该条可被散文满足（历史假阳性来源）。同一段的第 2 条已用正则精确解析，第 1 条没有 |
| D-3 | `perf_audit.py:27`（原始版本） | `sv.count('self.store.plan_get(') == 0` 全文件断言 | 把「别处合法的单日查询」误判为遗漏；已由 `fix_perf_audit.py` 收窄，但收窄手法是**字符串切片**而非语法分析 |
| D-4 | `perf_audit.py:27-29` / `self_audit.py:41-44` | 先 `sv.find('    def _collect_batch(')` 再切片，但**不校验 `find` 返回值**：方法改名后 `_start = -1`，切片退化为 `sv[-1:...]`，断言会以一种**无意义的方式**继续 PASS/FAIL | 重构后可能「假绿」 |
| D-5 | `self_audit.py:35-36` | `'store.claim_get(' in ops.py.replace('def claim_get','')`——`world/ops.py` 里其实**没有** `def claim_get`（定义在 `store.py`），这个 `replace` 是无效动作；同时 12 万字符的文件里任一 `store.claim_get(` 都能满足它 | 断言过宽，可被无关调用点满足 |
| D-6 | `self_audit.py:48` | `'secrets' not in life_src.replace('`secrets.token_hex`','')`：负向子串检查，任何提及（含注释 / 说明）都会 FAIL | 反向脆弱：合法引用会误报 |
| D-7 | `final_audit.py:59` | `B-9 点名规则结构断言就位` 的断言是 `Path('tests/test_ledger.py').exists()`——断言「测试文件存在」而非任何产品行为 | 近乎同义反复；已迁移为「文件存在**且**定义了测试函数」 |
| D-8 | `index_audit.py` 全文 | 只打印、不断言、恒退出 0 | 它是 S-4 的**证据生成器**，不是守卫；S-4 的真正守卫在 `perf_audit.py` / `self_audit.py` 与 pytest |
| D-9 | `fix_*.py` 全文 | 一次性字符串替换补丁，写 `.hermes/`，`assert` 不到目标文本即崩 | 非幂等、会改审计脚本本身；只作历史记录，**不可重复执行** |
| D-10 | `self_audit.py:10` / `final_audit.py:14` / `perf_audit.py:10` | 默认靶子写死 `.hermes/acceptF`（一个 S-4 之前的旧实例） | 无参运行**必然** 75/78，与「78/78」的历史结论冲突，容易被误读为回归 |

> 归档件**未**因此做任何修改：`MANIFEST.sha256` 记录的是「原件」，改一个字节就无法再证明
> 「当时到底跑了什么」。修正只发生在 `tools/run_audits.py`（外层）与
> `tests/test_audit_archive.py`（迁移后的断言）里。

---

## 5. 校验归档完整性

```pwsh
# 逐文件比对 SHA256（ASCII 清单，两列：hash  文件名）
Get-Content tools\archive_reference_audits\MANIFEST.sha256
```
