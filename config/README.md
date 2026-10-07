# 本地配置目录

- `config.example.yaml`：配置模板（入库）。
- `config.yaml`：实际生效的本地配置，**不入库**（见 `.gitignore`）；从模板复制后修改。
  注意：`config.yaml` 里可能已经有你填好的密钥——再复制一次会覆盖它。

## 要点

- `llm.api_key`：UI 写入或手编；读取时打码，不进入日志、第三方插件环境或导出件。
  也可用环境变量 `ISEKAI_LLM_API_KEY` 提供（优先级：配置文件里的值 → 环境变量）。
- `core.port = 0`：随机端口。端点由核心在启动时通过 stdout 的就绪握手交给壳，
  不写死在配置里，避免端口冲突。
- 阶段 0 的 `placeholder.*` 只是占位会话的三元组与提示词，不代表已经存在世界或角色。
- `runtime.sleep_wait_min_s` / `sleep_wait_max_s`（默认 30 / 120 秒）：角色处于睡眠块时，
  她的一条回复会先等一个随机长度再答（把这期间的多句话并成一批）。**第一句可能要等
  30–120 秒才出现回复，这是设计不是卡死**；嫌慢就把这两个值调小。
- 出问题时先跑这两条自检（源码或发行件里都可用）：
  `setup readiness`（本机与 AI 配置就绪）、`setup ai-test`（用给定地址 / 模型 / 密钥试一次调用）。

## 排错：中文 Windows 上的测试

- 子进程按代码页（cp936）写字节会让插件 / 规则插件帧解析失败，表现为
  `UnicodeEncodeError: surrogates not allowed` 或 `第一行不是合法 JSON`。
  跑测试或自带脚本时显式设 `PYTHONUTF8=1`（PowerShell：`$env:PYTHONUTF8=1`）。
  核心自己拉起子进程时已强制 UTF-8，不需要额外设置。

## 开发用开关（不写入正式配置）

- `ISEKAI_LLM_FAKE=1`：不调用远程 LLM，返回固定占位文本；用于协议链路自测与测试。
- `ISEKAI_LLM_FAKE_REPLY`：配合上一个开关使用的文本内容。
- `ISEKAI_ROOT`：覆盖数据根目录（默认仓库根）。
