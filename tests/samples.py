"""测试夹具：世界包 / 角色卡样本来自包内示例（同一真源）。"""

from __future__ import annotations

import json
from typing import Any

from isekai_core.world.example import DAY, example_card, example_package


def sample_package(name: str = "灰潮纪", *, moment: int = DAY * 1500) -> dict[str, Any]:
    return example_package(name, moment=moment)


def sample_card(
    package: dict[str, Any], *, name: str = "堤禾", born: int | None = None, confirmed: bool = True
) -> dict[str, Any]:
    return example_card(package, name=name, born=born, confirmed=confirmed)


def dump(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False)
