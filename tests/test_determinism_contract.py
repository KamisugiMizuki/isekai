"""确定性契约（A-8 的**不变量**，不只是「我修过一次」）。

`EVENT_ENGINE_SPEC.md:7` 明文承诺「禁止依赖进程随机化的语言内置 `hash()`」。
A-8 第一片修掉了三处（生活线计划 id、性格单元 id、授权转述条目 id）与两处缓存指纹，
但「修过一次」不等于「不会退回去」——这组测试**把公式钉死**：
任何人把 `stable_key` 换回 `secrets.token_hex` / `hash()`，这里立刻红灯。
"""

from __future__ import annotations

import hashlib

from isekai_core.runtime import events, life


class _CalendarStub:
    """`expand_plan` 只用到 `day_seconds`；用最小替身避免拉入整套历法夹具。"""

    day_seconds = 86400


def _card() -> dict:
    return {
        "meta": {"card_id": "cc-x"},
        "life_template": {"windows": [{"start": 0, "end": 3600, "activity": "巡岸"}]},
    }


def test_stable_key_is_fixed_encoding_blake2b() -> None:
    """钉住算法本身：固定分隔符 + blake2b(digest_size=12)，与进程无关。"""
    payload = "\x1f".join(["in-1", "tl-1", "cc-x"])
    expected = hashlib.blake2b(payload.encode("utf-8"), digest_size=12).hexdigest()
    assert events.stable_key("in-1", "tl-1", "cc-x") == expected
    assert len(expected) == 24, "12 字节 → 24 个十六进制字符：调用方按 [:12] 截断依赖这个长度"


def test_plan_id_is_derived_from_identity_not_randomness() -> None:
    """钉住生活线计划 id 的**公式**（原实现是 `secrets.token_hex`，同一天重跑会得到两个计划）。"""
    args = {
        "day_index": 7,
        "instance_id": "in-1",
        "timeline_id": "tl-1",
        "created_world": 0,
    }
    first = life.expand_plan(_card(), _CalendarStub(), **args)
    second = life.expand_plan(_card(), _CalendarStub(), **args)
    assert first["id"] == second["id"], "同一身份重复展开必须得到同一个计划 id"
    assert first["id"] == "lp-" + events.stable_key("in-1", "tl-1", "cc-x", 7)[:12], (
        "计划 id 必须由 (实例, 线, 角色, 世界日) 派生；退回随机 id 会让导出件不可复现"
    )
    other_day = life.expand_plan(_card(), _CalendarStub(), **{**args, "day_index": 8})
    assert other_day["id"] != first["id"], "不同世界日的计划必须是不同的计划"
