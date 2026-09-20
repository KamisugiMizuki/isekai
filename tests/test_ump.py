"""UMP v1 信封校验（CHANNEL_PLUGIN_SPEC §2.1 / §2.3）。"""

from __future__ import annotations

import pytest

from isekai_core import ump
from isekai_core.ump import UmpError


def envelope(env_type: str, payload: dict, **kwargs) -> dict:
    return ump.make(env_type, payload, **kwargs)


def test_valid_user_message_parses():
    env = envelope("user_message", {"text": "今天过得怎么样？"}, thread_id="dm-42", binding_token="route-17")
    parsed = ump.parse(env)
    assert parsed.type == "user_message"
    assert parsed.thread_id == "dm-42"
    assert parsed.binding_token == "route-17"
    assert parsed.payload["text"] == "今天过得怎么样？"


def test_unknown_type_is_rejected_not_guessed():
    env = envelope("user_message", {"text": "hi"}, thread_id="t", binding_token="b")
    env["type"] = "make_me_admin"
    with pytest.raises(UmpError) as excinfo:
        ump.parse(env)
    assert excinfo.value.code == ump.Err.UNSUPPORTED_TYPE


def test_thread_required_for_message_types():
    with pytest.raises(UmpError) as excinfo:
        ump.parse(envelope("user_message", {"text": "hi", "binding_token": "x"}))
    assert excinfo.value.code == ump.Err.PROTOCOL


def test_binding_token_required_for_user_message():
    with pytest.raises(UmpError) as excinfo:
        ump.parse(envelope("user_message", {"text": "hi"}, thread_id="dm-1"))
    assert excinfo.value.code == ump.Err.PROTOCOL


def test_text_length_limit_enforced():
    with pytest.raises(UmpError):
        ump.parse(envelope("user_message", {"text": "x" * 4001}, thread_id="t", binding_token="b"))


def test_ts_must_be_a_number_not_bool():
    env = envelope("ping", {})
    env["ts"] = True
    with pytest.raises(UmpError):
        ump.parse(env)


def test_direction_is_enforced():
    server_reply = envelope("reply", {"message_id": "m-1", "parts": [{"text": "hi"}], "batch_count": 1}, thread_id="t")
    with pytest.raises(UmpError):
        ump.parse(server_reply, direction="c2s")
    assert ump.parse(server_reply, direction="s2c").type == "reply"


def test_delivery_state_must_be_known_value():
    with pytest.raises(UmpError):
        ump.parse(
            envelope("delivery", {"message_id": "m-1", "batch_index": 0, "state": "read"}, thread_id="t", binding_token="b")
        )


def test_hello_requires_auth_material_and_positive_limits():
    with pytest.raises(UmpError) as excinfo:
        ump.parse(envelope("hello", {"channel": {"id": "builtin"}, "capabilities": {}}))
    assert excinfo.value.code == ump.Err.AUTH_REQUIRED

    with pytest.raises(UmpError):
        ump.parse(
            envelope(
                "hello",
                {"channel": {"id": "builtin"}, "capabilities": {"max_parts": 0}, "auth": {"bootstrap": "b"}},
            )
        )


def test_hello_normalises_capabilities():
    hello = ump.parse_hello(
        {
            "channel": {"id": "builtin", "name": "内建聊天窗口", "version": "0.1.0"},
            "capabilities": {"segments": True, "status": True, "max_text_len": 1000, "max_parts": 3},
            "auth": {"bootstrap": "bs-1"},
        }
    )
    assert hello["capabilities"] == {
        "segments": True,
        "status": True,
        "text": True,
        "attachments": False,  # v1 只文本：能力声明里说清边界（§2.1）
        "stream": False,
        "max_text_len": 1000,
        "max_parts": 3,
    }
    assert hello["bootstrap"] == "bs-1" and hello["credential"] is None


def test_error_envelope_carries_retryable_and_ref():
    error = UmpError(ump.Err.GENERATION_FAILED, "生成失败", retryable=True, ref="e-1")
    env = ump.error_envelope(error, thread_id="dm-1", ref="e-1")
    assert env["type"] == "error"
    assert env["payload"]["retryable"] is True
    assert env["payload"]["ref"] == "e-1"
