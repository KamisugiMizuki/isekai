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

    @property
    def packages(self) -> Path:
        """世界包 / 角色卡创作目录（管理面与桌面的默认落盘位置）。"""
        return self.root / "packages"

    @property
    def exports(self) -> Path:
        return self.root / "exports"


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


@dataclass(frozen=True)
class RuntimeConfig:
    """世界运行层参数：全局统一、仅开发者可配置（WORLD_RUNTIME_SPEC §2.2 / §2.6 / §4）。

    刻意不进设置 UI：倍率上限与世界运行预算不是日常可调项（§8.1「倍率上限被误改」）。
    """

    rate_max: int = 2592000             # 世界秒 / 现实秒
    max_active_timelines: int = 4       # 同时激活的时间线数量上限
    catch_up_batches: int = 8           # 单次推进批数上限（每批一个世界日）
    catch_up_lag_seconds: int = 172800  # 滞后超过 2 世界日即记为「追赶受限」
    render_calls_per_day: int = 20      # 事件表述 / 展开的现实日调用上限（§2.8 单任务预算）
    instance_tokens_per_day: int = 400_000   # 实例总预算（所有激活线共享）
    timeline_tokens_per_day: int = 150_000   # 时间线预算（防一条高倍率线占尽资源）
    task_tokens_per_day: int = 60_000        # 单任务预算（防重试或坏输入耗尽整条线）
    priority_reserve_ratio: float = 0.25     # 给更高优先级任务留出的额度比例


@dataclass
class Config:
    paths: Paths
    llm: LLMConfig = field(default_factory=LLMConfig)
    host: str = "127.0.0.1"
    port: int = 0  # 0 = 随机端口
    max_text_len: int = DEFAULT_MAX_TEXT_LEN
    max_parts: int = DEFAULT_MAX_PARTS
    context_history_max: int = 20
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    #: 阶段 0 占位会话三元组与提示词；阶段 1 起被真实实例 / 角色卡取代
    placeholder: dict[str, str] = field(
        default_factory=lambda: {
            "instance_id": "ph-instance",
            "timeline_id": "main",
            "character_id": "ph-character",
            "system_prompt": DEFAULT_PLACEHOLDER_PROMPT,
        }
    )


def _runtime_config(raw: dict[str, Any]) -> RuntimeConfig:
    """运行层参数：只读 config.yaml 的 runtime 段（无 UI 写入路径）。"""
    section = _section(raw, "runtime")
    base = RuntimeConfig()
    return RuntimeConfig(
        rate_max=int(section.get("rate_max") or base.rate_max),
        max_active_timelines=int(section.get("max_active_timelines") or base.max_active_timelines),
        catch_up_batches=int(section.get("catch_up_batches") or base.catch_up_batches),
        catch_up_lag_seconds=(
            int(section["catch_up_lag_seconds"])
            if "catch_up_lag_seconds" in section
            else base.catch_up_lag_seconds
        ),
        render_calls_per_day=(
            int(section["render_calls_per_day"])
            if "render_calls_per_day" in section
            else base.render_calls_per_day
        ),
    )


def _section(raw: dict[str, Any], key: str) -> dict[str, Any]:
    value = raw.get(key)
    return value if isinstance(value, dict) else {}


class SettingsError(ValueError):
    """设置项非法：保留原值，不写盘。"""


def mask_api_key(key: str) -> str:
    """读取打码：不回显完整 Key。"""
    if not key:
        return ""
    return "•" * 8 + key[-4:] if len(key) > 4 else "•" * 8


def validate_llm_updates(updates: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化 llm 段的可写字段；非法即抛 SettingsError（调用方保留原值）。"""
    allowed = {"base_url", "model", "api_key", "timeout_s", "max_tokens", "temperature"}
    unknown = set(updates) - allowed
    if unknown:
        raise SettingsError(f"不支持的设置项：{', '.join(sorted(unknown))}")
    cleaned: dict[str, Any] = {}
    for key, value in updates.items():
        if key in ("base_url", "model"):
            if not isinstance(value, str) or not value.strip():
                raise SettingsError(f"{key} 不能为空")
            cleaned[key] = value.strip()
        elif key == "api_key":
            if not isinstance(value, str):
                raise SettingsError("api_key 必须是字符串")
            cleaned[key] = value.strip()
        elif key == "temperature":
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= float(value) <= 2:
                raise SettingsError("temperature 必须在 0–2 之间")
            cleaned[key] = float(value)
        else:
            if not isinstance(value, (int, float)) or isinstance(value, bool) or float(value) <= 0:
                raise SettingsError(f"{key} 必须是正数")
            cleaned[key] = float(value)
    return cleaned


def save_llm_settings(cfg: Config, updates: dict[str, Any]) -> Config:
    """把 llm 段的修改写回 config.yaml（保留其它段与未知键），返回重新加载后的配置。"""
    cleaned = validate_llm_updates(updates)
    raw: dict[str, Any] = {}
    if cfg.paths.config_file.exists():
        loaded = yaml.safe_load(cfg.paths.config_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    llm_raw = _section(raw, "llm")
    llm_raw.update(cleaned)
    raw["llm"] = llm_raw
    cfg.paths.config_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.paths.config_file.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return load_config(cfg.paths.root)


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
        runtime=_runtime_config(raw),
    )

    placeholder = _section(raw, "placeholder")
    for key in ("instance_id", "timeline_id", "character_id", "system_prompt"):
        value = placeholder.get(key)
        if isinstance(value, str) and value.strip():
            cfg.placeholder[key] = value.strip()
    return cfg
