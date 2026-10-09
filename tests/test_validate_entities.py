"""名册实体字段校验（A-9c）：`born` / `died` 曾被包校验放行、却被运行期当真值消费。

这条修复是**向后不兼容的校验收紧**：以前能通过创建的包会被拒。因此除了正例（合法包仍然通过），
还要有反例（坏值必须被拒），否则「收紧」与「破坏既有包」无法区分。
"""

from __future__ import annotations

from isekai_core.world.validate import validate_package
from samples import sample_package  # noqa: F401  仓库既有夹具


def _package_with_entity(**overrides) -> dict:
    package = sample_package()
    entity = dict(package["entities"][0])
    entity.update(overrides)
    package["entities"] = [entity] + list(package["entities"][1:])
    return package


def test_valid_integer_born_died_still_passes() -> None:
    """正例：整数世界秒是合法形态（这是包内既有的写法）。"""
    package = _package_with_entity(born=-72576000, died=-60000000)
    errors = validate_package(package)
    assert not [item for item in errors if ".born" in item or ".died" in item], errors


def test_non_integer_born_is_rejected() -> None:
    """反例：`born` 是散文时运行期会算出垃圾年龄，必须在创建前拒绝。"""
    errors = validate_package(_package_with_entity(born="很久以前"))
    assert any("entities[0].born" in item for item in errors), errors


def test_died_before_born_is_rejected() -> None:
    """反例：死亡早于出生是自相矛盾的名册，运行期会推出「先死后生」。"""
    errors = validate_package(_package_with_entity(born=1000, died=999))
    assert any("entities[0].died" in item for item in errors), errors


def test_absent_born_died_is_still_allowed() -> None:
    """缺省仍合法（非人物实体没有生日/忌日）：收紧不得把「未登记」也一并拒掉。"""
    package = sample_package()
    for entity in package["entities"]:
        entity.pop("born", None)
        entity.pop("died", None)
    errors = validate_package(package)
    assert not [item for item in errors if ".born" in item or ".died" in item], errors
