"""世界设定层 CLI（阶段 1）：用管理面驱动核心完成世界包 / 角色卡 / 实例的全部操作。

用法（默认自行拉起核心，退出时关闭）：
  python -m isekai_core.world_cli package template --name 灰潮纪 --out greytide.json
  python -m isekai_core.world_cli package validate --file greytide.json
  python -m isekai_core.world_cli package generate --brief "退潮后的盐碱世界" --out greytide.json
  python -m isekai_core.world_cli card template --package greytide.json --name 堤禾 --out tihe.json
  python -m isekai_core.world_cli card confirm --package greytide.json --file tihe.json
  python -m isekai_core.world_cli instance create --package greytide.json --card tihe.json
  python -m isekai_core.world_cli instance list
  python -m isekai_core.world_cli instance export --id in-xxxx --out backup.json
  python -m isekai_core.world_cli instance import --file backup.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Sequence

from .client import MgmtClient
from .cli import spawn_core
from .config import load_config
from .world import ops

OP_BY_COMMAND = {
    ("package", "template"): "world.package.template",
    ("package", "load"): "world.package.load",
    ("package", "save"): "world.package.save",
    ("package", "validate"): "world.package.validate",
    ("package", "generate"): "world.package.generate",
    ("package", "revise"): "world.package.revise",
    ("package", "fill"): "world.package.fill",
    ("card", "template"): "world.card.template",
    ("card", "load"): "world.card.load",
    ("card", "save"): "world.card.save",
    ("card", "validate"): "world.card.validate",
    ("card", "confirm"): "world.card.confirm",
    ("card", "generate"): "world.card.generate",
    ("instance", "list"): "instance.list",
    ("instance", "create"): "instance.create",
    ("instance", "info"): "instance.info",
    ("instance", "setting"): "instance.setting",
    ("instance", "rename"): "instance.rename",
    ("instance", "delete"): "instance.delete",
    ("instance", "export"): "instance.export",
    ("instance", "import"): "instance.import",
    ("backup", "create"): "backup.create",
    ("backup", "restore"): "backup.restore",
    ("backup", "list"): "backup.list",
    ("runtime", "proactive"): "runtime.proactive",
    ("proactive", "list"): "proactive.list",
    ("runtime", "clock"): "runtime.clock",
    ("runtime", "activate"): "runtime.activate",
    ("runtime", "freeze"): "runtime.freeze",
    ("runtime", "rate"): "runtime.rate",
    ("runtime", "advance"): "runtime.advance",
    ("runtime", "card-add"): "runtime.card.add",
    ("runtime", "propose"): "runtime.propose",
    ("runtime", "extract"): "runtime.extract",
    ("runtime", "budget"): "runtime.budget",
    ("disclose", "confirm"): "disclose.confirm",
    ("disclose", "list"): "disclose.list",
    ("event", "draft"): "event.draft",
    ("event", "confirm"): "event.confirm",
    ("runtime", "timeline-rename"): "runtime.timeline.rename",
    ("runtime", "timeline-archive"): "runtime.timeline.archive",
    ("runtime", "timeline-delete"): "runtime.timeline.delete",
    ("runtime", "commit"): "runtime.commit",
    ("runtime", "commits"): "runtime.commits",
    ("runtime", "fork"): "runtime.fork",
    ("runtime", "rollback"): "runtime.rollback",
    ("runtime", "budget-set"): "runtime.budget.set",
    ("runtime", "backfill"): "runtime.backfill",
    ("event", "render"): "event.render",
    ("event", "expand"): "event.expand",
}


def build_args(ns: argparse.Namespace) -> dict[str, Any]:
    """按操作契约拼参数：世界包 = path/package，角色卡 = card_path/card，实例 = package_path + card_paths。"""
    group, cmd = ns.group, ns.command

    def read(path: str | None) -> dict[str, Any]:
        if not path:
            raise SystemExit("缺少文件路径（--file / --package / --card）")
        return json.loads(Path(path).read_text(encoding="utf-8"))

    if group == "package":
        if cmd == "template":
            return {"name": ns.name or "未命名世界", "density": ns.density or "normal"}
        if cmd in ("load", "save", "validate", "revise", "fill"):
            target = ns.file or ns.package
            args = {"path": target, "package": read(target)}
            if cmd == "revise":
                args["instruction"] = ns.instruction or ""
            if cmd == "fill":
                args["section"] = ns.section or ""
            return args
        if cmd == "generate":
            return {"brief": ns.brief or "", "name": ns.name or "未命名世界"}
    if group == "card":
        args: dict[str, Any] = {}
        if ns.package:
            args["package_path"] = ns.package
        if cmd == "template":
            return {**args, "name": ns.name or "未命名角色"}
        if cmd == "load":
            return {"card_path": ns.file}
        if cmd == "save":
            return {"card_path": ns.file, "card": read(ns.file)}
        if cmd in ("validate", "confirm"):
            return {**args, "card_path": ns.file, **({"moment": int(ns.moment)} if ns.moment else {})}
        if cmd == "generate":
            return {**args, "brief": ns.brief or ""}
    if group == "proactive":
        return {"instance_id": ns.id, "timeline_id": ns.timeline}
    if group == "backup":
        args = {}
        if cmd == "create":
            args["note"] = ns.note or ""
        if cmd == "restore":
            args["path"] = ns.file or ns.package
        return args
    if group == "disclose":
        args = {"instance_id": ns.id, "timeline_id": ns.timeline}
        if cmd == "confirm":
            args["from_character"] = ns.card or ""
            args["to_character"] = ns.at or ""
            args["refs"] = ns.ref or ""
            args["note"] = ns.note or ""
        if cmd == "list":
            args["to_character"] = ns.at or ""
        return args
    if group == "event":
        args = {"instance_id": ns.id, "timeline_id": ns.timeline}
        if cmd == "draft":
            args["intent"] = ns.instruction or ""
            args["payload"] = ns.candidate or ""
            args["commit_id"] = ns.commit or ""
        if cmd == "confirm":
            args["draft_id"] = ns.ref or ns.file or ""
            args["name"] = ns.display_name or ""
        if cmd == "render":
            args["event_id"] = ns.file
        if cmd == "expand":
            args["claim_id"] = ns.file
            args["character_id"] = ns.card
            if ns.note:
                args["question"] = ns.note
        return args
    if group == "runtime":
        args: dict[str, Any] = {"instance_id": ns.id, "timeline_id": ns.timeline}
        if cmd == "rate":
            args["rate"] = int(ns.rate or 0)
        if cmd == "activate" and ns.rate:
            args["rate"] = int(ns.rate)
        if cmd == "advance":
            args["max_batches"] = int(ns.max_batches or 16)
        if cmd == "timeline-rename":
            args["name"] = ns.display_name or ""
            args["description"] = ns.note
        if cmd == "timeline-delete":
            args["confirm"] = bool(ns.confirm)
        if cmd in ("commit", "rollback"):
            args["note"] = ns.note or ""
        if cmd in ("fork", "rollback"):
            args["commit_id"] = ns.file or ns.commit or ""
        if cmd == "fork":
            args["name"] = ns.display_name or ""
            args["activate"] = bool(ns.activate)
        if cmd == "rollback":
            args["confirm"] = bool(ns.confirm)
        if cmd == "budget-set":
            for key in ("instance_tokens_per_day", "timeline_tokens_per_day", "task_tokens_per_day"):
                value = getattr(ns, key, None)
                if value is not None:
                    args[key] = int(value)
            if ns.task:
                args["task"] = ns.task
                args["pause"] = bool(ns.pause) if not ns.pause_resume else False
        if cmd == "card-add":
            args["card_path"] = ns.card
            args["joined_world"] = int(ns.at) if ns.at is not None else None
            args["note"] = ns.note or ""
            args["acquainted"] = bool(ns.acquainted)
        return args
    if group == "instance":
        if cmd == "list":
            return {}
        if cmd == "create":
            return {
                "package_path": ns.package,
                "card_paths": [item.strip() for item in str(ns.card or "").split(",") if item.strip()],
                **({"display_name": ns.display_name} if ns.display_name else {}),
            }
        if cmd in ("info", "setting", "delete"):
            return {"id": ns.id}
        if cmd == "rename":
            return {"id": ns.id, "name": ns.name or ""}
        if cmd == "export":
            return {"id": ns.id, "path": ns.out}
        if cmd == "import":
            return {"path": ns.file, **({"display_name": ns.display_name} if ns.display_name else {})}
    raise SystemExit(f"未实现的命令：{group} {cmd}")


def persist(ns: argparse.Namespace, result: dict[str, Any]) -> None:
    """候选落盘：校验通过写 --out；未通过写 <out>.candidate.json（不冒充最终版本）。"""
    target = getattr(ns, "out", None)
    # 生成类返回 candidate，骨架类返回 package / card
    payload = result.get("candidate") or result.get("package") or result.get("card")
    if not target or not isinstance(payload, dict) or not payload:
        return
    path = Path(target) if not result.get("errors") else Path(f"{target}.candidate.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def print_result(result: dict[str, Any], *, out: str | None = None) -> int:
    errors = result.get("errors")
    if errors:
        print("校验未通过：")
        for item in errors:
            print(f"  - {item}")
        if result.get("candidate"):
            print("（候选未落盘为最终版本；修正后可 save 或重新 generate）")
        return 1
    if out and (result.get("candidate") or result.get("package") or result.get("card")):
        print(f"已写入 {out}")
    usage = result.get("usage")
    if isinstance(usage, dict):
        state = "已暂停（未继续重试）" if usage.get("paused") else "完成"
        print(f"用量：调用 {usage.get('calls')}/{usage.get('limit')} 次，{state}")
    print(json.dumps({key: value for key, value in result.items() if key != "candidate"}, ensure_ascii=False, indent=2))
    return 0


async def run(ns: argparse.Namespace) -> int:
    cfg = load_config(ns.root)
    op = OP_BY_COMMAND[(ns.group, ns.command)]
    proc = None
    endpoint, mgmt_token = ns.endpoint, ns.mgmt
    if endpoint is None:
        proc, ready = spawn_core(ns.root)
        endpoint, mgmt_token = ready["endpoint"], ready["mgmt"]
    mgmt = MgmtClient(endpoint, mgmt_token or "")
    await mgmt.connect()
    try:
        # ponytail: 生成类操作同步等待模型返回；真需要长任务队列时再改作业式接口
        timeout = 600.0 if op in ops.ASYNC_OPS else 30.0
        result = await mgmt.call(op, timeout=timeout, **build_args(ns))
    except Exception as exc:  # noqa: BLE001 —— CLI 只负责把错误讲清楚
        print(f"操作失败：{exc}")
        return 1
    finally:
        await mgmt.close()
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=10)
    persist(ns, result)
    return print_result(result, out=ns.out)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="isekai-world", description="世界设定层 CLI（阶段 1）")
    parser.add_argument("--root", default=None, help="数据根目录")
    parser.add_argument("--endpoint", default=None, help="连接已有核心（默认自行拉起）")
    parser.add_argument("--mgmt", default=None, help="已有核心的管理凭据")
    parser.add_argument(
        "group", choices=["package", "card", "instance", "runtime", "event", "disclose", "backup", "proactive"]
    )
    parser.add_argument("command", help="/".join(f"{g}.{c}" for g, c in OP_BY_COMMAND))
    parser.add_argument("--name", default=None)
    parser.add_argument("--density", default=None)
    parser.add_argument("--file", default=None, help="输入文件")
    parser.add_argument("--out", default=None, help="输出文件（结果落盘）")
    parser.add_argument("--package", default=None, help="目标世界包文件")
    parser.add_argument("--brief", default=None, help="对话式生成的描述")
    parser.add_argument("--instruction", default=None, help="修订指令")
    parser.add_argument("--section", default=None, help="待补全段落")
    parser.add_argument("--card", default=None, help="角色卡文件（多个用逗号分隔）")
    parser.add_argument("--display-name", dest="display_name", default=None)
    parser.add_argument("--id", default=None, help="实例标识")
    parser.add_argument("--timeline", default=None, help="时间线标识（运行层命令）")
    parser.add_argument("--rate", default=None, help="倍率（世界秒 / 现实秒）")
    parser.add_argument("--max-batches", dest="max_batches", default=None, help="单次推进的最大批数")
    parser.add_argument("--moment", default=None, help="校验基准时刻（世界秒）")
    parser.add_argument("--at", default=None, help="补卡：锚定补入的世界时刻（缺省=该线已完成水位）")
    parser.add_argument("--note", default=None, help="补卡：备注")
    parser.add_argument("--acquainted", action="store_true", help="补卡：声明与联络者已相识")
    parser.add_argument("--instance-tokens", dest="instance_tokens_per_day", type=int, default=None,
                        help="预算：实例总预算（token 量级 / 现实日）")
    parser.add_argument("--timeline-tokens", dest="timeline_tokens_per_day", type=int, default=None,
                        help="预算：单条时间线预算")
    parser.add_argument("--task-tokens", dest="task_tokens_per_day", type=int, default=None,
                        help="预算：单任务预算")
    parser.add_argument("--task", default=None, help="预算：要暂停 / 恢复的任务名")
    parser.add_argument("--pause", action="store_true", help="预算：暂停该任务")
    parser.add_argument("--commit", default=None, help="版本：提交标识")
    parser.add_argument("--ref", default=None, help="草案标识等引用")
    parser.add_argument("--confirm", action="store_true", help="版本：确认破坏性操作（回滚）")
    parser.add_argument("--activate", action="store_true", help="版本：分叉后立即激活")
    parser.add_argument("--resume", dest="pause_resume", action="store_true", help="预算：恢复该任务")
    parser.add_argument("--candidate", default=None, help="把文件内容当作候选对象提交")
    ns = parser.parse_args(argv)
    if (ns.group, ns.command) not in OP_BY_COMMAND:
        parser.error(f"未知命令 {ns.group} {ns.command}")
    return asyncio.run(run(ns))


if __name__ == "__main__":
    sys.exit(main())
