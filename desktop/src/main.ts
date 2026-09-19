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
let reconnectAttempt = 0;
let reconnectToken = 0; // 递增即作废在途的重连链（例如同时发生了核心重启）

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

//: 有界退避重连（§六：核心暂时不可用 / 网络断线时保留历史，不清空）
const RECONNECT_DELAYS_MS = [1000, 2000, 4000, 8000, 16000];

function showRestart(): void {
  $("restart").classList.remove("hidden");
}

function hideRestart(): void {
  $("restart").classList.add("hidden");
}

function applyHello(ack: Record<string, unknown>): void {
  // 握手回带已有 thread 令牌：重连不必再问管理面（§2.2）
  const threads = (ack.threads as Array<{ id: string; binding_token: string }>) ?? [];
  const mine = threads.find((item) => item.id === state.threadId);
  if (mine) state.token = mine.binding_token;
}

async function openChannel(endpoint: string, opts: { credential?: string | null; bootstrap?: string | null }): Promise<void> {
  ump?.close(); // 旧连接（若有）先关：避免同一通道挂两条连接
  const client = new UmpClient(endpoint, "builtin", "内建聊天窗口");
  client.onMessage(onEnvelope);
  client.onClose(onChannelClosed);
  let ack: Record<string, unknown>;
  try {
    ack = await client.connect(opts);
  } catch (error) {
    if (!opts.bootstrap) throw error;
    // 持久凭据失效：退回一次性引导凭据重新登记（受信启动通路）
    const fresh = new UmpClient(endpoint, "builtin", "内建聊天窗口");
    fresh.onMessage(onEnvelope);
    fresh.onClose(onChannelClosed);
    ack = await fresh.connect({ bootstrap: opts.bootstrap });
    ump = fresh;
    client.close();
    if (ack.credential) localStorage.setItem("isekai.credential", String(ack.credential));
    applyHello(ack);
    return;
  }
  ump = client;
  if (ack.credential) localStorage.setItem("isekai.credential", String(ack.credential));
  applyHello(ack);
}

function onChannelClosed(): void {
  if (state.phase !== "ready") return;
  state.phase = "starting";
  setStatus("连接已断开，正在重连…", "pending");
  void scheduleReconnect();
}

async function scheduleReconnect(): Promise<void> {
  const mine = reconnectToken;
  if (!shellStatus?.endpoint) {
    setStatus("核心未在运行，可使用「重启核心」", "bad");
    showRestart();
    return;
  }
  if (reconnectAttempt >= RECONNECT_DELAYS_MS.length) {
    setStatus("重连失败：核心可能已退出（可重启核心）", "bad");
    showRestart();
    return;
  }
  const delay = RECONNECT_DELAYS_MS[reconnectAttempt];
  reconnectAttempt += 1;
  await new Promise((resolve) => setTimeout(resolve, delay));
  if (mine !== reconnectToken) return; // 已被新的连接流程接管
  try {
    await openChannel(shellStatus.endpoint, {
      credential: localStorage.getItem("isekai.credential"),
      bootstrap: shellStatus.bootstrap ?? null,
    });
    await loadHistory(); // 重连后以核心持久记录为准重载
    state.phase = "ready";
    reconnectAttempt = 0;
    setStatus("已重新连接", "ok");
    hideRestart();
  } catch (error) {
    setStatus(`重连中…（第 ${reconnectAttempt} 次失败：${error}）`, "pending");
    void scheduleReconnect();
  }
}

async function restartCore(): Promise<void> {
  setStatus("重启核心中…", "pending");
  hideRestart();
  state.phase = "starting";
  reconnectToken += 1; // 作废在途重连链
  try {
    await invoke("core_restart");
  } catch (error) {
    setStatus(`重启失败：${error}`, "bad");
    showRestart();
    return;
  }
  const status = await waitForCore();
  if (status.state !== "ready") {
    setStatus(`核心未就绪：${status.error ?? status.state}`, "bad");
    showRestart();
    return;
  }
  try {
    await connectChat(status);
  } catch (error) {
    setStatus(`连接失败：${error}`, "bad");
    showRestart();
  }
}

async function connectChat(status: CoreStatus): Promise<void> {
  if (!status.endpoint || !status.mgmt) {
    setStatus(`核心未就绪：${status.error ?? status.state}`, "bad");
    showRestart();
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

  state.token = "";
  await openChannel(status.endpoint, { credential, bootstrap: status.bootstrap ?? null });
  if (!state.token) {
    // 该通道尚无此 thread 的绑定：由受信管理面创建（阶段 0 的占位会话）
    const thread = ((await mgmt.call("thread.bind", {
      channel: "builtin",
      thread_id: state.threadId,
      session_id: state.sessionId,
    })).thread ?? {}) as Record<string, unknown>;
    state.token = String(thread.binding_token ?? "");
  }

  await loadHistory();
  state.phase = "ready";
  reconnectAttempt = 0;
  reconnectToken += 1;
  renderTopbar();
  hideRestart();
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
  if (env.type === "binding") {
    // 管理面换代通知：更新令牌并按真值重载历史（旧令牌的视图作废）
    if (String(payload.thread_id ?? "") === state.threadId) {
      state.token = String(payload.binding_token ?? state.token);
      void loadHistory().then(() => setStatus("已就绪", "ok"));
      setStatus("绑定已更新，正在重载历史…", "pending");
    }
    return;
  }
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
      if (button.dataset.pane === "manage") void loadWorld();
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

/* ---------- 世界管理面（阶段 1：世界包 / 角色卡 / 实例 / 导入导出） ---------- */

interface PackageEntry {
  file: string;
  name: string | null;
  density?: string;
  valid: boolean;
  errors?: string[];
}
interface CardEntry {
  file: string;
  name: string | null;
  confirmed: boolean;
}
interface InstanceEntry {
  id: string;
  name: string;
  original_name: string;
  moment: number;
  timelines: number;
  sessions: number;
  imported: boolean;
}

interface WorldCache {
  packages: PackageEntry[];
  cards: CardEntry[];
  instances: InstanceEntry[];
  containers: Array<{ file: string }>;
}

const world: WorldCache = { packages: [], cards: [], instances: [], containers: [] };
const GENERATE_TIMEOUT_MS = 600000;

function fillSelect(select: HTMLSelectElement, entries: Array<[string, string]>): void {
  const previous = select.value;
  select.innerHTML = "";
  for (const [value, label] of entries) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    select.appendChild(option);
  }
  if (entries.some(([value]) => value === previous)) select.value = previous;
}

function worldNote(text: string, bad = false): void {
  const note = $("world-note");
  note.textContent = text;
  note.className = bad ? "muted bad" : "muted";
}

function showErrors(target: string, errors: string[] | undefined): void {
  $(target).textContent = errors && errors.length ? `未通过校验：\n${errors.map((item) => `· ${item}`).join("\n")}` : "";
}

async function loadWorld(): Promise<void> {
  if (!mgmt) return;
  try {
    const [pkgs, cards, instances] = await Promise.all([
      mgmt.call("world.package.list"),
      mgmt.call("world.card.list"),
      mgmt.call("instance.list"),
    ]);
    world.packages = (pkgs.packages ?? []) as unknown as PackageEntry[];
    world.containers = (pkgs.containers ?? []) as unknown as Array<{ file: string }>;
    world.cards = (cards.cards ?? []) as unknown as CardEntry[];
    world.instances = (instances.instances ?? []) as unknown as InstanceEntry[];
    worldNote("");
  } catch (error) {
    worldNote(String(error), true);
    return;
  }
  const packageOptions = world.packages.map((item) => [
    item.file,
    `${item.file}｜${item.name ?? "未命名"}${item.valid ? "" : "（未通过校验）"}`,
  ]);
  const cardOptions = world.cards.map((item) => [
    item.file,
    `${item.file}｜${item.name ?? "未命名"}${item.confirmed ? "" : "（未确认）"}`,
  ]);
  const instanceOptions = world.instances.map((item) => [
    item.id,
    `${item.name}｜${item.timelines} 线 / ${item.sessions} 会话｜时刻 ${item.moment}`,
  ]);
  fillSelect($<HTMLSelectElement>("pkg-select"), packageOptions as Array<[string, string]>);
  fillSelect($<HTMLSelectElement>("card-select"), cardOptions as Array<[string, string]>);
  fillSelect($<HTMLSelectElement>("inst-select"), instanceOptions as Array<[string, string]>);
  fillSelect($<HTMLSelectElement>("inst-package-select"), packageOptions as Array<[string, string]>);
  fillSelect($<HTMLSelectElement>("inst-cards-select"), cardOptions as Array<[string, string]>);
  const importOptions = world.containers.map((item) => [item.file, item.file] as [string, string]);
  fillSelect($<HTMLSelectElement>("import-select"), importOptions);
  const selected = $<HTMLSelectElement>("inst-select").value;
  if (selected) void showInstance(selected);
  else renderFacts($("world-facts"), [["实例", "还没有实例"]]);
}

async function showInstance(instanceId: string): Promise<void> {
  if (!mgmt) return;
  try {
    const detail = await mgmt.call("instance.info", { id: instanceId });
    const info = detail.instance as unknown as InstanceEntry;
    const characters = (detail.characters ?? []) as Array<Record<string, string>>;
    const timelines = (detail.timelines ?? []) as Array<Record<string, string>>;
    renderFacts($("world-facts"), [
      ["实例", `${info.name}（原始名称：${info.original_name}${info.imported ? "，导入" : ""}）`],
      ["初始世界时刻", `${info.moment} 世界秒`],
      ["角色", characters.map((item) => `${item.name}｜${item.occupation}`).join("；") || "无"],
      ["时间线", timelines.map((item) => `${item.name}（${item.state === "frozen" ? "冻结" : "激活"}）`).join("；")],
      ["世界内部", "不可浏览：管理面只暴露元数据与公开时钟"],
    ]);
  } catch (error) {
    worldNote(String(error), true);
  }
}

async function worldAction(action: () => Promise<string | void>): Promise<void> {
  try {
    const message = await action();
    await loadWorld();
    if (message) worldNote(message);
  } catch (error) {
    worldNote(String(error), true);
  }
}

function bindWorld(): void {
  $("world-refresh").addEventListener("click", () => void loadWorld());
  $<HTMLSelectElement>("inst-select").addEventListener("change", (event) => {
    void showInstance((event.target as HTMLSelectElement).value);
  });

  $("pkg-check").addEventListener("click", () =>
    void worldAction(async () => {
      const file = $<HTMLSelectElement>("pkg-select").value;
      if (!file) return "还没有世界包";
      const result = await mgmt!.call("world.package.validate", { path: file });
      const errors = (result.errors ?? []) as string[];
      showErrors("pkg-errors", errors);
      return errors.length ? "" : `${file} 通过校验`;
    }),
  );

  $("pkg-template").addEventListener("click", () =>
    void worldAction(async () => {
      const file = $<HTMLInputElement>("pkg-file").value.trim();
      if (!file) throw new Error("先填一个文件名");
      const created = await mgmt!.call("world.package.template", { name: file.replace(/\.json$/, "") });
      await mgmt!.call("world.package.save", { path: file, package: created.package });
      showErrors("pkg-errors", created.errors as string[]);
      return `已写入 ${file}（骨架还需填内容）`;
    }),
  );

  $("pkg-generate").addEventListener("click", () =>
    void worldAction(async () => {
      const brief = $<HTMLInputElement>("pkg-brief").value.trim();
      if (!brief) throw new Error("先写一段世界描述");
      const file = $<HTMLInputElement>("pkg-file").value.trim() || "world.json";
      worldNote("AI 生成中，可能需要一两分钟…");
      const result = await mgmt!.call(
        "world.package.generate",
        { brief, name: $<HTMLInputElement>("pkg-name").value.trim() || file.replace(/\.json$/, "") },
        GENERATE_TIMEOUT_MS,
      );
      const errors = (result.errors ?? []) as string[];
      if (errors.length) {
        showErrors("pkg-errors", errors);
        return "生成结果未通过校验，未写入";
      }
      await mgmt!.call("world.package.save", { path: file, package: result.candidate });
      showErrors("pkg-errors", []);
      return `已生成并写入 ${file}`;
    }),
  );

  $("card-template").addEventListener("click", () =>
    void worldAction(async () => {
      const pkg = $<HTMLSelectElement>("pkg-select").value;
      const file = $<HTMLInputElement>("card-file").value.trim() || "card.json";
      if (!pkg) throw new Error("先选一个世界包");
      const created = await mgmt!.call("world.card.template", {
        package_path: pkg,
        name: $<HTMLInputElement>("card-name-input").value.trim() || "未命名角色",
      });
      await mgmt!.call("world.card.save", { card_path: file, card: created.card });
      return `已写入 ${file}（骨架未确认）`;
    }),
  );

  $("card-generate").addEventListener("click", () =>
    void worldAction(async () => {
      const pkg = $<HTMLSelectElement>("pkg-select").value;
      const brief = $<HTMLInputElement>("card-brief").value.trim();
      const file = $<HTMLInputElement>("card-file").value.trim() || "card.json";
      if (!pkg) throw new Error("先选一个世界包");
      if (!brief) throw new Error("先写一段角色描述");
      worldNote("AI 生成角色卡中…");
      const result = await mgmt!.call(
        "world.card.generate",
        { package_path: pkg, brief },
        GENERATE_TIMEOUT_MS,
      );
      const errors = (result.errors ?? []) as string[];
      if (errors.length) {
        showErrors("card-errors", errors);
        return "生成结果未通过校验，未写入";
      }
      await mgmt!.call("world.card.save", { card_path: file, card: result.candidate });
      showErrors("card-errors", []);
      return `已生成并写入 ${file}（仍需确认）`;
    }),
  );

  $("card-confirm").addEventListener("click", () =>
    void worldAction(async () => {
      const pkg = $<HTMLSelectElement>("pkg-select").value;
      const card = $<HTMLSelectElement>("card-select").value;
      if (!pkg || !card) throw new Error("先选世界包与角色卡");
      await mgmt!.call("world.card.confirm", { package_path: pkg, card_path: card });
      showErrors("card-errors", []);
      return `${card} 已确认，可用于创建实例`;
    }),
  );

  $("inst-create").addEventListener("click", () =>
    void worldAction(async () => {
      const pkg = $<HTMLSelectElement>("inst-package-select").value;
      const cards = Array.from($<HTMLSelectElement>("inst-cards-select").selectedOptions).map(
        (option) => option.value,
      );
      if (!pkg) throw new Error("先选世界包");
      if (!cards.length) throw new Error("至少选一张已确认的角色卡");
      const result = await mgmt!.call("instance.create", {
        package_path: pkg,
        card_paths: cards,
        ...($<HTMLInputElement>("inst-name-input").value.trim()
          ? { display_name: $<HTMLInputElement>("inst-name-input").value.trim() }
          : {}),
      });
      const info = result.instance as unknown as InstanceEntry;
      return `已创建实例「${info.name}」（默认冻结）`;
    }),
  );

  $("inst-rename-btn").addEventListener("click", () =>
    void worldAction(async () => {
      const id = $<HTMLSelectElement>("inst-select").value;
      const name = $<HTMLInputElement>("inst-rename-input").value.trim();
      if (!id || !name) throw new Error("先选实例并填新名称");
      await mgmt!.call("instance.rename", { id, name });
      return `已重命名为「${name}」`;
    }),
  );

  $("inst-export").addEventListener("click", () =>
    void worldAction(async () => {
      const id = $<HTMLSelectElement>("inst-select").value;
      if (!id) throw new Error("先选实例");
      const entry = world.instances.find((item) => item.id === id);
      const file = `${(entry?.name ?? id).replace(/[^\w\u4e00-\u9fa5-]/g, "_")}.isekai.json`;
      const result = await mgmt!.call("instance.export", { id, path: file });
      const manifest = result.manifest as Record<string, unknown>;
      return `已导出 ${file}（设置 + ${JSON.stringify(manifest.counts ?? {})}）`;
    }),
  );

  $("inst-import").addEventListener("click", () =>
    void worldAction(async () => {
      const file = $<HTMLSelectElement>("import-select").value;
      if (!file) throw new Error("创作目录里没有导出件");
      const result = await mgmt!.call("instance.import", { path: file });
      const info = result.instance as unknown as InstanceEntry;
      return `已导入为「${info.name}」（默认冻结）`;
    }),
  );

  $("inst-delete").addEventListener("click", () =>
    void worldAction(async () => {
      const id = $<HTMLSelectElement>("inst-select").value;
      if (!id) throw new Error("先选实例");
      const entry = world.instances.find((item) => item.id === id);
      if (!window.confirm(`删除实例「${entry?.name ?? id}」及其对话？此操作不可撤销。`)) return "";
      await mgmt!.call("instance.delete", { id });
      return "已删除";
    }),
  );
}

/* ---------- 启动 ---------- */

async function boot(): Promise<void> {
  bindComposer();
  bindNav();
  bindWorld();
  $("settings-form").addEventListener("submit", (event) => void saveSettings(event));
  $("settings-reload").addEventListener("click", () => void loadSettings());
  $("restart").addEventListener("click", () => void restartCore());
  await listen("core-status", (event) => {
    shellStatus = event.payload as CoreStatus;
    if (shellStatus.state === "ready") return;
    state.phase = "starting";
    if (shellStatus.state === "persistence_blocked") {
      setStatus(`存储不可用：${shellStatus.error ?? ""}`, "bad");
    } else {
      setStatus(`核心未就绪：${shellStatus.error ?? shellStatus.state}`, "bad");
    }
    showRestart();
  });
  const status = await waitForCore();
  if (status.state !== "ready") {
    setStatus(`核心未就绪：${status.error ?? status.state}`, "bad");
    showRestart();
    return;
  }
  try {
    await connectChat(status);
  } catch (error) {
    setStatus(`连接失败：${error}`, "bad");
    showRestart();
  }
}

void boot();
