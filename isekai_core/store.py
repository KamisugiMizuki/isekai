"""SQLite 持久化（单一写入者）。

- WAL + 单连接 + 进程内锁；核心只有一个写库进程（DESKTOP_SPEC §2.1）。
- message 表同时承担入站去重与历史真值；delivery 表按批次记录投递状态。
- 去重作废记录（voided）不随世界历史回滚倒退，也不随实例导出（CHANNEL_PLUGIN_SPEC §2.2）。
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import struct
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .log import get_logger
from .version import DATA_FORMAT_VERSION

log = get_logger("isekai.store")
SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS channel_instance(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL UNIQUE,
  version TEXT,
  protocol TEXT NOT NULL DEFAULT '1.0',
  capabilities TEXT NOT NULL DEFAULT '{}',
  credential_hash TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'active',
  created_at REAL NOT NULL,
  last_seen_at REAL
);

CREATE TABLE IF NOT EXISTS session(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  created_at REAL NOT NULL,
  UNIQUE(instance_id, timeline_id, character_id)
);

CREATE TABLE IF NOT EXISTS thread(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  binding_version INTEGER NOT NULL DEFAULT 1,
  binding_token TEXT NOT NULL,
  updated_at REAL NOT NULL,
  UNIQUE(channel_id, thread_id)
);

CREATE TABLE IF NOT EXISTS message(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role TEXT NOT NULL,                 -- user | character | notice
  channel_id TEXT,
  thread_id TEXT,
  env_id TEXT,
  binding_version INTEGER,
  binding_token TEXT,                 -- 固化时捕获的目标令牌，不随重绑换代
  text TEXT,
  parts TEXT,
  message_id TEXT,                    -- 出站：稳定标识
  reply_message_id TEXT,              -- 入站：指向本轮的固化回复
  reply_to TEXT,
  batch_id TEXT,
  batch_index INTEGER,
  batch_count INTEGER,
  covers TEXT DEFAULT '[]',
  state TEXT NOT NULL,
  error_code TEXT,
  created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_message_inbound
  ON message(channel_id, thread_id, env_id) WHERE env_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS ux_message_out ON message(message_id) WHERE message_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_message_session ON message(session_id, seq);

CREATE TABLE IF NOT EXISTS delivery(
  msg_seq INTEGER NOT NULL,
  batch_index INTEGER NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',   -- pending|sent|accepted|failed|unknown
  updated_at REAL,
  PRIMARY KEY(msg_seq, batch_index)
);

CREATE TABLE IF NOT EXISTS voided(
  channel_id TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  env_id TEXT NOT NULL,
  reason TEXT,
  at REAL NOT NULL,
  PRIMARY KEY(channel_id, thread_id, env_id)
);

-- ---------- 世界设定层（阶段 1）：实例、锁定设定、时间线与提交 ----------

CREATE TABLE IF NOT EXISTS instance(
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  original_name TEXT NOT NULL,
  package_id TEXT NOT NULL,
  data_format TEXT NOT NULL,
  rules_version TEXT NOT NULL,
  app_version TEXT NOT NULL,
  seed TEXT NOT NULL,
  moment INTEGER NOT NULL,           -- 初始世界时刻（世界秒）
  setting TEXT NOT NULL,             -- 锁定设定快照 JSON：{world_package, original_name, cards}
  imported INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS timeline(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  name TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'frozen',   -- frozen|active|archived
  source_commit TEXT,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS commit_auto_state(
  timeline_id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  last_commit_at REAL NOT NULL DEFAULT 0,
  last_commit_moment INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS event_draft(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  source_timeline TEXT NOT NULL,
  source_commit TEXT,
  payload TEXT NOT NULL,               -- 规范化后的草案（用户意图 + 已确认部分）
  state TEXT NOT NULL DEFAULT 'draft', -- draft|confirmed|rejected
  timeline_id TEXT NOT NULL DEFAULT '',-- 确认后落成的新线
  created_world INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_event(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  at_world INTEGER NOT NULL,
  payload TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending',  -- pending|applied|cancelled|skipped
  note TEXT NOT NULL DEFAULT '',
  created_world INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS commit_snapshot(
  commit_id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  size INTEGER NOT NULL DEFAULT 0,
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS commit_log(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  kind TEXT NOT NULL,                     -- initial|manual|auto|import
  moment INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  created_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_timeline_instance ON timeline(instance_id);
CREATE INDEX IF NOT EXISTS ix_commit_instance ON commit_log(instance_id, timeline_id, created_at);
CREATE INDEX IF NOT EXISTS ix_session_instance ON session(instance_id);
-- 名称唯一：规范化比较在应用层（NFKC + 大小写折叠），这里兜底同名直插（§7.4）
CREATE UNIQUE INDEX IF NOT EXISTS ux_instance_name ON instance(name);

-- ---------- 世界运行层（阶段 2）：时钟、倍率、水位、角色状态 ----------

CREATE TABLE IF NOT EXISTS timeline_clock(
  timeline_id TEXT PRIMARY KEY,
  base_real REAL NOT NULL,               -- 当前倍率段的起点（现实秒，UTC）
  base_world INTEGER NOT NULL,           -- 当前倍率段的起点（世界秒）
  rate INTEGER NOT NULL DEFAULT 1,       -- 当前倍率（正整数；冻结用时间线状态表达）
  high_water_real REAL NOT NULL DEFAULT 0,
  anchor_real REAL NOT NULL,             -- 激活时的现实锚点
  processed_world INTEGER NOT NULL,      -- 已处理世界时刻（共同水位）
  generation INTEGER NOT NULL DEFAULT 1, -- 运行世代：回滚 / 删除使旧任务失效
  catching_up INTEGER NOT NULL DEFAULT 0,-- 目标时刻领先于处理水位（§2.6）
  limited INTEGER NOT NULL DEFAULT 0     -- 追赶受限：滞后超过预算，停止扩大目标（§2.6）
);

-- ---------- 事件引擎（阶段 3）：事件 / 说法 / 获知 / 效果状态 ----------

-- 环境事实状态（WORLD_RUNTIME_SPEC §11.2）：可选状态域，未声明的类型没有真值
CREATE TABLE IF NOT EXISTS environment_state(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  type_id TEXT NOT NULL,
  value TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL DEFAULT '',       -- initial | natural:<名> | event:<事件标识>
  from_world INTEGER NOT NULL DEFAULT 0,
  expiry TEXT NOT NULL DEFAULT 'until_cleared',
  updated_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, type_id)
);

-- 外部调用账本（WORLD_RUNTIME_SPEC §2.8）：只记次数与量级，不记正文 / prompt / 密钥
CREATE TABLE IF NOT EXISTS call_ledger(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  task TEXT NOT NULL,
  bucket INTEGER NOT NULL,               -- 现实日窗口（UTC 日序）
  calls INTEGER NOT NULL DEFAULT 0,
  tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, task, bucket)
);

CREATE TABLE IF NOT EXISTS memory(
  id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  text TEXT NOT NULL,
  kind TEXT NOT NULL,
  sources TEXT NOT NULL DEFAULT '[]',
  happened_world INTEGER,
  learned_world INTEGER NOT NULL,
  recorded_world INTEGER NOT NULL,
  semantic_watermark INTEGER NOT NULL,
  strength REAL NOT NULL DEFAULT 0.6,
  confidence REAL NOT NULL DEFAULT 0.7,
  state TEXT NOT NULL DEFAULT 'active',
  version INTEGER NOT NULL DEFAULT 1,
  supersedes TEXT,
  superseded_by TEXT,
  source_key TEXT,
  decay_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, id)
);

CREATE TABLE IF NOT EXISTS memory_task(
  id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  source_kind TEXT NOT NULL,
  source_ref TEXT NOT NULL,
  source_world INTEGER NOT NULL,
  created_world INTEGER NOT NULL,
  text TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  note TEXT NOT NULL DEFAULT '',
  UNIQUE(instance_id, timeline_id, character_id, source_kind, source_ref),
  PRIMARY KEY(instance_id, timeline_id, id)
);

CREATE TABLE IF NOT EXISTS memory_citation(
  turn_id TEXT NOT NULL,
  memory_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  world_seconds INTEGER NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(turn_id, memory_id)
);

CREATE TABLE IF NOT EXISTS memory_embedding(
  memory_id TEXT NOT NULL,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL DEFAULT '',
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  vector BLOB NOT NULL,
  source_version INTEGER NOT NULL,
  created_at REAL NOT NULL,
  PRIMARY KEY(instance_id, timeline_id, memory_id)
);

CREATE TABLE IF NOT EXISTS budget_reserve(
  id TEXT PRIMARY KEY,
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  task TEXT NOT NULL,
  bucket INTEGER NOT NULL,               -- 现实日窗口
  priority INTEGER NOT NULL,
  tokens_est INTEGER NOT NULL DEFAULT 0,
  tokens_actual INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'held',    -- held / settled / released
  outcome_kind TEXT NOT NULL DEFAULT '', -- ok / error / timeout / cancelled
  created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS budget_policy(
  instance_id TEXT PRIMARY KEY,
  paused_tasks TEXT NOT NULL DEFAULT '[]',
  instance_tokens_per_day INTEGER,
  timeline_tokens_per_day INTEGER,
  task_tokens_per_day INTEGER,
  updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS event(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  id TEXT NOT NULL,                      -- 稳定事件标识（种子 + 规则 + 历法日 + 槽）
  world_seconds INTEGER NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,        -- 同刻顺序（固定规则，不随消费者执行先后变）
  kind TEXT NOT NULL,                    -- world | character
  family TEXT NOT NULL DEFAULT '',
  template TEXT NOT NULL DEFAULT '',
  source TEXT NOT NULL,                  -- engine | backfill | life | character_action
  summary TEXT NOT NULL,                 -- 事实骨架（确定性；写后不可原地改写）
  detail TEXT NOT NULL DEFAULT '',       -- 实情文本（模板或 LLM 表述，固化后不改）
  text_source TEXT NOT NULL DEFAULT 'template',
  effects TEXT NOT NULL DEFAULT '[]',
  share_value INTEGER NOT NULL DEFAULT 0,
  importance REAL NOT NULL DEFAULT 0.0,
  created_real REAL NOT NULL DEFAULT 0.0,
  PRIMARY KEY(instance_id, timeline_id, id)
);
CREATE INDEX IF NOT EXISTS ix_event_window ON event(instance_id, timeline_id, world_seconds);

CREATE TABLE IF NOT EXISTS claim(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  source_id TEXT NOT NULL DEFAULT '',
  text TEXT NOT NULL,
  audience TEXT NOT NULL DEFAULT '公开',
  earliest_world INTEGER NOT NULL,       -- 最早可传播时刻
  credibility TEXT NOT NULL DEFAULT 'recorded',
  derived_from TEXT,                     -- 派生记录：展开自哪条说法（不原地改写原条目）
  PRIMARY KEY(instance_id, timeline_id, id)
);
CREATE INDEX IF NOT EXISTS ix_claim_event ON claim(instance_id, timeline_id, event_id);

CREATE TABLE IF NOT EXISTS knowledge(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  id TEXT NOT NULL,
  world_seconds INTEGER NOT NULL,        -- 获知时刻
  kind TEXT NOT NULL,                    -- claim | observation | experience
  target TEXT NOT NULL,                  -- 说法标识或事件标识
  source TEXT NOT NULL DEFAULT '',       -- 渠道 / 亲历
  stance TEXT NOT NULL DEFAULT 'recorded',
  text TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(instance_id, timeline_id, character_id, id)
);
CREATE INDEX IF NOT EXISTS ix_knowledge_character ON knowledge(instance_id, timeline_id, character_id, world_seconds);

-- 角色打算（WORLD_RUNTIME_SPEC §11.3）：角色状态，不是世界事实
CREATE TABLE IF NOT EXISTS intent(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  id TEXT NOT NULL,
  object TEXT NOT NULL,
  basis TEXT NOT NULL DEFAULT '',
  strength REAL NOT NULL DEFAULT 0.5,
  window_from INTEGER NOT NULL DEFAULT 0,
  window_to INTEGER NOT NULL DEFAULT 0,
  preconditions TEXT NOT NULL DEFAULT '[]',
  effect TEXT NOT NULL DEFAULT '{}',
  stage TEXT NOT NULL DEFAULT 'adopted',   -- candidate|adopted|waiting|done|deferred|abandoned
  note TEXT NOT NULL DEFAULT '',
  source_world INTEGER NOT NULL DEFAULT 0,
  updated_world INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(instance_id, timeline_id, character_id, id)
);
CREATE INDEX IF NOT EXISTS ix_intent_character ON intent(instance_id, timeline_id, character_id, stage);

CREATE TABLE IF NOT EXISTS effect_state(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  id TEXT NOT NULL,
  event_id TEXT NOT NULL,
  target TEXT NOT NULL DEFAULT '',
  kind TEXT NOT NULL,
  family TEXT NOT NULL DEFAULT '',       -- 事件族：自然恢复按同族后续事件判定（§六）
  value TEXT,                            -- 环境类效果的取值（取值域内）
  from_world INTEGER NOT NULL,
  expiry TEXT NOT NULL DEFAULT 'until_cleared',
  recovery TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  cleared_at INTEGER,
  PRIMARY KEY(instance_id, timeline_id, id)
);
CREATE INDEX IF NOT EXISTS ix_effect_active ON effect_state(instance_id, timeline_id, active, from_world);

-- 补卡：角色在某个世界时刻加入该线（实例设定锁死，加入记录只进运行层，§九 / 附录 B #18）
CREATE TABLE IF NOT EXISTS character_join(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  joined_world INTEGER NOT NULL,
  card TEXT NOT NULL,                    -- 卡片快照（复核后固化）
  note TEXT NOT NULL DEFAULT '',
  acquainted INTEGER NOT NULL DEFAULT 0, -- 「已相识」声明：补一条对话单元
  created_real REAL NOT NULL,
  PRIMARY KEY(instance_id, timeline_id, character_id)
);

CREATE TABLE IF NOT EXISTS rate_command(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  timeline_id TEXT NOT NULL,
  input_real REAL NOT NULL,              -- 权威输入时刻
  effective_real INTEGER NOT NULL,       -- 生效整秒
  rate INTEGER NOT NULL,
  seq INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'pending'  -- pending|applied|cancelled
);
CREATE INDEX IF NOT EXISTS ix_rate_pending ON rate_command(timeline_id, state, effective_real);

CREATE TABLE IF NOT EXISTS unit(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  id TEXT NOT NULL,
  mode TEXT NOT NULL,                    -- anchor|event|dialog|time
  semantic TEXT NOT NULL,
  basis TEXT NOT NULL,
  confidence REAL NOT NULL,
  stability REAL NOT NULL DEFAULT 0,     -- 累积稳定度（驱动迁移的隐式依据，不对用户可见）
  archived INTEGER NOT NULL DEFAULT 0,
  consumed TEXT NOT NULL DEFAULT '[]',   -- 已消费来源键（同一来源只消费一次）
  updated_world INTEGER NOT NULL,
  PRIMARY KEY(instance_id, timeline_id, character_id, id)
);
CREATE INDEX IF NOT EXISTS ix_unit_character ON unit(instance_id, timeline_id, character_id);

CREATE TABLE IF NOT EXISTS life_plan(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  day_index INTEGER NOT NULL,
  windows TEXT NOT NULL,                 -- 展开后的世界时间窗（含跨日）
  state TEXT NOT NULL DEFAULT 'fixed',
  created_world INTEGER NOT NULL,
  note TEXT NOT NULL DEFAULT '',
  id TEXT NOT NULL,
  PRIMARY KEY(instance_id, timeline_id, character_id, day_index),
  UNIQUE(instance_id, timeline_id, id)
);

CREATE TABLE IF NOT EXISTS experience(
  instance_id TEXT NOT NULL,
  timeline_id TEXT NOT NULL,
  character_id TEXT NOT NULL,
  id TEXT NOT NULL,
  world_seconds INTEGER NOT NULL,
  kind TEXT NOT NULL,                    -- life|knowledge|dialog
  summary TEXT NOT NULL,
  source_ref TEXT,                       -- 来源稳定标识（亲历为空）
  confidence TEXT NOT NULL DEFAULT 'experienced',
  PRIMARY KEY(instance_id, timeline_id, character_id, id)
);
CREATE INDEX IF NOT EXISTS ix_experience_window ON experience(instance_id, timeline_id, character_id, world_seconds);
"""


class EnvelopeConflict(Exception):
    """同键异文：不能静默丢弃或再执行。"""

    def __init__(self, existing: dict[str, Any]) -> None:
        super().__init__("same envelope id with different text")
        self.existing = existing


def _relabel_payload(payload: dict[str, Any], instance_id: str, timeline_id: str) -> dict[str, Any]:
    """把快照里每行都归到目标实例 / 线：分叉与回滚复用同一条装载路径（导入路径早已重映射过）。"""
    out: dict[str, Any] = dict(payload)
    for key, value in payload.items():
        if not isinstance(value, list):
            continue
        rows: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            row = dict(item)
            if "instance_id" in row:
                row["instance_id"] = instance_id
            if "timeline_id" in row:
                row["timeline_id"] = timeline_id
            rows.append(row)
        out[key] = rows
    return out


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def hash_credential(credential: str) -> str:
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def new_credential() -> str:
    return f"cr-{secrets.token_urlsafe(32)}"


def new_binding_token() -> str:
    return f"bt-{secrets.token_urlsafe(18)}"


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # ponytail: 单连接 + 锁；本地单用户负载，写盘是 WAL 级小事务，必要时再拆写入线程
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")

    # ---------- 生命周期 ----------

    def _migrate_memory_tables(self) -> None:
        """旧库迁移：memory / memory_task / memory_embedding 改成按线隔离的主键。"""
        def sql_of(table: str) -> str:
            row = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()
            return str(row["sql"] or "") if row is not None else ""

        timeline_sql = sql_of("timeline")
        if timeline_sql and "description" not in timeline_sql:
            with self._lock, self._conn:
                self._conn.execute("ALTER TABLE timeline ADD COLUMN description TEXT NOT NULL DEFAULT ''")

        memory_sql = sql_of("memory")
        if memory_sql and "PRIMARY KEY(instance_id, timeline_id, id)" not in memory_sql:
            with self._lock, self._conn:
                self._conn.execute(
                    """CREATE TABLE memory_new(
                         id TEXT NOT NULL, instance_id TEXT NOT NULL, timeline_id TEXT NOT NULL,
                         character_id TEXT NOT NULL, text TEXT NOT NULL, kind TEXT NOT NULL,
                         sources TEXT NOT NULL DEFAULT '[]', happened_world INTEGER,
                         learned_world INTEGER NOT NULL, recorded_world INTEGER NOT NULL,
                         semantic_watermark INTEGER NOT NULL, strength REAL NOT NULL DEFAULT 0.6,
                         confidence REAL NOT NULL DEFAULT 0.7, state TEXT NOT NULL DEFAULT 'active',
                         version INTEGER NOT NULL DEFAULT 1, supersedes TEXT, superseded_by TEXT,
                         source_key TEXT, decay_world INTEGER NOT NULL DEFAULT 0,
                         PRIMARY KEY(instance_id, timeline_id, id))"""
                )
                self._conn.execute(
                    """INSERT OR IGNORE INTO memory_new SELECT id, instance_id, timeline_id, character_id,
                         text, kind, sources, happened_world, learned_world, recorded_world,
                         semantic_watermark, strength, confidence, state, version, supersedes,
                         superseded_by, source_key, decay_world FROM memory"""
                )
                self._conn.execute("DROP TABLE memory")
                self._conn.execute("ALTER TABLE memory_new RENAME TO memory")

        task_sql = sql_of("memory_task")
        if task_sql and "PRIMARY KEY(instance_id, timeline_id, id)" not in task_sql:
            with self._lock, self._conn:
                self._conn.execute(
                    """CREATE TABLE memory_task_new(
                         id TEXT NOT NULL, instance_id TEXT NOT NULL, timeline_id TEXT NOT NULL,
                         character_id TEXT NOT NULL, source_kind TEXT NOT NULL, source_ref TEXT NOT NULL,
                         source_world INTEGER NOT NULL, created_world INTEGER NOT NULL,
                         text TEXT NOT NULL DEFAULT '', state TEXT NOT NULL DEFAULT 'pending',
                         attempts INTEGER NOT NULL DEFAULT 0, note TEXT NOT NULL DEFAULT '',
                         UNIQUE(instance_id, timeline_id, character_id, source_kind, source_ref),
                         PRIMARY KEY(instance_id, timeline_id, id))"""
                )
                self._conn.execute(
                    """INSERT OR IGNORE INTO memory_task_new SELECT id, instance_id, timeline_id,
                         character_id, source_kind, source_ref, source_world, created_world, text,
                         state, attempts, note FROM memory_task"""
                )
                self._conn.execute("DROP TABLE memory_task")
                self._conn.execute("ALTER TABLE memory_task_new RENAME TO memory_task")

        embed_sql = sql_of("memory_embedding")
        if embed_sql and "timeline_id" not in embed_sql:
            with self._lock, self._conn:
                self._conn.execute(
                    """CREATE TABLE memory_embedding_new(
                         memory_id TEXT NOT NULL, instance_id TEXT NOT NULL,
                         timeline_id TEXT NOT NULL DEFAULT '', model TEXT NOT NULL, dim INTEGER NOT NULL,
                         vector BLOB NOT NULL, source_version INTEGER NOT NULL, created_at REAL NOT NULL,
                         PRIMARY KEY(instance_id, timeline_id, memory_id))"""
                )
                self._conn.execute(
                    """INSERT OR IGNORE INTO memory_embedding_new
                         (memory_id, instance_id, timeline_id, model, dim, vector, source_version, created_at)
                       SELECT e.memory_id, e.instance_id, COALESCE(m.timeline_id, ''), e.model, e.dim,
                              e.vector, e.source_version, e.created_at
                       FROM memory_embedding e LEFT JOIN memory m ON m.id = e.memory_id"""
                )
                self._conn.execute("DROP TABLE memory_embedding")
                self._conn.execute("ALTER TABLE memory_embedding_new RENAME TO memory_embedding")

    def ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate_memory_tables()
            self._migrate_runtime_tables()
            row = self._conn.execute("SELECT value FROM meta WHERE key='data_format'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('data_format', ?)", (DATA_FORMAT_VERSION,)
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (str(SCHEMA_VERSION),)
            )
            self._conn.commit()

    def _migrate_runtime_tables(self) -> None:
        """运行层表的形态迁移（旧库就地升级；派生数据重建是最后手段）。"""
        expected = {
            "unit": ("instance_id", "timeline_id", "character_id", "id"),
            "experience": ("instance_id", "timeline_id", "character_id", "id"),
            "life_plan": ("instance_id", "timeline_id", "character_id", "day_index"),
        }
        for table, columns in expected.items():
            rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
            if not rows:
                continue
            primary = tuple(row["name"] for row in rows if row["pk"])
            if primary == columns:
                continue
            log.warning("重建运行层表 %s（主键形态升级：%s → %s）", table, primary, columns)
            self._conn.executescript(f"DROP TABLE {table};")
        # 加列：不重建，直接补（§2.6 追赶状态与补卡表随阶段 2 审计加入）
        clock_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(timeline_clock)")}
        for name, ddl in (("catching_up", "INTEGER NOT NULL DEFAULT 0"), ("limited", "INTEGER NOT NULL DEFAULT 0")):
            if clock_columns and name not in clock_columns:
                log.info("timeline_clock 增列 %s", name)
                self._conn.execute(f"ALTER TABLE timeline_clock ADD COLUMN {name} {ddl}")
        claim_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(claim)")}
        if claim_columns and "derived_from" not in claim_columns:
            log.info("claim 增列 derived_from")
            self._conn.execute("ALTER TABLE claim ADD COLUMN derived_from TEXT")
        effect_columns = {row["name"] for row in self._conn.execute("PRAGMA table_info(effect_state)")}
        if effect_columns and "family" not in effect_columns:
            log.info("effect_state 增列 family")
            self._conn.execute("ALTER TABLE effect_state ADD COLUMN family TEXT NOT NULL DEFAULT ''")
        if effect_columns and "value" not in effect_columns:
            log.info("effect_state 增列 value")
            self._conn.execute("ALTER TABLE effect_state ADD COLUMN value TEXT")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.commit()
            self._conn.close()

    # ---------- 通道实例 ----------

    def channel_by_name(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM channel_instance WHERE name=?", (name,)).fetchone()
        return _row_to_dict(row) if row else None

    def channel_get(self, channel_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM channel_instance WHERE id=?", (channel_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def channel_register(
        self,
        *,
        name: str,
        display_name: str,
        version: str,
        protocol: str,
        capabilities: dict[str, Any],
        rotate: bool = True,
    ) -> tuple[dict[str, Any], str | None]:
        """登记通道实例并签发凭据，返回 (行, 明文凭据)。

        `rotate=False` 且实例已存在时不轮换：凭据只在首次签发或显式轮换时更新，
        否则多个调用方会互相把对方踢下线。返回的凭据为 None 表示沿用既有凭据。
        """
        credential = new_credential()
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM channel_instance WHERE name=?", (name,)).fetchone()
            if row is None:
                channel_id = f"ci-{secrets.token_hex(4)}"
                self._conn.execute(
                    """INSERT INTO channel_instance(id, name, version, protocol, capabilities,
                                                   credential_hash, created_at, last_seen_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (
                        channel_id,
                        name,
                        version,
                        protocol,
                        json.dumps(capabilities, ensure_ascii=False),
                        hash_credential(credential),
                        now,
                        now,
                    ),
                )
                self._conn.commit()
                return self.channel_get(channel_id), credential  # type: ignore[return-value]
            channel_id = row["id"]
            if rotate:
                self._conn.execute(
                    """UPDATE channel_instance SET version=?, protocol=?, capabilities=?,
                                                   credential_hash=?, last_seen_at=? WHERE id=?""",
                    (
                        version,
                        protocol,
                        json.dumps(capabilities, ensure_ascii=False),
                        hash_credential(credential),
                        now,
                        channel_id,
                    ),
                )
                issued: str | None = credential
            else:
                self._conn.execute(
                    "UPDATE channel_instance SET version=?, last_seen_at=? WHERE id=?",
                    (version, now, channel_id),
                )
                issued = None
        return self.channel_get(channel_id), issued  # type: ignore[return-value]

    def channel_verify_credential(self, name: str, credential: str) -> dict[str, Any] | None:
        row = self.channel_by_name(name)
        if row is None:
            return None
        if not secrets.compare_digest(row["credential_hash"], hash_credential(credential)):
            return None
        return row

    def channel_touch(self, channel_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE channel_instance SET last_seen_at=? WHERE id=?", (time.time(), channel_id)
            )

    def channel_set_handshake(self, channel_id: str, protocol: str, capabilities: dict[str, Any]) -> None:
        """持久记录协商结果：协议版本、能力交集与限额（握手一次，重连复用）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE channel_instance SET protocol=?, capabilities=?, last_seen_at=? WHERE id=?",
                (protocol, json.dumps(capabilities, ensure_ascii=False), time.time(), channel_id),
            )

    # ---------- 会话 ----------

    def session_ensure(self, instance_id: str, timeline_id: str, character_id: str) -> dict[str, Any]:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM session WHERE instance_id=? AND timeline_id=? AND character_id=?",
                (instance_id, timeline_id, character_id),
            ).fetchone()
            if row is None:
                session_id = f"se-{secrets.token_hex(4)}"
                self._conn.execute(
                    "INSERT INTO session(id, instance_id, timeline_id, character_id, created_at) VALUES(?,?,?,?,?)",
                    (session_id, instance_id, timeline_id, character_id, time.time()),
                )
                row = self._conn.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()
        return _row_to_dict(row)  # type: ignore[arg-type]

    def session_get(self, session_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM session WHERE id=?", (session_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def session_list(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM session ORDER BY created_at").fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- thread 绑定 ----------

    def thread_get(self, channel_id: str, thread_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM thread WHERE channel_id=? AND thread_id=?", (channel_id, thread_id)
        ).fetchone()
        return _row_to_dict(row) if row else None

    def thread_bind(self, channel_id: str, thread_id: str, session_id: str) -> dict[str, Any]:
        """绑定 / 重绑：换代表令（binding_version + token 一起换）。"""
        token = new_binding_token()
        now = time.time()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM thread WHERE channel_id=? AND thread_id=?", (channel_id, thread_id)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO thread(channel_id, thread_id, session_id, binding_version, binding_token, updated_at)
                       VALUES(?,?,?,1,?,?)""",
                    (channel_id, thread_id, session_id, token, now),
                )
            else:
                self._conn.execute(
                    """UPDATE thread SET session_id=?, binding_version=binding_version+1,
                                         binding_token=?, updated_at=? WHERE id=?""",
                    (session_id, token, now, row["id"]),
                )
        return self.thread_get(channel_id, thread_id)  # type: ignore[return-value]

    def thread_list(self, channel_id: str | None = None) -> list[dict[str, Any]]:
        if channel_id is None:
            rows = self._conn.execute("SELECT * FROM thread ORDER BY id").fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM thread WHERE channel_id=? ORDER BY id", (channel_id,)
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 入站 ----------

    def inbound_put(
        self,
        *,
        session_id: str,
        channel_id: str,
        thread_id: str,
        env_id: str,
        binding_version: int,
        text: str,
    ) -> tuple[dict[str, Any], bool]:
        """插入入站消息；同键同文返回既有行，同键异文抛 EnvelopeConflict。"""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM message WHERE channel_id=? AND thread_id=? AND env_id=?",
                (channel_id, thread_id, env_id),
            ).fetchone()
            if row is not None:
                if row["text"] != text:
                    raise EnvelopeConflict(_row_to_dict(row))
                return _row_to_dict(row), False
            cur = self._conn.execute(
                """INSERT INTO message(session_id, role, channel_id, thread_id, env_id, binding_version,
                                       text, state, created_at)
                   VALUES(?, 'user', ?,?,?,?,?, 'queued', ?)""",
                (session_id, channel_id, thread_id, env_id, binding_version, text, time.time()),
            )
            seq = cur.lastrowid
            row = self._conn.execute("SELECT * FROM message WHERE seq=?", (seq,)).fetchone()
        return _row_to_dict(row), True  # type: ignore[arg-type]

    def message_get(self, seq: int) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM message WHERE seq=?", (seq,)).fetchone()
        return _row_to_dict(row) if row else None

    def inbound_find(self, channel_id: str, thread_id: str, env_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM message WHERE channel_id=? AND thread_id=? AND env_id=?",
            (channel_id, thread_id, env_id),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def inbound_set_state(self, seq: int, state: str, *, error_code: str | None = None,
                          reply_message_id: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE message SET state=?, error_code=?,
                                     reply_message_id=COALESCE(?, reply_message_id) WHERE seq=?""",
                (state, error_code, reply_message_id, seq),
            )

    def interrupt_open_turns(self) -> int:
        """启动时收尾上次进程留下的未完成轮次：标记中断，等待显式重试。

        提交是单事务（状态与产物一起发布），因此 queued / processing 的行必然没有
        已固化的结果，标记为 interrupted 可重试不会造成漏单或执行两次（SESSION_CORE §4.3）。
        """
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE message SET state='failed', error_code='interrupted'
                   WHERE role='user' AND state IN ('queued','processing')"""
            )
            return int(cur.rowcount or 0)

    # ---------- 出站 ----------

    def outbound_put(
        self,
        *,
        session_id: str,
        message_id: str,
        reply_to: str | None,
        covers: Iterable[str],
        batches: list[list[str]],
        target_channel: str,
        target_thread: str,
        binding_version: int,
        binding_token: str,
    ) -> dict[str, Any]:
        """固化回复（一次逻辑轮次的产物）。batches 是分批后的分段计划。"""
        with self._lock, self._conn:
            return self._outbound_put_locked(
                session_id=session_id,
                message_id=message_id,
                reply_to=reply_to,
                covers=covers,
                batches=batches,
                target_channel=target_channel,
                target_thread=target_thread,
                binding_version=binding_version,
                binding_token=binding_token,
            )

    def _outbound_put_locked(self, **kwargs: Any) -> dict[str, Any]:
        session_id = kwargs["session_id"]
        message_id = kwargs["message_id"]
        batches = kwargs["batches"]
        covers_json = json.dumps(list(kwargs["covers"]), ensure_ascii=False)
        now = time.time()
        cur = self._conn.execute(
            """INSERT INTO message(session_id, role, channel_id, thread_id, binding_version, binding_token,
                                   message_id, reply_to, batch_id, batch_index, batch_count, parts, covers,
                                   state, created_at)
               VALUES(?, 'character', ?,?,?,?, ?,?, ?, 0, ?, ?, ?, 'fixed', ?)""",
            (
                session_id,
                kwargs["target_channel"],
                kwargs["target_thread"],
                kwargs["binding_version"],
                kwargs["binding_token"],
                message_id,
                kwargs["reply_to"],
                message_id,
                len(batches),
                json.dumps(batches, ensure_ascii=False),
                covers_json,
                now,
            ),
        )
        msg_seq = cur.lastrowid
        for index in range(len(batches)):
            self._conn.execute(
                "INSERT OR REPLACE INTO delivery(msg_seq, batch_index, state, updated_at) VALUES(?,?, 'pending', ?)",
                (msg_seq, index, now),
            )
        row = self._conn.execute("SELECT * FROM message WHERE seq=?", (msg_seq,)).fetchone()
        return _row_to_dict(row)  # type: ignore[arg-type]

    def commit_turn(
        self,
        *,
        inbound_seq: int,
        outbound: dict[str, Any],
    ) -> dict[str, Any]:
        """一次逻辑轮次的固化：回复与输入处理状态共同发布（SESSION_CORE_SPEC §4.2）。"""
        with self._lock, self._conn:
            row = self._outbound_put_locked(**outbound)
            self._conn.execute(
                "UPDATE message SET state='done', error_code=NULL, reply_message_id=? WHERE seq=?",
                (outbound["message_id"], inbound_seq),
            )
            return row

    def outbound_by_message_id(self, message_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM message WHERE message_id=? AND role != 'user'", (message_id,)
        ).fetchone()
        return _row_to_dict(row) if row else None

    # ---------- 投递 ----------

    def delivery_rows(self, msg_seq: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM delivery WHERE msg_seq=? ORDER BY batch_index", (msg_seq,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def delivery_set(self, msg_seq: int, batch_index: int, state: str) -> str:
        """更新单批状态并返回整条消息的汇总状态。

        迟到回执不能倒退已确认状态：`accepted` 不被 `unknown` 覆盖（CHANNEL_PLUGIN_SPEC §2.3）。
        """
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT state FROM delivery WHERE msg_seq=? AND batch_index=?", (msg_seq, batch_index)
            ).fetchone()
            if row is not None:
                current = row["state"]
                regress = current == "accepted" and state in ("unknown", "pending")
                if not regress:
                    self._conn.execute(
                        "UPDATE delivery SET state=?, updated_at=? WHERE msg_seq=? AND batch_index=?",
                        (state, time.time(), msg_seq, batch_index),
                    )
        return self.delivery_rollup(msg_seq)

    def delivery_rollup(self, msg_seq: int) -> str:
        states = [r["state"] for r in self.delivery_rows(msg_seq)]
        if not states:
            return "unknown"
        if all(s == "accepted" for s in states):
            return "delivered"
        if any(s == "unknown" for s in states):
            return "unknown"
        if any(s == "failed" for s in states):
            return "failed"
        if any(s == "incompatible" for s in states):
            return "incompatible"
        if all(s in ("sent", "accepted") for s in states):
            return "sent"
        return "pending"

    def pending_outbound(self, channel_id: str, thread_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """仍可投递的已固化回复（未确认成功的批次）。"""
        rows = self._conn.execute(
            """SELECT m.* FROM message m
               JOIN delivery d ON d.msg_seq = m.seq
               WHERE m.channel_id=? AND m.thread_id=? AND m.role IN ('character','notice')
                 AND d.state IN ('pending','sent','failed')
               GROUP BY m.seq ORDER BY m.seq LIMIT ?""",
            (channel_id, thread_id, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 历史 ----------

    def history_page(self, session_id: str, *, limit: int = 50, before_seq: int | None = None) -> dict[str, Any]:
        params: list[Any] = [session_id]
        clause = ""
        if before_seq is not None:
            clause = "AND seq < ?"
            params.append(before_seq)
        params.append(limit + 1)
        rows = self._conn.execute(
            f"SELECT * FROM message WHERE session_id=? {clause} ORDER BY seq DESC LIMIT ?", params
        ).fetchall()
        rows = list(reversed(rows))
        has_more = len(rows) > limit
        rows = rows[-limit:] if has_more else rows
        return {
            "messages": [_row_to_dict(r) for r in rows],
            "has_more": has_more,
            "next_before_seq": rows[0]["seq"] if rows else None,
        }

    def context_window(self, session_id: str, limit: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM message WHERE session_id=? AND role IN ('user','character') AND state IN ('done','fixed')
               ORDER BY seq DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    # ---------- 作废 ----------

    def void_put(self, channel_id: str, thread_id: str, env_id: str, reason: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO voided(channel_id, thread_id, env_id, reason, at) VALUES(?,?,?,?,?)",
                (channel_id, thread_id, env_id, reason, time.time()),
            )

    def void_has(self, channel_id: str, thread_id: str, env_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM voided WHERE channel_id=? AND thread_id=? AND env_id=?",
            (channel_id, thread_id, env_id),
        ).fetchone()
        return row is not None

    def counts(self) -> dict[str, int]:
        def one(sql: str) -> int:
            return int(self._conn.execute(sql).fetchone()[0])

        return {
            "sessions": one("SELECT COUNT(*) FROM session"),
            "threads": one("SELECT COUNT(*) FROM thread"),
            "channels": one("SELECT COUNT(*) FROM channel_instance"),
            "messages": one("SELECT COUNT(*) FROM message"),
            "instances": one("SELECT COUNT(*) FROM instance"),
        }

    # ---------- 实例（世界设定层） ----------

    def instance_names(self) -> list[str]:
        rows = self._conn.execute("SELECT name FROM instance").fetchall()
        return [str(row["name"]) for row in rows]

    def instance_get(self, instance_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM instance WHERE id=?", (instance_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def instance_by_name(self, name: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM instance WHERE name=?", (name,)).fetchone()
        return _row_to_dict(row) if row else None

    def instance_list(self) -> list[dict[str, Any]]:
        """管理面列表：只有公开元数据，不含世界内部内容（§3.5）。"""
        rows = self._conn.execute(
            """SELECT i.id, i.name, i.original_name, i.package_id, i.data_format, i.rules_version,
                      i.seed, i.moment, i.imported, i.created_at,
                      (SELECT COUNT(*) FROM timeline t WHERE t.instance_id=i.id) AS timelines,
                      (SELECT COUNT(*) FROM session s WHERE s.instance_id=i.id) AS sessions
               FROM instance i ORDER BY i.created_at"""
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def instance_create(self, row: dict[str, Any], *, timelines: list[dict[str, Any]], commits: list[dict[str, Any]]) -> None:
        """原子固化：实例行 + 初始时间线 + 初始提交一起落入，失败不留半个实例（§3.4）。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO instance(id, name, original_name, package_id, data_format, rules_version,
                                        app_version, seed, moment, setting, imported, created_at)
                   VALUES(:id, :name, :original_name, :package_id, :data_format, :rules_version,
                          :app_version, :seed, :moment, :setting, :imported, :created_at)""",
                row,
            )
            for timeline in timelines:
                self._conn.execute(
                    """INSERT INTO timeline(id, instance_id, name, state, source_commit, created_at)
                       VALUES(:id, :instance_id, :name, :state, :source_commit, :created_at)""",
                    timeline,
                )
            for commit in commits:
                self._conn.execute(
                    """INSERT INTO commit_log(id, instance_id, timeline_id, kind, moment, note, created_at)
                       VALUES(:id, :instance_id, :timeline_id, :kind, :moment, :note, :created_at)""",
                    commit,
                )

    def instance_rename(self, instance_id: str, name: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE instance SET name=? WHERE id=?", (name, instance_id))

    def instance_delete(self, instance_id: str) -> None:
        """删除实例及其会话 / 线程绑定 / 消息 / 时间线 / 提交；凭据与通道不连带删除。"""
        with self._lock, self._conn:
            session_ids = [
                row["id"]
                for row in self._conn.execute("SELECT id FROM session WHERE instance_id=?", (instance_id,)).fetchall()
            ]
            for session_id in session_ids:
                self._conn.execute(
                    "DELETE FROM delivery WHERE msg_seq IN (SELECT seq FROM message WHERE session_id=?)",
                    (session_id,),
                )
                self._conn.execute("DELETE FROM message WHERE session_id=?", (session_id,))
                self._conn.execute("DELETE FROM thread WHERE session_id=?", (session_id,))
                self._conn.execute("DELETE FROM session WHERE id=?", (session_id,))
            # 运行层也要清：否则删掉实例会留下孤儿状态（阶段 2 审计发现）
            self._conn.execute(
                "DELETE FROM timeline_clock WHERE timeline_id IN (SELECT id FROM timeline WHERE instance_id=?)",
                (instance_id,),
            )
            self._conn.execute(
                "DELETE FROM rate_command WHERE timeline_id NOT IN (SELECT id FROM timeline)"
            )
            for table in (
                "unit",
                "life_plan",
                "experience",
                "character_join",
                "event",
                "claim",
                "knowledge",
                "effect_state",
                "intent",
                "call_ledger",
                "commit_snapshot",
                "event_draft",
                "pending_event",
                "commit_auto_state",
                "memory",
                "memory_task",
                "memory_embedding",
                "budget_reserve",
                "budget_policy",
                "environment_state",
            ):
                self._conn.execute(f"DELETE FROM {table} WHERE instance_id=?", (instance_id,))
            self._conn.execute(
                "DELETE FROM memory_citation WHERE timeline_id IN (SELECT id FROM timeline WHERE instance_id=?)",
                (instance_id,),
            )
            self._conn.execute("DELETE FROM commit_log WHERE instance_id=?", (instance_id,))
            self._conn.execute("DELETE FROM timeline WHERE instance_id=?", (instance_id,))
            self._conn.execute("DELETE FROM instance WHERE id=?", (instance_id,))

    def timeline_list(self, instance_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM timeline WHERE instance_id=? ORDER BY created_at", (instance_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def instance_sessions(self, instance_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM session WHERE instance_id=? ORDER BY created_at", (instance_id,)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def instance_messages(self, instance_id: str) -> list[dict[str, Any]]:
        """导出用的完整对话：不含投递回执、去重作废记录与通道凭据（§7.1）。"""
        rows = self._conn.execute(
            """SELECT m.session_id, m.role, m.text, m.state, m.binding_version, m.created_at, m.message_id
               FROM message m JOIN session s ON s.id = m.session_id
               WHERE s.instance_id=? ORDER BY m.seq""",
            (instance_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def instance_import_messages(self, session_id: str, messages: list[dict[str, Any]]) -> None:
        """导入的对话行：**重新签发消息标识**（导入=新实例，本地标识一律重映射，§7.3）。"""
        with self._lock, self._conn:
            for item in messages:
                self._conn.execute(
                    """INSERT INTO message(session_id, role, channel_id, thread_id, env_id, binding_version,
                                            binding_token, text, state, message_id, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id,
                        str(item.get("role") or "character"),
                        None,
                        None,
                        None,
                        0,
                        None,
                        str(item.get("text") or ""),
                        str(item.get("state") or "fixed"),
                        f"m-{secrets.token_hex(6)}",
                        float(item.get("created_at") or 0.0),
                    ),
                )

    def timeline_update(self, timeline_id: str, *, name: str | None = None, description: str | None = None) -> None:
        """可命名、可描述（§四 列表只含管理元数据）。"""
        fields: list[str] = []
        args: list[Any] = []
        if name is not None:
            fields.append("name=?")
            args.append(str(name))
        if description is not None:
            fields.append("description=?")
            args.append(str(description))
        if not fields:
            return
        args.append(timeline_id)
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE timeline SET {', '.join(fields)} WHERE id=?", args)

    def timeline_delete(self, instance_id: str, timeline_id: str) -> dict[str, int]:
        """删除一条线：清运行状态、对话与任务；其他线引用的提交数据保留（§四）。"""
        protected = {
            str(row["source_commit"])
            for row in self.timeline_list(instance_id)
            if row["id"] != timeline_id and row.get("source_commit")
        }
        doomed = [row["id"] for row in self.commit_list(instance_id, timeline_id) if row["id"] not in protected]
        counts: dict[str, int] = {}
        with self._lock, self._conn:
            self.timeline_clear_state(timeline_id)
            self.timeline_clear_dialog(timeline_id)
            for table in ("session", "character_join", "rate_command", "budget_reserve",
                          "commit_auto_state", "timeline_clock"):
                cur = self._conn.execute(f"DELETE FROM {table} WHERE timeline_id=?", (timeline_id,))
                counts[table] = int(cur.rowcount or 0)
            if doomed:
                marks = ",".join("?" for _ in doomed)
                cur = self._conn.execute(
                    f"DELETE FROM commit_snapshot WHERE commit_id IN ({marks})", tuple(doomed)
                )
                counts["commit_snapshot"] = int(cur.rowcount or 0)
                cur = self._conn.execute(f"DELETE FROM commit_log WHERE id IN ({marks})", tuple(doomed))
                counts["commit_log"] = int(cur.rowcount or 0)
            counts["commit_kept"] = len(protected & {row["id"] for row in self.commit_list(instance_id)})
            cur = self._conn.execute("DELETE FROM timeline WHERE id=?", (timeline_id,))
            counts["timeline"] = int(cur.rowcount or 0)
        return counts

    def timeline_set_state(self, timeline_id: str, state: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE timeline SET state=? WHERE id=?", (state, timeline_id))

    def commit_add(self, row: dict[str, Any], *, snapshot: dict[str, Any] | None = None) -> dict[str, Any]:
        """记一条提交；给快照就一并落盘（一致快照，§5.1）。"""
        payload = json.dumps(snapshot or {}, ensure_ascii=False) if snapshot is not None else None
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO commit_log(id, instance_id, timeline_id, kind, moment, note, created_at)
                   VALUES(:id, :instance_id, :timeline_id, :kind, :moment, :note, :created_at)""",
                row,
            )
            if payload is not None:
                self._conn.execute(
                    """INSERT INTO commit_snapshot(commit_id, instance_id, payload, size, created_at)
                       VALUES(?,?,?,?,?)""",
                    (row["id"], row["instance_id"], payload, len(payload), time.time()),
                )
        return row

    # ---------- 用户引入事件（§八） ----------

    def draft_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO event_draft(id, instance_id, source_timeline, source_commit, payload,
                                           state, timeline_id, created_world, created_at)
                   VALUES(:id, :instance_id, :source_timeline, :source_commit, :payload,
                          :state, :timeline_id, :created_world, :created_at)
                   ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, state=excluded.state,
                     timeline_id=excluded.timeline_id""",
                row,
            )

    def draft_get(self, draft_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM event_draft WHERE id=?", (draft_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def pending_event_add(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO pending_event(id, instance_id, timeline_id, at_world, payload, state,
                                             note, created_world, created_at)
                   VALUES(:id, :instance_id, :timeline_id, :at_world, :payload, :state,
                          :note, :created_world, :created_at)
                   ON CONFLICT(id) DO NOTHING""",
                row,
            )

    def pending_events_due(self, instance_id: str, timeline_id: str, *, until: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM pending_event WHERE instance_id=? AND timeline_id=? AND state='pending'
               AND at_world<=? ORDER BY at_world, id""",
            (instance_id, timeline_id, int(until)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def pending_event_set(self, event_id: str, *, state: str, note: str = "") -> bool:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE pending_event SET state=?, note=? WHERE id=? AND state='pending'",
                (state, note, event_id),
            )
        return bool(cur.rowcount)

    def commit_state_get(self, timeline_id: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM commit_auto_state WHERE timeline_id=?", (timeline_id,)
        ).fetchone()
        return _row_to_dict(row) if row is not None else {
            "timeline_id": timeline_id, "last_commit_at": 0.0, "last_commit_moment": 0
        }

    def commit_state_set(
        self, timeline_id: str, instance_id: str, *, last_commit_at: float, last_commit_moment: int
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO commit_auto_state(timeline_id, instance_id, last_commit_at, last_commit_moment)
                   VALUES(?,?,?,?)
                   ON CONFLICT(timeline_id) DO UPDATE SET
                     last_commit_at=excluded.last_commit_at,
                     last_commit_moment=excluded.last_commit_moment""",
                (timeline_id, instance_id, float(last_commit_at), int(last_commit_moment)),
            )

    def event_count_since(self, instance_id: str, timeline_id: str, *, since: int) -> int:
        row = self._conn.execute(
            """SELECT COUNT(*) n FROM event WHERE instance_id=? AND timeline_id=? AND world_seconds>?""",
            (instance_id, timeline_id, int(since)),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    def commit_snapshot_get(self, commit_id: str) -> dict[str, Any] | None:
        from .runtime import versioning as versioning_mod

        row = self._conn.execute("SELECT * FROM commit_snapshot WHERE commit_id=?", (commit_id,)).fetchone()
        if row is None:
            return None
        return versioning_mod.parse_snapshot(row["payload"])

    def commit_get(self, commit_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM commit_log WHERE id=?", (commit_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def timeline_add(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO timeline(id, instance_id, name, state, source_commit, created_at)
                   VALUES(:id, :instance_id, :name, :state, :source_commit, :created_at)""",
                row,
            )

    def timeline_clear_state(self, timeline_id: str) -> None:
        """回滚 / 分叉前清空该线的运行状态（保留线身份与时钟）。"""
        for table in (
            "unit", "life_plan", "experience", "claim", "knowledge", "effect_state", "intent",
            "event", "environment_state", "memory", "memory_task", "memory_citation",
            "character_join",   # 跨越补卡点的回滚要让补入角色在本线退出（§七）
            "rate_command",     # 历史里的待生效倍率不是现时控制命令（§七）
            "pending_event",    # 回滚撤销待执行状态及其后果（§八 末条）
        ):
            self._conn.execute(f"DELETE FROM {table} WHERE timeline_id=?", (timeline_id,))
        # 向量表两种形态都清：按线（新）与按 memory 归属（兼容早期没有 timeline_id 的行）
        self._conn.execute("DELETE FROM memory_embedding WHERE timeline_id=?", (timeline_id,))
        self._conn.execute(
            """DELETE FROM memory_embedding WHERE memory_id IN
               (SELECT id FROM memory WHERE timeline_id=?)""",
            (timeline_id,),
        )

    def timeline_void_inflight(self, timeline_id: str, *, reason: str = "rollback") -> int:
        """把该线还在处理中的输入登记作废：迟到的生成结果不得写回（§七）。

        走既有的 voided 表——重绑与回滚共用同一套「这条输入已经不算数」的判定。
        """
        rows = self._conn.execute(
            """SELECT channel_id, thread_id, env_id FROM message
               WHERE role='user' AND env_id IS NOT NULL AND state IN ('queued','processing')
                 AND session_id IN (SELECT id FROM session WHERE timeline_id=?)""",
            (timeline_id,),
        ).fetchall()
        for row in rows:
            self.void_put(str(row["channel_id"] or ""), str(row["thread_id"] or ""), str(row["env_id"]), reason)
        return len(rows)

    def timeline_cancel_undelivered(self, timeline_id: str) -> int:
        """该线已固化但还没投递出去的回复一并取消：不继续发送被回滚的内容（§七）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """UPDATE message SET state='cancelled'
                   WHERE role='character' AND state='fixed'  -- 已固化但还没投递完成
                     AND session_id IN (SELECT id FROM session WHERE timeline_id=?)""",
                (timeline_id,),
            )
            cancelled = int(cur.rowcount or 0)
            self._conn.execute(
                """UPDATE delivery SET state='cancelled'
                   WHERE msg_seq IN (SELECT seq FROM message WHERE session_id IN
                                     (SELECT id FROM session WHERE timeline_id=?))""",
                (timeline_id,),
            )
        return cancelled

    def timeline_clear_dialog(self, timeline_id: str) -> None:
        """该线会话的对话原文（回滚要把被截去的未来对话一并撤掉，§七）。"""
        self._conn.execute(
            """DELETE FROM message WHERE session_id IN (SELECT id FROM session WHERE timeline_id=?)""",
            (timeline_id,),
        )

    def commit_list(self, instance_id: str, timeline_id: str | None = None) -> list[dict[str, Any]]:
        if timeline_id is None:
            rows = self._conn.execute(
                "SELECT * FROM commit_log WHERE instance_id=? ORDER BY created_at", (instance_id,)
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM commit_log WHERE instance_id=? AND timeline_id=? ORDER BY created_at",
                (instance_id, timeline_id),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 运行层：时钟 / 倍率 / 水位 ----------

    def clock_get(self, timeline_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM timeline_clock WHERE timeline_id=?", (timeline_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def clock_put(self, row: dict[str, Any]) -> None:
        payload = {"catching_up": 0, "limited": 0, **row}
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO timeline_clock(timeline_id, base_real, base_world, rate, high_water_real,
                                              anchor_real, processed_world, generation, catching_up, limited)
                   VALUES(:timeline_id, :base_real, :base_world, :rate, :high_water_real,
                          :anchor_real, :processed_world, :generation, :catching_up, :limited)
                   ON CONFLICT(timeline_id) DO UPDATE SET
                     base_real=:base_real, base_world=:base_world, rate=:rate,
                     high_water_real=:high_water_real, anchor_real=:anchor_real,
                     processed_world=:processed_world, generation=:generation,
                     catching_up=:catching_up, limited=:limited""",
                payload,
            )

    def apply_runtime_batch(
        self,
        *,
        timeline_id: str,
        generation: int,
        processed_world: int,
        catching_up: bool,
        limited: bool = False,
        plans: Iterable[dict[str, Any]] = (),
        units: Iterable[dict[str, Any]] = (),
        experiences: Iterable[dict[str, Any]] = (),
        events: Iterable[dict[str, Any]] = (),
        claims: Iterable[dict[str, Any]] = (),
        knowledge: Iterable[dict[str, Any]] = (),
        effects: Iterable[dict[str, Any]] = (),
        intents: Iterable[dict[str, Any]] = (),
        environment: Iterable[dict[str, Any]] = (),
        clear_effects: Iterable[Any] = (),
    ) -> bool:
        """把一批事实转移整体提交（§2.7：一批失败即整批回到批前水位）。

        返回 False 表示世代已变（冻结 / 回滚后的迟到任务），整批不落盘。
        """
        with self._lock, self._conn:  # 单次 with → 一次提交，中途异常整批回滚
            row = self._conn.execute(
                "SELECT generation, processed_world FROM timeline_clock WHERE timeline_id=?", (timeline_id,)
            ).fetchone()
            if row is None or int(row["generation"]) != int(generation):
                return False
            if int(row["processed_world"]) > int(processed_world):
                return False  # 水位只前进：并发的另一批已经先写过
            for plan in plans:
                self._conn.execute(
                    """INSERT OR IGNORE INTO life_plan(id, instance_id, timeline_id, character_id, day_index,
                                                       windows, state, created_world, note)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :day_index,
                              :windows, :state, :created_world, :note)""",
                    plan,
                )
            for unit in units:
                self._conn.execute(
                    """INSERT INTO unit(instance_id, timeline_id, character_id, id, mode, semantic, basis,
                                        confidence, stability, archived, consumed, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :mode, :semantic, :basis,
                              :confidence, :stability, :archived, :consumed, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                         confidence=:confidence, stability=:stability, archived=:archived,
                         consumed=:consumed, updated_world=:updated_world""",
                    unit,
                )
            for item in experiences:
                self._conn.execute(
                    """INSERT OR IGNORE INTO experience(id, instance_id, timeline_id, character_id, world_seconds,
                                                        kind, summary, source_ref, confidence)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :world_seconds,
                              :kind, :summary, :source_ref, :confidence)""",
                    item,
                )
            for row in events:
                self._conn.execute(
                    """INSERT INTO event(instance_id, timeline_id, id, world_seconds, seq, kind, family,
                                        template, source, summary, detail, text_source, effects,
                                        share_value, importance, created_real)
                       VALUES(:instance_id, :timeline_id, :id, :world_seconds, :seq, :kind, :family,
                              :template, :source, :summary, :detail, :text_source, :effects,
                              :share_value, :importance, :created_real)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in claims:
                self._conn.execute(
                    """INSERT INTO claim(instance_id, timeline_id, id, event_id, source_id, text,
                                        audience, earliest_world, credibility)
                       VALUES(:instance_id, :timeline_id, :id, :event_id, :source_id, :text,
                              :audience, :earliest_world, :credibility)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in knowledge:
                self._conn.execute(
                    """INSERT INTO knowledge(instance_id, timeline_id, character_id, id, world_seconds,
                                            kind, target, source, stance, text)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :world_seconds,
                              :kind, :target, :source, :stance, :text)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING""",
                    row,
                )
            for raw in effects:
                row = {"value": None, "family": "", "recovery": "", "cleared_at": None, **raw}
                self._conn.execute(
                    """INSERT INTO effect_state(instance_id, timeline_id, id, event_id, target, kind, family,
                                               value, from_world, expiry, recovery, active, cleared_at)
                       VALUES(:instance_id, :timeline_id, :id, :event_id, :target, :kind, :family,
                              :value, :from_world, :expiry, :recovery, :active, :cleared_at)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in intents:
                self._conn.execute(
                    """INSERT INTO intent(instance_id, timeline_id, character_id, id, object, basis, strength,
                                          window_from, window_to, preconditions, effect, stage, note,
                                          source_world, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :object, :basis, :strength,
                              :window_from, :window_to, :preconditions, :effect, :stage, :note,
                              :source_world, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                         object=:object, basis=:basis, strength=:strength, window_from=:window_from,
                         window_to=:window_to, preconditions=:preconditions, effect=:effect,
                         stage=:stage, note=:note, updated_world=:updated_world""",
                    row,
                )
            for row in environment:
                self._conn.execute(
                    """INSERT INTO environment_state(instance_id, timeline_id, type_id, value, unit, source,
                                                    from_world, expiry, updated_world)
                       VALUES(:instance_id, :timeline_id, :type_id, :value, :unit, :source,
                              :from_world, :expiry, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, type_id) DO UPDATE SET
                         value=:value, unit=:unit, source=:source, from_world=:from_world,
                         expiry=:expiry, updated_world=:updated_world""",
                    row,
                )
            for item in clear_effects:
                effect_id, instance_id_ = item if isinstance(item, tuple) else (item, None)
                self._conn.execute(
                    """UPDATE effect_state SET active=0, cleared_at=?
                       WHERE id=? AND timeline_id=? AND active=1 AND (? IS NULL OR instance_id=?)""",
                    (processed_world, effect_id, timeline_id, instance_id_, instance_id_),
                )
            self._conn.execute(
                """UPDATE timeline_clock SET processed_world=?, catching_up=?, limited=?
                   WHERE timeline_id=? AND generation=?""",
                (processed_world, 1 if catching_up else 0, 1 if limited else 0, timeline_id, generation),
            )
            return True

    # ---------- 运行层快照（导出 / 导入：按已完成水位） ----------

    def runtime_dump(self, instance_id: str, timeline_id: str, *, watermark: int) -> dict[str, Any]:
        def rows(sql: str, *args: Any) -> list[dict[str, Any]]:
            return [_row_to_dict(r) for r in self._conn.execute(sql, args).fetchall()]

        return {
            "watermark": int(watermark),
            "characters": rows(
                "SELECT * FROM character_join WHERE instance_id=? AND timeline_id=? ORDER BY joined_world",
                instance_id,
                timeline_id,
            ),
            "units": rows(
                """SELECT * FROM unit WHERE instance_id=? AND timeline_id=? AND updated_world<=?
                   ORDER BY character_id, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "plans": rows(
                """SELECT * FROM life_plan WHERE instance_id=? AND timeline_id=? AND created_world<=?
                   ORDER BY character_id, day_index""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "memories": rows(
                """SELECT * FROM memory WHERE instance_id=? AND timeline_id=? AND semantic_watermark<=?
                   ORDER BY character_id, learned_world""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "memory_tasks": rows(
                """SELECT * FROM memory_task WHERE instance_id=? AND timeline_id=? AND source_world<=?
                   ORDER BY character_id, source_world""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "experiences": rows(
                """SELECT * FROM experience WHERE instance_id=? AND timeline_id=? AND world_seconds<=?
                   ORDER BY character_id, world_seconds""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "events": rows(
                """SELECT * FROM event WHERE instance_id=? AND timeline_id=? AND world_seconds<=?
                   ORDER BY world_seconds, seq""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "claims": rows(
                """SELECT * FROM claim WHERE instance_id=? AND timeline_id=? AND earliest_world<=?
                   ORDER BY event_id, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "knowledge": rows(
                """SELECT * FROM knowledge WHERE instance_id=? AND timeline_id=? AND world_seconds<=?
                   ORDER BY character_id, world_seconds""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "effects": rows(
                """SELECT * FROM effect_state WHERE instance_id=? AND timeline_id=? AND from_world<=?
                   ORDER BY from_world, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "environment": rows(
                """SELECT * FROM environment_state WHERE instance_id=? AND timeline_id=? AND updated_world<=?
                   ORDER BY type_id""",
                instance_id,
                timeline_id,
                watermark,
            ),
            "intents": rows(
                """SELECT * FROM intent WHERE instance_id=? AND timeline_id=? AND source_world<=?
                   ORDER BY character_id, id""",
                instance_id,
                timeline_id,
                watermark,
            ),
        }

    def runtime_load(
        self, instance_id: str, timeline_id: str, payload: dict[str, Any], *, clear: bool = False
    ) -> int:
        """导入运行层快照：整体一次提交，返回写入的行数。

        `clear=True` 时在同一事务里先撤掉该线现有状态与对话——回滚用它做原子切换（§七）。
        """
        payload = _relabel_payload(payload, instance_id, timeline_id)
        plans = [
            {**row, "created_world": int(row.get("created_world", payload.get("watermark", 0)))}
            for row in payload.get("plans") or []
        ]
        with self._lock, self._conn:
            if clear:
                self.timeline_clear_state(timeline_id)
                self.timeline_clear_dialog(timeline_id)
            for row in payload.get("characters") or []:
                self._conn.execute(
                    """INSERT OR REPLACE INTO character_join(instance_id, timeline_id, character_id, joined_world,
                                                             card, note, acquainted, created_real)
                       VALUES(:instance_id, :timeline_id, :character_id, :joined_world, :card, :note,
                              :acquainted, :created_real)""",
                    row,
                )
            for row in payload.get("units") or []:
                self._conn.execute(
                    """INSERT INTO unit(instance_id, timeline_id, character_id, id, mode, semantic, basis,
                                        confidence, stability, archived, consumed, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :mode, :semantic, :basis,
                              :confidence, :stability, :archived, :consumed, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING""",
                    row,
                )
            for plan in plans:
                self._conn.execute(
                    """INSERT OR IGNORE INTO life_plan(id, instance_id, timeline_id, character_id, day_index,
                                                       windows, state, created_world, note)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :day_index,
                              :windows, :state, :created_world, :note)""",
                    plan,
                )
            for row in payload.get("events") or []:
                self._conn.execute(
                    """INSERT INTO event(instance_id, timeline_id, id, world_seconds, seq, kind, family,
                                        template, source, summary, detail, text_source, effects,
                                        share_value, importance, created_real)
                       VALUES(:instance_id, :timeline_id, :id, :world_seconds, :seq, :kind, :family,
                              :template, :source, :summary, :detail, :text_source, :effects,
                              :share_value, :importance, :created_real)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("claims") or []:
                self._conn.execute(
                    """INSERT INTO claim(instance_id, timeline_id, id, event_id, source_id, text,
                                        audience, earliest_world, credibility)
                       VALUES(:instance_id, :timeline_id, :id, :event_id, :source_id, :text,
                              :audience, :earliest_world, :credibility)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("knowledge") or []:
                self._conn.execute(
                    """INSERT INTO knowledge(instance_id, timeline_id, character_id, id, world_seconds,
                                            kind, target, source, stance, text)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :world_seconds,
                              :kind, :target, :source, :stance, :text)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("effects") or []:
                self._conn.execute(
                    """INSERT INTO effect_state(instance_id, timeline_id, id, event_id, target, kind, family,
                                               value, from_world, expiry, recovery, active, cleared_at)
                       VALUES(:instance_id, :timeline_id, :id, :event_id, :target, :kind, :family,
                              :value, :from_world, :expiry, :recovery, :active, :cleared_at)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("environment") or []:
                self._conn.execute(
                    """INSERT INTO environment_state(instance_id, timeline_id, type_id, value, unit, source,
                                                    from_world, expiry, updated_world)
                       VALUES(:instance_id, :timeline_id, :type_id, :value, :unit, :source,
                              :from_world, :expiry, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, type_id) DO NOTHING""",
                    row,
                )
            for row in payload.get("memories") or []:
                self._conn.execute(
                    """INSERT INTO memory(id, instance_id, timeline_id, character_id, text, kind, sources,
                                          happened_world, learned_world, recorded_world, semantic_watermark,
                                          strength, confidence, state, version, supersedes, superseded_by,
                                          source_key, decay_world)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :text, :kind, :sources,
                              :happened_world, :learned_world, :recorded_world, :semantic_watermark,
                              :strength, :confidence, :state, :version, :supersedes, :superseded_by,
                              :source_key, :decay_world)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("memory_tasks") or []:
                self._conn.execute(
                    """INSERT INTO memory_task(id, instance_id, timeline_id, character_id, source_kind,
                                               source_ref, source_world, created_world, text, state, attempts, note)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :source_kind, :source_ref,
                              :source_world, :created_world, :text, :state, :attempts, :note)
                       ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                    row,
                )
            for row in payload.get("intents") or []:
                self._conn.execute(
                    """INSERT INTO intent(instance_id, timeline_id, character_id, id, object, basis, strength,
                                          window_from, window_to, preconditions, effect, stage, note,
                                          source_world, updated_world)
                       VALUES(:instance_id, :timeline_id, :character_id, :id, :object, :basis, :strength,
                              :window_from, :window_to, :preconditions, :effect, :stage, :note,
                              :source_world, :updated_world)
                       ON CONFLICT(instance_id, timeline_id, character_id, id) DO NOTHING""",
                    row,
                )
            for item in payload.get("experiences") or []:
                self._conn.execute(
                    """INSERT OR IGNORE INTO experience(id, instance_id, timeline_id, character_id, world_seconds,
                                                        kind, summary, source_ref, confidence)
                       VALUES(:id, :instance_id, :timeline_id, :character_id, :world_seconds,
                              :kind, :summary, :source_ref, :confidence)""",
                    item,
                )
        return sum(
            len(payload.get(key) or [])
            for key in (
                "characters",
                "units",
                "plans",
                "experiences",
                "events",
                "claims",
                "knowledge",
                "effects",
                "intents",
                "environment",
            )
        )

    # ---------- 环境事实状态 ----------

    def environment_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO environment_state(instance_id, timeline_id, type_id, value, unit, source,
                                                 from_world, expiry, updated_world)
                   VALUES(:instance_id, :timeline_id, :type_id, :value, :unit, :source,
                          :from_world, :expiry, :updated_world)
                   ON CONFLICT(instance_id, timeline_id, type_id) DO UPDATE SET
                     value=:value, unit=:unit, source=:source, from_world=:from_world,
                     expiry=:expiry, updated_world=:updated_world""",
                row,
            )

    def environment_list(self, instance_id: str, timeline_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM environment_state WHERE instance_id=? AND timeline_id=? ORDER BY type_id",
            (instance_id, timeline_id),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 表述与展开所需的存储 ----------

    def event_get(self, instance_id: str, timeline_id: str, event_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM event WHERE instance_id=? AND timeline_id=? AND id=?",
            (instance_id, timeline_id, event_id),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def event_render_save(
        self, instance_id: str, timeline_id: str, event_id: str, *, detail: str, claims: dict[str, str]
    ) -> None:
        """固化表述（§3.2）：写后不重生成，读取 / 重启 / 换消费者都用它。"""
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE event SET detail=?, text_source='llm'
                   WHERE instance_id=? AND timeline_id=? AND id=?""",
                (detail, instance_id, timeline_id, event_id),
            )
            for source_id, text in claims.items():
                self._conn.execute(
                    """UPDATE claim SET text=? WHERE instance_id=? AND timeline_id=? AND event_id=?
                       AND source_id=? AND derived_from IS NULL""",
                    (text, instance_id, timeline_id, event_id, source_id),
                )

    def claim_derived(
        self, instance_id: str, timeline_id: str, original_id: str
    ) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM claim WHERE instance_id=? AND timeline_id=? AND derived_from=?
               ORDER BY id LIMIT 1""",
            (instance_id, timeline_id, original_id),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def claim_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO claim(instance_id, timeline_id, id, event_id, source_id, text,
                                     audience, earliest_world, credibility, derived_from)
                   VALUES(:instance_id, :timeline_id, :id, :event_id, :source_id, :text,
                          :audience, :earliest_world, :credibility, :derived_from)
                   ON CONFLICT(instance_id, timeline_id, id) DO NOTHING""",
                row,
            )

    def knowledge_holders(self, instance_id: str, timeline_id: str, target: str) -> list[str]:
        """已经掌握该记载的角色（展开只能返回他们原本掌握范围内的内容，§3.4）。"""
        rows = self._conn.execute(
            """SELECT DISTINCT character_id FROM knowledge
               WHERE instance_id=? AND timeline_id=? AND target=?""",
            (instance_id, timeline_id, target),
        ).fetchall()
        return [str(row["character_id"]) for row in rows]

    # ---------- 角色记忆（MEMORY_SPEC） ----------

    def memory_scope(
        self, instance_id: str, timeline_id: str, character_id: str, *, until: int | None = None
    ) -> list[dict[str, Any]]:
        """可召回集合（§5.1 第一步）：实例 + 线 + 角色 + 查询水位；来源状态与归档不豁免隔离。"""
        sql = """SELECT * FROM memory WHERE instance_id=? AND timeline_id=? AND character_id=?
                 AND learned_world<=? ORDER BY learned_world, id"""
        rows = self._conn.execute(
            sql, (instance_id, timeline_id, character_id, int(until) if until is not None else 10**15)
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def memory_add(self, row: dict[str, Any]) -> dict[str, Any] | None:
        """写入一条规范记忆：同源幂等 + 同事实去重（同角色同线内）+ 相反说法建替代关系。"""
        from .runtime import memory as memory_mod

        character_id = str(row["character_id"])
        source_key = str(row.get("source_key") or "")
        if source_key:
            dup = self._conn.execute(
                "SELECT * FROM memory WHERE instance_id=? AND timeline_id=? AND character_id=? AND source_key=?",
                (row["instance_id"], row["timeline_id"], character_id, source_key),
            ).fetchone()
            if dup is not None:
                return None  # 同源重复提取：不重复写、不重复强化
        siblings = [
            item
            for item in self.memory_scope(str(row["instance_id"]), str(row["timeline_id"]), character_id)
            if str(item["kind"]) == str(row["kind"])
        ]
        supersedes = None
        for item in siblings:
            if memory_mod.contradicts(str(item["text"]), str(row["text"])):
                supersedes = str(item["id"])
                break
            if memory_mod.same_fact(str(item["text"]), str(row["text"])):
                return None  # 同事实：合并来源即可，不新增条目
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO memory(id, instance_id, timeline_id, character_id, text, kind, sources,
                                      happened_world, learned_world, recorded_world, semantic_watermark,
                                      strength, confidence, state, version, supersedes, superseded_by, source_key)
                   VALUES(:id,:instance_id,:timeline_id,:character_id,:text,:kind,:sources,
                          :happened_world,:learned_world,:recorded_world,:semantic_watermark,
                          :strength,:confidence,:state,1,:supersedes,NULL,:source_key)""",
                {
                    "sources": json.dumps(row.get("sources") or [], ensure_ascii=False),
                    "state": memory_mod.state_for(float(row.get("strength") or 0.6)),
                    "supersedes": supersedes,
                    **{key: row.get(key) for key in (
                        "id", "instance_id", "timeline_id", "character_id", "text", "kind",
                        "happened_world", "learned_world", "recorded_world", "semantic_watermark",
                        "strength", "confidence", "source_key",
                    )},
                },
            )
            if supersedes:
                # 明确纠正：新条目替代旧的，旧条目保留（历史可查当时认知）
                self._conn.execute(
                    "UPDATE memory SET superseded_by=?, state='archived' WHERE id=?", (row["id"], supersedes)
                )
        saved = self.memory_get(str(row["id"]))
        return saved

    def memory_get(
        self, memory_id: str, *, instance_id: str = "", timeline_id: str = ""
    ) -> dict[str, Any] | None:
        if instance_id and timeline_id:
            row = self._conn.execute(
                "SELECT * FROM memory WHERE instance_id=? AND timeline_id=? AND id=?",
                (instance_id, timeline_id, memory_id),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT * FROM memory WHERE id=?", (memory_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def memory_update_strength(
        self,
        memory_id: str,
        strength: float,
        *,
        state: str | None = None,
        instance_id: str = "",
        timeline_id: str = "",
    ) -> None:
        where = "id=?" if not (instance_id and timeline_id) else "instance_id=? AND timeline_id=? AND id=?"
        args: list[Any] = [float(strength)]
        if state is not None:
            args.append(state)
        args.extend([memory_id] if not (instance_id and timeline_id) else [instance_id, timeline_id, memory_id])
        columns = "strength=?" if state is None else "strength=?, state=?"
        with self._lock, self._conn:
            self._conn.execute(f"UPDATE memory SET {columns} WHERE {where}", args)

    def memory_task_add(self, row: dict[str, Any]) -> bool:
        """登记一条待提取来源（同角色同来源幂等，§4.1）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO memory_task(id, instance_id, timeline_id, character_id, source_kind,
                                                     source_ref, source_world, created_world, text,
                                                     state, attempts, note)
                   VALUES(?,?,?,?,?,?,?,?,?, 'pending', 0, '')""",
                (
                    row["id"], row["instance_id"], row["timeline_id"], row["character_id"],
                    row["source_kind"], row["source_ref"], int(row["source_world"]), int(row["created_world"]),
                    str(row.get("text") or ""),
                ),
            )
        return bool(cur.rowcount)

    def memory_tasks(self, instance_id: str, timeline_id: str, *, state: str = "pending") -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM memory_task WHERE instance_id=? AND timeline_id=? AND state=?
               ORDER BY source_world, id""",
            (instance_id, timeline_id, state),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def memory_task_set(self, task_id: str, *, state: str, note: str = "") -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE memory_task SET state=?, note=?, attempts=attempts+1 WHERE id=?", (state, note, task_id)
            )

    def memory_cite(
        self,
        turn_id: str,
        memory_id: str,
        *,
        instance_id: str = "",
        timeline_id: str,
        character_id: str,
        world_seconds: int,
    ) -> bool:
        """实际被采纳一轮用到的条目才强化：同一轮幂等（§5.3）。"""
        from .runtime import memory as memory_mod

        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO memory_citation(turn_id, memory_id, timeline_id, character_id,
                                                         world_seconds, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (turn_id, memory_id, timeline_id, character_id, int(world_seconds), time.time()),
            )
            if not cur.rowcount:
                return False
            if instance_id:
                row = self._conn.execute(
                    "SELECT strength FROM memory WHERE instance_id=? AND timeline_id=? AND id=?",
                    (instance_id, timeline_id, memory_id),
                ).fetchone()
            else:
                row = self._conn.execute("SELECT strength FROM memory WHERE id=?", (memory_id,)).fetchone()
            if row is None:
                return False
            strength = memory_mod.reinforce(float(row["strength"]))
            self._conn.execute(
                "UPDATE memory SET strength=?, state=? WHERE instance_id=? AND timeline_id=? AND id=?",
                (strength, memory_mod.state_for(strength), instance_id or self._scope_instance(memory_id),
                 timeline_id, memory_id),
            )
        return True

    def timeline_get(self, timeline_id: str) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM timeline WHERE id=?", (timeline_id,)).fetchone()
        return _row_to_dict(row) if row is not None else None

    def memory_vector_scores(
        self,
        instance_id: str,
        timeline_id: str,
        character_id: str,
        query: str,
        *,
        query_vector: list[float] | None = None,
        model: str = "",
    ) -> dict[str, float]:
        """向量召回分数（§5.2）：没有查询向量 / 模型不符 / 维度不符时返回空——调用方退化全文召回。"""
        if not query_vector:
            return {}
        import math

        rows = self._conn.execute(
            """SELECT e.memory_id, e.vector, e.dim FROM memory_embedding e
               JOIN memory m ON m.id = e.memory_id
               WHERE m.instance_id=? AND m.timeline_id=? AND m.character_id=? AND e.dim=? AND e.model=?""",
            (instance_id, timeline_id, character_id, len(query_vector), str(model or "")),
        ).fetchall()
        scores: dict[str, float] = {}
        norm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
        for row in rows:
            blob = row["vector"]
            dim = int(row["dim"])
            if len(blob) != dim * 4:
                continue
            values = struct.unpack(f"<{dim}f", blob)
            other = math.sqrt(sum(value * value for value in values)) or 1.0
            dot = sum(a * b for a, b in zip(query_vector, values))
            scores[str(row["memory_id"])] = dot / (norm * other)
        return scores

    def memory_decay(self, *, timeline_id: str, to_world: int, day_seconds: int, per_day: float) -> int:
        """按世界时长衰减（§六）：从每条的 decay_world 推到 to_world；同一水位重跑无副作用。"""
        from .runtime import memory as memory_mod

        rows = self._conn.execute(
            "SELECT id, strength, decay_world, state FROM memory WHERE timeline_id=? AND state<>'archived'",
            (timeline_id,),
        ).fetchall()
        changed = 0
        with self._lock, self._conn:
            for row in rows:
                start = int(row["decay_world"] or 0)
                if int(to_world) <= start:
                    continue
                strength = memory_mod.decayed_strength(
                    float(row["strength"]), from_world=start, to_world=int(to_world),
                    day_seconds=day_seconds, per_day=per_day,
                )
                self._conn.execute(
                    "UPDATE memory SET strength=?, state=?, decay_world=? WHERE id=?",
                    (strength, memory_mod.state_for(strength), int(to_world), row["id"]),
                )
                changed += 1
        return changed

    def _scope_instance(self, memory_id: str) -> str:
        row = self._conn.execute("SELECT instance_id FROM memory WHERE id=?", (memory_id,)).fetchone()
        return str(row["instance_id"]) if row is not None else ""

    def memory_embedding_put(
        self,
        memory_id: str,
        *,
        instance_id: str,
        timeline_id: str = "",
        model: str,
        vector: list[float],
        content_hash: str,
    ) -> None:
        """落一条向量（带模型指纹与源文本版本，§5.2）。"""
        from .runtime import embedding as embedding_mod

        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO memory_embedding(memory_id, instance_id, timeline_id, model, dim, vector,
                                               source_version, created_at)
                   VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(instance_id, timeline_id, memory_id) DO UPDATE SET
                     model=excluded.model, dim=excluded.dim, vector=excluded.vector,
                     source_version=excluded.source_version, created_at=excluded.created_at""",
                (
                    memory_id,
                    instance_id,
                    str(timeline_id or ""),
                    str(model),
                    len(vector),
                    embedding_mod.pack(vector),
                    str(content_hash),
                    time.time(),
                ),
            )

    def memory_missing_embeddings(
        self, instance_id: str, timeline_id: str, *, model: str, limit: int = 64
    ) -> list[dict[str, Any]]:
        """缺向量或指纹不符的条目（模型 / 维度变了就重建，旧向量不再参与召回）。"""
        rows = self._conn.execute(
            """SELECT m.id, m.text, m.character_id, e.model AS embed_model, e.source_version
               FROM memory m LEFT JOIN memory_embedding e ON e.memory_id = m.id
               WHERE m.instance_id=? AND m.timeline_id=?
                 AND (e.memory_id IS NULL OR e.model <> ?)
               ORDER BY m.learned_world, m.id LIMIT ?""",
            (instance_id, timeline_id, str(model), int(limit)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def memory_count(self, instance_id: str, timeline_id: str, character_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) n FROM memory WHERE instance_id=? AND timeline_id=? AND character_id=?",
            (instance_id, timeline_id, character_id),
        ).fetchone()
        return int(row["n"] if row is not None else 0)

    # ---------- 三层预算（§2.8） ----------

    def budget_usage(self, instance_id: str, *, bucket: int, timeline_id: str | None = None) -> dict[str, Any]:
        """已消耗 + 在途预占（按实例 / 线 / 任务汇总），预占未结算前也算占用。"""
        cond, args = "instance_id=? AND bucket=?", [instance_id, int(bucket)]
        if timeline_id:
            cond += " AND timeline_id=?"
            args.append(timeline_id)
        usage: dict[str, Any] = {"instance": 0, "timelines": {}, "tasks": {}}
        for row in self._conn.execute(
            f"""SELECT timeline_id, task, SUM(tokens) calls_tokens FROM call_ledger
                WHERE {cond} GROUP BY timeline_id, task""",
            args,
        ):
            tokens = int(row["calls_tokens"] or 0)
            usage["instance"] += tokens
            usage["timelines"][row["timeline_id"]] = usage["timelines"].get(row["timeline_id"], 0) + tokens
            usage["tasks"][f"{row['timeline_id']}|{row['task']}"] = tokens
        for row in self._conn.execute(
            f"""SELECT timeline_id, task, SUM(tokens_est) held FROM budget_reserve
                WHERE {cond} AND state='held' GROUP BY timeline_id, task""",
            args,
        ):
            held = int(row["held"] or 0)
            usage["instance"] += held
            usage["timelines"][row["timeline_id"]] = usage["timelines"].get(row["timeline_id"], 0) + held
            key = f"{row['timeline_id']}|{row['task']}"
            usage["tasks"][key] = usage["tasks"].get(key, 0) + held
        return usage

    def budget_reserve(
        self,
        *,
        instance_id: str,
        timeline_id: str,
        task: str,
        bucket: int,
        priority: int,
        tokens_est: int,
        limits: dict[str, int],
        reserved_for_higher: dict[int, int] | None = None,
    ) -> dict[str, Any] | None:
        """发起前原子预占；三层任何一层不够就拒绝（返回 None）。"""
        from .runtime.budget import decide

        with self._lock, self._conn:
            usage = self.budget_usage(instance_id, bucket=bucket)
            verdict = decide(
                usage=usage,
                timeline_id=timeline_id,
                task=task,
                priority=priority,
                tokens_est=max(0, int(tokens_est)),
                limits=limits,
                reserved_for_higher=reserved_for_higher or {},
            )
            if not verdict["ok"]:
                return {"ok": False, **verdict}
            ident = f"rs-{os.urandom(6).hex()}"
            self._conn.execute(
                """INSERT INTO budget_reserve(id, instance_id, timeline_id, task, bucket, priority,
                                             tokens_est, tokens_actual, state, outcome_kind, created_at)
                   VALUES(?,?,?,?,?,?,?,0,'held','',?)""",
                (ident, instance_id, timeline_id, task, int(bucket), int(priority), int(tokens_est), time.time()),
            )
        return {"ok": True, "id": ident, **verdict}

    def budget_settle(
        self, reserve_id: str, *, tokens_actual: int = 0, outcome: str = "ok", calls: int = 1
    ) -> dict[str, Any] | None:
        """结算：登记真实消耗（成功 / 失败 / 超时都算），释放未使用的预占。"""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM budget_reserve WHERE id=?", (reserve_id,)).fetchone()
            if row is None or row["state"] != "held":
                return None
            self._conn.execute(
                "UPDATE budget_reserve SET state='settled', outcome_kind=?, tokens_actual=? WHERE id=?",
                (str(outcome), max(0, int(tokens_actual)), reserve_id),
            )
            # 同一事务内登记消耗（不调 call_ledger_add，避免嵌套事务）
            self._conn.execute(
                """INSERT INTO call_ledger(instance_id, timeline_id, task, bucket, calls, tokens)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(instance_id, timeline_id, task, bucket) DO UPDATE SET
                     calls = calls + excluded.calls, tokens = tokens + excluded.tokens""",
                (
                    row["instance_id"],
                    row["timeline_id"],
                    row["task"],
                    int(row["bucket"]),
                    max(1, int(calls)),
                    max(0, int(tokens_actual)),
                ),
            )
        return {
            "id": reserve_id,
            "task": row["task"],
            "calls": max(1, int(calls)),
            "tokens_actual": int(tokens_actual),
            "outcome": outcome,
        }

    def budget_release(self, reserve_id: str) -> bool:
        """失效任务释放未使用的预占（已发生的外部调用不回滚）。"""
        with self._lock, self._conn:
            cur = self._conn.execute(
                "UPDATE budget_reserve SET state='released', outcome_kind='cancelled' WHERE id=? AND state='held'", (reserve_id,)
            )
        return bool(cur.rowcount)

    def budget_policy_get(self, instance_id: str) -> dict[str, Any]:
        row = self._conn.execute("SELECT * FROM budget_policy WHERE instance_id=?", (instance_id,)).fetchone()
        if row is None:
            return {
                "instance_id": instance_id,
                "paused_tasks": [],
                "instance_tokens_per_day": None,
                "timeline_tokens_per_day": None,
                "task_tokens_per_day": None,
            }
        data = _row_to_dict(row)
        data["paused_tasks"] = json.loads(data.get("paused_tasks") or "[]")
        return data

    def budget_policy_set(self, instance_id: str, **fields: Any) -> dict[str, Any]:
        current = self.budget_policy_get(instance_id)
        current.update(fields)
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO budget_policy(instance_id, paused_tasks, instance_tokens_per_day,
                                            timeline_tokens_per_day, task_tokens_per_day, updated_at)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(instance_id) DO UPDATE SET
                     paused_tasks=excluded.paused_tasks,
                     instance_tokens_per_day=excluded.instance_tokens_per_day,
                     timeline_tokens_per_day=excluded.timeline_tokens_per_day,
                     task_tokens_per_day=excluded.task_tokens_per_day,
                     updated_at=excluded.updated_at""",
                (
                    instance_id,
                    json.dumps(sorted(current.get("paused_tasks") or []), ensure_ascii=False),
                    current.get("instance_tokens_per_day"),
                    current.get("timeline_tokens_per_day"),
                    current.get("task_tokens_per_day"),
                    time.time(),
                ),
            )
        return self.budget_policy_get(instance_id)

    def budget_rows(self, instance_id: str, *, bucket: int) -> list[dict[str, Any]]:
        """非内容性的预算账目（来源 / 阶段 / 调用次数 / token 量级 / 结果类别，无正文）。"""
        rows = self._conn.execute(
            """SELECT timeline_id, task, bucket, calls, tokens FROM call_ledger
               WHERE instance_id=? AND bucket=? ORDER BY task""",
            (instance_id, int(bucket)),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def call_ledger_add(
        self, instance_id: str, timeline_id: str, task: str, *, bucket: int, calls: int = 1, tokens: int = 0
    ) -> int:
        """调用账本（§2.8）：按实例 / 线 / 任务 / 现实日窗口记次数与量级，不记正文。"""
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO call_ledger(instance_id, timeline_id, task, bucket, calls, tokens)
                   VALUES(?,?,?,?,?,?)
                   ON CONFLICT(instance_id, timeline_id, task, bucket) DO UPDATE SET
                     calls = calls + excluded.calls, tokens = tokens + excluded.tokens""",
                (instance_id, timeline_id, task, int(bucket), int(calls), int(tokens)),
            )
            row = self._conn.execute(
                """SELECT calls FROM call_ledger WHERE instance_id=? AND timeline_id=? AND task=? AND bucket=?""",
                (instance_id, timeline_id, task, int(bucket)),
            ).fetchone()
        return int(row["calls"]) if row else 0

    def call_ledger_get(self, instance_id: str, timeline_id: str, task: str, *, bucket: int) -> int:
        row = self._conn.execute(
            """SELECT calls FROM call_ledger WHERE instance_id=? AND timeline_id=? AND task=? AND bucket=?""",
            (instance_id, timeline_id, task, int(bucket)),
        ).fetchone()
        return int(row["calls"]) if row else 0

    # ---------- 角色打算 ----------

    def intent_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO intent(instance_id, timeline_id, character_id, id, object, basis, strength,
                                      window_from, window_to, preconditions, effect, stage, note,
                                      source_world, updated_world)
                   VALUES(:instance_id, :timeline_id, :character_id, :id, :object, :basis, :strength,
                          :window_from, :window_to, :preconditions, :effect, :stage, :note,
                          :source_world, :updated_world)
                   ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                     object=:object, basis=:basis, strength=:strength, window_from=:window_from,
                     window_to=:window_to, preconditions=:preconditions, effect=:effect,
                     stage=:stage, note=:note, updated_world=:updated_world""",
                row,
            )

    def intent_list(
        self, instance_id: str, timeline_id: str, character_id: str | None = None
    ) -> list[dict[str, Any]]:
        if character_id is None:
            rows = self._conn.execute(
                """SELECT * FROM intent WHERE instance_id=? AND timeline_id=?
                   ORDER BY character_id, id""",
                (instance_id, timeline_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM intent WHERE instance_id=? AND timeline_id=? AND character_id=?
                   ORDER BY id""",
                (instance_id, timeline_id, character_id),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    # ---------- 事件 / 说法 / 获知 / 效果 ----------

    def event_window(
        self, instance_id: str, timeline_id: str, *, until: int, limit: int = 50
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM event WHERE instance_id=? AND timeline_id=? AND world_seconds<=?
               ORDER BY world_seconds DESC, seq DESC LIMIT ?""",
            (instance_id, timeline_id, until, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    def event_ids(self, instance_id: str, timeline_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT id FROM event WHERE instance_id=? AND timeline_id=?", (instance_id, timeline_id)
        ).fetchall()
        return {str(r["id"]) for r in rows}

    def claim_list(
        self, instance_id: str, timeline_id: str, *, event_id: str | None = None
    ) -> list[dict[str, Any]]:
        if event_id is None:
            rows = self._conn.execute(
                "SELECT * FROM claim WHERE instance_id=? AND timeline_id=? ORDER BY earliest_world, id",
                (instance_id, timeline_id),
            ).fetchall()
        else:
            rows = self._conn.execute(
                """SELECT * FROM claim WHERE instance_id=? AND timeline_id=? AND event_id=?
                   ORDER BY earliest_world, id""",
                (instance_id, timeline_id, event_id),
            ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def knowledge_window(
        self, instance_id: str, timeline_id: str, character_id: str, *, until: int, limit: int = 40
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM knowledge WHERE instance_id=? AND timeline_id=? AND character_id=?
               AND world_seconds<=? ORDER BY world_seconds DESC, id DESC LIMIT ?""",
            (instance_id, timeline_id, character_id, until, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    def knowledge_ids(self, instance_id: str, timeline_id: str, character_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT id FROM knowledge WHERE instance_id=? AND timeline_id=? AND character_id=?",
            (instance_id, timeline_id, character_id),
        ).fetchall()
        return {str(r["id"]) for r in rows}

    def effect_active_ids(self, instance_id: str, timeline_id: str) -> set[str]:
        rows = self._conn.execute(
            "SELECT id FROM effect_state WHERE instance_id=? AND timeline_id=? AND active=1",
            (instance_id, timeline_id),
        ).fetchall()
        return {str(r["id"]) for r in rows}

    def effect_window(
        self, instance_id: str, timeline_id: str, *, until: int, targets: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        """仍有效的后果（§六）：过期或已解除的不再参与因果。"""
        rows = self._conn.execute(
            """SELECT * FROM effect_state WHERE instance_id=? AND timeline_id=? AND active=1
               AND from_world<=? ORDER BY from_world, id""",
            (instance_id, timeline_id, until),
        ).fetchall()
        wanted = {str(item) for item in targets} if targets else None
        out = []
        for row in rows:
            item = _row_to_dict(row)
            if wanted is not None and str(item.get("target")) not in wanted:
                continue
            out.append(item)
        return out

    # ---------- 补卡 ----------

    def character_join_add(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO character_join(instance_id, timeline_id, character_id, joined_world,
                                              card, note, acquainted, created_real)
                   VALUES(:instance_id, :timeline_id, :character_id, :joined_world, :card, :note,
                          :acquainted, :created_real)
                   ON CONFLICT(instance_id, timeline_id, character_id) DO UPDATE SET
                     joined_world=:joined_world, card=:card, note=:note, acquainted=:acquainted""",
                row,
            )

    def character_join_list(
        self, instance_id: str, timeline_id: str, *, until: int | None = None
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM character_join WHERE instance_id=? AND timeline_id=?"
        args: list[Any] = [instance_id, timeline_id]
        if until is not None:
            sql += " AND joined_world<=?"
            args.append(int(until))
        rows = self._conn.execute(sql + " ORDER BY joined_world, character_id", args).fetchall()
        return [_row_to_dict(r) for r in rows]

    def clock_set_processed(self, timeline_id: str, processed_world: int, *, generation: int | None = None) -> bool:
        """推进水位：世代不符即拒（迟到任务不得写回旧水位，§2.6/§7.1）。"""
        with self._lock, self._conn:
            if generation is None:
                cursor = self._conn.execute(
                    "UPDATE timeline_clock SET processed_world=? WHERE timeline_id=? AND processed_world<?",
                    (processed_world, timeline_id, processed_world),
                )
            else:
                cursor = self._conn.execute(
                    """UPDATE timeline_clock SET processed_world=?
                       WHERE timeline_id=? AND generation=? AND processed_world<?""",
                    (processed_world, timeline_id, generation, processed_world),
                )
            return cursor.rowcount > 0

    def rate_add(
        self, timeline_id: str, *, input_real: float, effective_real: int, rate: int, seq: int
    ) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """INSERT INTO rate_command(timeline_id, input_real, effective_real, rate, seq, state)
                   VALUES(?,?,?,?,?, 'pending')""",
                (timeline_id, input_real, effective_real, rate, seq),
            )
            return int(cursor.lastrowid)

    def rate_pending(self, timeline_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM rate_command WHERE timeline_id=? AND state='pending' ORDER BY effective_real, seq",
            (timeline_id,),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def rate_apply(self, ids: list[int]) -> None:
        if not ids:
            return
        with self._lock, self._conn:
            self._conn.executemany("UPDATE rate_command SET state='applied' WHERE id=?", [(i,) for i in ids])

    def rate_cancel_pending(self, timeline_id: str) -> int:
        with self._lock, self._conn:
            cursor = self._conn.execute(
                "UPDATE rate_command SET state='cancelled' WHERE timeline_id=? AND state='pending'",
                (timeline_id,),
            )
            return int(cursor.rowcount)

    # ---------- 运行层：角色状态 ----------

    def unit_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO unit(id, instance_id, timeline_id, character_id, mode, semantic, basis,
                                    confidence, stability, archived, consumed, updated_world)
                   VALUES(:id, :instance_id, :timeline_id, :character_id, :mode, :semantic, :basis,
                          :confidence, :stability, :archived, :consumed, :updated_world)
                   ON CONFLICT(instance_id, timeline_id, character_id, id) DO UPDATE SET
                     confidence=:confidence, stability=:stability, archived=:archived,
                     consumed=:consumed, updated_world=:updated_world""",
                row,
            )

    def unit_list(self, instance_id: str, timeline_id: str, character_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM unit WHERE instance_id=? AND timeline_id=? AND character_id=?
               ORDER BY archived, id""",
            (instance_id, timeline_id, character_id),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def plan_put(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO life_plan(id, instance_id, timeline_id, character_id, day_index,
                                                   windows, state, created_world, note)
                   VALUES(:id, :instance_id, :timeline_id, :character_id, :day_index,
                          :windows, :state, :created_world, :note)""",
                row,
            )

    def plan_get(self, instance_id: str, timeline_id: str, character_id: str, day_index: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM life_plan WHERE instance_id=? AND timeline_id=? AND character_id=? AND day_index=?""",
            (instance_id, timeline_id, character_id, day_index),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def plan_latest(self, instance_id: str, timeline_id: str, character_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            """SELECT * FROM life_plan WHERE instance_id=? AND timeline_id=? AND character_id=?
               ORDER BY day_index DESC LIMIT 1""",
            (instance_id, timeline_id, character_id),
        ).fetchone()
        return _row_to_dict(row) if row else None

    def experience_add(self, row: dict[str, Any]) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT OR IGNORE INTO experience(id, instance_id, timeline_id, character_id, world_seconds,
                                                    kind, summary, source_ref, confidence)
                   VALUES(:id, :instance_id, :timeline_id, :character_id, :world_seconds,
                          :kind, :summary, :source_ref, :confidence)""",
                row,
            )

    def experience_window(
        self, instance_id: str, timeline_id: str, character_id: str, *, until: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT * FROM experience WHERE instance_id=? AND timeline_id=? AND character_id=?
               AND world_seconds<=? ORDER BY world_seconds DESC LIMIT ?""",
            (instance_id, timeline_id, character_id, until, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in reversed(rows)]

    def experience_added_since(
        self, instance_id: str, timeline_id: str, *, since: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        """水位推进期间新增的经历（按角色分组由调用方处理）。"""
        rows = self._conn.execute(
            """SELECT * FROM experience WHERE instance_id=? AND timeline_id=? AND world_seconds>?
               ORDER BY world_seconds LIMIT ?""",
            (instance_id, timeline_id, since, limit),
        ).fetchall()
        return [_row_to_dict(r) for r in rows]

    def write_probe(self) -> None:
        """写盘自检：不可写时抛 sqlite3.Error（调用方据此进入 persistence_blocked）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('write_probe', ?)", (str(time.time()),)
            )
