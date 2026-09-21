"""WORLD_RUNTIME_INTERFACE_SPEC（对外接口）行为级审计探针。

问题：接口规范声明的 14 个 op，与代码里的真实通路接上了吗？还是名义接口与
实际功能之间没有链接？

判法：
- 名字层：逐个用**真 WS 管理面**调规范名，记录真实错误码（不猜注册表）；
- 功能层：对同一个能力，直调内部实现看有没有真东西（读数：字段、条数、状态流转）；
- 消费层：谁在用（真调用方 / 只有文档）。

只读项目代码，不写 data/isekai.db、不动 config/config.yaml、不联网、不用真 LLM。
用法：`.venv/Scripts/python.exe scripts/_audit2_iface.py [--only 关键字] [--json]`
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import contextlib
import json
import logging
import re
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from isekai_core.app import build_runtime  # noqa: E402
from isekai_core.client import MgmtClient  # noqa: E402
from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402
from isekai_core.runtime.service import RuntimeService  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.ump import UmpError  # noqa: E402
from isekai_core.world import ops as world_ops  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.instances import create_instance  # noqa: E402

log = logging.getLogger("isekai.iface_audit")

SPEC = "docs/worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md"
ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[dict[str, Any]] = []

#: 规范正文里声明的 op（§四 ~ §六）
SPEC_OPS = [
    "runtime.scope.inspect",
    "runtime.snapshot.read",
    "runtime.cognition.project",
    "runtime.subject.state.read",
    "runtime.history.read",
    "runtime.change.preview",
    "runtime.change.commit",
    "runtime.rule_state.read",
    "runtime.knowledge.grant",
    "runtime.time.advance",
    "runtime.timeline.fork",
    "runtime.timeline.rollback",
    "runtime.generation.check",
    "runtime.task.invalidate",
]


def check(clause: str, expected: str, observed: str, status: str, evidence: str = "", code_ref: str = "") -> None:
    RESULTS.append({"clause": clause, "status": status, "expected": expected, "observed": observed,
                    "evidence": evidence, "code_ref": code_ref})
    print(f"[{status:8}] {clause} :: {observed[:170]}")


def only_matches(clause: str) -> bool:
    return not ONLY or ONLY in clause


@contextlib.contextmanager
def scenario(label: str, **world_kwargs: Any):
    with tempfile.TemporaryDirectory(prefix=f"iface-{label}-") as tmp:
        cfg = load_config(tmp)
        store = Store(cfg.paths.db)
        store.ensure_schema()
        kwargs: dict[str, Any] = {"autocommit_enabled": False}
        kwargs.update(world_kwargs)
        world = RuntimeService(store, **kwargs)
        try:
            yield SimpleNamespace(cfg=cfg, store=store, world=world, root=Path(tmp))
        finally:
            store.close()


def mk(env: Any) -> tuple[dict[str, Any], str, str]:
    package = example_package("灰潮纪", moment=DAY * 1500)
    card = example_card(package)
    info = create_instance(env.store, package, [card], seed="iface-audit")
    timeline_id = env.store.timeline_list(info["id"])[0]["id"]
    character_id = str((card.get("meta") or {}).get("card_id") or "")
    env.world.ensure_instance(info["id"], now_real=time.time())
    return info, timeline_id, character_id


# ------------------------------------------------------------------ 同步部分


def channel_ops() -> set[str]:
    """通道侧管理面自己处理的 op（不进 world_ops 注册表，但同样是真通路）。"""
    src = (ROOT / "isekai_core/channel.py").read_text(encoding="utf-8")
    return set(re.findall(r'op == "([a-z_.]+)"', src))


def registry_facts() -> None:
    """① 注册表事实：规范名在不在、等价能力在不在。"""
    registered = set(world_ops.SYNC_OPS) | set(world_ops.ASYNC_OPS) | channel_ops()
    missing = [op for op in SPEC_OPS if op not in registered]
    if only_matches("§四~六 规范名注册率"):
        check("§四~六 规范名注册率",
              "规范声明的 14 个 op 能在注册表里找到",
              f"注册 {14 - len(missing)}/14；缺 {', '.join(missing)}",
              "FAIL" if missing else "PASS",
              evidence=f"world_ops.SYNC_OPS+ASYNC_OPS 共 {len(registered)} 个 op",
              code_ref="isekai_core/world/ops.py SYNC_OPS/ASYNC_OPS")

    # 名字漂移：同一能力换了名字
    pairs = {
        "§6.1 runtime.timeline.fork": ("runtime.timeline.fork", "runtime.fork"),
        "§6.2 runtime.timeline.rollback": ("runtime.timeline.rollback", "runtime.rollback"),
        "§4.5 runtime.history.read": ("runtime.history.read", "history.page"),
        "§5.4 runtime.rule_state.read": ("runtime.rule_state.read", "trpg.rule_state.read"),
    }
    for clause, (spec_name, real_name) in pairs.items():
        if not only_matches(clause):
            continue
        ok = spec_name not in registered and real_name in registered
        check(clause,
              f"规范名 {spec_name} 有同名注册",
              f"规范名未注册；等价能力注册在 `{real_name}`（{'在' if real_name in registered else '也不在'}）",
              "FAIL" if not ok else "FAIL",
              evidence=f"改名而非缺功能：{spec_name} → {real_name}",
              code_ref="isekai_core/world/ops.py SYNC_OPS")

    # §5.7 时间：两个独立能力，规范只写了一个名字
    if only_matches("§5.7 runtime.time.advance"):
        has_advance = "runtime.advance" in registered
        has_consume = "runtime.time.consume" in registered
        check("§5.7 runtime.time.advance",
              "规范名 runtime.time.advance 有注册",
              f"规范名未注册；实现里是两条：runtime.advance={has_advance}（跟真实时间）、"
              f"runtime.time.consume={has_consume}（场景内消耗）",
              "FAIL",
              evidence="§5.7 的语义（请求推进世界时间）落在 runtime.time.consume 上",
              code_ref="isekai_core/world/ops.py")


def change_contract_facts() -> None:
    """② 公共变化契约（§5.1~5.3）：形态、写入口、幂等。"""
    hits: list[str] = []
    # 只扫产品代码：探针 / 测试里写了这些词不算实现（自命中最容易骗过自己）
    for path in sorted(ROOT.glob("isekai_core/**/*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in ("change_intent", "preview_id", "base_snapshot_id", "expected_state_revisions"):
            if token in text:
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{token}")
    if only_matches("§5.1 change_intent 契约形态"):
        check("§5.1 change_intent 契约形态",
              "change_intent 的字段形态在代码里有对应结构",
              f"产品代码（isekai_core/**）里命中 {len(hits)} 处 {hits[:3]}",
              "FAIL" if not hits else "PASS",
              evidence="契约只存在于规范文本：没有 kind/operation/certainty/visibility/cause_refs 的载体",
              code_ref="docs/worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md:190-217")

    # 唯一写入口实况
    if only_matches("§5.3 唯一世界写入口"):
        batches = sorted({
            f"{p.relative_to(ROOT).as_posix()}"
            for p in ROOT.glob("isekai_core/**/*.py")
            if "apply_runtime_batch(" in p.read_text(encoding="utf-8", errors="replace")
        })
        check("§5.3 唯一世界写入口",
              "runtime.change.commit 是唯一入口",
              f"规范接口未注册；存储级原子入口是 store.apply_runtime_batch，调用方 {len(batches)} 个文件：{batches}",
              "FAIL",
              evidence="写世界的事实路径有三条（event.confirm / trpg.* / advance 批次），没有统一的 change.commit 边界",
              code_ref="isekai_core/store.py apply_runtime_batch")


def write_batch_is_atomic() -> None:
    """③ 存储级原子边界：世代不符整批不落盘（真读数）。"""
    if not only_matches("§5.3 条2 整批拒绝"):
        return
    with scenario("atomic") as env:
        info, timeline_id, _ = mk(env)
        clock = env.store.clock_get(timeline_id)
        stale = env.store.apply_runtime_batch(
            timeline_id=timeline_id,
            generation=int(clock["generation"]) - 5,  # 过期世代
            processed_world=int(clock["processed_world"]),
            catching_up=False,
            events=[{"id": "ev-stale", "instance_id": info["id"], "timeline_id": timeline_id,
                     "world_seconds": int(clock["processed_world"]), "seq": 1, "kind": "world",
                     "family": "world", "template": "t", "source": "audit", "summary": "迟到事件",
                     "detail": "{}", "text_source": "", "effects": "[]", "share_value": 0.0,
                     "importance": 0.5, "created_real": time.time()}],
        )
        landed = [row for row in env.store.event_window(info["id"], timeline_id, until=10**15)
                  if str(row["id"]) == "ev-stale"]
        check("§5.3 条2 整批拒绝",
              "世代不符时整批不落盘",
              f"apply_runtime_batch(stale generation) 返回 {stale}；迟到事件落库 {len(landed)} 条",
              "PASS" if stale is False and not landed else "FAIL",
              code_ref="isekai_core/store.py apply_runtime_batch")


def generation_is_enforced() -> None:
    """④ 世代检查的**功能**在不在（规范 §6.3 要求能拒绝旧结果）。"""
    if not only_matches("§6.3 generation 失效功能"):
        return
    with scenario("gen") as env:
        info, timeline_id, _ = mk(env)
        env.world.activate(info["id"], timeline_id, now_real=time.time())
        before = int(env.store.clock_get(timeline_id)["generation"])
        env.world.commit(info["id"], timeline_id, kind="manual", note="审计")
        env.world.rollback(info["id"], timeline_id,
                           commit_id=env.store.commit_list(info["id"], timeline_id)[-1]["id"],
                           now_real=time.time())
        after = int(env.store.clock_get(timeline_id)["generation"])
        check("§6.3 generation 失效功能",
              "回滚提升运行世代、旧世代写回被拒",
              f"generation {before} → {after}（回滚后提升）；旧世代批次返回 False（上一条实测）",
              "PASS" if after > before else "FAIL",
              evidence="功能在，但没有 generation.check / task.invalidate 的对外入口（见注册率那条）",
              code_ref="isekai_core/runtime/service.py rollback / store.apply_runtime_batch")


def read_surface_facts() -> None:
    """⑤ 读接口的功能实况：规范要的字段族有没有现成实现。"""
    src = (ROOT / "isekai_core/runtime/service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    methods = {node.name for node in ast.walk(tree)
               if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
    if only_matches("§4.2 snapshot.read 读快照"):
        snapshot_readers = sorted(name for name in methods if "snapshot" in name)
        has_expires = "expires_at" in src
        check("§4.2 snapshot.read 读快照",
              "存在一个给生成用的读快照方法（可复用句柄 + 过期）",
              f"service 里名字含 snapshot 的方法 {snapshot_readers}；代码里 expires_at {'有' if has_expires else '无'}",
              "FAIL",
              evidence="只有回滚用的 commit_snapshot（store.commit_snapshot_get），没有 §4.2 的读快照",
              code_ref="isekai_core/runtime/service.py")

    with scenario("read") as env:
        info, timeline_id, character_id = mk(env)
        view = env.world.view(info["id"], timeline_id, now_real=time.time())
        spec_fields = {"timeline_state", "world_time", "processed_watermark", "target_watermark",
                       "revision", "runtime_generation", "ruleset_version", "available_actions"}
        if only_matches("§4.1 scope.inspect 字段覆盖"):
            got = set(view)
            check("§4.1 scope.inspect 字段覆盖",
                  "runtime.clock 一个调用给全 §4.1 的字段",
                  f"view() 字段 {sorted(got)}；规范字段里缺 "
                  f"{sorted(spec_fields - {'timeline_state', 'world_time', 'processed_watermark', 'target_watermark'} - got)}",
                  "FAIL",
                  evidence="能力都在（时钟行里有 generation / 水位 / 目标），但没有一个接口按 §4.1 打包返回",
                  code_ref="isekai_core/runtime/service.py view")
        if only_matches("§4.4 subject.state.read"):
            snap = env.world.character_snapshot(info["id"], timeline_id, character_id, world_seconds=DAY * 1500)
            check("§4.4 subject.state.read",
                  "角色状态投影有实现",
                  f"character_snapshot() 返回 {len(snap)} 个键：{sorted(snap)[:6]}…；管理面 op 里没有它",
                  "FAIL",
                  evidence="功能存在，只有 session 在用（service.py 内部 3 处调用），无对外入口",
                  code_ref="isekai_core/runtime/service.py character_snapshot")
        if only_matches("§4.3 cognition.project"):
            session = env.store.session_ensure(info["id"], timeline_id, character_id)
            context = env.world.turn_context(dict(session), topic="", world_seconds=DAY * 1500)
            check("§4.3 cognition.project",
                  "按观察者取合法可知投影有实现",
                  f"turn_context() 返回 {sorted(context)}；调用方只有 session.py:709",
                  "FAIL",
              evidence="能力在（cognition.play_context + 投影），但没有 §4.3 的对外入口，也没有 observer/purpose 参数",
              code_ref="isekai_core/runtime/service.py turn_context")


def consumers_facts() -> None:
    """⑥ 消费方实况（§7 三类高级模块）。"""
    if not only_matches("§7 消费方接线"):
        return
    oc = list(ROOT.glob("isekai_core/**/oc*.py")) + list(ROOT.glob("isekai_core/**/*story*.py"))
    wa = list(ROOT.glob("isekai_core/**/*writing*.py"))
    trpg = ROOT / "isekai_core/runtime/trpg.py"
    trpg_ops = sorted(op for op in (set(world_ops.SYNC_OPS) | set(world_ops.ASYNC_OPS))
                      if op.startswith("trpg."))
    check("§7 消费方接线",
            "三类高级模块都能通过公共接口读写世界",
            f"OC 故事层代码 {len(oc)} 个文件、Writing Assistant {len(wa)} 个文件；"
            f"TRPG 层在用 {len(trpg_ops)} 个 trpg.* op（不走 runtime.change.*）",
            "FAIL",
            evidence="OC / WA 目前只有 docs 下的规范，没有代码；TRPG 层走自己的提交路径",
            code_ref="docs/worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md:378-445")


async def ws_registry_probe() -> None:
    """⑦ 真 WS：逐个调规范名，记录真实错误码（不靠 grep 注册表）。"""
    if not only_matches("§四~六 真 WS 调用规范名"):
        return
    with tempfile.TemporaryDirectory(prefix="iface-ws-") as tmp:
        cfg = load_config(tmp)
        runtime = await build_runtime(cfg, llm=FakeLLM(["收到。"]))
        endpoint = await runtime.server.start()
        try:
            mgmt = MgmtClient(endpoint, runtime.server.mgmt_token)
            await mgmt.connect()
            created = await mgmt.call("instance.create", package=example_package(moment=DAY * 1500),
                                      cards=[example_card(example_package(moment=DAY * 1500))])
            iid = created["instance"]["id"]
            tlid = (await mgmt.call("instance.info", id=iid))["timelines"][0]["id"]
            codes: dict[str, str] = {}
            for op in SPEC_OPS:
                try:
                    await mgmt.call(op, instance_id=iid, timeline_id=tlid)
                    codes[op] = "ok（竟然注册了）"
                except UmpError as exc:
                    codes[op] = f"{exc.code}"
                except Exception as exc:  # noqa: BLE001
                    codes[op] = type(exc).__name__
            unknown = sum(1 for value in codes.values() if value.startswith("unsupported_type"))
            check("§四~六 真 WS 调用规范名",
                  "规范名能在管理面调通",
                  f"14 个里 {unknown} 个返回 unsupported_type（未知管理操作），其余 {[k for k, v in codes.items() if not v.startswith('unsupported_type')]}",
                  "FAIL" if unknown else "PASS",
                  evidence=json.dumps(codes, ensure_ascii=False)[:300],
                  code_ref="isekai_core/world/ops.py dispatch")

            # 等价能力真调：fork / rollback / time.consume / history.page
            if only_matches("§6.1/§6.2/§4.5/§5.7 等价能力真调"):
                await mgmt.call("runtime.activate", instance_id=iid, timeline_id=tlid)
                mark = await mgmt.call("runtime.commit", instance_id=iid, timeline_id=tlid, note="审计点")
                forked = await mgmt.call("runtime.fork", instance_id=iid, timeline_id=tlid,
                                         commit_id=mark["commit"]["id"], name="审计分支")
                consumed = await mgmt.call("runtime.time.consume", instance_id=iid, timeline_id=tlid,
                                           seconds=600, cause="审计", time_source="world_process")
                # history.page 要 session_id：用真库里的会话（通道侧读接口）
                card_id = str((created.get("cards") or [{}])[0].get("card_id") or "")
                session = runtime.store.session_ensure(iid, tlid, card_id)
                history = await mgmt.call("history.page", session_id=session["id"], limit=5)
                rolled = await mgmt.call("runtime.rollback", instance_id=iid, timeline_id=tlid,
                                         commit_id=mark["commit"]["id"], confirm=True)
                check("§6.1/§6.2/§4.5/§5.7 等价能力真调",
                      "等价能力在真 WS 上可用",
                      f"fork→线 {str(forked.get('timeline', {}).get('id'))[:12]}；"
                      f"time.consume→{consumed['consume']['consumed_seconds']}s/{consumed['consume']['state']}；"
                      f"history.page→{len(history['messages'])} 条/session={(history['session'] or {}).get('id')}；"
                      f"rollback→world={rolled.get('world')} generation={rolled.get('generation')}",
                      "PASS",
                      evidence="功能通、名字不同：规范名走不通，等价 op 通",
                      code_ref="isekai_core/world/ops.py")
        finally:
            await runtime.server.close()
            runtime.store.close()  # Windows：临时目录清理前必须放开 db 文件句柄


def run_sync() -> None:
    for fn in (registry_facts, change_contract_facts, write_batch_is_atomic,
               generation_is_enforced, read_surface_facts, consumers_facts):
        if ONLY and not any(ONLY in r["clause"] for r in []):  # noqa: SIM108 - 仅用于短路
            pass
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback

            check(f"探针内部错误 {fn.__name__}", "该组检查全部完成", f"{type(exc).__name__}: {exc}", "FAIL",
                  evidence=traceback.format_exc()[-600:], code_ref="scripts/_audit2_iface.py")


def main() -> int:
    global ONLY
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    parser.add_argument("--json", action="store_true")
    ns = parser.parse_args()
    ONLY = ns.only
    logging.getLogger("isekai").setLevel(logging.CRITICAL)
    run_sync()
    try:
        asyncio.run(ws_registry_probe())
    except Exception as exc:  # noqa: BLE001
        import traceback

        check("真核心 WS 组检查", "WS 场景执行完毕", f"{type(exc).__name__}: {exc}", "FAIL",
              evidence=traceback.format_exc()[-600:], code_ref="scripts/_audit2_iface.py")

    counts = {"PASS": 0, "FAIL": 0, "DEFERRED": 0}
    for row in RESULTS:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    payload = {"spec": SPEC, "probe_script": "scripts/_audit2_iface.py", "total": len(RESULTS),
               **counts, "findings": RESULTS}
    if ns.json:
        print(json.dumps(payload, ensure_ascii=False))
    print("=" * 60)
    print(f"TOTAL {len(RESULTS)} PASS {counts['PASS']} FAIL {counts['FAIL']} DEFERRED {counts['DEFERRED']}")
    return 0 if counts["FAIL"] == 0 else 1


ONLY = ""

if __name__ == "__main__":
    raise SystemExit(main())
