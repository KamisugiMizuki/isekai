---
target: desktop/index.html
total: 27
max_total: 40
p0_count: 0
p1_count: 2
p2_count: 2
p3_count: 2
timestamp: 2026-09-21T11-48-20Z
slug: desktop-index-html
---
# isekai 桌面壳 · critique（第三轮 · 复审后收尾批之后）

Method: dual-agent (A: sa-0-690c2226 设计复审 · B: sa-1-942ef2e0 客观检测)

## Design Health Score

| # | Heuristic | R1 | R2 | R3 | Key Issue（本轮） |
|---|-----------|:--:|:--:|:--:|---|
| 1 | Visibility of Status | 2 | 3 | 3 | role=status 铺到了，但进度每秒重写进 atomic live region（读屏重复播报）；管理面掉线仍只有「时钟消失」 |
| 2 | Match Real World | 2 | 2 | 2 | 阶段标签/协议版本/时刻无单位照旧；新增预算句把「次」与「token」并排 |
| 3 | User Control & Freedom | 1 | 2 | 2 | Esc 清空筛选是真出口；仍无撤销（改名/设置/披露） |
| 4 | Consistency | 2 | 3 | 3 | 三档字号 + 删 .chip.small + id 尾段口径对齐回滚点；仍 9 处原生 confirm、.hidden 双规则并存 |
| 5 | Error Prevention | 1 | 2 | 3 | 最贵/最破坏两条路径三重防护（Key 门 + 互斥 + 真实预算）；仍：改名无确认 |
| 6 | Recognition | 1 | 2 | 3 | 筛选框自述快捷键、行内槽自述阻塞、chip 从「故障」改述为「默认」 |
| 7 | Flexibility & Efficiency | 1 | 1 | 2 | 快捷键 + 键入即筛可用；仍零批量、无列表键盘导航 |
| 8 | Aesthetic & Minimalist | 3 | 3 | 3 | 字号 5→3 档是真减法；顶栏常驻 238px 默认态句子 |
| 9 | Error Recovery | 2 | 3 | 3 | 互斥被拦说明哪组在跑；错误仍是会消失的文本，无重试/日志入口 |
| 10 | Help & Documentation | 2 | 3 | 3 | 占位符带快捷键、chip 带 title；仍无首跑引导 |

**Total 17 → 24 → 27 / 40（Acceptable 顶格，28 进 Good）**

## 逐条对照（上轮遗留 ①–⑥ + line-length）

- ① 预算：**部分** — budgetLine() 真从 runtime.budget 读核心三层限额 + 两组互斥；但 6/2 次调用上限仍是壳常量镜像（核心无该字段 = 天花板），且新预算句把「次」与「token」并排
- ② chip：**功能已修复 + 新引入 ARIA 缺陷** — 变 button 且可点跳转聚焦（实测 activeElement=set-mem-mode），默认安装不再被判异常；但 AX 树 role=status（盖住了 button），读屏不认它是控件
- ③ 字号：**已修复** — computed {12,14,24}，检测器 flat-type-hierarchy 归零；24/12 正好 2.0 零余量，已写进 CSS 规约
- ④ 效率：**已修复到最小集** — Ctrl+1/2/3 切 pane（defaultPrevented=true）、Ctrl+K 聚焦、键入即筛（实测 1/2、2/2 命中）、Esc 复原；仍零批量
- ⑤ 删除闸门：**部分（属取舍）** — 判据仍比显示名，但 id 尾段双处出现（提示 + 确认框）消掉同名歧义；作者写明不改成输内部 id
- ⑥ .row.hidden：**已修复（治症 + 根因入注释）** — 0,1,0 同特异度后写者胜，规则挪到文件末尾；根因仍存
- line-length：**未修复（1 条潜伏）** — 默认态被隐藏面板挡掉；强制显示后同一条命中 ~139 chars/line（settings 面 p.muted），根因 = 散文零 measure

## 客观检测 delta

| 检测项 | R1 | R2 | R3 |
|---|---|---|---|
| 文件级 | 0 | 0 | 0 |
| 渲染态（light） | 2 | 1 | **0** |
| 渲染态（dark） | 未测 | 未测 | **0** |
| flat-type-hierarchy | 命中 | 命中 | **归零**（sizes 12/14/24, ratio 2.0，零余量） |
| overused-font | arial 38% | 0 | **0**（96/96 文本元素 = Microsoft YaHei） |

**结构性盲区（本轮新证）**：文件级检测器**不读外部样式表** —— 用两个对照页证伪「看不见是误报」：内联 slop 报 3 条，同款 slop 走 `<link>` 引外部 CSS 报 []。所以 `dist/index.html` 永远报 0，CLI 干净 ≠ 渲染后干净。

## 新引入的问题（本批的账）

1. [P1] `role="status"` 压在 `<button>` 上：AX role=status、focusable=true → 读屏不认它是按钮
2. [P1] 每秒进度写进 atomic live region：一次 10 分钟生成可能重复播报数百次
3. [P2] 预算行单位混排（「今日已用 N 次 / 上限 M token」）；used 是当日全部任务 calls 之和
4. [P3] 顶栏常驻 238px chip 只播报默认态；Ctrl+1/2/3 无处可发现

## 已确认的天花板/取舍（不是缺口）

生成不可取消（核心无 cancel op，确认框已明说）；GENERATE_LIMIT 次数常量（核心无该字段）；删除闸门比显示名（刻意）；快捷键不显式提示（Nielsen 7 允许）

## Run Notes

- 双路隔离；两路子代理均只读，仓库 HEAD 仍 1463202、git status 空
- B 路用一次性 node 静态服务 + `window.__TAURI_INTERNALS__` 桩让壳真跑起来（nav/快捷键/筛选/chip 点击均按真实 handler 实测）
- 清理：B 路 8712/8907/8911 已 kill；遗留 :8899（PID 52844/7092，前一轮证据子代理留的）父代理本回合清掉
