"""验收比值的**修复前后同实例交替对照**：第 10 日实例 vs 第 1500 日实例。

`acceptance_ab.py` 只能测「当前代码的比值」；本脚本把**修复前**（还原那两处）与**修复后**
放在同一时间段内交替，于是能给出「修复把比值从多少改到多少」——这才是可采信的验收口径。

用法：python .hermes/lead_accept_ab.py <fresh_root> <aged_root> [days] [rounds]
"""

from __future__ import annotations

import asyncio
import importlib.util
import statistics
import sys
import time
from pathlib import Path

FRESH = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path('.hermes/acceptH').resolve()
AGED = Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else Path('.hermes/ab').resolve()
DAYS = int(sys.argv[3]) if len(sys.argv) > 3 else 5
ROUNDS = int(sys.argv[4]) if len(sys.argv) > 4 else 3
DAY = 86400
ROOT = Path('.').resolve()
BEFORE_SRC = Path('.hermes/_store_before2.py').resolve()

#: 与 `lead_s3_ab2.py` 保持一致的「还原那两处」定义
NEW_UPDATE = '''                if instance_id_:
                    self._conn.execute(
                        """UPDATE effect_state SET active=0, cleared_at=?
                           WHERE instance_id=? AND id=? AND timeline_id=? AND active=1""",
                        (cleared_at, instance_id_, effect_id, timeline_id),
                    )
                else:
                    self._conn.execute(
                        """UPDATE effect_state SET active=0, cleared_at=?
                           WHERE id=? AND timeline_id=? AND active=1""",
                        (cleared_at, effect_id, timeline_id),
                    )
'''
OLD_UPDATE = '''                self._conn.execute(
                    """UPDATE effect_state SET active=0, cleared_at=?
                       WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)""",
                    (cleared_at, effect_id, timeline_id, instance_id_, instance_id_),
                )
'''
NEW_INDEX = 'CREATE INDEX IF NOT EXISTS ix_reaction_source_ref ON reaction(timeline_id, source_ref);\n'


def write_before_module() -> None:
    src = (ROOT / 'isekai_core' / 'store.py').read_text(encoding='utf-8')
    if NEW_UPDATE not in src or NEW_INDEX not in src:
        raise SystemExit('无法定位待还原的两处（源码已变）')
    BEFORE_SRC.write_text(src.replace(NEW_UPDATE, OLD_UPDATE).replace(NEW_INDEX, ''), encoding='utf-8')


def load_before():
    spec = importlib.util.spec_from_file_location('isekai_core._store_before2', BEFORE_SRC)
    module = importlib.util.module_from_spec(spec)
    sys.modules['isekai_core._store_before2'] = module
    spec.loader.exec_module(module)
    return module


async def measure(store_cls, root: Path) -> float:
    from isekai_core import app as app_mod
    from isekai_core.app import build_runtime
    from isekai_core.config import load_config
    from isekai_core.llm import FakeLLM

    saved = app_mod.Store
    app_mod.Store = store_cls
    try:
        rt = await build_runtime(load_config(root), llm=FakeLLM(['收到。']))
    finally:
        app_mod.Store = saved
    world, store = rt.world, rt.store
    inst = store.instance_list()[0]['id']
    tl = store.timeline_list(inst)[0]['id']
    world.activate(inst, tl, now_real=time.time())
    clock = store.clock_get(tl)
    br, bw, pr = float(clock['base_real']), int(clock['base_world']), int(clock['processed_world'])
    start = time.perf_counter()
    res = world.advance(inst, tl, now_real=br + (pr + DAYS * DAY - bw) + 0.5, max_batches=DAYS)
    ms = (time.perf_counter() - start) * 1000 / max(1, res.get('batches') or 1)
    await rt.service.shutdown()
    store.close()
    return ms


async def main() -> None:
    write_before_module()
    before_mod = load_before()
    from isekai_core.store import Store as After

    fresh_after: list[float] = []
    aged_after: list[float] = []
    fresh_before: list[float] = []
    aged_before: list[float] = []
    for i in range(ROUNDS):
        for cls, tag in ((After, 'after'), (before_mod.Store, 'before')):
            f = await measure(cls, FRESH)
            a = await measure(cls, AGED)
            if tag == 'after':
                fresh_after.append(f); aged_after.append(a)
            else:
                fresh_before.append(f); aged_before.append(a)
            print(f'  轮 {i+1} {tag:<6} 新({DAYS}日) {f:.2f}  老 {a:.2f}  比值 {a/f:.2f}×')

    mfa, maa = statistics.median(fresh_after), statistics.median(aged_after)
    mfb, mab = statistics.median(fresh_before), statistics.median(aged_before)
    print(f'\n# 修复前：新 {mfb:.2f} ms/日，老 {mab:.2f} ms/日 ⇒ 比值 {mab/mfb:.2f}×')
    print(f'# 修复后：新 {mfa:.2f} ms/日，老 {maa:.2f} ms/日 ⇒ 比值 {maa/mfa:.2f}×')
    print(f'（目标 ≤ 1.2×；修复把老实例压到原来的 {maa/mab:.2f}×）')


if __name__ == '__main__':
    asyncio.run(main())
