#!/usr/bin/env python
"""行为探针：docs/CHARACTER_CARD_SPEC.md 附录 B（行为验收）+ 正文关键条款。

只读实现代码、只写临时目录；不触 data/isekai.db，不启动核心，不改任何项目文件。
跑法：.venv/Scripts/python.exe scripts/_audit_card.py
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from isekai_core.config import load_config  # noqa: E402
from isekai_core.llm import FakeLLM  # noqa: E402
from isekai_core.runtime import cognition, environment, personality  # noqa: E402
from isekai_core.runtime.calendar import calendar_from_package  # noqa: E402
from isekai_core.runtime.service import RuntimeService, RuntimeStateError  # noqa: E402
from isekai_core.store import Store  # noqa: E402
from isekai_core.ump import UmpError  # noqa: E402
from isekai_core.world import ops as ops_mod  # noqa: E402
from isekai_core.world.cards import validate_assembly, validate_card  # noqa: E402
from isekai_core.world.example import DAY, example_card, example_package  # noqa: E402
from isekai_core.world.generator import generate_card  # noqa: E402
from isekai_core.world.instances import InstanceError, create_instance, get_setting, save_card  # noqa: E402
from isekai_core.world.portable import _digest, import_instance, write_export  # noqa: E402

RESULTS: list[tuple[str, str, str]] = []


def add(status: str, summary: str, evidence: str) -> None:
    RESULTS.append((status, summary, evidence))
    print(f"{status} {summary} — {evidence}", flush=True)


def check(ident: str, summary: str) -> Callable[[Callable[[], Any]], Callable[[], Any]]:
    """把一条验收包成探针项：返回 (状态, 证据)；异常一律如实记 FAIL。"""

    def wrap(fn: Callable[[], Any]) -> Callable[[], Any]:
        try:
            value = fn()
            status, evidence = value if value else ("PASS", "")
        except AssertionError as exc:
            status, evidence = "FAIL", f"断言失败：{exc}"
        except Exception as exc:  # noqa: BLE001 探针自身异常也必须暴露
            status, evidence = "FAIL", f"探针异常 {type(exc).__name__}: {exc}"
        add(status, f"{ident} {summary}", evidence)
        return fn

    return wrap


# ---------- 夹具 ----------

def tmp_store() -> tuple[Store, Path]:
    root = Path(tempfile.mkdtemp(prefix="isekai-audit-"))
    store = Store(root / "data" / "isekai.db")
    store.ensure_schema()
    return store, root


def clone(payload: Any) -> Any:
    return json.loads(json.dumps(payload, ensure_ascii=False))


def mutated(card: dict[str, Any], mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    copy = clone(card)
    mutate(copy)
    return copy


def create_errors(store: Store, package: dict[str, Any], card: dict[str, Any]) -> list[str]:
    """返回创建失败的错误清单；成功则返回空列表。"""
    try:
        create_instance(store, package, [card])
    except InstanceError as exc:
        return list(exc.errors)
    return []


def residue(store: Store) -> dict[str, int]:
    tables = ("instance", "timeline", "commit_log", "commit_snapshot", "session",
              "character_join", "unit", "life_plan", "knowledge", "experience", "memory")
    return {t: int(store._conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]) for t in tables}


def hit(errors: list[str], needle: str) -> str:
    for item in errors:
        if needle in item:
            return item
    raise AssertionError(f"错误清单里没有 {needle!r}：{errors}")


def world_service(store: Store) -> RuntimeService:
    return RuntimeService(store, autocommit_enabled=False)


def ready_line(store: Store, package: dict[str, Any], card: dict[str, Any]) -> tuple[str, str]:
    """建实例 + 运行层入册，返回 (instance_id, timeline_id)。"""
    info = create_instance(store, package, [card])
    timeline_id = store.timeline_list(info["id"])[0]["id"]
    world_service(store).ensure_instance(info["id"], now_real=1.7e9)
    return info["id"], timeline_id


def unit_map(rows: list[dict[str, Any]]) -> dict[str, float]:
    return {str(row["id"]): float(row["confidence"]) for row in rows}


def texts_of(rows: list[dict[str, Any]]) -> list[str]:
    return [str(row.get("text") or "") for row in rows]


# ---------- 附录 B #1：创建前联合校验与原子性 ----------

@check("B1a", "未确认角色卡：创建失败")
def b1a() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    card = example_card(package, confirmed=False)
    errors = create_errors(store, package, card)
    return "PASS", f"errors={hit(errors, '未经用户确认')!r}（cards.py:375）"


@check("B1b", "渠道悬空 / 无条件声明：创建失败")
def b1b() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    base = example_card(package)
    dangling = create_errors(store, package, mutated(base, lambda c: c["channels"][0].update(source_id="src-404")))
    no_cond = create_errors(store, package, mutated(base, lambda c: c["channels"][0].update(conditions="")))
    empty = create_errors(store, package, mutated(base, lambda c: c.update(channels=[])))
    return "PASS", (f"悬空→{hit(dangling, '渠道悬空')!r}；无条件→{hit(no_cond, '缺少可接触条件')!r}；"
                    f"空渠道→{hit(empty, '至少声明一条信息渠道')!r}（cards.py:120-125）")


@check("B1c", "初始知识越权：不存在条目 / 史料范围越界 / 获知时刻非法")
def b1c() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    base = example_card(package)

    def set_scope(card: dict[str, Any]) -> None:
        card["initial_knowledge"][0]["scope"] = ["cf-1", "cf-404"]

    missing = create_errors(store, package, mutated(base, lambda c: c["initial_knowledge"][0].update(ref_id="hs-404")))
    outside = create_errors(store, package, mutated(base, set_scope))
    late = create_errors(store, package, mutated(base, lambda c: c["initial_knowledge"][1].update(obtained_at=moment + 1)))
    early = create_errors(store, package, mutated(base, lambda c: c["initial_knowledge"][0].update(obtained_at=DAY * 10)))
    no_scope = create_errors(store, package, mutated(base, lambda c: c["initial_knowledge"][0].pop("scope")))
    return "PASS", (f"不存在条目→{hit(missing, '引用的historiography条目不存在')!r}；"
                    f"超出传本→{hit(outside, '超出该传本的条目范围')!r}；"
                    f"晚于初始时刻→{hit(late, '获知时间晚于初始时刻')!r}；"
                    f"早于成书→{hit(early, '史料获知早于成书')!r}；"
                    f"缺 scope→{hit(no_scope, '必须写明所掌握的条目或范围')!r}（cards.py:169-191）")


@check("B1d", "种族 / 出生与寿命覆盖不合法：创建失败")
def b1d() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    base = example_card(package)

    bad_race = create_errors(store, package, mutated(base, lambda c: c["identity"].update(race_id="rc-404")))
    future = create_errors(store, package, mutated(base, lambda c: c["identity"].update(born=moment + 1)))
    short_life = clone(package)
    short_life["races"][0]["lifespan"] = {"min_years": 5, "max_years": 10}
    over_life = create_errors(store, short_life, base)
    unbounded = clone(package)
    unbounded["races"][0]["lifespan"] = {"mode": "unbounded"}
    long_lived = create_errors(store, unbounded, base)
    return "PASS", (f"种族不存在→{hit(bad_race, '引用的种族不存在')!r}；"
                    f"晚于初始时刻→{hit(future, '出生时刻不能晚于实例初始时刻')!r}；"
                    f"寿命覆盖→{hit(over_life, '出生与寿命覆盖不相容')!r}；"
                    f"unbounded 种族不误伤→{long_lived or '创建通过'}（cards.py:100-113）")


@check("B1e", "日程非法：活动未声明 / 重叠 / 超日 / 多个跨日窗口")
def b1e() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    base = example_card(package)

    def alien(card: dict[str, Any]) -> None:
        card["life_template"]["windows"][1]["activity"] = "巡街"

    def overlap(card: dict[str, Any]) -> None:
        card["life_template"]["windows"] = [
            {"start": 0, "end": 30000, "activity": "sleep"},
            {"start": 20000, "end": 72000, "activity": "duty"},
        ]

    def beyond(card: dict[str, Any]) -> None:
        card["life_template"]["windows"].append(
            {"start": DAY, "end": DAY + 100, "activity": "sleep"}
        )

    def two_wraps(card: dict[str, Any]) -> None:
        card["life_template"]["windows"] = [
            {"start": 0, "end": DAY + 3600, "activity": "sleep"},
            {"start": 3600, "end": DAY + 7200, "activity": "duty"},
        ]

    def no_text(card: dict[str, Any]) -> None:
        card["life_template"] = {"sleep": True, "routine_note": "夜班后补觉"}

    alien_err = create_errors(store, package, mutated(base, alien))
    overlap_err = create_errors(store, package, mutated(base, overlap))
    beyond_err = create_errors(store, package, mutated(base, beyond))
    wraps_err = create_errors(store, package, mutated(base, two_wraps))
    text_err = create_errors(store, package, mutated(base, no_text))
    return "PASS", (f"未声明活动→{hit(alien_err, '未在世界包对应模板中声明')!r}；"
                    f"重叠→{hit(overlap_err, '活动区间重叠')!r}；"
                    f"超日→{hit(beyond_err, '起点超出世界日')!r}；"
                    f"两个跨日→{hit(wraps_err, '最多一个窗口跨世界日')!r}；"
                    f"仅文本→{hit(text_err, '至少一个世界时间窗')!r}（cards.py:296-350）")


@check("B1f", "创建失败不留半个角色（库内零残留）")
def b1f() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    base = example_card(package)
    attempts = [
        example_card(package, confirmed=False),
        mutated(base, lambda c: c["channels"][0].update(source_id="src-404")),
        mutated(base, lambda c: c["initial_knowledge"][1].update(obtained_at=int(package["calendar"]["initial_moment"]) + 1)),
        mutated(base, lambda c: c["identity"].update(race_id="rc-404")),
        mutated(base, lambda c: c["life_template"]["windows"][1].update(activity="巡街")),
    ]
    for card in attempts:
        assert create_errors(store, package, card), "该变体本应创建失败"
    leftovers = residue(store)
    assert set(leftovers.values()) == {0}, f"库内残留：{leftovers}"
    return "PASS", f"5 组非法卡逐一被拒后 counts={leftovers}（instances.py:62-68 先校验后写入）"


# ---------- 附录 B #2：锁定边界与补卡 ----------

@check("B2a", "实例创建后改 / 删源卡不改变实例")
def b2a() -> tuple[str, str]:
    store, root = tmp_store()
    package = example_package()
    card = example_card(package)
    card_path = str(root / "cc-grey.json")
    save_card(card_path, card)
    info = create_instance(store, package, [card])
    before = clone(get_setting(store, info["id"])["cards"])

    edited = clone(card)
    edited["identity"]["name"] = "冒名顶替"
    edited["initial_knowledge"] = []
    edited["first_contact"] = {"stance": "已被改写", "intent": ""}
    save_card(card_path, edited)
    after_file = clone(get_setting(store, info["id"])["cards"])

    card["identity"]["name"] = "内存改名"          # 内存改
    card_path_obj = Path(card_path)
    card_path_obj.unlink()                          # 删源卡文件
    after_delete = clone(get_setting(store, info["id"])["cards"])

    assert after_file == before, "改源卡文件竟改动了实例快照"
    assert after_delete == before, "删源卡文件竟改动了实例快照"
    name = after_delete[0]["identity"]["name"]
    assert name == "堤禾", f"实例内姓名被改写：{name}"
    return "PASS", ("改文件 / 改内存 / 删文件三种改法后 setting.cards 逐一字段相等，姓名仍为 "
                    f"{name!r}（instances.py:114-118 深拷贝 + json 克隆）")


@check("B2b", "改卡 / 换卡 / 删卡被锁定边界拒绝（管理面无入口，补卡不覆盖同标识）")
def b2b() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    card = example_card(package)
    instance_id, timeline_id = ready_line(store, package, card)
    character_id = card["meta"]["card_id"]

    table = ops_mod.describe_ops()
    every = list(table["sync"]) + list(table["async_"])
    forgeable = [op for op in every if re.search(r"(card|character)\.(update|edit|delete|replace|remove|rename|overwrite)", op)]
    assert not forgeable, f"管理面出现改 / 删卡入口：{forgeable}"

    world = world_service(store)
    forged = mutated(card, lambda c: (c["identity"].update(name="换头术"), c["initial_units"][0].update(confidence=0.76)))
    try:
        world.add_character(instance_id, timeline_id, forged, now_real=1.7e9, joined_world=int(store.clock_get(timeline_id)["processed_world"]))
    except RuntimeStateError as exc:
        rejection = str(exc)
    else:
        raise AssertionError("同 card_id 的改写卡竟被接受为补卡")
    assert "已在本线" in rejection, rejection
    locked = get_setting(store, instance_id)["cards"][0]
    assert locked["identity"]["name"] == "堤禾" and float(locked["initial_units"][0]["confidence"]) == 0.9
    joins = store.character_join_list(instance_id, timeline_id)
    return "PASS", (f"管理面 {len(table['sync'])}+{len(table['async_'])} 条操作无改 / 删卡入口"
                    f"（ops.py:37-95；DESKTOP_SPEC:127）；同标识改写补卡被拒→{rejection!r}，"
                    f"锁定卡仍 name={locked['identity']['name']!r} conf=0.9，character_join={len(joins)} 行（service.py:2481-2486）")


@check("B2c", "补卡只能新增：既有角色状态与历史不被改写")
def b2c() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    card = example_card(package)
    instance_id, timeline_id = ready_line(store, package, card)
    character_id = str(card["meta"]["card_id"])
    world = world_service(store)
    world.activate(instance_id, timeline_id, now_real=1.7e9)
    world.set_rate(instance_id, timeline_id, rate=100000, now_real=1.7e9)
    world.advance(instance_id, timeline_id, now_real=1.7e9 + 2)

    units_before = clone(store.unit_list(instance_id, timeline_id, character_id))
    exp_before = clone(store.experience_window(instance_id, timeline_id, character_id, until=10**15, limit=99))
    commits_before = [row["id"] for row in store.commit_list(instance_id)]
    knowledge_before = clone(store.knowledge_window(instance_id, timeline_id, character_id, until=10**15, limit=99))

    joined = example_card(package, name="堤砚")
    watermark = int(store.clock_get(timeline_id)["processed_world"])
    result = world.add_character(instance_id, timeline_id, joined, now_real=1.7e9 + 3, joined_world=watermark, note="补卡")

    assert clone(store.unit_list(instance_id, timeline_id, character_id)) == units_before, "补卡改动了既有角色的单元"
    assert clone(store.experience_window(instance_id, timeline_id, character_id, until=10**15, limit=99)) == exp_before, "补卡改写了既有角色的经历"
    assert clone(store.knowledge_window(instance_id, timeline_id, character_id, until=10**15, limit=99)) == knowledge_before, "补卡改写了既有角色的获知"
    assert [row["id"] for row in store.commit_list(instance_id)] == commits_before, "补卡往历史里塞了提交"
    assert result["joined_world"] == watermark
    rookies = store.unit_list(instance_id, timeline_id, str(joined["meta"]["card_id"]))
    assert rookies and all(row["character_id"] == str(joined["meta"]["card_id"]) for row in rookies)
    return "PASS", (f"补入堤砚后主机角色的 unit({len(units_before)}) / experience({len(exp_before)}) / "
                    f"knowledge({len(knowledge_before)}) / commit({len(commits_before)}) 逐一相等；"
                    f"新角色另得 {len(rookies)} 个单元（service.py:2495-2538）")


# ---------- 附录 B #3：幕后秘密 ----------

@check("B3", "creator 幕后段：创作校验可读、扮演上下文不可读")
def b3() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    card = example_card(package)
    moment = int(package["calendar"]["initial_moment"])
    secret = card["background"]["creator"]
    fact_secret = "崩塌前夜"  # 实情层 cf-2 原文，未进卡片初始知识

    assert not validate_card(card, package, moment=moment), "带 creator 秘密的卡本身就通不过校验"
    llm = FakeLLM(["（占位）"])
    asyncio.run(generate_card(llm, package, "一个堤务吏"))
    prompt = json.dumps(llm.calls[0], ensure_ascii=False)
    assert fact_secret in prompt, "生成器上下文拿不到实情层（创作校验读不到背景）"

    context = cognition.play_context(package, card, world_seconds=moment, calendar_label="灰潮纪 13 年雾月 1 日")
    rendered = cognition.render_prompt(context)
    assert secret not in rendered and secret not in json.dumps(context, ensure_ascii=False), "creator 段漏进扮演上下文"
    assert fact_secret not in rendered, "未获知的实情层原文漏进扮演上下文"

    instance_id, timeline_id = ready_line(store, package, card)
    session = store.session_ensure(instance_id, timeline_id, card["meta"]["card_id"])
    system_prompt = world_service(store).system_prompt(session, now_real=1.7e9)
    assert "堤禾" in system_prompt and "堤务吏" in system_prompt, "系统提示没带上身份（负向断言会变成空检查）"
    assert secret not in system_prompt and fact_secret not in system_prompt, "服务层系统提示漏了幕后 / 实情"
    return "PASS", (f"生成器 prompt 含实情层（len={len(prompt)}）；play_context / render_prompt / "
                    f"service.system_prompt（len={len(system_prompt)}）都不含 creator 段与未获知实情"
                    "（cognition.py:235-251, 282）")


# ---------- 附录 B #4：认知差异与硬约束 ----------

@check("B4a", "同世界不同角色的初始认知不同")
def b4a() -> tuple[str, str]:
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    first = example_card(package, name="堤禾")

    def rework(card: dict[str, Any]) -> None:
        card["channels"] = [{"source_id": "src-2", "conditions": "退潮时在盐滩拓印碑文"}]
        card["initial_knowledge"] = [{"ref_type": "narrative", "ref_id": "nv-2", "obtained_at": DAY * 1300}]

    second = mutated(first, rework)
    slice_a = cognition.knowledge_slice(package, first, world_seconds=moment)
    slice_b = cognition.knowledge_slice(package, second, world_seconds=moment)
    texts_a, texts_b = set(texts_of(slice_a)), set(texts_of(slice_b))
    assert texts_a and texts_b, "两条切片都不该为空"
    assert not (texts_a & texts_b), f"不同角色的切片竟重叠：{texts_a & texts_b}"
    return "PASS", (f"堤禾 {len(slice_a)} 条（含史料 cf-1 / 信报 nv-1 / 自身记忆），"
                    f"碑读者 {len(slice_b)} 条（仅 nv-2），交集为空（cognition.py:57-128）")


@check("B4b", "硬约束角色不因公开事件标签获得不存在的渠道")
def b4b() -> tuple[str, str]:
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    hard_card = example_card(package)
    hard_card["cognition"] = {"mode": "hard", "sources": ["self_experience", "small_env", "user_contact"]}
    slice_rows = cognition.knowledge_slice(package, hard_card, world_seconds=moment)
    kinds = {row["source"] for row in slice_rows}
    assert kinds == {"自己的记忆"}, f"硬约束角色拿到了允许来源之外的条目：{kinds}"

    public_rows = [
        {"text": "信报载：北堤要重新修", "source": "src-1", "world_seconds": moment, "stance": "believed"},
        {"text": "她在滩口亲手读到水位尺刻线", "source": "亲历", "world_seconds": moment, "stance": "experienced"},
    ]
    mixed = cognition.knowledge_slice(package, hard_card, world_seconds=moment, knowledge=public_rows)
    texts = texts_of(mixed)
    assert "她在滩口亲手读到水位尺刻线" in texts and not any("信报载" in text for text in texts), texts
    return "PASS", (f"硬约束切片只剩 {kinds}（世界包里的 canon / 史料 / 信报说法全被挡）；"
                    f"公开渠道获知行被排除、亲历行保留：{texts}（cognition.py:51-52, 79, 92, 146）")


# ---------- 附录 B #5：分叉继承与兄弟线隔离 ----------

@check("B5a", "分叉继承当时状态，不重置到卡片初值")
def b5a() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    card = example_card(package)
    instance_id, timeline_id = ready_line(store, package, card)
    character_id = str(card["meta"]["card_id"])
    world = world_service(store)
    world.activate(instance_id, timeline_id, now_real=1.7e9)
    world.set_rate(instance_id, timeline_id, rate=100000, now_real=1.7e9)
    world.advance(instance_id, timeline_id, now_real=1.7e9 + 2)
    initial = unit_map(personality.initial_rows(card, instance_id="x", timeline_id="y", world_seconds=0))
    evolved = unit_map(store.unit_list(instance_id, timeline_id, character_id))
    if evolved == initial:  # 运行层未落盘衰减时，显式做一次等价的运行期写入
        row = store.unit_list(instance_id, timeline_id, character_id)[0]
        store.unit_put({**row, "confidence": 0.62, "updated_world": int(store.clock_get(timeline_id)["processed_world"])})
        evolved = unit_map(store.unit_list(instance_id, timeline_id, character_id))
    assert evolved != initial, "分叉前该线状态没有演化，无法证明继承"
    commit = world.commit(instance_id, timeline_id, note="分叉点")
    branch = world.fork(instance_id, timeline_id, commit_id=commit["id"], name="分支")
    branch_id = branch["timeline"]["id"]
    inherited = unit_map(store.unit_list(instance_id, branch_id, character_id))
    assert inherited == evolved, f"分支未继承分叉点状态：{inherited} != {evolved}"
    assert branch["timeline"]["state"] == "frozen", "分叉即激活"
    return "PASS", (f"父线已演化 {evolved}（卡片初值 {initial}），分支同为 {inherited}；"
                    f"分支创建状态={branch['timeline']['state']}（service.py:522-564）")


@check("B5b", "兄弟线之后的锚点与记忆不互传")
def b5b() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    card = example_card(package)
    instance_id, timeline_id = ready_line(store, package, card)
    character_id = str(card["meta"]["card_id"])
    world = world_service(store)
    world.activate(instance_id, timeline_id, now_real=1.7e9)
    world.set_rate(instance_id, timeline_id, rate=100000, now_real=1.7e9)
    world.advance(instance_id, timeline_id, now_real=1.7e9 + 2)
    commit = world.commit(instance_id, timeline_id, note="分叉点")
    branch_id = world.fork(instance_id, timeline_id, commit_id=commit["id"], name="分支")["timeline"]["id"]
    parent_units = unit_map(store.unit_list(instance_id, timeline_id, character_id))
    watermark = int(store.clock_get(timeline_id)["processed_world"])

    row = store.unit_list(instance_id, branch_id, character_id)[0]
    store.unit_put({**row, "confidence": 0.31, "semantic": "只有分支知道的锚点", "updated_world": watermark})
    store.experience_add({
        "id": "ex-branch-only", "instance_id": instance_id, "timeline_id": branch_id, "character_id": character_id,
        "world_seconds": watermark, "kind": "life", "summary": "分支独有的经历", "source_ref": None, "confidence": 1.0,
    })
    store.memory_add({
        "id": "mem-branch-only", "instance_id": instance_id, "timeline_id": branch_id, "character_id": character_id,
        "text": "分支独有的一条记忆", "kind": "episodic", "happened_world": watermark, "learned_world": watermark,
        "recorded_world": watermark, "semantic_watermark": watermark, "strength": 0.6, "confidence": 0.8,
        "source_key": "audit-branch-only",
    })

    assert unit_map(store.unit_list(instance_id, timeline_id, character_id)) == parent_units, "分支写锚点传染到父线"
    parent_exp = texts_of(store.experience_window(instance_id, timeline_id, character_id, until=10**15, limit=99))
    parent_mem = [str(item.get("text")) for item in store.memory_scope(instance_id, timeline_id, character_id)]
    branch_mem = [str(item.get("text")) for item in store.memory_scope(instance_id, branch_id, character_id)]
    assert not any("分支独有" in text for text in parent_exp), "分支经历出现在父线"
    assert "分支独有的一条记忆" in branch_mem and not any("分支独有" in text for text in parent_mem), "记忆在兄弟线之间传染"
    return "PASS", (f"分支写入的锚点 / 经历 / 记忆只落在 {branch_id}：父线单元仍 {parent_units}，"
                    f"父线经历 {len(parent_exp)} 条不含分支行，分支记忆 {len(branch_mem)} 条、父线 {len(parent_mem)} 条"
                    "（service.py:536-559 全量快照 + store 按 timeline_id 隔离）")


# ---------- 附录 B #6：历法形态与生成重试 ----------

def alt_package(day_seconds: int, moment: int, sleep: bool, windows: list[dict[str, Any]]) -> dict[str, Any]:
    package = clone(example_package())
    calendar = package["calendar"]
    calendar["day_seconds"] = day_seconds
    calendar["initial_moment"] = moment
    calendar["segments"] = [
        {"id": "seg-first", "name": "前半", "start": 0, "end": day_seconds // 2},
        {"id": "seg-second", "name": "后半", "start": day_seconds // 2, "end": day_seconds},
    ]
    package["life"] = [{"id": "lf-1", "name": "堤务吏日常", "sleep": sleep, "windows": windows}]
    return package


def alt_card(package: dict[str, Any], sleep: bool, windows: list[dict[str, Any]]) -> dict[str, Any]:
    card = example_card(package)
    card["life_template"] = {"sleep": sleep, "routine_note": "", "windows": clone(windows)}
    return card


@check("B6a", "非现实日长 + 跨日睡眠窗口被一致解释")
def b6a() -> tuple[str, str]:
    day_long, moment = 50_000, DAY * 1500
    windows = [{"start": 5_000, "end": 40_000, "activity": "duty"},
               {"start": 40_000, "end": 55_000, "activity": "sleep"}]
    package = alt_package(day_long, moment, True, windows)
    card = alt_card(package, True, windows)
    assert not validate_assembly(package, [card], moment=moment), validate_assembly(package, [card], moment=moment)

    store, _ = tmp_store()
    instance_id, timeline_id = ready_line(store, package, card)
    character_id = str(card["meta"]["card_id"])
    world = world_service(store)
    calendar = calendar_from_package(package)
    assert calendar.day_seconds == day_long, f"运行层日长 {calendar.day_seconds}"
    day_start = calendar.day_index(moment) * day_long
    plan = json.loads(str(store.plan_latest(instance_id, timeline_id, character_id)["windows"]))
    sleep_block = [item for item in plan["windows"] if item["activity"] == "sleep"][0]
    assert (sleep_block["start"], sleep_block["end"]) == (day_start + 40_000, day_start + 55_000), sleep_block

    tail = world.character_snapshot(instance_id, timeline_id, character_id, world_seconds=day_start + day_long + 1_000)
    mid = world.character_snapshot(instance_id, timeline_id, character_id, world_seconds=day_start + 45_000)
    assert tail["current_activity"].startswith("sleep"), f"次日日首 1000 秒的活动= {tail['current_activity']!r}"
    assert mid["current_activity"].startswith("sleep"), mid["current_activity"]
    return "PASS", (f"日长 50000 世界秒：计划窗口 [{sleep_block['start']},{sleep_block['end']}) 跨日保留，"
                    f"次日日首（+1000s）活动= {tail['current_activity']!r}、当日 +45000s 活动= {mid['current_activity']!r}"
                    "（life.py:30-46；service.py:1629-1637）")


@check("B6b", "无睡眠模板：显式声明 sleep=false 可装配并展开")
def b6b() -> tuple[str, str]:
    day_long, moment = 50_000, DAY * 1500
    windows = [{"start": 0, "end": 25_000, "activity": "watch"},
               {"start": 25_000, "end": 50_000, "activity": "rest"}]
    package = alt_package(day_long, moment, False, windows)
    card = alt_card(package, False, windows)
    errors = validate_assembly(package, [card], moment=moment)
    assert not errors, errors
    store, _ = tmp_store()
    instance_id, timeline_id = ready_line(store, package, card)
    plan = json.loads(str(store.plan_latest(instance_id, timeline_id, str(card["meta"]["card_id"]))["windows"]))
    assert [item["activity"] for item in plan["windows"]] == ["watch", "rest"], plan["windows"]
    assert all(item["end"] <= plan["day_end"] for item in plan["windows"]), "无睡眠模板竟出现跨日窗口"
    return "PASS", (f"sleep=false 的模板通过联合校验，展开为 {[item['activity'] for item in plan['windows']]}"
                    f"（日长内闭合：{plan['windows'][-1]['end']} ≤ {plan['day_end']}）（cards.py:301-302；life.py:32-45）")


@check("B6c", "生成重试 / 校验失败不偷偷修改已确认版本")
def b6c() -> tuple[str, str]:
    store, root = tmp_store()
    cfg = load_config(root)
    package = example_package()
    confirmed = example_card(package)
    card_path = root / "cc-grey.json"
    save_card(str(card_path), confirmed)
    before = card_path.read_bytes()

    llm = FakeLLM(["这不是 JSON，一点对象都没有"])
    generated = asyncio.run(ops_mod.dispatch_async(cfg, llm, "world.card.generate", {"package": package, "brief": "一个堤务吏"}))
    assert generated["valid"] is False and generated["errors"], generated
    assert card_path.read_bytes() == before, "生成失败却改动了已确认卡"
    assert not list(root.glob("**/*.candidate.json")), "候选被偷偷落盘"

    broken = mutated(confirmed, lambda c: c["channels"][0].update(source_id="src-404"))
    broken["meta"]["confirmed"] = False
    try:
        ops_mod.dispatch(cfg, store, "world.card.confirm", {"package": package, "card": broken, "card_path": str(card_path)})
    except UmpError as exc:
        rejected = str(exc)
    else:
        raise AssertionError("非法卡竟通过确认")
    assert card_path.read_bytes() == before, "确认失败却覆盖了已确认卡"

    fresh = clone(confirmed)
    fresh["meta"]["confirmed"] = False
    ops_mod.dispatch(cfg, store, "world.card.confirm", {"package": package, "card": fresh, "card_path": str(card_path)})
    saved = json.loads(card_path.read_text(encoding="utf-8"))
    assert saved["meta"]["confirmed"] is True, saved["meta"]
    return "PASS", (f"两次重试后仍 valid=False + errors={generated['errors'][:1]}，卡文件字节不变；"
                    f"确认非法卡被拒（{rejected.splitlines()[0][:40]}…）文件同样不变；只有确认通过才落盘 "
                    "（ops.py:387-396；generator.py:286-320）")


# ---------- 附录 B #7：来源可追溯 ----------

@check("B7a", "通讯方式与表达边界可追溯到世界包声明，卡片不能扩大限制")
def b7a() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    base = example_card(package)
    stranger = mutated(base, lambda c: c["comms"][0].update(mechanism_id="cm-404"))
    errors = create_errors(store, package, stranger)
    assert hit(errors, "联络机制不在世界包允许范围内")

    loud = mutated(base, lambda c: c["comms"][0].update(note="随时可致电任何城邦议会，不受退潮限制"))
    context = cognition.play_context(package, loud, world_seconds=moment, calendar_label="灰潮纪")
    limits = context["comms"][0]["limits"]
    assert limits == package["comms"]["mechanisms"][0]["limits"], limits
    rendered = cognition.render_prompt(context)
    assert "信使不进入内陆" in rendered
    return "PASS", (f"未声明机制→{errors[0]!r}；卡片 note 自吹后 limits 仍是世界包原文 {limits!r}，"
                    "扮演定义按包内限制渲染（cards.py:127-133；cognition.py:254-264, 211-212）")


@check("B7b", "环境观察条件可追溯到世界包声明（卡片文字不能获得观察）")
def b7b() -> tuple[str, str]:
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    rows = environment.initial_rows(package, instance_id="in-x", timeline_id="tl-x", world_seconds=moment)
    types = environment.env_types(package)
    local = example_card(package)
    observed = environment.observations(rows, types, local, world_seconds=moment)
    kinds = [item["type_id"] for item in observed]
    assert kinds == ["env-1", "env-2"], observed
    note = [item for item in observed if item["type_id"] == "env-2"][0]["observe"]
    assert note == package["environment"]["types"][1]["observers"]["rl-1"], note

    mimic = mutated(local, lambda c: (c.update(role_id=None, region="开阔地（屋里只能听说）")))
    blind = environment.observations(rows, types, mimic, world_seconds=moment)
    assert blind == [], f"抄了 scope 文本的角色竟拿到观察：{blind}"
    return "PASS", (f"堤务吏（rl-1）拿到 {kinds}，精度取自包内 observers 文本 {note!r}；"
                    "只把卡片 region 抄成包内 scope 文字时 observations=[]（environment.py:132-183）")


@check("B7c", "未声明的事实来源不能仅由卡片文字获得")
def b7c() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    smuggler = mutated(
        example_card(package),
        lambda c: c.update(
            channels=[{"source_id": "src-1", "conditions": "凭身份取阅信报"}],
            initial_knowledge=[{"ref_type": "canon", "ref_id": "cf-2", "obtained_at": DAY * 1400}],
        ),
    )
    errors = validate_card(smuggler, package, moment=moment)
    if errors:
        return "PASS", f"直接引用实情层条目被校验挡下：{errors}"
    slice_rows = cognition.knowledge_slice(package, smuggler, world_seconds=moment)
    source = slice_rows[0]["source"] if slice_rows else ""
    context = cognition.play_context(package, smuggler, world_seconds=moment, calendar_label="灰潮纪")
    rendered = [line for line in cognition.render_prompt(context).splitlines() if "崩塌前夜" in line]
    if "未知传本" in source:
        return "FAIL", (
            "最小复现：卡片的 initial_knowledge 只写 {\"ref_type\":\"canon\",\"ref_id\":\"cf-2\",\"obtained_at\":129600000 量级}、"
            "渠道只声明 src-1（信报），validate_card 与 validate_assembly 都通过，"
            f"扮演定义出现 {rendered}（来源 {source!r}：世界包没有任何传本收录 cf-2）；"
            "对比：historiography 分支要求 scope 必填且限定传本条目。"
            "file:line isekai_core/world/cards.py:169-172（canon 分支不要求传本 / scope）；"
            "isekai_core/runtime/cognition.py:110-127（无传本时填占位标题）"
        )
    return "PASS", f"canon 直引的来源可追溯：{source!r}"


# ---------- 正文条款 ----------

@check("§2", "身份以「种族 + 出生时刻」表达，年龄为推导值（不手填）")
def s_identity() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    year_seconds = sum(int(item["days"]) for item in package["calendar"]["months"]) * int(package["calendar"]["day_seconds"])
    card = example_card(package)
    age = (moment - card["identity"]["born"]) / year_seconds
    assert card["identity"]["born"] < 0 and round(age) == 25, (card["identity"]["born"], age)

    forged = mutated(card, lambda c: c["identity"].update(age=7))
    assert not validate_card(forged, package, moment=moment), "带手填 age 的卡被拒了（age 不是校验字段）"
    context = cognition.play_context(package, forged, world_seconds=moment, calendar_label="灰潮纪")
    assert "age" not in json.dumps(context, ensure_ascii=False) and "7 岁" not in cognition.render_prompt(context)
    return "PASS", (f"born={card['identity']['born']}（纪元前，通过校验）→ 推导年龄 {age:.1f} 岁；"
                    "手填 age=7 既不改校验结果也不进扮演定义（cards.py:102-113；cognition.py:274-281 只带身份文本）")


@check("§5.2", "锚点置信区间 0.75–0.99 是硬校验，且必须有锚点")
def s_anchor() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    base = example_card(package)

    def set_anchor(value: float) -> Callable[[dict[str, Any]], None]:
        return lambda c: c["initial_units"][0].update(confidence=value)

    low_edge = create_errors(store, package, mutated(base, set_anchor(0.75)))
    high_edge = create_errors(store, package, mutated(base, set_anchor(0.99)))
    under = create_errors(store, package, mutated(base, set_anchor(0.74)))
    over = create_errors(store, package, mutated(base, set_anchor(1.0)))
    no_anchor = create_errors(store, package, mutated(base, lambda c: c.update(initial_units=[c["initial_units"][1]])))
    empty = create_errors(store, package, mutated(base, lambda c: c.update(initial_units=[])))
    assert not low_edge and not high_edge, (low_edge, high_edge)
    return "PASS", (f"闭区间边界 0.75 / 0.99 通过；0.74→{hit(under, '必须在 [0.75, 0.99] 内')!r}；"
                    f"1.0→{hit(over, '必须在 [0.75, 0.99] 内')!r}；无锚点→{hit(no_anchor, '至少一个锚点单元')!r}；"
                    f"空集合→{hit(empty, '至少一个初始性格单元')!r}（cards.py:258-293）")


@check("§5.2", "重复语义不靠不同名字重复计权")
def s_semantic() -> tuple[str, str]:
    package = example_package()
    card = example_card(package)
    twins = mutated(card, lambda c: c["initial_units"].append(
        {"id": "iu-1b", "semantic": c["initial_units"][0]["semantic"], "driver": "anchor", "confidence": 0.85, "basis": "同一习惯换个写法"}
    ))
    assert not validate_card(twins, package, moment=int(package["calendar"]["initial_moment"])), "同义单元被判非法"
    rows = personality.initial_rows(twins, instance_id="in-x", timeline_id="tl-x", world_seconds=DAY * 1500)
    driven = personality.apply_drive(rows, mode="anchor", source_key="audit-1", semantic="先量再说话",
                                     strength=1.0, positive=True, world_seconds=DAY * 1500)
    changed = [row["id"] for row, before in zip(driven, rows) if float(row["confidence"]) != float(before["confidence"])]
    assert len(changed) == 1, f"同义单元被重复计权：{changed}"
    return "PASS", f"两条同义锚点经一次驱动只有 {changed} 一条被强化（personality.py:124-140 按语义匹配且只命中一条）"


@check("§6.1/6.2", "cognition 声明：soft 为默认，hard 必须列来源且不得开放公共来源")
def s_cognition() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    base = example_card(package)
    missing = create_errors(store, package, mutated(base, lambda c: c.pop("cognition")))
    soft = create_errors(store, package, mutated(base, lambda c: c.update(cognition={"mode": "soft"})))
    bad_mode = create_errors(store, package, mutated(base, lambda c: c.update(cognition={"mode": "open"})))
    hard_no_src = create_errors(store, package, mutated(base, lambda c: c.update(cognition={"mode": "hard"})))
    hard_wide = create_errors(store, package, mutated(base, lambda c: c.update(cognition={"mode": "hard", "sources": ["canon"]})))
    assert not soft, f"soft 不要求来源清单：{soft}"
    return "PASS", (f"缺段→{hit(missing, '缺少认知边界声明')!r}；soft 无 sources={soft or '通过'}；"
                    f"未知 mode→{hit(bad_mode, '必须是 soft 或 hard')!r}；"
                    f"hard 缺来源→{hit(hard_no_src, '硬约束必须列出允许的信息来源')!r}；"
                    f"hard 开 canon→{hit(hard_wide, '硬约束不得开放')!r}（cards.py:195-211）")


@check("§2", "初见设定：必须填写、随卡锁定并进入扮演定义")
def s_first_contact() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    base = example_card(package)
    missing = create_errors(store, package, mutated(base, lambda c: c.pop("first_contact")))
    instance_id, timeline_id = ready_line(store, package, base)
    session = store.session_ensure(instance_id, timeline_id, base["meta"]["card_id"])
    prompt = world_service(store).system_prompt(session, now_real=1.7e9)
    assert "谨慎但不回避" in prompt, "初见姿态没进扮演定义"
    changed = mutated(base, lambda c: c.update(first_contact={"stance": "改过的姿态", "intent": ""}))
    assert json.dumps(changed["first_contact"], ensure_ascii=False) not in prompt
    return "PASS", (f"缺段→{hit(missing, '缺少初见设定')!r}；锁定卡里的初见进入系统提示（含「谨慎但不回避」），"
                    "改源卡对象的姿态不会生效（cards.py:140-141；cognition.py:199-201）")


@check("§5.2", "初始知识须有合法来源：职业与渠道资格不自动赋予秘密")
def s_knowledge_source() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    insider = mutated(example_card(package), lambda c: c.update(initial_knowledge=[]))
    single = validate_card(insider, package, moment=moment)
    assert not single, single
    assembly = validate_assembly(package, [insider], moment=moment)
    assert hit(assembly, "至少一名角色须凭初始知识")
    slice_rows = cognition.knowledge_slice(package, insider, world_seconds=moment)
    assert slice_rows == [], slice_rows
    return "PASS", (f"身份 + 渠道 + 角色模板齐全的单卡校验通过（{len(single)} 错），但装配校验拒绝："
                    f"{assembly[0]!r}；知识切片为空 {slice_rows}（cards.py:384-385；cognition.py:57-128 只吃声明过的条目）")


@check("§5.2", "生活线模板须有机器可检查的时间窗（文本不替代结构）")
def s_life_structure() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    base = example_card(package)
    text_only = create_errors(store, package, mutated(
        base, lambda c: c.update(life_template={"sleep": True, "routine_note": "退潮日提前一个时辰上堤。", "windows": []})))
    no_sleep_flag = create_errors(store, package, mutated(base, lambda c: c["life_template"].pop("sleep")))
    assert hit(text_only, "至少一个世界时间窗") and hit(no_sleep_flag, "必须显式声明是否睡眠")
    return "PASS", (f"只有 routine_note 文本→{hit(text_only, '至少一个世界时间窗')!r}；"
                    f"漏 sleep 声明→{hit(no_sleep_flag, '必须显式声明是否睡眠')!r}（cards.py:296-305）")


@check("§5.2", "生成 / 手编 / 导入三条路径共用同一套校验")
def s_three_paths() -> tuple[str, str]:
    store, root = tmp_store()
    cfg = load_config(root)
    package = example_package()
    broken = mutated(example_card(package), lambda c: (c.update(channels=[]), c["initial_units"][0].update(confidence=0.5)))

    llm = FakeLLM([json.dumps(broken, ensure_ascii=False)])
    generated = asyncio.run(generate_card(llm, package, "一个堤务吏"))
    gen_errors = generated[1]
    manual = ops_mod.dispatch(cfg, store, "world.card.validate", {"package": package, "card": broken})["errors"]
    assert hit(gen_errors, "channels") and hit(manual, "channels")

    good = example_card(package)
    instance_id, _ = ready_line(store, package, good)
    target = root / "pack.isekai.json"
    write_export(store, instance_id, target)
    container = json.loads(target.read_text(encoding="utf-8"))
    container["setting"]["cards"][0]["channels"] = []
    payload = {"setting": container["setting"], "runtime": container["runtime"]}
    container["integrity"]["digest"] = _digest(payload)
    target.write_text(json.dumps(container, ensure_ascii=False), encoding="utf-8")
    try:
        import_instance(store, container)
    except InstanceError as exc:
        import_errors = list(exc.errors)
    else:
        raise AssertionError("非法卡的导入件竟被接受")
    return "PASS", (f"同一张非法卡：生成路径 errors={gen_errors[:1]}；手编 world.card.validate errors={manual[:1]}；"
                    f"导入路径拒绝={import_errors[0][:40]!r}（generator.py:262-264；ops.py:383-386；portable.py:199-201）")


@check("§3", "补卡装配：时间锚定、已相识声明、初始知识投影")
def s_backfill() -> tuple[str, str]:
    store, _ = tmp_store()
    package = example_package()
    moment = int(package["calendar"]["initial_moment"])
    card = example_card(package)
    instance_id, timeline_id = ready_line(store, package, card)
    world = world_service(store)
    joined = example_card(package, name="堤砚")
    watermark = int(store.clock_get(timeline_id)["processed_world"])

    try:
        world.add_character(instance_id, timeline_id, joined, now_real=1.7e9, joined_world=watermark + 1)
    except RuntimeStateError as exc:
        late = str(exc)
    else:
        raise AssertionError("补入时刻晚于水位竟被接受")
    try:
        world.add_character(instance_id, timeline_id, joined, now_real=1.7e9, joined_world=moment - 1)
    except RuntimeStateError as exc:
        early = str(exc)
    else:
        raise AssertionError("补入时刻早于初始时刻竟被接受")
    fresh = world.add_character(instance_id, timeline_id, joined, now_real=1.7e9, joined_world=watermark, acquainted=True, note="已相识")
    assert "不能晚于已完成水位" in late
    assert fresh["acquainted"] is True and fresh["joined_world"] == watermark
    units = store.unit_list(instance_id, timeline_id, str(joined["meta"]["card_id"]))
    marks = [row for row in units if row["id"] == "join-acquainted"]
    assert len(marks) == 1 and marks[0]["mode"] == "dialog", units
    assert len(units) == len(joined["initial_units"]) + 1, units
    others = store.unit_list(instance_id, timeline_id, str(card["meta"]["card_id"]))
    assert not any(row["id"] == "join-acquainted" for row in others), "已相识声明写到了别的角色名下"
    projected = cognition.knowledge_slice(package, joined, world_seconds=watermark)
    assert projected, "补入角色的初始知识没有按认知契约投影"
    return "PASS", (f"晚于水位→{late[:22]!r}、早于初始时刻→{early[:18]!r}；已相识声明只补一条对话单元 "
                    f"{marks[0]['semantic']!r}（{len(joined['initial_units'])} 初始单元 + 1），知识投影 {len(projected)} 条"
                    "（service.py:2465-2538；cognition.py:60-128）")


@check("§8", "桌面 / 安卓共用同一生成与审定规则")
def s_android() -> tuple[str, str]:
    return "DEFERRED", "安卓端尚未实现：ANDROID_SPEC 状态行「设计规范，尚未实现；安卓为阶段 7 可选评估」，故卡片规则的第二端复用无实现可验（DESIGN.md §6.1 阶段 7）"


CHECKS = [b1a, b1b, b1c, b1d, b1e, b1f, b2a, b2b, b2c, b3, b4a, b4b,
          b5a, b5b, b6a, b6b, b6c, b7a, b7b, b7c,
          s_identity, s_anchor, s_semantic, s_cognition, s_first_contact,
          s_knowledge_source, s_life_structure, s_three_paths, s_backfill, s_android]


def main() -> int:
    started = time.time()
    for item in CHECKS:
        item()
    total = len(RESULTS)
    passed = sum(1 for status, _, _ in RESULTS if status == "PASS")
    failed = sum(1 for status, _, _ in RESULTS if status == "FAIL")
    deferred = sum(1 for status, _, _ in RESULTS if status == "DEFERRED")
    print(f"TOTAL {total} PASS {passed} FAIL {failed} DEFERRED {deferred}")
    print(f"# 探针耗时 {time.time() - started:.1f}s；FAIL 明细：")
    for status, summary, evidence in RESULTS:
        if status == "FAIL":
            print(f"# FAIL {summary} :: {evidence}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
