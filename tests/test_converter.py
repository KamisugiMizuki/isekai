"""可信转换器（WORLD_SETTING_SPEC §7.5 / §7.6）。

判据：没有登记就不猜；未确认不动数据；转换只在副本上做、校验不过不发布；发布是原子的一步。
"""

from __future__ import annotations

import json

import pytest

from isekai_core.world import converters
from isekai_core.world.instances import compatibility, convert_instance
from test_runtime import make_instance, store, world  # noqa: F401  夹具定义在那边

OLD_FORMAT = "9.9"


def _make_old(store, info) -> None:
    with store._lock, store._conn:
        store._conn.execute("UPDATE instance SET data_format=? WHERE id=?", (OLD_FORMAT, info["id"]))


def test_registry_refuses_unknown_migration() -> None:
    assert not converters.can_convert(OLD_FORMAT, "0.1")
    with pytest.raises(converters.ConverterError):
        converters.convert_payload({"a": 1}, source=OLD_FORMAT, target="0.1")


def test_convert_payload_never_touches_the_original() -> None:
    original = {"x": {"y": 1}}

    def bump(payload):
        payload["x"]["y"] = 2
        return payload

    converters.register_converter("a", "b", bump)
    try:
        out = converters.convert_payload(original, source="a", target="b")
        assert out["x"]["y"] == 2 and original["x"]["y"] == 1, "转换失败不该动原件"
        assert converters.converters() == [("a", "b")]
    finally:
        converters.unregister_converter("a", "b")


def test_blocked_without_converter_stays_blocked(store, world) -> None:
    info, _timeline_id, _character = make_instance(store, world)
    _make_old(store, info)
    state, reason = compatibility(store.instance_get(info["id"]))
    assert state == "blocked", (state, reason)
    out = convert_instance(store, info["id"], confirmed=True)
    assert not out["converted"] and out["state"] == "blocked"
    assert "兼容版本" in out["hint"]
    assert store.instance_get(info["id"])["data_format"] == OLD_FORMAT


def test_convert_needs_confirmation_then_publishes_atomically(store, world, tmp_path) -> None:
    info, _timeline_id, _character = make_instance(store, world)
    _make_old(store, info)
    converters.register_converter(OLD_FORMAT, "0.1", lambda payload: {**payload, "migrated": True})
    try:
        assert compatibility(store.instance_get(info["id"]))[0] == "convertible", "有转换器 → convertible"
        pending = convert_instance(store, info["id"], confirmed=False)
        assert pending["needs_confirmation"] and not pending["converted"]
        assert store.instance_get(info["id"])["data_format"] == OLD_FORMAT, "未确认不动数据"

        out = convert_instance(store, info["id"], confirmed=True, exports_dir=tmp_path / "exports")
        assert out["converted"] and out["from"] == OLD_FORMAT and out["safety"]
        assert list((tmp_path / "exports").glob("*.isekai.json")), "转换前留了可恢复副本"
        row = store.instance_get(info["id"])
        assert row["data_format"] != OLD_FORMAT and row["rules_version"]
        assert json.loads(row["setting"])["migrated"] is True, "发布的是转换后的设定"
        assert all(item["state"] == "frozen" for item in store.timeline_list(info["id"])), "线先冻结"
    finally:
        converters.unregister_converter(OLD_FORMAT, "0.1")


def test_invalid_product_is_not_published(store, world) -> None:
    info, _timeline_id, _character = make_instance(store, world)
    _make_old(store, info)
    before = store.instance_get(info["id"])["setting"]
    converters.register_converter(OLD_FORMAT, "0.1", lambda payload: {**payload, "world_package": {"meta": {}}})
    try:
        with pytest.raises(converters.ConverterError) as exc:
            convert_instance(store, info["id"], confirmed=True)
        assert "完整校验" in str(exc.value)
        row = store.instance_get(info["id"])
        assert row["setting"] == before, "校验不过就不发布：原实例一字不动"
        assert row["data_format"] == OLD_FORMAT
    finally:
        converters.unregister_converter(OLD_FORMAT, "0.1")


def test_converter_failure_reason_reaches_the_caller(store, world, tmp_path) -> None:
    """转换失败要给真实原因（哪一段没过校验），不能落成 internal。"""
    from isekai_core.config import load_config
    from isekai_core.ump import UmpError
    from isekai_core.world import ops as world_ops

    info, _timeline_id, _character = make_instance(store, world)
    _make_old(store, info)
    converters.register_converter("9.9", "0.1", lambda payload: {**payload, "world_package": {"meta": {}}})
    try:
        cfg = load_config(tmp_path)
        with pytest.raises(UmpError) as exc:
            world_ops.dispatch(cfg, store, "instance.convert", {"instance_id": info["id"], "confirmed": True})
        assert "转换产物未通过完整校验" in str(exc.value), exc.value
    finally:
        converters.unregister_converter("9.9", "0.1")


def test_format_identifiers_are_normalised() -> None:
    """格式标识规范化（§十 残余）：大小写 / 全角 / 首尾空白命中同一个转换器；展示按登记写法。"""
    converters.register_converter("Terra Ｖ1.2", "0.1", lambda payload: payload)
    try:
        assert converters.can_convert("terra v1.2", "0.1")
        assert converters.can_convert("  TERRA V1.2  ", "0.1")
        assert ("Terra Ｖ1.2", "0.1") in converters.converters(), "展示按首次登记的写法"
    finally:
        converters.unregister_converter("terra v1.2", "0.1")
    assert not converters.can_convert("terra v1.2", "0.1"), "注销也走规范化键"


def test_conversion_provenance_lands_in_the_setting(store, world) -> None:
    """转换出处随设定落地（§十 残余「元数据物理存放位置」）：不另设侧车文件，随导出 / 导入走。"""
    info, _timeline_id, _character = make_instance(store, world)
    _make_old(store, info)
    before_rules = store.instance_get(info["id"])["rules_version"]
    converters.register_converter(OLD_FORMAT, "0.1", lambda payload: {**payload})
    try:
        out = convert_instance(store, info["id"], confirmed=True)
        assert out["converted"] is True
        provenance = json.loads(store.instance_get(info["id"])["setting"])["converted_from"]
        assert provenance["data_format"] == OLD_FORMAT
        assert provenance["rules_version"] == before_rules
        assert provenance["converter"] == f"{OLD_FORMAT} → 0.1"
        assert provenance["at"] > 0
    finally:
        converters.unregister_converter(OLD_FORMAT, "0.1")
