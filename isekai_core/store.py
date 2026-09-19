"""SQLite 持久化（单一写入者）。

- WAL + 单连接 + 进程内锁；核心只有一个写库进程（DESKTOP_SPEC §2.1）。
- message 表同时承担入站去重与历史真值；delivery 表按批次记录投递状态。
- 去重作废记录（voided）不随世界历史回滚倒退，也不随实例导出（CHANNEL_PLUGIN_SPEC §2.2）。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from .version import DATA_FORMAT_VERSION

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
  state TEXT NOT NULL DEFAULT 'frozen',   -- frozen|active
  source_commit TEXT,
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
"""


class EnvelopeConflict(Exception):
    """同键异文：不能静默丢弃或再执行。"""

    def __init__(self, existing: dict[str, Any]) -> None:
        super().__init__("same envelope id with different text")
        self.existing = existing


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

    def ensure_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            row = self._conn.execute("SELECT value FROM meta WHERE key='data_format'").fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO meta(key, value) VALUES('data_format', ?)", (DATA_FORMAT_VERSION,)
                )
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('schema', ?)", (str(SCHEMA_VERSION),)
            )
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
                        item.get("message_id"),
                        float(item.get("created_at") or 0.0),
                    ),
                )

    def timeline_set_state(self, timeline_id: str, state: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE timeline SET state=? WHERE id=?", (state, timeline_id))

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

    def write_probe(self) -> None:
        """写盘自检：不可写时抛 sqlite3.Error（调用方据此进入 persistence_blocked）。"""
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES('write_probe', ?)", (str(time.time()),)
            )
