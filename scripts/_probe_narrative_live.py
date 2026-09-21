"""叙事中介层真机验收：真实模型跑一遍主动发言与自然开场（NARRATIVE_LAYER_SPEC）。

用临时库，不碰本机开发数据；key 取自项目 config（不入库）。
看四件事：候选能不能编成单元、生成文本是否自然、后验检查拦不拦得住、开场块有没有带上。
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from isekai_core.app import build_llm
from isekai_core.config import load_config
from isekai_core.runtime import life as life_mod
from isekai_core.runtime.service import from_config
from isekai_core.store import Store
from isekai_core.world.example import DAY, example_card, example_package
from isekai_core.world.instances import create_instance


class Recording:
    """记录每次真实调用：分开看生成与后验检查。"""

    def __init__(self, inner) -> None:  # noqa: ANN001
        self.inner = inner
        self.calls: list[tuple[str, str]] = []

    async def chat(self, prompt, **kwargs):  # noqa: ANN001, ANN003
        text = await self.inner.chat(prompt, **kwargs)
        self.calls.append((str(prompt), str(text)))
        return text

    async def aclose(self) -> None:
        close = getattr(self.inner, "aclose", None)
        if close is not None:
            await close()


async def main() -> None:
    cfg = load_config()
    root = Path(tempfile.mkdtemp(prefix="isekai_narrative_"))
    store = Store(root / "data" / "isekai.db")
    store.ensure_schema()
    world = from_config(cfg, store)
    llm = Recording(build_llm(cfg))

    package = example_package("灰潮纪·叙事层活体验收")
    card = example_card(package, name="堤禾")
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    character_id = str(card["meta"]["card_id"])
    instance_id = info["id"]
    world.ensure_instance(instance_id, now_real=time.time())
    world.activate(instance_id, timeline_id, now_real=time.time())
    world_s = int(world.clock_row(timeline_id)["processed_world"])
    print("实例:", instance_id, "| 线:", timeline_id, "| 角色:", character_id, "| 水位:", world_s)

    # 两条同一来源的说法：按关系规则应编进同一个单元
    for ref, text in (
        ("cl-live-1", "北堤的通行牌这三天都停发了"),
        ("cl-live-2", "盐滩的秤被收走了，市面上一时没人敢出手"),
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
        result = await world.proactive_tick(instance_id, timeline_id, llm=llm, per_day=2)
    finally:
        life_mod.activity_label = original  # type: ignore[assignment]
    print("\n主动发言结果:", result)
    for row in store.narrative_unit_list(instance_id, timeline_id, character_id=character_id):
        print("叙事单元:", {k: row[k] for k in ("id", "stage", "refs", "relation", "message_id", "audit")})

    print("\n--- 真实调用 ---")
    for index, (prompt, reply) in enumerate(llm.calls, 1):
        kind = "后验检查" if "忠实度判断" in prompt else "生成"
        print(f"[{index}] {kind}")
        print("  prompt:", prompt.replace("\n", " / ")[:600])
        print("  reply :", reply.strip().replace("\n", " ")[:300])

    fixed = store.proactive_list(instance_id, timeline_id)
    print("\n消费记账:", [row["material_ref"] for row in fixed])

    session = store.session_ensure(instance_id, timeline_id, character_id)
    prompt = world.system_prompt(session, topic="今天怎么样")
    tail = prompt.split("她最近能提起的事")[-1]
    print("\n自然开场块:", ("她最近能提起的事" + tail)[:500] if "她最近能提起的事" in prompt else "（未注入）")
    print("长问题（有明确主题）是否注入:", "她最近能提起的事" in world.system_prompt(session, topic="你们那里的通行牌是怎么发的，说来听听"))

    await llm.aclose()
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
