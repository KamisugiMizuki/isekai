"""可信转换器（WORLD_SETTING_SPEC §7.5 / §7.6）。

转换器是**注册表 + 显式执行**：可以自动发现，执行必须由用户确认；不反序列化任意代码对象、
不执行包中自称的转换脚本。转换只在副本上做，完整校验通过后才原子发布。

当前数据格式 0.1 没有需要迁移的历史格式，因此内置表为空；格式发生不兼容变更时在此登记迁移，
导入 / 打开实例的兼容判定会自动看到它。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .package import normalize_name

Payload = dict[str, Any]
Converter = Callable[[Payload], Payload]

_REGISTRY: dict[tuple[str, str], Converter] = {}
#: 首次登记时的写法：查找按规范化键，展示按原样（§十 残余「名称规范化细则」）
_DISPLAY: dict[tuple[str, str], tuple[str, str]] = {}


def normalize_format(name: str) -> str:
    """格式标识的规范化：去首尾空白 + NFKC + 大小写折叠（与实例 / 包名同一套规则，§7.4）。

    规范化只作用于**登记与查找**：`Terra V1.2` 与 `terra v1.2` 命中同一个转换器；
    `converters()` 仍按首次登记时的写法展示。转换器返回的载荷不做任何规范化。
    """
    return normalize_name(name)


class ConverterError(ValueError):
    """转换器缺失或转换结果不可用。"""


def register_converter(source: str, target: str, fn: Converter) -> None:
    """登记一个可信转换器（内置迁移与测试共用这一个入口）。"""
    key = (normalize_format(source), normalize_format(target))
    _REGISTRY[key] = fn
    _DISPLAY.setdefault(key, (str(source), str(target)))


def unregister_converter(source: str, target: str) -> None:
    key = (normalize_format(source), normalize_format(target))
    _REGISTRY.pop(key, None)
    _DISPLAY.pop(key, None)


def converters() -> list[tuple[str, str]]:
    """已注册的转换器清单（管理面展示与兼容判定共用同一份表）。"""
    return sorted(_DISPLAY.get(key, key) for key in _REGISTRY)


def converter_for(source: str, target: str) -> Converter | None:
    return _REGISTRY.get((normalize_format(source), normalize_format(target)))


def can_convert(source: str, target: str) -> bool:
    return converter_for(source, target) is not None


def convert_payload(payload: Payload, *, source: str, target: str) -> Payload:
    """跑一次转换；没有登记就不猜（宁可拒绝，也不「尽量加载」）。"""
    fn = converter_for(source, target)
    if fn is None:
        raise ConverterError(f"没有 {source} → {target} 的可信转换器")
    out = fn(json.loads(json.dumps(payload)))  # 深拷贝：转换失败不动原件
    if not isinstance(out, dict):
        raise ConverterError("转换器必须返回对象")
    return out
