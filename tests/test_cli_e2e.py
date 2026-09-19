"""子进程级端到端：真实拉起核心（就绪握手 → UMP → 回复），以及单写入者保护。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def _env(**extra: str) -> dict[str, str]:
    env = {**os.environ, "ISEKAI_LLM_FAKE": "1", "PYTHONIOENCODING": "utf-8"}
    env.update(extra)
    return env


def test_cli_single_turn_over_ump(tmp_path):
    proc = subprocess.run(
        [sys.executable, "-m", "isekai_core.cli", "--root", str(tmp_path), "--say", "你好", "--quiet"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=_env(ISEKAI_LLM_FAKE_REPLY="这里是占位回复。"),
    )
    assert proc.returncode == 0, proc.stderr
    assert "这里是占位回复。" in proc.stdout
    # 会话与绑定落库
    assert (tmp_path / "data" / "isekai.db").exists()


def test_cli_reuses_persistent_credential_on_second_run(tmp_path):
    args = [sys.executable, "-m", "isekai_core.cli", "--root", str(tmp_path), "--say", "再问一次", "--quiet"]
    first = subprocess.run(args, cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=120, env=_env())
    second = subprocess.run(args, cwd=REPO, capture_output=True, text=True, encoding="utf-8", timeout=120, env=_env())
    assert first.returncode == 0 and second.returncode == 0, second.stderr
    credentials = json.loads((tmp_path / "data" / "clients" / "cli-dev.json").read_text(encoding="utf-8"))
    assert credentials["credential"].startswith("cr-")


def test_second_core_refuses_to_write_same_data_dir(tmp_path):
    first = subprocess.Popen(
        [sys.executable, "-m", "isekai_core", "--root", str(tmp_path)],
        cwd=REPO,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=_env(),
    )
    try:
        assert first.stdout is not None
        ready = json.loads(first.stdout.readline().decode("utf-8"))
        assert ready["event"] == "ready"
        assert ready["endpoint"].startswith("ws://127.0.0.1:")

        second = subprocess.run(
            [sys.executable, "-m", "isekai_core", "--root", str(tmp_path)],
            cwd=REPO,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            env=_env(),
        )
        assert second.returncode == 3
        assert "already_running" in second.stdout
    finally:
        first.terminate()
        first.wait(timeout=10)


def test_stale_lock_is_taken_over(tmp_path):
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    # 一个已经不存在的 pid 留下的锁文件：应被接管而不是永久阻塞
    (tmp_path / "data" / "core.lock").write_text(
        json.dumps({"pid": 999999, "started_at": 0, "app": "0.0"}), encoding="utf-8"
    )
    proc = subprocess.run(
        [sys.executable, "-m", "isekai_core.cli", "--root", str(tmp_path), "--say", "喂", "--quiet"],
        cwd=REPO,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        env=_env(ISEKAI_LLM_FAKE_REPLY="收到。"),
    )
    assert proc.returncode == 0, proc.stderr
    assert "收到。" in proc.stdout
