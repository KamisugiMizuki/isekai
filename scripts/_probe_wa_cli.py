"""CLI 端到端探针：编剧层（WRITING_ASSISTANT_SPEC §三 ~ §九）。

不是单测：每条命令都真的 spawn 一个核心进程，走 `isekai_core.world_cli` 的 `wa` 命令组，
链路是「建实例 → 存大纲 → 绑定 → 只读观察 → 评估缺口 → 提候选 → 批准 → 提交 → 条目决定 →
GM 声明与批准 → 模型提议 → 分支试演 → 世界回滚后重新评估」。

状态都在 SQLite 里，所以命令各自拉一个核心；`ISEKAI_LLM_FAKE=1` 不联网，
判断点脚本用 `ISEKAI_LLM_FAKE_JUDGEMENTS` 喂（正是为这条链路加的开关）。
运行：`.venv/Scripts/python.exe scripts/_probe_wa_cli.py`
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
FAILURES: list[str] = []
SUGGESTION = json.dumps({"candidates": [
    {"title": "碑文残片", "summary": "退潮的盐滩上露出半块碑文", "outline_ref": "it-know",
     "unsolved": ["缺的那半写的是什么"]},
    {"title": "议会的信", "summary": "驿站转来一封没署名的信", "outline_ref": "", "unsolved": []},
]}, ensure_ascii=False)


def run(root: str, *argv: str, judgements: bool = False) -> dict:
    cmd = CLI + ["--root", root, *argv]
    env = {**os.environ, "ISEKAI_LLM_FAKE": "1"}
    if judgements:
        env["ISEKAI_LLM_FAKE_JUDGEMENTS"] = json.dumps({"情节提议": SUGGESTION}, ensure_ascii=False)
    done = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", env=env)
    stdout = done.stdout or ""
    tail = (stdout.strip().splitlines() or [""])[-1][:110]
    print(f"$ wa {' '.join(argv)}\n  rc={done.returncode} {tail}")
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
    with tempfile.TemporaryDirectory(prefix="isekai_wa_cli_") as root:
        folder = Path(root) / "config"
        folder.mkdir(parents=True, exist_ok=True)
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

        info = run(root, "instance", "create",
                   "--package", str(creation / "pkg.json"), "--card", str(creation / "card.json"))
        instance_id = str(info.get("id") or (info.get("instance") or {}).get("id") or "")
        listed = run(root, "instance", "info", "--id", instance_id)
        timeline_id = str((listed.get("timelines") or [{}])[0].get("id") or "")
        expect("实例与时间线就位", bool(instance_id and timeline_id), f"{instance_id} / {timeline_id}")
        # world 激活（看世界得先有水位）
        activated = run(root, "runtime", "activate", "--id", instance_id, "--timeline", timeline_id)
        clock = activated.get("clock") or {}
        expect("世界线激活到她的白天",
               clock.get("state") == "active" and int(clock.get("world_seconds") or 0) > 0,
               f"状态={clock.get('state')}；水位={clock.get('world_seconds')}")

        # §三 新建大纲
        mark0 = run(root, "runtime", "commit", "--id", instance_id, "--timeline", timeline_id,
                    "--note", "起点")
        start_commit = str((mark0.get("commit") or {}).get("id") or "")
        expect("起点提交（试演与回滚的锚点）", bool(start_commit), f"commit={start_commit}")

        outline = {
            "id": "ol-1", "name": "潮汐志·第一卷",
            "items": [
                {"id": "it-know", "layer": "required_node", "title": "她得知道告警",
                 "statement": "堤禾在第一章结束前知道那份告警的存在", "scope": "timeline",
                 "success_criteria": "她的认知里出现告警相关内容",
                 "alternatives": ["由旁人转述"]},
                {"id": "it-theme", "layer": "theme", "title": "盐味与旧账",
                 "statement": "主题围绕记住与遗忘", "scope": "world", "success_criteria": "读者能说出主题"},
                {"id": "it-late", "layer": "required_node", "title": "到点没发生的节点",
                 "statement": "第三章之前拿到旧账本", "scope": "chapter",
                 "success_criteria": "账本出现在她的经历里", "watch_refs": ["ev-none"], "deadline_world": 1},
            ],
        }
        (creation / "outline.json").write_text(json.dumps(outline, ensure_ascii=False), encoding="utf-8")
        saved = run(root, "wa", "outline-save", "--file", str(creation / "outline.json"))
        layers = [item["layer"] for item in (saved.get("outline") or {}).get("items", [])]
        expect("§三 大纲落盘（条目分层）", layers == ["required_node", "theme", "required_node"], f"层级={layers}")
        got = run(root, "wa", "outline-get", "--outline", "ol-1")
        expect("§三 读回大纲", (got.get("outline") or {}).get("id") == "ol-1",
               f"名称={(got.get('outline') or {}).get('name')}；条目数={len((got.get('outline') or {}).get('items', []))}")

        # §4.1 绑定
        bound = run(root, "wa", "bind", "--id", instance_id, "--timeline", timeline_id,
                    "--outline", "ol-1", "--observers", character_id, "--chapter", "第一章")
        expect("§4.1 绑定到实例 + 时间线",
               (bound.get("state") or {}).get("outline_id") == "ol-1" and len((bound.get("state") or {}).get("items", [])) == 3,
               f"观察视角={character_id}；条目 {len((bound.get('state') or {}).get('items', []))} 条")

        # §5.2 只读观察（两层受众）
        player = run(root, "wa", "observe", "--id", instance_id, "--timeline", timeline_id,
                     "--observer", character_id, "--outline", "ol-1", "--audience", "player")
        gm = run(root, "wa", "observe", "--id", instance_id, "--timeline", timeline_id,
                 "--observer", character_id, "--outline", "ol-1", "--audience", "gm")
        player_text = json.dumps(player, ensure_ascii=False)
        expect("§5.2 玩家层不带主持依据",
               "gm_basis" not in player and "gm_basis" in gm and "崩堤当夜曾有人登堤敲钟" not in player_text,
               f"player 键={sorted(player)}；gm 有依据={'gm_basis' in gm}；"
               f"材料={len((player.get('player_view') or {}).get('materials') or [])} 条")

        # §十二 第三行：缺口报告
        report = run(root, "wa", "evaluate", "--id", instance_id, "--timeline", timeline_id, "--outline", "ol-1")
        gaps = sorted({gap["kind"] for gap in report.get("gaps") or []})
        expect("§十二 未达成硬约束只报缺口", "required_missing" in gaps,
               f"缺口={gaps}；评估水位={report.get('evaluated_world')}")

        # §4.3 候选：提出 → 批准 → 提交
        changes = [{"id": "c-1", "kind": "condition", "operation": "set", "certainty": "confirmed",
                    "target_refs": [character_id], "value": "封堤", "expiry": "until_cleared"}]
        (creation / "changes.json").write_text(json.dumps(changes, ensure_ascii=False), encoding="utf-8")
        (creation / "basis.json").write_text(
            json.dumps({"fact": "她守着水位尺", "causality": "封堤先于通行牌停发", "outline": "it-know"},
                       ensure_ascii=False), encoding="utf-8")
        proposed = run(root, "wa", "propose", "--id", instance_id, "--timeline", timeline_id,
                       "--outline", "ol-1", "--ref", "cd-1", "--kind", "world_change",
                       "--item", "it-know", "--display-name", "封堤", "--instruction", "堤上挂了封堤的木牌",
                       "--changes", str(creation / "changes.json"), "--basis",
                       json.dumps({"fact": "她守着水位尺", "causality": "封堤先于通行牌停发", "outline": "it-know"},
                                  ensure_ascii=False))
        expect("§4.3 候选带世界变化与依据", proposed.get("status") == "proposed" and bool(
            (proposed.get("candidate") or {}).get("preview_id")),
            f"status={proposed.get('status')}；预览={bool((proposed.get('candidate') or {}).get('preview_id'))}")
        approved = run(root, "wa", "candidate-decide", "--id", instance_id, "--timeline", timeline_id,
                       "--ref", "cd-1", "--status", "approved", "--reason", "批准采用")
        expect("§4.3 批准 ≠ 已提交",
               (approved.get("candidate") or {}).get("uncommitted") is True
               and (approved.get("candidate") or {}).get("must_not_imply") == "世界已经按它变了",
               f"未提交={(approved.get('candidate') or {}).get('uncommitted')}")
        committed = run(root, "wa", "commit", "--id", instance_id, "--timeline", timeline_id,
                        "--ref", "cd-1", "--idempotency", "wa-p1")
        expect("§4.3 提交成功才动世界",
               committed.get("status") == "ok" and (committed.get("candidate") or {}).get("status") == "committed",
               f"返回={committed.get('status')}；事件={committed.get('event_refs')}")

        # §4.2 条目状态：依据对得上才算达成
        decided = run(root, "wa", "item-decide", "--id", instance_id, "--timeline", timeline_id,
                      "--outline", "ol-1", "--item", "it-know", "--status", "achieved",
                      "--reason", "封堤落进世界了", "--evidence", ",".join(committed.get("event_refs") or []))
        expect("§4.2 达成要带可追溯依据",
               (decided.get("item") or {}).get("status") == "achieved"
               and (decided.get("item") or {}).get("evidence_refs"),
               f"状态={(decided.get('item') or {}).get('status')}；依据={(decided.get('item') or {}).get('evidence_refs')}")

        # §九 GM 声明 → 批准（经规则层联合提交）
        campaign = run(root, "trpg", "campaign-new", "--id", instance_id, "--timeline", timeline_id,
                       "--ruleset", "wa-probe", "--status", "active")
        campaign_id = str(campaign.get("campaign_id") or campaign.get("id") or "")
        gm_changes = {"consequences": [{"kind": "institution_state", "target": "off-1", "value": "vacant",
                                        "expiry": "until_cleared", "certainty": "confirmed"}],
                      "claims": [{"text": "堤长的位置空了出来", "source_id": "src-1", "audience": "公开"}]}
        (creation / "gm.json").write_text(json.dumps(gm_changes, ensure_ascii=False), encoding="utf-8")
        declared = run(root, "wa", "gm-declare", "--id", instance_id, "--timeline", timeline_id,
                       "--ref", "gm-1", "--campaign", campaign_id, "--display-name", "堤长去职",
                       "--gm-changes", str(creation / "gm.json"))
        expect("§九 GM 声明只是待批准结构",
               (declared.get("candidate") or {}).get("status") == "proposed"
               and (declared.get("candidate") or {}).get("source_mode") == "gm_declaration",
               f"status={(declared.get('candidate') or {}).get('status')}；"
               f"承诺文案={declared.get('must_not_imply')}")
        gm_done = run(root, "wa", "gm-approve", "--id", instance_id, "--timeline", timeline_id,
                      "--ref", "gm-1", "--idempotency", "wa-gm-1")
        expect("§九 批准后经规则层联合提交",
               gm_done.get("status") == "committed" and bool(gm_done.get("joint_commit_id")),
               f"返回={gm_done.get('status')}；联合提交={gm_done.get('joint_commit_id')}")

        # §六 模型提议（判断点脚本喂）
        suggested = run(root, "wa", "suggest", "--id", instance_id, "--timeline", timeline_id,
                        "--outline", "ol-1", "--observer", character_id, "--goal", "让她开始怀疑告警",
                        "--limit", "2", judgements=True)
        expect("§六 提议是未采用的候选",
               suggested.get("status") == "ok" and len(suggested.get("candidates") or []) == 2
               and all(item["uncommitted"] for item in suggested.get("candidates") or []),
               f"提议 {len(suggested.get('candidates') or [])} 条；"
               f"全未提交={all(item['uncommitted'] for item in suggested.get('candidates') or [])}")

        # §八 分支试演
        commit_id = start_commit
        branched = run(root, "wa", "branch", "--id", instance_id, "--timeline", timeline_id,
                       "--commit", commit_id, "--display-name", "试演线", "--outline", "ol-1")
        new_line = str((branched.get("timeline") or {}).get("id") or "")
        expect("§八 分支试演不改主线",
               bool(new_line) and (branched.get("timeline") or {}).get("state") == "frozen"
               and branched.get("note") == "分支继承共同过去；主线不被污染，项目不提供世界线合并",
               f"新线={new_line}；状态={(branched.get('timeline') or {}).get('state')}")

        # §8 世界回滚 → 重新评估
        run(root, "runtime", "rollback", "--id", instance_id, "--timeline", timeline_id,
            "--commit", commit_id, "--confirm")
        after = run(root, "wa", "evaluate", "--id", instance_id, "--timeline", timeline_id, "--outline", "ol-1")
        kinds = sorted({item["kind"] for item in after.get("deviations") or []})
        lost = [item for item in after.get("deviations") or [] if item["kind"] == "evidence_lost"]
        expect("§8 回滚后按目标时间线重新评估", "evidence_lost" in kinds,
               f"偏离={kinds}；依据已不在={bool(lost)}（{(lost[0]['detail'][:60] if lost else '')}）")
        state = run(root, "wa", "state", "--id", instance_id, "--timeline", timeline_id, "--outline", "ol-1")
        kept = [row for row in (state.get("public") or {}).get("candidates", []) if row["id"] == "cd-1"]
        expect("§8 文本与决定不随世界静默消失",
               bool(kept) and "未提交" not in str((kept[0] if kept else {}).get("status")),
               f"候选 {len((state.get('public') or {}).get('candidates', []))} 条；cd-1={kept[0]['status'] if kept else '（无）'}")

    print(f"\nTOTAL FAIL={len(FAILURES)}" + (f" 明细={FAILURES}" if FAILURES else ""))
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
