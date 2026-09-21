---
target: desktop/index.html
total: 24
max_total: 40
p0_count: 1
p1_count: 1
p2_count: 2
p3_count: 0
timestamp: 2026-09-21T08-46-32Z
slug: desktop-index-html
---
# isekai 桌面壳 · critique（复审 · 整改后）

Method: dual-agent (A: sa-0-2fe1ff94 · B: sa-1-51147b1b) — 同一目标 slug，对照首跑

## Design Health Score

| # | Heuristic | 首跑 | 复审 | Key Issue（本轮） |
|---|-----------|:--:|:--:|---|
| 1 | Visibility of System Status | 2 | 3 | 组内槽 + 生成进度行 + 顶栏时钟到位；进度行/状态 chip 仍无 role=status；管理面掉线只剩「时钟消失」 |
| 2 | Match System / Real World | 2 | 2 | 未动：阶段标签、不连 UMP、协议版本、时刻无单位 |
| 3 | User Control and Freedom | 1 | 2 | 删除有真闸门、死重试消失、不可取消改为明说；仍：闸门状态吞输入框文字、无撤销/回滚入口 |
| 4 | Consistency and Standards | 2 | 3 | 25 按钮统一外观、.primary、h3 统一；仍：页名不一致、3 个 select 同清单、8 处原生 confirm |
| 5 | Error Prevention | 1 | 2 | 付费守卫 + 确认框写全范围/上限/时长、删除要键入名；仍：未确认卡照收、两组生成闸门互不互斥、重命名无确认 |
| 6 | Recognition rather than Recall | 1 | 2 | 空态给下一步、顶栏常显时钟；仍：按钮自述输入来源、实例下拉主键仍是文件名+内部号 |
| 7 | Flexibility and Efficiency | 1 | 1 | 未动：零快捷键/零过滤/零批量 |
| 8 | Aesthetic and Minimalist | 3 | 3 | 旧账还清（UA 按钮 0、h3 层级、暗色错误可读、空槽不占位、版本表折叠）；新账：设置面 +3 表单密度搬家、字号阶梯仍平 |
| 9 | Error Recovery | 2 | 3 | 错误色 6.64/7.09:1、错误回落到动作组、死重试消失；仍：删除失败答在别行、生成无中途心跳 |
| 10 | Help and Documentation | 2 | 3 | 每组自带说明、配置目录可用、空态给下一步；仍：无首跑引导、概念解释散在散文里 |

**Total 24 / 40（Acceptable；首跑 17/40 Poor）**

## 逐条对照（首跑 ①–⑦）

- ① 反馈在屏外 — **已修复大面**（worldAction 就近落槽、`.note:empty` 不占位）；4 处影子残留（补卡答上一行、创建/导入实例答上一行、删除失败答别行）
- ② 付费生成裸奔 — **已修复**（在途守卫 + 进度行 + try/finally + 确认框写明不可取消）；残留：无「已用 n/上限 N」实时计数、两组生成互不互斥、上限写死在壳里
- ③ 删除无闸门 — **已修复**（独立虚线行相隔 186px、键入名才 enable、保留/删除清单）；残留：比对显示名而非 id
- ④ 视觉断裂 — **已修复**（实测 Arial 回退 29→3 且全是 checkbox 字形；h3 全 13px/600；--bad 6.64/7.09:1；focus ring 18.88/16.73:1）；残留：字号阶梯仍平
- ⑤ 首跑矛盾 — **部分**（空态与 chip 同口径 + 版本表折叠 + 去重）；**差的一半：第一次 AI 生成仍是一堵没有门的墙**
- ⑥ 时钟只在管理页 — **已修复**（2s 轮询无条件跟顶栏）
- ⑦ 语义召回死胡同 — **部分**（配置目录 + 记忆组可编辑落地）；chip 本身仍不可点、仍无 role=status、默认安装仍一开窗就被判异常

## 客观检测 delta

- 文件级：0 findings（= 基线，无 delta；根因同前：CSS 由 TS import）
- 渲染态：**2 → 1**。`overused-font: arial` 消失（arial 命中 0/93）；`flat-type-hierarchy` 仍在（11/12/13/14/16，ratio 1.5）——13.3px 档消失，规则触发是设计真实属性，非误报
- **因果反验证**：页内删掉唯一一条 `button{font:inherit}` → 同 build 立刻复现基线 2 条 → 消失源自修复，不是检测面变了
- 抽查：按钮 font-family 首选 Segoe UI（残留 3 个 checkbox UA 字形，无文本）；--bad 6.64:1 / 7.09:1；focus ring 两侧皆为页面底色 18.88:1 / 16.73:1

## 新引入的问题

1. 角色卡组两个槽两个主人（#card-import-note 闸门 vs #card-note 结果）
2. 三种写槽惯用法 + deleteResult 缓存对抗 loadWorld 覆盖（为共享槽打的补丁）
3. `.row.hidden` 治症不治根（建议 `.hidden{display:none!important}` 或挪文件末尾）
4. 新轮询的静默降级（时钟消失是唯一症状）
5. 密度搬家：管理页的自由度问题现在也适用于设置面
6. 本批新增 S8/S9 仍是源码文本存在性断言（上轮定性的失效模式），好在 dist 与 src 同代

## 剩余优先问题

- [P0] 默认安装第一次点「AI 生成」是没有门的墙 — `settings.get` 已返回 `llm.api_key_set`，但确认框只念模型与地址、设置面 5 行事实无 Key 状态；无 Key 时用户付出时间拿回原始核心错误。**改**：api_key_set=false 时不弹确认框，直接给「去设置面填 Key」+ 跳转聚焦；事实行加「API Key：已配置/未配置」。**命令**：onboard
- [P1] 破坏性能力在界面、恢复能力只在核心 — 文案写「撤回只能靠回滚」，而壳对 runtime.rollback/commits/commit/fork 调用数为 0。**改**：运行组加最小回滚入口（commits→选中→确认→rollback），或先删掉那句文案。**命令**：harden
- [P2] 组内槽与动作不对位（4 处影子）— 一行一槽，槽只服务本行，删除失败走 `#inst-delete-note`。**命令**：polish
- [P2] 付费预算写死在壳里且无实时用量 — 确认框前拉一次 `runtime.budget`，上限从核心读。**命令**：harden

## 做得好

1. 生成闸门是真闭环（try/finally 同时收计时器与禁用态；守卫拦在确认框前）
2. 删除闸门有真牙齿且解释后果（相隔 186px、键入名、随删/保留分组列出）
3. CSS 是系统性规则而非逐元素补丁（一条规则覆盖全部实例，Arial 29→3、对比度 2.87→6.64/7.09、焦点 1.43→18.88）

## Run Notes

- 首跑快照 `.impeccable/critique/2026-09-21T07-33-09Z__desktop-index-html.md`；本轮双路隔离，未降级
- 两路子代理均只读：仓库 HEAD 仍 `4ba437c`，git status 空；临时服务 :8899/:8900 已 kill，临时文件已删（B 路）；A 路用 file:// + 内联 CSS 无服务
- CLI URL 渲染模式不可用（需 puppeteer）→ 渲染态手动注入（与基线同路）；`scripts/detector/detect.mjs` 实为 `scripts/detect.mjs`
