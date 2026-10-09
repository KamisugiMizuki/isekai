"""S-3 备选线的定位：`effect_state` 的取数到底**在一个批里被调了几次、由谁调**。

Lead 的按表归因显示：`effect_state` 每批出现 4 条形状几乎相同的 SELECT（0.099/0.097/0.093/0.049…），
合计约 1.3 ms/世界日（占老实例 3.4 ms/日的 36%）。若它们确实是**同一次 advance 内的重复取数**，
那就是一个「同库变大无关」的确定性热点（S-3 解决不了）。

本脚本给 trace 回调加**调用栈**，把每条 effect_state 取数归到 `service.py:<行号>(<函数>)`。
"""

from __future__ import annotations

import asyncio
import collections
import re
import sys
import time
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path('.hermes/ab').resolve()
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 5
DAY = 86400

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402


async def main() -> None:
    rt = await build_runtime(load_config(ROOT), llm=FakeLLM(['收到。']))
    world, store = rt.world, rt.store
    inst = store.instance_list()[0]['id']
    tl = store.timeline_list(inst)[0]['id']
    world.activate(inst, tl, now_real=time.time())

    hits: collections.Counter = collections.Counter()
    n = [0]

    def cb(sql: str) -> None:
        s = re.sub(r'\s+', ' ', sql).strip()
        if not s.upper().startswith('SELECT') or 'effect_state' not in s:
            return
        n[0] += 1
        stack = sys._getframe()
        frames = []
        while stack is not None:
            fn = stack.f_code.co_filename
            if 'isekai_core' in fn:
                frames.append(f'{Path(fn).name}:{stack.f_lineno}({stack.f_code.co_name})')
            stack = stack.f_back
        hits[' <- '.join(reversed(frames[:3]))] += 1

    clock = store.clock_get(tl)
    base_real, base_world = float(clock['base_real']), int(clock['base_world'])
    processed = int(clock['processed_world'])
    store._conn.set_trace_callback(cb)
    start = time.perf_counter()
    res = world.advance(inst, tl, now_real=base_real + (processed + DAYS * DAY - base_world) + 0.5,
                        max_batches=DAYS)
    spent = time.perf_counter() - start
    store._conn.set_trace_callback(None)
    batches = max(1, res.get('batches') or 1)
    print(f'# {batches} 批，{spent*1000/batches:.2f} ms/日，effect_state 取数 {n[0]} 次 ⇒ {n[0]/batches:.1f} 次/批')
    for shape, c in hits.most_common():
        print(f'  {c:4d}  {c/batches:.1f}/批  {shape}')
    await rt.service.shutdown()
    rt.store.close()


if __name__ == '__main__':
    asyncio.run(main())
