"""常驻规则插件的**中继桥**（TRPG §二十一 残余第 3 条：跨核心复用）。

问题：常驻插件原来是核心的子进程、说 stdin/stdout 的 NDJSON——核心一重启，插件就跟着断了，
新核心只能重开一个（丢掉已经加载好的权重 / 索引）。

解法：核心不再直接拿 stdio，而是先连**本机回环 TCP**；桥负责：
1. 起真插件（还是 stdio NDJSON，**插件代码一个字都不用改**）；
2. 在 127.0.0.1 上监听，把「一个客户端 ↔ 真插件」的整行 JSON 双向搬；
3. 把端口与令牌写进共享文件，让**下一个核心**能照着连上来（这就是跨核心复用）；
4. 没人连、静置超过 `ISEKAI_PLUGIN_IDLE_EXIT` 秒就自退（不留孤儿）；真插件死了也收摊。

契约（`isekai.trpg.rules.share/1`）：共享文件 = `{"version": 1, "port": int, "token": str, "pid": int}`；
客户端连上后照旧一行一个 JSON，`{"type":"ping"}` → `{"type":"pong"}`——线协议不变，只换了承载。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

SHARE_VERSION = 1
DEFAULT_IDLE_EXIT_S = 600.0


def _idle_exit_seconds() -> float:
    raw = os.environ.get("ISEKAI_PLUGIN_IDLE_EXIT", "").strip()
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_IDLE_EXIT_S
    return value if value > 0 else DEFAULT_IDLE_EXIT_S


async def _pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """把 reader 的字节原样灌进 writer；任一端结束就收工。"""
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (OSError, asyncio.IncompleteReadError):  # 对端没了：正常收摊，不是错误
        pass
    finally:
        try:
            writer.close()
        except OSError:
            pass


async def serve(share_file: Path, token: str, entry: list[str]) -> int:
    """起真插件 + 监听回环；返回进程退出码（真插件的退出码，或自退的 0）。"""
    child = await asyncio.create_subprocess_exec(
        *entry,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=None,  # 真插件的 stderr 直通桥的 stderr（核心侧看不到，但用户终端能看到）
    )
    assert child.stdin is not None and child.stdout is not None

    client: dict[str, Any] = {"writer": None, "last": asyncio.get_running_loop().time()}

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        previous = client["writer"]
        if previous is not None:  # 一次只服务一个客户端：新的顶掉旧的（老核心没退干净时不至于卡死）
            try:
                previous.close()
            except OSError:
                pass
        client["writer"] = writer
        client["last"] = asyncio.get_running_loop().time()
        await _pump(reader, _child_stdin_writer(child))
        client["writer"] = None

    server = await asyncio.start_server(handle, host="127.0.0.1", port=0)
    port = int(server.sockets[0].getsockname()[1])
    share_file.parent.mkdir(parents=True, exist_ok=True)
    share_file.write_text(
        json.dumps({"version": SHARE_VERSION, "port": port, "token": token, "pid": child.pid}),
        encoding="utf-8",
    )

    idle = _idle_exit_seconds()
    child_out = asyncio.create_task(_pump(child.stdout, _ClientFanout(client)))
    idle_task = asyncio.create_task(_idle_watch(client, idle))
    child_task = asyncio.create_task(child.wait())
    done, pending = await asyncio.wait({child_task, idle_task}, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    child_out.cancel()
    if client["writer"] is not None:
        try:
            client["writer"].close()
        except OSError:
            pass
    server.close()
    if child_task in done and child.returncode is None:
        child.terminate()
        await child.wait()
    if child_task not in done and child.returncode is None:  # 静置自退：把真插件也带走
        child.terminate()
        await child.wait()
    try:
        share_file.unlink()
    except OSError:
        pass
    return int(child.returncode or 0)


class _ClientFanout:
    """把真插件的 stdout 字节写到当前客户端（没有客户端就攒着——插件只在被问时才说话）。

    `_pump` 要的是「同步 write + 异步 drain」的形状，所以这里先攒、drain 时再发
    （第一版把 write 写成 async，`_pump` 调它时建了个没人 await 的协程，回应全丢）。
    """

    def __init__(self, client: dict[str, Any]) -> None:
        self._client = client
        self._buffer = b""

    def write(self, chunk: bytes) -> None:
        self._buffer += chunk

    async def drain(self) -> None:
        writer = self._client["writer"]
        if writer is None or not self._buffer:
            self._buffer = b""
            return
        writer.write(self._buffer)
        self._buffer = b""
        try:
            await writer.drain()
        except OSError:
            self._client["writer"] = None

    def close(self) -> None:
        return None


def _child_stdin_writer(child: asyncio.subprocess.Process) -> asyncio.StreamWriter:
    """客户端字节 → 真插件 stdin。包一层满足 `_pump` 的 write/drain/close 形状。"""
    assert child.stdin is not None
    return _StdInWriter(child.stdin)


class _StdInWriter:
    def __init__(self, stream: asyncio.StreamWriter) -> None:
        self._stream = stream

    def write(self, chunk: bytes) -> None:
        self._stream.write(chunk)

    async def drain(self) -> None:
        await self._stream.drain()

    def close(self) -> None:
        # 客户端断开**不关**真插件的 stdin：插件要活过这个核心，下个核心还会连上来
        return None


async def _idle_watch(client: dict[str, Any], idle: float) -> None:
    loop = asyncio.get_running_loop()
    while True:
        await asyncio.sleep(min(5.0, idle))
        if client["writer"] is None and loop.time() - float(client["last"]) >= idle:
            return


def main(argv: list[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if "--" not in args:
        print("用法：plugin_bridge.py <share_file> <token> -- <插件入口…>", file=sys.stderr)
        return 2
    cut = args.index("--")
    share_file, token, entry = args[0], args[1], args[cut + 1 :]
    if not entry:
        print("缺少插件入口", file=sys.stderr)
        return 2
    return asyncio.run(serve(Path(share_file), token, entry))


if __name__ == "__main__":  # pragma: no cover - 由子进程执行
    raise SystemExit(main())
