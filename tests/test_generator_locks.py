"""锁定条目：重生成不覆盖（DESKTOP_GENERATION_WORKSPACE_SPEC §3.3 / §七 P2 / §八）。

行为级：段级重跑与整包重跑都要保留锁定条目，且锁定项要原样带进提示词。
"""

from __future__ import annotations

import json

import pytest

from isekai_core.llm import FakeLLM
from isekai_core.world import generator
from isekai_core.world.generator import apply_locks, locks_note, parse_locks
from isekai_core.world.package import PackageError, clone_package
from samples import sample_package


def test_apply_locks_keeps_locked_entries() -> None:
    current = sample_package()
    candidate = clone_package(current)
    candidate["canon"][0]["statement"] = "模型改写过的另一句话"
    candidate["world"]["axioms"][0]["text"] = "模型改写过的公理"
    locked = {"canon": [str(current["canon"][0]["id"])], "world.axioms": [str(current["world"]["axioms"][0]["id"])]}
    merged = apply_locks(candidate, current, locked)
    assert merged["canon"][0] == current["canon"][0], "锁定条目原样写回"
    assert merged["world"]["axioms"][0] == current["world"]["axioms"][0]
    assert merged["narratives"] == candidate["narratives"], "没锁的段落不动"
    assert apply_locks(candidate, current, {}) is candidate, "没有锁定就不复制、不改动"


def test_apply_locks_tolerates_wrong_paths_and_missing_ids() -> None:
    current = sample_package()
    candidate = clone_package(current)
    candidate["races"] = []
    merged = apply_locks(candidate, current, {"races": ["rc-不存在"], "不存在的段": ["x"], "canon": []})
    assert merged["races"] == [], "锁不上（id 不在册 / 段不在候选中）就当没锁"
    assert merged["canon"] == current["canon"]


def test_parse_locks_is_a_trust_boundary() -> None:
    assert parse_locks({"canon": [" cf-1 ", "cf-1"]}) == {"canon": ["cf-1"]}, "去重 + 去空白"
    assert parse_locks({"canon": "cf-1"}) == {"canon": ["cf-1"]}, "单条按一条处理"
    assert parse_locks({"canon": []}) == {} and parse_locks(None) == {}
    for bad in ({"": ["cf-1"]}, {".canon": ["cf-1"]}, {"canon.": ["cf-1"]}, {"canon": [1]}, ["canon"], {"canon": {"a": 1}}):
        with pytest.raises(PackageError):
            parse_locks(bad)


def test_locks_note_carries_the_entries_and_the_rule() -> None:
    package = sample_package()
    ident = str(package["canon"][0]["id"])
    note = locks_note(package, {"canon": [ident]})
    assert "已定稿" in note and "不得改动" in note
    assert str(package["canon"][0]["statement"]) in note, "锁定条目的内容要原样带进提示词"
    assert locks_note(package, {}) == "" and locks_note(package, {"canon": ["cf-不存在"]}) == ""


@pytest.mark.asyncio
async def test_fill_section_keeps_locked_entries() -> None:
    """段级重跑：模型改写了锁定条目 → 结果里仍是用户那一版；提示词里带着「已定稿」。"""
    package = sample_package()
    ident = str(package["canon"][0]["id"])
    rewritten = clone_package(package)
    rewritten["canon"][0]["statement"] = "模型自作主张的改写"
    renamed = clone_package(rewritten)
    renamed["canon"][0]["id"] = "cf-新写的"
    llm = FakeLLM([json.dumps(renamed, ensure_ascii=False)])
    out, errors, _usage = await generator.fill_section(
        llm, package, "canon", locked={"canon": [ident]}
    )
    assert errors == [], errors
    assert out["canon"][0] == package["canon"][0], "锁定项压过模型改写"
    assert any(item.get("id") == "cf-新写的" for item in out["canon"]), "没锁的新条目照常收下"
    prompt = "\n".join(str(message.get("content") or "") for call in llm.calls for message in call)
    assert "已定稿" in prompt and str(package["canon"][0]["statement"]) in prompt


@pytest.mark.asyncio
async def test_generate_package_keeps_locked_entries_across_a_full_rerun() -> None:
    """整包重跑：以当前候选为 base 发起，锁定条目在终局与逐段校验里都写回。"""
    package = sample_package()
    ident = str(package["world"]["axioms"][0]["id"])
    rewritten = clone_package(package)
    rewritten["world"]["axioms"][0]["text"] = "重跑后模型换掉的公理"
    llm = FakeLLM([json.dumps(rewritten, ensure_ascii=False)])
    out, errors, _usage = await generator.generate_package(
        llm, "重跑一遍", name="灰潮纪", locked={"world.axioms": [ident]}, base=package
    )
    assert errors == [], errors
    assert out["world"]["axioms"][0] == package["world"]["axioms"][0]
    prompt = "\n".join(str(message.get("content") or "") for call in llm.calls for message in call)
    assert "已定稿" in prompt, "整包重跑也要把锁定条目带进提示词"


@pytest.mark.asyncio
async def test_fill_section_can_rerun_a_whole_segment() -> None:
    """「重跑这段」= 一次调用补一段（逗号分隔的键）：只判本段的键，别的段逐字段不变。"""
    package = sample_package()
    segment = "sources,canon,narratives,races,entities"
    rewritten = clone_package(package)
    rewritten["canon"][0]["statement"] = "重写过的实情条目"
    rewritten["world"]["axioms"][0]["text"] = "这一段不该被本段重跑碰到"
    llm = FakeLLM([json.dumps(rewritten, ensure_ascii=False)])
    out, errors, _usage = await generator.fill_section(llm, package, segment)
    assert errors == [], errors
    assert out["world"] == package["world"], "没重跑的段逐字段不变"
    assert out["canon"][0]["statement"] == "重写过的实情条目", "重跑的段收下新内容"
    prompt = "\n".join(str(message.get("content") or "") for call in llm.calls for message in call)
    assert segment in prompt, "提示词里写明这一段要补哪些键"

    with pytest.raises(PackageError):
        await generator.fill_section(llm, package, "canon,不存在的段")


@pytest.mark.asyncio
async def test_fill_card_only_rewrites_named_fields_and_keeps_locks() -> None:
    """卡的字段级重跑（§4.2）：点名的字段收下模型结果，其余字段由核心强制取原卡；锁定字段原样保留。"""
    from isekai_core.world.cards import template_card
    from isekai_core.world.example import example_card

    package = sample_package()
    card = example_card(package)
    sections = "background,first_contact"
    rewritten = clone_package(card)
    rewritten["background"] = {"creator": "模型另写的身世", "self_knowledge": "模型另写的自述"}
    rewritten["first_contact"] = {"stance": "模型写的初见态度", "intent": "模型写的意图"}
    rewritten["identity"]["occupation"] = "模型顺手改的职业"  # 没点名：不该被收下
    llm = FakeLLM([json.dumps(rewritten, ensure_ascii=False)])
    out, errors, _usage = await generator.fill_card(
        llm, package, card, sections, locked_fields=["background.self_knowledge"]
    )
    assert errors == [], errors
    assert out["first_contact"]["stance"] == "模型写的初见态度", "点名的字段要收下新内容"
    assert out["background"]["creator"] == "模型另写的身世"
    assert out["background"]["self_knowledge"] == card["background"]["self_knowledge"], "锁定字段原样保留"
    assert out["identity"] == card["identity"], "没点名的字段逐字段不变"
    prompt = "\n".join(str(message.get("content") or "") for call in llm.calls for message in call)
    assert "已定稿" in prompt and "background,first_contact" in prompt

    with pytest.raises(PackageError):
        await generator.fill_card(llm, package, card, "background,不存在的字段")


@pytest.mark.asyncio
async def test_generate_card_keeps_locked_fields() -> None:
    from isekai_core.world.example import example_card

    package = sample_package()
    card = example_card(package)
    rewritten = clone_package(card)
    rewritten["identity"]["occupation"] = "模型重写的职业"
    llm = FakeLLM([json.dumps(rewritten, ensure_ascii=False)])
    out, errors, _usage = await generator.generate_card(
        llm, package, "随便描述", locked_fields=["identity.occupation"], base=card
    )
    assert errors == [], errors
    assert out["identity"]["occupation"] == card["identity"]["occupation"], "整卡重跑也不覆盖锁定字段"


@pytest.mark.asyncio
async def test_progress_snapshot_tracks_a_running_generation() -> None:
    """P3 进度可见：生成期间能读到「第 n/总段 · 已调用 m 次」，结束后 running 落回 False。"""
    import asyncio

    package = sample_package()
    llm = FakeLLM([json.dumps(package, ensure_ascii=False)])
    llm.delay_s = 0.25
    task = asyncio.create_task(generator.generate_package(llm, "灰潮沿岸", name="灰潮纪"))
    await asyncio.sleep(0.35)
    mid = generator.progress_snapshot()
    assert mid["running"] is True, mid
    assert mid["step"] >= 1 and mid["total"] == len(generator.PACKAGE_SEGMENTS), mid
    assert "世界包" in str(mid["label"]) and int(mid["calls"]) >= 1, mid
    await task
    assert generator.progress_snapshot()["running"] is False, "生成结束（含失败路径）要把进度收尾"


@pytest.mark.asyncio
async def test_progress_snapshot_closes_on_budget_exhaustion() -> None:
    llm = FakeLLM([json.dumps(sample_package(), ensure_ascii=False)])
    await generator.generate_package(llm, "灰潮沿岸", name="灰潮纪", max_calls=1)
    assert generator.progress_snapshot()["running"] is False


@pytest.mark.asyncio
async def test_revise_package_keeps_locked_entries() -> None:
    package = sample_package()
    ident = str(package["narratives"][0]["id"])
    rewritten = clone_package(package)
    rewritten["narratives"][0]["text"] = "修订器改掉的说法"
    llm = FakeLLM([json.dumps(rewritten, ensure_ascii=False)])
    out, errors, _usage = await generator.revise_package(
        llm, package, "把说法写得更含糊", locked={"narratives": [ident]}
    )
    assert errors == [], errors
    assert out["narratives"][0] == package["narratives"][0]
