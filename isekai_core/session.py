"""会话核心（阶段 0 最小实现）。

范围：持久会话身份、绑定令牌核验后的接受与去重、生成链路（取上下文 → 调用 LLM →
校验 → 固化 → 投递）、失败与重试、投递回执。世界 / 认知 / 记忆引擎在后续阶段接入。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from . import ump
from .config import Config
from .llm import LLMError
from .log import get_logger
from .store import EnvelopeConflict, Store
from .ump import Envelope, Err, Stage, UmpError

log = get_logger("isekai.session")

#: 单批发送的现实上限：通道端不读数据时不能把轮次拖死（发送结果按未知处理）
SEND_TIMEOUT_S = 15.0

#: (channel_id, thread_id, envelope) -> 是否已发出
Deliver = Callable[[str, str, dict[str, Any]], Awaitable[bool]]


def split_parts(text: str, max_len: int) -> list[str]:
    """按行长贪心切分：不裁剪正文尾部（CHANNEL_PLUGIN_SPEC §2.4）。"""
    text = text.replace("\r\n", "\n").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    parts: list[str] = []
    buf = ""
    for line in text.split("\n"):
        piece = line + "\n"
        while len(piece) > max_len:
            room = max_len - len(buf)
            if room > 0:
                buf += piece[:room]
                piece = piece[room:]
            parts.append(buf)
            buf = ""
        if len(buf) + len(piece) <= max_len:
            buf += piece
        else:
            parts.append(buf)
            buf = piece
    if buf:
        parts.append(buf)
    cleaned = [p.rstrip("\n") for p in parts]
    return [p for p in cleaned if p] or [text[:max_len]]


def plan_batches(parts: list[str], max_parts: int) -> list[list[str]]:
    """分段计划 → 批次计划：每批最多 max_parts 段，批次有序。"""
    return [parts[i : i + max_parts] for i in range(0, len(parts), max_parts)]


class SessionService:
    def __init__(
        self, *, store: Store, cfg: Config, llm: Any, deliver: Deliver, runtime: Any = None
    ) -> None:
        self.store = store
        self.cfg = cfg
        self.llm = llm
        self.deliver = deliver
        #: 世界运行层（阶段 2 起）：真实实例的会话用它构造扮演定义
        self.runtime = runtime
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task[Any]] = set()
        #: 本轮注入的记忆标识（§5.3：只有实际被采纳的一轮才强化）
        self._recalled: list[str] = []

    # ---------- 入站 ----------

    async def accept(self, *, channel_id: str, thread_row: dict[str, Any], env: Envelope) -> dict[str, Any]:
        """接受一条用户消息；同键异文报冲突，同键同文返回既有状态 / 结果。"""
        thread_id = env.thread_id or ""
        env_id = env.id
        text = env.payload["text"].strip()

        if self.store.void_has(channel_id, thread_id, env_id):
            raise UmpError(
                Err.VOIDED, "该输入已因回滚 / 重绑作废", retryable=False, ref=env_id, stage=Stage.RECEIVE
            )

        try:
            row, created = self.store.inbound_put(
                session_id=thread_row["session_id"],
                channel_id=channel_id,
                thread_id=thread_id,
                env_id=env_id,
                binding_version=thread_row["binding_version"],
                text=text,
            )
        except EnvelopeConflict as exc:
            raise UmpError(
                Err.CONFLICT,
                "同一信封标识但正文不同，拒绝执行",
                retryable=False,
                ref=env_id,
                stage=Stage.RECEIVE,
            ) from exc

        if created:
            self._schedule(row["seq"])
            return {"ref": env_id, "state": "queued", "message_id": None}

        # 重复发送：只查询 / 恢复原处理，不重新生成
        if row["state"] == "done" and row["message_id"]:
            fixed = self.store.outbound_by_message_id(row["message_id"])
            if fixed is not None:
                await self._send_batches(fixed)
        return {"ref": env_id, "state": row["state"], "message_id": row["message_id"]}

    async def retry(self, *, channel_id: str, thread_id: str, ref: str, kind: str | None = None) -> dict[str, Any]:
        """显式重试：入站生成失败恢复同一轮次；投递失败 / 未知只重发固化结果。"""
        if self.store.void_has(channel_id, thread_id, ref):
            raise UmpError(Err.VOIDED, "该请求已作废，不能重放", retryable=False, ref=ref, stage=Stage.RECEIVE)

        outbound = self.store.outbound_by_message_id(ref) if kind == "outbound" else None
        if outbound is None:
            outbound = self.store.inbound_find(channel_id, thread_id, ref)
        if outbound is None:
            raise UmpError(Err.NOT_FOUND, "找不到对应的输入或回复", retryable=False, ref=ref, stage=Stage.RECEIVE)

        if outbound["role"] in ("character", "notice"):
            if outbound["channel_id"] != channel_id:
                raise UmpError(Err.NOT_FOUND, "该回复不属于当前通道", retryable=False, ref=ref, stage=Stage.RECEIVE)
            await self._send_batches(outbound)
            rollup = self.store.delivery_rollup(outbound["seq"])
            return {"ref": ref, "state": rollup, "message_id": outbound["message_id"]}

        state = outbound["state"]
        if state in ("queued", "processing"):
            return {"ref": ref, "state": state, "message_id": outbound["message_id"]}
        if state == "done":
            fixed = self.store.outbound_by_message_id(outbound["message_id"] or "")
            if fixed is not None:
                await self._send_batches(fixed)
            return {"ref": ref, "state": "done", "message_id": outbound["message_id"]}
        if state == "cancelled":
            raise UmpError(Err.VOIDED, "该输入已作废", retryable=False, ref=ref, stage=Stage.RECEIVE)
        # failed：恢复同一逻辑轮次的新尝试
        self.store.inbound_set_state(outbound["seq"], "queued", error_code=None)
        self._schedule(outbound["seq"])
        return {"ref": ref, "state": "processing", "message_id": None}

    def _schedule(self, seq: int) -> None:
        task = asyncio.create_task(self._run(seq))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---------- 生成 ----------

    async def _run(self, seq: int) -> None:
        row = self.store.message_get(seq)
        if row is None:
            return
        lock = self._locks.setdefault(row["session_id"], asyncio.Lock())
        async with lock:  # 同一会话（三元组）串行处理
            row = self.store.message_get(seq)
            if row is None or row["state"] != "queued":
                return
            await self._generate(row)

    async def _generate(self, row: dict[str, Any]) -> None:
        seq = row["seq"]
        started = time.monotonic()
        self.store.inbound_set_state(seq, "processing")
        await self._status(row, "thinking")
        try:
            messages, recalled = self._build_messages(row)
            self._recalled = list(recalled)
            text = await self.llm.chat(messages)
        except LLMError as exc:
            log.warning(
                "generation failed seq=%s stage=%s code=%s elapsed=%.1fs",
                seq,
                Stage.GENERATE,
                exc.code,
                time.monotonic() - started,
            )
            self.store.inbound_set_state(seq, "failed", error_code=exc.code)
            await self._status(row, "idle")
            await self._error(row, UmpError(exc.code, exc.message, retryable=exc.retryable, stage=Stage.GENERATE))
            return
        except Exception:  # 兜底：异常不得被当成成功文本
            log.exception("generation crashed seq=%s", seq)
            self.store.inbound_set_state(seq, "failed", error_code=Err.INTERNAL)
            await self._status(row, "idle")
            await self._error(row, UmpError(Err.INTERNAL, "生成失败", retryable=True, stage=Stage.GENERATE))
            return

        parts = split_parts(text, self._limits(row["channel_id"])[0])
        if not parts:
            self.store.inbound_set_state(seq, "failed", error_code="empty_completion")
            await self._status(row, "idle")
            await self._error(
                row, UmpError(Err.GENERATION_FAILED, "空回复", retryable=True, stage=Stage.GENERATE)
            )
            return

        thread = self.store.thread_get(row["channel_id"], row["thread_id"])
        if thread is None or thread["binding_version"] != row["binding_version"]:
            # 提交前核对绑定版本：重绑后迟到结果不写入、不投递
            log.info("turn dropped: binding changed seq=%s", seq)
            self.store.inbound_set_state(seq, "cancelled", error_code=Err.BINDING_EXPIRED)
            await self._status(row, "idle")
            return

        message_id = ump.new_id("m")
        msg = self.store.commit_turn(
            inbound_seq=seq,
            outbound={
                "session_id": row["session_id"],
                "message_id": message_id,
                "reply_to": row["env_id"],
                "covers": [row["env_id"]],
                "batches": plan_batches(parts, self._limits(row["channel_id"])[1]),
                "target_channel": row["channel_id"],
                "target_thread": row["thread_id"],
                "binding_version": row["binding_version"],
                "binding_token": thread["binding_token"],
            },
        )
        self._settle_memory(row, message_id=str(message_id), reply_text=chr(10).join(parts))
        await self._send_batches(msg)
        await self._status(row, "idle")

    def _settle_memory(self, row: dict[str, Any], *, message_id: str, reply_text: str) -> None:
        """已固化回复的后续记账（§4.1 / §5.3）：登记来源待提取 + 本轮实际用到的记忆强化。

        失败不影响投递：记忆是派生数据，不能反向拖住已接受的回复。
        """
        runtime = getattr(self, "runtime", None)
        session = self.store.session_get(row["session_id"])
        if runtime is None or session is None:
            return
        instance_id = str(session.get("instance_id") or "")
        timeline_id = str(session.get("timeline_id") or "")
        character_id = str(session.get("character_id") or "")
        if not (instance_id and timeline_id and character_id):
            return
        try:
            world_seconds = runtime.world_moment(instance_id, timeline_id)
            runtime.cite_memories(
                instance_id, timeline_id, character_id,
                turn_id=str(message_id), memory_ids=list(self._recalled), world_seconds=world_seconds,
            )
            runtime.queue_dialog_turn(
                instance_id, timeline_id, character_id,
                world_seconds=world_seconds,
                user_ref=str(row.get("env_id") or ""),
                user_text=str(row.get("text") or ""),
                reply_message_id=str(message_id),
                reply_text=str(reply_text or ""),
            )
        except Exception:
            log.exception("memory settle failed session=%s", session["id"])

    def _limits(self, channel_id: str) -> tuple[int, int]:
        """按通道协商结果取分段限额；未协商多段时每批一段。"""
        channel = self.store.channel_get(channel_id) if channel_id else None
        caps: dict[str, Any] = {}
        if channel is not None:
            try:
                caps = json.loads(channel["capabilities"] or "{}")
            except json.JSONDecodeError:
                caps = {}
        max_len = min(int(caps.get("max_text_len") or self.cfg.max_text_len), self.cfg.max_text_len)
        if not caps.get("segments", False):
            return max_len, 1
        return max_len, min(int(caps.get("max_parts") or self.cfg.max_parts), self.cfg.max_parts)

    def _system_prompt(self, row: dict[str, Any]) -> str:
        """扮演定义：真实实例走运行层（已过滤的认知切片），占位会话仍用占位提示词。"""
        runtime = getattr(self, "runtime", None)
        if runtime is None:
            return self.cfg.placeholder["system_prompt"]
        session = self.store.session_get(row["session_id"])
        if session is None or str(session["instance_id"]).startswith("ph-"):
            return self.cfg.placeholder["system_prompt"]
        try:
            return runtime.system_prompt(session, topic=str(row.get("text") or ""))
        except Exception:  # 运行层不可用不得阻断对话
            log.exception("runtime prompt failed session=%s", session["id"])
            return self.cfg.placeholder["system_prompt"]

    def _system_prompt_with_memory(self, row: dict[str, Any]) -> tuple[str, list[str]]:
        """真实实例：扮演定义 + 记忆简报；占位会话或无运行层时退回占位提示词。"""
        runtime = getattr(self, "runtime", None)
        session = self.store.session_get(row["session_id"]) if runtime is not None else None
        if runtime is None or session is None or str(session["instance_id"]).startswith("ph-"):
            return self.cfg.placeholder["system_prompt"], []
        try:
            context = runtime.turn_context(session, topic=str(row.get("text") or ""))
        except Exception:  # 运行层不可用不得阻断对话
            log.exception("runtime context failed session=%s", session["id"])
            return self.cfg.placeholder["system_prompt"], []
        return str(context.get("prompt") or ""), list(context.get("memory_ids") or [])

    def _build_messages(self, row: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        """返回 (消息序列, 本轮注入的记忆标识)；记忆简报只进上下文（§5.1）。"""
        history = self.store.context_window(row["session_id"], self.cfg.context_history_max)
        prompt, recalled = self._system_prompt_with_memory(row)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt}
        ]
        for item in history:
            if item["seq"] >= row["seq"]:
                continue
            if item["role"] == "user":
                messages.append({"role": "user", "content": item["text"] or ""})
            elif item["role"] == "character":
                messages.append({"role": "assistant", "content": _flatten(item["parts"])})
        messages.append({"role": "user", "content": row["text"] or ""})
        return messages, recalled

    # ---------- 投递 ----------

    async def _send_batches(self, msg: dict[str, Any]) -> None:
        token = msg["binding_token"]
        if not token or not msg["channel_id"] or not msg["thread_id"]:
            return
        max_len, max_parts = self._limits(msg["channel_id"])
        batches = json.loads(msg["parts"] or "[]")
        states = {r["batch_index"]: r["state"] for r in self.store.delivery_rows(msg["seq"])}
        incompatible = False
        for index, batch in enumerate(batches):
            if states.get(index) == "accepted":  # 部分成功不重发已确认批次
                continue
            if len(batch) > max_parts or any(len(part) > max_len for part in batch):
                # 协商限额变小：只报告投递能力不兼容，不重排 / 裁剪 / 重新生成（§2.4）
                if states.get(index) != "incompatible":
                    self.store.delivery_set(msg["seq"], index, "incompatible")
                incompatible = True
                continue
            envelope = ump.make(
                "reply",
                {
                    "message_id": msg["message_id"],
                    "reply_to": msg["reply_to"],
                    "covers": json.loads(msg["covers"] or "[]"),
                    "batch_index": index,
                    "batch_count": len(batches),
                    "parts": [{"text": text} for text in batch],
                },
                thread_id=msg["thread_id"],
                binding_token=token,
                id=ump.new_id("s"),
            )
            delivered = False
            try:
                delivered = await asyncio.wait_for(
                    self.deliver(msg["channel_id"], msg["thread_id"], envelope), timeout=SEND_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                # 通道在现实中不读数据：发送结果按未知处理，不假称成功（§2.3）
                log.warning("delivery timeout msg=%s batch=%s", msg["message_id"], index)
                self.store.delivery_set(msg["seq"], index, "unknown")
                continue
            if delivered:
                self.store.delivery_set(msg["seq"], index, "sent")

        if incompatible:
            await self.deliver(
                msg["channel_id"],
                msg["thread_id"],
                ump.error_envelope(
                    UmpError(
                        Err.UNSUPPORTED_CAPABILITY,
                        "既有分段计划超出当前协商的通道能力，保留历史不重排",
                        retryable=False,
                        ref=msg["message_id"],
                        stage=Stage.DELIVERY,
                    ),
                    thread_id=msg["thread_id"],
                ),
            )

    async def resend_pending(self, channel_id: str, thread_id: str, limit: int = 20) -> int:
        """重连后有界补投仍在投递资格内的已固化回复；不重新生成。"""
        sent = 0
        for msg in self.store.pending_outbound(channel_id, thread_id, limit=limit):
            await self._send_batches(msg)
            sent += 1
        return sent

    async def _status(self, row: dict[str, Any], state: str) -> None:
        envelope = ump.make("status", {"state": state}, thread_id=row["thread_id"])
        await self.deliver(row["channel_id"], row["thread_id"] or "", envelope)

    async def _error(self, row: dict[str, Any], error: UmpError) -> None:
        envelope = ump.error_envelope(error, thread_id=row["thread_id"], ref=row["env_id"])
        await self.deliver(row["channel_id"], row["thread_id"] or "", envelope)

    async def shutdown(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()


def _flatten(parts_json: str | None) -> str:
    try:
        batches = json.loads(parts_json or "[]")
    except json.JSONDecodeError:
        return ""
    return "\n".join(text for batch in batches for text in batch)
