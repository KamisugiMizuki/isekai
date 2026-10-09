"""S-3 判别实验（关键一步）：文件大小效应是**被页缓存掩盖**，还是**本来就不存在**？

背景：Lead 已实测两点——
1. 大而冷的表几乎不花热路径成本（`claim` 9,891 行 0.14 ms/日、`knowledge` 6,592 行 0.07 ms/日）；
2. 把历史前缀移出热表（15,880 KB → 11,636 KB，−27%）后推进**没有变快**（1.04×，落在噪声内），
   这与 `.hermes/s3_archive_probe.py` 早先「删 49% ⇒ 快 26%」的读数**矛盾**。
   （那次的删法含 `effect_state` / `reaction` / `life_plan`，把「要算的工作量」也一起删了 ⇒ 它测的是
   「更少的工作」而不是「更小的库」，不能用来支撑 S-3。）

本实验的目的：production 用 `PRAGMA cache_size=-65536`（64 MB）而库只有 15.9 MB ⇒ **全库驻留内存**，
此时「文件大小」理论上不该有任何影响。所以要做一个**对照**：
- 小缓存（2 MB，模拟缓存不足 / 库大于缓存）；
- 生产缓存（64 MB）。

若「小缓存下归档更快、生产缓存下无差异」⇒ 大小效应**被缓存掩盖**（对生产配置无意义）；
若两者都无差异 ⇒ 「大小效应」在真实访问路径上**根本不存在**（早先那次是工作量的假象）。

同一实例、同一时间段、交替多轮取中位数（本文件固化的唯一可信口径）。
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

FULL = Path('.hermes/s3m_full').resolve()
ARCH = Path('.hermes/s3m_arch').resolve()
DAYS = 5
ROUNDS = 3
CACHES = (("-2048", "2 MB（小缓存）"), ("-65536", "64 MB（生产）"))
DAY = 86400

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402


class PrismaConn:
    """代理：在每次 execute 前把页缓存设成测试值（Store 可能在别处重设过）。"""

    def __init__(self, conn, cache: str) -> None:
        self._c = conn
        self._cache = cache
        self._applied = False

    def execute(self, sql, params=()):
        if not self._applied:
            self._applied = True
            self._c.execute(f'PRAGMA cache_size={self._cache}')
            self._c.execute('PRAGMA mmap_size=0')
        return self._c.execute(sql, params)

    def __enter__(self):
        self._c.__enter__()
        return self

    def __exit__(self, *a):
        return self._c.__exit__(*a)

    def __getattr__(self, name):
        return getattr(self._c, name)


async def one_round(root: Path, cache: str) -> float:
    rt = await build_runtime(load_config(root), llm=FakeLLM(['收到。']))
    world, store = rt.world, rt.store
    inst = store.instance_list()[0]['id']
    tl = store.timeline_list(inst)[0]['id']
    world.activate(inst, tl, now_real=time.time())
    proxy = PrismaConn(store._conn, cache)
    store._local.conn = proxy
    clock = store.clock_get(tl)
    base_real, base_world = float(clock['base_real']), int(clock['base_world'])
    processed = int(clock['processed_world'])
    start = time.perf_counter()
    res = world.advance(inst, tl, now_real=base_real + (processed + DAYS * DAY - base_world) + 0.5,
                        max_batches=DAYS)
    spent = time.perf_counter() - start
    batches = max(1, res.get('batches') or 1)
    await rt.service.shutdown()
    rt.store.close()
    return spent * 1000 / batches


async def main() -> None:
    for cache, label in CACHES:
        full_ms: list[float] = []
        arch_ms: list[float] = []
        for i in range(ROUNDS):
            if i % 2 == 0:
                arch_ms.append(await one_round(ARCH, cache))
                full_ms.append(await one_round(FULL, cache))
            else:
                full_ms.append(await one_round(FULL, cache))
                arch_ms.append(await one_round(ARCH, cache))
        mf, ma = statistics.median(full_ms), statistics.median(arch_ms)
        print(f'\n== 页缓存 {label} ==')
        print(f'  原库   ms/世界日 {[round(v,2) for v in full_ms]} 中位数 {mf:.2f}')
        print(f'  归档后 ms/世界日 {[round(v,2) for v in arch_ms]} 中位数 {ma:.2f}')
        print(f'  ⇒ 归档/原库 = {ma/mf:.2f}×（<1 表示归档更快）')


if __name__ == '__main__':
    asyncio.run(main())
