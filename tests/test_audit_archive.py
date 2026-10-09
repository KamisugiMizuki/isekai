"""参考审计存档的 pytest 化：把 `.hermes/*_audit.py` 的 78 项断言钉进测试套。

背景：A 类（`self_audit.py` 30 项）+ B 类（`final_audit.py` 30 项）+ 性能线
（`perf_audit.py` 18 项）= 78 项，此前只活在**未入库**的 `.hermes/` 里。脚本已原样归档到
`tools/archive_reference_audits/`（含 `MANIFEST.sha256`），显式运行器是
`tools/run_audits.py`。

本文件做三件事，**刻意不做第四件**：

1. **锁归档完整性**：7 个归档件必须与 `MANIFEST.sha256` 逐字节一致（「原样归档」不是口号）。
2. **把 78 项整体搬进 pytest**：三套审计只查 `sqlite_master` + 源码/规格文本，不读业务数据行，
   所以它们能在**夹具新建的实例**上稳定复现。本文件直接调用 `tools/run_audits.py`
   对临时实例跑一遍，断言 30/30 + 30/30 + 18/18 且退出码 0——这就是「78 项已进测试套」的证据。
   同时用**被人为改旧的实例**（补建 `ix_effect_target` / `ix_effect_retire`）断言 75/78 + 退出码 1，
   反向锁住运行器的退出码逻辑。
3. **补齐尚未被任何测试覆盖的条目**：78 项里有相当一部分已被既有专项测试**行为级**覆盖
   （见下表）；真正只在 `.hermes/` 里有断言、pytest 里没有的，才在本文件里逐条迁移。

**刻意不做**：不重复既有专项测试已经覆盖的断言。理由：重复不会增加守卫强度，只会让
「下一个人改行为时要改两处」——而 `.hermes/` 里的原始断言大多是**文本 grep**，
比既有专项测试（真调 `validate_package` / 真跑推进）弱。下表逐项标明去向。

### 78 项去向表

| 审计项 | 去向 |
| --- | --- |
| A-1 记忆衰减有界 + 惰性结算 | 行为级已覆盖：`test_memory.py::test_decay_sweep_is_bounded_and_lazy_reads_are_equivalent`、`test_advance_atomicity.py` |
| A-5 说法只取本页事件 / 已知事件按 id 批量 / backfill 判空 / ops 单条走主键 / intent 阶段下推 | **本文件迁移**（5 条，收窄到所属函数段） |
| A-7 注册表含 `effect_superseded`、回滚清单精确解析 | 行为级已覆盖：`test_state_domains.py`；`test_delivery_audit.py` 亦锁三张新表 |
| A-8 `stable_key` / 不再用 `secrets` / 不再用内置 `hash` | `test_determinism_contract.py` 锁编码与派生 id；**本文件补**「不可退回」的负向断言 |
| A-10 追赶不再拒绝 + 显式水位 + 冻结仍是 `not_ready` | 状态机行为已覆盖：`test_runtime_audit.py`、`test_runtime.py`；**本文件补** `lag_world_seconds` 与冻结文案 |
| A-11 默认 `rate_max` 下调 | **本文件迁移**（改为读 `load_config()` 的真实默认值） |
| A-4 自动提交最小间隔闸 | **本文件迁移**（改为读 `load_config()` 的真实默认值 + 接线） |
| B-7 取代式退休默认停用 | 行为级已覆盖：`test_effect_retire.py`（含两种开关形态 + 索引存在性） |
| 页缓存 / mmap / temp_store | 行为级已覆盖：`test_delivery_audit.py::test_page_cache_pragmas_are_set` |
| 方案② 窄取数 `effect_constraints` / 方案① target 下推 | **本文件迁移** |
| `effect_superseded` 表 + `ix_effect_expiry` / `ix_effect_active` / `ix_effect_target` 已删 / B-7 索引默认不建 | 已覆盖：`test_delivery_audit.py`、`test_effect_retire.py` |
| `ix_reaction_timeline_stage` 仍在 | **本文件迁移**（既有测试未锁这条索引） |
| B-5 规格条款 / B-8 规格条款 / A-10 规格同步 | **本文件迁移**（规格文本是产品契约的一部分） |
| B-6 说法差异化（含校验互异） | 行为级已覆盖：`test_claim_variants.py`（9 条） |
| B-5 身体后果（档位闭集 / 优先级最高 / 改写计划 / 死亡归档同批） | 行为级已覆盖：`test_casualty.py`（11 条） |
| B-1 压力量（纯函数 / 权重写死 / 抽取接入调制） | 行为级已覆盖：`test_pressures.py`（9 条） |
| B-3 + B-9 账本（表 / 写入方 / 只补不覆盖） | 行为级已覆盖：`test_ledger.py` |
| B-9 「点名规则结构断言就位」= `test_ledger.py` 存在 | **本文件迁移并加强**（存在**且**真的定义了测试函数；原断言是同义反复） |
| B-2 关系事实层（表 / 必须有依据 / 写入方） | 行为级已覆盖：`test_relations.py`（9 条） |
| B-4 拓扑（`space.py` / 邻接闭集 / 坐标拒绝） | 行为级已覆盖：`test_space_topology.py`；`MEMORY_SPEC` 措辞与规格条款**本文件迁移** |
| A-7 三张新表在回滚清单 + 三表存在 | 已覆盖：`test_delivery_audit.py`、`test_state_domains.py` |
| 结构断言：核心无坐标 / 寻路符号 | 已覆盖：`test_space_topology.py::test_core_has_no_coordinate_or_pathfinding_symbols`（含「距离」，比 `test_delivery_audit.py` 的副本更全） |
| 性能线 18 项（S-1 / S-4 / P0① / ④ / 页缓存 / 索引收敛） | 全部已覆盖：`test_delivery_audit.py` + `test_effect_retire.py`；**本文件用运行器端到端再锁一次** |

迁移时的两条纪律（都来自归档件里记录过的**假阳性**教训）：

- **收窄范围**：全文件 `'x' in src` 会被无关位置满足。迁进来的断言一律先切出**所属函数段**
  （`_segment`，且 `find` 不到就 `assert` 失败——归档件在这里是**不校验**的，见 README §4 D-4）。
- **能读真实值就不读源码文本**：`rate_max` / `autocommit_min_gap_seconds` 改为读
  `load_config()` 的默认值，`ix_reaction_timeline_stage` 改为读夹具实例的 `sqlite_master`。
"""

from __future__ import annotations

import ast
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from isekai_core.config import load_config
from isekai_core.store import Store

REPO = Path(__file__).resolve().parents[1]
ARCHIVE = REPO / "tools" / "archive_reference_audits"

#: 归档件清单：三套审计 + S-4 依据诊断 + 三个历史一次性补丁（见 README §1）。
ARCHIVED_FILES = (
    "self_audit.py",
    "final_audit.py",
    "perf_audit.py",
    "index_audit.py",
    "fix_audit.py",
    "fix_self_audit.py",
    "fix_perf_audit.py",
)

_RUNNER: ModuleType | None = None


def _runner() -> ModuleType:
    """按路径加载 `tools/run_audits.py`（`tools/` 不是包，别污染 `sys.path`）。"""
    global _RUNNER
    if _RUNNER is None:
        spec = importlib.util.spec_from_file_location("isekai_run_audits", REPO / "tools" / "run_audits.py")
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        # 必须先登记进 sys.modules：被加载模块里有 @dataclass，dataclasses 会去
        # sys.modules[cls.__module__] 取模块命名空间，漏登记会炸在 dataclasses.fields 上。
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _RUNNER = module
    return _RUNNER


def _src(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8")


def _segment(text: str, header: str) -> str:
    """取某个函数/方法的正文段。

    与归档件不同，**找不到 header 直接失败**：归档件用 `find` 不校验返回值，
    方法一改名切片就退化成 `text[-1:...]`，断言会以无意义的方式继续通过（README §4 D-4）。
    """
    start = text.find(header)
    assert start >= 0, f"未找到 {header}——被重构了？请同步更新本测试与归档说明"
    end = len(text)
    for marker in ("\ndef ", "\n    def ", "\nasync def ", "\n    async def "):
        pos = text.find(marker, start + len(header))
        if pos > 0:
            end = min(end, pos)
    segment = text[start:end]
    assert header.strip().split("(")[0].split()[-1] in segment
    return segment


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _identifiers(text: str) -> set[str]:
    """源码里出现过的标识符（含 import 的顶层模块名）；**注释与字符串字面量不算**。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module.split(".")[0])
    return found


def _called_names(text: str) -> set[str]:
    """源码里被**调用**的函数名（`f(x)` / `mod.f(x)` 都记 `f`）；同样不看注释。"""
    found: set[str] = set()
    for node in ast.walk(ast.parse(text)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            found.add(func.id)
        elif isinstance(func, ast.Attribute):
            found.add(func.attr)
    return found


@pytest.fixture
def fresh_instance(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """一个由**当前代码**建出的真实例根目录（`<root>/data/isekai.db`）。

    三套审计只查 `sqlite_master` 与源码文本，所以「空但结构完整」就是它们需要的全部前置条件；
    不需要 1500 世界日、不需要真实角色——这一点本身就是归档结论的一部分（README §2）。

    显式清掉 `ISEKAI_EFFECT_RETIRE`：审计断言里有一条是「B-7 的 `ix_effect_retire` 默认为**不存在**」，
    宿主环境若开着这个实验开关，新建的库就会带上它，测试会**假红**（`test_effect_retire.py`
    同样用 `monkeypatch.delenv` 制造默认态）。
    """
    monkeypatch.delenv("ISEKAI_EFFECT_RETIRE", raising=False)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    handle = Store(tmp_path / "data" / "isekai.db")
    handle.ensure_schema()
    try:
        yield tmp_path
    finally:
        handle.close()


# ---------------------------------------------------------------------------
# 1. 归档完整性：原件必须逐字节在场
# ---------------------------------------------------------------------------

def test_archived_scripts_match_the_manifest_byte_for_byte() -> None:
    """「原样归档」的机器证明：SHA256 必须与 `MANIFEST.sha256` 一致。

    归档件里保留着已知缺陷（README §4），所以它们**不该**被人顺手「修好」——
    修好就无法再证明「当时到底跑了什么」。
    """
    manifest_path = ARCHIVE / "MANIFEST.sha256"
    assert manifest_path.is_file(), "归档缺少 MANIFEST.sha256"
    manifest = {}
    for line in manifest_path.read_text(encoding="ascii").splitlines():
        if not line.strip():
            continue
        digest, name = line.split(None, 1)
        manifest[name.strip()] = digest.strip()
    assert set(manifest) == set(ARCHIVED_FILES), f"清单与预期不一致：{sorted(manifest)}"
    for name in ARCHIVED_FILES:
        path = ARCHIVE / name
        assert path.is_file(), f"归档件缺失：{name}"
        assert _sha256(path).lower() == manifest[name].lower(), (
            f"{name} 与 MANIFEST 不符——归档件必须与 .hermes/ 原件一致，"
            "若确需修正请同步更新 MANIFEST 并在 README 记明原因"
        )


def test_archive_readme_records_target_preconditions_and_dependency_rules() -> None:
    """README 必须写明：原靶子是什么、`fix_*.py` 是依赖还是补丁、以及「脚本没有退出码」。"""
    readme = (ARCHIVE / "README.md").read_text(encoding="utf-8")
    for token in (
        "acceptF",  # 原默认靶子
        "index_audit.py",  # S-4 依据的诊断工具（不是依赖）
        "fix_self_audit.py",  # 历史一次性补丁（不是依赖）
        "退出码",  # 「不设退出码、恒为 0」这条关键事实
    ):
        assert token in readme, f"README 未写明 {token!r}"


# ---------------------------------------------------------------------------
# 2. 运行器：把 78 项整体搬进 pytest
# ---------------------------------------------------------------------------

def test_resolve_target_accepts_both_root_and_db_path(tmp_path: Path) -> None:
    """`--instance` 既收实例根目录也收 db 路径——审计脚本只认根目录，人要方便。"""
    runner = _runner()
    root = tmp_path / "inst"
    (root / "data").mkdir(parents=True)
    db = root / "data" / "isekai.db"
    db.write_bytes(b"")
    assert runner.resolve_target(str(root)) == (root.resolve(), db.resolve())
    assert runner.resolve_target(str(db)) == (root.resolve(), db.resolve())


def test_resolve_target_rejects_a_missing_or_malformed_target(tmp_path: Path) -> None:
    runner = _runner()
    with pytest.raises(ValueError, match="不存在"):
        runner.resolve_target(str(tmp_path / "nope"))
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="data/isekai.db"):
        runner.resolve_target(str(empty))


def test_archived_audits_reproduce_78_of_78_on_a_fresh_instance(
    fresh_instance: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """端到端：三套归档审计在**夹具新建的实例**上必须 78/78，运行器退出码 0。

    这是「78 项已进 pytest」的载体：它不依赖 `.hermes/`、不依赖真实业务数据，
    只依赖「当前代码建出的实例结构」。任何一条审计断言失效，这里立刻红灯。
    """
    runner = _runner()
    code = runner.main(
        [
            "--instance", str(fresh_instance),
            "--log-dir", str(tmp_path / "logs"),
            "--quiet",
            "--json",
        ]
    )
    payload = json.loads(
        [line for line in capsys.readouterr().out.splitlines() if line.startswith("{")][-1]
    )
    assert code == 0, payload
    assert (payload["checks"], payload["passed"], payload["failed"]) == (78, 78, 0), payload
    by_script = {item["script"]: (item["passed"], item["checks"]) for item in payload["audits"]}
    assert by_script == {"self_audit": (30, 30), "final_audit": (30, 30), "perf_audit": (18, 18)}


def test_runner_turns_the_audits_failures_into_a_nonzero_exit_code(
    fresh_instance: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """运行器存在的**唯一理由**：审计脚本自己永不设退出码。

    这里人为把实例「改旧」（补回 S-4 之前的两条索引），复现归档结论里
    「旧靶子上必然 75/78」的形态：
      - 直接跑 `self_audit.py` 会打印 FAIL，但**退出码仍是 0**（脚本的硬缺陷，README §4 D-1）；
      - 经 `run_audits.py` 跑，则 75/78 且退出码 1，并在预检里点名这两条预期失败。
    """
    db = fresh_instance / "data" / "isekai.db"
    handle = Store(db)
    try:
        conn = handle._conn
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_effect_target "
            "ON effect_state(instance_id, timeline_id, target)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_effect_retire "
            "ON effect_state(instance_id, timeline_id, active)"
        )
        conn.commit()
    finally:
        handle.close()

    runner = _runner()
    code = runner.main(
        ["--instance", str(fresh_instance), "--log-dir", str(tmp_path / "logs"), "--quiet", "--json"]
    )
    out = capsys.readouterr().out
    payload = json.loads([line for line in out.splitlines() if line.startswith("{")][-1])
    assert code == 1, payload
    assert (payload["checks"], payload["passed"], payload["failed"]) == (78, 75, 3), payload
    assert payload["errored"] == [], "脚本本身没崩，是被审计的旧结构不达标"
    assert "ix_effect_target" in out, "预检必须点名旧索引，避免把预期失败读成回归"

    # 反向特征化：归档脚本自己在这个形态下也打印 FAIL，却依然退出 0。
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    log = tmp_path / "direct.log"
    with log.open("wb") as sink:
        proc = subprocess.run(
            [sys.executable, str(ARCHIVE / "self_audit.py"), str(fresh_instance)],
            cwd=str(REPO),
            stdout=sink,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
    text = log.read_text(encoding="utf-8", errors="replace")
    assert "FAIL" in text, "预期这条旧实例上 self_audit 会 FAIL"
    assert proc.returncode == 0, "归档脚本恒退出 0——这正是必须外挂运行器的原因"


# ---------------------------------------------------------------------------
# 3. 逐条迁移：只搬 pytest 里**还没有**的断言
# ---------------------------------------------------------------------------

def test_a5_claim_reads_are_scoped_to_the_page_and_to_the_known_set() -> None:
    """A-5「五处有界」中尚未被行为测试锁住的三处：

    - 页面装配只取**本页事件**的说法（原实现取该角色全部说法）；
    - 已知事件按 id **批量**取（不是逐个 `claim_get`）；
    - backfill 判空走 `has_events`（不是先取全表再判空）。
    """
    service = _src("isekai_core/runtime/service.py")
    assert "event_ids=page_event_ids" in service, "页面装配的说法查询必须收窄到本页事件"
    page_start = service.find("page_event_ids = [")
    assert page_start >= 0, "未找到本页事件 id 的构造点"
    page_window = service[page_start : page_start + 1200]
    assert "event_ids=page_event_ids" in page_window, "必须把本页事件 id 传进 claim_list"

    known_fn = _segment(service, "def _known_event_ids(")
    assert "ids=known" in known_fn, "已知事件必须按 id 批量取说法"
    assert "store.claim_list(" in known_fn

    assert "self.store.has_events(" in service, "backfill 判空要走 has_events，不得先取全表"


def test_a5_ops_single_claim_uses_the_primary_key_lookup() -> None:
    """A-5：管理面「单条说法」必须按主键取一行的路径。

    归档件只做 `'store.claim_get(' in ops.py`（12 万字符全文件），任何无关调用点都能满足它
    （README §4 D-5）。这里收窄到 `claim.coverage` 这个 op 的分支内。
    """
    ops = _src("isekai_core/world/ops.py")
    marker = 'if op == "claim.coverage":'
    start = ops.find(marker)
    assert start >= 0, f"未找到 {marker}"
    end = ops.find("\n        if op == ", start)
    window = ops[start : end if end > 0 else start + 2000]
    assert ".claim_get(" in window, "claim.coverage 必须走单条主键查询（claim_get）"
    assert "claim_list(" not in window, "单条说法不得退化成列表查询"


def test_a5_intent_stage_filter_is_pushed_down_not_filtered_in_python() -> None:
    """A-5：打算的阶段过滤要下推给存储层，别把三个阶段全捞回来再在 Python 里筛。

    注意断言方向：`service.py` 里 `intent_list(` 有**多处**调用，只有热路径那一处带 `stages=`。
    归档件查的是全文件出现过 `stages=(...)`——只要有一处带就够了；这里改成**成对**断言：
    带 `stages=` 的那一处必须真的是 `self.store.intent_list(` 调用。
    """
    service = _src("isekai_core/runtime/service.py")
    marker = 'stages=("adopted", "waiting", "deferred")'
    start = service.find(marker)
    assert start >= 0, f"未找到阶段下推参数 {marker}——过滤可能又退回 Python 侧了"
    window = service[max(0, start - 300) : start]
    assert "self.store.intent_list(" in window, (
        "阶段下推参数必须挂在 intent_list 调用上（别处出现 `stages=` 不算数）"
    )


def test_a11_default_rate_max_is_lowered(tmp_path: Path) -> None:
    """A-11：默认速率上限必须保持在下调后的值。

    归档件比对源码字面量 `rate_max: int = 864000`；这里直接读 `load_config()` 的真实默认值
    ——改默认值的写法（挪到别处、加后缀、改成 None 再回填）都不会再骗过断言。
    用空的 `tmp_path` 而不是仓库根：否则会读进仓库里的 `config.yaml`，那就不是「默认值」了。
    """
    cfg = load_config(tmp_path)
    assert cfg.runtime.rate_max == 864000, (
        f"默认 rate_max 应为 864000（10 世界日/现实秒），实际 {cfg.runtime.rate_max}"
    )


def test_a4_autocommit_min_gap_has_a_nonzero_default_and_is_wired(tmp_path: Path) -> None:
    """A-4：自动提交的「现实时间最小间隔」闸必须默认生效（0 等于关闸）。"""
    cfg = load_config(tmp_path)
    gap = cfg.runtime.autocommit_min_gap_seconds
    assert gap == 30.0 and gap > 0, f"默认最小间隔应为正数（30.0），实际 {gap!r}"
    assert "autocommit_min_gap_seconds" in _src("isekai_core/runtime/service.py"), "闸没接线"


def test_a10_lag_watermark_and_frozen_reason_are_present() -> None:
    """A-10：追赶要暴露**显式水位**（`lag_world_seconds`），且冻结仍返回「未就绪」。

    「追赶不再被拒」的状态机行为已由 `test_runtime_audit.py` 覆盖；这里补的是契约里
    容易被顺手删掉的两处可见性：水位字段与冻结文案。
    """
    service = _src("isekai_core/runtime/service.py")
    assert "catching_up" in service, "追赶态必须可见（否则又退回「拒绝服务」）"
    assert "lag_world_seconds" in service, "追赶必须给出显式水位，而不是让客户端猜"
    assert "时间线当前是" in service, "冻结态必须仍以 not_ready 拒绝，不得伪装成追赶"


def test_a8_world_fact_ids_stay_deterministic_and_secret_free() -> None:
    """A-8：世界事实路径不得依赖进程内随机源。

    `stable_key` 的编码已由 `test_determinism_contract.py` 锁住；这里补两条**负向**守卫
    ——它们防的是「把随机源加回来」这种回归，方向与正向测试相反。

    与归档件的差别：归档件用 `'secrets' not in src.replace('`secrets.token_hex`', '')` 这种
    **文本子串**检查（README §4 D-6）：注释里提一下 `secrets` 就会假红，而换一种写法引入随机源
    又未必被抓住。这里改成**语法级**：解析 AST，只看标识符与调用名，注释 / 文档字符串一律不算。
    """
    life = _src("isekai_core/runtime/life.py")
    assert "stable_key(instance_id, timeline_id, character_id" in life, "生活线计划 id 必须用 stable_key"
    assert "secrets" not in _identifiers(life), "life 不得回退到 secrets 生成 id"
    assert "random" not in _identifiers(life), "life 不得引入 random（同设定必须同答案）"

    personality = _src("isekai_core/runtime/personality.py")
    assert "hash" not in _called_names(personality), (
        "personality 不得调用内置 hash()（受 PYTHONHASHSEED 影响，会毁掉确定性）"
    )


def test_narrow_effect_fetch_and_target_pushdown_are_wired() -> None:
    """方案②「窄取数」+ 方案①「target 下推」：两半都必须在位，只留一半没有收益。"""
    store = _src("isekai_core/store.py")
    service = _src("isekai_core/runtime/service.py")
    assert "def effect_constraints(" in store, "缺窄取数方法 effect_constraints"
    assert "targets=tuple(sorted(constraint_targets))" in service, "target 下推没接线"
    assert ".effect_constraints(" in service, "窄取数方法存在但热路径没调用"

    memory = _src("isekai_core/runtime/memory.py")
    assert "def effective_strength(" in memory, "记忆强度必须惰性结算（A-1）"


def test_reaction_stage_index_survives_the_s4_pruning(fresh_instance: Path) -> None:
    """S-4 瘦身删掉了无人命中的索引，但**实测在用**的必须留下。

    `test_delivery_audit.py` 只锁了 `effect_state` 上的两条；`ix_reaction_timeline_stage`
    是反应阶段推进的热索引，此前只由未入库的 `self_audit.py` 守着。
    """
    handle = Store(fresh_instance / "data" / "isekai.db")
    try:
        names = {
            row[0]
            for row in handle._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        handle.close()
    assert "ix_reaction_timeline_stage" in names, "反应阶段推进的索引被误删（阶段推进将全表扫）"


def test_b9_named_rule_tests_actually_define_tests() -> None:
    """B-9：归档件断言的是 `Path('tests/test_ledger.py').exists()`——近乎同义反复（README §4 D-7）。

    加强为「存在 **且** 真的定义了测试函数」，否则删空文件也能通过。
    """
    path = REPO / "tests" / "test_ledger.py"
    assert path.is_file()
    tree = ast.parse(path.read_text(encoding="utf-8"))
    tests = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_")
    ]
    assert len(tests) >= 3, f"test_ledger.py 里的测试太少：{tests}"


def test_specs_keep_the_clauses_the_audits_cite() -> None:
    """规格文档是产品契约：审计引用的条款不能悄悄消失。

    这些条款此前**只**由 `.hermes/*_audit.py` 守着，pytest 里没有任何等价断言。
    """
    event_spec = _src("docs/worldruntime/EVENT_ENGINE_SPEC.md")
    runtime_spec = _src("docs/worldruntime/WORLD_RUNTIME_SPEC.md")
    iface_spec = _src("docs/worldruntime/WORLD_RUNTIME_INTERFACE_SPEC.md")
    memory_spec = _src("docs/worldruntime/MEMORY_SPEC.md")

    # B-5：身体后果只接受枚举档位（堵「自由数值后果」）
    assert "只接受枚举档位" in event_spec
    # B-4：拓扑不是坐标
    assert "是拓扑，不是坐标" in event_spec
    assert "不引入坐标与通行模拟" in runtime_spec
    # B-8：振荡器 / 棘轮（防「调参调到振荡」）
    assert "振荡器" in runtime_spec and "棘轮" in runtime_spec
    # A-10：接口规格必须同步为「追赶中返回 ok」，且不能再把追赶列进 not_ready
    assert "追赶中返回 `ok`" in iface_spec
    assert "追赶、冻结" not in iface_spec, "接口规格不得再声称追赶会被拒绝"
    # B-2：记忆规格的措辞修订（用户说法不等于角色亲历）
    assert "用户对世界" in memory_spec
