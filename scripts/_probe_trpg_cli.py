"""CLI 端到端探针：TRPG 战役运行时（TRPG_CAMPAIGN_RUNTIME_SPEC）。

不是单测：它真的拉起核心进程（spawn），用 world_cli 逐条命令走完
战役 → 场景 → 行动 → 裁定 → 联合提交 → 规则状态 的链路，并把读数打出来。
运行：python scripts/_probe_trpg_cli.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

from samples import DAY, sample_card, sample_package  # noqa: E402

PY = sys.executable
CLI = [PY, "-m", "isekai_core.world_cli"]

PLUGIN = '''
import json, sys
request = json.loads(sys.stdin.readline())
state = request.get("rule_state") or {}
base = int(state.get("state_revision") or 0)
print(json.dumps({
    "resolution": {"system": "cli-probe", "outcome": "success"},
    "rule_state_patch": {
        "ruleset_id": state.get("ruleset_id"),
        "base_state_revision": base,
        "operations": [{"path": "/actors/pc-1/hp", "op": "add" if base == 0 else "increase", "value": 2}],
    },
    "consequences": [{
        "kind": "state_change", "operation": "set", "target_refs": ["off-1"], "value": "vacant",
        "expiry": "until_cleared", "certainty": "confirmed",
    }],
    "scene_transition": {"status": "advanced"},
    "claims": [{"text": "职位出现变动", "source_id": "src-1", "audience": "public"}],
    "participants": ["pc-1"],
}, ensure_ascii=False))
'''


CONVERTER = '''
import json, sys

request = json.loads(sys.stdin.readline())
state = request.get("opaque_state") or {}
actors = {key: {"hp": int((value or {}).get("hp") or 0)} for key, value in (state.get("actors") or {}).items()}
print(json.dumps({
    "opaque_state": {"actors": actors, "version": request.get("to_version")},
    "losses": [],
    "notes": f"{request.get('from_version')} -> {request.get('to_version')}",
}, ensure_ascii=False))
'''


def run(root: str, *argv: str) -> dict:
    cmd = CLI + ["--root", root, *argv]
    env = {**os.environ, "ISEKAI_LLM_FAKE": "1"}
    done = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", env=env)
    stdout = done.stdout or ""
    print(f"$ {' '.join(argv)}\n  rc={done.returncode} {(stdout.strip().splitlines() or [''])[-1][:160]}")
    if done.returncode != 0:
        print((stdout + (done.stderr or ""))[-2000:])
        raise SystemExit(f"命令失败：{argv}")
    # stdout 里只有结果 JSON（日志走 stderr 与 logs/）：取第一个 { 到最后一个 }
    start, end = stdout.find("{"), stdout.rfind("}")
    if start < 0 or end <= start:
        return {}
    return json.loads(stdout[start : end + 1])


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="isekai_trpg_probe_") as root:
        package = sample_package(moment=DAY * 1500)
        card = sample_card(package)
        (Path(root) / "pkg.json").write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        (Path(root) / "card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
        plugin = Path(root) / "rules"
        plugin.mkdir()
        (plugin / "main.py").write_text(PLUGIN, encoding="utf-8")
        (plugin / "convert.py").write_text(CONVERTER, encoding="utf-8")
        (plugin / "manifest.json").write_text(json.dumps({
            "id": "cli-probe", "name": "cli probe", "version": "1.0",
            "protocol": "isekai.trpg.rules/1", "entry": [PY, "main.py"],
        }), encoding="utf-8")

        info = run(root, "instance", "create", "--package", "pkg.json", "--card", "card.json")
        instance_id = str(info.get("id") or (info.get("instance") or {}).get("id") or "")
        if not instance_id:
            print("实例创建返回值里没有 id：", info)
            return 1
        listed = run(root, "instance", "info", "--id", instance_id)
        timelines = listed.get("timelines") or []
        timeline_id = str(timelines[0]["id"]) if timelines else ""
        if not timeline_id:
            print("实例信息里没有时间线：", listed)
            return 1

        # 让世界时钟跑起来：冻结线上时间消耗按语义不动（§十四）
        run(root, "runtime", "activate", "--id", instance_id, "--timeline", timeline_id)

        created = run(
            root, "trpg", "campaign-new", "--id", instance_id, "--timeline", timeline_id,
            "--ruleset", "cli-probe", "--ruleset-version", "1.0",
            "--plugin", str(plugin / "manifest.json"), "--actor", str(card["meta"]["card_id"]),
            "--kind", "conflict", "--ref", "rl-1",
        )
        campaign_id = str(created["campaign_id"])
        print(f"  战役 {campaign_id} / 场景 {created['scene_id']}")

        declared = run(
            root, "trpg", "declare", "--id", instance_id, "--timeline", timeline_id,
            "--campaign", campaign_id, "--actor", str(card["meta"]["card_id"]),
            "--intent", "调查墙后通道", "--auto-confirm",
        )
        action_id = str(declared["action_id"])
        run(
            root, "trpg", "resolve", "--id", instance_id, "--timeline", timeline_id,
            "--campaign", campaign_id, "--action", action_id,
            "--plugin", str(plugin / "manifest.json"),
        )
        committed = run(
            root, "trpg", "commit", "--id", instance_id, "--timeline", timeline_id,
            "--campaign", campaign_id, "--action", action_id, "--idempotency", "cli-1",
        )
        state = run(
            root, "trpg", "rule-state", "--id", instance_id, "--timeline", timeline_id,
            "--campaign", campaign_id,
        )
        consumed = run(
            root, "runtime", "consume-time", "--id", instance_id, "--timeline", timeline_id,
            "--seconds", "1800", "--cause", "夜行赶路",
        )
        gm = run(
            root, "trpg", "gm-change", "--id", instance_id, "--timeline", timeline_id,
            "--campaign", campaign_id, "--idempotency", "cli-gm",
            "--changes", json.dumps({
                "consequences": [{
                    "kind": "state_change", "operation": "set", "target_refs": ["off-1"],
                    "value": "occupied", "expiry": "until_cleared", "certainty": "confirmed",
                }],
                "claims": [{"text": "职位换了人", "source_id": "src-1", "audience": "public"}],
            }),
        )
        # 版本迁移：把清单的规则版本改掉（真升级场景），再走声明的转换器
        manifest_path = plugin / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["ruleset_version"] = "2.0"
        manifest["converters"] = [{
            "converter_id": "cli-1to2", "from_version": "1.0", "to_version": "2.0",
            "entry": [PY, "convert.py"],
        }]
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        migrated = run(
            root, "trpg", "migrate", "--id", instance_id, "--timeline", timeline_id,
            "--campaign", campaign_id,
        )
        state_after = run(
            root, "trpg", "rule-state", "--id", instance_id, "--timeline", timeline_id,
            "--campaign", campaign_id,
        )
        view = run(
            root, "trpg", "scene", "--id", instance_id, "--timeline", timeline_id,
            "--campaign", campaign_id,
        )
        print("\n== 读数 ==")
        print("世界时间消耗：", json.dumps({k: consumed["consume"].get(k) for k in
                                       ("consumed_seconds", "state", "processed_world", "cause")}, ensure_ascii=False))
        print("GM 直接变化：", json.dumps({k: gm.get(k) for k in ("status", "audience", "effects")}, ensure_ascii=False))
        print("规则版本迁移：", json.dumps({k: migrated.get(k) for k in
                                       ("status", "converter_id", "old_ruleset_version", "new_ruleset_version",
                                        "new_state_revision")}, ensure_ascii=False))
        print("提交：", json.dumps({k: committed.get(k) for k in ("status", "state_revisions", "effects")}, ensure_ascii=False))
        print("规则状态：", json.dumps({k: state.get(k) for k in ("state_revision", "opaque_state")}, ensure_ascii=False))
        print("可行动局面：", json.dumps({"recent": view.get("recent"), "rule_state": view.get("rule_state")}, ensure_ascii=False))
        print("迁移后规则状态：", json.dumps({k: state_after.get(k) for k in
                                       ("state_revision", "state_ruleset_version", "opaque_state")}, ensure_ascii=False))
        ok = (
            committed.get("status") == "committed"
            and consumed["consume"].get("state") == "current"
            and consumed["consume"].get("consumed_seconds") == 1800
            and gm.get("status") == "committed"
            and migrated.get("status") == "converted"
            and migrated.get("new_ruleset_version") == "2.0"
            and state_after.get("state_revision") == 2
            and state_after.get("state_ruleset_version") == "2.0"
            and (state_after.get("opaque_state") or {}).get("actors", {}).get("pc-1", {}).get("hp") == 2
            and [item["status"] for item in (view.get("recent") or [])] == ["transitioned"]
        )
        print("\nPASS" if ok else "\nFAIL")
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
