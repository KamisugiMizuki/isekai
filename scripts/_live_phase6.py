"""阶段 6 真机验收：制度变化沿合法事件效果发生，且只有获知那件事的角色看见它。"""

from __future__ import annotations

import time

from isekai_core.config import load_config
from isekai_core.store import Store
from isekai_core.runtime.service import from_config
from isekai_core.world.example import DAY, example_card, example_package
from isekai_core.world.instances import create_instance


def main() -> None:
    cfg = load_config()
    store = Store(cfg.paths.db)
    store.ensure_schema()
    world = from_config(cfg, store)

    package = example_package("灰潮纪·制度二卡验收")
    card = example_card(package, name="堤禾")
    outsider = example_card(package, name="堤砚")
    info = create_instance(store, package, [card, outsider])
    timeline = store.timeline_list(info["id"])[0]
    instance_id, timeline_id = info["id"], timeline["id"]
    insider_id = str(card["meta"]["card_id"])
    outsider_id = str(outsider["meta"]["card_id"])
    print("实例:", instance_id, "线:", timeline_id, "| 角色:", insider_id, outsider_id)

    world.ensure_instance(instance_id, now_real=time.time())
    print(
        "初始制度状态:",
        [(r["office_id"], r["name"], r["holder"]) for r in store.institution_list(instance_id, timeline_id)],
    )

    for other in store.instance_list():
        for line in store.timeline_list(other["id"]):
            if line["id"] != timeline_id and str(line.get("state") or "") == "active":
                world.freeze(other["id"], line["id"], now_real=time.time())
    world.activate(instance_id, timeline_id, rate=int(cfg.runtime.rate_max), now_real=time.time())
    for step in range(4):
        result = world.advance(instance_id, timeline_id, now_real=time.time() + step * 60)
        print(f"推进 {step + 1}:", {k: v for k, v in result.items() if k != "batches"})
        time.sleep(0.2)

    truth = store.institution_list(instance_id, timeline_id)
    print("世界真值:", [(r["office_id"], r["name"], r["holder"], r["source"]) for r in truth])

    claims = {str(row["id"]): str(row["event_id"]) for row in store.claim_list(instance_id, timeline_id)}
    for who, label in ((insider_id, "堤禾"), (outsider_id, "堤砚")):
        knowledge = store.knowledge_window(instance_id, timeline_id, who, until=DAY * 5000, limit=40)
        reachable = {str(row.get("target") or "") for row in knowledge}
        reachable |= {str(row.get("id") or "") for row in knowledge}
        reachable |= {claims[item] for item in list(reachable) if item in claims}
        snapshot = world.character_snapshot(instance_id, timeline_id, who, world_seconds=DAY * 5000)
        print(
            f"{label}：获知 {len(knowledge)} 条，能触达的事件 {len([x for x in reachable if x])} 个 →",
            [(item["name"], item["value"]) for item in snapshot["institutions"]],
        )


if __name__ == "__main__":
    main()
