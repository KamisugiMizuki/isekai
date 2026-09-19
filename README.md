# isekai

isekai——一个持续运行的异世界。用户通过与世界内角色的对话（经由角色专属的双向联络方式），碎片化地发现这个世界的历史、人文与重要事件；世界不依赖用户在线而存在。

> 状态：设计完成（总纲 + 9 篇 SPEC）；**阶段 0 已完成**——核心进程（UMP v1 / 最小会话核心 / 管理面）、开发用 CLI 客户端与 Tauri 桌面壳（sidecar 监督 / 内建聊天 / 设置 / 托盘）均已落地并实机验证；世界包、实例与角色卡自阶段 1 起实现。
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
- 未做（阶段 1 起）：世界包 / 实例 / 角色卡创作、时间线与版本操作、桌面提醒、内建聊天停用开关、安装包（`npm run tauri build`）。

## 代码结构

| 路径 | 内容 |
|---|---|
| `isekai_core/ump.py` | 统一消息协议 v1：信封校验与构造、错误码 |
| `isekai_core/store.py` | SQLite 持久化：通道实例 / 会话 / thread 绑定 / 消息 / 投递状态 / 作废记录 |
| `isekai_core/session.py` | 会话核心：接受与去重、生成链路、固化、分批投递、重试 |
| `isekai_core/channel.py` | 通道宿主：回环 WebSocket、握手与认证、受信管理面（会话 / 绑定 / 历史 / 设置 / 状态） |
| `isekai_core/client.py` | UMP 客户端与管理面客户端（CLI 与桌面壳共用语义） |
| `isekai_core/llm.py` | LLM 调用（OpenAI 兼容；`ISEKAI_LLM_FAKE=1` 为无网络替身） |
| `isekai_core/cli.py` | 开发用聊天客户端（先行验证协议链路；替代品，不是最终 UI） |
| `desktop/` | Tauri v2 壳：核心监督、托盘、内建聊天客户端（`src/ump.ts` + `src/main.ts`） |
| `tests/` | 行为级测试：真 WebSocket + 真 SQLite，仅替换 LLM |
