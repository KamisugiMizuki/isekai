"""首次使用支撑的行为验收（`docs/user-interface/ONBOARDING_AND_RECOVERY.md`）。

判据都是行为级的：真 WebSocket + 真 SQLite，只把模型换成测试替身。

1. 本机检查：受管目录、单写入者、数据格式、存储可写都真的读一遍（不是回常量）；
2. 样例：随发行样例能被复制成用户自己的材料，重复开始不覆盖已有材料，坏样例不落盘；
3. AI 连接测试：用**正在编辑的值**试调用、不写配置；认证失败照实给状态码，不猜原因；
4. 界面草稿：写 / 读 / 列 / 丢，并随实例删除一起清理；
5. 请求身份：同一身份的重复创建回到同一结果（不重复造世界）。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from conftest import open_mgmt, running_core

REPO = Path(__file__).resolve().parents[1]
SAMPLE_SRC = REPO / "examples" / "sample_world"


def _install_samples(root: Path) -> None:
    """把随发行样例放进临时根（发行件里它随包提供，测试里照抄一份）。"""
    target = root / "examples" / "sample_world"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE_SRC, target)


async def test_readiness_reports_real_local_state(tmp_path) -> None:
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        facts = await mgmt.call("app.readiness")

    keys = {item["key"] for item in facts["checks"]}
    assert {"core", "data", "writer", "format", "storage", "config"} <= keys
    assert facts["ready"] is True
    data_check = next(item for item in facts["checks"] if item["key"] == "data")
    assert data_check["ok"] is True
    assert str(tmp_path / "data") in data_check["detail"]
    # Key 只回「是否已设置」：明文不进界面（§4.2 字段规则）
    assert facts["ai"]["configured"] is False
    assert "api_key" not in facts["ai"]
    assert facts["first_run"]["instances"] == 0
    assert Path(facts["paths"]["data"]).exists()


async def test_readiness_upgrades_when_ai_is_configured(tmp_path) -> None:
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)
    (tmp_path / "config" / "config.yaml").write_text(
        "llm:\n  base_url: https://api.example.com\n  model: test-model\n  api_key: sk-abcdefgh1234\n",
        encoding="utf-8",
    )
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        facts = await mgmt.call("app.readiness")

    assert facts["ai"]["configured"] is True
    assert facts["ai"]["api_key_masked"].endswith("1234")
    assert "abcdefgh" not in facts["ai"]["api_key_masked"]


async def test_sample_install_copies_material_without_overwriting(tmp_path) -> None:
    _install_samples(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        listed = await mgmt.call("world.sample.list")
        assert [item["id"] for item in listed["samples"]] == ["sample_world"]
        sample = listed["samples"][0]
        assert sample["title"] == "灰潮纪"
        assert [card["name"] for card in sample["cards"]] == ["堤禾", "潮生"]
        assert sample["installed"] is False

        first = await mgmt.call("world.sample.install", sample="sample_world", request_id="req-1")
        assert first["package_file"] == "huichao.json"
        assert [card["file"] for card in first["cards"]] == ["huichao.card1.json", "huichao.card2.json"]
        assert (tmp_path / "packages" / "huichao.json").exists()
        assert (tmp_path / "packages" / "huichao.card1.json").exists()

        # 同一身份重复点：回原结果，不再复制一遍
        again = await mgmt.call("world.sample.install", sample="sample_world", request_id="req-1")
        assert again["reused"] is True
        assert again["package_file"] == first["package_file"]

        # 换了身份重来：认已有材料，不覆盖、也不改名重来一份
        fresh = await mgmt.call("world.sample.install", sample="sample_world", request_id="req-2")
        assert fresh["package_reused"] is True
        assert [card["kept"] for card in fresh["cards"]] == [True, True]
        assert sorted(path.name for path in (tmp_path / "packages").glob("*.json")) == [
            "huichao.card1.json",
            "huichao.card2.json",
            "huichao.json",
        ]
        listed_after = await mgmt.call("world.sample.list")
        assert listed_after["samples"][0]["installed"] is True


async def test_broken_sample_is_refused(tmp_path) -> None:
    _install_samples(tmp_path)
    package_path = tmp_path / "examples" / "sample_world" / "huichao.json"
    payload = json.loads(package_path.read_text(encoding="utf-8"))
    payload["world"].pop("lexicon", None)  # 声明式字段缺一块：校验器要拦下
    package_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        try:
            await mgmt.call("world.sample.install", sample="sample_world")
        except Exception as exc:  # noqa: BLE001 —— 管理面把 invalid 返成异常
            assert "未通过校验" in str(exc) or "invalid" in str(exc).lower()
        else:  # pragma: no cover - 校验器没拦住就是缺陷
            raise AssertionError("坏样例被复制进创作目录了")
    assert not (tmp_path / "packages" / "huichao.json").exists()


async def test_ai_test_uses_edited_values_and_never_saves(tmp_path) -> None:
    async with running_core(tmp_path, replies=["可用", '{"ok": true}']) as harness:
        mgmt = await open_mgmt(harness)
        result = await mgmt.call(
            "settings.test",
            llm={"base_url": "https://api.example.com", "model": "candidate-model", "api_key": "sk-try-12345678"},
        )
        after = await mgmt.call("settings.get")

    assert result["ok"] is True
    assert result["text_ok"] is True and result["structured_ok"] is True
    assert result["model"] == "candidate-model"
    assert result["calls"] == 2 and result["calls"] <= result["call_budget"]
    assert [stage["key"] for stage in result["stages"]] == ["address", "access", "format"]
    # 测试不写配置：生效配置仍是原来那份（没有 Key）
    assert after["llm"]["model"] != "candidate-model"
    assert after["llm"]["api_key_set"] is False
    assert "sk-try-12345678" not in json.dumps(result)


async def test_ai_test_reports_status_code_from_the_response(tmp_path) -> None:
    from isekai_core.llm import LLMError

    async with running_core(tmp_path, fail_with=LLMError("llm_rejected", "HTTP 401", retryable=False)) as harness:
        mgmt = await open_mgmt(harness)
        result = await mgmt.call("settings.test", llm={"api_key": "sk-bad-12345678"})

    assert result["ok"] is False
    assert result["status"] == "failed"
    assert result["status_code"] == 401
    assert "401" in result["reason"] and "密钥" in result["reason"]
    assert result["retryable"] is False


async def test_ai_test_without_key_says_so(tmp_path) -> None:
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        result = await mgmt.call("settings.test", llm={})

    assert result["ok"] is False
    assert result["key_set"] is False
    assert "访问密钥" in result["reason"]


async def test_ui_drafts_persist_and_follow_instance_deletion(tmp_path) -> None:
    _install_samples(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        await mgmt.call("world.sample.install", sample="sample_world")
        created = await mgmt.call(
            "instance.create",
            package_path="huichao.json",
            card_paths=["huichao.card1.json"],
            display_name="草稿验收",
            request_id="create-1",
        )
        instance_id = created["instance"]["id"]
        timeline_id = created["instance"].get("timeline_id") or ""
        target = f"{instance_id}:{timeline_id}:cc-堤禾"

        await mgmt.call(
            "ui.draft.save",
            key="contact:builtin",
            module="contact",
            target=target,
            text="没发出去的一句话",
            payload={"selected": "cc-堤禾"},
        )
        loaded = await mgmt.call("ui.draft.load", key="contact:builtin")
        assert loaded["draft"]["text"] == "没发出去的一句话"
        assert loaded["draft"]["payload"] == {"selected": "cc-堤禾"}
        assert loaded["draft"]["state"] == "saved"
        listed = await mgmt.call("ui.draft.list", module="contact")
        assert [item["key"] for item in listed["drafts"]] == ["contact:builtin"]

        await mgmt.call("instance.delete", id=instance_id, confirm=True)
        gone = await mgmt.call("ui.draft.list", module="contact")
        assert gone["drafts"] == []


async def test_request_identity_returns_the_same_instance(tmp_path) -> None:
    _install_samples(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        await mgmt.call("world.sample.install", sample="sample_world")
        first = await mgmt.call(
            "instance.create",
            package_path="huichao.json",
            card_paths=["huichao.card1.json"],
            request_id="same-request",
        )
        second = await mgmt.call(
            "instance.create",
            package_path="huichao.json",
            card_paths=["huichao.card1.json"],
            request_id="same-request",
        )
        instances = await mgmt.call("instance.list")

    assert second["reused"] is True
    assert second["instance"]["id"] == first["instance"]["id"]
    assert len(instances["instances"]) == 1


async def test_ui_draft_discard_reports_whether_it_existed(tmp_path) -> None:
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        await mgmt.call("ui.draft.save", key="writing:one", module="writing", target="outline-1", text="x")
        assert (await mgmt.call("ui.draft.discard", key="writing:one"))["ok"] is True
        assert (await mgmt.call("ui.draft.discard", key="writing:one"))["ok"] is False
        try:
            await mgmt.call("ui.draft.load", key="writing:one")
        except Exception as exc:  # noqa: BLE001
            assert "没有这份草稿" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("丢掉的草稿还能读出来")
