"""可用性修复的行为判据（2026-10 可用性审计 → 修复）。

一条测试一个「正常人会踩到的坑」，红了就说明那个坑回来了：

1. 子进程编码：插件 / 规则插件的 stdio 是 UTF-8 JSON 帧，不能取决于 Windows 代码页。
2. `setup ai-test` 的覆盖参数曾经是死代码（提前 return）。
3. 假模型路径的回复曾经被打印两遍（增量一次 + 最终一次）。
4. 缺密钥时曾经只给裸 traceback，不说去哪改。
5. 发行件里的 README 曾经是整份开发者 README（一片指向包内不存在 docs/ 的死链）。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import io
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from isekai_core import cli as cli_mod
from isekai_core.config import load_config
from isekai_core.plugins import child_env
from isekai_core.runtime import rules as rules_mod
from isekai_core.ump import Envelope, UmpError
from isekai_core.world_cli import OP_BY_COMMAND, _parser, build_args

REPO = Path(__file__).resolve().parent.parent


def _ns(**overrides) -> argparse.Namespace:
    base = {
        "group": "setup", "command": "ai-test", "name": None, "density": None, "file": None,
        "out": None, "package": None, "brief": None, "instruction": None, "section": None,
        "card": None, "display_name": None, "id": None, "timeline": None, "rate": None,
        "max_batches": None, "moment": None, "at": None, "note": None, "acquainted": False,
        "base_url": None, "model": None, "api_key": None, "timeout": None, "max_tokens": None,
        "temperature": None, "key": None, "module": None, "target": None, "payload": None,
        "text": None, "request": None,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


# ---------- 1. 子进程 UTF-8 ----------

def test_plugin_child_env_pins_utf8() -> None:
    """插件子进程必须拿到 UTF-8 开关：否则中文 Windows 上握手帧按 cp936 写出、核心解不动。"""
    env = child_env(ISEKAI_PLUGIN_ID="demo")
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert env["PYTHONUTF8"] == "1"
    assert env["ISEKAI_PLUGIN_ID"] == "demo"
    # 最小环境原则不变：不继承核心凭据
    assert "ISEKAI_LLM_API_KEY" not in env


def test_rules_child_spawns_pin_utf8() -> None:
    """常驻规则插件与其桥的子进程同样要钉住编码（三条 create_subprocess_exec 都要带 env）。"""
    source = (REPO / "isekai_core" / "runtime" / "rules.py").read_text(encoding="utf-8")
    spawns = source.count("asyncio.create_subprocess_exec")
    assert spawns == 3, f"规则层子进程数量变了（{spawns}），逐条确认是否都带 env"
    assert source.count("_CHILD_UTF8_ENV") >= spawns, "有子进程没带 UTF-8 环境"
    assert rules_mod._CHILD_UTF8_ENV["PYTHONIOENCODING"] == "utf-8"

    bridge = (REPO / "isekai_core" / "runtime" / "plugin_bridge.py").read_text(encoding="utf-8")
    assert 'PYTHONIOENCODING": "utf-8"' in bridge, "桥起真插件时没钉住编码"


# ---------- 2. setup ai-test 的覆盖参数 ----------

def test_setup_ai_test_overrides_reach_build_args() -> None:
    """曾经 `group == "setup"` 提前 return，把这几个参数变成死代码（实测 applied:{}）。"""
    args = build_args(_ns(base_url="http://127.0.0.1:1", api_key="sk-x", timeout="5"))
    assert args == {"llm": {"base_url": "http://127.0.0.1:1", "api_key": "sk-x", "timeout_s": 5.0}}


def test_setup_migrate_keeps_path_argument() -> None:
    """setup migrate 的 path 参数不能因为收口而死掉。"""
    args = build_args(_ns(command="migrate", file="old-root"))
    assert args["path"] == "old-root" and args["note"]


def test_parser_help_lists_commands_by_group() -> None:
    """`--help` 开头必须有命令总览：新人拿到的第一屏不该是 255 行选项墙。"""
    help_text = _parser().format_help()
    assert "命令总览" in help_text
    for group, command in OP_BY_COMMAND:
        assert command in help_text


# ---------- 3. 回复不重复打印 ----------

class _FakeClient:
    """只实现 run_turn 用到的那几个方法：发一条、按序给帧、回收执。"""

    def __init__(self, envelopes: list[Envelope]) -> None:
        self._envelopes = list(envelopes)
        self.deliveries: list[dict] = []

    async def send_user_message(self, **_kw) -> str:
        return "env-1"

    async def expect(self, _predicate, *, timeout: float = 30.0) -> Envelope:
        if not self._envelopes:
            raise AssertionError("run_turn 多要了一帧")
        return self._envelopes.pop(0)

    async def report_delivery(self, **kw) -> None:
        self.deliveries.append(kw)


def _turn_envelopes(reply: str) -> list[Envelope]:
    return [
        Envelope(type="status", id="e1", ts=1.0, payload={"state": "thinking"}),
        Envelope(type="reply_delta", id="e2", ts=1.0, payload={"index": 0, "text": reply}),
        Envelope(type="reply", id="e3", ts=1.0, payload={
            "reply_to": "env-1", "message_id": "m-1", "batch_index": 0, "batch_count": 1,
            "parts": [{"text": reply}],
        }),
    ]


def test_streamed_reply_printed_once(capsys) -> None:
    """假模型（会走流式分支）的正文只该出现一次；曾经增量与最终各打一遍。"""
    client = _FakeClient(_turn_envelopes("（占位回复）"))
    text = asyncio.run(cli_mod.run_turn(client, thread_id="t", token="tok", text="你好"))
    out = capsys.readouterr().out
    assert text == "（占位回复）"
    assert out.count("（占位回复）") == 1, f"回复被打印了多次：{out!r}"


def test_example_config_model_matches_code_default() -> None:
    """示例配置里的模型名不能和代码默认值漂移：新人照抄示例，两处不一致就是踩坑。"""
    from isekai_core.config import LLMConfig

    raw = (REPO / "config" / "config.example.yaml").read_text(encoding="utf-8")
    match = re.search(r"(?m)^\s{2}model:\s*(\S+)\s*$", raw)
    assert match, "config.example.yaml 里找不到 llm.model"
    assert match.group(1) == LLMConfig.model, (
        f"示例配置模型 {match.group(1)!r} ≠ 代码默认 {LLMConfig.model!r}"
    )


# ---------- 4. 错误信息能定位 ----------
def test_llm_not_configured_error_says_where_to_fix(tmp_path) -> None:
    cfg = load_config(tmp_path)
    message = cli_mod._explain_error(UmpError("llm_not_configured", "未配置 LLM API Key"), cfg)
    assert "llm.api_key" in message
    assert str(cfg.paths.config_file) in message
    assert "setup ai-test" in message


def test_unknown_llm_error_still_readable(tmp_path) -> None:
    cfg = load_config(tmp_path)
    message = cli_mod._explain_error(UmpError("whatever", "出错了"), cfg)
    assert message.startswith("失败：") and "出错了" in message


# ---------- 5. CLI 自己的 stdout 也要是 UTF-8 ----------

class _RecordingStream:
    """替身流：只记录有没有被要求重配编码（真实 sys.stdout 不能随便换）。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def reconfigure(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_cli_console_encoding_is_pinned(monkeypatch) -> None:
    """CLI 自己也是被管道捕获的一方：cp936 下写中文 / `•` 会抛 UnicodeEncodeError。

    既有缺陷（不是本轮引入）：`setup ai-test --api-key … | 管道` 曾以
    `UnicodeEncodeError: 'gbk' codec can't encode character '\\u2022'` 崩溃，
    把「自检失败」变成堆栈。核心/插件是子进程，编码早钉住了；父进程自己也要钉。
    """
    monkeypatch.setattr(cli_mod, "_UTF8_CONSOLE_READY", False)
    out, err = _RecordingStream(), _RecordingStream()
    monkeypatch.setattr(cli_mod.sys, "stdout", out)
    monkeypatch.setattr(cli_mod.sys, "stderr", err)

    cli_mod.ensure_utf8_console()
    assert out.calls and out.calls[0]["encoding"] == "utf-8"
    assert err.calls and err.calls[0]["encoding"] == "utf-8"
    # 非交互（测试环境不是 TTY）：退回 replace，别让一个怪字符中断读数
    assert out.calls[0]["errors"] == "replace"

    cli_mod.ensure_utf8_console()   # 幂等：进程内标记，不会重复动流
    assert len(out.calls) == 1


def test_console_encoding_not_leaked_via_environment(monkeypatch) -> None:
    """幂等标记不能用环境变量：CLI 起子进程时 `{**os.environ}` 会把它带过去，
    继承的子 Python 进程会静默跳过重配。"""
    monkeypatch.setattr(cli_mod, "_UTF8_CONSOLE_READY", False)
    monkeypatch.setattr(cli_mod.sys, "stdout", _RecordingStream())
    monkeypatch.setattr(cli_mod.sys, "stderr", _RecordingStream())
    cli_mod.ensure_utf8_console()
    assert not any("UTF8_CONSOLE" in name for name in cli_mod.os.environ)


def test_cli_console_encoding_survives_streams_without_reconfigure(monkeypatch) -> None:
    """测试里 stdout 常被换成 StringIO：没有 reconfigure 就跳过，不许炸。"""
    monkeypatch.setattr(cli_mod, "_UTF8_CONSOLE_READY", False)
    monkeypatch.setattr(cli_mod.sys, "stdout", io.StringIO())
    monkeypatch.setattr(cli_mod.sys, "stderr", io.StringIO())
    cli_mod.ensure_utf8_console()   # 不抛异常即通过


@pytest.mark.parametrize("module_args", [
    ["isekai_core.cli", "--help"],
    ["isekai_core.world_cli", "--help"],
])
def test_entrypoint_help_is_utf8_even_when_piped(module_args) -> None:
    """调用点必须早于参数解析：`-h` 在 parse_args 内部就打印，重配晚一步就还是 cp936。

    这里用真子进程 + 管道（等价于 `| Out-File`），输出必须是合法 UTF-8——
    这条判据正是上一轮漏掉的那种「函数对了、调用点不对」。
    """
    proc = subprocess.run(
        [sys.executable, "-m", *module_args],
        capture_output=True, cwd=str(REPO),
        env={k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")},
    )
    assert proc.returncode == 0, proc.stderr
    proc.stdout.decode("utf-8")   # 不是 UTF-8 就抛 UnicodeDecodeError
    assert "usage" in proc.stdout.decode("utf-8").lower() or "用法" in proc.stdout.decode("utf-8")


@pytest.mark.parametrize("module_args", [
    ["isekai_core.cli", "--definitely-not-a-flag"],
    ["isekai_core.world_cli", "--definitely-not-a-flag"],
])
def test_entrypoint_errors_are_utf8_on_stderr_too(module_args) -> None:
    """argparse 的错误走 stderr：它同样是管道里被捕获的一方，也要是合法 UTF-8。

    只守 stdout 的判据会漏掉「有人把 ensure 挪回去 / 只对 stdout 生效」这类回退。
    （两个入口的报错文本不同：`cli` 是 unrecognized arguments，`world_cli` 是
    「缺少 group/command」——所以这里只断言「是合法 UTF-8 且有内容」。）
    """
    proc = subprocess.run(
        [sys.executable, "-m", *module_args],
        capture_output=True, cwd=str(REPO),
        env={k: v for k, v in os.environ.items() if k not in ("PYTHONUTF8", "PYTHONIOENCODING")},
    )
    assert proc.returncode != 0
    stderr = proc.stderr.decode("utf-8")   # 不是 UTF-8 就抛 UnicodeDecodeError
    assert stderr.strip(), "错误信息不该为空"


# ---------- 6. 发行件 README 自包含 ----------
def _build_release_module():
    spec = importlib.util.spec_from_file_location(
        "build_release_under_test", REPO / "scripts" / "build_release.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_release_readme_has_no_dead_repo_links() -> None:
    """发行件里没有 docs/、tests/、scripts/，README 不能链向它们。"""
    readme = _build_release_module().release_readme()
    dead = [
        line for line in readme.splitlines()
        if re.search(r"\]\((?!https?:)[^)]*(?:^|/)docs/", line)
    ]
    assert not dead, f"发行件 README 有指向仓库 docs/ 的死链：{dead}"
    assert "五步" in readme and "发行说明.md" in readme
    assert "python -m pytest" not in readme, "发行件 README 不该出现开发命令"


def test_release_note_covers_first_run_frictions() -> None:
    """首次上手最容易劝退的三件事要写在发行说明里：签名 / 慢一拍 / 没有密钥也能用。"""
    note = _build_release_module().release_note("0.1.0")
    assert "已保护你的电脑" in note
    assert "30–120 秒" in note
    assert "稍后配置" in note
