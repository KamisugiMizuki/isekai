"""S-3 的第二个收益点（提交里的 `runtime_dump` 占比）在**当前代码**上的实测。

设计书称：提交里 `runtime_dump` 仍占 124 ms（提交耗时 83%），归档会同时缩小它。
但那次实测是在 A-4 的 P0①/④ 两刀**之后**的温态口径（第 54 轮：dump 124.2 / commit 241.0 = 52%）。
本脚本在**同一实例**上测 `runtime_dump` 的耗时与其**分节行数构成**，据此判断归档能省下多少。

用法：python .hermes/s3_dump_cost.py <root> [rounds]
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path('.hermes/ab').resolve()
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 3

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402


async def main() -> None:
    rt = await build_runtime(load_config(ROOT), llm=FakeLLM(['收到。']))
    store = rt.store
    inst = store.instance_list()[0]['id']
    tl = store.timeline_list(inst)[0]['id']
    clock = store.clock_get(tl)
    wm = int(clock['processed_world'])

    times: list[float] = []
    dump = None
    for _ in range(ROUNDS):
        t = time.perf_counter()
        dump = store.runtime_dump(inst, tl, watermark=wm)
        times.append((time.perf_counter() - t) * 1000)
    print(f'# runtime_dump ms（{ROUNDS} 轮）: {[round(v,1) for v in times]} 中位数 {statistics.median(times):.1f}')

    sections = sorted(((len(v), k) for k, v in dump.items() if isinstance(v, list)), reverse=True)
    print(f'# 总行数 {sum(n for n, _ in sections)}')
    print(f'{"行数":>8}  分节')
    for n, k in sections:
        if n:
            print(f'{n:>8}  {k}')
    await rt.service.shutdown()
    store.close()


if __name__ == '__main__':
    asyncio.run(main())
