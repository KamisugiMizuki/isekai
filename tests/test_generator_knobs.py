"""参数层旋钮 → 提示词（DESKTOP_GENERATION_WORKSPACE_SPEC §3.2 / §七 P1）。

行为级：断言真实生成链路（FakeLLM 记录到的系统提示词）里能看见旋钮落成的句子；
没给旋钮时提示词逐字不变（既有行为不受影响）。
"""

from __future__ import annotations

import json
import re

import pytest

from isekai_core.llm import FakeLLM
from isekai_core.world import generator
from isekai_core.world.generator import KNOB_COUNTS, KNOB_LISTS, KNOB_TONES, knob_brief, parse_knobs
from isekai_core.world.package import PackageError
from samples import sample_package

VALID = json.dumps(sample_package(), ensure_ascii=False)


async def _generate(knobs=None):
    llm = FakeLLM([VALID])
    await generator.generate_package(llm, "一片灰潮沿岸", name="灰潮纪", knobs=knobs)
    return "\n".join(str(message.get("content") or "") for call in llm.calls for message in call)


def _mask(text: str) -> str:
    return re.sub(r"wp-[0-9a-f]+", "wp-x", text)


def test_knob_brief_maps_every_knob() -> None:
    """13 计数 + 9 取向 + 3 内容指定：每个旋钮都要在提示词里有对应句子。"""
    knobs = {key: f"{label}值" for key, label in KNOB_TONES}
    knobs |= {key: index + 1 for index, (key, _) in enumerate(KNOB_COUNTS)}
    knobs |= {"include": ["盐税", "旧堤砖"], "exclude": ["火器"], "homage": ["海国志"]}
    text = knob_brief(knobs)
    for key, label in KNOB_TONES:
        assert f"{label} {knobs[key]}" in text, (key, text)
    for key, label in KNOB_COUNTS:
        assert f"{label} {knobs[key]}" in text, (key, text)
    assert "必须出现：盐税、旧堤砖" in text and "禁止出现：火器" in text and "可参考致敬：海国志" in text
    assert knob_brief({}) == "" and knob_brief(None) == "", "没旋钮 = 不加句子"


def test_zero_count_says_do_not_generate() -> None:
    text = knob_brief({"axioms": 4, "festivals": 0})
    assert "世界公理 4" in text and "节庆 0" in text
    assert "标 0 的段不要生成：节庆" in text
    assert "以校验为准" in text, "零计数与最小内容标准冲突时要写清由校验裁决"


def test_knobs_are_a_trust_boundary() -> None:
    assert parse_knobs({"tone": " 冷硬 ", "axioms": 3}) == {"tone": "冷硬", "axioms": 3}
    assert parse_knobs({"include": "一行一条"}) == {"include": ["一行一条"]}, "单条文本按一条处理"
    assert parse_knobs({"include": ["", "  "]}) == {}, "全空清单等于没给"
    for bad in ({"nope": 1}, {"axioms": "四"}, {"axioms": -1}, {"include": [1]}, {"tone": 3}, "文本"):
        with pytest.raises(PackageError):
            parse_knobs(bad)
    assert parse_knobs({"tone": "字" * 500})["tone"] == "字" * 200, "超长截断，不把提示词撑爆"


@pytest.mark.asyncio
async def test_generated_prompt_carries_knobs() -> None:
    prompt = await _generate({"genre": "低魔海国", "tone": "冷硬", "races": 2, "festivals": 0, "exclude": ["火器"]})
    assert "用户旋钮（按此调性与规模产出）" in prompt
    assert "体裁 低魔海国" in prompt and "基调 冷硬" in prompt
    assert "种族 2" in prompt and "节庆 0" in prompt and "禁止出现：火器" in prompt


@pytest.mark.asyncio
async def test_prompt_is_unchanged_without_knobs() -> None:
    plain = await _generate(None)
    empty = await _generate({})
    # 骨架里的 package_id 每次随机，屏蔽掉它再比——要比的是旋钮有没有多出句子
    assert _mask(plain) == _mask(empty), "空旋钮不能改变既有提示词"
    assert "用户旋钮" not in plain
