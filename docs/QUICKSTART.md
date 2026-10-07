# QUICKSTART：十分钟跑通一个世界

面向**对本项目零经验**的开发者。目标：从 clone 到「控制台里看到角色回你一句话」，
只用仓库自带的样例世界，**不需要 AI 密钥**（先跑通链路，之后要真的对话再配密钥）。

> 全程命令都写成 PowerShell（Windows 默认）。bash 用户把 `$env:X=1` 换成 `X=1` 前缀即可。
> 想改代码、跑测试、看目录结构：接 [开发指南](DEVELOPING.md)。

---

## 0. 前置条件

| 需要 | 版本 | 说明 |
|---|---|---|
| Python | **3.11** | 必须；`requires-python = ">=3.11"` |
| [uv](https://docs.astral.sh/uv/) | 任意近期版 | 用来建虚拟环境；不想装 uv 也可以直接 `python -m venv .venv` |
| 终端 | PowerShell / bash | 下面命令两者都有对应写法 |

下面命令默认按 Windows / PowerShell 写；bash 用户把 `$env:X=1` 换成 `X=1` 前缀即可。
仓库的测试在中文 Windows（cp936）上也能直接跑通，不需要额外设编码开关（见文末「常见卡点」）。

## 1. 装好环境（约 1 分钟）

```powershell
uv venv .venv --python 3.11
uv pip install --python .venv/Scripts/python.exe -e ".[dev]"
```

bash / macOS / Linux 把第二行的路径换成 `.venv/bin/python`。

> 这一步只装 3 个运行依赖（websockets / httpx / PyYAML）+ 2 个测试依赖，不需要联网模型。

## 2. 先看一句回复（不需要任何密钥）

```powershell
$env:ISEKAI_LLM_FAKE=1
.venv/Scripts/python.exe -m isekai_core.cli --say "你好"
```

- `ISEKAI_LLM_FAKE=1` 让核心不调远程模型，返回固定占位文本；用来确认链路是通的。
- 你应该看到 `· 核心已就绪 ws://127.0.0.1:<随机端口>` 然后 `角色> （占位回复）`。
- **注意**：`ISEKAI_LLM_FAKE=1 .venv/...`（写成一行前缀）在 PowerShell 里不生效，
  必须像上面那样先 `$env:ISEKAI_LLM_FAKE=1` 再执行命令。

## 3. 跑通样例世界（约 2 分钟）

样例世界「灰潮纪」在 [`examples/sample_world/`](../examples/sample_world/README.md)（一部世界包 + 两张已审定角色卡）。

**重要**：下面的命令都带 `--root .tmp-run`，让数据落在仓库里的临时目录，不去碰 `data/`、`packages/`、`logs/`。
不带 `--root` 时会回落到仓库根——那是开发常态，但你第一次跑建议先隔离。

```powershell
New-Item -ItemType Directory -Force .tmp-run/packages | Out-Null
Copy-Item examples/sample_world/huichao*.json .tmp-run/packages/

$py = ".venv/Scripts/python.exe"
$py -m isekai_core.world_cli --root .tmp-run package validate --file .tmp-run/packages/huichao.json
$py -m isekai_core.world_cli --root .tmp-run card confirm --package .tmp-run/packages/huichao.json --file .tmp-run/packages/huichao.card1.json
$py -m isekai_core.world_cli --root .tmp-run card confirm --package .tmp-run/packages/huichao.json --file .tmp-run/packages/huichao.card2.json
$py -m isekai_core.world_cli --root .tmp-run instance create --package .tmp-run/packages/huichao.json `
    --card .tmp-run/packages/huichao.card1.json,.tmp-run/packages/huichao.card2.json --display-name 灰潮纪
```

看好顺序：**包先过校验 → 两张卡都确认 → 才能创建实例**。未确认的角色卡进不了实例。

创建完拿实例标识，再激活它并说一句话：

```powershell
$py -m isekai_core.world_cli --root .tmp-run instance list          # 取 "id"
$inst = "in-xxxxxxxxxxxx"                                           # 填上面拿到的 id

$py -m isekai_core.world_cli --root .tmp-run runtime activate --id $inst
$py -m isekai_core.cli --root .tmp-run --instance $inst --say "退潮了吗？"
```

- `--timeline` 可以省略：只给 `--instance` 时会自动取该实例第一条时间线。
- 创建出来的实例**默认冻结**；不激活只能读历史，激活后世界才随现实时间推进。

> **第一句可能要等 30–120 秒才出回复**：角色若处于睡眠块，她的回复会先攒一拍
> （把这段时间的多句话并成一批，见 `SESSION_CORE_SPEC §4.5`）。这是设计不是卡死；
> 在 `.tmp-run/config/config.yaml` 里把 `runtime.sleep_wait_min_s` / `sleep_wait_max_s`
> 调成 1 / 2 就会立刻回答。

## 4. 接真模型（可选）

```powershell
Copy-Item config/config.example.yaml .tmp-run/config/config.yaml   # 注意别覆盖 config/config.yaml
# 编辑 .tmp-run/config/config.yaml，填 llm.api_key（地址与模型名按你用的服务商改）
$py -m isekai_core.world_cli --root .tmp-run setup ai-test          # 自检：连得上、模型回得出结构化输出
$py -m isekai_core.cli --root .tmp-run --instance $inst --say "退潮了吗？"
```

- 只填密钥不想改文件：设 `$env:ISEKAI_LLM_API_KEY="sk-..."`。
- 报错时客户端会说清**哪一项配置错了、去哪里改**；`setup readiness` 看整体就绪状态。

## 5. 跑测试

```powershell
$env:ISEKAI_LLM_FAKE=1
.venv/Scripts/python.exe -m pytest -q
```

当前基线：**605 项全过**（含新增的 `tests/test_usability_fixes.py` 18 项）。

> 中文 Windows 不需要额外设 `PYTHONUTF8`：核心 / 插件 / 规则插件的子进程由代码显式钉住
> UTF-8（`isekai_core.plugins.child_env`、`runtime/rules.py`）。你自己写的脚本起子进程时
> 仍建议设一次。

---

## 常见卡点

| 现象 | 原因 | 怎么办 |
|---|---|---|
| `UnicodeEncodeError: surrogates not allowed` 或 `第一行不是合法 JSON` | 非 UTF-8 代码页（中文 Windows cp936）下的子进程按代码页写字节；核心自己拉起的子进程已强制 UTF-8，但你自己写的脚本 / 外部工具没有 | 在你自己的脚本环境里设 `$env:PYTHONUTF8=1`；仓库测试不需要 |
| `ISEKAI_LLM_FAKE=1 ...` 报「术语不被识别」 | 那是 bash 的写法，PowerShell 不支持行内环境变量前缀 | 先 `$env:ISEKAI_LLM_FAKE=1` 再执行命令 |
| 回复要等一分钟 | 角色处于睡眠块（合并批） | 正常；调小 `runtime.sleep_wait_min_s/max_s`，或换一个不在睡眠时段的角色 / 时刻 |
| `llm_not_configured` | 没填密钥 | 按客户端提示改 `config/config.yaml` 的 `llm.api_key`，或设 `ISEKAI_LLM_API_KEY`；只跑链路就 `ISEKAI_LLM_FAKE=1` |
| 世界包说「文件不存在」 | 命令没带 `--root`，落到了默认根 | 像本文一样统一带 `--root .tmp-run` |
| `world_cli` 不记得有哪些命令 | — | 直接跑 `python -m isekai_core.world_cli --help`，开头就是命令总览 |

## 接下来读什么

1. [开发指南](DEVELOPING.md) —— 目录结构、桌面壳构建、测试组织、审计探针。
2. [设计文档地图](README.md) —— 按模块进入各 SPEC。
3. [WorldRuntime 总纲](worldruntime/DESIGN.md) —— 世界事实、时间、认知、版本的主线。
