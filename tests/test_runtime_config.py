"""运行层配置链路（回归：runtime 段 → RuntimeConfig → 运行层服务）。

踩过的坑：新键只加了 dataclass 字段，`_runtime_config` 逐字段手抄没同步 → 配置静默失效；
示例配置还把这批键落到了 placeholder 段下面。
"""

from __future__ import annotations

import pathlib

import yaml

from isekai_core.config import load_config
from isekai_core.runtime.service import from_config
from test_runtime import store  # noqa: F401  夹具在那边

REPO = pathlib.Path(__file__).resolve().parents[1]


def _write_config(tmp_path: pathlib.Path, runtime: dict) -> pathlib.Path:
    """load_config 接收的是数据根目录（其下 config/config.yaml）。"""
    (tmp_path / "config").mkdir(exist_ok=True)
    path = tmp_path / "config" / "config.yaml"
    path.write_text(
        yaml.safe_dump({"llm": {"base_url": "http://127.0.0.1:1/v1", "model": "m", "api_key": "k"},
                        "runtime": runtime}, allow_unicode=True),
        encoding="utf-8",
    )
    return tmp_path


def test_runtime_section_reaches_the_service(tmp_path, store) -> None:
    """config.yaml 的 runtime 段必须真的走到运行层（embedding / 记忆 / 自动提交一视同仁）。"""
    path = _write_config(tmp_path, {
        "memory_embedding_model": "stub-embed",
        "memory_embedding_base_url": "http://127.0.0.1:18080/v1",
        "memory_embedding_api_key": "local",
        "memory_recall_limit": 3,
        "memory_brief_tokens": 500,
        "memory_decay_per_day": 0.05,
        "memory_extract_per_day": 7,
        "autocommit_enabled": False,
        "autocommit_minutes": 15,
        "autocommit_events": 3,
        "render_calls_per_day": 9,
    })
    cfg = load_config(path)
    world = from_config(cfg, store)
    assert world.embedding_ready is True
    assert world.embedding_model == "stub-embed"
    assert world.embedding_base_url.endswith("/v1")
    assert (world.memory_recall_limit, world.memory_brief_tokens) == (3, 500)
    assert world.memory_decay_per_day == 0.05 and world.memory_extract_per_day == 7
    assert world.autocommit_enabled is False and world.autocommit_minutes == 15 and world.autocommit_events == 3
    assert world.render_calls_per_day == 9


def test_unset_and_bad_values_fall_back_to_defaults(tmp_path, store) -> None:
    """没写的键保持默认；写坏的键退回默认而不是挡住启动。"""
    path = _write_config(tmp_path, {"memory_recall_limit": "不是数字", "memory_embedding_model": ""})
    cfg = load_config(path)
    world = from_config(cfg, store)
    assert world.memory_recall_limit == 6, "坏值退回默认"
    assert world.embedding_ready is False and world.autocommit_minutes == 60


def test_example_config_keeps_runtime_keys_in_the_runtime_section() -> None:
    """示例配置里这批键必须在 runtime 段——曾整体落到 placeholder 段下面（写进去也不生效）。"""
    data = yaml.safe_load((REPO / "config" / "config.example.yaml").read_text(encoding="utf-8"))
    runtime = data.get("runtime") or {}
    for key in (
        "memory_extract_per_day", "memory_recall_limit", "memory_brief_tokens", "memory_decay_per_day",
        "memory_embedding_model", "memory_embedding_base_url", "memory_embedding_api_key",
        "autocommit_enabled", "autocommit_minutes", "autocommit_events",
    ):
        assert key in runtime, f"{key} 不在 runtime 段"
        assert key not in (data.get("placeholder") or {}), f"{key} 误落在 placeholder 段"
