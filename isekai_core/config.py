"""本地配置与路径。

配置只在本地：`config/config.yaml`（gitignore），代码内为默认值。
路径可被 ISEKAI_ROOT 覆盖，便于测试与打包。
"""

from __future__ import annotations

import os
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .version import (
    DEFAULT_MAX_ATTACHMENTS,
    DEFAULT_MAX_ATTACHMENT_BYTES,
    DEFAULT_MAX_CONNECTIONS,
    DEFAULT_MAX_PARTS,
    DEFAULT_MAX_QUEUED_INBOUND,
    DEFAULT_MAX_TEXT_LEN,
    DEFAULT_RATE_LIMIT_MSGS,
    DEFAULT_RATE_LIMIT_WINDOW_S,
)

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
    #: 角色记忆（MEMORY_SPEC §十：配额由运行层统一决定）
    memory_extract_per_day: int = 40     # 每日提取的现实日调用上限
    memory_recall_limit: int = 6         # 单轮简报条数上限
    memory_brief_tokens: int = 900       # 简报预算（字符量级）
    memory_decay_per_day: float = 0.02   # 每世界日强度衰减率
    #: 归档条目「强相关可唤起」的向量相似度门槛（§六）；逐字命中不受此限
    memory_archived_recall_min: float = 0.82
    memory_embedding_model: str = ""     # 远程 embedding 模型（空 = 只用全文召回）
    memory_embedding_base_url: str = ""
    memory_embedding_api_key: str = ""
    #: 睡眠期等待与合并（SESSION_CORE_SPEC §4.5）。等待区间取 §4.5 的起点值 30–120 秒：
    #: 每批在区间内随机取一拍（默认 45 秒量级），批内只等一拍——后续输入不重置、不叠加。
    sleep_wait_min_s: float = 30.0
    sleep_wait_max_s: float = 120.0
    merge_batch_max: int = 8            # 合并批容量（条数）：达到即封口，后来输入属下一批
    #: 版本管理（阶段 4）：自动提交默认现实 1 小时或新增事件 50 条
    autocommit_enabled: bool = True
    autocommit_minutes: int = 60
    autocommit_events: int = 50


@dataclass
class BackupConfig:
    """备份：目录、间隔、保留数（DESKTOP_SPEC §三 设置行）。"""

    dir: str = "backups"          # 相对数据根
    interval_hours: int = 24      # 到期检查间隔；0 = 只在显式退出前补做
    keep: int = 7                 # 保留份数


@dataclass
class Config:
    paths: Paths
    llm: LLMConfig = field(default_factory=LLMConfig)
    host: str = "127.0.0.1"
    port: int = 0  # 0 = 随机端口
    max_text_len: int = DEFAULT_MAX_TEXT_LEN
    max_parts: int = DEFAULT_MAX_PARTS
    context_history_max: int = 20
    #: 容量与限速（CHANNEL_PLUGIN_SPEC §3.2；仅 config.yaml，不进设置面）
    max_connections: int = DEFAULT_MAX_CONNECTIONS
    max_queued_inbound: int = DEFAULT_MAX_QUEUED_INBOUND
    rate_limit_msgs: int = DEFAULT_RATE_LIMIT_MSGS
    rate_limit_window_s: float = DEFAULT_RATE_LIMIT_WINDOW_S
    #: 附件配额（CHANNEL_PLUGIN_SPEC §七）：通道声明附件能力时按交集取小
    max_attachments: int = DEFAULT_MAX_ATTACHMENTS
    max_attachment_bytes: int = DEFAULT_MAX_ATTACHMENT_BYTES
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    #: 阶段 0 占位会话三元组与提示词；阶段 1 起被真实实例 / 角色卡取代
    placeholder: dict[str, str] = field(
        default_factory=lambda: {
            "instance_id": "ph-instance",
            "timeline_id": "main",
            "character_id": "ph-character",
            "system_prompt": DEFAULT_PLACEHOLDER_PROMPT,
        }
    )


def _mapped(model: Any, raw: dict[str, Any], section: str) -> Any:
    """按 dataclass 字段通用映射一个配置段——新字段只要同名就生效。"""
    values: dict[str, Any] = {}
    section_raw = _section(raw, section)
    base = model()
    for field_info in dataclasses.fields(model):
        name = field_info.name
        if name not in section_raw:
            continue
        raw_value = section_raw[name]
        default = getattr(base, name)
        try:
            if isinstance(default, bool):
                values[name] = bool(raw_value)
            elif isinstance(default, int):
                values[name] = int(raw_value)
            elif isinstance(default, float):
                values[name] = float(raw_value)
            else:
                values[name] = str(raw_value or "")
        except (TypeError, ValueError):
            continue  # 坏值退回默认，不让配置挡住启动
    return model(**values)


def _runtime_config(raw: dict[str, Any]) -> RuntimeConfig:
    """运行层参数：只读 config.yaml 的 `runtime` 段（通用映射，新字段自动生效）。"""
    return _mapped(RuntimeConfig, raw, "runtime")


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


# DESKTOP_SPEC §3.3：日常用户可调项的白名单（开发者专用键一律不开放）
SETTABLE_SECTIONS: dict[str, tuple[str, dict[str, str]]] = {
    "memory": (
        "runtime",
        {
            "mode": "memory_embedding_base_url",  # 派生态：chat = 清掉三项（见 validate_section_updates）
            "base_url": "memory_embedding_base_url",
            "model": "memory_embedding_model",
            "api_key": "memory_embedding_api_key",
        },
    ),
    "commit": (
        "runtime",
        {
            "auto_enabled": "autocommit_enabled",
            "minutes": "autocommit_minutes",
            "events": "autocommit_events",
        },
    ),
    "backup": ("backup", {"dir": "dir", "interval_hours": "interval_hours", "keep": "keep"}),
}

_NUMERIC_SECTION_KEYS = {"minutes", "events", "interval_hours", "keep"}


def validate_section_updates(section: str, updates: dict[str, Any]) -> dict[str, Any]:
    """段内可写键白名单 + 类型校验：不在白名单里的键直接拒绝并点名（§3.3 之外不开放）。"""
    if section not in SETTABLE_SECTIONS:
        raise SettingsError(f"不开放的设置段：{section}")
    _, mapping = SETTABLE_SECTIONS[section]
    cleaned: dict[str, Any] = {}
    for key, value in updates.items():
        if key not in mapping:
            raise SettingsError(f"{section} 段不开放这个键：{key}")
        if key == "mode":
            # 记忆向量化的「模式」是派生值：chat = 只用全文 → 显式清空三项（这里落空串是有意的，与「空串=不改」不同）
            if value not in ("chat", "separate"):
                raise SettingsError("memory.mode 只能是 chat 或 separate")
            if value == "chat":
                for target in ("memory_embedding_base_url", "memory_embedding_model", "memory_embedding_api_key"):
                    cleaned[target] = ""
            continue
        if key == "auto_enabled":
            cleaned[mapping[key]] = bool(value)
        elif key in _NUMERIC_SECTION_KEYS:
            number = int(value)
            if number < 0:
                raise SettingsError(f"{section}.{key} 不能为负")
            cleaned[mapping[key]] = number
        elif isinstance(value, bool):
            raise SettingsError(f"{section}.{key} 不是布尔项")
        else:
            text = str(value or "").strip()
            if text:  # 空串 = 不改（与 llm.api_key 同一口径）
                cleaned[mapping[key]] = text
    return cleaned


def save_section_settings(cfg: Config, section: str, updates: dict[str, Any]) -> Config:
    """把某个可写段的修改写回 config.yaml（保留其它段与未知键），返回重新加载后的配置。"""
    cleaned = validate_section_updates(section, updates)
    if not cleaned:
        return load_config(cfg.paths.root)
    raw: dict[str, Any] = {}
    if cfg.paths.config_file.exists():
        loaded = yaml.safe_load(cfg.paths.config_file.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            raw = loaded
    yaml_section, _ = SETTABLE_SECTIONS[section]
    target = _section(raw, yaml_section)
    target.update(cleaned)
    raw[yaml_section] = target
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
        max_connections=int(core_raw.get("max_connections") or DEFAULT_MAX_CONNECTIONS),
        max_queued_inbound=int(core_raw.get("max_queued_inbound") or DEFAULT_MAX_QUEUED_INBOUND),
        rate_limit_msgs=int(core_raw.get("rate_limit_msgs") or DEFAULT_RATE_LIMIT_MSGS),
        rate_limit_window_s=float(core_raw.get("rate_limit_window_s") or DEFAULT_RATE_LIMIT_WINDOW_S),
        max_attachments=int(core_raw.get("max_attachments") or DEFAULT_MAX_ATTACHMENTS),
        max_attachment_bytes=int(core_raw.get("max_attachment_bytes") or DEFAULT_MAX_ATTACHMENT_BYTES),
        runtime=_runtime_config(raw),
        backup=_mapped(BackupConfig, raw, "backup"),
    )

    placeholder = _section(raw, "placeholder")
    for key in ("instance_id", "timeline_id", "character_id", "system_prompt"):
        value = placeholder.get(key)
        if isinstance(value, str) and value.strip():
            cfg.placeholder[key] = value.strip()
    return cfg
