"""随发行的样例世界包（`examples/sample_world/`）的行为验收。

判据：这一份样例**真的能走完用户路径**——导入世界包 → 导入并审定两张角色卡 → 创建实例 →
激活 → 打开联络面时是「可联络」，且两位角色都能被选中。样例是随发行内容，不许腐烂。
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import bind_thread, open_mgmt, running_core

SAMPLE = Path(__file__).resolve().parents[1] / "examples" / "sample_world"
PACKAGE_FILE = "huichao.json"
CARD_FILES = ("huichao.card1.json", "huichao.card2.json")


def _copy_into(root: Path, name: str) -> Path:
    """样例按只读资产交付：导入走文件副本，不动仓库里那份。"""
    target = root / name
    target.write_text(json.dumps(json.loads((SAMPLE / name).read_text(encoding="utf-8")),
                                 ensure_ascii=False), encoding="utf-8")
    return target


async def test_sample_world_imports_confirmes_and_creates_a_talkable_instance(tmp_path) -> None:
    package_file = _copy_into(tmp_path, PACKAGE_FILE)
    card_files = [_copy_into(tmp_path, name) for name in CARD_FILES]

    async with running_core(tmp_path) as h:
        mgmt = await open_mgmt(h)
        try:
            imported = await mgmt.call("world.package.import", source_path=str(package_file))
            assert imported["imported"] == PACKAGE_FILE
            saved_package = str(h.cfg.paths.packages / PACKAGE_FILE)

            for path in card_files:
                await mgmt.call("world.card.import", source_path=str(path), package_path=saved_package)
                await mgmt.call("world.card.confirm", package_path=saved_package, card_path=str(path))

            info = (await mgmt.call(
                "instance.create",
                package_path=saved_package,
                card_paths=[str(path) for path in card_files],
                display_name="灰潮纪",
            ))["instance"]
            detail = await mgmt.call("instance.info", id=info["id"])
            names = [str(row["name"]) for row in detail["characters"]]
            assert names == ["堤禾", "潮生"], f"样例的两张卡都要装上，读数={names}"
            timeline_id = str(detail["timelines"][0]["id"])

            # 创建即冻结：先激活（README 给的路径也是这几步）
            await mgmt.call("runtime.activate", instance_id=info["id"], timeline_id=timeline_id)

            face = await mgmt.call("story.enter", instance_id=info["id"], timeline_id=timeline_id,
                                   character_id="cc-堤禾")
            assert face["product_state"] == "available", face
            assert not face["steps"][-1]["done"], "还没进会话：最后一步不该是就绪"

            # 进会话（登记通道 → 建会话 → 绑 thread，与客户端走的同一条路）
            client, _bound = await bind_thread(h, mgmt, channel_id="probe", thread_id="main",
                                               instance=info["id"], timeline=timeline_id,
                                               character="cc-堤禾")
            try:
                ready = await mgmt.call("story.enter", instance_id=info["id"], timeline_id=timeline_id,
                                        character_id="cc-堤禾")
                assert all(step["done"] for step in ready["steps"]), \
                    f"样例走完六步应该全部就绪：{[(s['key'], s['done']) for s in ready['steps']]}"
                assert "堤禾" in [row["name"] for row in ready["characters"]]
            finally:
                await client.close()

            second = await mgmt.call("story.scene", instance_id=info["id"], timeline_id=timeline_id,
                                     character_id="cc-潮生")
            assert second["product_state"] == "available", second
        finally:
            await mgmt.close()
