"""编剧层随实例导出件走（WRITING_ASSISTANT_SPEC §4.1 / §八）。

判据：导出件带**引用闭包**里的大纲定义 + 各线绑定状态与候选 / 决定；导入后新实例能用同一份
定义（id 不重铸）、状态与候选落在新线上；本机已有同名大纲时**保留本机那份**并如实回报。
"""

from __future__ import annotations

import json

from isekai_core.runtime.service import RuntimeService
from isekai_core.world.portable import build_container, import_instance
from test_runtime import make_instance, store, world  # noqa: F401

OUTLINE = {
    "id": "ol-export",
    "name": "导出大纲",
    "items": [
        {
            "id": "it-node",
            "layer": "required_node",
            "statement": "北堤第一次放行",
            "scope": "world",
            "success_criteria": "世界事件里有放行",
            "status": "unstarted",
        }
    ],
}


def _seed_writing(store, instance_id: str, timeline_id: str, character_id: str) -> None:
    store.wa_outline_put({"id": OUTLINE["id"], "name": OUTLINE["name"], "payload": json.dumps(OUTLINE, ensure_ascii=False)})
    store.wa_state_put(
        {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "outline_id": OUTLINE["id"],
            "observers": json.dumps([character_id]),
            "chapter": "第一章",
            "items": json.dumps([{"id": "it-node", "status": "in_progress"}]),
            "evaluated_world": 0,
            "evaluated_generation": 1,
        }
    )
    store.wa_candidate_put(
        {
            "instance_id": instance_id,
            "timeline_id": timeline_id,
            "id": "cd-locked",
            "outline_id": OUTLINE["id"],
            "kind": "text",
            "item_refs": json.dumps(["it-node"]),
            "title": "锁定的段落",
            "text": "北堤的闸门在第三日清晨开了半扇。",
            "status": "approved",
        }
    )


def test_outline_state_and_candidate_survive_export_import(store) -> None:  # noqa: F811
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    _seed_writing(store, info["id"], timeline_id, character_id)

    container = build_container(store, info["id"])
    writing = container["runtime"]["writing"]
    assert [row["id"] for row in writing["outlines"]] == [OUTLINE["id"]], "定义按引用闭包随件"
    assert len(writing["states"]) == 1 and len(writing["candidates"]) == 1

    imported = import_instance(store, container, display_name="编剧副本")
    assert "writing_kept_outlines" not in imported, "本机已有同一份定义：不算被保留"

    new_line = store.timeline_list(imported["id"])[0]["id"]
    states = store.wa_state_list(imported["id"], new_line)
    assert [row["outline_id"] for row in states] == [OUTLINE["id"]], "绑定状态落在导入线的新 id 上"
    assert json.loads(states[0]["items"]) == [{"id": "it-node", "status": "in_progress"}]
    candidates = store.wa_candidate_list(imported["id"], new_line)
    assert [row["id"] for row in candidates] == ["cd-locked"]
    assert candidates[0]["text"] == "北堤的闸门在第三日清晨开了半扇。"
    assert candidates[0]["status"] == "approved", "锁定的文本草稿随件，不因导入丢失"
    assert store.wa_outline_list() and len(store.wa_outline_list()) == 1, "定义按 id 幂等，不重复落库"


def test_existing_outline_is_kept_not_overwritten(store) -> None:  # noqa: F811
    world_service = RuntimeService(store)
    info, timeline_id, character_id = make_instance(store, world_service)
    _seed_writing(store, info["id"], timeline_id, character_id)
    container = build_container(store, info["id"])

    # 本机在这之后改了同一份大纲（作者资产是活的）：导入端不许拿导出件里的旧版覆盖它
    local = dict(OUTLINE, name="本机改过的名字")
    store.wa_outline_put({"id": OUTLINE["id"], "name": local["name"], "payload": json.dumps(local, ensure_ascii=False)})

    imported = import_instance(store, container, display_name="编剧副本二")
    assert imported["writing_kept_outlines"] == [OUTLINE["id"]], "内容不同要如实回报，不静默替换"
    stored = store.wa_outline_get(OUTLINE["id"])
    assert json.loads(stored["payload"])["name"] == "本机改过的名字", "保留本机那份"
