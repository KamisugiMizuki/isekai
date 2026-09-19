# isekai

isekai——一个持续运行的异世界。用户通过与世界内角色的对话（经由角色专属的双向联络方式），碎片化地发现这个世界的历史、人文与重要事件；世界不依赖用户在线而存在。

> 状态：设计完成（总纲 + 9 篇 SPEC）；**阶段 0 已完成**——核心进程（UMP v1 / 最小会话核心 / 管理面）、开发用 CLI 客户端与 Tauri 桌面壳（sidecar 监督 / 内建聊天 / 设置 / 托盘）均已落地并实机验证；世界包、角色卡、世界实例、导入导出与生成器已随**阶段 1**落地（见「世界设定层」一节）；运行时（时钟、时间线推进、事件、记忆）自阶段 2 起实现。
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
- **生成器**（两端共用）：表单式骨架 + 对话式 AI 候选，共用同一套 schema / 模板 / 校验 / 修订流程；候选只在校验通过后才写文件。世界包按语义分三段生成（整包一次产出会被模型长度上限截断）；模型输出带代码围栏、尾随逗号、整段截断都有兜底与一次带错误清单的重试。
- 桌面壳的「世界」页提供同一批操作（下拉选择世界包 / 角色卡 / 实例，AI 生成、校验、创建、导入导出、删除）。

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
| `isekai_core/world_cli.py` | 世界设定层 CLI（世界包 / 角色卡 / 实例的创建与导入导出） |
| `isekai_core/cli.py` | 开发用聊天客户端（先行验证协议链路；替代品，不是最终 UI） |
| `desktop/` | Tauri v2 壳：核心监督、托盘、内建聊天客户端（`src/ump.ts` + `src/main.ts`） |
| `tests/` | 行为级测试：真 WebSocket + 真 SQLite，仅替换 LLM |
