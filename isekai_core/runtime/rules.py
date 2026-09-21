"""Low-coupling external TRPG rule-plugin bridge.

The plugin owns rules and dice. Core only exchanges a bounded JSON request and
validates the returned world effects before applying them.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any


class RulePluginError(ValueError):
    """The external rule plugin did not produce a usable result."""


def manifest_identity(manifest_path: str | Path) -> dict[str, Any]:
    """读规则清单的标识（§十六 兼容性比对用）。

    `ruleset_version` 只在 opaque_state 的格式变化时才该变；清单没声明就退回插件版本。
    清单读不动 / 缺字段一律返回空字典：比对失败不该让裁定先炸。
    """
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(manifest, dict):
        return {}
    return {
        "ruleset_id": str(manifest.get("ruleset_id") or manifest.get("id") or ""),
        "ruleset_version": str(manifest.get("ruleset_version") or manifest.get("version") or ""),
    }


def load_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """读规则插件清单：不可读 / 结构不对一律报错，不返回半个清单。"""
    path = Path(manifest_path)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RulePluginError(f"规则插件清单不可读：{path}") from exc
    if not isinstance(manifest, dict) or not isinstance(manifest.get("entry"), list):
        raise RulePluginError("规则插件清单缺少 entry 参数数组")
    if manifest.get("protocol") != "isekai.trpg.rules/1":
        raise RulePluginError("规则插件协议版本不受支持")
    if not [str(part) for part in manifest["entry"] if str(part)]:
        raise RulePluginError("规则插件入口为空")
    return manifest


def converters_of(manifest_path: str | Path) -> list[dict[str, Any]]:
    """清单里声明的状态转换器（§十六）：坏清单 / 没声明 → 空表，调用方自己决定怎么办。"""
    try:
        manifest = load_manifest(manifest_path)
    except RulePluginError:
        return []
    items = manifest.get("converters")
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict) and str(item.get("converter_id") or "")]


def pick_converter(
    converters: list[dict[str, Any]], *, converter_id: str = "", from_version: str = "", to_version: str = ""
) -> dict[str, Any] | None:
    """挑转换器：给了 id 就按 id，否则要 from→to 唯一命中——两个都能干同一个活时不许瞎猜。"""
    if converter_id:
        for item in converters:
            if str(item.get("converter_id")) == converter_id:
                return item
        return None
    hits = [
        item for item in converters
        if str(item.get("from_version") or "") == from_version and str(item.get("to_version") or "") == to_version
    ]
    return hits[0] if len(hits) == 1 else None


async def convert(
    manifest_path: str | Path, converter: dict[str, Any], request: dict[str, Any], *, timeout: float = 60.0
) -> dict[str, Any]:
    """调转换器（§十六）：核心只搬运与记账，不猜旧状态里每个字段该变成什么。"""
    manifest = load_manifest(manifest_path)
    entry = converter.get("entry") or manifest["entry"]
    if not isinstance(entry, list) or not [str(part) for part in entry if str(part)]:
        raise RulePluginError("转换器没有可用的 entry")
    result = await _invoke(Path(manifest_path), [str(part) for part in entry if str(part)], request, timeout=timeout)
    state = result.get("opaque_state")
    if not isinstance(state, dict):
        raise RulePluginError("转换器必须返回 opaque_state 对象")
    losses = result.get("losses") or []
    if not isinstance(losses, list):
        raise RulePluginError("转换器返回的 losses 必须是数组")
    return {
        "opaque_state": state,
        "losses": [str(item) for item in losses],
        "notes": str(result.get("notes") or ""),
    }


class RulePluginSession:
    """常驻规则插件会话（§五 进程边界）：一个插件进程活过多次裁定。

    常驻省掉的只是**进程启动与模块加载**。状态仍然只能经快照进出——插件不许把状态
    藏在进程内存里，否则回滚 / 分叉会带着不该有的记忆（状态生命周期不因常驻而改变）。

    协议：一行一个 JSON 请求 / 响应；`{"type": "ping"}` 必须回 `{"type": "pong"}`；
    读到 stdin EOF 必须自己退出（核心被杀时不会有人来回收它）。
    """

    def __init__(self, manifest_path: Path, entry: list[str], *, timeout: float = 60.0) -> None:
        self.manifest_path = manifest_path
        self.entry = entry
        self.timeout = float(timeout)
        self.spawns = 0
        self.last_used_real = 0.0
        self._proc: asyncio.subprocess.Process | None = None

    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def _spawn(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self.entry,
            cwd=str(self.manifest_path.parent),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.spawns += 1

    async def request(self, payload: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        """发一条取一条。发之前先确认进程还活着——**死在半路的请求不重发**（裁定不重跑）。

        崩溃恢复的边界就在这里：进程在「我们还没写请求」时就已经死了 → 重开一个再发；
        请求发出去之后进程死了 → 报错，由调用方按 plugin_failed 处理（§12.2 不猜结果）。
        """
        wait = float(timeout or self.timeout)
        if not self.alive():
            await self.close()
            await self._spawn()
        proc = self._proc
        assert proc is not None and proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
            await proc.stdin.drain()
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=wait)
        except (OSError, asyncio.TimeoutError) as exc:
            await self.close()
            raise RulePluginError(f"常驻规则插件调用失败：{type(exc).__name__}") from exc
        self.last_used_real = time.time()
        if not raw:
            tail = await self._stderr_tail()
            await self.close()
            raise RulePluginError(f"常驻规则插件没有回应（进程已退出）{tail}")
        try:
            result = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RulePluginError("常驻规则插件返回的第一行不是合法 JSON") from exc
        if not isinstance(result, dict):
            raise RulePluginError("常驻规则插件返回的不是 JSON 对象")
        return result

    async def ping(self, *, timeout: float = 5.0) -> bool:
        """心跳：活着且答得上 pong（顺序协议下这就是唯一安全的探活方式）。"""
        try:
            answer = await self.request(
                {"type": "ping", "protocol": "isekai.trpg.rules/1"}, timeout=min(self.timeout, timeout)
            )
        except RulePluginError:
            return False
        return str(answer.get("type") or "") == "pong"

    async def _stderr_tail(self, limit: int = 400) -> str:
        """进程已经没了的时候，把它 stderr 的最后几行带出来——不然只剩一句「没有回应」（§12 排查）。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return ""
        try:
            raw = await asyncio.wait_for(proc.stderr.read(), timeout=1.0)
        except (OSError, asyncio.TimeoutError):
            return ""
        text = raw.decode("utf-8", "replace").strip()
        return f"：{text[-limit:]}" if text else ""

    async def close(self) -> None:
        """关掉：先关 stdin 等它自己退（协议要求），超时才杀。"""
        proc, self._proc = self._proc, None
        if proc is None or proc.returncode is not None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
            await asyncio.wait_for(proc.wait(), timeout=3.0)
        except (OSError, asyncio.TimeoutError):
            proc.kill()
            await proc.wait()


async def resolve(
    manifest_path: str | Path,
    request: dict[str, Any],
    *,
    timeout: float = 30.0,
    session: "RulePluginSession | None" = None,
) -> dict[str, Any]:
    """一次裁定：给了常驻会话就走会话，否则一次调用一个进程。"""
    if session is not None:
        result = await session.request(request, timeout=timeout)
        return _check_resolution(result)
    manifest = load_manifest(manifest_path)
    entry = [str(part) for part in manifest["entry"] if str(part)]
    result = await _invoke(Path(manifest_path), entry, request, timeout=timeout)
    return _check_resolution(result)


def _check_resolution(result: dict[str, Any]) -> dict[str, Any]:
    """裁定响应的共同校验：常驻与一次性两条路都必须过（别让常驻绕过边界）。"""
    if not isinstance(result, dict) or not isinstance(result.get("resolution"), dict):
        raise RulePluginError("规则插件结果必须包含 resolution 对象")
    # 世界后果清单：B0 resolver 用 `effects`，战役裁定器用 `consequences`——
    # 两者都要认，否则新协议一上线就被拦在插件边界（TRPG_RULE_PLUGIN_SPEC §响应）
    if not isinstance(result.get("effects"), list) and not isinstance(result.get("consequences"), list):
        raise RulePluginError("规则插件结果必须包含 effects 或 consequences 数组")
    if not isinstance(result.get("claims", []), list):
        raise RulePluginError("规则插件结果的 claims 必须是数组")
    return result


async def _invoke(
    path: Path, entry: list[str], request: dict[str, Any], *, timeout: float
) -> dict[str, Any]:
    """跑一次插件进程并取回首行 JSON（§五 进程边界）：裁定与转换共用这一条。"""
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
    if not isinstance(result, dict):
        raise RulePluginError("规则插件返回的第一行不是 JSON 对象")
    return result
