"""世界设定层管理面操作（阶段 1）：真实 WS + 真实 SQLite + 真文件，只把 LLM 换成脚本。"""

from __future__ import annotations

import json

import pytest

from conftest import open_mgmt, running_core
from samples import sample_card, sample_package


@pytest.fixture
def root(tmp_path):
    (tmp_path / "packages").mkdir(parents=True, exist_ok=True)
    return tmp_path


def write_json(path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.mark.asyncio
async def test_package_template_save_validate_roundtrip(root) -> None:
    async with running_core(root) as harness:
        mgmt = await open_mgmt(harness)
        try:
            created = await mgmt.call("world.package.template", name="灰潮纪")
            package = created["package"]
            assert package["meta"]["original_name"] == "灰潮纪"

            result = await mgmt.call("world.package.validate", package=package)
            assert result["errors"], "骨架必须被填满才能通过"

            with pytest.raises(Exception) as excinfo:
                await mgmt.call("world.package.save", path="空壳.json", package=package)
            assert "未通过校验" in str(excinfo.value)
            assert not (root / "packages" / "空壳.json").exists(), "校验失败不得写盘"

            good = sample_package()
            saved = await mgmt.call("world.package.save", path="greytide.json", package=good)
            assert saved["path"].endswith("greytide.json")
            loaded = await mgmt.call("world.package.load", path="greytide.json")
            assert loaded["package"]["meta"]["original_name"] == "灰潮纪"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_generate_package_over_mgmt_with_scripted_llm(root) -> None:
    good = json.dumps(sample_package(), ensure_ascii=False)
    async with running_core(root, replies=[good]) as harness:
        mgmt = await open_mgmt(harness)
        try:
            result = await mgmt.call("world.package.generate", brief="随便描述", name="灰潮纪", timeout=60)
            assert result["valid"] is True
            assert result["errors"] == []
            assert result["candidate"]["world"]["axioms"]
            assert not list((root / "packages").glob("*.json")), "生成候选不落盘"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_generate_retries_once_with_error_feedback(root) -> None:
    broken = sample_package()
    broken["world"]["axioms"][0]["text"] = ""  # 设定核心段落有缺陷
    good = json.dumps(sample_package(), ensure_ascii=False)
    async with running_core(root, replies=[json.dumps(broken, ensure_ascii=False), good, good, good]) as harness:
        mgmt = await open_mgmt(harness)
        try:
            result = await mgmt.call("world.package.generate", brief="随便描述", timeout=60)
            assert result["valid"] is True, result["errors"]
            retry_prompt = harness.fake.calls[1]
            assert "公理内容为空" in retry_prompt[-1]["content"], "第一次的错误清单必须回灌给模型"
            assert "未通过" in retry_prompt[-1]["content"]
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_generate_keeps_candidate_out_of_disk_when_invalid(root) -> None:
    async with running_core(root, replies=["{}"]) as harness:
        mgmt = await open_mgmt(harness)
        try:
            result = await mgmt.call("world.package.generate", brief="随便描述", timeout=60)
            assert result["valid"] is False
            assert result["errors"], "空壳候选必须带出逐条原因"
            assert not list((root / "packages").glob("*.json")), "未通过校验的候选不得落盘"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_card_template_confirm_and_instance_create(root) -> None:
    package = sample_package()
    write_json(root / "packages" / "greytide.json", package)
    async with running_core(root) as harness:
        mgmt = await open_mgmt(harness)
        try:
            card = (await mgmt.call("world.card.template", package_path="greytide.json", name="堤禾"))["card"]
            assert card["meta"]["confirmed"] is False
            assert card["identity"]["race_id"] == package["races"][0]["id"]

            with pytest.raises(Exception) as excinfo:
                await mgmt.call("world.card.confirm", package_path="greytide.json", card=card)
            assert "不能确认" in str(excinfo.value)

            card = sample_card(package, confirmed=False)
            confirmed = await mgmt.call(
                "world.card.confirm", package_path="greytide.json", card=card, card_path="tihe.json"
            )
            assert confirmed["card"]["meta"]["confirmed"] is True
            assert (root / "packages" / "tihe.json").exists()

            created = await mgmt.call(
                "instance.create", package_path="greytide.json", card_paths=["tihe.json"]
            )
            info = created["instance"]
            assert info["name"] == "灰潮纪"
            assert info["moment"] == package["calendar"]["initial_moment"]

            detail = await mgmt.call("instance.info", id=info["id"])
            assert detail["characters"][0]["name"] == "堤禾"
            assert detail["timelines"][0]["state"] == "frozen"
            assert "world_package" not in json.dumps(detail), "实例信息不含世界内部内容"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_instance_export_import_and_conflict(root) -> None:
    package = sample_package()
    write_json(root / "packages" / "greytide.json", package)
    write_json(root / "packages" / "tihe.json", sample_card(package))
    async with running_core(root) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info = (
                await mgmt.call("instance.create", package_path="greytide.json", card_paths=["tihe.json"])
            )["instance"]
            exported = await mgmt.call("instance.export", id=info["id"], path="backup.isekai.json")
            assert exported["manifest"]["counts"]["sessions"] == 0
            assert (root / "packages" / "backup.isekai.json").exists()

            imported = (await mgmt.call("instance.import", path="backup.isekai.json"))["instance"]
            assert imported["name"] == "灰潮纪_2", "同名实例存在时自动追加序号"
            assert imported["imported"] is True

            names = [item["name"] for item in (await mgmt.call("instance.list"))["instances"]]
            assert names == ["灰潮纪", "灰潮纪_2"]

            with pytest.raises(Exception) as excinfo:
                await mgmt.call("instance.rename", id=imported["id"], name="灰潮纪")
            assert "已被占用" in str(excinfo.value)

            renamed = (await mgmt.call("instance.rename", id=imported["id"], name="盐滩纪"))["instance"]
            assert renamed["name"] == "盐滩纪"

            await mgmt.call("instance.delete", id=info["id"])
            remaining = [item["name"] for item in (await mgmt.call("instance.list"))["instances"]]
            assert remaining == ["盐滩纪"]
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_mgmt_connection_survives_operation_errors(root) -> None:
    async with running_core(root) as harness:
        mgmt = await open_mgmt(harness)
        try:
            with pytest.raises(Exception):
                await mgmt.call("world.package.load", path="不存在.json")
            with pytest.raises(Exception):
                await mgmt.call("instance.info", id="in-nope")
            with pytest.raises(Exception):
                await mgmt.call("world.未知操作")
            listed = await mgmt.call("instance.list")
            assert listed["instances"] == [], "出错后管理连接仍可用"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_generate_respects_call_budget(root) -> None:
    """用量预算（§2.4）：达到确认上限即暂停并保留进度，不靠无限重试扩支。"""
    async with running_core(root, replies=[json.dumps(sample_package(), ensure_ascii=False)]) as harness:
        mgmt = await open_mgmt(harness)
        try:
            result = await mgmt.call("world.package.generate", brief="x", max_calls=1, timeout=60)
            assert result["usage"] == {"calls": 1, "limit": 1, "paused": True}
            assert result["valid"] is False
            assert any("已达确认的调用上限" in item for item in result["errors"])
            assert len(harness.fake.calls) == 1, "达到上限后不再调用模型"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_draft_roundtrip_and_discard(root) -> None:
    """草稿态（§2.4）：允许未通过校验的候选暂存、继续与丢弃；不进正式包列表。"""
    async with running_core(root) as harness:
        mgmt = await open_mgmt(harness)
        try:
            await mgmt.call(
                "world.draft.save",
                name="盐滩纪-package",
                kind="package",
                payload={"meta": {"original_name": "草稿世界"}},
                errors=["world.axioms: 至少一条世界公理（阻断项）"],
            )
            leftovers = [
                item.name for item in (root / "packages").glob("*.json") if not item.name.endswith(".draft.json")
            ]
            assert leftovers == [], "草稿不冒充正式包"
            loaded = await mgmt.call("world.draft.load", name="盐滩纪-package")
            assert loaded["draft"]["errors"], "草稿保留未通过原因"
            drafts = (await mgmt.call("world.package.list"))["packages"]
            assert drafts == [], "草稿不进世界包列表"
            assert [item["name"] for item in (await mgmt.call("world.draft.list"))["drafts"]] == ["盐滩纪-package"]

            await mgmt.call("world.draft.discard", name="盐滩纪-package")
            assert (await mgmt.call("world.draft.list"))["drafts"] == []
            with pytest.raises(Exception):
                await mgmt.call("world.draft.load", name="盐滩纪-package")
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_export_is_atomic_on_disk(root) -> None:
    """导出先写临时文件再发布：不留下半截产物（§7.1）。"""
    package = sample_package()
    write_json(root / "packages" / "greytide.json", package)
    write_json(root / "packages" / "tihe.json", sample_card(package))
    async with running_core(root) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info = (await mgmt.call("instance.create", package_path="greytide.json", card_paths=["tihe.json"]))[
                "instance"
            ]
            await mgmt.call("instance.export", id=info["id"], path="out.isekai.json")
            files = sorted(item.name for item in (root / "packages").iterdir())
            assert "out.isekai.json" in files
            assert not [name for name in files if name.endswith(".tmp")], "不留临时文件"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_opening_an_instance_reports_compatibility(root) -> None:
    """打开实例时的兼容检查（§7.6）：不兼容即 blocked，检查本身不改状态。"""
    import sqlite3

    package = sample_package()
    write_json(root / "packages" / "greytide.json", package)
    write_json(root / "packages" / "tihe.json", sample_card(package))
    async with running_core(root) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info = (await mgmt.call("instance.create", package_path="greytide.json", card_paths=["tihe.json"]))[
                "instance"
            ]
            assert info["compatibility"] == "compatible"
            # 模拟「旧格式实例」：直接改库中记录的数据格式版本
            with sqlite3.connect(root / "data" / "isekai.db") as conn:
                conn.execute("UPDATE instance SET data_format='9.0' WHERE id=?", (info["id"],))
                conn.commit()
            detail = await mgmt.call("instance.info", id=info["id"])
            assert detail["instance"]["compatibility"] == "blocked"
            assert "主版本不兼容" in detail["instance"]["compatibility_note"]
            assert detail["timelines"][0]["state"] == "frozen", "检查不改动实例状态"
        finally:
            await mgmt.close()


@pytest.mark.asyncio
async def test_instance_setting_exposes_locked_snapshot(root) -> None:
    package = sample_package()
    write_json(root / "packages" / "greytide.json", package)
    write_json(root / "packages" / "tihe.json", sample_card(package))
    async with running_core(root) as harness:
        mgmt = await open_mgmt(harness)
        try:
            info = (
                await mgmt.call("instance.create", package_path="greytide.json", card_paths=["tihe.json"])
            )["instance"]
            setting = (await mgmt.call("instance.setting", id=info["id"]))["setting"]
            assert setting["original_name"] == "灰潮纪"
            assert setting["world_package"]["meta"]["package_id"] == package["meta"]["package_id"]
            # 改文件不追溯实例
            edited = json.loads((root / "packages" / "greytide.json").read_text(encoding="utf-8"))
            edited["world"]["axioms"][0]["text"] = "被改过的公理"
            write_json(root / "packages" / "greytide.json", edited)
            again = (await mgmt.call("instance.setting", id=info["id"]))["setting"]
            assert again["world_package"]["world"]["axioms"][0]["text"].startswith("潮汐每三十日")
        finally:
            await mgmt.close()
