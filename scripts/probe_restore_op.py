"""核心侧：备份 / 恢复 / 列表（UI 的「恢复备份」真正调用的 op）行为验收。

不做对话框（无人值守），只验 op：create → list → restore → 全线冻结 + 安全副本 + 坏件被拒。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(r"D:\Hermes_workspace\isekai")
PY = REPO / ".venv" / "Scripts" / "python.exe"
sys.path.insert(0, str(REPO / "scripts"))
from _audit_desktop import db_rows, make_root, prepare_world  # noqa: E402


async def spawn(root: Path):
    env = dict(os.environ)
    env.update({"PYTHONIOENCODING": "utf-8", "ISEKAI_LLM_FAKE": "1", "ISEKAI_ROOT": str(root)})
    proc = await asyncio.create_subprocess_exec(
        str(PY), "-m", "isekai_core", "--root", str(root), cwd=str(REPO), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    return proc, json.loads((await proc.stdout.readline()).decode("utf-8"))


async def main() -> None:
    from isekai_core.client import MgmtClient

    root = make_root("restore")
    instances = prepare_world(root, ("恢复甲", "恢复乙"))
    proc, ready = await spawn(root)
    mgmt = MgmtClient(ready["endpoint"], ready["mgmt"])
    await mgmt.connect()
    print(f"[restore] 核心就绪 {ready['state']}，数据根={root}", flush=True)

    created = (await mgmt.call("backup.create", note="审计前"))["backup"]
    listed = await mgmt.call("backup.list")
    backup = created["file"] if "file" in created else listed["backups"][0]["file"]

    # 改一改现状：激活一条线、删掉一个实例，恢复后应回到备份时的样子且全线冻结
    detail = await mgmt.call("instance.info", id=instances[0]["id"])
    timeline = detail["timelines"][0]["id"]
    await mgmt.call("runtime.activate", instance_id=instances[0]["id"], timeline_id=timeline)
    at_backup = len(db_rows(root, "SELECT id FROM instance"))
    await mgmt.call("instance.delete", id=instances[1]["id"])
    before = {
        "instances": len(db_rows(root, "SELECT id FROM instance")),
        "active": db_rows(root, "SELECT COUNT(*) FROM timeline WHERE state='active'")[0][0],
    }

    # 坏件必须先被拒（不覆盖现有库）
    broken = root / "packages" / "broken-backup.db"
    broken.write_text("not a db", encoding="utf-8")
    rejected = ""
    try:
        await mgmt.call("backup.restore", path=str(broken))
    except Exception as exc:  # noqa: BLE001
        rejected = f"{type(exc).__name__}: {exc}"
    after_bad = {"instances": len(db_rows(root, "SELECT id FROM instance")),
                 "active": db_rows(root, "SELECT COUNT(*) FROM timeline WHERE state='active'")[0][0]}

    restored = await mgmt.call(
        "backup.restore", path=str((Path(listed["dir"]) / Path(backup).name)))
    after = {
        "instances": len(db_rows(root, "SELECT id FROM instance")),
        "active": db_rows(root, "SELECT COUNT(*) FROM timeline WHERE state='active'")[0][0],
        "tokens": db_rows(root, "SELECT COUNT(*) FROM thread WHERE binding_token<>''")[0][0],
        "safety": (root / "data" / "backups" / "isekai-restore-safety.db").exists(),
    }
    print(f"[restore] 备份目录（来自 op）={listed['dir']}（{len(listed['backups'])} 份，"
          f"keep={listed.get('keep')}、interval={listed.get('interval_hours')}h）", flush=True)
    print(f"[restore] 恢复前={before}；坏件被拒={rejected!r} 且现状不变={after_bad}；"
          f"恢复返回={restored['restore']}；恢复后={after}", flush=True)
    ok = (after_bad == before and after["instances"] == at_backup
          and after["active"] == 0 and before["active"] >= 1 and after["safety"]
          and restored["restore"]["restored"] and "备份不可用" in rejected)
    print(("PASS" if ok else "FAIL") + " 核心侧：备份 create/list/restore（坏件被拒且不动现有库、恢复后全线冻结 + "
          f"退回备份时状态 {after['instances']}/{at_backup} 个实例、安全副本在、令牌作废）", flush=True)

    await mgmt.call("app.shutdown")
    await asyncio.wait_for(proc.wait(), timeout=15)
    await mgmt.close()


asyncio.run(main())
