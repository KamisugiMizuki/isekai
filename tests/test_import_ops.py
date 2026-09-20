"""导入 op（WORLD_SETTING_SPEC §7.5）：`world.package.import` / `world.card.import`。

判据：读取前走字节限额 → 结构 / 联合校验 → **校验不过不落盘**；同名要显式确认才覆盖。
"""

from __future__ import annotations

import json

import pytest

from isekai_core.config import load_config
from isekai_core.ump import UmpError
from isekai_core.world import ops as world_ops
from isekai_core.world.example import example_card
from samples import sample_package
from test_runtime import make_instance, store, world  # noqa: F401


def _call(cfg, store, op: str, args: dict) -> dict:
    return world_ops.dispatch(cfg, store, op, args)


def test_package_import_writes_after_validation(store, tmp_path) -> None:
    cfg = load_config(tmp_path)
    src = tmp_path / "outside" / "greytide.json"
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_text(json.dumps(sample_package(), ensure_ascii=False), encoding="utf-8")

    out = _call(cfg, store, "world.package.import", {"source_path": str(src)})
    assert out["imported"] == "greytide.json"
    assert (cfg.paths.packages / "greytide.json").is_file()

    with pytest.raises(UmpError) as exc:
        _call(cfg, store, "world.package.import", {"source_path": str(src)})
    assert "已存在" in str(exc.value), "同名不静默覆盖"
    again = _call(cfg, store, "world.package.import", {"source_path": str(src), "force": True})
    assert again["replaced"] is True


def test_package_import_rejects_invalid_without_writing(store, tmp_path) -> None:
    cfg = load_config(tmp_path)
    bad = json.loads(json.dumps(sample_package()))
    bad["world"]["axioms"] = []
    src = tmp_path / "bad.json"
    src.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(UmpError) as exc:
        _call(cfg, store, "world.package.import", {"source_path": str(src)})
    assert "未通过校验" in str(exc.value)
    assert not (cfg.paths.packages / "bad.json").exists(), "校验不过不落盘"


def test_card_import_checks_against_its_package(store, tmp_path) -> None:
    cfg = load_config(tmp_path)
    package = sample_package()
    pkg_file = tmp_path / "wp.json"
    pkg_file.write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")

    card = example_card(package)
    card["channels"][0]["source_id"] = "src-ghost"  # 引用包内不存在的传本
    src = tmp_path / "card.json"
    src.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(UmpError) as exc:
        _call(cfg, store, "world.card.import", {"source_path": str(src), "package_path": str(pkg_file)})
    assert "联合校验" in str(exc.value)
    assert not (cfg.paths.packages / "card.json").exists()

    card["channels"][0]["source_id"] = str(package["sources"][0]["id"])
    src.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
    out = _call(cfg, store, "world.card.import", {"source_path": str(src), "package_path": str(pkg_file)})
    assert out["imported"] == "card.json"
    assert (cfg.paths.packages / "card.json").is_file()
