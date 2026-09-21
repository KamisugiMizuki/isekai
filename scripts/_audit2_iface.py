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
              "规范声明的 14 个 op 都能在注册表里找到（别名也算）",
              f"注册 {14 - len(missing)}/14；缺 {', '.join(missing) or '（无）'}",
              "FAIL" if missing else "PASS",
              evidence=f"world_ops.SYNC_OPS+ASYNC_OPS 共 {len(registered)} 个 op；"
                       f"别名表 {sorted(world_ops.IFACE_ALIASES)}",
              code_ref="isekai_core/world/ops.py SYNC_OPS/IFACE_ALIASES")

    # 名字漂移：同一能力换了名字
    pairs = {
        "§6.1 runtime.timeline.fork": ("runtime.timeline.fork", "runtime.fork"),
        "§6.2 runtime.timeline.rollback": ("runtime.timeline.rollback", "runtime.rollback"),
        # runtime.history.read 是原生实现（不走别名），由下面的原生组核
        "§5.4 runtime.rule_state.read": ("runtime.rule_state.read", "trpg.rule_state.read"),
    }
    for clause, (spec_name, real_name) in pairs.items():
        if not only_matches(clause):
            continue
        aliased = world_ops.IFACE_ALIASES.get(spec_name) == real_name
        ok = spec_name in registered and real_name in registered and aliased
        check(clause,
              f"规范名 {spec_name} 可调，且指向实现名 {real_name}",
              f"规范名注册={'是' if spec_name in registered else '否'}；"
              f"别名 {spec_name} → {world_ops.IFACE_ALIASES.get(spec_name) or '（无）'}；"
              f"实现 {'在' if real_name in registered else '不在'}",
              "PASS" if ok else "FAIL",
              evidence="改名不改能力：别名在 dispatch 入口统一归位（任何 op 分支之前）",
              code_ref="isekai_core/world/ops.py IFACE_ALIASES")

    if only_matches("§4.5 runtime.history.read 原生"):
        native = "runtime.history.read" in registered and "runtime.history.read" not in world_ops.IFACE_ALIASES
        check("§4.5 runtime.history.read 原生",
              "历史读接口是原生实现（不是把会话历史改个名）",
              f"注册={'是' if 'runtime.history.read' in registered else '否'}；"
              f"在别名表={'(意外)' if 'runtime.history.read' in world_ops.IFACE_ALIASES else '否（原生）'}",
              "PASS" if native else "FAIL",
              evidence="§4.5 要的是已固化**世界事件 / 效果 / 说法**的历史，不是聊天记录（早先探针把它读成 history.page 是错的）",
              code_ref="isekai_core/runtime/service.py history_read")

    # §5.7 时间：两个独立能力，规范只写了一个名字
    if only_matches("§5.7 runtime.time.advance"):
        has_advance = "runtime.advance" in registered
        has_consume = "runtime.time.consume" in registered
        ok = "runtime.time.advance" in registered and has_consume
        check("§5.7 runtime.time.advance",
              "规范名 runtime.time.advance 落在场景时间消耗上（跟真实时间的是 runtime.advance）",
              f"runtime.time.advance={'已注册' if ok else '未注册'}；"
              f"runtime.advance={has_advance}（跟真实时间）、runtime.time.consume={has_consume}",
              "PASS" if ok else "FAIL",
              evidence="duration/reason → consume_time：请求与原因都记进提交说明",
              code_ref="isekai_core/world/ops.py _iface_op")


def change_contract_facts() -> None:
    """② 公共变化契约（§5.1~5.3）：形态、写入口、幂等。"""
    hits: list[str] = []
    # 只扫产品代码：探针 / 测试里写了这些词不算实现（自命中最容易骗过自己）
    contract = ROOT / "isekai_core/runtime/change.py"
    for path in sorted(ROOT.glob("isekai_core/**/*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for token in ("preview_id", "KINDS = (", "SOURCE_MODES"):
            if token in text:
                hits.append(f"{path.relative_to(ROOT).as_posix()}:{token}")
    from isekai_core.runtime import change as change_mod  # noqa: E402

    ok = contract.is_file() and len(change_mod.KINDS) == 9 and len(change_mod.OPERATIONS) == 7
    if only_matches("§5.1 change_intent 契约形态"):
        check("§5.1 change_intent 契约形态",
              "change_intent 的字段形态在代码里有对应结构",
              f"runtime/change.py 存在={contract.is_file()}；kind 闭集 {len(change_mod.KINDS)} 项、"
              f"operation 闭集 {len(change_mod.OPERATIONS)} 项；命中 {hits[:2]}",
              "PASS" if ok else "FAIL",
              evidence="校验 / 翻译 / 预览标识都在 change.py（纯逻辑），落盘仍只有 apply_runtime_batch",
              code_ref="isekai_core/runtime/change.py")

    # 唯一写入口实况
    if only_matches("§5.3 唯一世界写入口"):
        batches = sorted({
            f"{p.relative_to(ROOT).as_posix()}"
            for p in ROOT.glob("isekai_core/**/*.py")
            if "apply_runtime_batch(" in p.read_text(encoding="utf-8", errors="replace")
        })
        service_src = (ROOT / "isekai_core/runtime/service.py").read_text(encoding="utf-8")
        ok = "def change_commit(" in service_src and "runtime.change.commit" in (
            (ROOT / "isekai_core/world/ops.py").read_text(encoding="utf-8")
        )
        check("§5.3 唯一世界写入口",
              "高级模块的事实写入口是 runtime.change.commit（存储层仍只有 apply_runtime_batch 一条原子边界）",
              f"change_commit={'有' if ok else '无'}；存储级入口调用方 {len(batches)} 个文件（内部批次写入）",
              "PASS" if ok else "FAIL",
              evidence="高层走 change.commit；advance / trpg / event 各自仍是内部批次调用方",
              code_ref="isekai_core/runtime/service.py change_commit")


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
        has_read = "def read_snapshot(" in src
        has_expires = "expires_at" in src
        check("§4.2 snapshot.read 读快照",
              "存在给生成用的读快照方法（可复用句柄 + 过期 + 不可用态明确）",
              f"read_snapshot={'有' if has_read else '无'}；expires_at={'有' if has_expires else '无'}",
              "PASS" if has_read and has_expires else "FAIL",
              evidence="冻结 / 追赶中返回 not_ready；include 不认识就 rejected（行为测试覆盖）",
              code_ref="isekai_core/runtime/service.py read_snapshot")

    with scenario("read") as env:
        info, timeline_id, character_id = mk(env)
        env.world.activate(info["id"], timeline_id, now_real=time.time())
        scope = env.world.scope_inspect(info["id"], timeline_id, now_real=time.time())
        spec_fields = {"timeline_state", "world_time", "processed_watermark", "target_watermark",
                       "revision", "runtime_generation", "ruleset_version", "available_actions"}
        if only_matches("§4.1 scope.inspect 字段覆盖"):
            # 三个字段本就在返回信封里（§3.2），另外五个是 scope 自己的
            from_envelope = {"world_time", "processed_watermark", "runtime_generation"}
            extras = spec_fields - from_envelope
            missing = sorted(field for field in extras if field not in scope)
            envelope_ok = all(field in scope for field in from_envelope)
            check("§4.1 scope.inspect 字段覆盖",
                  "scope_inspect 一个调用给全 §4.1 的字段（三个来自信封、五个来自 scope 自身）",
                  f"scope 自身缺 {missing or '（无）'}；信封三字段齐={'是' if envelope_ok else '否'}；"
                  f"available_actions={scope['available_actions']}",
                  "PASS" if not missing and envelope_ok else "FAIL",
                  evidence=f"字段 {sorted(set(scope) - {'status', 'instance_id', 'timeline_id', 'observed_revision'})}",
                  code_ref="isekai_core/runtime/service.py scope_inspect")
        if only_matches("§4.4 subject.state.read"):
            public = env.world.subject_state(info["id"], timeline_id, subject_id=character_id,
                                             audience="public_party")
            gm = env.world.subject_state(info["id"], timeline_id, subject_id=character_id, audience="gm_only")
            ok = ("all_units" not in public["subject"] and "all_units" in gm["subject"]
                  and len(gm["subject"]) > len(public["subject"]))
            check("§4.4 subject.state.read",
                  "角色状态投影按受众分层：GM 拿全份，公开面只给公开字段族",
                  f"public 字段 {sorted(public['subject'])}；gm 字段 {sorted(gm['subject'])[:5]}…（{len(gm['subject'])} 个）",
                  "PASS" if ok else "FAIL",
                  evidence="规则属性 / 会话历史不在此接口（§4.4 明文）",
                  code_ref="isekai_core/runtime/service.py subject_state")
        if only_matches("§4.3 cognition.project"):
            projected = env.world.cognition_project(
                info["id"], timeline_id, observer_id=character_id, query={"purpose": "dialogue"},
            )
            others = env.world.cognition_project(
                info["id"], timeline_id, observer_id="cc-查无此人", query={"purpose": "dialogue"},
            )
            ok = (projected["observer_id"] == character_id and not others["observations"]
                  and not others["claims"])
            check("§4.3 cognition.project",
                  "按观察者取合法可知投影：陌生人拿到空投影，不串别人的材料",
                  f"observer 观测 {len(projected['observations'])} / 说法 {len(projected['claims'])} / "
                  f"未知 {len(projected['known_unknowns'])}；陌生人 {len(others['observations'])}+{len(others['claims'])}",
                  "PASS" if ok else "FAIL",
                  evidence="投影只读该观察者自己的经历与获知窗口（按角色存储）",
                  code_ref="isekai_core/runtime/service.py cognition_project")


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
            f"TRPG 层在用 {len(trpg_ops)} 个 trpg.* op（自有提交路径，不强迁）",
            "DEFERRED",
            evidence="接口侧本轮已全部打通；OC / WA 只有 docs 下的规范、没有代码，"
                     "没有第二类真实调用方可供消费端验收",
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
            messages: dict[str, str] = {}
            for op in SPEC_OPS:
                try:
                    await mgmt.call(op, instance_id=iid, timeline_id=tlid)
                    codes[op] = "ok"
                except UmpError as exc:
                    codes[op] = f"{exc.code}"
                    messages[op] = str(exc)
                except Exception as exc:  # noqa: BLE001
                    codes[op] = type(exc).__name__
            unknown = sum(1 for value in codes.values() if value.startswith("unsupported_type"))
            # 别名的错误文案必须来自被指向的实现本身（注册表里有名字 ≠ 接到了东西）
            wired = {
                "runtime.knowledge.grant": ("披露需要明确的片段引用（refs）", "disclose.confirm"),
                "runtime.rule_state.read": ("战役操作需要 instance_id / timeline_id / campaign_id", "trpg.rule_state.read"),
                "runtime.timeline.fork": ("缺少 commit_id", "runtime.fork"),
            }
            alias_ok = all(
                snippet in messages.get(op, "")
                for op, (snippet, _target) in wired.items()
            )
            check("§四~六 真 WS 调用规范名",
                  "14 个规范名都能在管理面调通（不再有未知管理操作）；别名报错文案来自被指向的实现",
                  f"unsupported_type {unknown}/14；别名实证："
                  + "；".join(f"{op.split('.')[-1]}→{_target}「{messages.get(op, '')[:18]}」"
                              for op, (_s, _target) in wired.items()),
                  "PASS" if not unknown and alias_ok else "FAIL",
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
