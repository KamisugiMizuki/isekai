"""叙事图谱面板的真壳验收：临时数据根 + 假 LLM，经 CDP 驱动真壳（不碰本机数据）。

跑法：.venv/Scripts/python.exe scripts/_probe_storymap_desk.py

看五件事：
  1) 面板画出真数据（节点 / 关系 / 文字列表）且真占位（量几何，不看源码猜）；
  2) 讲出口的节点带正文，没讲出口的只留记号（内容不许上图）；
  3) 她没有素材的实例 → 空态；零实例 → 回到「未选择实例或时间线」；
  4) 「挑候选」从她讲过的线索里挑片段，点一条即选定；
  5) 选定 → 披露 → 候选里那条消失（授权仍走 disclose.confirm）。
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
import _audit2_desk as desk  # noqa: E402


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class Stub:
    """不联网的生成器：生成与后验检查共用同一个固定回复。"""

    def __init__(self, reply: str) -> None:
        self.reply = reply

    async def chat(self, prompt, **kwargs):  # noqa: ANN001, ANN003
        return self.reply


async def seed(root: Path) -> dict:
    """走真实链路造两条线索：一条讲出口（主动发言）、一条没讲出口（后验拦下）。"""
    from isekai_core.config import load_config
    from isekai_core.runtime import life as life_mod
    from isekai_core.runtime.service import from_config
    from isekai_core.store import Store
    from isekai_core.world.example import example_card, example_package
    from isekai_core.world.instances import create_instance

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    world = from_config(cfg, store)

    package = example_package("图谱验收世界")
    first = example_card(package, name="堤禾")
    second = example_card(package, name="潮生")
    info = create_instance(store, package, [first, second])
    instance_id = info["id"]
    timeline_id = store.timeline_list(instance_id)[0]["id"]
    character_id = str(first["meta"]["card_id"])
    # 第二个实例：同样的形状，但一条素材都没有 → 面板空态
    empty_package = example_package("空象限世界")
    empty_info = create_instance(
        store,
        empty_package,
        [example_card(empty_package, name="甲"), example_card(empty_package, name="乙")],
    )
    world.ensure_instance(instance_id, now_real=time.time())
    world.activate(instance_id, timeline_id, now_real=time.time())
    world_s = int(world.clock_row(timeline_id)["processed_world"])
    for ref, text in (
        ("cl-map-1", "北堤的通行牌这三天都停发了"),
        ("cl-map-2", "盐滩的秤被收走了，市面上一时没人敢出手"),
    ):
        store.knowledge_put(
            {
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
                "id": f"kn-{ref}",
                "world_seconds": world_s,
                "kind": "claim",
                "target": ref,
                "source": "src-1",
                "stance": "recorded",
                "text": text,
            }
        )
    original = life_mod.activity_label
    life_mod.activity_label = lambda window: "日间活动"  # type: ignore[assignment]
    try:
        await world.proactive_tick(instance_id, timeline_id, llm=Stub("盐滩的秤被收走了，我听说的。"), per_day=2)
        store.knowledge_put(
            {
                "instance_id": instance_id,
                "timeline_id": timeline_id,
                "character_id": character_id,
                "id": "kn-cl-map-3",
                "world_seconds": world_s,
                "kind": "claim",
                "target": "cl-map-3",
                "source": "src-2",
                "stance": "recorded",
                "text": "驿站新到一份灾年编年的补页",
            }
        )
        # 数字越界 → 后验拦下 → 只留「没讲出口」的记号
        await world.proactive_tick(instance_id, timeline_id, llm=Stub("驿站到了 7 份补页。"), per_day=2)
    finally:
        life_mod.activity_label = original  # type: ignore[assignment]
    rows = store.narrative_unit_list(instance_id, timeline_id, character_id=character_id)
    store.close()
    return {
        "instance_id": instance_id,
        "timeline_id": timeline_id,
        "character_id": character_id,
        "other_id": str(second["meta"]["card_id"]),
        "empty_instance_id": empty_info["id"],
        "empty_name": empty_info["name"],
        "name": info["name"],
        "units": [(row["id"], row["stage"]) for row in rows],
    }


# 读面板：一次取全（含几何——渲染没落位的话宽度会是 0）
READ_PANEL = """(()=>{const svg=document.querySelector('#story-graph svg');
const dot=document.querySelector('#story-graph svg circle');
const rect=svg?svg.getBoundingClientRect():null;
return {note:(document.getElementById('story-note')||{}).textContent||'',
  dots:document.querySelectorAll('#story-graph svg circle').length,
  held:document.querySelectorAll('#story-graph svg circle.held').length,
  edges:document.querySelectorAll('#story-graph svg path').length,
  items:[...document.querySelectorAll('#story-list li')].map(li=>li.textContent),
  w:rect?Math.round(rect.width):0, h:rect?Math.round(rect.height):0,
  dotW:dot?Math.round(dot.getBoundingClientRect().width*10)/10:0,
  cands:[...document.querySelectorAll('#disclose-candidates li')].map(li=>li.textContent),
  candNote:(document.getElementById('disclose-suggest-note')||{}).textContent||'',
  discloseNote:(document.getElementById('disclose-note')||{}).textContent||'',
  cancelHidden:document.getElementById('disclose-cancel').classList.contains('hidden')};})()"""


async def panel(cdp: desk.Cdp, *, want: str = "", timeout: float = 45.0) -> dict:
    """等面板出内容（渲染是异步的：轮询而不是固定 sleep）。"""
    deadline = time.time() + timeout
    value: dict = {}
    while time.time() < deadline:
        value = await cdp.js(READ_PANEL) or {}
        if value.get("note") and (not want or want in str(value.get("note"))):
            return value
        await asyncio.sleep(0.4)
    return value


async def wait_js(cdp: desk.Cdp, expr: str, *, timeout: float = 30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if await cdp.js(expr):
            return True
        await asyncio.sleep(0.4)
    return False


async def click(wait_id: str, cdp: desk.Cdp, element_id: str) -> None:
    await cdp.js(f"document.getElementById({json.dumps(element_id)}).click()")
    if wait_id:
        await wait_js(cdp, f"!!document.getElementById({json.dumps(wait_id)})")


async def main() -> None:
    root = desk.make_root("storymap")
    seeded = await seed(root)
    print("临时根:", root)
    print("播种:", seeded["units"])

    proc, cdp, _targets = await desk.boot_shell(root, port=free_port())
    problems: list[str] = []
    try:
        await cdp.js(desk.STUB)  # 原生 confirm/对话框 stub：删除这类破坏性路径要用
        await cdp.pane("manage")
        listed = await wait_js(
            cdp,
            "(()=>{const s=document.getElementById('inst-select');"
            "return s && s.options.length >= 2;})()",
        )
        assert listed, "壳里没有列出这两个实例"

        # 1+2) 有线索的实例：节点、记号、正文、几何
        await cdp.select("inst-select", seeded["instance_id"])
        first = await panel(cdp, want="讲出口")
        print("[面板]", json.dumps({k: first[k] for k in ("note", "dots", "held", "edges", "w", "h", "dotW")},
                                   ensure_ascii=False))
        print("[列表]", json.dumps(first["items"], ensure_ascii=False))
        if not first["dots"]:
            problems.append("图谱没有节点")
        if first["held"] != 1:
            problems.append(f"「没讲出口」记号数={first['held']}（应 1）")
        if first["w"] <= 0 or first["h"] <= 0 or first["dotW"] <= 0:
            problems.append(f"图没落位：{first['w']}x{first['h']} 点宽={first['dotW']}")
        if not any("世界第" in item for item in first["items"]):
            problems.append("文字列表没有线索")
        if any("补页" in item for item in first["items"]):
            problems.append("「没讲出口」的内容漏进了列表")

        # 3a) 空素材的实例 → 空态
        await cdp.select("inst-select", seeded["empty_instance_id"])
        blank = await panel(cdp, want="讲出口 0 条")
        print("[空态]", json.dumps({"note": blank["note"], "dots": blank["dots"], "items": blank["items"]},
                                   ensure_ascii=False))
        if "讲出口 0 条" not in blank["note"] or blank["dots"] != 0:
            problems.append(f"空实例没走空态：{blank['note']} / {blank['dots']} 点")
        if not any("还没讲过什么" in item for item in blank["items"]):
            problems.append("空态没有说明文案")

        # 4) 披露候选：从她讲过的线索里挑
        await cdp.select("inst-select", seeded["instance_id"])
        await panel(cdp, want="讲出口 1 条")
        await cdp.select("disclose-to", seeded["other_id"])
        await click("", cdp, "disclose-suggest")
        ok = await wait_js(cdp, "document.querySelectorAll('#disclose-candidates li').length > 0")
        cand = await panel(cdp, want="讲出口 1 条")
        print("[候选]", json.dumps({"note": cand["candNote"], "cands": cand["cands"]}, ensure_ascii=False))
        if not ok:
            problems.append(f"没挑出候选：{cand['candNote']}")
        else:
            # 5) 选定 → 披露
            await cdp.js("document.querySelector('#disclose-candidates li button').click()")
            await wait_js(cdp, "(document.getElementById('disclose-note').textContent||'').includes('已选定')")
            picked = await panel(cdp, want="讲出口 1 条")
            await click("", cdp, "disclose-confirm")
            done = await wait_js(
                cdp, "(document.getElementById('disclose-note').textContent||'').includes('已披露')")
            after = await panel(cdp, want="讲出口 1 条")
            print("[披露]", json.dumps({"note": after["discloseNote"], "剩余候选": len(after["cands"])},
                                       ensure_ascii=False))
            if not done:
                problems.append(f"披露没走通：{after['discloseNote']}")
            if after["cands"]:
                problems.append(f"披露过的片段仍在候选里：{after['cands']}")
            if picked["cancelHidden"]:
                problems.append("选定后没出现「取消选择」")

        # 3b) 零实例 → 面板回到「未选择实例或时间线」（删两个实例，走真界面）
        for instance_id in (seeded["empty_instance_id"], seeded["instance_id"]):
            await cdp.select("inst-select", instance_id)
            name = await cdp.js(
                f"document.getElementById('inst-select').selectedOptions[0].textContent.split('｜')[0]")
            await cdp.js(
                "(function(){const i=document.getElementById('inst-delete-name');"
                f"i.value={json.dumps(str(name))}; i.dispatchEvent(new Event('input'));}})()")
            await cdp.js("document.getElementById('inst-delete').click()")
            gone = await wait_js(
                cdp,
                "(()=>{const s=document.getElementById('inst-select');"
                f"return ![...s.options].some(o=>o.value==={json.dumps(instance_id)});}})()",
                timeout=30.0,
            )
            if not gone:
                # 删除失败不静默：把提示槽读出来（跨组事件会写页顶）
                problems.append(
                    f"删除实例失败：{await desk.note_of(cdp, 'world-note', 'inst-delete-note', 'inst-note')}")
                break
        else:
            blank2 = await panel(cdp, want="未选择实例")
            print("[零实例]", json.dumps({"note": blank2["note"], "dots": blank2["dots"]}, ensure_ascii=False))
            if "未选择实例" not in blank2["note"] or blank2["dots"] != 0:
                problems.append(f"零实例没有清空面板：{blank2['note']} / {blank2['dots']} 点")
        print("\n结果:", "PASS" if not problems else "FAIL", "；".join(problems))
    finally:
        desk.kill_tree()
        try:
            proc.terminate()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    asyncio.run(main())
