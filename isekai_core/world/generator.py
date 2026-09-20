"""世界包与角色卡生成器（表单式 + 对话式），桌面端与安卓端共用同一套规则。

- 共享 schema / 模板 / 校验 / 生成与修订流程：两端都通过核心调用，不各自实现一遍（§2.5）；
- AI 只产候选：候选经同一校验器检查，**失败不落盘**，用户确认后才成为最终版本；
- 校验失败时把错误清单回灌一次，重试仍失败就原样交回候选与错误，由用户改。
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..llm import LLMClient, LLMError
from ..log import get_logger
from .cards import validate_card
from .example import example_package
from .package import PackageError, clone_package, template_package
from .validate import validate_package

log = get_logger("isekai.world.generator")

#: 形状参考：模型照此填写字段类型与粒度（空骨架看不出 effects / contributors 该长什么样）
EXAMPLE = example_package()

GENERATOR_BUDGET = 8192
#: 长产物（世界包分段 / 角色卡）的单次等待上限：60s 默认超时在长预算下会被截断成 ReadTimeout
GENERATOR_TIMEOUT_S = 300.0
#: 结构化产物用低温：默认温度按对话场景设定，生成整包 JSON 时残句率明显更高
GENERATOR_TEMPERATURE = 0.4

#: 世界包分段生成：一次调用产出整包会被长度上限截断，按语义分三段、逐段校验
PACKAGE_SEGMENTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("设定核心", ("meta", "calendar", "world")),
    ("双轨与名册", ("sources", "canon", "narratives", "races", "entities")),
    ("机制与现状", ("historiography", "environment", "events", "life", "roles", "comms", "initial_state")),
)

STRUCTURE_HINT = (
    "只输出一个 JSON 对象，不要输出解释、Markdown 代码围栏或多余文字；输出紧凑 JSON（不要缩进换行）。"
    "顶层键必须与给定骨架完全一致，不得增删键名；所有标识（id）用短横线小写英文，稳定且互不重复。"
    "id 字段只填短标识（如 cf-1、ef-1、et-1），不填描述；引用字段只填给定标识，不填自然语言。"
)
MIN_CONTENT = (
    "内容最小标准（不满足即视为生成失败）：至少两条世界公理；至少一条命名语汇；"
    "实情层与说法层分开存放，每条说法必须有来源与获知条件；至少一个种族并给出寿命覆盖；"
    "至少一份史料，含贡献者角色与时段、覆盖区间与非空条目；至少一个事件族及其模板与事实效果"
    "（效果 target 只能填已存在的标识，且每条效果必须给 expiry：with_cause / until_cleared / "
    "natural_recovery 三选一，natural_recovery 还要给 recovery 条件）；"
    "events.density 必填（稀疏 / 常规 / 丰盛 三选一）；环境效果（environment_state）只能引用本包 "
    "environment.types 里已声明的类型与取值域内的值，先声明类型再引用；"
    "至少一个生活线模板（显式声明是否睡眠）与一个可装配的角色模板；至少一种与外界联络的机制。"
    "声明了制度就必须写明职权、适用范围、延续与承接规则；制度还可声明职位（offices：id / name / holder，holder 留空即空缺）"
    "与空缺规则（vacancy_policy：continues / suspended 两份事务名清单，缺一不可）；"
    "声明了惯例就必须写明适用群体、当前做法、形成依据与允许变化范围；"
    "给了 forms 就必须把当前做法 practice 写进 forms 里，惯例变化只能落在 forms 内。"
)


def stamp_generator_meta(package: dict[str, Any], *, model: str = "") -> dict[str, Any]:
    """给生成出来的包 / 卡打上生成器指纹与文本模型（管理元数据，不是世界事实）。"""
    meta = package.get("meta") if isinstance(package.get("meta"), dict) else {}
    meta["generator_fingerprint"] = _fingerprint(model)
    if model:
        meta["generator_model"] = str(model)
    package["meta"] = meta
    return package


def _available_ids(package: dict[str, Any]) -> dict[str, list[str]]:
    """已有段落的稳定标识：分段生成时后续段落必须引用它们，而不是自己另造。"""
    out: dict[str, list[str]] = {}
    for key in ("sources", "canon", "narratives", "races", "entities", "historiography", "life", "roles"):
        items = package.get(key)
        if isinstance(items, list):
            ids = [str(item.get("id")) for item in items if isinstance(item, dict) and _text(item.get("id"))]
            if ids:
                out[key] = ids
    return out


def _text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def extract_json(text: str) -> dict[str, Any]:
    """容错取 JSON：容忍代码围栏、前后说明文字与常见的逗号残句。"""
    body = text.strip()
    if body.startswith("```"):
        body = body.split("```")[1] if "```" in body[3:] else body[3:]
        if body.lstrip().lower().startswith("json"):
            body = body.lstrip()[4:]
    start = body.find("{")
    end = body.rfind("}")
    if start == -1 or end <= start:
        raise PackageError("模型输出里没有 JSON 对象")
    snippet = body[start : end + 1]
    try:
        parsed = json.loads(snippet)
    except json.JSONDecodeError as exc:
        repaired = re.sub(r",(\s*[}\]])", r"\1", snippet)  # 尾随逗号是最常见的模型笔误
        try:
            parsed = json.loads(repaired)
        except json.JSONDecodeError:
            log.warning("generator json parse failed len=%s at=%s", len(snippet), exc.pos)
            raise PackageError(f"模型输出的 JSON 无法解析：{exc}") from exc
    if not isinstance(parsed, dict):
        raise PackageError("模型输出不是 JSON 对象")
    return parsed


def _merge_structure(candidate: dict[str, Any], skeleton: dict[str, Any]) -> dict[str, Any]:
    """以骨架为准补齐缺失键（模型偶尔漏段），不缺内容不缺字段。"""
    merged = clone_package(skeleton)
    for key, value in candidate.items():
        if key in merged:
            merged[key] = value
    return merged


#: 单次生成请求的默认调用上限（含重试）：三段各两次 / 卡片两次（§2.4 用量预算）
def _fingerprint(model: str = "") -> str:
    """本生成器的指纹（段定义 + 结构/最小内容提示 + 模型名）。"""
    from ..version import generator_fingerprint

    return generator_fingerprint(
        segments=tuple(f"{name}:{','.join(keys)}" for name, keys in PACKAGE_SEGMENTS),
        hints=(STRUCTURE_HINT, MIN_CONTENT),
        model=model,
    )


DEFAULT_PACKAGE_CALLS = len(PACKAGE_SEGMENTS) * 2
DEFAULT_CARD_CALLS = 2


class BudgetExhausted(Exception):
    """调用预算用尽：暂停并保留已完成的段落，不继续重试扩支。"""


def _spend(budget: dict[str, int], label: str) -> None:
    if budget["calls"] >= budget["limit"]:
        raise BudgetExhausted(f"{label}：已达确认的调用上限")
    budget["calls"] += 1


def _usage(budget: dict[str, int], *, paused: bool) -> dict[str, Any]:
    return {"calls": budget["calls"], "limit": budget["limit"], "paused": paused}


async def generate_package(
    llm: LLMClient, brief: str, *, name: str = "未命名世界", max_calls: int = DEFAULT_PACKAGE_CALLS
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """对话式：用户描述 → 候选世界包（分段生成，逐段校验）→ 整包校验。

    返回 (候选, 错误列表, 用量)；任一必需段落失败或调用预算用尽即整份不落盘，由调用方交回用户。
    """
    skeleton = template_package(name)
    package = clone_package(skeleton)
    budget = {"calls": 0, "limit": max(1, int(max_calls))}
    for label, keys in PACKAGE_SEGMENTS:
        sub_skeleton = {key: skeleton[key] for key in keys}
        system = (
            "你是世界设定层的世界包生成器。按用户描述生成一份完整、自洽、内部不矛盾的世界包。"
            "地理、社会、历法、势力与信息渠道必须能互相解释；不要输出现实世界专有名词。"
            f"这次只产出这些键：{'、'.join(keys)}。" + STRUCTURE_HINT + MIN_CONTENT
        )
        available = _available_ids(package)
        user = (
            f"用户描述：\n{brief}\n\n"
            f"已生成的部分（保持一致，不要重复输出）：\n{json.dumps({k: package[k] for k in package if k not in keys}, ensure_ascii=False)}\n\n"
            + (
                "可引用的既有标识（引用字段只能填这些，不要臆造，也不要留空字符串）：\n"
                f"{json.dumps(available, ensure_ascii=False)}\n"
                "史料条目 entries 只能引用 canon / narratives 的标识；每位贡献者必须给出 role 与 period（如「崩塌后第 3 年」）。\n\n"
                if available
                else ""
            )
            + f"本次必须遵守的 JSON 骨架：\n{json.dumps(sub_skeleton, ensure_ascii=False)}\n\n"
            f"形状参考（字段类型与粒度照此填写，内容按用户描述替换；这是示例世界，不要照抄其中设定）：\n"
            f"{json.dumps({key: EXAMPLE[key] for key in keys if key in EXAMPLE}, ensure_ascii=False)}"
        )

        def check(candidate: dict[str, Any], current: dict[str, Any] = package, only: tuple[str, ...] = keys) -> list[str]:
            """只看本段负责的顶层键：别的段落还没生成，不该在本段报错。"""
            merged = {**current, **{key: candidate.get(key, current[key]) for key in only}}
            return [item for item in validate_package(merged) if item.split(":")[0].split(".")[0].split("[")[0] in only]

        try:
            candidate, errors = await _generate_with_retry(
                llm, system, user, sub_skeleton, check, label=f"世界包·{label}", budget=budget
            )
        except BudgetExhausted as exc:
            return {**package}, [str(exc)], _usage(budget, paused=True)
        if errors:
            return {**package, **candidate}, [f"{label}：{item}" for item in errors], _usage(budget, paused=False)
        for key in keys:
            package[key] = candidate.get(key, package[key])
    # 文本产物的边界：谁、用什么模型生成的（管理元数据；不参与事实与可读性判定）
    stamp_generator_meta(package, model=str(getattr(llm, "model", "") or ""))
    return package, validate_package(package), _usage(budget, paused=False)


async def revise_package(
    llm: LLMClient, package: dict[str, Any], instruction: str, *, max_calls: int = DEFAULT_CARD_CALLS
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """在现有世界包上按指令修订：产出完整新版本，不就地改文件。"""
    system = (
        "你是世界设定层的世界包修订器。按用户指令修改给定世界包，只改与指令相关的部分，其余保持原样。"
        + STRUCTURE_HINT
    )
    user = (
        f"修订指令：\n{instruction}\n\n当前世界包：\n{json.dumps(package, ensure_ascii=False)}\n\n"
        f"形状参考（字段类型与粒度照此填写；内容按当前世界包替换）：\n{json.dumps(EXAMPLE, ensure_ascii=False)}"
    )
    budget = {"calls": 0, "limit": max(1, int(max_calls))}
    try:
        candidate, errors = await _generate_with_retry(
            llm, system, user, package, validate_package, label="世界包修订", budget=budget
        )
    except BudgetExhausted as exc:
        return {**package}, [str(exc)], _usage(budget, paused=True)
    return candidate, errors, _usage(budget, paused=False)


async def fill_section(
    llm: LLMClient, package: dict[str, Any], section: str, *, max_calls: int = DEFAULT_CARD_CALLS
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """表单式补全：只补指定段落（world / sources / canon / narratives / races / events / life / roles …）。"""
    if section not in package:
        raise PackageError(f"未知段落：{section}")
    system = (
        "你是世界设定层的内容补全器。只补全用户指定的段落，其余段落原样返回。" + STRUCTURE_HINT
    )
    user = (
        f"需要补全的段落：{section}\n"
        f"当前值（可能为空壳）：{json.dumps(package.get(section), ensure_ascii=False)}\n"
        f"完整世界包（其他段落作为上下文，请原样返回）：{json.dumps(package, ensure_ascii=False)}\n\n"
        f"形状参考（字段类型与粒度照此填写；内容按当前世界包替换）：\n"
        f"{json.dumps({section: EXAMPLE.get(section)}, ensure_ascii=False)}"
    )
    budget = {"calls": 0, "limit": max(1, int(max_calls))}
    try:
        candidate, errors = await _generate_with_retry(
            llm, system, user, package, validate_package, label=f"段落 {section}", budget=budget
        )
    except BudgetExhausted as exc:
        return {**package}, [str(exc)], _usage(budget, paused=True)
    return candidate, errors, _usage(budget, paused=False)


async def generate_card(
    llm: LLMClient, package: dict[str, Any], brief: str, *, max_calls: int = DEFAULT_CARD_CALLS
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """AI 生成角色卡候选：创作校验可用实情层，但候选仍需用户确认后才进入实例。"""
    from .cards import template_card
    from .example import example_card

    skeleton = template_card(package)
    moment = int(package.get("calendar", {}).get("initial_moment") or 0)
    calendar = package.get("calendar") or {}
    day = int(calendar.get("day_seconds") or 86400)
    months = calendar.get("months") or []
    year_days = sum(int(item.get("days") or 0) for item in months if isinstance(item, dict)) or len(months) * 30 or 360
    year_seconds = year_days * day
    system = (
        "你是角色卡生成器。按用户描述生成一张角色卡：身份、职业、背景与世界公理相容，"
        "信息渠道只能用世界包已定义的来源，初始知识必须有合法来源与获知时间（不晚于初始时刻，"
        "史料不得早于成书，且引用史料时必须用 scope 写明所掌握的条目，scope 只能取该传本 entries 里的标识），"
        "初始性格单元至少一个锚点（置信度 0.75–0.99）、其余按驱动区间取值，"
        "生活线模板的活动必须来自世界包对应模板。creator 段是幕后设定，self_knowledge 段是角色自己知道的。" + STRUCTURE_HINT
    )
    user = (
        f"角色描述：\n{brief}\n\n"
        f"目标世界包：\n{json.dumps(package, ensure_ascii=False)}\n\n"
        f"世界历法：日长 {day} 世界秒；一年 {year_days} 日 = {year_seconds} 世界秒；实例初始时刻 {moment} 世界秒。\n"
        f"身份用「种族 + 出生时刻（世界秒整数）」表达，年龄是推导值：若角色约 N 岁，"
        f"出生时刻 = {moment} − N×{year_seconds}；纪元开始之前为负数，负数是合法的，不要把它截成 0。"
        f"不要填年龄数字，也不要写文字描述（如「二十出头」）。\n\n"
        f"必须遵守的 JSON 骨架：\n{json.dumps(skeleton, ensure_ascii=False)}\n\n"
        f"形状参考（字段类型与粒度照此填写；这是示例角色，内容与标识都按目标世界包替换）：\n"
        f"{json.dumps(example_card(EXAMPLE), ensure_ascii=False)}"
    )

    def check(candidate: dict[str, Any]) -> list[str]:
        card = _merge_structure(candidate, skeleton)
        return validate_card(card, package, moment=moment)

    budget = {"calls": 0, "limit": max(1, int(max_calls))}
    try:
        candidate, errors = await _generate_with_retry(
            llm, system, user, skeleton, check, label="角色卡", budget=budget
        )
    except BudgetExhausted as exc:
        return {**skeleton}, [str(exc)], _usage(budget, paused=True)
    return candidate, errors, _usage(budget, paused=False)


async def _generate_with_retry(
    llm: LLMClient,
    system: str,
    user: str,
    skeleton: dict[str, Any],
    check: Any,
    *,
    label: str,
    budget: dict[str, int],
) -> tuple[dict[str, Any], list[str]]:
    """两次机会：失败把错误清单回灌；仍失败则原样交回候选与错误（不落盘）。调用前先记预算。"""
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    candidate: dict[str, Any] | None = None
    errors: list[str] = []
    for attempt in (0, 1):
        _spend(budget, label)
        log.info("generator call label=%s attempt=%s budget=%s", label, attempt, GENERATOR_BUDGET)
        try:
            text = await llm.chat(
                messages, max_tokens=GENERATOR_BUDGET, timeout=GENERATOR_TIMEOUT_S, temperature=GENERATOR_TEMPERATURE
            )
        except LLMError:
            raise
        try:
            candidate = _merge_structure(extract_json(text), skeleton)
        except PackageError as exc:
            errors = [str(exc)]
            candidate = None
        else:
            errors = check(candidate)
        if not errors and candidate is not None:
            return candidate, []
        if attempt == 0:
            complaint = "\n".join(f"- {item}" for item in errors) or "- 输出不是合法 JSON 对象"
            messages = messages[:2] + [
                {"role": "assistant", "content": json.dumps(candidate, ensure_ascii=False) if candidate else "(无法解析)"},
                {
                    "role": "user",
                    "content": f"上一次输出未通过{label}校验：\n{complaint}\n\n请修正后重新输出完整 JSON。",
                },
            ]
    return candidate if candidate is not None else {}, errors
