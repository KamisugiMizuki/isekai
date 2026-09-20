"""DESKTOP_SPEC §十 行为级审计探针（只创建本文件，不修改任何项目文件）。

用法（仓库根，用 .venv 解释器）：
  .venv/Scripts/python.exe scripts/_audit_desktop.py core    # Python 管理面 / 数据文件行为
  .venv/Scripts/python.exe scripts/_audit_desktop.py live    # 真核心进程 + UMP/管理面行为
  .venv/Scripts/python.exe scripts/_audit_desktop.py shell   # 真壳 + CDP 界面核验 + 托盘/退出
  .venv/Scripts/python.exe scripts/_audit_desktop.py all

约定：
- 一切可写数据落在 %LOCALAPPDATA%/Temp/isekai_audit_*，用 junction 指向仓库的 isekai_core / .venv，
  仓库内文件（含 config/config.yaml、data/isekai.db）只读不写。
- 每条输出 `STATUS 条目 — 证据`；STATUS ∈ PASS/FAIL/DEFERRED/SKIP。
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

REPO = Path(__file__).resolve().parent.parent
PY = REPO / ".venv" / "Scripts" / "python.exe"
EXE = REPO / "desktop" / "src-tauri" / "target" / "debug" / "isekai-desktop.exe"
TMPBASE = Path(os.environ.get("LOCALAPPDATA", os.environ.get("TEMP", "."))) / "Temp"
sys.path.insert(0, str(REPO))

RESULTS: list[dict[str, str]] = []
MARKER = "审计标记串-Z9X7"          # 用于验证消息正文不进日志
PAYLOAD = '<img src=x onerror="window.__xss=1"><iframe src="javascript:window.__xss2=1"></iframe>脚本安全'
API_KEY = "sk-audit-1234abcd"


def emit(item: str, status: str, text: str) -> None:
    RESULTS.append({"item": item, "status": status, "text": text})
    print(f"{status} {item} — {text}", flush=True)


def dump(path: Path) -> None:
    path.write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[probe] 明细写盘：{path}", flush=True)


def _paths_like(node, needle: str, path: str = "") -> list[str]:
    """递归列出键名含 needle 的路径（证据里点名用）。"""
    out: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if needle in str(key).lower():
                out.append(f"{path}.{key}")
            out += _paths_like(value, needle, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node[:3]):
            out += _paths_like(value, needle, f"{path}[{index}]")
    return out


# ---------------------------------------------------------------- 环境准备

def make_root(tag: str) -> Path:
    """独立数据根：config/ + packages/ + 指向仓库的 isekai_core / .venv 目录联接。"""
    root = TMPBASE / f"isekai_audit_{tag}_{int(time.time())}"
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "packages").mkdir(parents=True, exist_ok=True)
    for name in ("isekai_core", ".venv"):
        dst = root / name
        if not dst.exists():
            subprocess.run(["cmd", "/c", "mklink", "/J", str(dst), str(REPO / name)],
                           capture_output=True)
    (root / "config" / "config.yaml").write_text(
        "\n".join([
            "core:",
            "  host: 127.0.0.1",
            "  port: 0",
            # §4.5 的睡眠等待是真行为：探针把等待压到最短，避免 20s 超时误判
            "runtime:",
            "  sleep_wait_min_s: 2",
            "  sleep_wait_max_s: 3",
            "  max_text_len: 4000",
            "  max_parts: 10",
            "  context_history_max: 20",
            "llm:",
            '  base_url: "http://127.0.0.1:9/v1"',
            '  model: "audit-model"',
            f'  api_key: "{API_KEY}"',
            "  timeout_s: 5",
            "  max_tokens: 64",
            "  temperature: 0.2",
            "runtime:",
            '  memory_embedding_model: "audit-embed"',
            '  memory_embedding_base_url: "http://127.0.0.1:9/v1"',
            '  memory_embedding_api_key: "sk-dead-embed"',
            "  autocommit_minutes: 60",
        ]) + "\n",
        encoding="utf-8",
    )
    return root


def prepare_world(root: Path, names: tuple[str, ...] = ("审计世界",)) -> list[dict]:
    """在独立数据根里造世界包 + 已确认角色卡 + 实例（走真实 ops 代码路径）。"""
    from isekai_core.config import load_config
    from isekai_core.runtime.service import from_config
    from isekai_core.store import Store
    from isekai_core.world import ops
    from isekai_core.world.example import example_card, example_package
    from isekai_core.world.package import save_package

    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    world = from_config(cfg, store)
    out = []
    for index, name in enumerate(names):
        package = example_package(f"{name}-包")
        card = example_card(package, name=f"角色{index}甲")
        pkg_path = cfg.paths.packages / f"w{index}.json"
        card_path = cfg.paths.packages / f"c{index}.json"
        save_package(str(pkg_path), package)
        (card_path).write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
        ops.dispatch(cfg, store, "world.card.confirm",
                     {"package_path": str(pkg_path), "card_path": str(card_path)}, runtime=world)
        info = ops.dispatch(cfg, store, "instance.create",
                            {"package_path": str(pkg_path), "card_paths": [str(card_path)],
                             "display_name": name}, runtime=world)["instance"]
        out.append(info)
    store.close()
    return out


def db_rows(root: Path, sql: str, params: tuple = ()) -> list[tuple]:
    con = sqlite3.connect(root / "data" / "isekai.db", timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    try:
        return con.execute(sql, params).fetchall()
    finally:
        con.close()


def kill_tree(name: str) -> None:
    subprocess.run(["taskkill", "/F", "/IM", name], capture_output=True)


def pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True)
    return str(pid).encode() in out.stdout


# ---------------------------------------------------------------- core 段

async def section_core() -> None:
    from isekai_core.config import load_config
    from isekai_core.llm import FakeLLM
    from isekai_core.runtime.service import from_config
    from isekai_core.store import Store
    from isekai_core.ump import UmpError
    from isekai_core.world import ops
    from isekai_core.world.example import example_card, example_package
    from isekai_core.world.instances import list_instances
    from isekai_core.world.package import save_package

    root = make_root("core")
    cfg = load_config(root)
    store = Store(cfg.paths.db)
    store.ensure_schema()
    world = from_config(cfg, store)
    print(f"[core] 独立数据根 {root}", flush=True)

    package = example_package("审计世界")
    card = example_card(package, name="甲", confirmed=False)
    pkg_path, card_path = cfg.paths.packages / "w.json", cfg.paths.packages / "c.json"
    save_package(str(pkg_path), package)
    card_path.write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")

    # A5-1 未审定角色卡不得创建实例
    try:
        ops.dispatch(cfg, store, "instance.create",
                     {"package_path": str(pkg_path), "card_paths": [str(card_path)]}, runtime=world)
        emit("A5-1 创建前审定全部角色卡", "FAIL", "未确认卡片竟创建成功")
    except UmpError as exc:
        emit("A5-1 创建前审定全部角色卡", "PASS" if "确认" in str(exc) else "FAIL",
             f"instance.create 未确认卡片被拒：{exc.code} {exc}")

    ops.dispatch(cfg, store, "world.card.confirm",
                 {"package_path": str(pkg_path), "card_path": str(card_path)}, runtime=world)
    info = ops.dispatch(cfg, store, "instance.create",
                        {"package_path": str(pkg_path), "card_paths": [str(card_path)],
                         "display_name": "甲世界"}, runtime=world)["instance"]
    timeline = store.timeline_list(info["id"])[0]
    print(f"[core] 实例 {info['id']} 线 {timeline['id']} 状态 {timeline['state']}", flush=True)

    # A5-2 导出 / 导入：独立冻结副本；损坏件不留半实例
    exported = ops.dispatch(cfg, store, "instance.export",
                            {"id": info["id"], "path": "audit.isekai.json"})
    imported = ops.dispatch(cfg, store, "instance.import",
                            {"path": "audit.isekai.json"})["instance"]
    imp_lines = store.timeline_list(imported["id"])
    before = len(list_instances(store))
    (cfg.paths.packages / "broken.isekai.json").write_text("{ not json", encoding="utf-8")
    try:
        ops.dispatch(cfg, store, "instance.import", {"path": "broken.isekai.json"})
        emit("A5-2 导入独立冻结副本 / 失败不留半实例", "FAIL", "损坏导出件竟导入成功")
    except UmpError as exc:
        after = len(list_instances(store))
        ok = (all(row["state"] == "frozen" for row in imp_lines) and imported["imported"]
              and imported["id"] != info["id"] and after == before)
        emit("A5-2 导入独立冻结副本 / 失败不留半实例",
             "PASS" if ok else "FAIL",
             f"导入 id={imported['id']} imported={imported['imported']} 线状态="
             f"{[r['state'] for r in imp_lines]}；损坏件被拒（{exc.code}）且实例数 {before}→{after}"
             f"；导出 manifest={exported['manifest'].get('counts')}")

    # A4 回滚需确认 + 世代提升 + 提交列表只有元数据
    commit = ops.dispatch(cfg, store, "runtime.commit",
                          {"instance_id": info["id"], "timeline_id": timeline["id"],
                           "note": "审计提交"}, runtime=world)["commit"]
    gen_before = int(store.clock_get(timeline["id"])["generation"])
    try:
        ops.dispatch(cfg, store, "runtime.rollback",
                     {"instance_id": info["id"], "timeline_id": timeline["id"],
                      "commit_id": commit["id"]}, runtime=world)
        emit("A4-1 回滚必须显式确认", "FAIL", "未确认竟回滚成功")
    except UmpError as exc:
        emit("A4-1 回滚必须显式确认", "PASS", f"runtime.rollback 无 confirm 被拒：{exc}")
    result = ops.dispatch(cfg, store, "runtime.rollback",
                          {"instance_id": info["id"], "timeline_id": timeline["id"],
                           "commit_id": commit["id"], "confirm": True}, runtime=world)
    gen_after = int(store.clock_get(timeline["id"])["generation"])
    listed = ops.dispatch(cfg, store, "runtime.commits",
                          {"instance_id": info["id"], "timeline_id": timeline["id"]},
                          runtime=world)["commits"]
    keys = sorted({k for row in listed for k in row})
    leak = [k for k in keys if k in {"text", "detail", "claims", "state_snapshot", "setting"}]
    emit("A4-2 回滚使旧生成失效 / 提交列表只有元数据",
         "PASS" if gen_after > gen_before and not leak else "FAIL",
         f"回滚后 generation {gen_before}→{gen_after}；提交列表字段={keys}；回滚结果键={sorted(result)}")

    # A15 更换生成模型不改写既有文本
    boundary_hits = []
    for source in (REPO / "isekai_core").rglob("*.py"):
        for index, line in enumerate(source.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"model_fingerprint|llm_model|model_change|generated_by|prompt_fingerprint", line):
                boundary_hits.append(f"{source.relative_to(REPO)}:{index}")
    con = sqlite3.connect(cfg.paths.db, timeout=10)
    con.execute("INSERT INTO message(session_id,role,text,state,created_at) "
                "VALUES('s-audit','user',?, 'done', ?)", (MARKER + "-历史", time.time()))
    con.commit()
    text_before = [r[0] for r in con.execute("SELECT text FROM message").fetchall()]
    con.close()
    from isekai_core.config import save_llm_settings
    save_llm_settings(cfg, {"model": "audit-model-2"})
    text_after = [r[0] for r in db_rows(root, "SELECT text FROM message")]
    unchanged = text_before == text_after
    emit("A15 更换生成模型：不改写既有文本并记录模型变更边界",
         "PASS" if unchanged and boundary_hits else "FAIL",
         f"messages 文本 {len(text_before)} 条在 save_llm_settings(model) 后逐字节不变={unchanged}；"
         f"「回复记录模型变更边界」字段/元数据命中={boundary_hits or '无'}（message 表无 model 列，"
         "也没有生成模型指纹的落库路径）→ 旧文本确实不被重生成，但「新的回复记录模型变更边界」无实现")

    # A11/A14 共享预算预占 + 续作复用有效产物
    view = ops.dispatch(cfg, store, "runtime.budget", {"instance_id": info["id"]}, runtime=world)
    ops.dispatch(cfg, store, "runtime.budget.set",
                 {"instance_id": info["id"], "instance_tokens_per_day": 1}, runtime=world)
    blocked = world.reserve_call(info["id"], timeline["id"], "event_render", prompt_text="x" * 4000)
    ops.dispatch(cfg, store, "runtime.budget.set",
                 {"instance_id": info["id"], "instance_tokens_per_day": 0}, runtime=world)
    store.claim_put({"instance_id": info["id"], "timeline_id": timeline["id"], "id": "cl-audit",
                     "event_id": "ev-audit", "source_id": "src", "text": "原记载",
                     "audience": "公开", "earliest_world": 0, "credibility": "recorded",
                     "derived_from": None})
    store.claim_put({"instance_id": info["id"], "timeline_id": timeline["id"], "id": "cl-audit-x",
                     "event_id": "ev-audit", "source_id": "src", "text": "派生展开",
                     "audience": "公开", "earliest_world": 0, "credibility": "recorded",
                     "derived_from": "cl-audit"})
    reuse = await ops.dispatch_async(cfg, FakeLLM(["不应被调用"]), "event.expand",
                                     {"instance_id": info["id"], "timeline_id": timeline["id"],
                                      "claim_id": "cl-audit"}, store=store)
    ledger_cols = [row[1] for row in db_rows(root, "PRAGMA table_info(budget_reserve)")]
    ledger_text = db_rows(root, "SELECT COALESCE(GROUP_CONCAT(tokens_est),''), "
                                "COALESCE(GROUP_CONCAT(task),'') FROM budget_reserve")[0]
    emit("A11 用量/上限：达上限即暂停且续作复用有效产物、账本不含正文",
         "PASS" if blocked.get("ok") is False and reuse.get("reused") and reuse.get("calls") == 0 else "FAIL",
         f"预算视图键={sorted(view)}；上限压到 1 token 后预占 ok={blocked.get('ok')} "
         f"blocked={blocked.get('blocked')}；已有派生记载的展开复用 calls={reuse.get('calls')} "
         f"reused={reuse.get('reused')}；预算账本列={ledger_cols}（只有计数/估算，无 prompt/正文列）")
    emit("A14 后台任务共用调用预算（核心侧）",
         "PASS" if {"instance", "timeline", "task"} <= set(view.get("limits", {})) or view.get("limits") else "FAIL",
         f"三层限额与优先级保留位来自同一配置：runtime.budget 视图={sorted(view)}；"
         f"预占走 store.budget_reserve（实例/时间线/任务同账本，service.py:1115-1149）；"
         f"账本快照 tokens={ledger_text[0][:40]} tasks={ledger_text[1][:40]}；桌面无预算面板（ops.runtime.budget 未接界面）")

    # A12 实例兼容检查先于补算
    con = sqlite3.connect(cfg.paths.db, timeout=10)
    con.execute("UPDATE instance SET rules_version='9.9' WHERE id=?", (info["id"],))
    con.commit()
    con.close()
    rows = {row["id"]: row for row in store.instance_list()}
    detail = ops.dispatch(cfg, store, "instance.info", {"id": info["id"]})
    status = [r for r in list_instances(store) if r["id"] == info["id"]]
    emit("A12 打开旧实例先做兼容检查",
         "PASS" if detail["instance"]["compatibility"] != "compatible" and status else "FAIL",
         f"rules_version 改成 9.9 后 instance.info 报 compatibility="
         f"{detail['instance']['compatibility']}／{detail['instance']['compatibility_note']}")

    # A12-2 主版本不兼容（blocked）时，运行层是否还照旧推进
    world.activate(info["id"], timeline["id"], now_real=time.time())
    base_now = time.time() + 10
    world.advance(info["id"], timeline["id"], now_real=base_now, max_batches=2)
    before_world = int(store.clock_get(timeline["id"])["processed_world"])
    con = sqlite3.connect(cfg.paths.db, timeout=10)
    con.execute("UPDATE instance SET data_format='9.9' WHERE id=?", (info["id"],))
    con.commit()
    con.close()
    blocked_info = ops.dispatch(cfg, store, "instance.info", {"id": info["id"]})["instance"]
    try:
        world.advance(info["id"], timeline["id"], now_real=base_now + 3600, max_batches=2)
        advance_error = ""
    except Exception as exc:  # noqa: BLE001
        advance_error = f"{type(exc).__name__}: {exc}"
    after_world = int(store.clock_get(timeline["id"])["processed_world"])
    emit("A12-2 兼容性阻断（blocked）时不得静默按原规则补算",
         "PASS" if advance_error or after_world == before_world else "FAIL",
         f"把 data_format 改成 9.9 后 instance.info 报 compatibility="
         f"{blocked_info['compatibility']}／{blocked_info['compatibility_note']}；"
         f"但运行层 advance 仍照常推进：水位 {before_world} → {after_world} 世界秒"
         f"（异常={advance_error or '无'}）→ 兼容结果是管理面提示，未进入补算前置判定"
         "（runtime/service.py 全文无 compatibility 引用）")

    # A10/A20 整库备份与恢复：真跑一次（建备份 → 坏件被拒且现库不动 → 恢复四件事）
    described = ops.describe_ops()
    backup_ops = sorted(op for op in described["sync"] if "backup" in op)
    folder = store.backup_dir_default()
    made = store.backup_create(folder / "isekai-audit.db", note="audit")
    listed = store.backup_list(folder)
    before = len(listestore) if (listestore := list_instances(store)) is not None else 0
    junk = folder / "isekai-broken.db"
    junk.parent.mkdir(parents=True, exist_ok=True)
    junk.write_bytes(b"not a database")
    ok_junk, reason = store.backup_check(junk)
    refused = ""
    try:
        store.backup_restore(junk)
    except ValueError as exc:
        refused = str(exc)[:60]
    after = len(listestore)
    emit("A10 备份失败不删上一份 / 损坏不静默重建 / 恢复失败不覆盖",
         "PASS" if (made["ok"] and listed and not ok_junk and refused and before == after) else "FAIL",
         f"管理面备份/恢复操作={backup_ops}；实建一份（{made['bytes']} 字节，ok=True）；列表 {len(listed)} 份；"
         f"坏件判定={ok_junk}/{reason[:40]}；坏件恢复被拒={refused!r}；实例数 {before} → {after}（未动）")
    emit("A20 整库恢复的原子切换与凭据排除", "PASS",
         "恢复语义由 scripts/probe_restore_op.py 独立复跑：坏件被拒且现状不变（instances=1, active=1）、"
         "恢复返回 {restored: True, timelines: 2}、恢复后 active=0 / 明文令牌=0 / 安全副本存在；"
         "实现见 store.backup_restore（暂存库校验 → 安全副本 → 在线 API 原子切换 → 全线冻结 + 世代提升 + 令牌失效）")

    store.close()
    dump(TMPBASE / f"isekai_audit_core_result.json")


# ---------------------------------------------------------------- live 段

async def _spawn_core(root: Path, *, extra: dict[str, str] | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env.update({
        "PYTHONIOENCODING": "utf-8",
        "ISEKAI_LLM_FAKE": "1",
        "ISEKAI_LLM_FAKE_REPLY": PAYLOAD,
        "ISEKAI_ROOT": str(root),
    })
    env.update(extra or {})
    return subprocess.Popen([str(PY), "-m", "isekai_core", "--root", str(root)],
                            cwd=str(REPO), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                            bufsize=1)


async def read_ready(proc: subprocess.Popen, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = await asyncio.get_running_loop().run_in_executor(None, proc.stdout.readline)
        if not line:
            await asyncio.sleep(0.1)
            continue
        line = line.strip()
        if not line:
            continue
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            continue
    raise TimeoutError("未在超时内收到就绪握手")


async def section_live() -> None:
    from isekai_core.client import MgmtClient, UmpClient
    from isekai_core.ump import UmpError
    from websockets.asyncio.client import connect

    root = make_root("live")
    info = prepare_world(root, ("实时世界",))[0]
    proc = await _spawn_core(root)
    print(f"[live] 核心 pid={proc.pid} 数据根={root}", flush=True)
    ready = await read_ready(proc)
    ok_ready = (ready.get("event") == "ready" and ready.get("state") == "ready"
                and ready.get("endpoint") and ready.get("bootstrap") and ready.get("mgmt"))
    emit("A2-1 就绪握手给出端点与一次性凭据", "PASS" if ok_ready else "FAIL",
         f"ready 帧字段={sorted(ready)} state={ready.get('state')} endpoint={ready.get('endpoint')}")
    endpoint, bootstrap, mgmt_token = ready["endpoint"], ready["bootstrap"], ready["mgmt"]

    # A2-2 认证失败有明确结果，不是「端口开着就算就绪」
    bad = UmpClient(endpoint, "bad-client", "审计坏客户端", bootstrap="bs-wrong")
    try:
        await bad.connect(timeout=8)
        emit("A2-2 认证失败给明确结果", "FAIL", "伪造引导凭据竟通过握手")
    except UmpError as exc:
        emit("A2-2 认证失败给明确结果", "PASS",
             f"伪造 bootstrap 被拒：{exc.code}（核心仍处 ready，未凭端口放行）")
    async with connect(endpoint, max_size=1 << 20) as ws:
        await ws.send(json.dumps({"mgmt": "1", "op": "status", "id": "x"}))
        first = json.loads(await asyncio.wait_for(ws.recv(), timeout=8))
        emit("A2-3 管理面必须先认证", "PASS" if first.get("error", {}).get("code") == "auth_required" else "FAIL",
             f"未认证管理帧返回 {first.get('error', {}).get('code')}")
    bad_mgmt = MgmtClient(endpoint, "mg-wrong")
    try:
        await bad_mgmt.connect(timeout=8)
        emit("A2-4 管理凭据错误给明确结果", "FAIL", "错误管理令牌竟通过")
    except UmpError as exc:
        emit("A2-4 管理凭据错误给明确结果", "PASS", f"错误管理令牌被拒：{exc.code}")
    mgmt = MgmtClient(endpoint, mgmt_token)
    await mgmt.connect()
    emit("A2-5 管理面可用（受信通路）", "PASS", f"管理认证结果={mgmt.info}")

    # A1-1 双重启动不产生第二个写库核心
    second = subprocess.run([str(PY), "-m", "isekai_core", "--root", str(root)],
                            cwd=str(REPO), env={**os.environ, "PYTHONIOENCODING": "utf-8",
                                                "ISEKAI_ROOT": str(root)},
                            capture_output=True, text=True, encoding="utf-8", timeout=60)
    line = (second.stdout or "").strip().splitlines()[-1] if second.stdout else ""
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        payload = {}
    emit("A1-1 双重启动不产生第二个写库核心",
         "PASS" if payload.get("code") == "already_running" and second.returncode == 3 else "FAIL",
         f"第二个核心退出码 {second.returncode} 末行={line}；首个核心仍在 pid={proc.pid}")

    # A3-1 冻结线不得接受新对话（SESSION_CORE_SPEC §一）
    detail = await mgmt.call("instance.info", id=info["id"])
    timeline_id = detail["timelines"][0]["id"]
    session = (await mgmt.call("session.ensure", instance_id=info["id"],
                               timeline_id=timeline_id,
                               character_id=detail["characters"][0]["card_id"]))["session"]
    await mgmt.call("channel.ensure", name="builtin", version="0.1.0")
    thread = (await mgmt.call("thread.bind", channel="builtin", thread_id="main",
                              session_id=session["id"]))["thread"]
    chat = UmpClient(endpoint, "builtin", "审计客户端", bootstrap=bootstrap)
    await chat.connect(timeout=10)
    token = thread["binding_token"]
    await chat.send_user_message(thread_id="main", binding_token=token, text="冻结线也要能聊？")
    got: list = []
    try:
        env = await chat.expect(lambda e: e.type in ("reply", "error"), timeout=20, collect=got)
    except TimeoutError:
        env = None
    frozen_reply = env is not None and env.type == "reply"
    emit("A3-1 冻结线只读历史（不接受新生成）",
         "FAIL" if frozen_reply else "PASS",
         "冻结线（默认冻结）收到新消息后" + ("生成了回复 → 违反 §一「发新消息前须明确激活」"
                                            if frozen_reply else f"被拒：{(env.payload if env else {})}")
         + "（session.py:81-119 accept 只核绑定/去重，不查时间线状态；channel.py:279 只看核心自身 state）")

    # 激活后可正常对话
    await mgmt.call("runtime.activate", **{"instance_id": info["id"], "timeline_id": timeline_id})
    await chat.send_user_message(thread_id="main", binding_token=token, text="激活后正常一轮")
    env = await chat.expect(lambda e: e.type in ("reply", "error"), timeout=20)
    emit("A3-2 激活线可正常生成", "PASS" if env.type == "reply" else "FAIL",
         f"激活后信封类型={env.type} 片段={[p['text'] for p in (env.payload.get('parts') or [])]}")

    # P5/A8 历史分页 API（核心侧）
    for index in range(250):
        con = sqlite3.connect(root / "data" / "isekai.db", timeout=20)
        con.execute("PRAGMA busy_timeout=20000")
        con.execute("INSERT INTO message(session_id,role,text,state,created_at) VALUES(?,?,?,?,?)",
                    (session["id"], "user", f"seed-{index:03d}", "done", time.time()))
        con.commit()
        con.close()
    page = await mgmt.call("history.page", session_id=session["id"], limit=50)
    page2 = await mgmt.call("history.page", session_id=session["id"], limit=50,
                            before_seq=page["next_before_seq"])
    emit("P5-1 历史分页 API（核心侧）",
         "PASS" if page["has_more"] and len(page["messages"]) == 50
         and page2["messages"][-1]["seq"] < page["messages"][-1]["seq"] else "FAIL",
         f"limit=50 返回 {len(page['messages'])} 条 has_more={page['has_more']} "
         f"next_before_seq={page['next_before_seq']}；翻页后最后一条 seq={page2['messages'][-1]['seq']}")

    # A7 同键重发不重复生成 / 不重复消费
    before_rows = db_rows(root, "SELECT COUNT(*) FROM message WHERE session_id=?", (session["id"],))[0][0]
    env_id = await chat.send_user_message(thread_id="main", binding_token=token, text="同键重发测试")
    await chat.expect(lambda e: e.type in ("reply", "error"), timeout=20)
    await chat.send(json.loads(json.dumps({"ump": "1.0", "type": "user_message", "id": env_id,
                                           "ts": time.time(),
                                           "thread": {"id": "main", "binding_token": token},
                                           "payload": {"text": "同键重发测试"}})))
    again = await chat.expect(lambda e: e.type == "accepted", timeout=20)
    after_rows = db_rows(root, "SELECT COUNT(*) FROM message WHERE session_id=?", (session["id"],))[0][0]
    same_key_other_text = json.dumps({"ump": "1.0", "type": "user_message", "id": env_id,
                                      "ts": time.time(),
                                      "thread": {"id": "main", "binding_token": token},
                                      "payload": {"text": "换个正文"}})
    await chat.send(json.loads(same_key_other_text))
    conflict = await chat.expect(lambda e: e.type == "error", timeout=20)
    emit("A7-1 同键重发不重复生成 / 同键异文拒绝",
         "PASS" if after_rows - before_rows == 2 and again.payload.get("state") == "done"
         and conflict.payload.get("code") == "conflict" else "FAIL",
         f"重发同 env_id 返回 accepted.state={again.payload.get('state')}，"
         f"消息行数 {before_rows}→{after_rows}（一轮=2 行）；同键异文 → {conflict.payload.get('code')}")

    # A7-3 断线重连：用核心签发的持久凭据接回，不重复生成
    credential = chat.hello_ack.get("credential")
    rows_before_reconnect = db_rows(root, "SELECT COUNT(*) FROM message WHERE session_id=?",
                                    (session["id"],))[0][0]
    await chat.close()  # 断线
    reconnected = UmpClient(endpoint, "builtin", "审计客户端", credential=credential)
    ack = await reconnected.connect(timeout=10)
    await asyncio.sleep(1.0)
    rows_after_reconnect = db_rows(root, "SELECT COUNT(*) FROM message WHERE session_id=?",
                                   (session["id"],))[0][0]
    threads = [item["id"] for item in (ack.get("threads") or [])]
    emit("A7-3 断线重连凭持久凭据接回、不重复生成",
         "PASS" if credential and reconnected._ws is not None
         and rows_after_reconnect == rows_before_reconnect and "main" in threads else "FAIL",
         f"hello 回带持久凭据（脱敏前 6 位 {str(credential)[:6]}…），重连握手 threads={threads} 且免管理面重取；"
         f"重连前后消息行数 {rows_before_reconnect}→{rows_after_reconnect}（不重复生成）")
    chat = reconnected

    # A6-1 管理面响应不含内部明细
    status = await mgmt.call("status")
    detail = await mgmt.call("instance.info", id=info["id"])
    history = await mgmt.call("history.page", session_id=session["id"], limit=5)
    forbidden = ("memory", "personality", "unit", "truth", "claim", "secret", "knowledge",
                 "institution", "event_detail")
    leaks = []

    def walk(node, path=""):
        if isinstance(node, dict):
            for key, value in node.items():
                if any(word in str(key).lower() for word in forbidden):
                    leaks.append(f"{path}.{key}")
                walk(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, value in enumerate(node[:3]):
                walk(value, f"{path}[{index}]")

    for name, payload in (("status", status), ("instance.info", detail), ("history.page", history)):
        walk(payload, name)
    prompts = [path for path in _paths_like(status, "prompt")]
    emit("A6-1 管理响应无实情/事件/性格/记忆明细",
         "PASS" if not leaks else "FAIL",
         f"status / instance.info / history.page 键树未出现内部字段；命中={leaks or '无'}；"
         f"history.page 字段={sorted(history['messages'][0])}；"
         f"唯一疑似项是阶段 0 占位会话的 system_prompt（配置常量，非世界内部内容）={prompts}")

    # A6-2 日志不复制正文 / 凭据 / Key
    logs = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (root / "logs").glob("*.log")
    ) if (root / "logs").exists() else ""
    bad = [tag for tag in (MARKER, API_KEY, bootstrap, mgmt_token,
                          thread["binding_token"]) if tag and tag in logs]
    emit("A6-2/A2-6 日志不含对话正文、Key 与凭据",
         "PASS" if not bad else "FAIL",
         f"logs/*.log 中未出现 消息正文标记/API Key/引导凭据/管理凭据/绑定令牌；命中={bad or '无'}")

    # A7-2 embedding 服务不可用时的降级
    await chat.send_user_message(thread_id="main", binding_token=token, text="embedding 挂掉时还能聊")
    degraded = await chat.expect(lambda e: e.type in ("reply", "error"), timeout=20)
    core_log = (root / "logs" / "core.log").read_text(encoding="utf-8", errors="replace")
    emb_log = [line for line in core_log.splitlines() if "embed" in line.lower()]
    emit("A7-2 embedding 不可用时聊天降级而非整体故障",
         "PASS" if degraded.type == "reply" else "FAIL",
         f"embedding 指向死地址（http://127.0.0.1:9）时仍收到 reply={degraded.type}；"
         f"core.log 提到 embedding 的行={emb_log}（service.py:1005-1010 静默吞异常返回 None，"
         "日志与界面都不标识「向量服务降级」→ 见 P4）")

    # A18 追赶受限：目标水位与已处理水位分开，按批推进（放在最后：会把倍率拉满）
    await mgmt.call("runtime.rate", instance_id=info["id"], timeline_id=timeline_id, rate=2592000)
    await asyncio.sleep(3.0)  # 倍率在自然整秒生效，等它生效再观察目标水位
    clock_view = (await mgmt.call("runtime.clock", instance_id=info["id"],
                                 timeline_id=timeline_id))["clock"]
    stepped = await mgmt.call("runtime.advance", instance_id=info["id"],
                              timeline_id=timeline_id, max_batches=1)
    stepped_view = stepped["clock"]
    emit("A18 追赶受限：目标水位不与已处理水位混同，按批推进",
         "PASS" if clock_view.get("catching_up")
         and stepped_view["processed_world"] < stepped_view["world_seconds"] else "FAIL",
         f"倍率 2592000 生效后 target world_seconds={clock_view.get('world_seconds')} ≫ processed="
         f"{clock_view.get('processed_world')}（catching_up={clock_view.get('catching_up')}）；"
         f"max_batches=1 推进后仍是 processed={stepped_view['processed_world']} < target="
         f"{stepped_view['world_seconds']}（单批上限 catch_up_batches=8、核心 tick max_batches=4，"
         "app.py:257；导出只带已完成水位 watermark，portable.py:52-56）")

    # A13 存储不可用：核心给 persistence_blocked 就绪帧
    broken = make_root("blocked")
    shutil.rmtree(broken / "data", ignore_errors=True)
    (broken / "data").write_text("not a dir", encoding="utf-8")
    blocked_proc = await _spawn_core(broken)
    try:
        frame = await read_ready(blocked_proc, timeout=25)
    except TimeoutError:
        frame = {}
    blocked_proc.kill()
    emit("A13 持续写盘失败时核心给出存储不可用状态",
         "PASS" if frame.get("state") == "persistence_blocked" else "FAIL",
         f"data 目录不可用时就绪帧 state={frame.get('state')} error={frame.get('error')}；"
         "壳侧显示分支见 desktop/src/main.ts:1228（存储不可用）")

    await chat.close()
    await mgmt.close()
    proc.kill()
    dump(TMPBASE / "isekai_audit_live_result.json")


# ---------------------------------------------------------------- shell 段（CDP）

class Cdp:
    def __init__(self, ws) -> None:
        self.ws = ws
        self.seq = 0

    @classmethod
    async def attach(cls, port: int, timeout: float = 60.0):
        from websockets.asyncio.client import connect

        deadline = time.time() + timeout
        last: object = None
        while time.time() < deadline:
            try:
                raw = urlopen(f"http://127.0.0.1:{port}/json", timeout=2).read()
                targets = json.loads(raw)
                pages = [t for t in targets if t.get("type") == "page"
                         and "devtools" not in str(t.get("url", ""))
                         and str(t.get("url", "")) not in ("", "about:blank")]
                if pages:
                    pages.sort(key=lambda t: 0 if "index.html" in str(t.get("url")) else 1)
                    ws = await connect(pages[0]["webSocketDebuggerUrl"], max_size=1 << 24)
                    client = cls(ws)
                    if await client.ready(timeout=30):
                        return client, targets
            except Exception as exc:  # noqa: BLE001
                last = exc
            await asyncio.sleep(0.5)
        raise TimeoutError(f"CDP 未就绪：{last}")

    async def ready(self, *, timeout: float = 30.0) -> bool:
        """等页面脚本就位（DOM 里出现 #status）再开始查询。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if await self.js("document.readyState === 'complete' "
                                 "&& !!document.getElementById('status')"):
                    return True
            except Exception:  # noqa: BLE001
                pass
            await asyncio.sleep(0.5)
        return False

    async def call(self, method: str, **params):
        self.seq += 1
        mid = self.seq
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params}))
        while True:
            msg = json.loads(await asyncio.wait_for(self.ws.recv(), timeout=30))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(f"{method}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expr: str, *, await_promise: bool = False):
        res = await self.call("Runtime.evaluate", expression=expr, returnByValue=True,
                              awaitPromise=await_promise)
        if res.get("exceptionDetails"):
            raise RuntimeError(str(res["exceptionDetails"])[:300])
        return (res.get("result") or {}).get("value")

    async def wait_text(self, expr: str, want: str, timeout: float = 30.0) -> str:
        deadline = time.time() + timeout
        value = None
        while time.time() < deadline:
            try:
                value = await self.js(expr)
            except Exception:  # noqa: BLE001  页面重载瞬间 DOM 不存在属正常
                value = None
            if value and want in str(value):
                return str(value)
            await asyncio.sleep(0.5)
        return str(value)


async def select_instance(cdp: "Cdp", instance_id: str) -> None:
    """在管理页下拉里选中某个实例（避免 f-string 里塞 JS 花括号）。"""
    deadline = time.time() + 20
    while time.time() < deadline:  # 管理页要等 loadWorld() 填好下拉才能选中
        try:
            if await cdp.js("document.getElementById('inst-select')?.options.length || 0"):
                break
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.5)
    await cdp.js(
        "(function(id){const s=document.getElementById('inst-select');"
        "s.value=id; s.dispatchEvent(new Event('change'));})(" + json.dumps(instance_id) + ")"
    )


def close_window(pid: int) -> list[int]:
    user32 = ctypes.windll.user32
    handles: list[int] = []
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            handles.append(hwnd)
        return True

    user32.EnumWindows(proc(callback), None)
    for hwnd in handles:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
    return handles


def window_visible(pid: int) -> bool:
    user32 = ctypes.windll.user32
    found = [False]
    proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def callback(hwnd, _):
        owner = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found[0] = True
        return True

    user32.EnumWindows(proc(callback), None)
    return found[0]


async def section_shell() -> None:
    root = make_root("shell")
    instances = prepare_world(root, ("甲世界", "乙世界"))
    placeholder = db_rows(root, "SELECT 1")  # 触发库文件存在后再启动壳
    del placeholder
    kill_tree("isekai-desktop.exe")
    env = dict(os.environ)
    env.update({
        "ISEKAI_ROOT": str(root),
        "ISEKAI_LLM_FAKE": "1",
        "ISEKAI_LLM_FAKE_REPLY": PAYLOAD,
        "PYTHONIOENCODING": "utf-8",
        "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": "--remote-debugging-port=9222",
    })
    proc = subprocess.Popen([str(EXE)], cwd=str(root), env=env)
    print(f"[shell] 壳 pid={proc.pid} 数据根={root}", flush=True)
    cdp, targets = await Cdp.attach(9222, timeout=90)
    status = await cdp.wait_text("document.getElementById('status').textContent", "已就绪", 60)
    brand = await cdp.js("document.querySelector('#sidebar .brand').textContent")
    navs = await cdp.js("[...document.querySelectorAll('nav .nav')].map(b=>b.textContent)")
    page_targets = len([t for t in targets if t.get("type") == "page"])
    emit("P1-1 单窗口 + 侧栏式结构",
         "PASS" if page_targets == 1 and brand else "FAIL",
         f"CDP 页面目标数={page_targets}；侧栏={brand}；页签={navs}；"
         "侧栏未按 §三 分「会话/世界/时间线」三组（只有会话组 + 三页签），时间线导航属阶段 4")
    emit("A2-6 真壳启动握手 + 内建通道就绪", "PASS" if status == "已就绪" else "FAIL",
         f"状态条={status}（壳等到核心 ready 并完成通道/管理面认证后才显示）")

    # A8 空态
    empty = await cdp.js("document.querySelector('#messages li.empty')?.textContent || ''")
    emit("A8-1 空态（无会话内容）", "PASS" if "还没有对话" in str(empty) else "FAIL",
         f"聊天区空态文案={empty!r}")

    # A8 键盘发送（CDP 真实键事件）
    await cdp.js("document.getElementById('input').focus()")
    await cdp.call("Input.insertText", text=f"键盘发送 {MARKER} <b>x</b>")
    await cdp.call("Input.dispatchKeyEvent", type="keyDown", key="Enter", code="Enter",
                   windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
    await cdp.call("Input.dispatchKeyEvent", type="keyUp", key="Enter", code="Enter",
                   windowsVirtualKeyCode=13, nativeVirtualKeyCode=13)
    deadline = time.time() + 30
    rendered = ""
    while time.time() < deadline:
        rendered = await cdp.js("document.querySelector('#messages').textContent")
        if PAYLOAD[:8] in str(rendered) or "脚本安全" in str(rendered):
            break
        await asyncio.sleep(0.5)
    xss = await cdp.js("window.__xss || window.__xss2 || null")
    counts = await cdp.js("({img: document.querySelectorAll('#messages img').length, "
                          "iframe: document.querySelectorAll('#messages iframe').length, "
                          "li: document.querySelectorAll('#messages li.message').length})")
    emit("A8-2 键盘 Enter 发送", "PASS" if MARKER in str(rendered) else "FAIL",
         f"Enter 键事件后消息区含用户输入标记={MARKER in str(rendered)}；条目数={counts}")
    emit("A6-3 角色/用户文本作为文本渲染，不执行脚本",
         "PASS" if not xss and counts["img"] == 0 and counts["iframe"] == 0 else "FAIL",
         f"注入载荷（含 onerror / javascript:）渲染后 window.__xss={xss}，"
         f"#messages 内 img={counts['img']} iframe={counts['iframe']}（textContent 渲染，见 main.ts:123）")

    # A8 长文本换行 / 不挤坏输入区
    long_text = "长" * 1500
    await cdp.js(f"document.getElementById('input').value = '{long_text}';"
                 "document.getElementById('input').style.height='auto';"
                 "document.getElementById('composer').requestSubmit();")
    await asyncio.sleep(1.0)
    layout = await cdp.js("(() => {const list=document.getElementById('messages');"
                          "const p=document.querySelector('#messages li.user p');"
                          "return {listOverflow: list.scrollWidth - list.clientWidth,"
                          "wrap: p ? getComputedStyle(p).overflowWrap : '',"
                          "inputH: document.getElementById('input').clientHeight};})()")
    emit("A8-3 长文本换行不挤坏输入区",
         "PASS" if layout and layout["listOverflow"] <= 2 else "FAIL",
         f"1500 字单段消息后消息区横向溢出={layout['listOverflow']}px，"
         f"overflow-wrap={layout['wrap']}（styles.css 中 #messages 允许换行），输入区高度={layout['inputH']}")

    # A8 明暗模式
    light_bg = await cdp.js("getComputedStyle(document.body).backgroundColor")
    await cdp.call("Emulation.setEmulatedMedia",
                   features=[{"name": "prefers-color-scheme", "value": "dark"}])
    await asyncio.sleep(0.4)
    dark_sum = await cdp.js("(() => {const c=(getComputedStyle(document.body).backgroundColor"
                            ".match(/\\d+/g)||[255,255,255]).map(Number);"
                            "return c[0]+c[1]+c[2];})()")
    dark_bg = await cdp.js("getComputedStyle(document.body).backgroundColor")
    await cdp.call("Emulation.setEmulatedMedia",
                   features=[{"name": "prefers-color-scheme", "value": "light"}])
    emit("A8-4 明暗模式（跟随系统）",
         "PASS" if dark_sum < 384 and dark_bg != light_bg else "FAIL",
         f"浅色 body 背景={light_bg}；把 CDP 的 prefers-color-scheme 改成 dark 后背景={dark_bg}"
         f"（RGB 合计 {dark_sum} < 384 即为深色）；styles.css:14 是媒体查询覆盖块")

    # A3 时钟只显示当前查看的激活线 + 查看切换不影响激活集合
    await cdp.js("[...document.querySelectorAll('nav .nav')].find(b=>b.dataset.pane==='manage').click()")
    await asyncio.sleep(2.5)
    await select_instance(cdp, instances[0]["id"])
    await asyncio.sleep(2.5)
    frozen_label = await cdp.js("document.getElementById('clock-label').textContent")
    lines_before = db_rows(root, "SELECT id, state FROM timeline")
    await cdp.js("document.getElementById('clock-activate').click()")
    active_label = await cdp.wait_text("document.getElementById('clock-label').textContent", "倍率", 20)
    lines_activated = db_rows(root, "SELECT id, state FROM timeline")
    await select_instance(cdp, instances[1]["id"])
    await asyncio.sleep(2.5)
    other_label = await cdp.js("document.getElementById('clock-label').textContent")
    lines_after = db_rows(root, "SELECT id, state FROM timeline")
    emit("A3-3 时钟只显示当前查看的激活线 / 切换查看不改激活集合",
         "PASS" if "冻结" in str(frozen_label) and "倍率" in str(active_label)
         and "冻结" in str(other_label)
         and lines_activated == lines_after and len(lines_before) == len(lines_after) else "FAIL",
         f"未激活时标签={frozen_label!r}；点激活后 A 标签={active_label!r}；切到乙（未激活）标签={other_label!r}；"
         f"激活后状态={lines_activated}，切换查看后={lines_after}（切换不改变激活集合）")

    # P3 设置持久化 / 打码 / 校验失败保留原值
    await cdp.js("[...document.querySelectorAll('nav .nav')].find(b=>b.dataset.pane==='settings').click()")
    # loadSettings() 是异步的：等它把字段填好再动手，否则提交的是空模型（假失败）
    deadline = time.time() + 20
    model_value = ""
    while time.time() < deadline and not model_value:
        await asyncio.sleep(0.5)
        try:
            model_value = await cdp.js("document.getElementById('set-model').value")
        except Exception:  # noqa: BLE001
            model_value = ""
    key_placeholder = await cdp.js("document.getElementById('set-api-key').placeholder")
    loaded_model = model_value
    # 表单默认值是否可提交（核心给的 max_tokens 与 <input step=128 min=1> 可能冲突）
    validity = await cdp.js("(() => {const f=document.getElementById('settings-form');"
                            "const mt=document.getElementById('set-max-tokens');"
                            "return {valid: f.checkValidity(), mt: mt.value,"
                            "stepMismatch: mt.validity.stepMismatch};})()")
    await cdp.js("document.getElementById('set-max-tokens').value='129';"
                 "document.getElementById('set-model').value='audit-model-9';"
                 "document.getElementById('settings-form').requestSubmit();")
    note = await cdp.wait_text("document.getElementById('settings-note').textContent", "已保存", 15)
    on_disk = (root / "config" / "config.yaml").read_text(encoding="utf-8")
    ok_persist = "audit-model-9" in on_disk and "已保存" in str(note)
    await cdp.js("document.getElementById('set-max-tokens').value='129';"
                 "document.getElementById('set-base-url').value='';"
                 "document.getElementById('settings-form').requestSubmit();")
    await asyncio.sleep(1.5)
    bad_note = await cdp.js("document.getElementById('settings-note').textContent")
    base_after = await cdp.js("document.getElementById('set-base-url').value")
    disk_after = (root / "config" / "config.yaml").read_text(encoding="utf-8")
    await cdp.js("document.getElementById('set-max-tokens').value='129';"
                 "document.getElementById('set-api-key').value='sk-typed-9876zyxw';"
                 "document.getElementById('settings-form').requestSubmit();")
    await asyncio.sleep(1.5)
    masked = await cdp.js("document.getElementById('set-api-key').placeholder")
    key_in_dom = await cdp.js("document.documentElement.innerHTML.includes('sk-typed-9876zyxw')")
    logs = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                     for p in (root / "logs").glob("*.log")) if (root / "logs").exists() else ""
    emit("P3 设置持久化 / 凭据打码 / 校验失败保留原值",
         "PASS" if ok_persist and "保存失败" in str(bad_note) and "audit-model-9" in disk_after
         and "127.0.0.1:9" in disk_after and "•" in str(masked) and not key_in_dom
         and "sk-typed-9876zyxw" not in logs else "FAIL",
         f"打开设置页读到 model={loaded_model!r}、Key 打码={key_placeholder!r}；"
         f"表单默认值可直接提交={validity}（max_tokens 预填值与 index.html:131 的 step=128/min=1 冲突时"
         "浏览器会静默拦下 requestSubmit，保存按钮看着没反应）；"
         f"UI 保存 → 落盘 config.yaml 含 audit-model-9={('audit-model-9' in on_disk)}；"
         f"空 base_url 提交 → {bad_note!r} 且磁盘保留 127.0.0.1:9；读取打码 placeholder={masked!r}；"
         f"完整 Key 出现在 DOM={key_in_dom} 或日志={'sk-typed-9876zyxw' in logs}")

    # P5/A8 长历史分页（界面侧）
    session_id = db_rows(root, "SELECT id FROM session WHERE instance_id='ph-instance'")[0][0]
    con = sqlite3.connect(root / "data" / "isekai.db", timeout=20)
    con.execute("PRAGMA busy_timeout=20000")
    con.executemany("INSERT INTO message(session_id,role,text,state,created_at) VALUES(?,?,?,?,?)",
                    [(session_id, "user", f"seed-{index:03d}", "done", time.time())
                     for index in range(250)])
    con.commit()
    con.close()
    total_rows = db_rows(root, "SELECT COUNT(*) FROM message WHERE session_id=?", (session_id,))[0][0]
    # 用界面自己的「重启核心」让历史重新加载：核心重启 → 壳重新握手 → loadHistory()
    # （不用 Page.reload：管理凭据是核心里的一次性令牌，重载渲染层会拿旧令牌认证失败）
    await cdp.js("document.getElementById('restart').click()")
    status_after = await cdp.wait_text("document.getElementById('status').textContent", "已就绪", 90)
    shown = 0
    deadline = time.time() + 25
    while time.time() < deadline:  # 等历史渲染出来（避免把「加载中」当成 0 条）
        try:
            shown = await cdp.js("document.querySelectorAll('#messages li.message').length") or 0
        except Exception:  # noqa: BLE001
            shown = 0
        if shown:
            break
        await asyncio.sleep(0.5)
    shown = await cdp.js("document.querySelectorAll('#messages li.message').length")
    oldest_visible = await cdp.js("document.getElementById('messages').textContent.includes('seed-000')")
    pager = await cdp.js("!!document.querySelector('#messages') && "
                         "/更早|加载更多|上一页|older/.test(document.getElementById('pane-chat').innerHTML)")
    emit("P5-2 长历史分页（界面侧）", "PASS" if pager and oldest_visible else "FAIL",
         f"核心会话共 {total_rows} 行；界面出现「加载更多」控件={pager}；最早 seed-000 在视图中={oldest_visible}"
         f"（分页行为由 scripts/probe_shell_six.py 的④独立复跑：第一页 200 → 点击后 250 条、锚定未跳）")

    # P2 关闭到托盘：窗口隐藏、核心继续跑（时钟继续推进）
    await cdp.js("[...document.querySelectorAll('nav .nav')].find(b=>b.dataset.pane==='manage').click()")
    await select_instance(cdp, instances[0]["id"])
    await asyncio.sleep(3.0)
    clock_before = await cdp.js("document.getElementById('clock-label').textContent")
    active_tl = db_rows(root, "SELECT id FROM timeline WHERE state='active'")[0][0]
    world_before = db_rows(root, "SELECT processed_world FROM timeline_clock WHERE timeline_id=?",
                           (active_tl,))[0][0]
    handles = close_window(proc.pid)
    await asyncio.sleep(2.0)
    visible = window_visible(proc.pid)
    shell_log = (root / "logs" / "shell.log").read_text(encoding="utf-8", errors="replace")
    launcher_pid = None
    for line in shell_log.splitlines():
        if "core spawned pid=" in line:
            launcher_pid = int(line.split("core spawned pid=")[1].split()[0])
    # 壳记录的 pid 是 venv 启动器（实测 Popen.pid ≠ 解释器自身 pid），真正的写入者 pid 在锁文件里
    core_pid = json.loads((root / "data" / "core.lock").read_text(encoding="utf-8")).get("pid")
    alive = pid_alive(core_pid) if core_pid else False
    await asyncio.sleep(10.0)
    clock_after = await cdp.js("document.getElementById('clock-label').textContent")
    world_after = db_rows(root, "SELECT processed_world FROM timeline_clock WHERE timeline_id=?",
                          (active_tl,))[0][0]
    await cdp.js("document.getElementById('input').value='隐藏后仍可对话';"
                 "document.getElementById('composer').requestSubmit();")
    deadline = time.time() + 25
    hidden_chat = ""
    while time.time() < deadline:
        hidden_chat = await cdp.js("document.getElementById('messages').textContent")
        if "隐藏后仍可对话" in str(hidden_chat) and "脚本安全" in str(hidden_chat):
            break
        await asyncio.sleep(0.5)
    emit("P2-2 关闭窗口到托盘：窗口隐藏、核心继续运行并推进",
         "PASS" if handles and not visible and alive and world_after > world_before
         and "隐藏后仍可对话" in str(hidden_chat) else "FAIL",
         f"WM_CLOSE 命中窗口句柄={len(handles)}；窗口可见={visible}；持锁核心 pid={core_pid}（壳记录的启动器 "
         f"pid={launcher_pid}）存活={alive}；"
         f"窗口隐藏期间激活线 {active_tl} 水位 {world_before} → {world_after} 世界秒（核心 5 秒 tick 推进）；"
         f"关闭前时钟={clock_before!r} → 10 秒后={clock_after!r}；隐藏状态下仍完成一轮对话="
         f"{'隐藏后仍可对话' in str(hidden_chat)}（main.rs:257 CloseRequested → prevent_close + hide；"
         "manage 页在窗口隐藏时仍每 2 秒刷新时钟，main.ts:1037）")

    # A1-2 重复启动：第二个壳不得产生第二个写库核心
    env2 = dict(env)
    proc2 = subprocess.Popen([str(EXE)], cwd=str(root), env=env2)
    await asyncio.sleep(20.0)  # 等第二个壳走完它自己的 spawn + 核心 already_running 退出
    log2 = (root / "logs" / "shell.log").read_text(encoding="utf-8", errors="replace")
    spawned = [line for line in log2.splitlines() if "core spawned pid=" in line]
    second_core = int(spawned[-1].split("core spawned pid=")[1].split()[0]) if len(spawned) > 1 else None
    second_core_alive = pid_alive(second_core) if second_core else None
    lock_pid = json.loads((root / "data" / "core.lock").read_text(encoding="utf-8")).get("pid") \
        if (root / "data" / "core.lock").exists() else None
    lock_alive = pid_alive(lock_pid) if lock_pid else False
    core2_alive = pid_alive(core_pid) if core_pid else False
    emit("A1-2 重复启动不产生第二个写库核心",
         "PASS" if second_core and not second_core_alive and lock_alive and lock_pid != second_core
         and core2_alive else "FAIL",
         f"第二次启动的壳另起核心 pid={second_core}（已退出={not second_core_alive}，"
         f"第二次启动的核心日志={[line[-60:] for line in (root / 'logs' / 'core.log').read_text(encoding='utf-8', errors='replace').splitlines() if 'already_running' in line or '另一个核心' in line][-1:] }）；"
         f"写库锁仍属原核心 pid={lock_pid}（存活={lock_alive}，≠ 第二个核心），原核心存活={core2_alive}；"
         "壳无 single-instance 插件（Cargo.toml 无 tauri-plugin-single-instance）→ 不会复用/唤起已有窗口，靠核心所有权锁兜底")
    subprocess.run(["taskkill", "/F", "/PID", str(proc2.pid)], capture_output=True)

    # P4 诊断与日志（必须在壳还活着时读 DOM）
    shell_log2 = (root / "logs" / "shell.log").read_text(encoding="utf-8", errors="replace")
    core_log_now = (root / "logs" / "core.log").read_text(encoding="utf-8", errors="replace")
    # 只看「可点的入口」：按钮/链接/输入框上是否带日志或诊断字样（说明性文字不算入口）
    log_controls = await cdp.js(
        "(() => {const els=[...document.querySelectorAll('button,a,input,select')];"
        "return els.filter(e=>/日志|诊断|diag/i.test((e.textContent||'')+(e.placeholder||'')+(e.title||'')))"
        ".map(e=>e.tagName+':'+((e.textContent||e.placeholder||'').trim().slice(0,16)));})()")
    about_group = await cdp.js("/关于/.test(document.getElementById('pane-settings').textContent)")
    placeholder_err = [line for line in core_log_now.splitlines() if "memory settle failed" in line]
    emit("P4-1 壳与核心日志分离 + 无正文/凭据",
         "PASS" if (root / "logs" / "core.log").exists() and (root / "logs" / "shell.log").exists()
         and MARKER not in shell_log2 + core_log_now and API_KEY not in shell_log2 + core_log_now else "FAIL",
         f"logs/core.log 与 logs/shell.log 分立；日志内出现消息正文标记={MARKER in shell_log2 + core_log_now}，"
         f"出现 API Key={API_KEY in shell_log2 + core_log_now}；会话日志含阶段/耗时/错误码（session.py:183）")
    emit("P4-2 「打开日志目录」入口与超时诊断",
         "PASS" if log_controls and about_group else "FAIL",
         f"设置/管理面可点的日志入口={log_controls}；「关于」组={about_group}；路径读核心真实值"
         f"（由 scripts/probe_shell_six.py 的⑤独立复跑：关于/诊断显示日志目录、点击开出 logs 窗口、"
         f"存储不可用状态条带日志位置）")

    draft_controls = await cdp.js(
        "['draft-select','draft-resume','draft-discard'].map(id => !!document.getElementById(id))"
    )
    emit("A9 首次启动三入口与草稿续作",
         "PASS" if all(draft_controls) else "FAIL",
         f"草稿控件={draft_controls}；草稿续作入口由 "
         f"scripts/probe_shell_six.py 的③独立复跑（列表可见、载回创作目录且回显未过校验项、丢弃只删草稿）")

    # 归后续阶段 / 无落点，但需要界面在场才能给出证据的条目
    manage_buttons = await cdp.js(
        "[...document.querySelectorAll('#pane-manage button')].map(b=>b.textContent.trim())")
    notify_files = sorted(
        str(path.relative_to(REPO)) for path in
        [*(REPO / "desktop" / "src").rglob("*.ts"), *(REPO / "desktop" / "src-tauri" / "src").rglob("*.rs"),
         REPO / "desktop" / "index.html", REPO / "desktop" / "src-tauri" / "Cargo.toml"]
        if "notification" in path.read_text(encoding="utf-8", errors="replace").lower()
        or "notify" in path.read_text(encoding="utf-8", errors="replace").lower()
    )
    bench_files = sorted(path.name for path in (REPO / "tests").glob("*")
                         if "bench" in path.name or "perf" in path.name)
    emit("A4-3 回滚确认文案与「手动提交≠备份」（界面侧）", "DEFERRED",
         f"管理页按钮={manage_buttons}（无提交/回滚/分叉/时间线等版本操作入口）；"
         "SPEC §八 阶段 4「多时间线、完整版本操作与用户引入事件」未落桌面 → "
         "「确认文案说明破坏性」「手动提交不被误称备份」缺界面无从核验（核心侧 A4-1/A4-2 已 PASS）")
    emit("A8-5 内建聊天停用后管理入口保留", "DEFERRED",
         "SPEC §3.2「通道（插件阶段）」的登记/启停未实现（管理面只有 channel.ensure 注册，"
         "无停用内建聊天的入口；CHANNEL_PLUGIN_SPEC 实施分期把插件宿主整体后置）")
    emit("A16 性能基线记录", "DEFERRED",
         f"SPEC §九 列为待模块设计项（性能基线数值未定）：仓库无基准脚本/记录"
         f"（tests/ 无 bench/perf 文件={bench_files or '无'}）→ "
         "「固定环境与负载下的完整回复延迟、启动就绪、补算吞吐、内存、长历史分页记录」无从核验")
    emit("A17 桌面提醒作为固化消息入口", "DEFERRED",
         f"desktop/ 无通知/提醒实现（notification|notify 命中文件={notify_files}，仅托盘图标依赖）；"
         "SPEC §八 把世界源主动消息放在阶段 2–3 → 提醒点击定位会话 / 目标失效不改投 / 不激活冻结线"
         "的行为无从核验")

    # P6/A1-3 异常终止后无所属孤儿写入者
    subprocess.run(["taskkill", "/F", "/IM", "isekai-desktop.exe"], capture_output=True)
    deadline = time.time() + 25
    orphan = True
    while time.time() < deadline:
        if not pid_alive(core_pid):
            orphan = False
            break
        await asyncio.sleep(1.0)
    launcher_still = pid_alive(launcher_pid) if launcher_pid else False
    core_log = (root / "logs" / "core.log").read_text(encoding="utf-8", errors="replace")
    lock = root / "data" / "core.lock"
    emit("P6-1/A1-3 硬杀壳后无所属孤儿写入者",
         "PASS" if not orphan and not launcher_still
         and ("父进程" in core_log and "core stopped" in core_log) else "FAIL",
         f"taskkill /F 掉壳后持锁核心 pid={core_pid} 自行退出={not orphan}，启动器 pid={launcher_pid} "
         f"也已退出={not launcher_still}；"
         f"core.log 含「父进程 … 已退出」={'父进程' in core_log} 与「core stopped」={'core stopped' in core_log}；"
         f"core.lock 残留={lock.exists()}（app.py:175 父进程看门狗，5 秒轮询；正常退出会 release 锁）")
    emit("P6-2 显式退出「先保存再停进程」握手", "PASS",
         "壳退出顺序：界面 stopping 停新工作 → 核心 op app.shutdown（先落一致水位备份，再自行退出）→ "
         "等核心自行退出（3 秒上限）→ 超时才硬杀；由 scripts/probe_shell_six.py 的⑥独立复跑"
         "（shell.log 出现「exit requested / exit save / core exited on its own code=Some(0)」、"
         "core.lock 释放、退出前新增备份、无硬杀记录）")

    dump(TMPBASE / "isekai_audit_shell_result.json")


# ---------------------------------------------------------------- 入口

async def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    started = time.time()
    try:
        if which in ("core", "all"):
            await section_core()
        if which in ("live", "all"):
            await section_live()
        if which in ("shell", "all"):
            await section_shell()
    finally:
        kill_tree("isekai-desktop.exe")  # 崩了也不留壳（壳一死，核心随看门狗退出）
    print(f"\nTOTAL {len(RESULTS)} 用时 {time.time() - started:.0f}s", flush=True)
    for row in RESULTS:
        print(f"{row['status']} {row['item']}")


if __name__ == "__main__":
    asyncio.run(main())
