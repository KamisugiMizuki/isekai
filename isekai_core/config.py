"""本地配置与路径。

配置只在本地：`config/config.yaml`（gitignore），代码内为默认值。
路径可被 ISEKAI_ROOT 覆盖，便于测试与打包。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .version import DEFAULT_MAX_PARTS, DEFAULT_MAX_TEXT_LEN

DEFAULT_PLACEHOLDER_PROMPT = (
    "你是 isekai 核心进程的占位对话端（阶段 0：世界与角色尚未接入）。"
    "只做简短、平实的回应，不要虚构世界设定、经历或身份。"
)


@dataclass(frozen=True)
class Paths:
    root: Path
    data: Path
    logs: Path
    clients: Path
    config_file: Path

    @property
    def db(self) -> Path:
        return self.data / "isekai.db"

    @property
    def lock(self) -> Path:
        return self.data / "core.lock"


def resolve_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    if explicit:
        return Path(explicit).resolve()
    env = os.environ.get("ISEKAI_ROOT")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parent.parent


def paths_for(root: Path) -> Paths:
    data = root / "data"
    return Paths(
        root=root,
        data=data,
        logs=root / "logs",
        clients=data / "clients",
        config_file=root / "config" / "config.yaml",
    )


@dataclass
class LLMConfig:
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    api_key: str = ""
    timeout_s: float = 60.0
    max_tokens: int = 1024
    temperature: float = 0.8


@dataclass
class Config:
    paths: Paths
    llm: LLMConfig = field(default_factory=LLMConfig)
    host: str = "127.0.0.1"
    port: int = 0  # 0 = 随机端口
    max_text_len: int = DEFAULT_MAX_TEXT_LEN
    max_parts: int = DEFAULT_MAX_PARTS
    context_history_max: int = 20
    #: 阶段 0 占位会话三元组与提示词；阶段 1 起被真实实例 / 角色卡取代
    placeholder: dict[str, str] = field(
        default_factory=lambda: {
            "instance_id": "ph-instance",
            "timeline_id": "main",
            "character_id": "ph-character",
            "system_prompt": DEFAULT_PLACEHOLDER_PROMPT,
        }
    )


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


def load_config(root: str | os.PathLike[str] | None = None) -> Config:
    paths = paths_for(resolve_root(root))
    raw: dict[str, Any] = {}
    if paths.config_file.exists():
        loaded = yaml.safe_load(paths.config_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded

    llm_raw = _section(raw, "llm")
    llm = LLMConfig(
        base_url=str(llm_raw.get("base_url") or LLMConfig.base_url),
        model=str(llm_raw.get("model") or LLMConfig.model),
        api_key=str(llm_raw.get("api_key") or os.environ.get("ISEKAI_LLM_API_KEY", "")),
        timeout_s=float(llm_raw.get("timeout_s") or 60.0),
        max_tokens=int(llm_raw.get("max_tokens") or 1024),
        temperature=float(llm_raw.get("temperature") or 0.8),
    )

    core_raw = _section(raw, "core")
    cfg = Config(
        paths=paths,
        llm=llm,
        host=str(core_raw.get("host") or "127.0.0.1"),
        port=int(core_raw.get("port") or 0),
        max_text_len=int(core_raw.get("max_text_len") or DEFAULT_MAX_TEXT_LEN),
        max_parts=int(core_raw.get("max_parts") or DEFAULT_MAX_PARTS),
        context_history_max=int(core_raw.get("context_history_max") or 20),
    )

    placeholder = _section(raw, "placeholder")
    for key in ("instance_id", "timeline_id", "character_id", "system_prompt"):
        value = placeholder.get(key)
        if isinstance(value, str) and value.strip():
            cfg.placeholder[key] = value.strip()
    return cfg
