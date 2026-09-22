"""「尝试世界变化」的界面侧契约（USER_INTERFACE_DESIGN §9.3 / EVENT_ENGINE_SPEC §八）。

真 WebSocket + 真 SQLite，只把模型换成测试替身。判据：

1. 可选对象清单给的是「已登记的 id + 可读名称 + 类别」，界面不必要求用户填标识；
2. 草案只翻译与校验，**确认前不产生任何世界变化**；
3. 确认后原子建一条**暂停**的新线（原线不动），事件落在那条新线上。
"""

from __future__ import annotations

import shutil
from pathlib import Path

from conftest import open_mgmt, running_core

REPO = Path(__file__).resolve().parents[1]
SAMPLE_SRC = REPO / "examples" / "sample_world"


def _install_samples(root: Path) -> None:
    target = root / "examples" / "sample_world"
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SAMPLE_SRC, target)


async def _seed(mgmt) -> dict:
    await mgmt.call("world.sample.install", sample="sample_world")
    created = await mgmt.call(
        "instance.create",
        package_path="huichao.json",
        card_paths=["huichao.card1.json", "huichao.card2.json"],
    )
    instance_id = str(created["instance"]["id"])
    info = await mgmt.call("instance.info", id=instance_id)
    timeline_id = str(info["timelines"][0]["id"])
    cards = {str(item["name"]): str(item["card_id"]) for item in info["characters"]}
    return {"instance_id": instance_id, "timeline_id": timeline_id, "cards": cards}


async def test_targets_lists_registered_objects_with_labels(tmp_path) -> None:
    _install_samples(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        seeded = await _seed(mgmt)
        result = await mgmt.call(
            "world.event.targets", instance_id=seeded["instance_id"], timeline_id=seeded["timeline_id"]
        )

    characters = {item["label"] for item in result["groups"]["character"]}
    assert characters == {"堤禾", "潮生"}
    kinds = {item["id"]: item["label"] for item in result["effect_kinds"]}
    assert "activity_constraint" in kinds and kinds["activity_constraint"] == "活动受限"
    # 目标清单只给 id 与名称：内部正文（实情 / 史料）不进这一面
    assert all(set(item) == {"id", "label"} for items in result["groups"].values() for item in items)


async def test_event_draft_previews_then_confirm_creates_paused_timeline(tmp_path) -> None:
    _install_samples(tmp_path)
    async with running_core(tmp_path) as harness:
        mgmt = await open_mgmt(harness)
        seeded = await _seed(mgmt)
        instance_id, timeline_id = seeded["instance_id"], seeded["timeline_id"]
        target = seeded["cards"]["堤禾"]

        draft = await mgmt.call(
            "event.draft",
            instance_id=instance_id,
            timeline_id=timeline_id,
            payload={
                "intent": "让堤禾这几天走不开",
                "when": "now",
                "effects": [
                    {
                        "kind": "activity_constraint",
                        "target": target,
                        "expiry": "with_cause",
                        "value": "堤上事务缠身，这几日走不开",
                    }
                ],
            },
        )
        assert draft["accepted"] is True, draft
        draft_id = str(draft["draft"]["draft_id"])
        assert draft["draft"]["intent"] == "让堤禾这几天走不开"
        before = await mgmt.call("instance.info", id=instance_id)
        assert len(before["timelines"]) == 1, "草案阶段不该建线"

        confirmed = await mgmt.call(
            "event.confirm", instance_id=instance_id, draft_id=draft_id, name="用户引入：堤上事务"
        )
        after = await mgmt.call("instance.info", id=instance_id)
        new_line = next(item for item in after["timelines"] if str(item["id"]) == str(confirmed["timeline_id"]))
        events = await mgmt.call(
            "runtime.history.read",
            instance_id=instance_id,
            timeline_id=str(new_line["id"]),
            filters={"kinds": ["event"]},
        )

        # 同一身份再确认：回原结果，不建第二条线
        again = await mgmt.call("event.confirm", instance_id=instance_id, draft_id=draft_id)
        final = await mgmt.call("instance.info", id=instance_id)

    assert confirmed["timeline_id"] == new_line["id"]
    assert str(new_line["state"]) == "frozen", "新线先暂停：启动是显式动作"
    assert len(final["timelines"]) == 2
    assert again.get("reused") is True
    assert events["items"], "确认后事件应落在新线上"
