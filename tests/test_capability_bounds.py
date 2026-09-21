"""能力边界（CHANNEL_PLUGIN_SPEC §2.1 / §七）：

- 附件（2026-09-22 落地）：**按能力位**——通道在 hello 里声明才收，没声明就给 = 明确拒绝；
- 流式 / 富媒体其余字段：仍是更后置的扩展点，收到必须拒绝，不静默忽略。
"""

from __future__ import annotations

import base64

import pytest

from isekai_core import ump
from isekai_core.ump import Err, UmpError

PNG_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"x" * 8).decode("ascii")


def _message(**extra) -> dict:
    payload = {"text": "在吗", **extra}
    return ump.make("user_message", payload, thread_id="dm-1", binding_token="tok")


def _image() -> dict:
    return {"name": "a.png", "media_type": "image/png", "data": PNG_B64}


def test_hello_declares_capabilities() -> None:
    hello = ump.parse_hello({"channel": {"id": "probe"}, "auth": {"bootstrap": "b"}})
    caps = hello["capabilities"]
    assert caps["text"] is True
    assert caps["attachments"] is False, "端不声明就不开（交集语义）"
    assert caps["stream"] is False, "流式仍是更后置的扩展点"


def test_plain_text_still_passes() -> None:
    env = ump.parse(_message(), direction="c2s")
    assert env.payload["text"] == "在吗"


@pytest.mark.parametrize(
    "extra",
    [
        {"stream": True},
        {"content_type": "image/png"},
        {"media": [{"kind": "image"}]},
    ],
)
def test_extension_fields_are_refused_explicitly(extra) -> None:
    with pytest.raises(UmpError) as exc:
        ump.parse(_message(**extra), direction="c2s")
    assert exc.value.code == Err.UNSUPPORTED_CAPABILITY, exc.value
    assert "文本" in str(exc.value) or "text" in str(exc.value)


def test_attachment_needs_negotiation() -> None:
    """没协商 = 明确拒绝（不是静默丢掉字段）。"""
    with pytest.raises(UmpError) as exc:
        ump.parse(_message(attachments=[_image()]), direction="c2s")
    assert exc.value.code == Err.UNSUPPORTED_CAPABILITY
    assert "附件" in str(exc.value)


def test_attachment_accepted_when_negotiated_and_normalised() -> None:
    env = ump.parse(_message(attachments=[_image()]), direction="c2s", attachments=(3, 4096))
    stored = env.payload["attachments"]
    assert stored[0]["name"] == "a.png" and stored[0]["media_type"] == "image/png"
    assert stored[0]["size"] == 16, "大小按解码后计"


def test_attachment_quota_is_enforced() -> None:
    with pytest.raises(UmpError) as exc:
        ump.parse(_message(attachments=[_image()]), direction="c2s", attachments=(0, 4096))
    assert exc.value.code == Err.UNSUPPORTED_CAPABILITY and "条数" in str(exc.value)
    with pytest.raises(UmpError) as exc:
        ump.parse(_message(attachments=[_image()]), direction="c2s", attachments=(3, 8))
    assert exc.value.code == Err.UNSUPPORTED_CAPABILITY and "字节" in str(exc.value)


def test_empty_extension_fields_are_tolerated() -> None:
    env = ump.parse(_message(attachments=[], stream=""), direction="c2s")
    assert env.type == "user_message", "空值等于没声明，不算越界"
