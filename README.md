# isekai

> **一个持续运行的异世界。** 世界不依赖你在线而存在：它按自己的时钟推进，
> 你不在时它照常运转。你通过与世界内角色的双向联络方式与他们持续对话，
> 碎片化地拼出这个世界的历史、人文与重要事件——每次开口都是「探访」一个一直在走的世界，
> 而不是「启动」一段对话。

`0.1.0`（应用版本） · Windows 10 22H2 / 11 x64 · 开发需 Python 3.11 · [MIT](LICENSE)

本项目独立于 veranima-companion；后者只作为设计参考与资产来源，不是运行依赖。

---

## 你是哪一类读者

| 你想做的事 | 从这里开始 |
|---|---|
| **只想用它**（普通用户，不碰命令行） | 下面的[五步开始](#普通用户五步开始)：拿到发行件 → 解压 → 双击 `isekai.exe` |
| **想跑起来 / 改代码**（开发者） | [QUICKSTART：十分钟跑通一个世界](docs/QUICKSTART.md)（不需要 AI 密钥） |
| **想知道现在到底实现到哪** | 下面的[现在实现到哪](#现在实现到哪) |
| **想读懂设计与协议** | [设计文档地图](docs/README.md) |
| **想参与开发**（环境、测试、目录、探针） | [开发指南](docs/DEVELOPING.md) |

## 它要解决的问题

现有角色陪伴类系统里，**角色层**基本可用：日程、地点、记忆、人格都有。缺的是
**角色所生活的那个世界**——历史是什么、发生过什么、社会如何运转。把世界写在提示词里，
它就是静态、被动、不可探索的。isekai 要做一个**独立运行的世界模型**，角色生活其中，
用户经由角色进入其中。

核心设计原则（完整版见[总纲](docs/worldruntime/DESIGN.md) §二）：

- **世界为主体，角色为表达面**：世界是独立运行的数据面；角色说的每句话由其处境、经历与认知决定。
- **用户在世界外**：你是世界的观测者，不是世界成员；通过角色特有的双向联络方式说话。
- **世界不依赖用户在线**：世界时钟独立推进，重开时按确定性规则追赶（离线补算）。
- **碎片化是结构保证**：角色只知道她获知过的那一版（「说法 / 实情」分离）；
  同一世界的不同角色可以知道不同的事，用户靠换人、换时间、换话题拼图。
- **世界新设定只从有来源的地方长出来**：不接受文本生成器凭空增加世界公理、实体或幕后真相。

## 现在实现到哪

状态以本表为准；每一行的实现细节与验收证据见表下引用块指向的文档。

| 阶段 | 状态 | 一句话 |
|---|---|---|
| 阶段 0：核心进程与外壳 | **已落地** | UMP v1 协议、会话核心、通道与管理面、开发用 CLI、Tauri 桌面壳（sidecar 监督、内建聊天、托盘） |
| 阶段 1：世界设定层 | **已落地** | 世界包与角色卡的结构 / 校验 / 生成器、实例创建与一次性固化、导入导出、命名 |
| 阶段 2：世界运行层 | **已落地** | 世界时钟与倍率、激活 / 冻结、离线补算、性格单元、生活线、认知接口 |
| 阶段 3：事件引擎 | **已落地** | 四族事件模板、确定性候选与每日预算、事实效果与失效方式、说法与获知链、角色经历、历史回填、角色自主提案、环境事实 |
| 阶段 4：版本管理 | **已落地** | 提交 / 分叉 / 回滚 / 自动提交 / 用户引入事件 |
| 阶段 5：多角色披露 | **已落地** | 默认隔离、显式授权、转述不冒充亲历、撤回只有回滚 |
| 阶段 6：制度与惯例 | **已落地** | 制度 / 惯例状态只沿已有依据变化，角色只看到自己获知的那一版 |
| 应用层：OC 故事层 | **核心语义已落地** | 输入分类闸、产品状态翻译、表达契约、用户可见面投影、版本与创作操作编排 |
| 应用层：Writing Assistant | **核心语义已落地** | 大纲约束与条目状态机、只读观察、候选生命周期、GM 直接变化、分支试演 |
| 应用层：TRPG 客户端与规则层 | **已落地** | 战役 / 场景运行时、规则插件协议、规则共用模块、规则登记簿与联合提交；客户端层与 U4 界面已接上 |
| U1 启动与联络 | **已实施** | 首次设置向导、首页、角色联络、世界与素材入口、设置与「帮助与诊断」 |
| U2 时间线 / 创建向导 / 迁移 | **已实施** | 时间线管理、尝试世界变化、单文件全量备份、六步创建向导（世界设定 + 角色卡）、开发目录迁移 |
| U3 辅助写作 | **已实施** | 一个工作区四个分区：大纲 / 当前素材 / 推进建议 / 文字草稿 |
| U4 跑团工作区 | **已实施** | 规则插件登记簿、战役与场景、玩家面 / 主持面与行动裁定 |
| U5 发行件与总体验收 | **部分实施** | 自带解释器、数据根、组装脚本、五步说明已做；**安装程序、真人试用与无障碍人工走查未做** |

> 判定口径：「已落地 / 已实施」= 有对应实现文件、有行为级测试或审计脚本、能实跑。
> 逐条证据见[用户界面定案 · 实施进度](docs/user-interface/README.md#实施进度)、
> [设计总纲](docs/worldruntime/DESIGN.md)与[开发指南](docs/DEVELOPING.md)的各阶段小节。

## 普通用户：五步开始

不用命令行、不用写 JSON。拿到发行件 `isekai-0.1.0-win64.zip`
（由 `python scripts/build_release.py` 组装）之后：

1. **解压**到任意一个有写权限的目录（例如 `D:\isekai`），不要放在需要管理员权限的位置。
2. **双击 `isekai.exe`**：程序自带运行环境，不需要另外装 Python。
   界面依赖 Windows 的 **WebView2 运行时**：Win11 与多数已更新的 Win10 已预装；
   若双击**没有反应**，装一次 [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
   （Evergreen Standalone Installer → x64）再双击。
   程序未购买数字签名，Windows 可能先弹「已保护你的电脑」——点「更多信息 → 仍要运行」。
3. **首次设置**（第一次打开直接进）：本机检查 → 连接 AI → 选择第一件事 → 准备材料 → 开始使用。
   用 AI 需要你自己有一个 AI 服务账号（要注册，多数要充值或领额度）；界面里推荐 DeepSeek，
   **地址与模型名已填好**，点「打开密钥申请页」按步骤创建密钥、把整串粘回来即可。
   不想现在弄就点「稍后配置，先整理素材」——**没有 AI 也能先建世界、起草角色**，
   密钥只存在你这台机器上，随时能在设置里改。
4. **准备材料**：在「世界与素材」里从**样例世界「灰潮纪」**开始（推荐），
   或者自己新建一份世界设定、起草角色卡。
5. **开始用**：想认识角色去「角色联络」，想整理故事走向去「辅助写作」，想跑一局去「跑团」。
   **关窗口 ≠ 退出程序**：点 × 只是收进托盘、世界继续走；要真正退出，
   右键任务栏的 isekai 图标 →「退出」。卸载 = 托盘退出后删除程序目录，
   数据在 `%LOCALAPPDATA%\isekai`，要一起清掉再删那个目录。

出问题时界面会说清四件事：**操作对象、直接原因、已完成范围、下一步**；
界面里的「帮助与诊断」有常见问题（密钥去哪申请、填哪个模型名、回复为什么慢、
怎么彻底退出、怎么备份）、本机检查、日志位置与高阶调试。
本次针对普通用户的可用性评审见
[普通用户视角可用性评审](docs/user-interface/PLAIN_USER_USABILITY_REVIEW_2026-10-07.md)，
配套还有[易读性与反模式审计](docs/user-interface/UI_READABILITY_AND_ANTIPATTERN_AUDIT_2026-10-08.md)
与[视觉体系审查](docs/user-interface/UI_VISUAL_SYSTEM_REVIEW_2026-10-08.md)。

> 目前是 zip + 手动解压：壳里已配 NSIS 打包目标，但发行流程（`scripts/build_release.py`）
> 只出 zip，安装程序尚未接入、也没做过安装验收；面向普通用户的三条完整路径的真人试用记录、
> 无障碍人工走查也还没做。未开放的界面入口在程序里如实标注「正在实现中」，不摆可点击的空壳。

## 开发者

环境、命令、目录与各阶段细节在[开发指南](docs/DEVELOPING.md)；下面是最短路径。

```powershell
# 1. 环境（Python 3.11；也可用 python -m venv 代替 uv）
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"

# 2. 不联网验证链路：核心起得来、角色回得出话
$env:ISEKAI_LLM_FAKE=1
.venv/Scripts/python.exe -m isekai_core.cli --say "你好"

# 3. 全量测试（当前基线 605 项全过，2026-10-08 实测）
.venv/Scripts/python.exe -m pytest -q

# 4. 桌面壳（Tauri v2；另需 Node.js 与 Rust 工具链）
cd desktop; npm install; npm run tauri:dev

# 5. 组装发行件 → release/isekai-0.1.0-win64.zip
python scripts/build_release.py
```

- 十分钟跑通一个世界（含样例世界「灰潮纪」的包校验 → 角色卡确认 → 建实例 → 激活 → 对话）：
  [QUICKSTART](docs/QUICKSTART.md)。样例题材在 [`examples/sample_world/`](examples/sample_world/README.md)。
- 中文 Windows（cp936）**不需要**额外设 `PYTHONUTF8`：核心 / 插件 / 规则插件的子进程由代码显式钉住 UTF-8；
  你自己写的脚本起子进程时仍建议设一次。踩坑清单见 QUICKSTART 文末「常见卡点」。
- 测试基线以现场跑出来的为准，历史数字（543 / 564 / 570 / 587）只是当时快照。
  要准确条数就用 `--junit-xml` 出报告。

## 架构一览

```text
┌ 桌面壳 desktop/（Tauri v2 + TypeScript，唯一正式用户入口）
│   首页 · 角色联络 · 辅助写作 · 跑团 · 世界与素材 · 设置 / 帮助与诊断
└── UMP v1（回环 WebSocket / 外部插件 stdio）──┐
                                              ▼
        应用层   story/（OC 故事层）· writing/（Writing Assistant）
                 trpg_client/ + runtime/{trpg,campaign,rules,rule_common}.py（跑团）
                                              │ 只交换有界 JSON、校验世界效果
        世界层   runtime/（时钟与倍率 · 激活冻结 · 离线补算 · 认知 · 事件引擎 · 记忆 · 版本）
                 world/（世界包 · 角色卡 · 实例 · 导入导出 · 生成器）
        内核     ump.py · session.py · channel.py · store.py · client.py
```

| 位置 | 内容 |
|---|---|
| `isekai_core/` | 内核 + 三个应用层的核心语义（下分 `world/` `runtime/` `story/` `writing/` `trpg_client/`） |
| `desktop/` | Tauri v2 桌面壳：核心进程监督、托盘、普通用户界面（`src/user/`） |
| `examples/` | 样例世界「灰潮纪」、潮汐骰池规则插件与通道插件的参考实现 |
| `tests/` | 行为级测试：真 WebSocket + 真 SQLite，只替换 LLM |
| `scripts/` | 发行组装（`build_release.py`）+ 逐条对 SPEC 的审计与真壳 CDP 探针（`_audit*` / `_probe_*`） |
| `docs/` | 设计与协议文档（入口见[设计文档地图](docs/README.md)） |

## 术语表

第一次出现的缩写都在这里解释一次（各 SPEC 里首次出现处另有指路）：

| 术语 | 含义 |
|---|---|
| **UMP** | 统一消息协议 v1：客户端 / 插件与核心之间的信封格式、错误码与能力协商（[规范](docs/worldruntime/CHANNEL_PLUGIN_SPEC.md)） |
| **通道 / 插件** | 与核心对话的进程；内建通道走回环 WebSocket，外部插件走 stdio 的同一套协议 |
| **实例 / 时间线** | 一份锁定设定加一个运行中的世界叫实例；同一实例可以有多条时间线（分支） |
| **水位** | 某条时间线上「世界已经推进到哪」的记录点；补算就是按水位一批批往前推 |
| **世代** | 每次冻结 / 激活递增的运行世代号；迟到任务带旧世代一律整批不落盘 |
| **说法 / 实情** | 世界真值与「谁听说了什么版本」分开；角色只知道她获知过的那一版 |
| **OC 故事层** | 面向「持续联络一个生活中的角色」的应用层，不拥有世界事实 |
| **WA（Writing Assistant）** | 面向作者的叙事约束编排层：大纲、候选、草稿；大纲不构成写世界的授权 |
| **TRPG 规则插件** | 拥有规则与骰子的外部进程；核心只交换有界 JSON 并校验世界效果 |
| **驱动 / 性格单元** | 角色性格的可解释单元，由锚点 / 事件 / 对话 / 时间四类驱动更新 |

## 文档

- 设计文档地图（其余规范总入口）：[`docs/README.md`](docs/README.md)
- 十分钟跑通一个世界：[`docs/QUICKSTART.md`](docs/QUICKSTART.md)
- 开发指南（环境、命令、测试、目录、探针、各阶段细节）：[`docs/DEVELOPING.md`](docs/DEVELOPING.md)
- 普通用户界面设计 v1.0（实现进度与已知边界在文末）：[`docs/user-interface/README.md`](docs/user-interface/README.md)
- WorldRuntime 总纲：[`docs/worldruntime/DESIGN.md`](docs/worldruntime/DESIGN.md)
- 会话核心 / 事件引擎 / 记忆 / 世界运行层：[`docs/worldruntime/`](docs/worldruntime/)
- Core Debugging（桌面壳与生成工作区）：[`docs/core%20debugging/DESKTOP_SPEC.md`](docs/core%20debugging/DESKTOP_SPEC.md)
- OC 故事层：[`docs/oc-story/OC_STORY_LAYER_SPEC.md`](docs/oc-story/OC_STORY_LAYER_SPEC.md)
- Writing Assistant：[`docs/writing-assistant/WRITING_ASSISTANT_SPEC.md`](docs/writing-assistant/WRITING_ASSISTANT_SPEC.md)
- TRPG 客户端 / 战役运行层 / 规则：[`docs/trpg-client/`](docs/trpg-client/)、[`docs/trpg-rules/`](docs/trpg-rules/)
- 历史存档（总设计对话全文 / 迭代记录 / V0.1 早期条款）：[`docs/worldruntime/archive/`](docs/worldruntime/archive/)

## 数据与隐私

- 数据根：源码运行默认仓库根；发行件默认 `%LOCALAPPDATA%\isekai`（`ISEKAI_ROOT` 可覆盖）。
- 数据库 `data/isekai.db`（SQLite，WAL）、通道凭据 `data/clients/*.json`、日志 `logs/`，都在 `.gitignore` 内。
- API 密钥只写在数据根的 `config/config.yaml` 里（或用环境变量 `ISEKAI_LLM_API_KEY`），
  不进入日志、插件环境或导出件；备份文件里也不含密钥。配置项见 [`config/README.md`](config/README.md)。
- 模型调用会把所需文本发到你配置的服务，本机存储与无云同步不等于全部计算离线。
- 规则插件是本机扩展程序：登记时会显示来源、规则与版本、入口，进程隔离不是完整安全沙箱。

## 这一版明确还没有的

- 安装程序（发行流程只出 zip + 手动解压；NSIS 目标已配置但未接入、未验收）、
  面向普通用户三条完整路径的真人试用记录、无障碍人工走查。
- 安卓工程本体（阶段 7 可选评估；目前只有进程内传输 `local_channel.py`）。
- 云同步、联网房间、多人账号、世界线合并、全规则书导入、通用战斗面板、自动出版。

## 许可

MIT，见 [LICENSE](LICENSE)。
