"""Low-coupling external TRPG rule-plugin bridge.

The plugin owns rules and dice. Core only exchanges a bounded JSON request and
validates the returned world effects before applying them.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class RulePluginError(ValueError):
    """The external rule plugin did not produce a usable result."""


async def resolve(manifest_path: str | Path, request: dict[str, Any], *, timeout: float = 30.0) -> dict[str, Any]:
    path = Path(manifest_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RulePluginError(f"规则插件清单不可读：{path}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entry"), list):
        raise RulePluginError("规则插件清单缺少 entry 参数数组")
    if manifest.get("protocol") != "isekai.trpg.rules/1":
        raise RulePluginError("规则插件协议版本不受支持")
    entry = [str(part) for part in manifest["entry"] if str(part)]
    if not entry:
        raise RulePluginError("规则插件入口为空")
    proc: asyncio.subprocess.Process | None = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *entry,
            cwd=str(path.parent),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write((json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        raw = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except (OSError, asyncio.TimeoutError) as exc:
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise RulePluginError(f"规则插件调用失败：{type(exc).__name__}") from exc
    if proc.returncode != 0:
        raise RulePluginError(f"规则插件退出：{proc.returncode}")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RulePluginError("规则插件返回的第一行不是合法 JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("resolution"), dict):
        raise RulePluginError("规则插件结果必须包含 resolution 对象")
    # 世界后果清单：B0 resolver 用 `effects`，战役裁定器用 `consequences`——
    # 两者都要认，否则新协议一上线就被拦在插件边界（TRPG_RULE_PLUGIN_SPEC §响应）
    if not isinstance(result.get("effects"), list) and not isinstance(result.get("consequences"), list):
        raise RulePluginError("规则插件结果必须包含 effects 或 consequences 数组")
    if not isinstance(result.get("claims", []), list):
        raise RulePluginError("规则插件结果的 claims 必须是数组")
    return result
