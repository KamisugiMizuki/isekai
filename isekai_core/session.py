"""会话核心（阶段 0 最小实现）。

范围：持久会话身份、绑定令牌核验后的接受与去重、生成链路（取上下文 → 调用 LLM →
校验 → 固化 → 投递）、失败与重试、投递回执。世界 / 认知 / 记忆引擎在后续阶段接入。
"""

from __future__ import annotations

import asyncio
import json
import random
import secrets
import time
from typing import Any, Awaitable, Callable

from . import ump
from .config import Config
from .llm import LLMError
from .log import get_logger
from .runtime import life, narrative, proactive
from .store import EnvelopeConflict, Store
from .ump import Envelope, Err, Stage, UmpError

log = get_logger("isekai.session")

#: 单批发送的现实上限：通道端不读数据时不能把轮次拖死（发送结果按未知处理）
SEND_TIMEOUT_S = 15.0

#: 睡眠期等待到点后的状态表述（§4.5）：只进生成上下文，不展示给用户，也不暴露内部睡眠状态。
SLEEP_REPLY_HINT = "（她此刻还在睡：只答一句短暂、朦胧的话，不要说她起身、做事或者已经清醒。）"
WAKE_REPLY_HINT = "（她已经醒了：按清醒状态回答，可以回一句刚才还睡着，但不要用睡意否认这段时间里世界已经推进。）"

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

        # 冻结线一律不允许对话（§2.2）：必须是已激活的时间线才能接新消息。
        # 阶段 0 占位会话没有时间线行，不在此列（它不是世界实例）。
        session_row = self.store.session_get(str(thread_row["session_id"])) or {}
        timeline_id = str(session_row.get("timeline_id") or "")
        line = self.store.timeline_get(timeline_id) if timeline_id else None
        # 归档（身故）后不再产生新回复：首次说明一次，之后只按协议拒绝（§5.7）
        instance_id = str(session_row.get("instance_id") or "")
        character_id = str(session_row.get("character_id") or "")
        if instance_id and character_id and self.store.death_exists(instance_id, timeline_id, character_id):
            await self._archive_notice(session_row, character_id)
            raise UmpError(
                Err.STATE_BLOCKED,
                "该角色已归档（身故），不再产生新回复；历史保留可查",
                retryable=False,
                ref=env_id,
                stage=Stage.RECEIVE,
            )
        await self._flush_proactive(str(session_row.get("id") or ""))
        if line is not None:
            # 真实时间线：冻结 / 归档一律不允许对话（§2.2）；占位会话没有时间线行，不在此列
            if str(line.get("state") or "") != "active":
                raise UmpError(
                    Err.STATE_BLOCKED,
                    "时间线当前不可对话（未激活或已归档）；先激活再发",
                    retryable=False,
                    ref=env_id,
                    stage=Stage.RECEIVE,
                )
            # 兼容性阻断的实例不接受新对话提交（§7.6）：检查先于提交、不静默按当前规则作答
            runtime = getattr(self, "runtime", None)
            if runtime is not None and not runtime.compatible(instance_id):
                raise UmpError(
                    Err.STATE_BLOCKED,
                    "实例兼容性阻断：只读历史，等兼容版本或用户确认转换后再对话",
                    retryable=False,
                    ref=env_id,
                    stage=Stage.RECEIVE,
                )

        # 容量闸（CHANNEL_PLUGIN_SPEC §3.2）：排队满了就拒新输入，已接受的照常处理。
        # 重复发送（同 env_id）不走这道闸——幂等重放必须能查回既有状态。
        if self.store.inbound_find(channel_id, thread_id, env_id) is None:
            cap = int(getattr(self.cfg, "max_queued_inbound", 0) or 0)
            queued = self.store.inbound_queued_count(str(thread_row["session_id"]))
            if cap and queued >= cap:
                raise UmpError(
                    Err.OVERLOADED,
                    f"排队入站已达上限（{cap}），稍后重试",
                    retryable=True,
                    ref=env_id,
                    stage=Stage.RECEIVE,
                )

        try:
            row, created = self.store.inbound_put(
                session_id=thread_row["session_id"],
                channel_id=channel_id,
                thread_id=thread_id,
                env_id=env_id,
                binding_version=thread_row["binding_version"],
                text=text,
                attachments=list(env.payload.get("attachments") or []),
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
        # 入站行不持有 message_id：关联列是 reply_message_id（出站行的稳定标识）
        reply_id = row["message_id"] or row["reply_message_id"]
        if row["state"] == "done" and reply_id:
            fixed = self.store.outbound_by_message_id(str(reply_id))
            if fixed is not None:
                await self._send_batches(fixed)
        return {"ref": env_id, "state": row["state"], "message_id": reply_id}

    async def _archive_notice(self, session_row: dict[str, Any], character_id: str) -> None:
        """归档说明：一个会话只给一次，且只进历史与待投递（不占配额、不重发）。"""
        session_id = str(session_row.get("id") or "")
        if not session_id or self.store.session_notice_get(session_id, "archive") is not None:
            return
        target = self.store.thread_for_session(session_id) or {}
        message_id = f"m-{secrets.token_hex(6)}"
        who = ""
        runtime = getattr(self, "runtime", None)
        try:
            if runtime is not None:
                candidate = runtime._display_name(
                    str(session_row.get("instance_id") or ""),
                    str(session_row.get("timeline_id") or ""),
                    character_id,
                )
                who = "" if candidate == character_id else candidate
        except Exception:
            who = ""
        # 说明是联络系统 / 管理机制发的（role=notice），不冒充成她本人的新发言，也不带内部标识
        text = f"{who or '她'}已经不在了。之后的消息不会再转给她，早先的对话都还留着。"
        self.store.outbound_put(
            session_id=session_id,
            message_id=message_id,
            reply_to=None,
            covers=[],
            batches=[[text]],
            target_channel=str(target.get("channel_id") or ""),
            target_thread=str(target.get("thread_id") or ""),
            binding_version=int(target.get("binding_version") or 0),
            binding_token=str(target.get("binding_token") or ""),
            role="notice",
        )
        self.store.session_notice_put(
            {
                "session_id": session_id,
                "instance_id": str(session_row.get("instance_id") or ""),
                "timeline_id": str(session_row.get("timeline_id") or ""),
                "kind": "archive",
                "message_id": message_id,
                "created_real": time.time(),
            }
        )
        try:
            fixed = self.store.outbound_by_message_id(message_id)
            if fixed is not None:
                await self._send_batches(fixed)
        except Exception:
            pass  # 投递失败不留半成品：消息已在历史与待投递里

    async def flush_proactive(self, session_id: str) -> int:
        """投递仍有效的主动消息（§5.3）：核心 tick 与入站路径共用这一条。」

        只发最新一条，积压留在历史里，不做洪峰补发。
        """
        return await self._flush_proactive(session_id)

    async def _flush_proactive(self, session_id: str) -> int:
        """投递仍有效的主动消息（§5.3）：只发最新一条，积压留在历史里，不做洪峰补发。"""
        pending = self.store.proactive_pending(session_id, since_world=0)
        if not pending:
            return 0
        newest = pending[-1]
        await self._send_batches(newest)
        return 1

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
            # accepted.state 只报接收侧枚举；投递汇总另置字段（否则客户端按协议校验失败、静默丢弃）
            settled = {"delivered": "done", "accepted": "done", "sent": "done"}.get(
                str(rollup), "queued"
            )
            return {
                "ref": ref,
                "state": settled,
                "delivery": rollup,
                "message_id": outbound["message_id"],
            }

        state = outbound["state"]
        # 入站行不持有 message_id：关联列是 reply_message_id；合并批里每条入站都指向同一份固化回复（§4.5）
        reply_id = outbound["message_id"] or outbound.get("reply_message_id")
        if state in ("queued", "processing"):
            return {"ref": ref, "state": state, "message_id": reply_id}
        if state == "done":
            fixed = self.store.outbound_by_message_id(str(reply_id or ""))
            if fixed is not None:
                await self._send_batches(fixed)
            return {"ref": ref, "state": "done", "message_id": reply_id}
        if state == "cancelled":
            raise UmpError(Err.VOIDED, "该输入已作废", retryable=False, ref=ref, stage=Stage.RECEIVE)
        # failed：恢复同一逻辑轮次的新尝试
        if self.store.has_later_success(str(outbound["session_id"] or ""), int(outbound["seq"])):
            # 这轮之后已经聊过新的了：重放旧轮次会插队，让用户直接重发（§4.3）
            raise UmpError(
                Err.CONFLICT,
                "这轮之后已经有过新的对话，旧消息不能插队重试；请直接重发",
                retryable=False,
                ref=ref,
                stage=Stage.RECEIVE,
            )
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
            batch = await self._wait_and_collect(row)
            if batch is None:  # 等待期间已失效：不生成、不投递
                return
            await self._generate(batch[0], batch=batch)

    # ---------- 睡眠期等待与合并（§4.5） ----------

    async def _wait_and_collect(self, row: dict[str, Any]) -> list[dict[str, Any]] | None:
        """睡眠期等待一拍，再取同一来源的一段连续入站合成一批（多入一回）。

        - 只在该角色此刻处于睡眠块时等待；非睡眠时段不额外等待，同会话仍按接受顺序逐条处理。
        - 截止点以首次接受该批输入的现实时间为基准，一次确定并持久化：后续输入不重置、
          不按条数叠加；取快照与排队已耗去的时间计入等待（到点即不再等）。
        - 等待只让本任务睡，世界推进不被暂停。
        - 返回 None 表示这一轮在等待期间已失效（冻结 / 回滚 / 归档 / 删除 / 重绑）。
        """
        session = self.store.session_get(str(row["session_id"])) or {}
        if not self._sleeping_now(session):
            return [row]
        deadline = self.store.inbound_claim_deadline(int(row["seq"]), delay=self._sleep_delay())
        row = self.store.message_get(int(row["seq"])) or row  # 取回已持久化截止点的行
        remaining = deadline - time.time()
        if remaining > 0:
            # 睡眠期等待在通道侧显示为正常处理中，不暴露内部睡眠状态（DESKTOP_SPEC §3.1）
            await self._status(row, "thinking")
            await asyncio.sleep(remaining)
        batch = self._collect_batch(row)
        stale = self._stale_code(row)
        if stale:
            # 等待期间被冻结 / 回滚 / 删除 / 重绑：旧任务失效，批内各输入一并结算（§4.5）
            log.info("waiting turn dropped: %s seq=%s", stale, row["seq"])
            for item in batch:
                self.store.inbound_set_state(int(item["seq"]), "cancelled", error_code=stale)
            # 打断要说出来（status.interrupted）：客户端别把这轮当成正常结束
            await self._status(row, "interrupted")
            await self._status(row, "idle")
            return None
        return batch

    async def _generate(self, row: dict[str, Any], *, batch: list[dict[str, Any]] | None = None) -> None:
        rows = list(batch or [row])
        seqs = [int(item["seq"]) for item in rows]
        started = time.monotonic()
        for seq in seqs:
            self.store.inbound_set_state(seq, "processing")
        # 先占号：流式增量与最终固化帧共用一个 message_id（客户端据此把预览换成正文）
        message_id = ump.new_id("m")
        await self._status(row, "thinking")
        try:
            query_vector = await self._query_vector(rows)
            messages, recalled, unit = self._build_messages(rows, query_vector=query_vector)
            self._recalled = list(recalled)
            if self._caps(row["channel_id"]).get("streaming") and hasattr(self.llm, "chat_stream"):
                text = await self._stream_reply(row, message_id=message_id, messages=messages)
            else:
                text = await self.llm.chat(messages)
            text, audit_findings = await self._audit_or_retry(unit, text, messages)
        except LLMError as exc:
            log.warning(
                "generation failed seq=%s stage=%s code=%s elapsed=%.1fs",
                row["seq"],
                Stage.GENERATE,
                exc.code,
                time.monotonic() - started,
            )
            for seq in seqs:
                self.store.inbound_set_state(seq, "failed", error_code=exc.code)
            await self._status(row, "idle")
            await self._error(row, UmpError(exc.code, exc.message, retryable=exc.retryable, stage=Stage.GENERATE))
            return
        except Exception:  # 兜底：异常不得被当成成功文本
            log.exception("generation crashed seq=%s", row["seq"])
            for seq in seqs:
                self.store.inbound_set_state(seq, "failed", error_code=Err.INTERNAL)
            await self._status(row, "idle")
            await self._error(row, UmpError(Err.INTERNAL, "生成失败", retryable=True, stage=Stage.GENERATE))
            return

        parts = split_parts(text, self._limits(row["channel_id"])[0])
        if not parts:
            for seq in seqs:
                self.store.inbound_set_state(seq, "failed", error_code="empty_completion")
            await self._status(row, "idle")
            await self._error(
                row, UmpError(Err.GENERATION_FAILED, "空回复", retryable=True, stage=Stage.GENERATE)
            )
            return

        # 提交前核对作废 / 换代 / 冻结 / 归档 / 身故：迟到结果不写入、不投递（§七、§4.5、§5.7）
        stale = self._stale_code(row)
        if stale:
            log.info("turn dropped: %s seq=%s", stale, row["seq"])
            for seq in seqs:
                self.store.inbound_set_state(seq, "cancelled", error_code=stale)
            await self._status(row, "interrupted")
            await self._status(row, "idle")
            return
        thread = self.store.thread_get(row["channel_id"], row["thread_id"])
        if thread is None or thread["binding_version"] != row["binding_version"]:
            # 提交前核对绑定版本：重绑后迟到结果不写入、不投递
            log.info("turn dropped: binding changed seq=%s", row["seq"])
            for seq in seqs:
                self.store.inbound_set_state(seq, "cancelled", error_code=Err.BINDING_EXPIRED)
            await self._status(row, "interrupted")
            await self._status(row, "idle")
            return

        # 追赶中要说清（§2.6 条 5）：回复按已完成的过去作答（安全侧），但别让通道端以为世界停下不动。
        # 先发提示再固化回复——客户端普遍按「最后一条＝她的回复」判断一轮结束，顺序反了会卡住。
        await self._catching_up_notice(row, binding_token=str(thread["binding_token"]))
        msg = self.store.commit_turn(
            inbound_seq=row["seq"],
            inbound_seqs=seqs,
            outbound={
                "session_id": row["session_id"],
                "message_id": message_id,
                "reply_to": rows[-1]["env_id"],          # 批内最后一条入站（§4.5）
                "covers": [item["env_id"] for item in rows],
                "batches": plan_batches(parts, self._limits(row["channel_id"])[1]),
                "model_fingerprint": self._model_fingerprint(),
                "target_channel": row["channel_id"],
                "target_thread": row["thread_id"],
                "binding_version": row["binding_version"],
                "binding_token": thread["binding_token"],
            },
        )
        self._settle_memory(rows, message_id=str(message_id), reply_text=chr(10).join(parts))
        self._record_turn_unit(row, unit, message_id=str(message_id), findings=audit_findings)
        await self._send_batches(msg)
        await self._status(row, "idle")

    async def _audit_or_retry(
        self, unit: dict[str, Any] | None, text: str, messages: list[dict[str, Any]]
    ) -> tuple[str, list[dict[str, Any]]]:
        """问答轮的后验检查（NARRATIVE_LAYER §6.2 落地口径）：不过就加严重试一次。

        与主动路径的差别：这里不能拿用户的问题当筹码（拒答比越界更糟），
        所以第二次仍不过时**照发并把检查结果留档**——管理面能看到这条留痕。
        """
        runtime = getattr(self, "runtime", None)
        if runtime is None or not unit or not str(text or "").strip():
            return text, []
        try:
            ok, why = await runtime.audit_reply(unit, text, llm=self.llm)
        except Exception:  # 检查本身失败不算越界，也不阻断回答
            log.exception("turn audit failed session=%s", unit.get("id"))
            return text, []
        if ok:
            return text, []
        findings = [{"kind": "audit", "detail": str(why or "")}]
        strict = narrative.strict_note()
        head = str((messages[0] or {}).get("content") or "")
        retry_messages = [{"role": "system", "content": f"{head}\n\n{strict}"}, *messages[1:]]
        try:
            second = await self.llm.chat(retry_messages)
        except Exception:
            log.exception("turn audit retry failed")
            return text, findings
        if not str(second or "").strip():
            return text, findings
        try:
            ok2, why2 = await runtime.audit_reply(unit, second, llm=self.llm)
        except Exception:
            return text, findings
        if ok2:
            return str(second), []
        return str(second), [{"kind": "audit", "detail": str(why2 or "")}]

    def _record_turn_unit(
        self,
        row: dict[str, Any],
        unit: dict[str, Any] | None,
        *,
        message_id: str,
        findings: list[dict[str, Any]] | None = None,
    ) -> None:
        """她这一轮讲过的线索记进同一本账：主动消息与问答共用消费与图谱（§7.1）。"""
        runtime = getattr(self, "runtime", None)
        if runtime is None or not unit:
            return
        session = self.store.session_get(str(row["session_id"]))
        if session is None:
            return
        try:
            runtime.record_turn_unit(session, unit, message_id=str(message_id), findings=list(findings or []))
        except Exception:
            log.exception("record turn unit failed session=%s", row.get("session_id"))

    def _model_fingerprint(self) -> str:
        """产出这条回复的模型标识（§十.15）：换模型后旧回复仍看得出边界。"""
        llm = getattr(self, "llm", None)
        model = str(getattr(getattr(llm, "cfg", None), "model", "") or "")
        if not model:
            cfg = getattr(self, "cfg", None)
            model = str(getattr(getattr(cfg, "llm", None), "model", "") or "")
        return model[:80]

    async def _catching_up_notice(self, row: dict[str, Any], *, binding_token: str) -> None:
        """追赶期的系统提示：每个追赶档只发一条（session_notice 去重），追平后自动解除。"""
        session_id = str(row["session_id"])
        session = self.store.session_get(session_id) or {}
        timeline_id = str(session.get("timeline_id") or "")
        clock = self.store.clock_get(timeline_id) if timeline_id else None
        if clock is None:
            return
        # 判「追赶中」要看**当下投影**（目标水位 vs 已完成水位），不能只看落库的 catching_up：
        # 高倍率下还没跑到第一次补算时，标志位仍是 0，而世界已经领先一大截（§2.6 条 5）。
        behind = int(clock.get("catching_up") or 0) == 1
        runtime = getattr(self, "runtime", None)
        if runtime is not None and timeline_id and str(session.get("instance_id") or ""):
            try:
                view = runtime.view(
                    str(session["instance_id"]), timeline_id, now_real=time.time()
                )
            except Exception:
                view = {}
            if view.get("world_seconds") is not None:
                behind = int(view["world_seconds"]) > int(view.get("processed_world") or 0)
        existing = self.store.session_notice_get(session_id, "catching_up")
        if not behind:
            if existing is not None:
                self.store.session_notice_clear(session_id, "catching_up")
            return
        if existing is not None:
            return
        message_id = f"m-{secrets.token_hex(6)}"
        self.store.outbound_put(
            session_id=session_id,
            message_id=message_id,
            reply_to=None,
            covers=[],
            batches=[["世界还在追赶：这条回复按已完成的过去作答，追平后我接着往下说。"]],
            target_channel=str(row["channel_id"]),
            target_thread=str(row["thread_id"]),
            binding_version=int(row["binding_version"]),
            binding_token=binding_token,
            role="notice",
        )
        self.store.session_notice_put(
            {
                "session_id": session_id,
                "instance_id": str(session.get("instance_id") or ""),
                "timeline_id": timeline_id,
                "kind": "catching_up",
                "message_id": message_id,
                "created_real": time.time(),
            }
        )
        fixed = self.store.outbound_by_message_id(message_id)
        if fixed is not None:
            await self._send_batches(fixed)

    def _collect_batch(self, row: dict[str, Any]) -> list[dict[str, Any]]:
        """封口（§4.5）：到点、达到容量、或遇到其他来源 / 已作废的入站即止，后来输入属下一批。"""
        capacity = max(1, int(self.cfg.runtime.merge_batch_max))
        source = (row["channel_id"], row["thread_id"], int(row["binding_version"]))
        batch = [row]
        for candidate in self.store.inbound_queued_after(str(row["session_id"]), int(row["seq"])):
            if len(batch) >= capacity:
                break
            if (candidate["channel_id"], candidate["thread_id"], int(candidate["binding_version"])) != source:
                break  # 不跨通道 / thread / 绑定版本合并
            if self.store.void_has(
                str(candidate["channel_id"] or ""),
                str(candidate["thread_id"] or ""),
                str(candidate["env_id"] or ""),
            ):
                break  # 已作废（回滚 / 重绑过）：让它自己按下一批结算
            batch.append(candidate)
        return batch

    def _stale_code(self, row: dict[str, Any]) -> str:
        """等待中的旧任务是否已失效（§4.5 恢复与作废）；返回空串表示仍然有效。"""
        channel_id = str(row.get("channel_id") or "")
        thread_id = str(row.get("thread_id") or "")
        if self.store.void_has(channel_id, thread_id, str(row.get("env_id") or "")):
            return Err.VOIDED
        thread = self.store.thread_get(channel_id, thread_id)
        if thread is None or int(thread["binding_version"]) != int(row["binding_version"]):
            return Err.BINDING_EXPIRED
        session = self.store.session_get(str(row["session_id"])) or {}
        timeline_id = str(session.get("timeline_id") or "")
        line = self.store.timeline_get(timeline_id) if timeline_id else None
        if line is not None and str(line.get("state") or "") != "active":
            return Err.STATE_BLOCKED  # 冻结 / 归档
        instance_id = str(session.get("instance_id") or "")
        character_id = str(session.get("character_id") or "")
        if instance_id and character_id and self.store.death_exists(instance_id, timeline_id, character_id):
            return Err.STATE_BLOCKED  # 角色已归档（身故）：迟到轮次同样不写回、不投递（§5.7）
        return ""

    def _sleep_delay(self) -> float:
        """等待一拍的长度：区间来自配置（§4.5 起点 30–120 秒），一批只取一次。"""
        low = max(0.0, float(self.cfg.runtime.sleep_wait_min_s))
        high = max(low, float(self.cfg.runtime.sleep_wait_max_s))
        return random.uniform(low, high)

    def _sleeping_now(self, session: dict[str, Any]) -> bool:
        """该角色此刻是否处于睡眠块；生活线 / 时钟不可读按未就绪处理——不等待，也不凭现实钟猜（§4.5）。"""
        runtime = getattr(self, "runtime", None)
        instance_id = str(session.get("instance_id") or "")
        timeline_id = str(session.get("timeline_id") or "")
        character_id = str(session.get("character_id") or "")
        if runtime is None or not (instance_id and timeline_id and character_id):
            return False
        if instance_id.startswith("ph-"):  # 阶段 0 占位会话：没有生活线
            return False
        try:
            world_seconds = runtime.world_moment(instance_id, timeline_id)
            plan = self.store.plan_latest(instance_id, timeline_id, character_id)
            window = life.current_window(plan, world_seconds)
        except Exception:  # 运行层不可用不得当作「她在睡」
            log.exception("life line unreadable session=%s", session.get("id"))
            return False
        return bool(window) and proactive.sleeping(str(window.get("activity") or ""))

    def _sleep_hint(self, row: dict[str, Any]) -> str:
        """到期的真实状态表述（§4.5）：仍睡眠 → 短暂朦胧；已醒来 → 按清醒状态，不否认推进。"""
        if not float(row.get("wait_until") or 0):
            return ""  # 这一轮没等待过：不加睡眠相关的口吻约束
        session = self.store.session_get(str(row["session_id"])) or {}
        return SLEEP_REPLY_HINT if self._sleeping_now(session) else WAKE_REPLY_HINT

    def _settle_memory(self, rows: list[dict[str, Any]], *, message_id: str, reply_text: str) -> None:
        """已固化回复的后续记账（§4.1 / §5.3）：登记来源待提取 + 本轮实际用到的记忆强化。

        合并批（§4.5）：每条入站按自己的来源各登记一次，回复按本轮唯一标识登记一次
        （不重复提取同一来源）。失败不影响投递：记忆是派生数据，不能反向拖住已接受的回复。
        """
        runtime = getattr(self, "runtime", None)
        head = rows[0]
        session = self.store.session_get(head["session_id"])
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
            for row in rows:
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

    def _caps(self, channel_id: str) -> dict[str, Any]:
        """该通道握手协商后的能力（缺省空）。"""
        channel = self.store.channel_get(channel_id) if channel_id else None
        if channel is None:
            return {}
        try:
            caps = json.loads(channel["capabilities"] or "{}")
        except json.JSONDecodeError:
            return {}
        return caps if isinstance(caps, dict) else {}

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

    async def _query_vector(self, rows: list[dict[str, Any]]) -> list[float] | None:
        """查询向量（§5.2）：未配置或失败即 None，召回退化全文，不阻断对话。"""
        runtime = getattr(self, "runtime", None)
        if runtime is None or not getattr(runtime, "embedding_ready", False):
            return None
        try:
            session = self.store.session_get(rows[0]["session_id"]) or {}
            return await runtime.embed_query(
                _batch_text(rows),
                instance_id=str(session.get("instance_id") or ""),
                timeline_id=str(session.get("timeline_id") or ""),
            )
        except Exception:
            log.exception("query embedding failed seq=%s", rows[0].get("seq"))
            return None

    def _system_prompt_with_memory(
        self, rows: list[dict[str, Any]], *, query_vector: list[float] | None = None
    ) -> tuple[str, list[str], dict[str, Any] | None]:
        """真实实例：扮演定义 + 记忆简报；占位会话或无运行层时退回占位提示词。

        第三个返回值是「本轮用到的叙事单元」（可能为 None）：问答轮的后验检查要用它。
        """
        runtime = getattr(self, "runtime", None)
        session = self.store.session_get(rows[0]["session_id"]) if runtime is not None else None
        if runtime is None or session is None or str(session["instance_id"]).startswith("ph-"):
            return self.cfg.placeholder["system_prompt"], [], None
        try:
            context = runtime.turn_context(
                session, topic=_batch_text(rows), query_vector=query_vector
            )
        except Exception:  # 运行层不可用不得阻断对话
            log.exception("runtime context failed session=%s", session["id"])
            return self.cfg.placeholder["system_prompt"], [], None
        unit = context.get("unit")
        return str(context.get("prompt") or ""), list(context.get("memory_ids") or []), (
            dict(unit) if isinstance(unit, dict) else None
        )

    def _build_messages(
        self, rows: list[dict[str, Any]], *, query_vector: list[float] | None = None
    ) -> tuple[list[dict[str, Any]], list[str], dict[str, Any] | None]:
        """返回 (消息序列, 本轮注入的记忆标识, 本轮叙事单元)；记忆简报只进上下文（§5.1）。

        合并批（§4.5）：批内各输入按接受顺序保留自己的原文，合成同一轮的用户侧输入；
        到期的真实状态（仍睡 / 已醒）作为口吻约束随扮演定义一起进上下文。
        """
        head = rows[0]
        history = self.store.context_window(head["session_id"], self.cfg.context_history_max)
        prompt, recalled, unit = self._system_prompt_with_memory(rows, query_vector=query_vector)
        hint = self._sleep_hint(head)
        if hint:
            prompt = f"{prompt}\n\n{hint}"
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": prompt}
        ]
        for item in history:
            if item["seq"] >= head["seq"]:
                continue
            if item["role"] == "user":
                messages.append({"role": "user", "content": _user_content(item["text"] or "", item.get("attachments"))})
            elif item["role"] == "character":
                messages.append({"role": "assistant", "content": _flatten(item["parts"])})
        for item in rows:
            messages.append({"role": "user", "content": _user_content(item["text"] or "", item.get("attachments"))})
        return messages, recalled, unit

    # ---------- 投递 ----------

    async def _send_batches(self, msg: dict[str, Any]) -> None:
        token = msg["binding_token"]
        if not token or not msg["channel_id"] or not msg["thread_id"]:
            return
        max_len, max_parts = self._limits(msg["channel_id"])
        batches = json.loads(msg["parts"] or "[]")
        states = {r["batch_index"]: r["state"] for r in self.store.delivery_rows(msg["seq"])}
        notice = str(msg.get("role") or "") == "notice"
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
            if notice:
                # 联络系统 / 管理机制的说明以 system_notice 分类上线，不伪装成角色回复（§2.3）
                envelope = ump.make(
                    "system_notice",
                    {
                        "text": "\n".join(str(text) for text in batch),
                        "message_id": str(msg["message_id"]),
                    },
                    thread_id=msg["thread_id"],
                    binding_token=token,
                    id=ump.new_id("s"),
                )
            else:
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
        """重连后有界补投仍在投递资格内的已固化回复；不重新生成。

        主动消息只补发**最新一条**（§5.2 / §5.3）：离线积压留在历史里，不因重连变成补发洪峰。
        """
        rows = self.store.pending_outbound(channel_id, thread_id, limit=limit)
        proactive_by_session: dict[str, list[str]] = {}
        for session_id in {str(msg.get("session_id") or "") for msg in rows}:
            proactive_by_session[session_id] = [
                str(item["message_id"]) for item in self.store.proactive_pending(session_id, since_world=0)
            ]
        sent = 0
        for msg in rows:
            message_id = str(msg.get("message_id") or "")
            ids = proactive_by_session.get(str(msg.get("session_id") or "")) or []
            if ids and message_id in ids and message_id != ids[-1]:
                continue  # 旧的主动消息：留在历史里，不补发
            await self._send_batches(msg)
            sent += 1
        return sent

    async def _status(self, row: dict[str, Any], state: str) -> None:
        envelope = ump.make("status", {"state": state}, thread_id=row["thread_id"])
        await self.deliver(row["channel_id"], row["thread_id"] or "", envelope)

    async def _stream_reply(self, row: dict[str, Any], *, message_id: str, messages: list[dict[str, Any]]) -> str:
        """流式生成：逐段投递 `reply_delta`（**临时预览**），返回全文。

        只发给协商了 `streaming` 的通道；增量不作数——最终正文仍以固化的 `reply` 帧为准
        （后验检查可能改字，客户端拿最终帧覆盖缓冲区）。流到一半失败时，已发的增量由客户端按
        「最终帧没来就不算数」处理，这里如实抛错走生成失败路径。
        """
        chunks: list[str] = []
        index = 0
        async for piece in self.llm.chat_stream(messages):
            chunks.append(piece)
            await self.deliver(
                row["channel_id"],
                row["thread_id"] or "",
                ump.make(
                    "reply_delta",
                    {"message_id": message_id, "index": index, "text": piece},
                    thread_id=row["thread_id"],
                    id=ump.new_id("s"),
                ),
            )
            index += 1
        return "".join(chunks)

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


def attachment_list(raw: Any) -> list[dict[str, Any]]:
    """消息行里的附件（JSON 文本或已解析的列表）→ 列表；坏数据当没有。"""
    if isinstance(raw, list):
        return [item for item in raw if isinstance(item, dict)]
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return []
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
    return []


def _user_content(text: str, attachments: Any) -> Any:
    """用户侧内容：有附件时按多模态块给（§七 附件项）。

    图像直接给 `image_url`（data URL，模型能不能看由模型决定）；其余类型**只**留一行文字标注
    （名字 / 类型 / 字节数），不把文件内容塞进提示词。
    """
    items = attachment_list(attachments)
    if not items:
        return text
    notes = [
        f"{item.get('name')}（{item.get('media_type')}，{int(item.get('size') or 0)} 字节）"
        for item in items
        if not str(item.get("media_type") or "").startswith("image/")
    ]
    body = text if not notes else f"{text}\n（随信附上：{'、'.join(notes)}）"
    blocks: list[dict[str, Any]] = [{"type": "text", "text": body}]
    for item in items:
        media_type = str(item.get("media_type") or "")
        if media_type.startswith("image/") and item.get("data"):
            blocks.append(
                {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{item['data']}"}}
            )
    return blocks


def _batch_text(rows: list[dict[str, Any]]) -> str:
    """合并批的按序全文（召回与上下文用的主题文本）。"""
    return "\n".join(str(row.get("text") or "") for row in rows)
