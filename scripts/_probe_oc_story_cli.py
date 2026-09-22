"""CLI 端到端探针：OC 故事层（OC_STORY_LAYER_SPEC §四 ~ §八）。

不是单测：真的拉起核心进程（spawn），用 `isekai_core.world_cli` 的 `story` 命令组
与开发 CLI 的 `--say`，走完「建实例 → 联络一轮 → 产品状态 → 分类 → 分支 → 恢复」的链路，
每条命令都把真实读数打出来。

命令都是各自拉一个核心（状态在 SQLite 里），用 `ISEKAI_LLM_FAKE=1` 不联网。
运行：`.venv/Scripts/python.exe scripts/_probe_oc_story_cli.py`
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
DEV = [PY, "-m", "isekai_core.cli"]
FAILURES: list[str] = []


def run(root: str, *argv: str, tool: list[str] | None = None) -> dict:
    cmd = (tool or CLI) + ["--root", root, *argv]
    env = {**os.environ, "ISEKAI_LLM_FAKE": "1"}
    done = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", env=env)
    stdout = done.stdout or ""
    tail = (stdout.strip().splitlines() or [""])[-1][:150]
    print(f"$ {' '.join(argv)}\n  rc={done.returncode} {tail}")
    if done.returncode != 0:
        print((stdout + (done.stderr or ""))[-2000:])
        raise SystemExit(f"命令失败：{argv}")
    start, end = stdout.find("{"), stdout.rfind("}")
    if start < 0 or end <= start:
        return {}
    return json.loads(stdout[start : end + 1])


def expect(clause: str, condition: bool, observed: str) -> None:
    status = "PASS" if condition else "FAIL"
    if not condition:
        FAILURES.append(clause)
    print(f"[{status:8}] {clause} :: {observed[:170]}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="isekai_ocstory_cli_") as root:
        folder = Path(root) / "config"
        folder.mkdir(parents=True, exist_ok=True)
        # 睡眠期等待压到 0.05s：探针不该为节拍等分钟
        (folder / "config.yaml").write_text(
            "runtime:\n  sleep_wait_min_s: 0.05\n  sleep_wait_max_s: 0.05\n", encoding="utf-8"
        )
        package = sample_package(moment=DAY * 1500 + 30000)
        card = sample_card(package)
        creation = Path(root) / "packages"
        creation.mkdir(parents=True, exist_ok=True)
        (creation / "pkg.json").write_text(json.dumps(package, ensure_ascii=False), encoding="utf-8")
        (creation / "card.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")
        character_id = str(card["meta"]["card_id"])

        # §4.1 首次进入：还没实例 → 准备中，步骤未就绪
        before = run(root, "story", "enter")
        expect("§4.1 未就绪时是准备中", before.get("product_state") == "preparing",
               f"product_state={before.get('product_state')}；steps 未就绪={[s['key'] for s in before.get('steps', []) if not s['done']]}")

        info = run(root, "instance", "create", "--package", "pkg.json", "--card", "card.json")
        instance_id = str(info.get("id") or (info.get("instance") or {}).get("id") or "")
        listed = run(root, "instance", "info", "--id", instance_id)
        timeline_id = str((listed.get("timelines") or [{}])[0].get("id") or "")
        expect("实例与时间线就位", bool(instance_id and timeline_id), f"{instance_id} / {timeline_id}")

        # 联络一轮（开发 CLI 的 --say）：激活 + 发一句话
        run(root, "--say", "我今天加班到很晚，有点累", "--instance", instance_id,
            "--timeline", timeline_id, "--character", character_id, "--activate", "--quiet", tool=DEV)

        entries = run(root, "story", "enter", "--id", instance_id, "--timeline", timeline_id,
                      "--card", character_id)
        done = {step["key"]: step["done"] for step in entries.get("steps") or []}
        expect("§4.1 六步就绪到会话",
               all(done.get(key) for key in ("describe", "review", "confirm", "create", "pick", "talk"))
               and entries.get("product_state") == "available",
               f"product_state={entries.get('product_state')}；steps={done}；"
               f"角色={[item['name'] for item in entries.get('characters') or []]}")

        turn = run(root, "story", "turn", "--id", instance_id, "--timeline", timeline_id,
                   "--card", character_id)
        expect("§3.5 一轮联络的产品结果", turn.get("status") == "expressed",
               f"status={turn.get('status')}；投递={turn.get('delivery', {}).get('state')}；"
               f"送达={turn.get('delivery', {}).get('delivered')}")
        expect("§4.4 已表达不暗示送达", turn.get("must_not_imply") == "通道已经送达或世界事实已改变",
               f"must_not_imply={turn.get('must_not_imply')}")

        home = run(root, "story", "home", "--id", instance_id, "--timeline", timeline_id,
                   "--card", character_id)
        texts = [item["text"] for item in home.get("messages") or []]
        expect("§6.1 首页给角色 / 世界 / 会话与已完成时刻",
               bool(home.get("character", {}).get("name")) and bool(home.get("time", {}).get("world_label")),
               f"角色={home.get('character', {}).get('name')}；世界={home.get('world', {}).get('name')}；"
               f"时刻={home.get('time', {}).get('world_label')}；消息 {len(texts)} 条")
        expect("§6.2 首页不带内部字段",
               not any(token in json.dumps(home, ensure_ascii=False)
                       for token in ("truth", "canon", "memory", "prompt", "audit")),
               f"keys={sorted(home)}")

        # §3.4 分类：结构性请求走预筛，分不清按联络
        rollback = run(root, "story", "classify", "--text", "回滚到上一个存档")
        expect("§3.4 版本操作转交", rollback.get("category") == "version_op"
               and rollback.get("handoff") == "version" and rollback.get("source") == "rule",
               f"{rollback.get('category')}/{rollback.get('source')} → {rollback.get('handoff')}；"
               f"说明={rollback.get('notice', '')[:40]}")
        change = run(root, "story", "classify", "--text", "帮我把世界设定改成终年下雪")
        expect("§3.4 改世界转创作流程", change.get("handoff") == "creation" and "没有执行" in change.get("notice", ""),
               f"{change.get('category')} → {change.get('handoff')}")
        vague = run(root, "story", "classify", "--text", "嗯……")
        expect("§3.4 分不清按联络", vague.get("category") == "contact_share",
               f"{vague.get('category')}/{vague.get('source')}")

        # §八 版本与创作操作
        mark = run(root, "runtime", "commit", "--id", instance_id, "--timeline", timeline_id, "--note", "分支点")
        commit_id = str((mark.get("commit") or {}).get("id") or "")
        run(root, "runtime", "consume-time", "--id", instance_id, "--timeline", timeline_id,
            "--seconds", "3600", "--cause", "日常推进")
        branched = run(root, "story", "branch", "--id", instance_id, "--timeline", timeline_id,
                       "--commit", commit_id, "--display-name", "另一种可能")
        expect("§八 保留分支", branched.get("status") == "ok" and branched.get("timeline", {}).get("state") == "frozen",
               f"新线={branched.get('timeline', {}).get('id')}（{branched.get('timeline', {}).get('state')}）；"
               f"提示={branched.get('note', '')[:40]}")

        preview = run(root, "story", "restore", "--id", instance_id, "--timeline", timeline_id,
                      "--commit", commit_id)
        expect("§八 恢复前先说覆盖范围与保存路径",
               preview.get("status") == "waiting"
               and {item["action"] for item in preview.get("save_paths") or []} == {"branch", "export"},
               f"status={preview.get('status')}；覆盖 {preview.get('coverage', {}).get('delta_seconds')} 秒；"
               f"提示={preview.get('warning', '')[:50]}")
        held = run(root, "story", "restore", "--id", instance_id, "--timeline", timeline_id,
                   "--commit", commit_id, "--confirm")
        expect("§八 没先保存时停住", held.get("status") == "waiting" and "先保存" in held.get("reason", ""),
               f"status={held.get('status')}；原因={held.get('reason', '')[:60]}")
        done_restore = run(root, "story", "restore", "--id", instance_id, "--timeline", timeline_id,
                           "--commit", commit_id, "--confirm", "--saved")
        expect("§八 显式确认后执行恢复", done_restore.get("status") == "ok",
               f"status={done_restore.get('status')}；generation={done_restore.get('result', {}).get('generation')}；"
               f"提示={done_restore.get('warning', '')[:60]}")

        scene = run(root, "story", "scene", "--id", instance_id, "--timeline", timeline_id, "--card", character_id)
        expect("§4.4 场景状态可读", scene.get("product_state") in ("available", "catching_up", "blocked"),
               f"product_state={scene.get('product_state')}（可做 {scene.get('can')}）")

    print(f"\nFAIL={len(FAILURES)}{'' if not FAILURES else '：' + '、'.join(FAILURES)}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
