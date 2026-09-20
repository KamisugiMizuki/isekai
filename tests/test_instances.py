"""实例创建 / 锁定 / 多实例 / 导入导出（阶段 1 验收）。"""

from __future__ import annotations

import copy
import json

import pytest

from isekai_core.store import Store
from isekai_core.world.instances import (
    InstanceError,
    create_instance,
    delete_instance,
    get_setting,
    list_instances,
    rename_instance,
)
from isekai_core.world.package import save_package
from isekai_core.world.portable import (
    build_container,
    check_compatibility,
    import_instance,
    read_container,
    write_export,
)
from samples import DAY, sample_card, sample_package


@pytest.fixture
def store(tmp_path):
    handle = Store(tmp_path / "data" / "isekai.db")
    handle.ensure_schema()
    yield handle
    handle.close()


def make(store, *, name: str = "灰潮纪", card_kwargs: dict | None = None) -> dict:
    package = sample_package(name)
    card = sample_card(package, **(card_kwargs or {}))
    return create_instance(store, package, [card])


def test_instance_is_locked_snapshot_and_starts_frozen(store) -> None:
    info = make(store)
    assert info["name"] == "灰潮纪"
    assert info["original_name"] == "灰潮纪"
    timelines = store.timeline_list(info["id"])
    assert len(timelines) == 1
    assert timelines[0]["state"] == "frozen", "创建不等于激活"
    commits = store.commit_list(info["id"])
    assert [c["kind"] for c in commits] == ["initial"]

    setting = get_setting(store, info["id"])
    assert setting["world_package"]["world"]["axioms"][0]["text"].startswith("潮汐")
    assert len(setting["cards"]) == 1


def test_editing_package_file_does_not_touch_existing_instance(store, tmp_path) -> None:
    package_path = tmp_path / "packages" / "greytide.json"
    package = sample_package()
    save_package(package_path, package)
    card = sample_card(package)
    first = create_instance(store, package, [card])

    # 改文件、改内存对象：旧实例的锁定快照都不受影响
    edited = json.loads(package_path.read_text(encoding="utf-8"))
    edited["world"]["axioms"][0]["text"] = "潮汐改为每四十日一次。"
    edited["meta"]["display_name"] = "改过的名字"
    save_package(package_path, edited)
    assert get_setting(store, first["id"])["world_package"]["world"]["axioms"][0]["text"].startswith("潮汐每三十日")

    second = create_instance(store, edited, [sample_card(edited)])
    assert second["id"] != first["id"]
    assert get_setting(store, second["id"])["world_package"]["world"]["axioms"][0]["text"].startswith("潮汐改为每四十日")


def test_display_name_follows_recorded_original_name(store) -> None:
    package = sample_package("灰潮纪")
    card = sample_card(package)
    first = create_instance(store, package, [card])
    assert first["name"] == "灰潮纪"

    package["meta"]["display_name"] = "完全不一样的名字"
    second = create_instance(store, package, [sample_card(package)])
    assert second["name"] == "灰潮纪_2", "显示名从原始名称复制一次，改包名不生效"

    third = create_instance(store, package, [sample_card(package)])
    assert third["name"] == "灰潮纪_3", "冲突取最小可用序号"


def test_rename_conflict_is_rejected_not_silently_renamed(store) -> None:
    first = make(store)
    second = make(store)
    with pytest.raises(InstanceError):
        rename_instance(store, first["id"], second["name"])
    renamed = rename_instance(store, first["id"], "另一个名字")
    assert renamed["name"] == "另一个名字"
    with pytest.raises(InstanceError):
        rename_instance(store, first["id"], "   ")


def test_failed_creation_leaves_nothing_behind(store) -> None:
    package = sample_package()
    bad_card = sample_card(package, confirmed=False)
    with pytest.raises(InstanceError):
        create_instance(store, package, [bad_card])
    assert store.counts()["instances"] == 0
    assert store.instance_list() == []

    with pytest.raises(InstanceError):
        create_instance(store, package, [])
    assert store.counts()["instances"] == 0


def test_multiple_instances_coexist_and_delete_is_scoped(store) -> None:
    first = make(store)
    second = make(store, name="盐滩记")
    assert [item["name"] for item in list_instances(store)] == ["灰潮纪", "盐滩记"]

    session = store.session_ensure(first["id"], store.timeline_list(first["id"])[0]["id"], "cc-堤禾")
    store.instance_import_messages(session["id"], [{"role": "user", "text": "在吗", "state": "fixed"}])
    delete_instance(store, first["id"])
    names = [item["name"] for item in list_instances(store)]
    assert names == ["盐滩记"]
    assert store.history_page(session["id"])["messages"] == [], "实例删除连带其会话与对话"
    assert store.counts()["messages"] == 0


def test_export_is_single_file_without_credentials_or_receipts(store, tmp_path) -> None:
    info = make(store)
    timeline = store.timeline_list(info["id"])[0]
    session = store.session_ensure(info["id"], timeline["id"], "cc-堤禾")
    store.instance_import_messages(
        session["id"],
        [
            {"role": "user", "text": "退潮了吗", "state": "fixed"},
            {"role": "character", "text": "还没，再等一个时辰。", "state": "fixed"},
        ],
    )
    store.thread_bind("cli-dev", "t1", session["id"])
    target = tmp_path / "export" / "greytide.isekai.json"
    manifest = write_export(store, info["id"], target)
    assert manifest["counts"]["sessions"] == 1
    assert manifest["counts"]["messages"] == 2
    assert manifest["counts"]["timelines"] == 1, "导出携带时间线与提交闭包（§7.1）"

    text = target.read_text(encoding="utf-8")
    container = json.loads(text)
    forbidden_keys = {
        "credential",
        "credential_hash",
        "binding_token",
        "delivery",
        "voided",
        "channel_id",
        "thread_id",
    }
    assert not (forbidden_keys & set(_all_keys(container))), "导出件不得携带凭据 / 回执 / 绑定 / 作废记录"
    assert container["setting"]["world_package"]["meta"]["original_name"] == "灰潮纪"
    assert len(container["runtime"]["messages"]) == 2


def _all_keys(node) -> list[str]:
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            found.append(key)
            found.extend(_all_keys(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_all_keys(item))
    return found


def test_import_creates_new_frozen_instance_and_keeps_original(store, tmp_path) -> None:
    info = make(store)
    timeline = store.timeline_list(info["id"])[0]
    session = store.session_ensure(info["id"], timeline["id"], "cc-堤禾")
    store.instance_import_messages(session["id"], [{"role": "user", "text": "退潮了吗", "state": "fixed"}])
    target = tmp_path / "export.isekai.json"
    write_export(store, info["id"], target)

    imported = import_instance(store, read_container(target))
    assert imported["name"] == "灰潮纪_2", "同名实例存在时导入自动追加序号"
    assert imported["imported"] is True
    assert imported["id"] != info["id"]
    timelines = store.timeline_list(imported["id"])
    assert timelines[0]["state"] == "frozen", "导入后默认冻结"
    assert timelines[0]["id"] != timeline["id"], "本地标识重新映射，不与原实例相连"
    assert [c["kind"] for c in store.commit_list(imported["id"])] == ["initial"], "提交闭包随件恢复（保持来源）"

    restored_session = store.instance_sessions(imported["id"])[0]
    messages = [m["text"] for m in store.history_page(restored_session["id"])["messages"]]
    assert messages == ["退潮了吗"]
    assert len(store.history_page(session["id"])["messages"]) == 1, "原实例不受影响"
    assert len(list_instances(store)) == 2

    third = import_instance(store, read_container(target))
    assert third["name"] == "灰潮纪_3"


def test_import_rejects_tampered_container(store, tmp_path) -> None:
    info = make(store)
    target = tmp_path / "export.isekai.json"
    write_export(store, info["id"], target)
    container = read_container(target)
    container["setting"]["world_package"]["world"]["axioms"][0]["text"] = "被改过的公理"
    with pytest.raises(InstanceError) as excinfo:
        import_instance(store, container)
    assert "完整性" in str(excinfo.value)
    assert len(list_instances(store)) == 1


def test_import_rejects_incompatible_versions(store, tmp_path) -> None:
    info = make(store)
    target = tmp_path / "export.isekai.json"
    write_export(store, info["id"], target)

    container = read_container(target)
    container["container"]["container_version"] = "2.0"
    status, reason = check_compatibility(container)
    assert status == "incompatible" and "容器格式" in reason
    with pytest.raises(InstanceError):
        import_instance(store, container)

    container = read_container(target)
    container["container"]["data_format"] = "9.0"
    status, reason = check_compatibility(container)
    assert status == "incompatible" and "数据格式" in reason

    container = read_container(target)
    container["container"]["capabilities"] = ["world.package.v2", "future.thing"]
    status, reason = check_compatibility(container)
    assert status == "incompatible" and "future.thing" in reason

    assert len(list_instances(store)) == 1, "拒绝导入不得留下半个实例"


def test_import_validates_locked_setting(store, tmp_path) -> None:
    info = make(store)
    target = tmp_path / "export.isekai.json"
    write_export(store, info["id"], target)
    container = read_container(target)
    container["setting"]["cards"][0]["meta"]["confirmed"] = False
    _reseal(container)
    with pytest.raises(InstanceError) as excinfo:
        import_instance(store, container)
    assert "未通过校验" in str(excinfo.value)
    assert len(list_instances(store)) == 1

    container = read_container(target)
    container["setting"]["cards"][0]["channels"][0]["source_id"] = "src-missing"
    _reseal(container)
    with pytest.raises(InstanceError):
        import_instance(store, container)
    assert len(list_instances(store)) == 1


def _reseal(container: dict) -> None:
    """测试用：改过内容后重算指纹，模拟「结构合法但校验不通过」的导入件。"""
    from isekai_core.world.portable import _digest

    payload = {"setting": container["setting"], "runtime": container["runtime"]}
    container["integrity"]["digest"] = _digest(payload)


def test_export_import_roundtrip_keeps_character_cards_and_original_name(store, tmp_path) -> None:
    package = sample_package()
    cards = [sample_card(package, name="堤禾"), sample_card(package, name="潮生")]
    info = create_instance(store, package, cards)
    target = tmp_path / "two-cards.isekai.json"
    write_export(store, info["id"], target)
    imported = import_instance(store, read_container(target))
    setting = get_setting(store, imported["id"])
    assert [card["identity"]["name"] for card in setting["cards"]] == ["堤禾", "潮生"]
    assert setting["original_name"] == "灰潮纪"
    assert copy.deepcopy(setting)["world_package"] == json.loads(json.dumps(package, ensure_ascii=False))


def test_export_manifest_moment_and_seed_are_stable(store, tmp_path) -> None:
    info = make(store)
    container = build_container(store, info["id"])
    assert container["container"]["moment"] == DAY * 1500
    seed = store.instance_get(info["id"])["seed"]
    imported = import_instance(store, container)
    assert store.instance_get(imported["id"])["seed"] == seed, "导入保留同一世界种子"
