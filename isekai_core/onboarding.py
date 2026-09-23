"""首次使用支撑（`docs/user-interface/ONBOARDING_AND_RECOVERY.md`）。

四件事，都只做「读本机状态 / 复制随包材料 / 用正在编辑的值试一次调用」：

- **本机检查**（§4.1）：核心版本、受管目录可写、单写入者、数据格式可读——首次启动就查，
  不等到保存时才发现写不了盘；
- **随发行样例**（§3.1 / §4.3）：样例世界与角色卡随发行件提供，使用时**复制**成用户自己的材料，
  重复开始不覆盖已有材料（同名不同内容就换名字，绝不覆盖）；
- **AI 连接测试**（§4.2）：用**正在编辑的值**验证基础调用形态（简短文本 + 最小结构化输出），
  一个测试最多两次小请求（含内部重试总上限 4 次）——不生成世界、不发送用户材料；
- **界面草稿与请求身份**（§3.5 / §7.1）：草稿按「模块 + 对象」本地持久化；请求在受理前先留住身份，
  重复点击 / 断线重试按同一身份回到同一结果。

保存仍走 `settings.set`、创建仍走 `instance.create`：本模块不碰正式配置与正式材料。
"""

from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from .config import Config, LLMConfig, mask_api_key
from .llm import LLMClient, LLMError
from .log import get_logger
from .store import Store
from .ump import Err, UmpError
from .version import APP_VERSION, DATA_FORMAT_VERSION, RULES_VERSION
from .world.package import load_package, save_package
from .world.validate import validate_package

log = get_logger("isekai.onboarding")

#: 随发行样例所在的目录名（仓库根的 `examples/`；发行件里同名目录随包提供）
SAMPLE_DIR_NAME = "examples"
#: 本机检查的写盘探测文件（写完就删，不进备份）
WRITE_PROBE = "isekai-write-probe.tmp"

#: 连接测试的调用预算（§4.2）：一个测试最多两类小请求，含内部重试总上限 4 次
TEST_CALL_BUDGET = 4
TEST_TOTAL_BUDGET_S = 120.0
TEST_TIMEOUT_S = 60.0

#: 测试用固定文本：只够判断「基础调用形态可用」，不涉及用户的世界与聊天内容
TEST_TEXT_PROMPT = "连接测试：请只回复两个字「可用」。"
TEST_JSON_PROMPT = '连接测试：请只输出这样一个 JSON，不要任何解释——{"ok": true}'


def _writable(folder: Path) -> tuple[bool, str]:
    """受管目录可写探测：真写一个文件再删掉（照实报错，不猜测）。"""
    try:
        folder.mkdir(parents=True, exist_ok=True)
        probe = folder / WRITE_PROBE
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _readable_data_format(store: Store | None) -> tuple[bool, str]:
    """数据格式可读：库能读、记录的格式版本认得（读不出就是不可读，不猜）。"""
    if store is None:
        return True, "（未打开数据库：跳过读取检查）"
    try:
        row = store._conn.execute("SELECT value FROM meta WHERE key='data_format'").fetchone()
        counts = store.counts()
    except Exception as exc:  # noqa: BLE001 —— 读不了就是不通过，原因照实给
        return False, f"{type(exc).__name__}: {exc}"
    stored = str(row["value"]) if row else DATA_FORMAT_VERSION
    if stored.split(".")[0] != DATA_FORMAT_VERSION.split(".")[0]:
        return False, f"数据格式 {stored} 与当前版本 {DATA_FORMAT_VERSION} 的主版本不同"
    return True, f"可读（记录格式 {stored}，实例 {counts.get('instances', 0)} 个）"


def readiness(cfg: Config, *, server: Any = None, store: Store | None = None) -> dict[str, Any]:
    """首次设置第一步 / 帮助与诊断共用的本机检查（§4.1 步骤表第一行）。"""
    data_ok, data_detail = _writable(cfg.paths.data)
    format_ok, format_detail = _readable_data_format(store)
    lock = cfg.paths.lock
    writer_ok = True
    writer_detail = "本进程是唯一写入者"
    if server is None and lock.exists():
        writer_ok = False
        writer_detail = f"已有写入者在运行（{lock}）"
    config_ok = True
    config_detail = str(cfg.paths.config_file)
    if cfg.paths.config_file.exists():
        try:
            cfg.paths.config_file.read_text(encoding="utf-8")
        except OSError as exc:
            config_ok = False
            config_detail = f"{config_detail}（{type(exc).__name__}: {exc}）"
    else:
        config_detail = f"{config_detail}（还没有配置文件：保存设置时会创建）"
    counts: dict[str, int] = {}
    if store is not None:
        try:
            counts = store.counts()
        except Exception:  # noqa: BLE001
            counts = {}
    state = str(getattr(server, "state", "") or "")
    storage_detail = "存储可写"
    if not state and store is not None:
        try:
            store.write_probe()
            state = "ready"
        except Exception as exc:  # noqa: BLE001 —— 写不进就是写不进，照实报
            state = "persistence_blocked"
            storage_detail = f"{type(exc).__name__}: {exc}"
    elif state == "persistence_blocked":
        storage_detail = "存储不可写：不能产生新内容"
    checks = [
        {
            "key": "core",
            "label": "核心程序可用",
            "ok": bool(APP_VERSION),
            "detail": f"版本 {APP_VERSION}（规则 {RULES_VERSION}）",
        },
        {
            "key": "data",
            "label": "受管目录可写",
            "ok": data_ok,
            "detail": str(cfg.paths.data) if data_ok else data_detail,
            "fix": "" if data_ok else "检查这个目录的权限或换一个可写位置",
        },
        {
            "key": "writer",
            "label": "单一写入者",
            "ok": writer_ok,
            "detail": writer_detail,
            "fix": "" if writer_ok else "先关闭另一个正在运行的 isekai 窗口",
        },
        {
            "key": "format",
            "label": "数据格式可读",
            "ok": format_ok,
            "detail": format_detail,
            "fix": "" if format_ok else "先备份 data 目录，再用兼容版本打开",
        },
        {
            "key": "config",
            "label": "配置文件",
            "ok": config_ok,
            "detail": config_detail,
            "fix": "" if config_ok else "检查文件权限，或在设置里重新保存一次",
        },
        {
            "key": "storage",
            "label": "存储可写",
            "ok": state != "persistence_blocked",
            "detail": storage_detail,
            "fix": "" if state != "persistence_blocked" else "检查数据目录权限与剩余空间后重启核心",
        },
    ]
    llm = cfg.llm
    return {
        "app": APP_VERSION,
        "data_format": DATA_FORMAT_VERSION,
        "rules": RULES_VERSION,
        "state": state,
        "storage_ok": state != "persistence_blocked",
        "checks": checks,
        "ready": all(item["ok"] for item in checks),
        "paths": {
            "root": str(cfg.paths.root),
            "data": str(cfg.paths.data),
            "config": str(cfg.paths.config_file),
            "logs": str(cfg.paths.logs),
            "packages": str(cfg.paths.packages),
            "backups": str(cfg.paths.root / cfg.backup.dir),
        },
        "ai": llm_facts(llm),
        "counts": counts,
        "first_run": {
            "instances": int(counts.get("instances", 0) or 0),
            "sessions": int(counts.get("sessions", 0) or 0),
            "packages": len(list(cfg.paths.packages.glob("*.json"))) if cfg.paths.packages.exists() else 0,
        },
    }


def llm_facts(llm: LLMConfig) -> dict[str, Any]:
    """AI 服务的可公开事实：Key 只回「是否已设置」与允许的末尾片段（§4.2 字段规则）。"""
    return {
        "base_url": llm.base_url,
        "model": llm.model,
        "api_key_masked": mask_api_key(llm.api_key),
        "api_key_set": bool(llm.api_key),
        "timeout_s": llm.timeout_s,
        "max_tokens": llm.max_tokens,
        "temperature": llm.temperature,
        "configured": bool(llm.base_url and llm.model and llm.api_key),
    }


# ------------------------------------------------------------------ 随发行样例


def _sample_root(cfg: Config) -> Path:
    return cfg.paths.root / SAMPLE_DIR_NAME


def _split_sample(folder: Path) -> tuple[Path | None, list[dict[str, Any]]]:
    """把样例目录分成「世界包 + 角色卡」：按内容判定，不看扩展名。"""
    package: Path | None = None
    cards: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("calendar"), dict) and isinstance(payload.get("world"), dict):
            package = package or path
            continue
        if isinstance(payload.get("identity"), dict):
            cards.append(
                {
                    "file": path.name,
                    "name": str((payload.get("identity") or {}).get("name") or path.stem),
                    "card_id": str((payload.get("meta") or {}).get("card_id") or ""),
                    "confirmed": bool((payload.get("meta") or {}).get("confirmed")),
                }
            )
    return package, cards


def list_samples(cfg: Config) -> list[dict[str, Any]]:
    """随发行的样例（只回公开摘要：名称、简介、角色名单、是否已复制到创作目录）。"""
    root = _sample_root(cfg)
    if not root.exists():
        return []
    samples: list[dict[str, Any]] = []
    for folder in sorted(item for item in root.iterdir() if item.is_dir()):
        package_path, cards = _split_sample(folder)
        if package_path is None:
            continue
        try:
            payload = json.loads(package_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        target = cfg.paths.packages / package_path.name
        samples.append(
            {
                "id": folder.name,
                "title": str(meta.get("display_name") or meta.get("original_name") or folder.name),
                "description": str(meta.get("description") or ""),
                "package_file": package_path.name,
                "cards": cards,
                "installed": target.exists(),
            }
        )
    return samples


def find_sample(cfg: Config, sample_id: str) -> Path:
    folder = _sample_root(cfg) / str(sample_id or "")
    if not folder.is_dir():
        raise UmpError(Err.NOT_FOUND, f"没有这个样例：{sample_id}", retryable=False)
    return folder


def _unique_creation_path(cfg: Config, name: str) -> Path:
    """创作目录内不重名的落盘点：同名文件内容一致就复用，否则换名字（不覆盖用户材料）。"""
    safe = re.sub(r"[^0-9A-Za-z_\u4e00-\u9fa5.-]", "_", str(name).strip()) or "imported.json"
    target = cfg.paths.packages / safe
    if not target.exists():
        return target
    stem = target.stem
    suffix = target.suffix or ".json"
    for index in range(2, 100):
        candidate = cfg.paths.packages / f"{stem}-{index}{suffix}"
        if not candidate.exists():
            return candidate
    raise UmpError(Err.INVALID, f"同名材料太多：{safe}", retryable=False)


def install_sample(
    cfg: Config, sample_id: str, *, store: Store | None = None, request_id: str = ""
) -> dict[str, Any]:
    """把随发行样例复制成用户自己的材料（§4.3）：校验通过才落盘，重复开始不覆盖已有材料。"""
    op = "world.sample.install"
    if store is not None:
        cached = cached_request(store, request_id, op)
        if cached is not None:
            cached["reused"] = True
            return cached
    folder = find_sample(cfg, sample_id)
    package_path, cards = _split_sample(folder)
    if package_path is None:
        raise UmpError(Err.INVALID, f"样例里没有世界包：{sample_id}", retryable=False)
    package = load_package(package_path)
    errors = list(validate_package(package))
    if errors:
        raise UmpError(
            Err.INVALID,
            "样例世界包未通过校验，未落盘：" + "；".join(str(item) for item in errors[:5]),
            retryable=False,
        )
    meta = package.get("meta") if isinstance(package.get("meta"), dict) else {}
    cfg.paths.packages.mkdir(parents=True, exist_ok=True)
    package_target = cfg.paths.packages / package_path.name
    reused_package = package_target.exists()
    if not reused_package:
        save_package(package_target, package)
    copied_cards: list[dict[str, Any]] = []
    for card in cards:
        source = folder / str(card["file"])
        target = cfg.paths.packages / str(card["file"])
        if target.exists():
            copied_cards.append({**card, "file": target.name, "kept": True})
            continue
        payload = json.loads(source.read_text(encoding="utf-8"))
        target = _unique_creation_path(cfg, str(card["file"]))
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        copied_cards.append({**card, "file": target.name, "kept": False})
    result = {
        "status": "ok",
        "sample": str(sample_id),
        "title": str(meta.get("display_name") or meta.get("original_name") or sample_id),
        "description": str(meta.get("description") or ""),
        "package_file": package_target.name,
        "package_reused": reused_package,
        "cards": copied_cards,
        "characters": [{"card_id": item["card_id"], "name": item["name"], "file": item["file"]} for item in copied_cards],
        "must_not_imply": "样例已经创建成你的世界（这一步只是把材料复制进来）",
    }
    if store is not None:
        store.request_log_put(request_id, op, result)
    return result


# ------------------------------------------------------------------ AI 连接测试


def _json_from_text(text: str) -> Any:
    """最小结构化输出的解析：去围栏 → 取大括号段 → 解析（只判形态，不猜内容）。"""
    body = str(text or "").strip()
    if body.startswith("```"):
        body = re.sub(r"^```[a-zA-Z]*\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        pass
    start, end = body.find("{"), body.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(body[start : end + 1])
        except json.JSONDecodeError:
            return None
    return None


def _error_facts(exc: LLMError) -> dict[str, Any]:
    """把底层错误翻成用户能读懂的事实：状态码来自**本次实际响应**，不猜（§5 / §6）。"""
    status: int | None = None
    match = re.match(r"HTTP (\d{3})", str(exc.message or ""))
    if match:
        status = int(match.group(1))
    if status == 401:
        reason = (
            "服务没通过这把访问密钥（HTTP 401）：核对填进去的密钥是不是完整可用的那一串——"
            "有没有带引号或空格、是不是复制自平台的打码显示、末尾有没有被截断。"
        )
    elif status == 403:
        reason = f"服务拒绝了这次访问（HTTP 403）：密钥有效但权限不足，或该账号 / 地区被服务方限制。"
    elif status == 404:
        reason = "服务没有找到这个地址或模型（HTTP 404）：检查服务地址与模型名称。"
    elif status in (400, 422):
        reason = (
            f"服务不接受这次请求（HTTP {status}）：多半是模型名或高级参数的问题——"
            "检查模型名称与服务提供方一致、高级参数（输出长度 / 随机程度）在允许范围内。"
        )
    elif status == 429:
        reason = "服务暂时不接受请求（HTTP 429）：稍后再试，或检查该服务的用量与额度。"
    elif status is not None and status >= 500:
        reason = f"服务端出错（HTTP {status}）：这是服务方的问题，稍后再试。"
    elif exc.code == "llm_unreachable":
        reason = "连不上这个服务地址：检查地址、网络与代理设置。"
    elif exc.code == "empty_completion":
        reason = "模型返回了空内容：换一个模型，或调高单次输出长度。"
    elif exc.code == "truncated_completion":
        reason = "模型输出被长度上限截断：调高单次输出长度后重试。"
    elif exc.code == "llm_bad_response":
        reason = "服务返回的内容不是预期格式：确认这是 OpenAI 兼容接口。"
    else:
        reason = f"这次测试没有成功（{exc.code}）。"
    return {
        "code": str(exc.code),
        "status_code": status,
        "reason": reason,
        "retryable": bool(exc.retryable),
        # 服务端原话（截断）：写进日志用于定位，不上主界面（§5：原始原因放详情）
        "server_message": str(exc.message or "")[:200],
    }


def _probe_llm(base: LLMConfig, overrides: dict[str, Any]) -> tuple[LLMConfig, dict[str, Any]]:
    """用正在编辑的值造一个临时配置（不改正式配置）：空 Key 表示沿用已保存的那份。"""
    allowed = {"base_url", "model", "api_key", "timeout_s", "max_tokens", "temperature"}
    clean: dict[str, Any] = {}
    applied: dict[str, Any] = {}
    for key, value in (overrides or {}).items():
        if key not in allowed or value in (None, ""):
            continue
        if key == "api_key":
            clean[key] = str(value).strip()
        elif key in ("timeout_s", "max_tokens"):
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if number <= 0:
                continue
            clean[key] = float(int(number)) if key == "max_tokens" else number
        elif key == "temperature":
            try:
                number = float(value)
            except (TypeError, ValueError):
                continue
            if not 0 <= number <= 2:
                continue
            clean[key] = number
        else:
            clean[key] = str(value).strip()
        applied[key] = "已按编辑值" if key != "api_key" else "已按新填的密钥"
    probe = dataclasses.replace(base, **clean) if clean else dataclasses.replace(base)
    return probe, applied


async def test_llm(
    base: LLMConfig,
    overrides: dict[str, Any] | None = None,
    *,
    client_factory: Callable[[LLMConfig], Any] | None = None,
    timeout_s: float = TEST_TIMEOUT_S,
) -> dict[str, Any]:
    """用正在编辑的值测试基础调用形态（§4.2）：只发两段固定测试文字，不保存、不建任何东西。"""
    started = time.time()
    probe_cfg, applied = _probe_llm(base, overrides or {})
    stages: list[dict[str, Any]] = []

    def stage(key: str, label: str, ok: bool, detail: str = "") -> None:
        stages.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})

    # ① 检查地址（不发请求）：格式错在字段旁指出
    url = str(probe_cfg.base_url or "").strip()
    address_ok = bool(re.match(r"^https?://[^\s/]+", url))
    stage("address", "检查地址", address_ok, url or "（空地址）")
    checks: list[dict[str, Any]] = []
    calls = 0
    failure: dict[str, Any] = {}
    text_ok = False
    structured_ok = False

    factory = client_factory or (lambda cfg: LLMClient(cfg))
    client = factory(probe_cfg) if address_ok else None
    if client is not None and probe_cfg.api_key:
        remaining = max(0.0, TEST_TOTAL_BUDGET_S - (time.time() - started))
        # ② 验证访问 + 文本回复
        text_result: dict[str, Any] = {}
        try:
            reply = await client.chat(
                [{"role": "user", "content": TEST_TEXT_PROMPT}],
                timeout=min(timeout_s, max(1.0, remaining)),
            )
            calls += 1
            text_ok = bool(str(reply or "").strip())
            text_result = {"ok": text_ok, "detail": str(reply or "").strip()[:40]}
        except LLMError as exc:
            calls += 1
            failure = _error_facts(exc)
            text_result = {"ok": False, "detail": failure["reason"]}
        except Exception as exc:  # noqa: BLE001 —— 非 LLMError 也照实报，不伪装
            failure = {
                "code": type(exc).__name__,
                "status_code": None,
                "reason": f"测试时出现内部错误：{type(exc).__name__}",
                "retryable": False,
            }
            text_result = {"ok": False, "detail": failure["reason"]}
        checks.append({"key": "text", "label": "简短文本回复", **text_result})
        stage("access", "验证访问", text_ok or failure.get("status_code") != 401, failure.get("reason", ""))
        # ③ 检查回复格式：只有在文本可用且预算没超时才继续
        if text_ok and calls < TEST_CALL_BUDGET:
            remaining = max(0.0, TEST_TOTAL_BUDGET_S - (time.time() - started))
            try:
                raw = await client.chat(
                    [{"role": "user", "content": TEST_JSON_PROMPT}],
                    timeout=min(timeout_s, max(1.0, remaining)),
                )
                calls += 1
                parsed = _json_from_text(raw)
                structured_ok = isinstance(parsed, dict) and bool(parsed)
                checks.append(
                    {
                        "key": "structured",
                        "label": "最小结构化输出",
                        "ok": structured_ok,
                        "detail": str(raw or "").strip()[:60],
                    }
                )
            except LLMError as exc:
                calls += 1
                structured_ok = False
                checks.append(
                    {"key": "structured", "label": "最小结构化输出", "ok": False, "detail": _error_facts(exc)["reason"]}
                )
        stage("format", "检查回复格式", structured_ok, "" if structured_ok else "结构化输出未通过")
    else:
        reason = (
            "还没有填访问密钥" if client is not None else "服务地址格式不对（要以 http:// 或 https:// 开头）"
        )
        stage("access", "验证访问", False, reason)
        checks.append({"key": "text", "label": "简短文本回复", "ok": False, "detail": reason})
        checks.append({"key": "structured", "label": "最小结构化输出", "ok": False, "detail": reason})
        failure = {"code": "not_tried", "status_code": None, "reason": reason, "retryable": False}

    if client is not None and hasattr(client, "aclose"):
        try:
            await client.aclose()
        except Exception:  # noqa: BLE001 —— 关连接失败不影响测试结论
            pass
    duration_ms = int((time.time() - started) * 1000)
    ok = bool(text_ok and structured_ok)
    status = "ok" if ok else ("partial" if text_ok else "failed")
    result = {
        "status": status,
        "ok": ok,
        "text_ok": text_ok,
        "structured_ok": structured_ok,
        "service": probe_cfg.base_url,
        "model": probe_cfg.model,
        "api_key_masked": mask_api_key(probe_cfg.api_key),
        "key_set": bool(probe_cfg.api_key),
        "applied": applied,
        "stages": stages,
        "checks": checks,
        "calls": calls,
        "call_budget": TEST_CALL_BUDGET,
        "duration_ms": duration_ms,
        "tested_at": time.time(),
        "reason": str(failure.get("reason") or ""),
        "code": str(failure.get("code") or ""),
        "server_message": str(failure.get("server_message") or ""),
        "status_code": failure.get("status_code"),
        "retryable": bool(failure.get("retryable", False)),
        "note": "测试只发送两段固定测试文字，不发送你的世界与聊天内容；通过不代表任意篇幅的生成都会成功。",
    }
    log.info(
        "llm test status=%s calls=%s model=%s duration_ms=%s code=%s status_code=%s reason=%s message=%s",
        status,
        calls,
        probe_cfg.model,
        duration_ms,
        result.get("code"),
        result.get("status_code"),
        result.get("reason"),
        result.get("server_message"),
    )
    return result


# ------------------------------------------------------------------ 请求身份


def cached_request(store: Store, request_id: str, op: str) -> dict[str, Any] | None:
    """同一身份重复提交：回原结果（不重复执行）。身份为空则不做缓存。"""
    if not request_id:
        return None
    row = store.request_log_get(str(request_id))
    if row is None:
        return None
    if str(row.get("op") or "") != str(op):
        raise UmpError(
            Err.INVALID,
            f"这个请求身份已经用于别的操作（{row.get('op')}）：换一个身份或查询原结果",
            retryable=False,
        )
    try:
        result = json.loads(str(row.get("result") or "{}"))
    except json.JSONDecodeError:
        return None
    return result if isinstance(result, dict) else None
