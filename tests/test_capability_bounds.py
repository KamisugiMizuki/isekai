"""能力边界（CHANNEL_PLUGIN_SPEC §2.1 / §七 更后置）：

v1 只承担文本；附件 / 富媒体 / 流式是更后置的扩展点——收到相关字段必须**明确拒绝**，不静默忽略。
"""

from __future__ import annotations

import pytest

from isekai_core import ump
from isekai_core.ump import Err, UmpError


def _message(**extra) -> dict:
    payload = {"text": "在吗", **extra}
    return ump.make("user_message", payload, thread_id="dm-1", binding_token="tok")


def test_hello_declares_text_only() -> None:
    hello = ump.parse_hello({"channel": {"id": "probe"}, "auth": {"bootstrap": "b"}})
    caps = hello["capabilities"]
    assert caps["text"] is True
    assert caps["attachments"] is False and caps["stream"] is False, "v1 的能力声明说清边界"


def test_plain_text_still_passes() -> None:
    env = ump.parse(_message(), direction="c2s")
    assert env.payload["text"] == "在吗"


@pytest.mark.parametrize(
    "extra",
    [
        {"attachments": [{"kind": "image", "url": "x"}]},
        {"stream": True},
        {"content_type": "image/png"},
    ],
)
def test_extension_fields_are_refused_explicitly(extra) -> None:
    with pytest.raises(UmpError) as exc:
        ump.parse(_message(**extra), direction="c2s")
    assert exc.value.code == Err.UNSUPPORTED_CAPABILITY, exc.value
    assert "文本" in str(exc.value) or "text" in str(exc.value)


def test_empty_extension_fields_are_tolerated() -> None:
    env = ump.parse(_message(attachments=[], stream=""), direction="c2s")
    assert env.type == "user_message", "空值等于没声明，不算越界"
