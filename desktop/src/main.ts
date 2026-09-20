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

/// 会话行（session 表）：一个角色一条；切换会话 = 换一份历史，不迁移内容（DESKTOP_SPEC §3.1）
interface SessionRow {
  id: string;
  instance_id: string;
  timeline_id: string;
  character_id: string;
}

/// 阶段 0 的占位三元组：老数据与老入口还在用，启动时保证它存在，但聊天面走真实会话
const STAGE0 = { instance_id: "ph-instance", timeline_id: "main", character_id: "ph-character" };

/// 本地配置文件里的只读事实（§3.3 记忆语义召回 / 提交 / 世界·会话 组）：
/// 核心 settings 契约只有 llm + core 两段，这几个键由壳读本地配置展示可读值；
/// 凭据只回「是否已配置」，明文不进渲染层（§二.6）。
interface LocalFacts {
  config_file: string;
  mtime: number;
  packages_dir: string;
  memory_model: string;
  memory_base_url: string;
  memory_key_set: boolean;
  commit_enabled: boolean | null;
  commit_minutes: number | null;
  commit_events: number | null;
  max_active_timelines: number | null;
  rate_max: number | null;
  render_calls_per_day: number | null;
}

const $ = <T extends HTMLElement>(id: string): T => document.getElementById(id) as T;

const state = {
  phase: "starting" as "starting" | "ready" | "stopping" | "failed",
  threadId: "main",
  token: "",
  sessionId: "",
  characterId: "",
  //: 当前聊天会话所属实例 / 时间线：顶栏与历史读同一份事实，不留上一次的实例（§3.1）
  instanceId: "",
  timelineId: "",
  sessions: [] as SessionRow[],
  //: 恢复后全部世界线冻结：先在管理面激活再对话（§五 / §十.20）
  needActivate: false,
  //: 当前实例的线还没激活：聊天先落在内建初始会话上，界面要说清楚（§3.1 冻结线提示需激活）
  idleLine: false,
  facts: null as LocalFacts | null,
  endpoint: "",
  credential: null as string | null,
  bootstrap: null as string | null,
  messages: [] as Message[],
  thinking: false,
  //: 历史分页：只取最新一页，更早的按 before_seq 续取（DESKTOP_SPEC §3.1）
  hasMore: false,
  oldestSeq: 0,
  loadingMore: false,
  //: 壳给的日志目录：未就绪 / 超时文案里要点出来（§2「等待超时给日志位置」）
  logDir: "",
  backupDir: "",
};

const HISTORY_PAGE = 200;

let ump: UmpClient | null = null;
let mgmt: MgmtClient | null = null;
let shellStatus: CoreStatus | null = null;
let reconnectAttempt = 0;
let reconnectToken = 0; // 递增即作废在途的重连链（例如同时发生了核心重启）

/* ---------- 渲染 ---------- */

function logHint(): string {
  return state.logDir ? `；日志：${state.logDir}` : "";
}

function formatSize(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${bytes} B`;
}

function setStatus(text: string, kind: "pending" | "ok" | "bad"): void {
  const chip = $("status");
  chip.textContent = text;
  chip.className = `chip ${kind}`;
}

/// 核心状态 → 状态条文案（DESKTOP_SPEC §2.8 / §5.7：非 ready 也要给可执行提示，不能都变成「未就绪」）
function coreStatusText(state: string, error?: string | null): { text: string; kind: "pending" | "ok" | "bad" } {
  if (state === "ready") return { text: "已就绪", kind: "ok" };
  if (state === "persistence_blocked") {
    return { text: `存储不可用：${error ?? ""}${logHint()}`, kind: "bad" };
  }
  if (state === "compatibility_blocked") {
    // 兼容性阻断：核心整体只读，管理面仍可用（导出 / 按兼容版本处理后重启），不接受新对话
    return {
      text: `兼容性阻断：有实例与当前规则 / 数据格式不兼容，核心整体只读；管理面仍可用（导出或按提示处理后「重启核心」）${error ? `：${error}` : ""}`,
      kind: "bad",
    };
  }
  if (state === "catching_up") return { text: `某条线追赶中：${error ?? ""}`, kind: "pending" };
  if (state === "starting") return { text: "启动中…（等待核心就绪握手）", kind: "pending" };
  return { text: `核心未就绪：${error ?? state}${logHint()}`, kind: "bad" };
}

/// 核心是否处于可接受新对话的 ready 态（非 ready 一律不提交，§2.2）
function coreReady(): boolean {
  return (shellStatus?.state ?? "starting") === "ready";
}

function isStage0(row: { instance_id?: string }): boolean {
  return String(row.instance_id ?? "") === STAGE0.instance_id;
}

function timelineName(timelineId: string): string {
  return world.timelines.find((item) => item.id === timelineId)?.name ?? timelineId;
}

function sessionLabel(row: SessionRow): string {
  if (isStage0(row)) return "初始会话（阶段 0）";
  return `${currentCharacterName(row.character_id)} · ${timelineName(row.timeline_id)}`;
}

/// 顶栏 = 当前上下文（实例 / 角色 / 时间线），名称以核心返回的为准（§3.1）；
/// 内建初始会话不在实例表里时，给当前查看的实例并标明聊天落在哪个会话。
function sessionTitle(): string {
  if (!state.sessionId) return "未选择会话";
  const instance = world.instances.find((item) => item.id === state.instanceId);
  if (instance) {
    return `${instance.name}（${instance.id}）· ${currentCharacterName(state.characterId)} · ${timelineName(state.timelineId)}`;
  }
  const viewing = world.instances.find((item) => item.id === world.instanceId);
  return viewing
    ? `${viewing.name}（${viewing.id}）· 聊天：初始会话（阶段 0）`
    : "初始会话（阶段 0）";
}

function renderTopbar(): void {
  $("title").textContent = shellStatus?.state === "ready" ? sessionTitle() : "未连接";
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

function renderMessages(anchor?: number): void {
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
    if (message.role === "character" && message.messageId && state.characterId) {
      // 披露入口挂在消息上：沿已有对话选片段（DESKTOP_SPEC §6）
      const disclose = document.createElement("button");
      disclose.className = "link";
      disclose.textContent = "披露";
      disclose.title = "把这条片段披露给另一个角色";
      disclose.addEventListener("click", () =>
        selectForDisclosure(
          String(message.messageId),
          state.characterId,
          world.characters.find((item) => item.card_id === state.characterId)?.name ?? state.characterId,
        ),
      );
      chips.appendChild(disclose);
    }
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
  // anchor = 渲染前「距底部」的距离：更早的消息接在前面时用它把视口钉在原处，不让视图跳走
  list.scrollTop = anchor === undefined ? list.scrollHeight : list.scrollHeight - anchor;
}

function chip(text: string, kind: string): HTMLElement {
  const element = document.createElement("span");
  element.className = `chip small ${kind}`;
  element.textContent = text;
  return element;
}

function renderFacts(target: HTMLElement | null, facts: Array<[string, string]>): void {
  if (!target) {
    // 缺节点只记一笔，绝不让渲染把连接 / 聊天主路径拖死
    console.warn("renderFacts: 目标节点不存在");
    return;
  }
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
  return { state: "failed", error: `等待核心就绪超时（30 秒）${logHint()}` };
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
  if (state.phase !== "ready" || !chatEnabled()) return;
  state.phase = "starting";
  setStatus("连接已断开，正在重连…", "pending");
  void scheduleReconnect();
}

async function scheduleReconnect(): Promise<void> {
  const mine = reconnectToken;
  if (!shellStatus?.endpoint) {
    setStatus(`核心未在运行，可使用「重启核心」${logHint()}`, "bad");
    showRestart();
    return;
  }
  if (reconnectAttempt >= RECONNECT_DELAYS_MS.length) {
    setStatus(`重连失败：核心可能已退出（可重启核心）${logHint()}`, "bad");
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
  const info = coreStatusText(status.state, status.error);
  if (status.state === "ready" || status.state === "compatibility_blocked") {
    // 兼容性阻断也连管理面：核心只读但管理入口保留（§5.7）
    try {
      await connectChat(status);
    } catch (error) {
      setStatus(`${info.text}（管理面未连上：${error}）`, info.kind);
      showRestart();
    }
  } else {
    setStatus(info.text, info.kind);
    showRestart();
  }
}

/// 内建聊天开关（DESKTOP_SPEC §一 / CHANNEL_PLUGIN_SPEC）：可以停用内建聊天但保留管理面。
/// 状态存在壳自己的设置文件里（壳侧偏好，不动核心配置）；停用后不 channel.ensure、不连 UMP、也不提交对话。
const CHAT_FLAG = "chat_enabled";
let chatOn = true;

function chatEnabled(): boolean {
  return chatOn;
}

/// 读壳自己的设置（非 Tauri 环境按默认值走，不影响连接与聊天）
async function loadShellSettings(): Promise<void> {
  try {
    const settings = await invoke<Record<string, unknown>>("shell_settings");
    chatOn = settings[CHAT_FLAG] !== false;
  } catch (error) {
    console.warn(`壳设置读取失败：${error}`);
    chatOn = true;
  }
}

async function setChatEnabled(enabled: boolean): Promise<void> {
  chatOn = enabled;
  try {
    await invoke("shell_setting_set", { key: CHAT_FLAG, value: enabled });
  } catch (error) {
    $("chat-note").textContent = `壳设置写入失败：${error}`;
  }
}

/// 停用内建聊天：断开通道连接并作废绑定令牌（管理面不动）
function closeBuiltinChat(note = ""): void {
  ump?.close();
  ump = null;
  state.token = "";
  $("chat-note").textContent = note || "已停用：不登记 / 不连接聊天通道，管理面保留";
  renderComposeGate();
}

/// 启用内建聊天：登记通道 → 绑定 thread → 连 UMP → 补读历史
async function openBuiltinChat(): Promise<boolean> {
  if (!mgmt) return false;
  const issued = await mgmt.call("channel.ensure", { name: "builtin", version: "0.1.0" });
  let credential = (issued.credential as string | null) ?? localStorage.getItem("isekai.credential");
  if (!credential) {
    credential = (await mgmt.call("channel.ensure", { name: "builtin", rotate: true })).credential as string;
  }
  localStorage.setItem("isekai.credential", credential);
  state.credential = credential;
  $("chat-note").textContent = "已启用：对话走内建通道（builtin）";
  return openChat();
}

async function toggleBuiltinChat(enabled: boolean): Promise<void> {
  await setChatEnabled(enabled);
  if (!enabled) {
    closeBuiltinChat();
    setStatus("已就绪（内建聊天已停用：管理面仍可用）", "ok");
    return;
  }
  if (!mgmt || !coreReady()) {
    $("chat-note").textContent = "已记录启用：核心就绪后连上内建通道";
    renderComposeGate();
    return;
  }
  try {
    const opened = await openBuiltinChat();
    renderComposeGate();
    setStatus(opened ? "已就绪" : "已就绪（还没有世界实例：先在管理面创建）", "ok");
  } catch (error) {
    $("chat-note").textContent = `启用失败：${error}`;
  }
}

async function connectChat(status: CoreStatus): Promise<void> {
  if (!status.endpoint || !status.mgmt) {
    setStatus(`核心未就绪：${status.error ?? status.state}${logHint()}`, "bad");
    showRestart();
    return;
  }
  mgmt = new MgmtClient(status.endpoint, status.mgmt);
  await mgmt.connect();
  state.endpoint = status.endpoint;
  state.bootstrap = status.bootstrap ?? null;

  const overview = await mgmt.call("status");
  //: 阶段 0 的占位会话继续存在（老数据 / 老入口还在用），也是「线还没激活」时的内建会话
  await mgmt.call("session.ensure", STAGE0);

  await loadWorld(); // 世界数据 + 当前实例详情：顶栏 / 侧栏 / 管理面都靠它
  let opened = false;
  if (chatEnabled()) {
    opened = await openBuiltinChat(); // 登记通道 + 绑定 + 连 UMP + 补读历史
  } else {
    // 停用内建聊天：只接管理面（世界数据、会话列表与备份照常可读），不建聊天通道连接
    ump?.close();
    ump = null;
    state.token = "";
    closeBuiltinChat("已停用：不登记 / 不连接聊天通道，管理面保留");
    await loadSessions();
  }
  state.phase = "ready";
  reconnectAttempt = 0;
  reconnectToken += 1;
  renderTopbar();
  const info = coreStatusText(status.state, status.error);
  if (info.kind === "ok") hideRestart();
  else showRestart(); // 阻断态仍要留恢复入口（§5.7 / §2.9）
  const suffix = chatEnabled() && !opened ? "（还没有世界实例：先在管理面创建）" : "";
  setStatus(`${info.text}${suffix}`, info.kind);
  renderComposeGate();
  renderManagePane(overview);
}

async function loadHistory(): Promise<void> {
  if (!mgmt) return;
  const page = await mgmt.call("history.page", { session_id: state.sessionId, limit: HISTORY_PAGE });
  const rows = (page.messages as HistoryRow[]) ?? [];
  state.messages = rows.map(toMessage);
  state.hasMore = Boolean(page.has_more);
  state.oldestSeq = Number(page.next_before_seq ?? 0);
  renderMessages();
  renderHistoryMore();
}

function renderHistoryMore(): void {
  $("history-more").classList.toggle("hidden", !state.hasMore);
  $("history-note").textContent = state.hasMore ? `已加载最近 ${state.messages.length} 条` : "";
}

/// 更早的历史按 before_seq 续取，接在前面；视口按距底部距离锚定，不跳（§4「长历史分页」）
async function loadMoreHistory(): Promise<void> {
  const button = $<HTMLButtonElement>("history-more");
  if (!mgmt || !state.hasMore || state.loadingMore) return;
  state.loadingMore = true;
  button.disabled = true;
  $("history-note").textContent = "正在读取更早的记录…";
  const list = $("messages");
  const anchor = list.scrollHeight - list.scrollTop;
  try {
    const page = await mgmt.call("history.page", {
      session_id: state.sessionId,
      before_seq: state.oldestSeq,
      limit: HISTORY_PAGE,
    });
    const rows = (page.messages as HistoryRow[]) ?? [];
    state.messages = [...rows.map(toMessage), ...state.messages];
    state.hasMore = Boolean(page.has_more);
    state.oldestSeq = Number(page.next_before_seq ?? 0);
    renderMessages(anchor);
  } catch (error) {
    $("history-note").textContent = String(error);
  } finally {
    state.loadingMore = false;
    button.disabled = false;
    renderHistoryMore();
  }
}

/// 侧栏会话组：当前实例的会话（一个角色一条）+ 正在用的那条（含阶段 0 老会话）；点一条就换会话
function renderSessionList(): void {
  const list = $("sessions");
  list.innerHTML = "";
  const shown = state.sessions.filter(
    (row) => row.instance_id === world.instanceId || row.id === state.sessionId,
  );
  if (!shown.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = "还没有会话";
    list.appendChild(empty);
  }
  for (const row of shown) {
    const item = document.createElement("li");
    const button = document.createElement("button");
    button.type = "button";
    button.className = `session${row.id === state.sessionId ? " active" : ""}`;
    button.textContent = sessionLabel(row);
    button.title = `${sessionLabel(row)}（${row.id}）`;
    button.addEventListener("click", () => void switchSession(row));
    item.appendChild(button);
    list.appendChild(item);
  }
}

/// 侧栏世界组：当前实例的时间线与公开状态（不做世界内容浏览，§一）
function renderTimelines(): void {
  const list = $("timelines");
  list.innerHTML = "";
  if (!world.timelines.length) {
    const empty = document.createElement("li");
    empty.className = "muted";
    empty.textContent = world.instanceId ? "没有时间线" : "先选一个实例";
    list.appendChild(empty);
    return;
  }
  for (const line of world.timelines) {
    const item = document.createElement("li");
    item.textContent = `${line.name}（${line.state === "frozen" ? "冻结" : "激活"}）`;
    list.appendChild(item);
  }
}

/* ---------- 会话：选会话 / 换会话（§3.1 切换角色、世界、时间线就是选择另一会话，不迁移历史） ---------- */

/// 会话列表（含阶段 0 老会话）：侧栏与顶栏都读这一份真值
async function loadSessions(): Promise<void> {
  if (!mgmt) return;
  try {
    const listed = await mgmt.call("session.list");
    state.sessions = ((listed.sessions ?? []) as unknown as SessionRow[]).slice();
  } catch (error) {
    console.warn(`会话列表读取失败：${error}`);
  }
  renderSessionList();
}

/// 该会话最后一条消息的序号（没有消息 = -1）；序号是全局自增，可跨会话比较新旧
async function tailSeq(row: SessionRow): Promise<number> {
  if (!mgmt) return -1;
  try {
    const page = await mgmt.call("history.page", { session_id: row.id, limit: 1 });
    const rows = (page.messages ?? []) as HistoryRow[];
    return rows.length ? Number(rows[rows.length - 1].seq) : -1;
  } catch {
    return -1;
  }
}

/// 默认会话：当前查看实例的第一个角色 / 时间线——一句往来都没有时从这里开始
async function defaultSession(): Promise<SessionRow | null> {
  const instance = world.instances[0];
  if (!mgmt || !instance) return null;
  const loaded = await loadInstanceDetail(instance.id);
  const timeline = loaded?.timelines[0]?.id ?? "";
  const character = world.characters[0]?.card_id ?? "";
  if (!timeline || !character) return null;
  const ensured = await mgmt.call("session.ensure", {
    instance_id: instance.id,
    timeline_id: timeline,
    character_id: character,
  });
  return (ensured.session ?? null) as unknown as SessionRow | null;
}

/// 切换会话：换一份历史，不迁移内容；顶栏 / 侧栏 / 聊天历史跟着同一条事实更新
async function switchSession(row: SessionRow, connect = false): Promise<void> {
  if (!mgmt) return;
  if (!chatEnabled()) {
    // 内建聊天已停用：不绑定 thread、不连 UMP（管理面仍可用）
    closeBuiltinChat("已停用：不建立聊天通道连接；要换会话先在设置面启用内建聊天");
    return;
  }
  // 先按选中的会话把顶栏 / 侧栏 / 角色刷成目标值（切换立即生效），再异步绑定与补读
  state.sessionId = row.id;
  state.instanceId = row.instance_id;
  state.timelineId = row.timeline_id;
  state.characterId = row.character_id;
  renderTopbar();
  renderSessionList();
  const thread = ((await mgmt.call("thread.bind", {
    channel: "builtin",
    thread_id: state.threadId,
    session_id: row.id,
  })).thread ?? {}) as Record<string, unknown>;
  state.token = String(thread.binding_token ?? "");
  if (!isStage0(row)) {
    state.idleLine = false; // 用户明确切到了世界会话：不再提示「当前线未激活」
    renderComposeGate();
    const loaded = await loadInstanceDetail(row.instance_id);
    world.timeline = row.timeline_id; // 会话所在的那条线，不是实例的第一条线
    world.characterId = row.character_id;
    if (loaded) {
      renderRoleControls();
      renderTimelines();
      await refreshClock(row.instance_id, row.timeline_id);
    }
  }
  renderTopbar();
  if ((connect || !ump) && state.endpoint) {
    // 没有连接（首次进入 / 恢复后重来）时才建通道：换会话本身不重连，免得平白换代令牌
    await openChannel(state.endpoint, { credential: state.credential, bootstrap: state.bootstrap });
  }
  await loadHistory();
  await loadSessions();
  renderTopbar();
  hideRestart();
}

/// 回到最近有往来的会话；一句往来都没有时用默认会话（best<0 表示没有任何往来）
async function pickSessionRow(): Promise<{ target: SessionRow | null; fallback: SessionRow | null; best: number }> {
  if (!mgmt) return { target: null, fallback: null, best: -1 };
  const fallback = await defaultSession();
  await loadSessions();
  let target = fallback;
  let best = fallback ? await tailSeq(fallback) : -1;
  for (const row of state.sessions) {
    if (fallback && row.id === fallback.id) continue;
    const seq = await tailSeq(row);
    if (seq > best) {
      best = seq;
      target = row;
    }
  }
  return { target, fallback, best };
}

/// 打开聊天面：回到最近有往来的会话（阶段 0 的老会话也算候选）；
/// 一句往来都没有时，当前实例的线已激活就用它的第一个角色会话，线还没激活则用内建初始会话（始终可对话）。
async function openChat(): Promise<boolean> {
  if (!mgmt) return false;
  const { target: picked, fallback, best } = await pickSessionRow();
  let target = picked;
  state.idleLine = false;
  if (best < 0 && !(fallback && (await lineActive(fallback)))) {
    const stage0 = state.sessions.find(isStage0) ?? null;
    if (stage0) {
      target = stage0;
      state.idleLine = true;
    }
  }
  if (!target) {
    setStatus(`还没有可用的会话：先在管理面创建实例${logHint()}`, "bad");
    return false;
  }
  await switchSession(target, true);
  renderComposeGate();
  return true;
}

/// 重新握手：重新绑定 thread 并连回通道（恢复后旧令牌已失效；用户明确激活线之后才走这一步）
async function rehandshake(): Promise<void> {
  if (!mgmt || !chatEnabled() || !state.sessionId) return;
  try {
    const thread = ((await mgmt.call("thread.bind", {
      channel: "builtin",
      thread_id: state.threadId,
      session_id: state.sessionId,
    })).thread ?? {}) as Record<string, unknown>;
    state.token = String(thread.binding_token ?? "");
    await openChannel(state.endpoint, { credential: state.credential, bootstrap: state.bootstrap });
    await loadHistory();
    renderTopbar();
  } catch (error) {
    setStatus(`重新握手失败：${error}${logHint()}`, "bad");
    showRestart();
  }
}

/// 会话所在线是否可对话（已激活）；阶段 0 的内建会话没有时间线行，按可对话处理
async function lineActive(row: SessionRow): Promise<boolean> {
  if (!mgmt || isStage0(row)) return true;
  try {
    const clock = (await mgmt.call("runtime.clock", {
      instance_id: row.instance_id,
      timeline_id: row.timeline_id,
    })).clock as unknown as ClockView;
    return clock.state === "active";
  } catch {
    return false;
  }
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
  if (!ump || state.phase !== "ready" || !coreReady()) return;
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

/// 恢复后全部线冻结 / 当前线还没激活 / 核心阻断的提示：写在输入框上方，不冒充系统消息
function renderComposeGate(): void {
  const notes: string[] = [];
  if (state.needActivate) {
    notes.push("恢复后全部世界线已冻结：先到管理页「运行」里激活这条线，再继续对话。");
  } else if (state.idleLine && world.instances.length) {
    notes.push("当前实例的线还没激活：聊天先用内建初始会话；要跟角色对话，先在管理页激活这条线。");
  }
  if (!chatEnabled()) {
    notes.push("内建聊天已停用：不建立聊天通道连接；可在设置面重新启用。");
  } else if (shellStatus?.state === "compatibility_blocked") {
    notes.push("核心因兼容性阻断处于只读：管理面仍可用，按提示处理后再「重启核心」。");
  } else if (shellStatus?.state === "persistence_blocked") {
    notes.push("存储不可用：先恢复数据目录可写，再由核心从最后完整水位继续。");
  }
  $("composer-note").textContent = notes.join(" ");
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
    if (state.needActivate || !coreReady() || !chatEnabled()) {
      // 恢复后全部线冻结 / 核心非 ready / 内建聊天已停用：一律不提交给核心（§五 / §十.20 / §2.2）
      input.value = "";
      renderComposeGate();
      return;
    }
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

/// 最近一次变更时间：核心 settings 段不带时间字段，壳读本地配置文件的 mtime 补上（不伪造服务端字段）
function changedLabel(seconds: number): string {
  return seconds
    ? new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false })
    : "未知（配置文件还没写过）";
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
    ["当前模型", String(settings.llm.model ?? "-")],
    ["最近一次变更", changedLabel(Number(settings.llm.changed_at ?? 0))],
    ["配置文件", String(settings.core.config_file ?? "-")],
    ["单段上限 / 单批段数", `${settings.core.max_text_len} / ${settings.core.max_parts}`],
    ["上下文条数", String(settings.core.context_history_max ?? "-")],
  ]);
}

/// 记忆检索是否走远程语义召回：判据与核心一致（模型 + 地址 + 凭据都齐才可用），缺任一项即全文降级（§六 / §十.7）
function recallReady(): boolean {
  const facts = state.facts;
  return Boolean(facts && facts.memory_model && facts.memory_base_url && facts.memory_key_set);
}

/// 降级只在顶栏给一句标识，不带地址与凭据（§二.6 / §十.7）
function renderDegrade(): void {
  const chip = $("degrade");
  const degraded = Boolean(state.facts) && !recallReady();
  chip.classList.toggle("hidden", !degraded);
  chip.textContent = degraded ? "语义召回不可用（已降级为全文）" : "";
}

/// 只读设置事实（记忆 / 提交 / 世界·会话 组）：核心 settings 契约不含这些键，
/// 壳读本地配置只展示可读值与说明，不伪造保存成功（§3.3）。
async function loadLocalFacts(): Promise<void> {
  try {
    state.facts = await invoke<LocalFacts>("config_facts");
  } catch (error) {
    console.warn(`本地配置事实读取失败：${error}`); // 非 Tauri 环境（浏览器调试）不影响连接与聊天
  }
  renderLocalFacts();
}

function renderLocalFacts(): void {
  const facts = state.facts;
  if (!facts) {
    renderFacts($("mem-facts"), [["配置", "（壳未提供本地配置事实）"]]);
    renderFacts($("commit-facts"), [["配置", "（壳未提供本地配置事实）"]]);
    renderFacts($("worldset-facts"), [["配置", "（壳未提供本地配置事实）"]]);
    return;
  }
  renderFacts($("mem-facts"), [
    ["服务地址", facts.memory_base_url || "未配置"],
    ["模型", facts.memory_model || "未配置"],
    ["API Key", facts.memory_key_set ? "已配置（读取打码）" : "尚未配置"],
    ["当前状态", recallReady() ? "可用：记忆检索走远程语义召回" : "不可用：已降级为全文召回"],
    ["说明", "这组键由核心运行时配置管理，本界面只读"],
  ]);
  renderFacts($("commit-facts"), [
    ["提交开关", facts.commit_enabled === null ? "（未知）" : facts.commit_enabled ? "开启" : "关闭"],
    ["现实间隔", facts.commit_minutes === null ? "（未知）" : `${facts.commit_minutes} 分钟`],
    ["事件阈值", facts.commit_events === null ? "（未知）" : `${facts.commit_events} 条新增事件`],
  ]);
  renderFacts($("worldset-facts"), [
    ["创作目录（世界包 / 角色卡）", facts.packages_dir || "-"],
    ["可同时激活的线", facts.max_active_timelines === null ? "（未知）" : `${facts.max_active_timelines} 条`],
    ["主动每日额度", facts.render_calls_per_day === null ? "（未知）" : `${facts.render_calls_per_day} 次 / 现实日`],
    ["倍率上限（仅开发者）", facts.rate_max === null ? "（未知）" : `${facts.rate_max} 世界秒 / 现实秒`],
  ]);
  renderDegrade();
}

/// 用量（§3.3 / §十.11）：只显示调用次数与 token 量级；含正文的账目核心不会给，壳也不猜价格
async function loadUsage(): Promise<void> {
  const instanceId = state.instanceId || world.instanceId;
  if (!mgmt || !instanceId) {
    renderFacts($("usage-facts"), [["用量", "还没有选实例"]]);
    return;
  }
  try {
    const budget = await mgmt.call("runtime.budget", { instance_id: instanceId });
    const limits = (budget.limits ?? {}) as Record<string, number>;
    const rows = (budget.rows ?? []) as Array<{ task: string; calls: number; tokens: number }>;
    const usage = (budget.usage ?? {}) as { instance?: number };
    const paused = (budget.paused_tasks ?? []) as string[];
    renderFacts($("usage-facts"), [
      [
        "调用上限（实例 / 单线 / 单任务）",
        `${limits.instance_tokens_per_day ?? "-"} / ${limits.timeline_tokens_per_day ?? "-"} / ${limits.task_tokens_per_day ?? "-"} token`,
      ],
      ["今日已记 token 量级", String(usage.instance ?? 0)],
      [
        "今日调用记录",
        rows.length
          ? rows.map((row) => `${row.task} ${row.calls} 次 / ${row.tokens} token`).join("；")
          : "无",
      ],
      ["暂停的派生任务", paused.length ? paused.join("、") : "无"],
    ]);
  } catch (error) {
    renderFacts($("usage-facts"), [["用量", String(error)]]);
  }
}

async function loadSettings(): Promise<void> {
  if (!mgmt) return;
  try {
    const settings = (await mgmt.call("settings.get")) as unknown as SettingsPayload;
    await loadLocalFacts();
    if (state.facts) settings.llm.changed_at = state.facts.mtime; // 本地文件 mtime，不是服务端字段
    fillSettings(settings);
    $("settings-note").textContent = "";
  } catch (error) {
    $("settings-note").textContent = String(error);
  }
  await loadBackups();
  await loadAbout();
  await loadUsage();
}

/* ---------- 备份组（DESKTOP_SPEC §3.3）：入口与展示在壳里，备份由核心执行 ---------- */

interface BackupEntry {
  file: string;
  name: string;
  bytes: number;
  mtime: number;
  ok: boolean;
  reason?: string;
}

function stamp(seconds: number): string {
  return new Date(seconds * 1000).toLocaleString("zh-CN", { hour12: false });
}

async function loadBackups(): Promise<void> {
  if (!mgmt) return;
  try {
    const listed = await mgmt.call("backup.list");
    const backups = (listed.backups ?? []) as unknown as BackupEntry[];
    state.backupDir = String(listed.dir ?? "");
    renderFacts($("backup-facts"), [
      ["备份目录", state.backupDir || "-"],
      [
        "检查间隔",
        `${listed.interval_hours ?? "-"} 小时（暂不支持运行期自动补做：只有「立即备份」与显式退出前的补做会真正落盘）`,
      ],
      ["保留份数", String(listed.keep ?? "-")],
      [
        "最近一份",
        backups.length
          ? `${stamp(backups[0].mtime)}　${formatSize(backups[0].bytes)}　${backups[0].ok ? "完整" : "校验未通过"}`
          : "还没有备份",
      ],
    ]);
    const list = $("backup-list");
    list.innerHTML = "";
    for (const item of backups) {
      const row = document.createElement("li");
      // 只显示时间、大小、完整性与原因，不做内容浏览（§3.3）
      row.textContent =
        `${stamp(item.mtime)}　${formatSize(item.bytes)}　${item.ok ? "完整" : `校验未通过：${item.reason ?? "未知原因"}`}`;
      list.appendChild(row);
    }
  } catch (error) {
    $("backup-note").textContent = String(error);
  }
}

async function backupNow(): Promise<void> {
  if (!mgmt) return;
  $("backup-note").textContent = "备份中…";
  try {
    const result = await mgmt.call("backup.create", { note: "手动" }, 120000);
    const info = result.backup as { ok?: boolean; bytes?: number } | undefined;
    $("backup-note").textContent = info?.ok
      ? `已备份（${formatSize(info.bytes ?? 0)}）`
      : "备份未通过完整性校验，旧备份保留";
  } catch (error) {
    $("backup-note").textContent = `备份失败（旧备份保留）：${error}`;
  }
  await loadBackups();
}

async function restoreBackup(): Promise<void> {
  if (!mgmt) return;
  $("backup-note").textContent = "等待选择备份文件…";
  let picked: string | null = null;
  try {
    picked = await invoke<string | null>("pick_backup_file", { dir: state.backupDir });
  } catch (error) {
    $("backup-note").textContent = `打开文件对话框失败：${error}`;
    return;
  }
  if (!picked) {
    $("backup-note").textContent = "已取消选择";
    return;
  }
  const ok = window.confirm(
    `将用「${picked}」替换整套受管数据（不是实例导入）：\n` +
      "· 完成后全部世界线先冻结，需要你明确激活\n" +
      "· 旧连接、绑定令牌与在途任务一并失效，需重新握手\n" +
      "· 当前库会先留一份安全副本在备份目录（isekai-restore-safety.db）\n" +
      "继续？",
  );
  if (!ok) {
    $("backup-note").textContent = "已取消，未改动任何数据";
    return;
  }
  $("backup-note").textContent = "恢复中…";
  try {
    const result = await mgmt.call("backup.restore", { path: picked }, 120000);
    const info = result.restore as { restored?: boolean; timelines?: number; safety?: string } | undefined;
    if (info?.restored) {
      $("backup-note").textContent = `已恢复：${info.timelines ?? 0} 条线已冻结（安全副本 ${info.safety ?? ""}）`;
      await resyncAfterRestore();
    } else {
      $("backup-note").textContent = "恢复未完成，现有数据保留";
    }
  } catch (error) {
    $("backup-note").textContent = `恢复失败，现有数据保留：${error}`;
  }
  await loadBackups();
}

/// 整库恢复后：旧连接、绑定令牌与在途任务统一失效（§五 / §十.20）。
/// 壳不再用旧连接发消息：断开通道、按恢复后的库重载世界数据与会话历史、提示需先激活；
/// 重新握手留到用户明确激活线之后（§五「用户明确激活后再按运行层规则恢复」），期间不提交对话。
async function resyncAfterRestore(): Promise<void> {
  ump?.close(); // 旧连接作废；主动关闭不触发自动重连链
  ump = null;
  state.token = "";
  reconnectToken += 1;
  state.needActivate = true;
  renderComposeGate();
  try {
    await loadWorld();
    const { target } = await pickSessionRow();
    if (target) {
      state.sessionId = target.id;
      state.instanceId = target.instance_id;
      state.timelineId = target.timeline_id;
      state.characterId = target.character_id;
      await loadHistory();
    } else {
      state.messages = [];
      renderMessages();
    }
    renderTopbar();
    renderSessionList();
    state.phase = "ready";
    setStatus("已就绪（恢复后全部世界线已冻结：先在管理页激活这条线再对话）", "ok");
  } catch (error) {
    state.phase = "failed";
    setStatus(`恢复后重新握手失败：${error}${logHint()}`, "bad");
    showRestart();
  }
}

/* ---------- 关于 / 诊断（§3.3）：版本、日志目录与打开入口 ---------- */

async function loadAbout(): Promise<void> {
  let overview: Record<string, unknown> = {};
  if (mgmt) {
    try {
      overview = await mgmt.call("status");
    } catch (error) {
      $("about-note").textContent = String(error);
    }
  }
  renderFacts($("about-facts"), [
    ["应用版本", String(overview.app ?? shellStatus?.app ?? "-")],
    ["数据格式版本", String(overview.data_format ?? shellStatus?.data_format ?? "-")],
    ["世界规则版本", String(overview.rules ?? shellStatus?.rules ?? "-")],
    ["日志目录", state.logDir || "（壳未提供）"],
    ["脱敏诊断", "只含阶段 / 耗时 / 错误码；不含对话正文、实情、角色卡秘密、记忆与凭据"],
  ]);
}

/// 打开目录（日志 / 备份）：走壳的 open_dir，不新增依赖
async function openDir(path: string, note: HTMLElement): Promise<void> {
  if (!path) {
    note.textContent = "还不知道目录位置：先连上核心或做一次备份";
    return;
  }
  try {
    await invoke("open_dir", { path });
    note.textContent = `已用资源管理器打开 ${path}`;
  } catch (error) {
    note.textContent = String(error);
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
  compatibility?: string;
  compatibility_note?: string;
}

interface WorldCache {
  packages: PackageEntry[];
  cards: CardEntry[];
  instances: InstanceEntry[];
  containers: Array<{ file: string }>;
  instanceId: string;
  timeline: string;
  characterId: string;
  characters: Array<{ card_id: string; name: string; occupation?: string }>;
  timelines: Array<{ id: string; name: string; state: string }>;
  clock: ClockView | null;
}

interface DisclosureSelection {
  messageId: string;
  fromCharacter: string;
  fromName: string;
}

interface ClockView {
  state: string;
  world_seconds?: number;
  processed_world?: number;
  label?: string;
  rate?: number;
  catching_up?: boolean;
}

const world: WorldCache = {
  packages: [],
  cards: [],
  instances: [],
  containers: [],
  instanceId: "",
  timeline: "",
  characterId: "",
  characters: [],
  timelines: [],
  clock: null,
};
let disclosureSelection: DisclosureSelection | null = null;
const GENERATE_TIMEOUT_MS = 600000;

function fillSelect(select: HTMLSelectElement | null, entries: Array<[string, string]>): void {
  if (!select) {
    console.warn("fillSelect: 目标节点不存在");
    return;
  }
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
  world.instanceId = selected;
  if (selected) void showInstance(selected);
  else renderFacts($("world-facts"), [["实例", "还没有实例"]]);
  void loadDrafts();
}

/// 实例详情：元数据 + 时间线 + 角色，都以核心返回为准填进 world 缓存（顶栏 / 侧栏 / 运行面共用一份事实）
async function loadInstanceDetail(
  instanceId: string,
): Promise<{ info: InstanceEntry; timelines: Array<{ id: string; name: string; state: string }> } | null> {
  if (!mgmt) return null;
  const detail = await mgmt.call("instance.info", { id: instanceId });
  const info = detail.instance as unknown as InstanceEntry;
  const characters = (detail.characters ?? []) as Array<Record<string, string>>;
  const timelines = (detail.timelines ?? []) as Array<{ id: string; name: string; state: string }>;
  world.instanceId = info.id; // 运行面状态跟着渲染的事实走，避免实例与时间线拼成混合参数
  world.timeline = timelines[0]?.id ?? "";
  world.timelines = timelines;
  world.characters = (characters as unknown as WorldCache["characters"]) ?? [];
  if (!world.characters.some((item) => item.card_id === world.characterId)) {
    world.characterId = world.characters[0]?.card_id ?? "";
  }
  return { info, timelines };
}

async function showInstance(instanceId: string): Promise<void> {
  if (!mgmt) return;
  try {
    const loaded = await loadInstanceDetail(instanceId);
    if (!loaded) return;
    const { info, timelines } = loaded;
    renderRoleControls();
    await renderDisclosures();
    // 补卡的目标时间线：跟当前查看的实例走（§3.2 角色行「选择目标时间线」）
    fillSelect(
      $<HTMLSelectElement>("card-add-timeline"),
      timelines.map((item) => [item.id, `${item.name}（${item.state === "frozen" ? "冻结" : "激活"}）`]),
    );
    renderFacts($("world-facts"), [
      ["实例", `${info.name}（原始名称：${info.original_name}${info.imported ? "，导入" : ""}）`],
      ["初始世界时刻", `${info.moment} 世界秒`],
      ["角色", world.characters.map((item) => `${item.name}｜${item.occupation}`).join("；") || "无"],
      ["时间线", timelines.map((item) => `${item.name}（${item.state === "frozen" ? "冻结" : "激活"}）`).join("；")],
      [
        "兼容性",
        info.compatibility === "compatible"
          ? "可运行"
          : `${info.compatibility}：${info.compatibility_note}`,
      ],
      ["世界内部", "不可浏览：管理面只暴露元数据与公开时钟"],
    ]);
    renderTimelines();
    renderSessionList();
    await refreshClock(info.id, world.timeline);
    void loadUsage();
  } catch (error) {
    worldNote(String(error), true);
  }
}

function currentCharacterName(cardId: string): string {
  return world.characters.find((item) => item.card_id === cardId)?.name ?? cardId;
}

function renderRoleControls(): void {
  const roleSelect = $("role-select") as HTMLSelectElement;
  fillSelect(
    roleSelect,
    world.characters.map((item) => [
      item.card_id,
      `${item.name}${item.occupation ? `｜${item.occupation}` : ""}`,
    ]),
  );
  roleSelect.value = world.characterId;
  const target = $("disclose-to") as HTMLSelectElement;
  fillSelect(
    target,
    world.characters
      .filter((item) => item.card_id !== world.characterId)
      .map((item) => [item.card_id, item.name]),
  );
  const note = $("role-note");
  note.textContent = world.characterId
    ? `当前会话角色：${currentCharacterName(world.characterId)}`
    : "先在世界实例里选一个实例";
}

async function renderDisclosures(): Promise<void> {
  const facts = $("disclosure-facts");
  facts.innerHTML = "";
  if (!mgmt || !world.instanceId || !world.timeline) {
    renderFacts(facts, [["披露", "未选择实例或时间线"]]);
    return;
  }
  try {
    const listed = await mgmt.call("disclose.list", {
      instance_id: world.instanceId,
      timeline_id: world.timeline,
    });
    const rows = (listed.disclosures ?? []) as Array<Record<string, unknown>>;
    renderFacts(facts, [
      ["已有披露", String(rows.length)],
      [
        "明细",
        rows.length
          ? rows
              .map(
                (row) =>
                  `${currentCharacterName(String(row.from_character))} → ${currentCharacterName(
                    String(row.to_character),
                  )}（${row.count} 条，世界 ${row.granted_world}）`,
              )
              .join("；")
          : "无",
      ],
    ]);
  } catch (error) {
    renderFacts(facts, [["披露", String(error)]]);
  }
}

async function switchRole(): Promise<void> {
  const roleSelect = $("role-select") as HTMLSelectElement;
  const cardId = roleSelect.value;
  const note = $("role-note");
  if (!mgmt || !world.instanceId || !world.timeline || !cardId) {
    note.textContent = "缺实例 / 时间线 / 角色，无法切换";
    return;
  }
  // 切换前的事实快照：乐观更新失败时回退，别把没发生的切换显示成已切好
  const before = {
    instanceId: state.instanceId,
    timelineId: state.timelineId,
    characterId: state.characterId,
  };
  try {
    // 用户已经点了「切换会话角色」：顶栏立刻跟上目标实例 / 角色 / 时间线，
    // 会话 id 由核心确认（session.ensure）
    state.instanceId = world.instanceId;
    state.timelineId = world.timeline;
    state.characterId = cardId;
    renderTopbar();
    // 切换角色 = 换一个会话：历史不迁移，各自读自己的
    const session = ((await mgmt.call("session.ensure", {
      instance_id: world.instanceId,
      timeline_id: world.timeline,
      character_id: cardId,
    })).session ?? {}) as unknown as SessionRow;
    await switchSession(session);
    world.characterId = cardId;
    note.textContent = `当前会话角色：${currentCharacterName(cardId)}`;
    setStatus(`已就绪 · 已切到 ${currentCharacterName(cardId)}`, "ok");
  } catch (error) {
    Object.assign(state, before);
    renderTopbar();
    note.textContent = String(error);
  }
}

async function confirmDisclosure(): Promise<void> {
  const note = $("disclose-note");
  const target = $("disclose-to") as HTMLSelectElement;
  if (!mgmt || !disclosureSelection) {
    note.textContent = "先在聊天里点角色消息上的「披露」选定片段";
    return;
  }
  if (!target.value) {
    note.textContent = "没有可披露的接收角色";
    return;
  }
  try {
    const result = await mgmt.call("disclose.confirm", {
      instance_id: world.instanceId,
      timeline_id: world.timeline,
      from_character: disclosureSelection.fromCharacter,
      to_character: target.value,
      refs: [disclosureSelection.messageId],
      note: "",
    });
    note.textContent = `已披露给 ${currentCharacterName(target.value)}（${result.reused ? "同一范围已存在" : "已授权"}）`;
    disclosureSelection = null;
    $("disclose-cancel").classList.add("hidden");
    await renderDisclosures();
  } catch (error) {
    note.textContent = String(error);
  }
}

function selectForDisclosure(messageId: string, fromCharacter: string, fromName: string): void {
  disclosureSelection = { messageId, fromCharacter, fromName };
  $("disclose-note").textContent = `已选定 ${fromName} 的片段（${messageId}）`;
  $("disclose-cancel").classList.remove("hidden");
}

function bindDisclosure(): void {
  ($("role-switch") as HTMLButtonElement).addEventListener("click", () => void switchRole());
  ($("disclose-confirm") as HTMLButtonElement).addEventListener("click", () => void confirmDisclosure());
  ($("disclose-cancel") as HTMLButtonElement).addEventListener("click", () => {
    disclosureSelection = null;
    $("disclose-note").textContent = "已取消选择";
    $("disclose-cancel").classList.add("hidden");
  });
}

async function refreshClock(instanceId = world.instanceId, timelineId = world.timeline): Promise<void> {
  const label = $("clock-label");
  if (!mgmt || !instanceId || !timelineId) {
    label.textContent = "未连接或未选择实例";
    return;
  }
  try {
    const result = await mgmt.call("runtime.clock", {
      instance_id: instanceId,
      timeline_id: timelineId,
    });
    if (instanceId !== world.instanceId || timelineId !== world.timeline) return; // 已被切走
    const clock = result.clock as unknown as ClockView;
    world.clock = clock;
    if (clock.state !== "active") {
      label.textContent = `已冻结（已处理 ${clock.processed_world ?? 0} 世界秒）`;
      return;
    }
    if (state.needActivate && timelineId === state.timelineId) {
      // 用户明确激活了当前会话这条线：解除「恢复后先激活」的闸门，并按 §五 重新握手
      state.needActivate = false;
      renderComposeGate();
      await rehandshake();
      setStatus("已就绪", "ok");
    }
    label.textContent =
      `${clock.label}　倍率 ${clock.rate}` +
      (clock.catching_up ? `　追赶中（已处理 ${clock.processed_world}）` : "");
  } catch (error) {
    if (instanceId !== world.instanceId || timelineId !== world.timeline) return;
    label.textContent = String(error);
  }
}

async function clockAction(action: () => Promise<string>, pair?: { instance_id: string; timeline_id: string }): Promise<void> {
  try {
    const message = await action();
    await refreshClock(pair?.instance_id, pair?.timeline_id);
    worldNote(message);
  } catch (error) {
    worldNote(String(error), true);
  }
}

async function saveDraft(
  name: string,
  kind: "package" | "card",
  payload: unknown,
  errors: string[],
): Promise<void> {
  if (!mgmt) return;
  try {
    await mgmt.call("world.draft.save", {
      name: `${name.replace(/\.json$/, "")}-${kind}`,
      kind,
      payload,
      errors,
    });
  } catch (error) {
    worldNote(`草稿保存失败：${error}`, true);
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

/// 补卡：把一张已审定的卡锚定补入目标实例 / 时间线（DESKTOP_SPEC §3.2 角色行）
async function addCharacter(): Promise<string> {
  const card = $<HTMLSelectElement>("card-select").value;
  const timeline = $<HTMLSelectElement>("card-add-timeline").value;
  const note = $<HTMLInputElement>("card-add-note").value.trim();
  if (!world.instanceId) throw new Error("先在世界实例里选一个实例");
  if (!timeline) throw new Error("先选目标时间线");
  if (!card) throw new Error("先在角色卡里选一张卡");
  const instanceName =
    world.instances.find((item) => item.id === world.instanceId)?.name ?? world.instanceId;
  const confirmed = window.confirm(
    `把「${card}」补入「${instanceName} / ${timeline}」？\n` +
      "· 补入只决定她自哪一刻起出现在本线，加入点不晚于已完成水位\n" +
      "· 预算与调用上限沿用本条线既有限额（不因补入重新计）\n" +
      "· 补入本身不激活冻结线；失败或取消不会留下半个角色",
  );
  if (!confirmed) return "";
  const result = await mgmt!.call("runtime.card.add", {
    instance_id: world.instanceId,
    timeline_id: timeline,
    card_path: card,
    ...(note ? { note } : {}),
  });
  const join = (result.join ?? {}) as {
    name?: string;
    joined_world?: number;
    joined_label?: string;
    timeline_state?: string;
    note?: string;
  };
  renderFacts($("card-add-facts"), [
    ["最近补入", `${join.name ?? card} @ ${join.joined_label ?? "（核心未回标签）"}`],
    ["加入点", `世界 ${join.joined_world ?? "?"} 秒（不晚于已完成水位）`],
    ["线状态", join.timeline_state === "active" ? "激活（补入未改动）" : "冻结（要对话需显式激活）"],
    ["加入说明", join.note || "（未填）"],
  ]);
  return `已补入 ${join.name ?? card}：加入于 ${join.joined_label ?? ""}`;
}

/// 草稿：候选世界包 / 角色卡单独保存，可显式继续，只有「丢弃草稿」才删除（§3.2）
async function loadDrafts(): Promise<void> {
  if (!mgmt) return;
  try {
    const listed = await mgmt.call("world.draft.list");
    const drafts = (listed.drafts ?? []) as Array<{
      file: string;
      name: string | null;
      kind: string | null;
      updated_at: number | null;
    }>;
    fillSelect(
      $<HTMLSelectElement>("draft-select"),
      drafts.map((item) => [
        String(item.name ?? item.file),
        `${item.name ?? item.file}｜${item.kind === "card" ? "角色卡" : "世界包"}｜${
          item.updated_at ? stamp(Number(item.updated_at)) : "时间未知"
        }`,
      ]),
    );
    $("draft-note").textContent = drafts.length ? "" : "没有未完成的草稿";
  } catch (error) {
    $("draft-note").textContent = String(error);
  }
}

async function continueDraft(): Promise<string> {
  const name = $<HTMLSelectElement>("draft-select").value;
  if (!name) throw new Error("没有可继续的草稿");
  const loaded = await mgmt!.call("world.draft.load", { name });
  const draft = (loaded.draft ?? {}) as { kind?: string; payload?: unknown; errors?: string[] };
  const isCard = draft.kind === "card";
  showErrors(isCard ? "card-errors" : "pkg-errors", (draft.errors ?? []) as string[]);
  if (isCard) {
    const target = $<HTMLInputElement>("card-file").value.trim() || `${name}.card.json`;
    await mgmt!.call("world.card.save", { card_path: target, card: draft.payload });
    $<HTMLInputElement>("card-file").value = target;
  } else {
    const target = $<HTMLInputElement>("pkg-file").value.trim() || `${name}.json`;
    await mgmt!.call("world.package.save", { path: target, package: draft.payload, force: true });
    $<HTMLInputElement>("pkg-file").value = target;
  }
  return `草稿「${name}」已载回创作目录（未过校验的项照旧列出），可继续改文件或再走一次 AI 生成`;
}

async function discardDraft(): Promise<string> {
  const name = $<HTMLSelectElement>("draft-select").value;
  if (!name) throw new Error("没有可丢弃的草稿");
  if (!window.confirm(`丢弃草稿「${name}」？只删这份草稿，不动已有世界包 / 角色卡与实例。`)) return "";
  await mgmt!.call("world.draft.discard", { name });
  return `已丢弃草稿「${name}」`;
}

function bindWorld(): void {
  $("world-refresh").addEventListener("click", () => void loadWorld());
  $("card-add").addEventListener("click", () => void worldAction(addCharacter));
  $("draft-continue").addEventListener("click", () => void worldAction(continueDraft));
  $("draft-discard").addEventListener("click", () => void worldAction(discardDraft));
  $<HTMLSelectElement>("inst-select").addEventListener("change", (event) => {
    void showInstance((event.target as HTMLSelectElement).value);
  });
  $("clock-activate").addEventListener("click", () => {
    const pair = { instance_id: world.instanceId, timeline_id: world.timeline };
    void clockAction(async () => {
      if (!pair.instance_id || !pair.timeline_id) throw new Error("先选一个实例");
      const rateText = $<HTMLInputElement>("clock-rate").value.trim();
      const rate = rateText ? Number(rateText) : undefined;
      if (rate !== undefined && (!Number.isInteger(rate) || rate < 1)) throw new Error("倍率必须是正整数");
      // 上限被调低或导入端上限更低时，激活需要在此确认一个合法倍率（§2.4）
      const result = await mgmt!.call("runtime.activate", rate === undefined ? pair : { ...pair, rate });
      const clock = result.clock as unknown as ClockView;
      const confirmed = (result.clock as unknown as { confirmed_rate?: number }).confirmed_rate;
      return `已激活：${clock.label ?? ""}${confirmed ? `（确认倍率 ${confirmed}）` : ""}`;
    }, pair);
  });
  $("clock-freeze").addEventListener("click", () => {
    const pair = { instance_id: world.instanceId, timeline_id: world.timeline };
    void clockAction(async () => {
      if (!pair.instance_id || !pair.timeline_id) throw new Error("先选一个实例");
      const result = await mgmt!.call("runtime.freeze", pair);
      const clock = result.clock as unknown as ClockView & { cancelled_commands?: number };
      return `已冻结于 ${clock.label ?? clock.world_seconds}（取消未生效命令 ${clock.cancelled_commands ?? 0} 条）`;
    }, pair);
  });
  $("clock-set-rate").addEventListener("click", () => {
    const pair = { instance_id: world.instanceId, timeline_id: world.timeline };
    void clockAction(async () => {
      if (!pair.instance_id || !pair.timeline_id) throw new Error("先选一个实例");
      const rate = Number($<HTMLInputElement>("clock-rate").value);
      if (!Number.isInteger(rate) || rate < 1) throw new Error("倍率必须是正整数");
      const result = await mgmt!.call("runtime.rate", { ...pair, rate });
      const changed = result.rate as { changed?: boolean; effective_real?: number; duplicate?: boolean };
      if (changed.changed === false) return "倍率未变化";
      return `倍率 ${rate} 将于整秒 ${changed.effective_real} 生效${changed.duplicate ? "（重试未重复登记）" : ""}`;
    }, pair);
  });

  // 时钟显示：世界在走，界面每 2 秒跟一次（管理页可见时才请求）
  setInterval(() => {
    if (!$("pane-manage").classList.contains("hidden")) void refreshClock();
  }, 2000);

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
      const settings = (await mgmt!.call("settings.get")) as unknown as SettingsPayload;
      const ok = window.confirm(
        `将向 ${settings.llm.model}（${settings.llm.base_url}）发送你填写的世界描述与生成上下文，` +
          `预计调用 3–6 次（含重试，上限 6 次），用量会在完成后显示。继续？`,
      );
      if (!ok) return "已取消，未发送任何内容";
      worldNote("AI 生成中，可能需要一两分钟…");
      const result = await mgmt!.call(
        "world.package.generate",
        { brief, name: $<HTMLInputElement>("pkg-name").value.trim() || file.replace(/\.json$/, "") },
        GENERATE_TIMEOUT_MS,
      );
      const errors = (result.errors ?? []) as string[];
      const usage = result.usage as { calls?: number; limit?: number; paused?: boolean } | undefined;
      if (errors.length) {
        showErrors("pkg-errors", errors);
        await saveDraft(file, "package", result.candidate, errors);
        return `生成未通过校验，已存为草稿（调用 ${usage?.calls ?? "?"}/${usage?.limit ?? "?"}）`;
      }
      await mgmt!.call("world.package.save", { path: file, package: result.candidate });
      showErrors("pkg-errors", []);
      return `已生成并写入 ${file}（调用 ${usage?.calls ?? "?"}/${usage?.limit ?? "?"}）`;
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
      const settings = (await mgmt!.call("settings.get")) as unknown as SettingsPayload;
      const ok = window.confirm(
        `将向 ${settings.llm.model}（${settings.llm.base_url}）发送角色描述与目标世界包，` +
          `预计调用 1–2 次（上限 2 次）。继续？`,
      );
      if (!ok) return "已取消，未发送任何内容";
      worldNote("AI 生成角色卡中…");
      const result = await mgmt!.call(
        "world.card.generate",
        { package_path: pkg, brief },
        GENERATE_TIMEOUT_MS,
      );
      const errors = (result.errors ?? []) as string[];
      const usage = result.usage as { calls?: number; limit?: number } | undefined;
      if (errors.length) {
        showErrors("card-errors", errors);
        await saveDraft(file, "card", result.candidate, errors);
        return `生成未通过校验，已存为草稿（调用 ${usage?.calls ?? "?"}/${usage?.limit ?? "?"}）`;
      }
      await mgmt!.call("world.card.save", { card_path: file, card: result.candidate });
      showErrors("card-errors", []);
      return `已生成并写入 ${file}（仍需确认；调用 ${usage?.calls ?? "?"}/${usage?.limit ?? "?"}）`;
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

/* ---------- 显式退出握手（DESKTOP_SPEC §五）：先保存再停进程 ---------- */

/// 壳要退出时叫我们：停掉新工作 → 走管理面 op app.shutdown（一致水位备份 + 请求核心自行退出）
/// → 回报壳，让壳按上限等核心退出，超时才硬杀。退出前保存失败也照实回报，不拖着不退。
/// 壳可能重发退出请求（隐藏到托盘时事件投递会被挂起）：只跑一次，别用后一次覆盖前一次的结果。
let exitHandshakeDone = false;

async function flushBeforeExit(): Promise<void> {
  if (exitHandshakeDone) return;
  exitHandshakeDone = true;
  state.phase = "stopping";
  $<HTMLTextAreaElement>("input").disabled = true;
  $<HTMLButtonElement>("send").disabled = true;
  setStatus("正在保存并退出…", "pending");
  if (!mgmt) {
    await invoke("exit_ready", { detail: "管理面未连接：没有可保存的连接（按已有持久化水位退出）", saved: false });
    return;
  }
  try {
    const result = await mgmt.call("app.shutdown", {}, 10000);
    const saved = result.saved as { ok?: boolean; bytes?: number } | undefined;
    const detail = saved?.ok
      ? `退出前备份 ${formatSize(saved.bytes ?? 0)}（一致水位），已请求核心自行退出`
      : "退出前备份未通过完整性校验，已请求核心自行退出";
    await invoke("exit_ready", { detail, saved: Boolean(saved?.ok) });
    setStatus("已保存，核心正在退出…", "ok");
  } catch (error) {
    await invoke("exit_ready", { detail: `退出前保存失败：${error}`, saved: false });
    setStatus("保存失败，仍将退出", "bad");
  }
}

/* ---------- 启动 ---------- */

async function boot(): Promise<void> {
  await loadShellSettings(); // 壳侧偏好（内建聊天开关）先读出来，再决定要不要建聊天通道
  bindComposer();
  bindNav();
  bindWorld();
  bindDisclosure();
  try {
    state.logDir = await invoke<string>("log_dir");
  } catch {
    state.logDir = ""; // 非 Tauri 环境（纯浏览器调试）没有这个命令，不影响主流程
  }
  $("history-more").addEventListener("click", () => void loadMoreHistory());
  $("backup-now").addEventListener("click", () => void backupNow());
  $("backup-restore").addEventListener("click", () => void restoreBackup());
  $("backup-open").addEventListener("click", () =>
    void openDir(state.backupDir, $("backup-note")),
  );
  $("open-log-dir").addEventListener("click", () =>
    void openDir(state.logDir, $("about-note")),
  );
  $("settings-form").addEventListener("submit", (event) => void saveSettings(event));
  $("settings-reload").addEventListener("click", () => void loadSettings());
  // 内建聊天开关（默认开）：状态在壳自己的设置里，停用后不建立聊天通道连接
  const chatBox = $<HTMLInputElement>("set-chat-enabled");
  chatBox.checked = chatEnabled();
  chatBox.addEventListener("change", () => void toggleBuiltinChat(chatBox.checked));
  $("chat-note").textContent = chatEnabled()
    ? "已启用：对话走内建通道（builtin）"
    : "已停用：不登记 / 不连接聊天通道，管理面保留";
  $("restart").addEventListener("click", () => void restartCore());
  renderComposeGate();
  await loadLocalFacts(); // 顶栏的语义召回降级标识来自本地配置事实（§六）
  // 壳的退出请求：先保存再让它停核心（有上限，超时由壳硬杀）。
  // 隐藏到托盘时壳叫不动页面（tauri emit / eval / show 全报 failed to send message to the webview，
  // 2026-09 实测），而页面自己的定时器与 invoke 照常 —— 所以这里轮询壳的 exit_pending 取退出请求。
  setInterval(() => {
    if (exitHandshakeDone) return;
    void invoke<boolean>("exit_pending")
      .then((pending) => (pending ? flushBeforeExit() : undefined))
      .catch(() => undefined);
  }, 600);
  await listen("core-status", (event) => {
    shellStatus = event.payload as CoreStatus;
    if (shellStatus.state === "ready") return;
    state.phase = "starting";
    const info = coreStatusText(shellStatus.state, shellStatus.error);
    setStatus(info.text, info.kind);
    renderComposeGate();
    showRestart();
  });
  const status = await waitForCore();
  const info = coreStatusText(status.state, status.error);
  if (status.state === "ready" || status.state === "compatibility_blocked") {
    try {
      await connectChat(status);
    } catch (error) {
      setStatus(`${info.text}（管理面未连上：${error}）`, info.kind);
      showRestart();
    }
    return;
  }
  setStatus(info.text, info.kind);
  showRestart();
}

void boot();
