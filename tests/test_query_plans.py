"""两处第一档修法的回归守卫（第 59 轮）：**查询计划**与**索引在用**。

为什么锁「计划」而不是锁「耗时」：
耗时受机器与实例影响（本项目已固化 ±20% 噪声底线），而 `EXPLAIN QUERY PLAN` 的
`SCAN` / `SEARCH` 是**确定性**的——正是它把这两处缺陷暴露出来的（`.hermes/S3_ADVERSARIAL_REVIEW.md`）。

1. `clear_effects` 的 UPDATE（`store.apply_runtime_batch`）必须走**主键前缀**。
   旧写法 `WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)`
   里 `instance_id` 被 `OR` 包住 ⇒ SQLite 用不上主键 ⇒ `SCAN effect_state`（全表），
   而推进路径每批要跑 2.4–2.9 条 ⇒ 实测 0.30 ms/条、约 1.0 ms/世界日（占墙钟 ~24%）。
   改成主键全列等值后为 `SEARCH ... USING INDEX sqlite_autoindex_effect_state_1`，单条 0.187 ms。
2. `reaction` 必须有 `(timeline_id, source_ref)` 索引。
   只有 `(timeline_id, stage, started_world)` 时，那条取数要**逐行过滤 3,406 条 active/fading**
   （返回 0–6 行）：实测 **0.4172 → 0.0187 ms/条（22×）**。
"""

from __future__ import annotations

import sqlite3
from typing import Any

import pytest

INSTANCE = "in-plan"
TIMELINE = "tl-plan"


def _store(tmp_path) -> Any:
    from isekai_core.store import Store

    handle = Store(tmp_path / "isekai.db")
    handle.ensure_schema()
    return handle


def _plan(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> str:
    return " | ".join(str(row[3]) for row in conn.execute("EXPLAIN QUERY PLAN " + sql, params))


def _write_effect(store: Any, ident: str, at: int) -> None:
    store.apply_runtime_batch(
        timeline_id=TIMELINE,
        generation=1,
        processed_world=int(at),
        catching_up=False,
        effects=[
            {
                "id": ident,
                "instance_id": INSTANCE,
                "timeline_id": TIMELINE,
                "event_id": f"ev-{ident}",
                "target": "env-1",
                "kind": "environment_state",
                "family": "ef-1",
                "value": "1",
                "from_world": int(at),
                "expiry": "until_cleared",
                "recovery": "",
                "active": 1,
                "cleared_at": None,
            }
        ],
    )


@pytest.fixture
def store(tmp_path):
    handle = _store(tmp_path)
    handle.clock_put(
        {
            "timeline_id": TIMELINE,
            "base_real": 0.0,
            "base_world": 0,
            "rate": 1,
            "high_water_real": 0.0,
            "anchor_real": 0.0,
            "processed_world": 0,
            "generation": 1,
        }
    )
    yield handle
    handle.close()


def test_clear_effects_update_uses_the_primary_key_not_a_full_scan(store) -> None:
    """`clear_effects` 的解除 UPDATE 必须 `SEARCH`（主键前缀），不得 `SCAN effect_state`。"""
    _write_effect(store, "fx-plan-a", 100)
    plan = _plan(
        store._conn,
        """UPDATE effect_state SET active=0, cleared_at=?
           WHERE instance_id=? AND id=? AND timeline_id=? AND active=1""",
        (1, INSTANCE, "fx-plan-a", TIMELINE),
    )
    assert "SEARCH" in plan.upper(), f"必须走索引，实际计划 = {plan}"
    assert "SCAN" not in plan.upper(), f"不得全表扫描，实际计划 = {plan}"


def test_the_old_or_wrapped_where_degrades_to_a_scan(store) -> None:
    """反证：旧写法（`(? IS NULL OR instance_id=?)`）**确实**退化为全表扫描。

    这条用「旧写法必须 SCAN」把缺陷本身钉住：如果将来有人用别的形式把它改回来，
    只要那条形式仍退化成 SCAN，这里就说明「退化是真实存在的」，而不是我在猜测。
    """
    _write_effect(store, "fx-plan-b", 100)
    plan = _plan(
        store._conn,
        """UPDATE effect_state SET active=0, cleared_at=?
           WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)""",
        (1, "fx-plan-b", TIMELINE, None, None),
    )
    assert "SCAN" in plan.upper(), f"旧写法应当退化为 SCAN，实际计划 = {plan}"


def test_source_of_the_update_statement_uses_the_keyed_branch() -> None:
    """源码里那条 UPDATE 必须有「instance_id 在场 ⇒ 主键等值」的分支。

    计划断言证明「写对的形式能走索引」，这一条证明**生产代码用的就是这个形式**
    （否则计划断言可以全绿而生产仍走旧写法）。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "isekai_core" / "store.py"
    text = src.read_text(encoding="utf-8")
    assert "WHERE instance_id=? AND id=? AND timeline_id=? AND active=1" in text, (
        "clear_effects 的 UPDATE 必须用主键前缀等值形式"
    )
    keyed = text.index("if instance_id_:")
    assert "UPDATE effect_state SET active=0" in text[keyed : keyed + 400], (
        "instance_id 在场时的分支必须就是那条主键等值 UPDATE"
    )


def test_reaction_lookup_index_exists_and_is_used(store) -> None:
    """`reaction` 的 `(timeline_id, source_ref)` 取数必须走 `ix_reaction_source_ref`。"""
    indexes = {str(row[1]) for row in store._conn.execute("PRAGMA index_list('reaction')")}
    assert "ix_reaction_source_ref" in indexes, "缺少 (timeline_id, source_ref) 索引"

    plan = _plan(
        store._conn,
        "SELECT * FROM reaction WHERE timeline_id=? AND stage IN ('active','fading')"
        " AND source_ref IN (?,?)",
        (TIMELINE, "ref-a", "ref-b"),
    )
    assert "SEARCH" in plan.upper(), f"必须走索引，实际计划 = {plan}"
    assert "SCAN" not in plan.upper(), f"不得全表扫描，实际计划 = {plan}"


def test_reaction_index_definition_is_the_expected_column_pair() -> None:
    """索引列必须是 `(timeline_id, source_ref)`——列写反或用别的列就失去意义。"""
    store_src = (
        __import__("pathlib").Path(__file__).resolve().parents[1] / "isekai_core" / "store.py"
    ).read_text(encoding="utf-8")
    assert "CREATE INDEX IF NOT EXISTS ix_reaction_source_ref ON reaction(timeline_id, source_ref)" in store_src
