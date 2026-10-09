"""B-6.1：同一事件的不同来源**可以**说不同的话（包里声明多版本说法）。

第 1 批实测的病：1500 世界日、9147 条说法**只有 8 句不同文本**，且每个事件给 3 个来源发的说法**完全相同**——
结构上「谁不知道什么」成立了，内容上「谁看到的版本不同」不成立。

B-6.1 是纯增量：模板声明 `claims` 时按来源取表述，**未声明时逐字节回到原行为**。这两点都必须被测试锁住，
否则「机制上线」与「既有内容被悄悄改写」无法区分。
"""

from __future__ import annotations

from isekai_core.runtime import events
from samples import sample_package  # noqa: F401  仓库既有夹具


def _template_id(package: dict) -> str:
    return str(package["events"]["families"][0]["templates"][0]["id"])


def _first_template(package: dict) -> dict:
    return package["events"]["families"][0]["templates"][0]


def _rows(package: dict, *, summary: str = "潮线过境", template: str = "") -> list[dict]:
    return events.claim_rows(
        {"summary": summary, "template": template},
        package=package,
        instance_id="in-1",
        timeline_id="tl-1",
        event_ident="ev-1",
        world_seconds=0,
        calendar=None,
    )


def test_without_declared_claims_every_source_keeps_the_summary() -> None:
    """回归：未声明 `claims` 的包，各来源文本必须**仍然完全相同**（既有内容不被改写）。"""
    package = sample_package()
    rows = _rows(package, template=_template_id(package))
    assert len(rows) >= 2, "样例包的来源数不足以检验这条性质"
    assert {row["text"] for row in rows} == {"潮线过境"}, "未声明时必须逐字节沿用事件摘要"


def test_declared_claims_give_each_source_its_own_text() -> None:
    """机制：声明了 `claims` 时，各来源按自己的骨架表述——这正是「内容分离」的落点。"""
    package = sample_package()
    template_id = _template_id(package)
    source_ids = [str(item["id"]) for item in package["sources"]]
    phrasing = {source_ids[0]: "北岸的船家说，潮线今夜过境。"}
    if len(source_ids) > 1:
        phrasing[source_ids[1]] = "官府文告只写：潮线已过，无碍。"
    _first_template(package)["claims"] = phrasing
    rows = _rows(package, template=template_id)
    assert rows[0]["text"] == phrasing[source_ids[0]]
    if len(source_ids) > 1:
        assert rows[1]["text"] == phrasing[source_ids[1]]
        assert rows[0]["text"] != rows[1]["text"], "同一事件的两个来源必须能说不同的话"


def test_undeclared_source_still_falls_back_to_the_summary() -> None:
    """部分声明时，未声明的来源回退到摘要——不做「猜一份说法」这种越权行为。"""
    package = sample_package()
    template_id = _template_id(package)
    source_ids = [str(item["id"]) for item in package["sources"]]
    _first_template(package)["claims"] = {source_ids[0]: "只有这一家有记载。"}
    rows = _rows(package, template=template_id)
    assert rows[0]["text"] == "只有这一家有记载。"
    for row in rows[1:]:
        assert row["text"] == "潮线过境"


def test_claims_are_matched_by_template_not_by_guess() -> None:
    """模板标识对不上时不得套用别处的说法（身份必须精确）。"""
    package = sample_package()
    _first_template(package)["claims"] = {"src-any": "不该被用到"}
    rows = _rows(package, template="t-不存在的模板")
    assert {row["text"] for row in rows} == {"潮线过境"}


# ---- B-6.2：把「可写」变成「被强制」（校验收紧） ----


def _validate_with_claims(claims) -> list[str]:
    from isekai_core.world.validate import validate_package

    package = sample_package()
    _first_template(package)["claims"] = claims
    return validate_package(package)


def _source_ids() -> list[str]:
    return [str(item["id"]) for item in sample_package()["sources"]]


def test_distinct_claims_pass_validation() -> None:
    """正例：各来源表述互异时校验通过。"""
    errors = _validate_with_claims({_source_ids()[0]: "北岸船家：潮线今夜过。"})
    assert not [item for item in errors if ".claims" in item], errors


def test_identical_claims_are_rejected() -> None:
    """反例：两个来源写成同一句 ⇒ 「声明了多版本」名存实亡，必须拒绝。"""
    ids = _source_ids()
    assert len(ids) >= 2, "样例包来源数不足以检验互异性"
    errors = _validate_with_claims({ids[0]: "潮线已过。", ids[1]: "潮线已过。"})
    assert any("完全相同" in item for item in errors), errors


def test_claim_for_unknown_source_is_rejected() -> None:
    """反例：键不是已登记来源 ⇒ 说法会挂在不存在的人身上。"""
    errors = _validate_with_claims({"src-不存在": "某句表述"})
    assert any("未登记的来源" in item for item in errors), errors


def test_empty_claim_text_is_rejected() -> None:
    """反例：空文本等于「这家没有记载」，与「有记载但没写」无法区分。"""
    errors = _validate_with_claims({_source_ids()[0]: "   "})
    assert any("不能为空" in item for item in errors), errors


def test_claims_must_be_a_mapping() -> None:
    """反例：形态不对（列表）要在创建期拒绝，而不是运行期静默忽略。"""
    errors = _validate_with_claims(["潮线过境"])
    assert any("必须是" in item and ".claims" in item for item in errors), errors
