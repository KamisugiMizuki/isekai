# isekai

isekai——一个持续运行的异世界。用户通过与世界内角色的对话（经由角色专属的双向联络方式），碎片化地发现这个世界的历史、人文与重要事件；世界不依赖用户在线而存在。

> 状态：设计完成（总纲 + 9 篇 SPEC）；实现处于**阶段 0**——核心进程（UMP v1 / 最小会话核心 / 管理面）与开发用 CLI 客户端已落地并实测，Tauri 桌面壳与内建聊天 UI 尚未开始。
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

## 代码结构

| 路径 | 内容 |
|---|---|
| `isekai_core/ump.py` | 统一消息协议 v1：信封校验与构造、错误码 |
| `isekai_core/store.py` | SQLite 持久化：通道实例 / 会话 / thread 绑定 / 消息 / 投递状态 / 作废记录 |
| `isekai_core/session.py` | 会话核心：接受与去重、生成链路、固化、分批投递、重试 |
| `isekai_core/channel.py` | 通道宿主：回环 WebSocket、握手与认证、受信管理面 |
| `isekai_core/client.py` | UMP 客户端与管理面客户端（CLI 与桌面壳共用语义） |
| `isekai_core/llm.py` | LLM 调用（OpenAI 兼容；`ISEKAI_LLM_FAKE=1` 为无网络替身） |
| `isekai_core/cli.py` | 开发用聊天客户端（先行验证协议链路；替代品，不是最终 UI） |
| `tests/` | 行为级测试：真 WebSocket + 真 SQLite，仅替换 LLM |
