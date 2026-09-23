"""U5 的发行件事（ONBOARDING §3.2）：数据根与随发行样例。"""

from __future__ import annotations

from pathlib import Path

from isekai_core import config as config_mod
from isekai_core.__main__ import seed_bundled_examples


def test_packaged_root_is_user_data_dir(monkeypatch, tmp_path) -> None:
    """打包运行：数据根在用户目录；显式路径与 ISEKAI_ROOT 优先级更高（开发态不变）。"""
    monkeypatch.delenv("ISEKAI_ROOT", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "LocalAppData"))
    monkeypatch.setenv("ISEKAI_PACKAGED", "1")
    assert config_mod.resolve_root() == tmp_path / "LocalAppData" / "isekai"
    assert config_mod.is_packaged() is True

    monkeypatch.setenv("ISEKAI_ROOT", str(tmp_path / "explicit-env"))
    assert config_mod.resolve_root() == tmp_path / "explicit-env"
    assert config_mod.resolve_root(tmp_path / "explicit") == tmp_path / "explicit"

    monkeypatch.delenv("ISEKAI_PACKAGED")
    assert config_mod.resolve_root() == tmp_path / "explicit-env"
    monkeypatch.delenv("ISEKAI_ROOT")
    assert config_mod.resolve_root() == Path(config_mod.__file__).resolve().parent.parent


def test_bundled_examples_land_in_data_root_once(monkeypatch, tmp_path) -> None:
    """发行件首次运行把随包样例放进数据根；已有的一份不被覆盖（升级不动用户数据）。"""
    monkeypatch.setenv("ISEKAI_PACKAGED", "1")
    root = tmp_path / "data"
    root.mkdir()
    seed_bundled_examples(root)
    copied = root / "examples"
    assert copied.is_dir() and any(copied.rglob("*.json")), "随发行样例应当进数据根"

    marker = copied / "用户改过.txt"
    marker.write_text("用户自己动的", encoding="utf-8")
    seed_bundled_examples(root)
    assert marker.read_text(encoding="utf-8") == "用户自己动的", "第二次运行不许覆盖数据根里的样例目录"

    monkeypatch.delenv("ISEKAI_PACKAGED")
    other = tmp_path / "dev"
    other.mkdir()
    seed_bundled_examples(other)
    assert not (other / "examples").exists(), "开发态不做这件事（样例就在仓库里）"
