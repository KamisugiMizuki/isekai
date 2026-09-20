"""版本三件套第三件（DESIGN §5.7）：生成器 / 提示词 / 文本模型指纹。"""

from __future__ import annotations

from isekai_core.version import DATA_FORMAT_VERSION, RULES_VERSION, generator_fingerprint
from isekai_core.world import generator


def test_fingerprint_is_stable_and_model_scoped() -> None:
    """同一生成器 + 同一模型 → 同值；换模型 → 换值；与存储 / 规则版本互不替代。"""
    a = generator._fingerprint("model-a")
    b = generator._fingerprint("model-a")
    c = generator._fingerprint("model-b")
    assert a == b and len(a) == 16
    assert a != c
    # 与另外两件不互相顶替：指纹不随 data_format / rules_version 变
    assert generator_fingerprint(segments=("x",), hints=("y",), model="m") == generator_fingerprint(
        segments=("x",), hints=("y",), model="m"
    )
    assert (DATA_FORMAT_VERSION, RULES_VERSION) == ("0.1", "0.1")


def test_generated_package_carries_generator_boundary() -> None:
    """产出的包带上生成器指纹与模型名（管理元数据，不进世界事实）。"""
    package = generator.template_package("边界世界")
    generator.stamp_generator_meta(package, model="model-x")
    assert package["meta"]["generator_fingerprint"] == generator._fingerprint("model-x")
    assert package["meta"]["generator_model"] == "model-x"
    # 只是元数据：世界事实段不受影响
    assert "world" in package and "axioms" in package["world"]
