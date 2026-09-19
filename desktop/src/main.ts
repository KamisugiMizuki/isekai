/*
 * 内建聊天客户端（官方 UMP 通道客户端）+ 管理占位 + 设置面。
 * 业务都在核心：本文件只做连接状态、渲染与协议收发（DESKTOP_SPEC §3）。
 */

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { MgmtClient, UmpClient, type Envelope } from "./ump";
import "./styles.css";

interface CoreStatus {
  state: string;
  endpoint?: string | null;
  bootstrap?: string | null;
  mgmt?: string | null;
  pid?: number | null;
  error?: string | null;
  app?: string | null;
  data_format?: string | null;
  rules?: string | null;
}

interface HistoryRow {
  seq: number;
  role: string;
  text: string | null;
  parts: string[][] | null;
  env_id?: string | null;
  message_id?: string | null;
  reply_message_id?: string | null;
  state: string;
  created_at: number;
}

interface Message {
  role: "user" | "character" | "notice";
  text: string;
  parts: string[];
  envId?: string;
  messageId?: string;
  seenBatches?: number[];
  state: string;
  errorCode?: string;
  delivery?: string;
  time: number;
}

const $ = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;

const state = {
  phase: "starting" as "starting" | "ready" | "failed",
  threadId: "main",
  token: "",
  sessionId: "",
  messages: [] as Message[],
  thinking: false,
};

let ump: UmpClient | null = null;
let mgmt: MgmtClient | null = null;
let shellStatus: CoreStatus | null = null;

/* ---------- 渲染 ---------- */

function setStatus(text: string, kind: "pending" | "ok" | "bad"): void {
  const chip = $("status");
  chip.textContent = text;
  chip.className = `chip ${kind}`;
}

function renderTopbar(): void {
  const session = state.sessionId ? state.sessionId : "未连接";
  $("title").textContent = shellStatus?.state === "ready" ? `占位会话 · ${session}` : "未连接";
}

function roleLabel(role: string): string {
  return role === "user" ? "你" : role === "character" ? "角色" : "系统";
}

function formatTime(seconds: number): string {
  const date = new Date(seconds * 1000);
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

const ACCEPT_LABEL: Record<string, string> = {
  queued: "排队中",
  processing: "生成中",
  done: "已完成",
  failed: "生成失败",
  cancelled: "已作废",
  fixed: "已固化",
};

function renderMessages(): void {
  const list = $("messages");
  list.innerHTML = "";
  if (state.messages.length === 0) {
    const empty = document.createElement("li");
    empty.className = "empty muted";
    empty.textContent = "还没有对话。发一条消息试试。";
    list.appendChild(empty);
  }
  for (const message of state.messages) {
    const item = document.createElement("li");
    item.className = `message ${message.role}`;

    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${roleLabel(message.role)} · ${formatTime(message.time)}`;
    item.appendChild(meta);

    const body = document.createElement("div");
    body.className = "body";
    const segments = message.role === "character" ? message.parts : [message.text];
    for (const segment of segments) {
      const block = document.createElement("p");
      block.textContent = segment;
      body.appendChild(block);
    }
    item.appendChild(body);

    const chips = document.createElement("div");
    chips.className = "chips";
    if (message.role === "user") {
      chips.appendChild(chip(ACCEPT_LABEL[message.state] ?? message.state, message.state === "failed" ? "bad" : ""));
    }
    if (message.role === "character" && message.delivery) {
      chips.appendChild(chip(`投递：${message.delivery}`, message.delivery === "delivered" ? "ok" : ""));
    }
    if (message.role === "user" && message.state === "failed") {
      const retry = document.createElement("button");
      retry.className = "link";
      retry.textContent = "重试";
      retry.onclick = () => retryMessage(message);
      chips.appendChild(retry);
      if (message.errorCode) chips.appendChild(chip(message.errorCode, "bad"));
    }
    if (chips.childElementCount > 0) item.appendChild(chips);
    list.appendChild(item);
  }

  if (state.thinking) {
    const item = document.createElement("li");
    item.className = "message character thinking";
    item.textContent = "思考中…";
    list.appendChild(item);
  }
  list.scrollTop = list.scrollHeight;
}

function chip(text: string, kind: string): HTMLElement {
  const element = document.createElement("span");
  element.className = `chip small ${kind}`;
  element.textContent = text;
  return element;
}

function renderFacts(target: HTMLElement, facts: Array<[string, string]>): void {
  target.innerHTML = "";
  for (const [key, value] of facts) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    target.append(dt, dd);
  }
}

/* ---------- 核心连接 ---------- */

async function waitForCore(): Promise<CoreStatus> {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    const status = await invoke<CoreStatus>("core_status");
    shellStatus = status;
    if (status.state !== "starting") return status;
    setStatus("启动中…（等待核心就绪握手）", "pending");
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  return { state: "failed", error: "等待核心就绪超时" };
}

function toMessage(row: HistoryRow): Message {
  const isCharacter = row.role === "character";
  return {
    role: (row.role === "user" ? "user" : row.role === "notice" ? "notice" : "character") as Message["role"],
    text: row.text ?? "",
    parts: row.parts ? row.parts.flat() : [],
    envId: row.env_id ?? undefined,
    // 只有出站消息持有稳定标识；入站行记录的是指向回复的引用，不能混用
    messageId: isCharacter ? (row.message_id ?? undefined) : undefined,
    seenBatches: isCharacter ? (row.parts ?? []).map((_, index) => index) : undefined,
    state: row.state,
    time: row.created_at,
  };
}

async function connectChat(status: CoreStatus): Promise<void> {
  if (!status.endpoint || !status.mgmt) {
    setStatus(`核心未就绪：${status.error ?? status.state}`, "bad");
    return;
  }
  mgmt = new MgmtClient(status.endpoint, status.mgmt);
  await mgmt.connect();

  const overview = await mgmt.call("status");
  const placeholder = overview.placeholder as Record<string, string>;
  const sessions = (overview.sessions as Array<Record<string, string>>) ?? [];
  let session = sessions.find(
    (item) =>
      item.instance_id === placeholder.instance_id &&
      item.timeline_id === placeholder.timeline_id &&
      item.character_id === placeholder.character_id,
  );
  if (!session) {
    session = ((await mgmt.call("session.ensure", placeholder)).session ?? {}) as Record<string, string>;
  }
  state.sessionId = String(session.id ?? "");
  renderSessionList(session);

  const issued = await mgmt.call("channel.ensure", { name: "builtin", version: "0.1.0" });
  let credential = (issued.credential as string | null) ?? localStorage.getItem("isekai.credential");
  if (!credential) {
    const rotated = await mgmt.call("channel.ensure", { name: "builtin", rotate: true });
    credential = rotated.credential as string;
  }
  localStorage.setItem("isekai.credential", credential);

  const thread = ((await mgmt.call("thread.bind", {
    channel: "builtin",
    thread_id: state.threadId,
    session_id: state.sessionId,
  })).thread ?? {}) as Record<string, unknown>;
  state.token = String(thread.binding_token ?? "");

  ump = new UmpClient(status.endpoint, "builtin", "内建聊天窗口");
  ump.onMessage(onEnvelope);
  try {
    await ump.connect({ credential });
  } catch {
    // 持久凭据失效：退回一次性引导凭据重新登记（受信启动通路）
    const fresh = new UmpClient(status.endpoint, "builtin", "内建聊天窗口");
    fresh.onMessage(onEnvelope);
    const ack = await fresh.connect({ bootstrap: status.bootstrap ?? null });
    if (ack.credential) localStorage.setItem("isekai.credential", String(ack.credential));
    ump = fresh;
  }

  await loadHistory();
  state.phase = "ready";
  renderTopbar();
  setStatus("已就绪", "ok");
  renderManagePane(overview);
}

async function loadHistory(): Promise<void> {
  if (!mgmt) return;
  const page = await mgmt.call("history.page", { session_id: state.sessionId, limit: 200 });
  const rows = (page.messages as HistoryRow[]) ?? [];
  state.messages = rows.map(toMessage);
  renderMessages();
}

function renderSessionList(session: Record<string, unknown>): void {
  const list = $("sessions");
  list.innerHTML = "";
  const item = document.createElement("li");
  item.textContent = `${session.instance_id} / ${session.timeline_id} / ${session.character_id}`;
  item.title = `阶段 0 占位会话（${session.id}）`;
  list.appendChild(item);
}

function renderManagePane(overview: Record<string, unknown>): void {
  const counts = (overview.counts as Record<string, number>) ?? {};
  renderFacts($("manage-facts"), [
    ["应用版本", String(overview.app ?? "-")],
    ["数据格式版本", String(overview.data_format ?? "-")],
    ["世界规则版本", String(overview.rules ?? "-")],
    ["协议版本", String(overview.ump ?? "-")],
    ["核心状态", String(overview.state ?? "-")],
    ["端点", String(overview.endpoint ?? "-")],
    ["会话/线程/通道", `${counts.sessions ?? 0} / ${counts.threads ?? 0} / ${counts.channels ?? 0}`],
    ["消息条数", String(counts.messages ?? 0)],
  ]);
}

/* ---------- 协议事件 ---------- */

function findUserMessage(envId: string | undefined): Message | undefined {
  if (!envId) return undefined;
  return [...state.messages].reverse().find((item) => item.envId === envId);
}

function onEnvelope(env: Envelope): void {
  const payload = env.payload as Record<string, unknown>;
  if (env.type === "accepted") {
    const target = findUserMessage(payload.ref as string);
    if (target) {
      target.state = String(payload.state ?? target.state);
      target.messageId = (payload.message_id as string | null) ?? target.messageId;
      renderMessages();
    }
    return;
  }
  if (env.type === "status") {
    state.thinking = payload.state === "thinking";
    renderMessages();
    return;
  }
  if (env.type === "reply") {
    mergeReply(payload);
    return;
  }
  if (env.type === "error") {
    const ref = payload.ref as string | undefined;
    const target = findUserMessage(ref);
    if (target) {
      target.state = "failed";
      target.errorCode = String(payload.code ?? "error");
    } else if (ref) {
      const delivered = state.messages.find((item) => item.messageId === ref);
      if (delivered) delivered.delivery = String(payload.code ?? "投递失败");
    }
    state.thinking = false;
    renderMessages();
  }
}

function mergeReply(payload: Record<string, unknown>): void {
  const messageId = String(payload.message_id ?? "");
  const batchIndex = Number(payload.batch_index ?? 0);
  const parts = ((payload.parts as Array<{ text: string }>) ?? []).map((part) => part.text);
  let message = state.messages.find((item) => item.role === "character" && item.messageId === messageId);
  if (!message) {
    message = {
      role: "character",
      text: "",
      parts: [],
      messageId,
      seenBatches: [],
      state: "fixed",
      time: Date.now() / 1000,
    };
    state.messages.push(message);
  }
  const seen = message.seenBatches ?? (message.seenBatches = []);
  if (!seen.includes(batchIndex)) {
    seen.push(batchIndex);
    message.parts.push(...parts); // 批次按序到达，重复投递不重复渲染
  }
  const inbound = findUserMessage(payload.reply_to as string);
  if (inbound) inbound.state = "done";
  state.thinking = false;
  renderMessages();
  if (ump && state.token) {
    ump.reportDelivery(state.threadId, state.token, messageId, batchIndex, "accepted");
  }
}

/* ---------- 交互 ---------- */

function sendMessage(text: string): void {
  if (!ump || state.phase !== "ready") return;
  const message: Message = { role: "user", text, parts: [], state: "queued", time: Date.now() / 1000 };
  try {
    message.envId = ump.userMessage(state.threadId, state.token, text);
  } catch (error) {
    message.state = "failed";
    message.errorCode = String(error);
  }
  state.messages.push(message);
  renderMessages();
}

function retryMessage(message: Message): void {
  if (!ump || !message.envId) return;
  message.state = "queued";
  message.errorCode = undefined;
  ump.retry(state.threadId, state.token, message.envId);
  renderMessages();
}

function bindComposer(): void {
  const form = $<HTMLFormElement>("composer");
  const input = $<HTMLTextAreaElement>("input");
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      form.requestSubmit();
    }
  });
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
  });
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    input.style.height = "auto";
    sendMessage(text);
  });
}

function bindNav(): void {
  for (const button of Array.from(document.querySelectorAll<HTMLButtonElement>("nav .nav"))) {
    button.addEventListener("click", () => {
      for (const other of Array.from(document.querySelectorAll<HTMLButtonElement>("nav .nav"))) {
        other.classList.toggle("active", other === button);
      }
      for (const pane of Array.from(document.querySelectorAll<HTMLElement>(".pane"))) {
        pane.classList.toggle("hidden", pane.id !== `pane-${button.dataset.pane}`);
      }
      if (button.dataset.pane === "settings") void loadSettings();
    });
  }
}

/* ---------- 设置面（走核心契约，不在壳里另存一份配置） ---------- */

interface SettingsPayload {
  llm: Record<string, unknown>;
  core: Record<string, unknown>;
}

function fillSettings(settings: SettingsPayload): void {
  $<HTMLInputElement>("set-base-url").value = String(settings.llm.base_url ?? "");
  $<HTMLInputElement>("set-model").value = String(settings.llm.model ?? "");
  const key = $<HTMLInputElement>("set-api-key");
  key.value = "";
  key.placeholder = settings.llm.api_key_set ? `已配置：${settings.llm.api_key}` : "尚未配置";
  $<HTMLInputElement>("set-timeout").value = String(settings.llm.timeout_s ?? "");
  $<HTMLInputElement>("set-max-tokens").value = String(settings.llm.max_tokens ?? "");
  $<HTMLInputElement>("set-temperature").value = String(settings.llm.temperature ?? "");
  renderFacts($("settings-facts"), [
    ["配置文件", String(settings.core.config_file ?? "-")],
    ["单段上限 / 单批段数", `${settings.core.max_text_len} / ${settings.core.max_parts}`],
    ["上下文条数", String(settings.core.context_history_max ?? "-")],
  ]);
}

async function loadSettings(): Promise<void> {
  if (!mgmt) return;
  try {
    fillSettings((await mgmt.call("settings.get")) as unknown as SettingsPayload);
    $("settings-note").textContent = "";
  } catch (error) {
    $("settings-note").textContent = String(error);
  }
}

async function saveSettings(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!mgmt) return;
  const llm: Record<string, unknown> = {
    base_url: $<HTMLInputElement>("set-base-url").value.trim(),
    model: $<HTMLInputElement>("set-model").value.trim(),
    timeout_s: Number($<HTMLInputElement>("set-timeout").value),
    max_tokens: Number($<HTMLInputElement>("set-max-tokens").value),
    temperature: Number($<HTMLInputElement>("set-temperature").value),
  };
  const key = $<HTMLInputElement>("set-api-key").value.trim();
  if (key) llm.api_key = key;
  try {
    const saved = (await mgmt.call("settings.set", { llm })) as unknown as SettingsPayload;
    fillSettings(saved);
    $("settings-note").textContent = "已保存并生效";
  } catch (error) {
    $("settings-note").textContent = `保存失败（未改动）：${error}`;
  }
}

/* ---------- 启动 ---------- */

async function boot(): Promise<void> {
  bindComposer();
  bindNav();
  $("settings-form").addEventListener("submit", (event) => void saveSettings(event));
  $("settings-reload").addEventListener("click", () => void loadSettings());
  await listen("core-status", (event) => {
    shellStatus = event.payload as CoreStatus;
  });
  const status = await waitForCore();
  if (status.state !== "ready") {
    setStatus(`核心未就绪：${status.error ?? status.state}`, "bad");
    return;
  }
  try {
    await connectChat(status);
  } catch (error) {
    setStatus(`连接失败：${error}`, "bad");
  }
}

void boot();
