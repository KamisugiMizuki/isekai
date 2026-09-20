"""统一消息协议（UMP）v1：信封解析、校验与构造。

只承载消息流：不出现世界实例、时间线、角色内部标识，不传提示词、中间推理、
记忆简报或实情（CHANNEL_PLUGIN_SPEC §一）。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .version import DEFAULT_MAX_PARTS, DEFAULT_MAX_TEXT_LEN, UMP_MAJOR, UMP_VERSION


class Err:
    """错误码（有限集合，错误信封 code 取值）。"""

    PROTOCOL = "protocol_error"
    BAD_FRAME = "bad_frame"
    UNSUPPORTED_TYPE = "unsupported_type"
    AUTH_REQUIRED = "auth_required"
    AUTH_FAILED = "auth_failed"
    INVALID = "invalid_input"
    UNKNOWN_THREAD = "unknown_thread"
    BINDING_EXPIRED = "binding_expired"
    CONFLICT = "conflict"
    VOIDED = "voided"
    NOT_FOUND = "not_found"
    UNSUPPORTED_CAPABILITY = "unsupported_capability"
    STATE_BLOCKED = "state_blocked"
    GENERATION_FAILED = "generation_failed"
    LLM_NOT_CONFIGURED = "llm_not_configured"
    INTERNAL = "internal"


CLIENT_TYPES = frozenset({"hello", "user_message", "delivery", "retry", "ping", "pong"})
SERVER_TYPES = frozenset(
    {"hello_ack", "binding", "accepted", "reply", "system_notice", "status", "error", "ping", "pong"}
)
THREAD_REQUIRED = frozenset(
    {"user_message", "accepted", "reply", "system_notice", "delivery", "retry", "status", "binding"}
)
#: 必须回传绑定令牌的类型（消息 / 回执 / 重试）
TOKEN_REQUIRED = frozenset({"user_message", "retry", "delivery"})
ALL_TYPES = CLIENT_TYPES | SERVER_TYPES

DELIVERY_STATES = frozenset({"accepted", "failed", "unknown"})
ACCEPT_STATES = frozenset({"queued", "processing", "done", "failed", "cancelled"})
CORE_STATES = frozenset({"ready", "catching_up", "compatibility_blocked", "persistence_blocked", "failed"})


class Stage:
    """错误所属阶段（CHANNEL_PLUGIN_SPEC §六：错误须明确属于接收 / 生成 / 投递阶段）。"""

    RECEIVE = "receive"
    GENERATE = "generate"
    DELIVERY = "delivery"
    PROTOCOL = "protocol"
    AUTH = "auth"

    ALL = frozenset({RECEIVE, GENERATE, DELIVERY, PROTOCOL, AUTH})


class UmpError(Exception):
    """协议 / 业务错误，可直接序列化为 error 信封。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        ref: str | None = None,
        close: bool = False,
        stage: str = Stage.PROTOCOL,
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = retryable
        self.ref = ref
        self.close = close
        self.stage = stage

    def to_payload(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "ref": self.ref,
            "stage": self.stage,
        }


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass
class Envelope:
    type: str
    id: str
    ts: float
    payload: dict[str, Any] = field(default_factory=dict)
    thread_id: str | None = None
    binding_token: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def thread(self) -> dict[str, str] | None:
        if self.thread_id is None:
            return None
        t = {"id": self.thread_id}
        if self.binding_token is not None:
            t["binding_token"] = self.binding_token
        return t


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _require_str(obj: dict[str, Any], key: str, *, max_len: int = 200, allow_empty: bool = False) -> str:
    value = obj.get(key)
    if not isinstance(value, str):
        raise UmpError(Err.PROTOCOL, f"field '{key}' must be a string")
    if len(value) > max_len:
        raise UmpError(Err.PROTOCOL, f"field '{key}' too long")
    if not allow_empty and not value:
        raise UmpError(Err.PROTOCOL, f"field '{key}' must not be empty")
    return value


def _check_limit(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise UmpError(Err.PROTOCOL, f"limit '{name}' must be a positive integer")
    return value


def parse_hello(payload: dict[str, Any]) -> dict[str, Any]:
    """校验并归一化 hello 载荷。"""
    channel = payload.get("channel")
    if not isinstance(channel, dict):
        raise UmpError(Err.PROTOCOL, "hello.channel must be an object")
    caps = payload.get("capabilities") or {}
    if not isinstance(caps, dict):
        raise UmpError(Err.PROTOCOL, "hello.capabilities must be an object")
    auth = payload.get("auth")
    if not isinstance(auth, dict):
        raise UmpError(Err.AUTH_REQUIRED, "hello.auth required", retryable=False)
    bootstrap = auth.get("bootstrap")
    credential = auth.get("credential")
    if not isinstance(bootstrap, str) and not isinstance(credential, str):
        raise UmpError(Err.AUTH_REQUIRED, "hello.auth needs bootstrap or credential")
    limits = {
        "max_text_len": _check_limit(caps.get("max_text_len", DEFAULT_MAX_TEXT_LEN), "max_text_len"),
        "max_parts": _check_limit(caps.get("max_parts", DEFAULT_MAX_PARTS), "max_parts"),
    }
    return {
        "channel": {
            "id": _require_str(channel, "id", max_len=64),
            "name": str(channel.get("name") or channel.get("id")),
            "version": str(channel.get("version") or "0"),
        },
        "capabilities": {
            "segments": bool(caps.get("segments", False)),
            "status": bool(caps.get("status", False)),
            # v1 基线：只文本、非流式（CHANNEL_PLUGIN_SPEC §2.1）。附件 / 富媒体 / 流式是**更后置**的
            # 扩展点：这里显式声明不支持，收到相关字段一律明确拒绝，不静默忽略。
            "text": True,
            "attachments": False,
            "stream": False,
            **limits,
        },
        "bootstrap": bootstrap if isinstance(bootstrap, str) else None,
        "credential": credential if isinstance(credential, str) else None,
    }


#: v1 只承担文本；这些字段出现即明确拒绝（扩展这些能力时再在此开闸，不提前铺设）
_EXTENSION_FIELDS = ("attachments", "attachment", "media", "stream", "stream_id")


def _reject_unsupported_extensions(payload: dict[str, Any]) -> None:
    for field in _EXTENSION_FIELDS:
        if payload.get(field) not in (None, [], {}, ""):
            raise UmpError(
                Err.UNSUPPORTED_CAPABILITY,
                f"v1 只承担文本：{field} 属更后置的附件 / 流式扩展（不静默忽略）",
                retryable=False,
            )
    content_type = payload.get("content_type")
    if isinstance(content_type, str) and content_type and content_type != "text":
        raise UmpError(
            Err.UNSUPPORTED_CAPABILITY,
            f"v1 只承担 text：content_type={content_type!r} 未支持",
            retryable=False,
        )


def _validate_payload(env_type: str, payload: dict[str, Any], max_text_len: int) -> None:
    if env_type == "hello":
        parse_hello(payload)
        return
    _reject_unsupported_extensions(payload)
    if env_type == "user_message":
        text = payload.get("text")
        if not isinstance(text, str) or not text.strip():
            raise UmpError(Err.PROTOCOL, "user_message.text must be a non-empty string")
        if len(text) > max_text_len:
            raise UmpError(Err.PROTOCOL, f"text exceeds {max_text_len} characters")
    elif env_type == "delivery":
        _require_str(payload, "message_id", max_len=64)
        index = payload.get("batch_index", 0)
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise UmpError(Err.PROTOCOL, "delivery.batch_index must be a non-negative integer")
        if payload.get("state") not in DELIVERY_STATES:
            raise UmpError(Err.PROTOCOL, "delivery.state must be accepted|failed|unknown")
    elif env_type == "retry":
        _require_str(payload, "ref", max_len=64)
        if payload.get("kind") not in (None, "input", "outbound"):
            raise UmpError(Err.PROTOCOL, "retry.kind must be input|outbound")
    elif env_type == "error":
        _require_str(payload, "code", max_len=64)
        if "message" in payload and not isinstance(payload["message"], str):
            raise UmpError(Err.PROTOCOL, "error.message must be a string")
        if not isinstance(payload.get("retryable", False), bool):
            raise UmpError(Err.PROTOCOL, "error.retryable must be a boolean")
        stage = payload.get("stage")
        if stage is not None and stage not in Stage.ALL:
            raise UmpError(Err.PROTOCOL, "error.stage must be receive|generate|delivery|protocol|auth")
    elif env_type == "accepted":
        _require_str(payload, "ref", max_len=64)
        if payload.get("state") not in ACCEPT_STATES:
            raise UmpError(Err.PROTOCOL, "accepted.state must be queued|processing|done|failed|cancelled")
    elif env_type == "reply":
        _require_str(payload, "message_id", max_len=64)
        parts = payload.get("parts")
        if not isinstance(parts, list) or not parts:
            raise UmpError(Err.PROTOCOL, "reply.parts must be a non-empty array")
        for part in parts:
            if not isinstance(part, dict) or not isinstance(part.get("text"), str):
                raise UmpError(Err.PROTOCOL, "reply.parts[].text must be a string")
        for key in ("batch_index", "batch_count"):
            value = payload.get(key, 0)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise UmpError(Err.PROTOCOL, f"reply.{key} must be a non-negative integer")
            if key == "batch_count" and value < 1:
                raise UmpError(Err.PROTOCOL, "reply.batch_count must be >= 1")
    elif env_type == "status":
        if payload.get("state") not in ("thinking", "idle", "interrupted"):
            raise UmpError(Err.PROTOCOL, "status.state must be thinking|idle|interrupted")
    elif env_type == "hello_ack":
        if payload.get("state") not in CORE_STATES:
            raise UmpError(Err.PROTOCOL, "hello_ack.state must be a known core state")
    elif env_type == "binding":
        if payload.get("state") not in ("active", "revoked"):
            raise UmpError(Err.PROTOCOL, "binding.state must be active|revoked")
    elif env_type == "system_notice":
        if not isinstance(payload.get("text"), str) or not payload["text"]:
            raise UmpError(Err.PROTOCOL, "system_notice.text must be a non-empty string")


def parse(
    raw: str | bytes | dict[str, Any],
    *,
    direction: str = "c2s",
    max_text_len: int = DEFAULT_MAX_TEXT_LEN,
) -> Envelope:
    """解析并校验一个信封。任何不合规都抛 UmpError。"""
    if isinstance(raw, (str, bytes)):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise UmpError(Err.BAD_FRAME, "frame is not valid JSON") from exc
    else:
        data = raw
    if not isinstance(data, dict):
        raise UmpError(Err.BAD_FRAME, "frame must be a JSON object")

    version = data.get("ump")
    if not isinstance(version, str) or not version.startswith(f"{UMP_MAJOR}."):
        raise UmpError(Err.PROTOCOL, f"unsupported ump version: {version!r}")

    env_type = data.get("type")
    allowed = CLIENT_TYPES if direction == "c2s" else SERVER_TYPES
    if not isinstance(env_type, str) or env_type not in ALL_TYPES:
        raise UmpError(Err.UNSUPPORTED_TYPE, f"unknown envelope type: {env_type!r}")
    if env_type not in allowed:
        raise UmpError(Err.PROTOCOL, f"type '{env_type}' not allowed in {direction}")

    env_id = _require_str(data, "id", max_len=64)
    ts = data.get("ts")
    if not _is_number(ts):
        raise UmpError(Err.PROTOCOL, "field 'ts' must be a number")

    thread_id: str | None = None
    token: str | None = None
    thread = data.get("thread")
    if thread is not None:
        if not isinstance(thread, dict):
            raise UmpError(Err.PROTOCOL, "field 'thread' must be an object")
        thread_id = _require_str(thread, "id", max_len=128)
        raw_token = thread.get("binding_token")
        if raw_token is not None:
            token = _require_str(thread, "binding_token", max_len=128)
    if env_type in THREAD_REQUIRED and thread_id is None:
        raise UmpError(Err.PROTOCOL, f"type '{env_type}' requires thread")
    if env_type in TOKEN_REQUIRED and token is None:
        raise UmpError(Err.PROTOCOL, f"type '{env_type}' requires thread.binding_token")

    payload = data.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise UmpError(Err.PROTOCOL, "field 'payload' must be an object")
    _validate_payload(env_type, payload, max_text_len)

    return Envelope(
        type=env_type,
        id=env_id,
        ts=float(ts),
        payload=payload,
        thread_id=thread_id,
        binding_token=token,
        raw=data,
    )


def make(
    env_type: str,
    payload: dict[str, Any],
    *,
    thread: dict[str, str] | None = None,
    thread_id: str | None = None,
    binding_token: str | None = None,
    id: str | None = None,
    ts: float | None = None,
) -> dict[str, Any]:
    """构造信封（dict 形式，可直接 json.dumps）。"""
    if thread is None and thread_id is not None:
        thread = {"id": thread_id}
        if binding_token is not None:
            thread["binding_token"] = binding_token
    env: dict[str, Any] = {
        "ump": UMP_VERSION,
        "type": env_type,
        "id": id or new_id("e"),
        "ts": ts if ts is not None else time.time(),
        "payload": payload,
    }
    if thread is not None:
        env["thread"] = thread
    return env


def error_envelope(
    error: UmpError,
    *,
    thread_id: str | None = None,
    binding_token: str | None = None,
    ref: str | None = None,
) -> dict[str, Any]:
    payload = error.to_payload()
    payload["ref"] = ref or payload["ref"]
    return make("error", payload, thread_id=thread_id, binding_token=binding_token)
