"""通道插件参考实现（CHANNEL_PLUGIN_SPEC §3.3「Python 参考实现」）。

它把「一个第三方通道该怎么活」写到最小可读：

1. **启动姿势**：独立进程，清单 `manif‑est.json` 声明 `entry`；认证材料由核心经环境变量交给它
   （`ISEKAI_PLUGIN_ID` / `ISEKAI_PLUGIN_CREDENTIAL`），**不要**把凭据写进 URL、日志或清单。
2. **握手**：首帧必须是 `hello`（`ump` 版本前缀、通道 id/name/version、capabilities、auth）。
   核心回 `hello_ack`（含协商后的 `negotiated` 限额与已有 `threads`）；不兼容则回 `error` 且不再收普通消息。
3. **收发与回执**：核心投递 `message` 帧 → 必须用 `delivery` 帧回执（`accepted|failed|unknown`）；
   发送用户输入用 `user_message`（带 `thread.id` 与 `binding_token`）；重发同一 `id` 幂等，改正文复用 id 会报冲突。
4. **错误与重试**：`error` 帧带 `code / retryable / ref`；`retryable=false` 的（认证、协议、超限）**别重连风暴**，
   退避只用于 `retryable=true` 或连接断开；`unknown` 回执表示结果未知，重试不会重跑模型。
5. **退出**：核心停用 / 退出时会关管道；收到 EOF 就干净退出，别留子进程（核心会 `taskkill /T` 兜底）。

本文件可直接当模板改；`--selfcheck` 会自检信封形状，不连核心。
"""

from __future__ import annotations

import json
import os
import sys
import time

UMP_VERSION = "1.0"
BACKOFF_START, BACKOFF_MAX = 0.5, 8.0


def envelope(env_type: str, payload: dict, *, env_id: str | None = None) -> dict:
    """按 UMP 造一个信封（id 自己发号，重发同一逻辑请求要沿用同一个 id）。"""
    return {
        "ump": UMP_VERSION,
        "type": env_type,
        "id": env_id or f"{env_type}-{int(time.time() * 1000)}",
        "ts": time.time(),
        "payload": payload,
    }


def hello(channel_id: str, credential: str, *, name: str = "", caps: dict | None = None) -> dict:
    return envelope(
        "hello",
        {
            "channel": {"id": channel_id, "name": name or channel_id, "version": "0.1.0"},
            "capabilities": {"segments": True, "status": True, **(caps or {})},
            "auth": {"credential": credential},
        },
    )


def delivery(message_id: str, *, state: str = "accepted", index: int = 0) -> dict:
    return envelope("delivery", {"message_id": message_id, "batch_index": index, "state": state})


def user_message(text: str, *, thread_id: str, binding_token: str, env_id: str | None = None) -> dict:
    frame = envelope("user_message", {"text": text}, env_id=env_id)
    frame["thread"] = {"id": thread_id, "binding_token": binding_token}
    return frame


def main() -> int:
    if "--selfcheck" in sys.argv:
        return selfcheck()
    channel_id = os.environ.get("ISEKAI_PLUGIN_ID")
    credential = os.environ.get("ISEKAI_PLUGIN_CREDENTIAL")
    if not channel_id or not credential:
        # 缺凭据就别装作连上了：明确退出，由核心把「启用失败」报到界面
        sys.stderr.write("缺少 ISEKAI_PLUGIN_ID / ISEKAI_PLUGIN_CREDENTIAL，退出\n")
        return 2
    send(hello(channel_id, credential, name=os.environ.get("ISEKAI_PLUGIN_NAME", channel_id)))
    backoff = BACKOFF_START
    for line in sys.stdin:  # 核心 → 插件：NDJSON，一行一帧
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except json.JSONDecodeError:
            sys.stderr.write("收到不是 JSON 的一行，忽略\n")
            continue
        kind = str(frame.get("type") or "")
        payload = frame.get("payload") if isinstance(frame.get("payload"), dict) else {}
        if kind == "hello_ack":
            backoff = BACKOFF_START  # 握手成功，退避清零
            threads = payload.get("threads") or []
            if threads:
                sys.stderr.write(f"已有 thread 绑定：{[item.get('id') for item in threads]}\n")
            continue
        if kind == "message":
            # 这里才是「把核心的回复送去外部平台」的地方；无论成败都要回执，别假装成功
            ok = True  # 换成真实发送；失败置 False 并按需回 failed / unknown
            send(delivery(str(payload.get("message_id") or ""), state="accepted" if ok else "unknown",
                          index=int(payload.get("batch_index") or 0)))
            continue
        if kind == "error":
            code = str(payload.get("code") or "")
            sys.stderr.write(f"核心报错：{code} / {payload.get('message')}\n")
            if not payload.get("retryable", False):
                return 1  # 永久错误不重连风暴
            time.sleep(backoff)
            backoff = min(BACKOFF_MAX, backoff * 2)
            continue
        if kind == "ping":
            send(envelope("pong", {}))
            continue
    return 0  # EOF：核心关管道，干净退出


def send(frame: dict) -> None:
    """一帧一行，UTF-8，不缓冲（核心按行读）。"""
    sys.stdout.write(json.dumps(frame, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def selfcheck() -> int:
    """不连核心的自检：信封形状与关键字段对不对（改模板时先跑这个）。"""
    h = hello("demo", "cr-demo", name="示例通道")
    assert h["ump"].startswith("1.") and h["type"] == "hello"
    assert h["payload"]["auth"]["credential"] == "cr-demo"
    assert h["payload"]["channel"]["id"] == "demo"
    d = delivery("m-1")
    assert d["payload"]["state"] == "accepted" and d["payload"]["batch_index"] == 0
    u = user_message("在吗", thread_id="dm-1", binding_token="tok-1")
    assert u["thread"] == {"id": "dm-1", "binding_token": "tok-1"} and u["payload"]["text"] == "在吗"
    assert not any("://" in json.dumps(item, ensure_ascii=False) for item in (h, u)), "凭据不进 URL"
    print("selfcheck ok：信封形状、认字段、线程绑定、凭据不放 URL")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
