"""加载限额（WORLD_SETTING_SPEC §2.3）：世界包读取前按字节数拦，超限不读进来。"""

from __future__ import annotations

import json

import pytest

from isekai_core.world.package import MAX_PACKAGE_BYTES, PackageError, load_package, read_json_file
from samples import sample_package


def test_oversized_package_is_rejected(tmp_path) -> None:
    path = tmp_path / "big.json"
    payload = sample_package()
    payload["filler"] = "x" * MAX_PACKAGE_BYTES  # 结构上仍是个包，只是字节数超限
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert path.stat().st_size > MAX_PACKAGE_BYTES
    with pytest.raises(PackageError) as exc:
        load_package(path)
    assert "加载限额" in str(exc.value), exc.value
    assert {"meta", "calendar"} <= set(payload), "被拒的是超限文件，不是「因为不是包」"


def test_normal_package_still_loads(tmp_path) -> None:
    path = tmp_path / "ok.json"
    path.write_text(json.dumps(sample_package(), ensure_ascii=False), encoding="utf-8")
    assert load_package(path)["meta"]


def test_container_limit_is_wider_than_package_limit(tmp_path) -> None:
    from isekai_core.world.portable import MAX_CONTAINER_BYTES

    assert MAX_CONTAINER_BYTES > MAX_PACKAGE_BYTES
    path = tmp_path / "middle.json"
    path.write_text(json.dumps({"x": "y" * (MAX_PACKAGE_BYTES + 1024)}), encoding="utf-8")
    with pytest.raises(PackageError):
        read_json_file(path)
    assert read_json_file(path, limit=MAX_CONTAINER_BYTES)["x"], "容器件走更宽的那道闸"
