# isekai

isekai——一个持续运行的异世界。用户通过与世界内角色的对话（经由角色专属的双向联络方式），碎片化地发现这个世界的历史、人文与重要事件；世界不依赖用户在线而存在。

> 状态：设计完成（总纲 + 9 篇 SPEC）；**阶段 0 已完成**——核心进程（UMP v1 / 最小会话核心 / 管理面）、开发用 CLI 客户端与 Tauri 桌面壳（sidecar 监督 / 内建聊天 / 设置 / 托盘）均已落地并实机验证；世界包、角色卡、世界实例、导入导出与生成器已随**阶段 1**落地（见「世界设定层」一节）；**阶段 2（世界运行层）已落地**——世界时钟与倍率、激活 / 冻结、离线补算、性格单元、生活线与最小认知接口（见「世界运行层」一节）；事件引擎与记忆自阶段 3 起实现。
> 本项目独立于 veranima-companion；后者作为设计参考与资产来源。

## 文档

- 总设计（总纲，粗颗粒度）：[`docs/DESIGN.md`](docs/DESIGN.md)
- 模块设计（SPEC）：
  - [`docs/CHANNEL_PLUGIN_SPEC.md`](docs/CHANNEL_PLUGIN_SPEC.md) — 通道插件层
  - [`docs/DESKTOP_SPEC.md`](docs/DESKTOP_SPEC.md) — 桌面壳
  - [`docs/SESSION_CORE_SPEC.md`](docs/SESSION_CORE_SPEC.md) — 会话核心层
  - [`docs/MEMORY_SPEC.md`](docs/MEMORY_SPEC.md) — 角色记忆
  - [`docs/WORLD_RUNTIME_SPEC.md`](docs/WORLD_RUNTIME_SPEC.md) — 世界运行层
  - [`docs/EVENT_ENGINE_SPEC.md`](docs/EVENT_ENGINE_SPEC.md) — 世界事件引擎
  - [`docs/WORLD_SETTING_SPEC.md`](docs/WORLD_SETTING_SPEC.md) — 世界设定层
  - [`docs/CHARACTER_CARD_SPEC.md`](docs/CHARACTER_CARD_SPEC.md) — 角色卡
  - [`docs/ANDROID_SPEC.md`](docs/ANDROID_SPEC.md) — 安卓端
- 存档：[`docs/archive/`](docs/archive/)（总设计对话全文 / 迭代记录 / V0.1 早期条款）

## 运行（阶段 0）

```bash
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
cp config/config.example.yaml config/config.yaml                  # 填 llm.api_key
.venv/Scripts/python.exe -m isekai_core.cli                       # 交互聊天（自动拉起核心进程）
```

| 目的 | 命令 |
|---|---|
| 单轮对话 | `.venv/Scripts/python.exe -m isekai_core.cli --say "你好"` |
| 只跑核心 | `.venv/Scripts/python.exe -m isekai_core` |
| 跑测试 | `.venv/Scripts/python.exe -m pytest` |
| 不联网自测链路 | `ISEKAI_LLM_FAKE=1 .venv/Scripts/python.exe -m isekai_core.cli --say "你好"` |

- 核心启动时向 stdout 输出**一行 JSON 就绪握手**（端点 + 一次性引导凭据 + 管理凭据）；壳 / 客户端按字节（UTF-8）读这一行取得连接材料，不猜端口。
- 数据与日志：`data/isekai.db`（SQLite，WAL）、`data/clients/*.json`（通道持久凭据）、`logs/core.log`；均在 `.gitignore` 内。
- 配置项与环境变量见 [`config/README.md`](config/README.md)。

## 桌面壳（阶段 0）

```bash
cd desktop
npm install
npm run tauri:dev                     # 开发模式（vite 127.0.0.1:1420 + 壳 + 核心）

# 自包含运行（内嵌前端资源，不需要 dev 服务器）
npm run build
cd src-tauri && cargo build --features custom-protocol
./target/debug/isekai-desktop.exe
```

- 壳自己拉起核心（`--parent-pid` 看门狗）、读 stdout 就绪握手拿端点与凭据，再经 UMP 连接并绑会话；关闭窗口默认到托盘，托盘菜单「显示主窗口 / 重启核心 / 退出」。
- 日志分离：壳 `logs/shell.log`，核心 `logs/core.log`（轮转，2MB × 3）。
- 已验证行为：真实往返（输入 → UMP → 核心 → LLM → 渲染 + 投递回执）、关窗到托盘（核心继续跑）、硬杀壳不留孤儿核心、设置面读写（Key 打码）、**核心崩溃 → 界面提示 + 有界退避重连（1/2/4/8/16s）→ 重启核心按钮恢复**、握手回带已有绑定令牌（重连不必再问管理面）。
- 未做（后续阶段）：时间线与版本操作、世界时钟与补算、事件引擎、记忆子系统、桌面提醒、安装包（`npm run tauri build`）。

## 世界设定层（阶段 1）

创作目录默认 `<根目录>/packages/`：世界包、角色卡与导出件都是单个 JSON 文件（可直接手编、可 diff）。

```bash
# 世界包：骨架 → 校验 → （对话式）AI 生成
python -m isekai_core.world_cli package template --name 盐滩纪 --out packages/saltflat.json
python -m isekai_core.world_cli package validate --file packages/saltflat.json
python -m isekai_core.world_cli package generate --name 盐滩纪 --brief "潮汐退去后露出盐滩的沿岸世界…" --out packages/saltflat.json

# 角色卡：骨架 / AI 生成 → 确认（未确认不能进实例）
python -m isekai_core.world_cli card template --package packages/saltflat.json --name 堤禾 --out packages/tihe.json
python -m isekai_core.world_cli card confirm --package packages/saltflat.json --file packages/tihe.json

# 实例：创建（默认冻结）→ 列表 → 导出 → 导入（新实例）
python -m isekai_core.world_cli instance create --package packages/saltflat.json --card packages/tihe.json
python -m isekai_core.world_cli instance list
python -m isekai_core.world_cli instance export --id in-xxxxxx --out packages/saltflat.isekai.json
python -m isekai_core.world_cli instance import --file packages/saltflat.isekai.json
```

已实现并验证的行为（对照 `WORLD_SETTING_SPEC` §3 / §7、`CHARACTER_CARD_SPEC` §5 与附录 D）：

- **世界包校验**：结构、稳定标识唯一、引用闭包（说法 → 来源、史料 → 条目、谜题 → 挂靠）、历法自洽（时段覆盖整日且不重叠）、实情层与说法层分开（每条说法必须有来源与获知条件）、史料含贡献者与时段、事件族与事实效果、生活模板、角色模板与联络机制。空壳骨架会被挡在保存与创建之前。
- **实例创建**：装配 → 联合校验（卡片须已确认、渠道不悬空、初始知识有来源且获知不晚于初始时刻、史料不早于成书、种族与出生寿命相容、日程合法）→ 命名 → **一次性固化**（设定快照 + 世界种子 + 初始时间线 + 初始提交，默认冻结）；任一步失败不留半个实例。
- **设定与文件解耦**：实例持锁定快照，改世界包文件不追溯已有实例；新实例才用新内容。
- **命名**：显示名在创建时从**原始名称**复制一次（改包名不生效）；全局唯一，冲突自动追加 `_2`/`_3`（NFKC + 大小写折叠比较）；显式重命名冲突直接拒绝，不静默改名。
- **导入导出**：单文件 JSON（不加密），含清单 + 锁定设定 + 运行部分 + 完整性指纹；不含凭据 / 投递回执 / 通道绑定 / 作废记录；导入总是创建新实例、默认冻结，版本或能力不兼容即拒绝导入且不产生半个实例。
- **生成器**（两端共用）：表单式骨架 + 对话式 AI 候选，共用同一套 schema / 模板 / 校验 / 修订流程；候选只在校验通过后才写文件。世界包按语义分三段生成（整包一次产出会被模型长度上限截断）；模型输出带代码围栏、尾随逗号、整段截断都有兜底与一次带错误清单的重试；结构化产物用低温 + **形状参考**（示例世界包，同时是测试夹具真源）。
- 桌面壳的「世界」页提供同一批操作（下拉选择世界包 / 角色卡 / 实例，AI 生成、校验、创建、导入导出、删除）。

### 阶段 1 审计补齐（对照 WORLD_SETTING_SPEC §2.3 / §2.4 / §2.5 / §7.1 / §7.6 与附录 C/D）

- **加载限额**：节点数、嵌套深度、单条文本长度与集合长度超限即明确拒绝，不静默裁掉设定。
- **必需能力**：世界包 `meta.requires` 声明所需运行能力，本端不认识就在确认前报错。
- **草稿态**：未通过校验的候选可存为草稿（`world.draft.save/load/list/discard`）、显式继续或丢弃；草稿不冒充正式包、不进创作目录列表，也不另建生成历史库。
- **用量预算**：生成类操作带调用上限（世界包 6 次 / 卡片 2 次，含重试），达到上限即暂停并保留已完成的段落，返回 `usage{calls,limit,paused}`；桌面壳在发送前显示目标模型与预计调用次数供用户确认。
- **导出原子性**：先写临时文件、校验完整性后再原子发布，中途失败不留半截产物；容器携带**时间线与提交闭包**，导入时重新映射本地标识并一律冻结。
- **打开实例的兼容检查**：`compatible` / `convertible` / `blocked` 三态随实例元数据返回（检查本身不推进世界、不改状态）；运行层按此决定推进或阻断（阶段 2 接入）。
- **声明式字段补全**：制度（职权 / 适用范围 / 延续与承接）、惯例（适用群体 / 做法 / 依据 / 允许变化范围）、环境事实（单位 / 取值域 / 观察条件 / 失效方式）——声明了就必须能被一致解释，未声明即不适用。
- **引用闭合加强**：事件效果的 `target` 必须指向已登记标识；角色初始知识引用史料时必须写明所掌握条目（`scope`）且不得越出该传本范围。

## 世界运行层（阶段 2）

对照 `docs/WORLD_RUNTIME_SPEC.md` §2 / §3 / §10 / §11 / §13 与 `docs/CHARACTER_CARD_SPEC.md` §6。

- **世界时钟**：世界秒是唯一时间基元，日期 / 月份 / 时段只是历法视图（纯函数换算，无闰年闰月）。目标时刻 = 基准世界时刻 +（现实用时）× 当前倍率；倍率变更按**分段累计**结算，不做「最新倍率 × 整个离线区间」。
- **倍率**：每条时间线独立，默认 1；正整数，受全局上限约束（仅开发者可配）。请求在**严格晚于输入时刻的第一个自然整秒**生效，同一生效点以最后请求为准；重试同一请求不产生第二次变更；冻结线不接受倍率调整。
- **激活与冻结**：实例创建 / 导入后一律冻结；激活即用当下现实时间重锚（冻结期间不补算），冻结先结算已生效段并取消未生效请求。核心启动时只对中断前激活的线补算。
- **离线补算**：水位按世界日分批推进，每批原子提交；重复执行不重复产生经历（幂等），推进落后于目标时如实报告「追赶中」。核心每 5 秒推进一次激活线（冻结线跳过，单线失败不影响其他线）。
- **性格单元**：四驱动（锚点 / 事件 / 对话 / 时间）各有生成区间，区间只约束**初始**置信度；后续更新限制在 [0,1]，允许低于区间并归档；非锚点低于阈值即归档（保留依据），锚点受保护下限约束；后来的弱驱动不能把已有高置信单元直接裁到自己的上限；单元由来源键去重（同一来源只消费一次），驱动迁移隐式发生、不产生可见事件。
- **生活线**：从角色卡模板展开「她在做什么」的时间窗 + 活动标识（无坐标、无路网、无通行模拟，跨日睡眠窗自动拆分）；每角色每世界日一份计划、写入即固化、重启不重抽；**计划不等于经历**——只有已过去的时间窗才产生经历。
- **认知接口**：按（实例、时间线、角色、水位）返回该角色已可接触的信息子集，每条带来源、获知时间与主观确信度；实情层与幕后设定不进入扮演上下文；硬约束角色只接受自身经历 / 小环境 / 用户通讯来源。会话层据此构造扮演定义：真实实例走运行层，占位会话仍用占位提示词。
- **CLI**：`runtime clock / activate / freeze / rate / advance`（`world_cli.py`）；聊天客户端可用 `--instance / --timeline / --character / --activate / --rate` 直接绑定真实实例。

### 阶段 2 审计补齐（对照 WORLD_RUNTIME_SPEC §2.2–2.8 / §3 / §4 / §11 / §13 与附录 A/B）

- **倍率上限可配置**（§2.2）：`runtime.rate_max` 默认 2592000，全局统一、仅开发者可配置，不进设置 UI；`runtime.max_active_timelines`（同时激活上限）、`catch_up_batches`、`catch_up_lag_seconds` 同为开发者配置。
- **上限降低不改写历史**（§2.4）：该线保留原倍率、保持冻结，激活必须确认一个合法倍率（CLI `--rate` / 壳上先填倍率再点激活）；不静默改写、不静默丢历史。
- **运行世代**（§2.2 / §4）：冻结与激活都递增世代，迟到批次带旧世代一律整批不落盘；重新激活不接收旧世代结果。
- **一批一提交**（§2.7）：计划、单元衰减、经历与水位的推进在**同一事务**内提交，中途失败整批回到批前水位，不留半批数据。
- **追赶受限**（§2.6）：目标持续领先时持久化 `catching_up` / `limited` 并如实上报；追平后自动清除；处理水位超过合法目标另记为一致性错误，不静默回退。
- **导出 / 导入带运行状态**（§2.6）：容器新增 `runtime.state`，按**已完成水位**导出角色单元、生活计划、经历与补卡记录；导入在建线时钟上停在该水位、保持冻结、不恢复待生效倍率命令、不补算备份日至今。
- **补卡**（§九 / 附录 B #18）：`runtime card-add`——角色按世界时刻锚定补入，个人史与初始知识按同一认知契约投影（先过设定层校验），补入本身不激活该线，冻结线补入锚定冻结时刻；可选 `--acquainted` 只补一条对话单元，不改任何既有角色状态。
- **四驱动的真实入口**（§10）：新增 `drive_unit`——对话 / 事件驱动按来源键幂等落单元，同源重试返回 None；驱动水位不得超过已完成水位；数值与迁移依旧不对用户可见。
- **查询主题**（§13.1）：认知接口接收主题并按命中排序，**只排序不过滤**——过滤的对象始终是可接触性，角色不因提问角度失忆。
- **生活线边界形态**（§11 / 附录 B #7）：夜班、跨日睡眠与无睡眠角色共用同一世界日界；跨日窗口按世界秒展开到次日日首；未来窗口不产生经历。
- 桌面壳「世界」页底部为运行面：当前查看实例的时钟（只显示公开时刻与倍率，追赶中如实标注）、激活 / 冻结 / 设定倍率，每 2 秒自动跟一次。

## 代码结构

| 路径 | 内容 |
|---|---|
| `isekai_core/ump.py` | 统一消息协议 v1：信封校验与构造、错误码 |
| `isekai_core/store.py` | SQLite 持久化：通道实例 / 会话 / thread 绑定 / 消息 / 投递状态 / 作废记录 |
| `isekai_core/session.py` | 会话核心：接受与去重、生成链路、固化、分批投递、重试 |
| `isekai_core/channel.py` | 通道宿主：回环 WebSocket、握手与认证、受信管理面（会话 / 绑定 / 历史 / 设置 / 状态） |
| `isekai_core/client.py` | UMP 客户端与管理面客户端（CLI 与桌面壳共用语义） |
| `isekai_core/llm.py` | LLM 调用（OpenAI 兼容；`ISEKAI_LLM_FAKE=1` 为无网络替身） |
| `isekai_core/world/` | 世界设定层：世界包结构与校验、角色卡、实例创建与命名、导入导出、生成器、管理面操作 |
| `isekai_core/runtime/` | 世界运行层：历法换算、时钟与倍率、时间线服务与水位推进、性格单元、生活线、认知接口、补卡与性格驱动入口 |
| `isekai_core/world_cli.py` | 世界设定层 CLI（世界包 / 角色卡 / 实例 / 运行时钟的创建、导入导出与推进） |
| `isekai_core/cli.py` | 开发用聊天客户端（先行验证协议链路；替代品，不是最终 UI） |
| `desktop/` | Tauri v2 壳：核心监督、托盘、内建聊天客户端（`src/ump.ts` + `src/main.ts`） |
| `tests/` | 行为级测试：真 WebSocket + 真 SQLite，仅替换 LLM |
