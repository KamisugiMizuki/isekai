"""开发用 UMP 聊天客户端（CLI 先行）。

默认自行拉起核心进程（与桌面壳同一条受信启动通路：读 stdout 的就绪握手取端点与凭据），
也可用 `--endpoint/--token/--mgmt` 连接已在运行的核心。

示例：
    python -m isekai_core.cli                      # 交互聊天
    python -m isekai_core.cli --say "你好"          # 单轮，打印回复后退出
    python -m isekai_core.cli --endpoint ws://127.0.0.1:PORT --token bs-... --mgmt mg-...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import ump
from .client import MgmtClient, UmpClient
from .config import Config, load_config
from .ump import Envelope, UmpError
from .version import APP_VERSION


def _credentials_path(cfg: Config, channel_id: str) -> Path:
    return cfg.paths.clients / f"{channel_id}.json"


def _load_credential(cfg: Config, channel_id: str) -> str | None:
    path = _credentials_path(cfg, channel_id)
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("credential")
    except (OSError, json.JSONDecodeError):
        return None


def _save_credential(cfg: Config, channel_id: str, credential: str) -> None:
    path = _credentials_path(cfg, channel_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"credential": credential}, ensure_ascii=False), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def spawn_core(root: str | None) -> tuple[subprocess.Popen[bytes], dict[str, Any]]:
    """拉起核心并等待就绪握手（超时给启动阶段诊断）。"""
    cmd = [sys.executable, "-m", "isekai_core"]
    if root:
        cmd += ["--root", root]
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        cwd=str(Path(__file__).resolve().parent.parent),
        env=env,
    )
    assert proc.stdout is not None
    line = proc.stdout.readline()
    if not line:
        raise SystemExit("核心进程启动失败：未收到就绪握手（看 logs/core.log）")
    ready = json.loads(line.decode("utf-8"))
    if ready.get("event") != "ready":
        proc.terminate()
        raise SystemExit(f"核心未就绪：{ready}")
    return proc, ready


async def ensure_binding(cfg: Config, mgmt: MgmtClient, channel_id: str, thread_id: str) -> dict[str, Any]:
    """取（或建立）占位会话与 thread 绑定；阶段 0 由开发工具代管理面完成。"""
    sessions = (await mgmt.call("session.list")).get("sessions") or []
    placeholder = cfg.placeholder
    session = next(
        (
            s
            for s in sessions
            if s["instance_id"] == placeholder["instance_id"]
            and s["timeline_id"] == placeholder["timeline_id"]
            and s["character_id"] == placeholder["character_id"]
        ),
        None,
    )
    if session is None:
        session = (await mgmt.call("session.ensure", **placeholder))["session"]
    threads = (await mgmt.call("thread.list")).get("threads") or []
    thread = next(
        (t for t in threads if t["channel_id"] and t["thread_id"] == thread_id and t["session_id"] == session["id"]),
        None,
    )
    if thread is None:
        thread = (await mgmt.call("thread.bind", channel=channel_id, thread_id=thread_id, session_id=session["id"]))["thread"]
    return {"session": session, "thread": thread}


async def connect_channel(
    cfg: Config,
    endpoint: str,
    *,
    channel_id: str,
    name: str,
    bootstrap: str | None = None,
    credential: str | None = None,
) -> UmpClient:
    """建立通道连接：优先持久凭据，失败时退回一次性引导凭据重新登记。"""
    if credential is None:
        credential = _load_credential(cfg, channel_id)
    client = UmpClient(
        endpoint=endpoint,
        channel_id=channel_id,
        name=name,
        version=APP_VERSION,
        credential=credential,
        bootstrap=bootstrap,
    )
    try:
        ack = await client.connect()
    except UmpError:
        if not credential or not bootstrap:
            raise
        await client.close()
        client = UmpClient(endpoint=endpoint, channel_id=channel_id, name=name, bootstrap=bootstrap)
        ack = await client.connect()
    if ack.get("credential"):
        _save_credential(cfg, channel_id, ack["credential"])
    return client


async def run_turn(
    client: UmpClient,
    *,
    thread_id: str,
    token: str,
    text: str,
    timeout: float = 180.0,
    quiet: bool = False,
) -> str:
    """发送一条消息并等待最终回复；打印状态与分段。返回回复全文。"""
    env_id = await client.send_user_message(thread_id=thread_id, binding_token=token, text=text)
    parts: list[str] = []
    expected_batches = 1
    while True:
        envelope = await client.expect(lambda _e: True, timeout=timeout)
        if envelope.type == "status":
            if not quiet:
                if envelope.payload.get("state") == "thinking":
                    print("· 思考中…")
            continue
        if envelope.type == "error":
            if envelope.payload.get("ref") in (env_id, None):
                raise UmpError(
                    envelope.payload.get("code", ump.Err.GENERATION_FAILED),
                    envelope.payload.get("message", "生成失败"),
                    retryable=bool(envelope.payload.get("retryable")),
                    ref=env_id,
                )
            continue
        if envelope.type != "reply" or envelope.payload.get("reply_to") != env_id:
            continue
        payload = envelope.payload
        expected_batches = payload.get("batch_count", 1)
        texts = [part["text"] for part in payload.get("parts", [])]
        parts.extend(texts)
        if not quiet:
            for text_part in texts:
                print(f"角色> {text_part}")
        await client.report_delivery(
            thread_id=thread_id,
            binding_token=token,
            message_id=payload["message_id"],
            batch_index=payload.get("batch_index", 0),
            state="accepted",
        )
        if payload.get("batch_index", 0) >= expected_batches - 1:
            break
    return "\n".join(parts)


async def chat_loop(client: UmpClient, *, thread_id: str, token: str) -> None:
    print("已连接。输入内容回车发送；/quit 退出，/retry <ref> 重试生成，/history 查看最近记录。")
    history_client: MgmtClient | None = None
    while True:
        try:
            line = (await asyncio.to_thread(input, "你> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue
        if line in ("/quit", "/exit"):
            return
        if line.startswith("/retry "):
            ref = line.split(" ", 1)[1].strip()
            await client.request_retry(thread_id=thread_id, binding_token=token, ref=ref, kind="input")
            print(f"· 已请求重试 {ref}")
            continue
        if line == "/history" and history_client is not None:
            result = await history_client.call("history.page", session_id=history_client.info.get("session_id", ""), limit=20)
            for row in result.get("messages", []):
                who = {"user": "你", "character": "角色", "notice": "系统"}.get(row["role"], row["role"])
                body = row["text"] or " / ".join(
                    part for batch in (row.get("parts") or []) for part in batch
                )
                print(f"  [{row['seq']}] {who}: {body}")
            continue
        try:
            await run_turn(client, thread_id=thread_id, token=token, text=line)
        except (UmpError, TimeoutError) as exc:
            print(f"! 本轮失败：{exc}")


async def amain(args: argparse.Namespace, cfg: Config) -> int:
    proc: subprocess.Popen[bytes] | None = None
    ready: dict[str, Any] = {}
    if args.endpoint:
        endpoint, bootstrap, mgmt_token = args.endpoint, args.token, args.mgmt
    else:
        proc, ready = spawn_core(args.root)
        endpoint = ready["endpoint"]
        bootstrap = ready["bootstrap"]
        mgmt_token = ready["mgmt"]
        print(f"· 核心已就绪 {endpoint}（pid={ready.get('pid')}）")

    client: UmpClient | None = None
    mgmt: MgmtClient | None = None
    try:
        if mgmt_token:
            mgmt = MgmtClient(endpoint, mgmt_token)
            info = await mgmt.connect()
            issued = await mgmt.call("channel.ensure", name=args.channel_id, version=APP_VERSION)
            _save_credential(cfg, args.channel_id, issued["credential"])
            binding = await ensure_binding(cfg, mgmt, args.channel_id, args.thread)
            session, thread = binding["session"], binding["thread"]
            client = await connect_channel(
                cfg,
                endpoint,
                channel_id=args.channel_id,
                name=args.name,
                bootstrap=bootstrap,
                credential=issued["credential"],
            )
            print(
                f"· 会话 {session['instance_id']}/{session['timeline_id']}/{session['character_id']}"
                f"  绑定版本 {thread['binding_version']}"
            )
        else:
            print("· 未提供管理凭据：只能使用既有绑定（--thread 对应的 binding_token 需自备）")
            client = await connect_channel(
                cfg, endpoint, channel_id=args.channel_id, name=args.name, bootstrap=bootstrap
            )
            thread = {"binding_token": args.binding_token}
            if not args.binding_token:
                raise SystemExit("缺少 --binding-token：无管理凭据时无法查询绑定")

        if args.say:
            reply = await run_turn(client, thread_id=args.thread, token=thread["binding_token"], text=args.say)
            if args.quiet:
                print(reply)
            return 0
        await chat_loop(client, thread_id=args.thread, token=thread["binding_token"])
        return 0
    finally:
        if client is not None:
            await client.close()
        if mgmt is not None:
            await mgmt.close()
        if proc is not None and not args.keep_core:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="isekai_chat", description="isekai 开发用 UMP 聊天客户端")
    parser.add_argument("--root", default=None, help="数据根目录")
    parser.add_argument("--endpoint", default=None, help="连接已有核心（默认自行拉起）")
    parser.add_argument("--token", default=None, help="一次性引导凭据（连接已有核心时）")
    parser.add_argument("--mgmt", default=None, help="管理面凭据（连接已有核心时）")
    parser.add_argument("--binding-token", default=None, help="无管理凭据时直接给出绑定令牌")
    parser.add_argument("--channel-id", default="cli-dev")
    parser.add_argument("--name", default="开发 CLI")
    parser.add_argument("--thread", default="dm-cli")
    parser.add_argument("--say", default=None, help="单轮模式：发送该文本并打印回复")
    parser.add_argument("--quiet", action="store_true", help="单轮模式只打印回复正文")
    parser.add_argument("--keep-core", action="store_true", help="退出后保留核心进程")
    args = parser.parse_args(argv)

    cfg = load_config(args.root)
    started = time.time()
    try:
        return asyncio.run(amain(args, cfg))
    finally:
        print(f"· 会话结束（{time.time() - started:.1f}s）")


if __name__ == "__main__":
    sys.exit(main())
