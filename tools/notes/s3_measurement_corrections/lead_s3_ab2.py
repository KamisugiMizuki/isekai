"""两处第一档修法的**合并**同实例交替 A/B：
① `clear_effects` 的 UPDATE 补主键前缀（原 `(? IS NULL OR instance_id=?)` ⇒ SCAN effect_state）；
② `reaction` 加 `(timeline_id, source_ref)` 索引。

基线 = **当前 store.py 只把这两处还原**（其余逐字节相同）⇒ 唯一变量就是这两处。
"""

from __future__ import annotations

import asyncio
import importlib.util
import shutil
import statistics
import sys
import time
from pathlib import Path

SRC = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path('.hermes/ab').resolve()
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
ROUNDS = int(sys.argv[3]) if len(sys.argv) > 3 else 7
DAY = 86400
ROOT = Path('.').resolve()
BEFORE_SRC = Path('.hermes/_store_before2.py').resolve()
AFTER_ROOT = Path('.hermes/s3ab2_after').resolve()
BEFORE_ROOT = Path('.hermes/s3ab2_before').resolve()

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

NEW_INDEX = ('-- 后果解除后推进受影响反应的那条取数（`apply_runtime_batch`）：\n'
             "--   `WHERE timeline_id=? AND stage IN ('active','fading') AND source_ref IN (...)`。\n"
             '-- 只有上面那条 `(timeline_id, stage, started_world)` 时，计划是 `SEARCH ... (timeline_id=? AND stage=?)`，\n'
             '-- 于是要把该线 **3,406 条 active/fading 行逐行过滤** `source_ref`（实测返回 0–6 行）。\n'
             '-- 实测单条 **0.4172 → 0.0187 ms（22×）**（同进程交替 7 轮 × 100 次中位数，`.hermes/lead_s3_reaction_idx.py`）。\n'
             '-- S-4 的原则是「没有查询命中就是纯维护成本」——这条索引有明确命中，因此**默认创建**（不挂开关）。\n'
             'CREATE INDEX IF NOT EXISTS ix_reaction_source_ref ON reaction(timeline_id, source_ref);\n')


def write_before_module() -> None:
    src = (ROOT / 'isekai_core' / 'store.py').read_text(encoding='utf-8')
    if NEW_UPDATE not in src or NEW_INDEX not in src:
        raise SystemExit('无法定位待还原的两处（源码已变，请更新 A/B 脚本）')
    reverted = src.replace(NEW_UPDATE, OLD_UPDATE).replace(NEW_INDEX, '')
    BEFORE_SRC.write_text(reverted, encoding='utf-8')


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
    from isekai_core.store import Store as StoreAfter

    for dst in (BEFORE_ROOT, AFTER_ROOT):
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(SRC, dst, ignore=shutil.ignore_patterns('*.db-wal', '*.db-shm'))
    print(f'# 基线 = 当前 store.py 还原那两处（{BEFORE_SRC.name}）；后 = 工作区版')
    print(f'# 每轮 {DAYS} 天，交替 {ROUNDS} 轮（同实例、同一时间段）')

    b: list[float] = []
    a: list[float] = []
    for i in range(ROUNDS):
        if i % 2 == 0:
            a.append(await measure(StoreAfter, AFTER_ROOT))
            b.append(await measure(before_mod.Store, BEFORE_ROOT))
        else:
            b.append(await measure(before_mod.Store, BEFORE_ROOT))
            a.append(await measure(StoreAfter, AFTER_ROOT))
        print(f'  轮 {i+1}: 前 {b[-1]:.2f}  后 {a[-1]:.2f}')

    mb, ma = statistics.median(b), statistics.median(a)
    print(f'\n# 还原后 ms/世界日 {[round(v,2) for v in b]} 中位 {mb:.2f}')
    print(f'# 修复后 ms/世界日 {[round(v,2) for v in a]} 中位 {ma:.2f}')
    print(f'# ⇒ 后/前 = {ma/mb:.2f}×   成对 {[round(x/y,2) for y,x in zip(b,a)]}')


if __name__ == '__main__':
    asyncio.run(main())
