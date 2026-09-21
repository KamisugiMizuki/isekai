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
  //: 顶栏世界时钟（§3.1）：只跟当前查看的线，且只在激活时显示；冻结 / 未激活不显示
  chatClock: null as ClockView | null,
  //: 核心给的配置文件路径（settings.get 的 core.config_file）：「打开配置目录」取它的父目录
  configFile: "",
};

const HISTORY_PAGE = 200;

/// 验收观察点：记账本壳发出去的管理面 op（不新增权限——页面本来就能调这些 op；只在内存里留最近 200 条）
const opLog: string[] = [];

function traceOps(client: MgmtClient): void {
  const raw = client.call.bind(client);
  client.call = (op, args, timeoutMs) => {
    opLog.push(op);
    if (opLog.length > 200) opLog.shift();
    return raw(op, args, timeoutMs);
  };
}

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
    list.appendChild(emptyState());
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
    if (message.role === "user" && message.state === "failed" && message.envId) {
      // 只有持有稳定出站标识的消息才给重试（sendMessage 抛错路径不设 envId → 不给按不动的按钮）
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

/// 空态按状态分支（§四：空实例 / 无会话各有明确空态）：无实例就给一条直达入口，
/// 不再说「发一条消息试试」跟顶栏 chip「还没有世界实例」互相矛盾（P0-1）。
function emptyState(): HTMLElement {
  const empty = document.createElement("li");
  empty.className = "empty muted";
  if (!world.instances.length) {
    empty.textContent = "还没有世界实例。";
    const button = document.createElement("button");
    button.type = "button";
    button.id = "empty-create";
    button.className = "primary";
    button.textContent = "创建你的第一个世界";
    button.addEventListener("click", () => openFirstWorld());
    empty.appendChild(button);
    return empty;
  }
  empty.textContent = state.sessionId
    ? "还没有对话。发一条消息试试。"
    : "还没有选中的会话：从左侧「会话」列表点一条开始。";
  return empty;
}

/// 首跑引导落点：切到管理页并把世界包生成入口顶到眼前（只导航与聚焦，不替用户做任何事）
function openFirstWorld(): void {
  document.querySelector<HTMLButtonElement>('nav .nav[data-pane="manage"]')?.click();
  const brief = $<HTMLInputElement>("pkg-brief");
  brief.scrollIntoView({ block: "center" });
  brief.focus();
}

/// 设置面落点：切到设置页并把生成模型的 API Key 输入框顶到眼前（同样只导航与聚焦）
function openApiKeyField(): void {
  document.querySelector<HTMLButtonElement>('nav .nav[data-pane="settings"]')?.click();
  const field = $<HTMLInputElement>("set-api-key");
  field.scrollIntoView({ block: "center" });
  field.focus();
}

/// 设置面落点：切到设置页并把「记忆向量化（语义召回）」组的第一个可编辑控件顶到眼前
/// （顶栏降级 chip 的落点；照 openApiKeyField 的写法——只导航与聚焦，不替用户改配置）
function openMemoryGroup(): void {
  document.querySelector<HTMLButtonElement>('nav .nav[data-pane="settings"]')?.click();
  const field = $<HTMLSelectElement>("set-mem-mode");
  field.scrollIntoView({ block: "center" });
  field.focus();
}

/// 生成前的付费闸门（世界包与角色卡共用这一处）：没配 API Key 就不弹确认框 ——
/// 否则第一次生成是一堵没有门的墙：用户付出等待，只拿回核心的原始错误。
/// 这里直接在发起按钮旁的行内槽写「去设置面填 Key」+ 一个真跳转（照 openFirstWorld 的写法）。
function apiKeyBlocked(settings: SettingsPayload, slotId: string): boolean {
  if (settings.llm.api_key_set) return false;
  const note = document.getElementById(slotId);
  if (note) {
    note.className = "muted note bad";
    note.textContent = "还没配 API Key：去设置面「生成模型」填 Key";
    const jump = document.createElement("button");
    jump.type = "button";
    jump.className = "link api-key-jump"; // 用类不用 id：两组可能同时给出闸门提示，id 会撞
    jump.textContent = "去填 Key";
    jump.addEventListener("click", () => openApiKeyField());
    note.appendChild(jump);
  }
  return true;
}

function chip(text: string, kind: string): HTMLElement {
  const element = document.createElement("span");
  element.className = `chip ${kind}`;
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

/// 重启核心：全程占住「连接权」（restartInFlight）——管理令牌是一次性的，
/// 让时钟轮询里的重建路径同时去连只会两边都认证失败（2026-09 实测）。
async function restartCore(): Promise<void> {
  restartInFlight = true;
  try {
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
  } finally {
    restartInFlight = false;
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
    notifyOn = settings[NOTIFY_FLAG] !== false;
  } catch (error) {
    console.warn(`壳设置读取失败：${error}`);
    chatOn = true;
    notifyOn = true;
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
  mgmt?.close(); // 旧连接（若有）先关：重建时别把旧 socket 挂在那里
  mgmt = new MgmtClient(status.endpoint, status.mgmt);
  traceOps(mgmt);
  await mgmt.connect();
  mgmtTokenUsed = String(status.mgmt ?? ""); // 记下这次用掉的一次性令牌：没换令牌就没有可重建的凭据
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
  // 还没世界实例时，顶栏 chip 与聊天空态说同一件事（空态给直达按钮，两者不再互相矛盾）
  const suffix = chatEnabled() && (!opened || !world.instances.length)
    ? "（还没有世界实例：先在管理面创建）"
    : "";
  setStatus(`${info.text}${suffix}`, info.kind);
  renderComposeGate();
  renderManagePane(overview);
  void refreshClockChip(); // 顶栏世界时钟（§3.1）：连上就按当前查看的线跟一次
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
  applySideFilter(); // 筛选是常驻状态：重渲染后照旧生效
}

/// 侧栏筛选（老手效率最小集）：一个过滤框管两条列表，键入即筛（大小写不敏感的子串）；
/// Ctrl+K 聚焦、Esc 清空。列表重渲染后要再筛一次，否则筛选状态会被 innerHTML 冲掉。
function applySideFilter(): void {
  const input = document.getElementById("side-filter") as HTMLInputElement | null;
  const needle = input?.value.trim().toLowerCase() ?? "";
  for (const listId of ["sessions", "timelines"]) {
    for (const item of Array.from($(listId).children)) {
      const hit = !needle || (item.textContent ?? "").toLowerCase().includes(needle);
      item.classList.toggle("hidden", !hit);
    }
  }
}

/// 键盘最小集（老手效率）：Ctrl+K 聚焦侧栏筛选框，Ctrl+1/2/3 切聊天 / 管理 / 设置三个 pane。
/// 只做这三条（不做批量操作、不做全局命令面板），键位与浏览器/系统不冲突。
function bindShortcuts(): void {
  $<HTMLInputElement>("side-filter").addEventListener("input", () => applySideFilter());
  $<HTMLInputElement>("side-filter").addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    (event.target as HTMLInputElement).value = "";
    applySideFilter();
  });
  const panes: Record<string, string> = { "1": "chat", "2": "manage", "3": "settings" };
  document.addEventListener("keydown", (event) => {
    if (!event.ctrlKey || event.altKey) return;
    if (event.key === "k" || event.key === "K") {
      event.preventDefault();
      const filter = $<HTMLInputElement>("side-filter");
      filter.focus();
      filter.select();
      return;
    }
    const pane = panes[event.key];
    if (!pane) return;
    event.preventDefault();
    document.querySelector<HTMLButtonElement>(`nav .nav[data-pane="${pane}"]`)?.click();
  });
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
  applySideFilter(); // 筛选是常驻状态：重渲染后照旧生效
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
  void refreshClockChip(); // 换会话 = 换一条线：顶栏时钟跟着走
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
  // 版本三项与设置面「关于 / 诊断」重复：只留一处（关于面是版本的正位），这里只放本页独有的计数
  renderFacts($("manage-facts"), [
    ["核心状态", String(overview.state ?? "-")],
    ["端点", String(overview.endpoint ?? "-")],
    ["协议版本", String(overview.ump ?? "-")],
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
  if (!payload.reply_to) {
    // 主动消息（没有对应入站）到达：登记提醒 + 系统通知；同一 message_id 的重复投递由去重挡住
    void registerNotice(messageId, parts.join(""));
  }
}

/* ---------- 桌面提醒（§3.1 末条 / §十.17）：只作已固化主动消息的入口，不做第二份历史 ---------- */

/// 桌面提醒开关（默认开）：关掉后不再登记提醒，也不发系统通知；管理面照常。
const NOTIFY_FLAG = "notify_enabled";
let notifyOn = true;
/// 系统通知不可用时留一句可读说明（不挡聊天与历史）
let notifyUnavailable = "";
/// 登记到的提醒：noticeId → message_id（不存正文，不是第二份历史）
const notices = new Map<string, string>();
/// 幂等：同一条固化消息只登记一次（重连补读 / 重复投递不再登记）
const noticedMessages = new Set<string>();
let noticeNote = "";
let noticeNoteBad = false;

function notifyEnabled(): boolean {
  return notifyOn;
}

async function setNotifyEnabled(enabled: boolean): Promise<void> {
  notifyOn = enabled;
  try {
    await invoke("shell_setting_set", { key: NOTIFY_FLAG, value: enabled });
  } catch (error) {
    $("notify-note").textContent = `壳设置写入失败：${error}`;
    return;
  }
  renderNotices();
}

/// 提醒入口与说明：开关状态、系统通知是否可用、以及不切换时的管理错误都写在这里
function renderNotices(): void {
  const count = notices.size;
  const button = $("notice-open");
  button.classList.toggle("hidden", count === 0);
  if (count > 0) {
    button.textContent = count > 1 ? `提醒：打开最新一条（另有 ${count - 1} 条）` : "提醒：打开最新一条主动消息";
  }
  $("notice-note").textContent = noticeNote;
  $("notice-note").classList.toggle("bad", noticeNoteBad);
  $("notify-note").textContent = !notifyOn
    ? "已停用：不登记提醒、不发系统通知；管理面照常"
    : notifyUnavailable || "已启用：主动消息到达时发系统通知";
}

/// 通知标题 / 正文只用显示名与消息原文：实例 / 时间线 / 会话标识不进载荷（§3.1 / A17）
function noticePayload(text: string): { title: string; body: string } {
  const place = world.instances.find((item) => item.id === state.instanceId)?.name ?? "";
  const who = world.characters.find((item) => item.card_id === state.characterId)?.name ?? "";
  return {
    title: [place, who].filter(Boolean).join(" · ") || "isekai",
    body: text.replace(/\s+/g, " ").trim().slice(0, 80) || "有一条新的主动消息",
  };
}

/// 主动消息固化 / 到达：先登记提醒（管理面 notice.create），再发系统通知。
/// 通知不可用只降级说明；登记失败不吞消息——下次投递还能补（§十.17：提醒不可用时历史仍可读）。
async function registerNotice(messageId: string, text: string): Promise<void> {
  if (!messageId || !notifyOn || noticedMessages.has(messageId)) return;
  noticedMessages.add(messageId);
  if (!mgmt || !state.sessionId) return;
  let noticeId = "";
  try {
    // 会话版本：提醒固定引用固化消息 + 原会话版本；回滚让版本倒退，提醒随之后失效
    const page = await mgmt.call("history.page", { session_id: state.sessionId, limit: 1 });
    const created = await mgmt.call("notice.create", {
      instance_id: state.instanceId,
      timeline_id: state.timelineId,
      session_id: state.sessionId,
      message_id: messageId,
      revision: Number(page.revision ?? 0),
    });
    noticeId = String(((created.notice ?? {}) as Record<string, unknown>).id ?? "");
  } catch (error) {
    noticedMessages.delete(messageId);
    noticeNote = `提醒登记失败（${error}）：历史照常可读。`;
    noticeNoteBad = true;
    renderNotices();
    return;
  }
  if (noticeId) notices.set(noticeId, messageId);
  noticeNote = "";
  noticeNoteBad = false;
  renderNotices();
  try {
    const payload = noticePayload(text);
    await invoke("notify_message", { notice_id: noticeId, title: payload.title, body: payload.body });
  } catch (error) {
    notifyUnavailable = `系统通知不可用（${error}）`;
    renderNotices();
  }
}

/// 点击提醒（系统通知 / 提醒入口）→ 管理面解析定位：目标仍有效才切到原会话；
/// 目标被删除 / 归档 / 回滚 / 重绑时只显示管理错误，不改投其他会话、不激活冻结线（§3.1 / A17）。
async function openNotice(noticeId: string): Promise<void> {
  if (!mgmt || !noticeId) return;
  let target: Record<string, unknown> | null = null;
  try {
    const resolved = await mgmt.call("notice.resolve", { id: noticeId });
    target = (resolved.target ?? null) as Record<string, unknown> | null;
  } catch (error) {
    noticeNote = `提醒定位失败（${error}）：未切换会话。`;
    noticeNoteBad = true;
    renderNotices();
    return;
  }
  const sessionId = String(target?.session_id ?? "");
  if (!target || !target.valid || !sessionId) {
    noticeNote = "系统错误：这条提醒指向的消息已不在有效会话上（目标被删除 / 归档 / 回滚或重绑），"
      + "不会改投其他角色；历史仍可从会话列表读取。";
    noticeNoteBad = true;
    renderNotices();
    return;
  }
  let row = state.sessions.find((item) => item.id === sessionId) ?? null;
  if (!row) {
    await loadSessions();
    row = state.sessions.find((item) => item.id === sessionId) ?? null;
  }
  if (!row) {
    noticeNote = "系统错误：提醒指向的会话不在会话列表里（可能已删除），未切换。";
    noticeNoteBad = true;
    renderNotices();
    return;
  }
  await switchSession(row);
  notices.delete(noticeId);
  noticeNote = `已定位到原会话：${sessionLabel(row)}`;
  noticeNoteBad = false;
  renderNotices();
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
  //: 冻结契约（核心侧同步扩展中）：settings.set 接受任一段，成功返回与 settings.get 同形
  memory?: Record<string, unknown>;
  commit?: Record<string, unknown>;
  backup?: Record<string, unknown>;
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
  state.configFile = String(settings.core.config_file ?? "");
  renderFacts($("settings-facts"), [
    ["当前模型", String(settings.llm.model ?? "-")],
    ["API Key", settings.llm.api_key_set ? "已配置（读取打码）" : "未配置：AI 生成会先提示去填 Key"],
    ["最近一次变更", changedLabel(Number(settings.llm.changed_at ?? 0))],
    ["配置文件", String(settings.core.config_file ?? "-")],
    ["单段上限 / 单批段数", `${settings.core.max_text_len} / ${settings.core.max_parts}`],
    ["上下文条数", String(settings.core.context_history_max ?? "-")],
  ]);
  fillMemorySegment(settings.memory);
  fillCommitSegment(settings.commit);
}

/// 记忆向量化组（§3.3）：核心给了 memory 段就填成可编辑表单；没给就照实说明并保留本地事实展示
function fillMemorySegment(segment: Record<string, unknown> | undefined): void {
  const mode = $<HTMLSelectElement>("set-mem-mode");
  const url = $<HTMLInputElement>("set-mem-base-url");
  const model = $<HTMLInputElement>("set-mem-model");
  const key = $<HTMLInputElement>("set-mem-api-key");
  if (!segment) {
    mode.value = "chat";
    url.value = "";
    model.value = "";
    key.value = "";
    key.placeholder = "（核心这一版未开放 memory 段）";
    renderMemModeGate();
    return;
  }
  mode.value = segment.mode === "separate" ? "separate" : "chat";
  url.value = String(segment.base_url ?? "");
  model.value = String(segment.model ?? "");
  key.value = "";
  key.placeholder = segment.api_key_set ? `已配置：${segment.api_key}` : "尚未配置（留空表示不修改）";
  renderMemModeGate();
  renderFacts($("mem-facts"), [
    ["召回模式", segment.mode === "separate" ? "独立服务（远程语义召回）" : "只用全文（降级）"],
    ["服务地址", String(segment.base_url || "未配置")],
    ["模型", String(segment.model || "未配置")],
    ["API Key", segment.api_key_set ? "已配置（读取打码）" : "尚未配置"],
    ["当前状态", segment.ready ? "可用：记忆检索走远程语义召回" : "全文召回（默认：语义召回未启用）"],
  ]);
}

/// 只用全文（降级）时独立服务三项不参与召回：置灰但保留已填值（改的是模式，不动配置）
function renderMemModeGate(): void {
  const separate = $<HTMLSelectElement>("set-mem-mode").value === "separate";
  for (const id of ["set-mem-base-url", "set-mem-model", "set-mem-api-key"]) {
    $<HTMLInputElement>(id).disabled = !separate;
  }
}

function fillCommitSegment(segment: Record<string, unknown> | undefined): void {
  if (!segment) return;
  $<HTMLInputElement>("set-commit-enabled").checked = Boolean(segment.auto_enabled);
  $<HTMLInputElement>("set-commit-minutes").value = String(segment.minutes ?? "");
  $<HTMLInputElement>("set-commit-events").value = String(segment.events ?? "");
  renderFacts($("commit-facts"), [
    ["提交开关", segment.auto_enabled ? "开启" : "关闭"],
    ["现实间隔", segment.minutes === undefined ? "（未知）" : `${segment.minutes} 分钟`],
    ["事件阈值", segment.events === undefined ? "（未知）" : `${segment.events} 条新增事件`],
    ["当前状态", segment.auto_enabled ? "按现实间隔或新增事件条数触发" : "已关闭（不影响状态日常持久化）"],
  ]);
}

/// 写入 settings.set 的一段：成功即以核心返回的同形结果回填；失败照实显示核心的原因（不伪造成功）
async function saveSegment(key: string, segment: Record<string, unknown>, noteId: string): Promise<void> {
  const note = $(noteId);
  note.className = "muted note";
  note.textContent = "保存中…";
  try {
    const saved = (await mgmt!.call("settings.set", { [key]: segment })) as unknown as SettingsPayload;
    fillSettings(saved);
    note.className = "muted note";
    note.textContent = (saved as unknown as Record<string, unknown>)[key]
      ? "已保存并生效"
      : `已保存，但核心未回读 ${key} 段（界面显示的值可能滞后）`;
  } catch (error) {
    note.className = "muted note bad";
    note.textContent = `保存失败（未改动）：${error}`;
  }
}

async function saveMemoryForm(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!mgmt) return;
  const mode = $<HTMLSelectElement>("set-mem-mode").value;
  if (mode === "chat") {
    // 「只用全文（降级）」：核心的 mode 由「有没有配向量模型」派生，没有「清空键」语义。
    // 先按契约把 mode=chat 发出去；核心不开放这个键就如实说明（不假装成功、不悄悄改别的键）。
    const note = $("mem-note");
    note.className = "muted note";
    note.textContent = "保存中…";
    try {
      const saved = (await mgmt.call("settings.set", { memory: { mode: "chat" } })) as unknown as SettingsPayload;
      fillSettings(saved);
      note.textContent = "已保存并生效：记忆检索按全文召回";
    } catch (error) {
      note.className = "muted note bad";
      note.textContent =
        `保存失败（未改动）：${error}｜当前核心的召回模式由「有没有配向量模型」派生，` +
        "要让记忆回到全文降级，只能清掉配置文件里的 memory_embedding_* 后重启核心。";
    }
    return;
  }
  const segment: Record<string, unknown> = {
    base_url: $<HTMLInputElement>("set-mem-base-url").value.trim(),
    model: $<HTMLInputElement>("set-mem-model").value.trim(),
  };
  const key = $<HTMLInputElement>("set-mem-api-key").value.trim();
  if (key) segment.api_key = key; // 留空不改（与 LLM 段同一写法）
  await saveSegment("memory", segment, "mem-note");
}

async function saveCommitForm(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!mgmt) return;
  await saveSegment(
    "commit",
    {
      auto_enabled: $<HTMLInputElement>("set-commit-enabled").checked,
      minutes: Number($<HTMLInputElement>("set-commit-minutes").value),
      events: Number($<HTMLInputElement>("set-commit-events").value),
    },
    "commit-note",
  );
}

async function saveBackupForm(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!mgmt) return;
  await saveSegment(
    "backup",
    {
      dir: $<HTMLInputElement>("set-backup-dir").value.trim(),
      interval_hours: Number($<HTMLInputElement>("set-backup-interval").value),
      keep: Number($<HTMLInputElement>("set-backup-keep").value),
    },
    "backup-note",
  );
  await loadBackups();
}

/// 记忆检索是否走远程语义召回：判据与核心一致（模型 + 地址 + 凭据都齐才可用），缺任一项即全文降级（§六 / §十.7）
function recallReady(): boolean {
  const facts = state.facts;
  return Boolean(facts && facts.memory_model && facts.memory_base_url && facts.memory_key_set);
}

/// 降级只在顶栏给一句标识，不带地址与凭据（§二.6 / §十.7）。
/// 口径（2026-09-21 复审）：全文召回是默认态，不是故障——chip 说的是「现在用哪种召回」，
/// 不再写成「不可用」；chip 自身可点：切设置面「记忆向量化」组并聚焦第一个可编辑控件。
function renderDegrade(): void {
  const chip = $<HTMLButtonElement>("degrade");
  const fullTextOnly = Boolean(state.facts) && !recallReady();
  chip.classList.toggle("hidden", !fullTextOnly);
  chip.textContent = fullTextOnly ? "召回：全文（默认；点此启用语义召回）" : "";
  // 播报走单独的 sr-only 节点：role=status 挂在可点按钮上会盖掉 button 语义（AX 实测 role=status），
  // 读屏把它当状态播报、键盘用户也拿不到「按钮」；chip 上因此不留 role / aria-live。
  // 只在文案真的变了时写一次，避免重复播报。
  const live = $("degrade-live");
  const spoken = fullTextOnly ? "记忆召回：当前用全文（默认），可在设置面启用语义召回" : "";
  if (live.textContent !== spoken) live.textContent = spoken;
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
    ["当前状态", recallReady() ? "可用：记忆检索走远程语义召回" : "全文召回（默认：语义召回未启用）"],
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
    // 备份组表单（§3.3）：目录 / 间隔 / 保留数就填核心回的值（与下方事实同一份真值）
    $<HTMLInputElement>("set-backup-dir").value = state.backupDir;
    $<HTMLInputElement>("set-backup-interval").value = String(listed.interval_hours ?? "");
    $<HTMLInputElement>("set-backup-keep").value = String(listed.keep ?? "");
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
    picked = await invoke<string | null>("pick_file", { dir: state.backupDir });
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

/// 打开目录（日志 / 备份 / 配置）：走壳的 open_dir，不新增依赖
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

/// 打开配置目录（§3.3 关于）：目录取核心给的配置文件路径的父目录（壳复用既有 open_dir）
async function openConfigDir(): Promise<void> {
  const file = state.configFile || state.facts?.config_file || "";
  if (!file) {
    $("about-note").textContent = "还不知道配置文件位置：先连上核心或重新读取设置";
    return;
  }
  await openDir(file.replace(/[\\/][^\\/]*$/, ""), $("about-note"));
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

interface ConvertResult {
  converted: boolean;
  state?: string;
  reason?: string;
  hint?: string;
  needs_confirmation?: boolean;
  from?: string;
  to?: string;
  safety?: string;
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

/// 生成（世界包 / 角色卡）闸门：两组共用一个**全局在途闸门**（互斥）——一个在跑时另一组也发不起；
/// 进行中禁用两组控件，二次点击直接拦下（连点 = 第二次付费调用）。
/// 过程没有进度事件，只有已用时长与上限——不装进度条、不写「一两分钟」（§3.1 诚实反馈）。
const generateBusy = { package: false, card: false };
/// 调用上限：与核心 generator 的默认值一致（段数 × 2 / 卡片 2）；完成后以核心回的 usage.limit 为准
const GENERATE_LIMIT = { package: 6, card: 2 };
const GENERATE_CONTROLS = {
  package: ["pkg-generate", "pkg-brief", "pkg-name"],
  card: ["card-generate", "card-brief", "card-name-input"],
};
/// 每个组各一个进度计时器：同组重入时先清掉上一只，绝不让计时器泄漏着一直改写结果槽
const progressTimers: Record<"package" | "card", number | null> = { package: null, card: null };

function setGenerateGate(kind: "package" | "card", busy: boolean): void {
  generateBusy[kind] = busy;
  // 全局互斥：任一组在跑，两组的控件都禁用（同一实例上叠两次付费调用没有意义）
  const blocked = generateBusy.package || generateBusy.card;
  for (const group of ["package", "card"] as const) {
    for (const id of GENERATE_CONTROLS[group]) {
      ($(id) as HTMLButtonElement | HTMLInputElement).disabled = blocked;
    }
  }
  setLiveGate(blocked);
}

/// 进度槽的播报闸门（§四）：进度行每秒改写一次，槽又是 aria-live=polite——一次 10 分钟生成会被重复
/// 播报几百句「同一句话只差几秒」。生成期间两个进度槽置 aria-live=off（文本照写、界面照看），
/// 结束 / 失败后回 polite，只有结果那一句才播报。闸门跟 generateBusy 同一处开关：两条生成路径都走这里。
function setLiveGate(quiet: boolean): void {
  for (const slotId of ["pkg-note", "card-note"]) {
    $(slotId).setAttribute("aria-live", quiet ? "off" : "polite");
  }
}

/// 发起前的互斥闸门：本组在跑（连点）静默拦下；另一组在跑就在本组行内槽说清楚（§3.1 就近反馈）
function generateBlocked(kind: "package" | "card", slotId: string): boolean {
  if (!generateBusy.package && !generateBusy.card) return false;
  if (!generateBusy[kind]) {
    const running = generateBusy.package ? "世界包" : "角色卡";
    groupNote(slotId, `${running}生成在跑：两组生成互斥，等它结束再发起`, true);
  }
  return true;
}

/// 确认框前的预算事实（harden：付费预算不再写死在壳里）：
/// 拉一次 `runtime.budget` 写「今日已用 n 次调用（全部任务） / 单任务上限 N token；本次最多 k 次调用」：
/// n = 本实例今日账本（call_ledger）里所有任务、所有线的 calls 逐行求和——不是单条任务、也不是本组的次数；
/// N = 核心三层 token 限额的单任务档（本实例 / 单线两档一并写在同一条括号里）；
/// 核心没有「生成调用次数上限」这一字段，次数上限仍是壳侧 GENERATE_LIMIT（= 核心 generator 默认值），
/// 完成后再以核心回的 usage.limit 为准。
/// ponytail: 读不到预算就退回壳常量并在文案里标明「本地常量」——确认框照弹，不因为读不到预算就卡住生成；
///           要更强的一致性就把「读不到预算 → 不给生成」写进闸门，等核心的预算面稳定后再升级。
async function budgetLine(kind: "package" | "card"): Promise<string> {
  try {
    const budget = await mgmt!.call("runtime.budget", {
      instance_id: world.instanceId || state.instanceId,
    });
    const limits = (budget.limits ?? {}) as Record<string, number>;
    const rows = (budget.rows ?? []) as Array<{ calls?: number }>;
    const used = rows.reduce((sum, row) => sum + Number(row.calls ?? 0), 0);
    return (
      `今日已用 ${used} 次调用（全部任务） / 单任务上限 ${limits.task_tokens_per_day ?? "?"} token（本实例 ` +
      `${limits.instance_tokens_per_day ?? "?"}、单线 ${limits.timeline_tokens_per_day ?? "?"}）；` +
      `本次最多 ${GENERATE_LIMIT[kind]} 次调用`
    );
  } catch (error) {
    // 唯一的降级路径：上限回落到壳常量，并在文案里如实说明是本地常量（不冒充核心值）
    return `今日已用 ? 次调用（全部任务；读不到核心预算 ${String(error)}） / 单任务上限 ? token；` +
      `本次最多 ${GENERATE_LIMIT[kind]} 次调用（本地常量）`;
  }
}

function elapsedLabel(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, "0")}`;
}

function startProgress(kind: "package" | "card", slotId: string): void {
  stopProgress(kind);
  const started = Date.now();
  // 进度行落在 role="status" 的槽里——10 分钟等待里读屏要有反馈（§四）。槽的 aria-live 由
  // setGenerateGate → setLiveGate 压在 off：进度逐秒写、只有结果那一句真播报。
  const tick = (): void =>
    groupNote(
      slotId,
      `${kind === "package" ? "世界包" : "角色卡"}生成中：已用 ${elapsedLabel(Date.now() - started)}` +
        ` / 上限 ${elapsedLabel(GENERATE_TIMEOUT_MS)}　调用上限 ${GENERATE_LIMIT[kind]} 次（过程中无法取消）`,
    );
  tick();
  progressTimers[kind] = window.setInterval(tick, 1000);
}

function stopProgress(kind: "package" | "card"): void {
  if (progressTimers[kind] !== null) window.clearInterval(progressTimers[kind]);
  progressTimers[kind] = null;
}

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

/// 页顶提示：只留跨组 / 严重事件；带锚点时给「回到该组」入口（组内结果就近在组里，页顶只是索引）
let worldNoteAnchor = "";

function worldNote(text: string, bad = false, anchor = ""): void {
  const note = $("world-note");
  note.textContent = text;
  note.className = bad ? "muted note bad" : "muted note";
  worldNoteAnchor = anchor;
  $("world-note-jump").classList.toggle("hidden", !anchor);
}

/// 组内结果槽：动作的结果落在动作所在的那一组（§3.1 就近反馈；页顶只有一个 1800px 外的槽）
function groupNote(slotId: string, text: string, bad = false): void {
  const note = document.getElementById(slotId) as HTMLElement | null;
  if (!note) {
    console.warn(`groupNote: 目标节点不存在 ${slotId}`);
    return;
  }
  note.textContent = text;
  note.className = bad ? "muted note bad" : "muted note";
}

/// 组内提示按组写进槽；跨组 / 严重事件才写页顶
function reportNote(slotId: string, text: string, bad = false): void {
  if (slotId === "world-note") worldNote(text, bad);
  else groupNote(slotId, text, bad);
}

/// 「回到该组」：滚回发出这条页顶事件的那一组
function jumpToWorldGroup(): void {
  document.getElementById(worldNoteAnchor)?.scrollIntoView({ block: "center" });
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
    // 零实例时把「版本与计数」折叠成一行：首屏留给世界包与实例两组（P0-1）
    $<HTMLDetailsElement>("manage-facts-box").open = world.instances.length > 0;
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
    `${item.name}｜${item.timelines} 线 / ${item.sessions} 会话｜时刻 ${item.moment}${compatibilityMark(item.compatibility)}`,
  ]);
  fillSelect($<HTMLSelectElement>("pkg-select"), packageOptions as Array<[string, string]>);
  fillSelect($<HTMLSelectElement>("card-select"), cardOptions as Array<[string, string]>);
  fillSelect($<HTMLSelectElement>("inst-select"), instanceOptions as Array<[string, string]>);
  fillSelect($<HTMLSelectElement>("inst-package-select"), packageOptions as Array<[string, string]>);
  fillSelect($<HTMLSelectElement>("inst-cards-select"), cardOptions as Array<[string, string]>);
  // 导入角色卡要先指定归属包（联合校验用），所以这个下拉带一个「未选」占位项
  fillSelect(
    $<HTMLSelectElement>("card-package-select"),
    [["", "（未选：导入角色卡先选归属世界包）"], ...packageOptions] as Array<[string, string]>,
  );
  renderCardImportGate();
  const importOptions = world.containers.map((item) => [item.file, item.file] as [string, string]);
  fillSelect($<HTMLSelectElement>("import-select"), importOptions);
  const selected = $<HTMLSelectElement>("inst-select").value;
  world.instanceId = selected;
  renderDeleteGate();
  // 详情这次等它渲染完：不等的话 showInstance 的异步续写会晚于动作结果落槽，把结果盖回旧文案
  if (selected) await showInstance(selected);
  else {
    renderFacts($("world-facts"), [["实例", "还没有实例"]]);
    void loadCommits(); // 零实例：回滚区照实说「先选一个实例与时间线」
  }
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
    // 不兼容实例（convertible / blocked）才给转换入口；compatible 时整行隐藏（§7.6）
    const incompatible = info.compatibility === "convertible" || info.compatibility === "blocked";
    $("inst-convert-row").classList.toggle("hidden", !incompatible);
    $("inst-convert-note").textContent = incompatible
      ? `${info.compatibility}：${info.compatibility_note ?? ""}`
      : "";
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
    void loadCommits(); // 回滚点跟着当前查看的实例 / 时间线走
    void loadUsage();
  } catch (error) {
    // 实例详情读不出来属于跨组事实（选择、重命名、导出、删除全靠它）：写页顶并给「回到该组」锚点
    worldNote(String(error), true, "group-instances");
  }
}

/* ---------- 删除闸门（§3.2）：离开实例选择行，要求键入实例名，同处写明保留什么 ---------- */

/// 删除行的默认提示（保留清单 + 实例标识尾段）：换实例时复位；删除结果留在这一行自己的槽里（#inst-delete-note）
const DELETE_HINT = "将保留：世界包 / 角色卡 / 导出件";

/// 同名实例不再歧义：默认提示里带上实例标识尾段（照回滚点列表的 slice(-6) 口径）；
/// 删除闸门仍然比显示名——不改成让用户输内部 id。
function deleteHint(id: string): string {
  return id ? `${DELETE_HINT}（实例 #${id.slice(-6)}）` : DELETE_HINT;
}

function instanceNameOf(id: string): string {
  return world.instances.find((item) => item.id === id)?.name ?? "";
}

/// 删除按钮只在键入的名字与选中实例完全一致时可点（浏览器原生 OK 即执行的破坏性路径先过这道闸）。
/// 这里只动闸门（disable / enable），不写提示槽：结果槽由动作自己写，不需要再拿缓存对抗 loadWorld。
function renderDeleteGate(): void {
  const id = $<HTMLSelectElement>("inst-select").value;
  const name = instanceNameOf(id);
  const note = $("inst-delete-note");
  // 槽里是默认提示（不是动作结果）时跟着选中实例刷新标识尾段：同名实例不再靠猜是哪一个
  if (!note.textContent || note.textContent.startsWith(DELETE_HINT)) {
    note.className = "muted";
    note.textContent = deleteHint(id);
  }
  const typed = $<HTMLInputElement>("inst-delete-name").value.trim();
  $<HTMLButtonElement>("inst-delete").disabled = !name || typed !== name;
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

/* ---------- 顶栏世界时钟（§3.1）：世界在跑，聊天页也要看得见 ---------- */

/// 时钟跟随的目标：聊天页当前会话这条线优先；还没开会话时退回管理页正在查看的实例（同一份事实）
function clockTarget(): { instance_id: string; timeline_id: string } {
  return state.instanceId && state.timelineId
    ? { instance_id: state.instanceId, timeline_id: state.timelineId }
    : { instance_id: world.instanceId, timeline_id: world.timeline };
}

/// 顶栏时钟只显示「当前查看且已激活」的线：冻结 / 未激活就整枚隐藏，不拿别的线的钟充数
async function refreshClockChip(): Promise<void> {
  const chip = $("topbar-clock");
  const pair = clockTarget();
  if (!mgmt || !pair.instance_id || !pair.timeline_id) {
    state.chatClock = null;
  } else {
    try {
      // 3 秒上限：本地时钟读不出来只可能是连接没了（核心重启 / 断线），别等默认 30 秒
      const clock = (await mgmt.call("runtime.clock", pair, 3000)).clock as unknown as ClockView;
      const now = clockTarget();
      if (now.instance_id !== pair.instance_id || now.timeline_id !== pair.timeline_id) return; // 已被切走
      state.chatClock = clock;
    } catch (error) {
      state.chatClock = null;
      // 核心重启后旧的管理面连接已死（或响应不回来）：令牌是一次性的，只能整条重建再继续跟
      const text = String(error);
      if (text.includes("未连接核心") || text.includes("管理面响应超时")) void rebuildMgmt();
    }
  }
  const clock = state.chatClock;
  if (!clock || clock.state !== "active") {
    chip.classList.add("hidden");
    return;
  }
  chip.classList.remove("hidden");
  chip.textContent = `${clock.label}　倍率 ${clock.rate}${clock.catching_up ? "　追赶中" : ""}`;
}

/* ---------- 回滚入口（§七）：提交列表 → 二次确认 → 覆盖语义 ---------- */

/// 上一次由「列表加载」写进回滚槽的文本。列表加载与回滚动作都会写这一行，
/// 而 showInstance 里的加载不是 await 的（可能晚于动作结果落槽），所以要能分清
/// 「槽里现在是加载写的」还是「动作结果」：只有前者允许被下一次加载改写。
let commitsLoadText = "";

function commitsNote(text: string, bad = false): void {
  const note = $("rollback-note");
  if (note.textContent && note.textContent !== commitsLoadText) return; // 槽里是动作结果：不动
  note.className = bad ? "muted note bad" : "muted note";
  note.textContent = text;
  commitsLoadText = text;
}

/// 回滚点（提交）列表：只显示管理元数据（时间 / 世界时刻 / 备注 / 标识尾段）。
/// 提交由自动提交与退出补做产生，壳里不提供手动提交入口（YAGNI）。
async function loadCommits(): Promise<void> {
  const select = $<HTMLSelectElement>("commit-select");
  if (!mgmt || !world.instanceId || !world.timeline) {
    fillSelect(select, []);
    commitsNote("先选一个实例与时间线");
    return;
  }
  try {
    const listed = await mgmt.call("runtime.commits", {
      instance_id: world.instanceId,
      timeline_id: world.timeline,
    });
    const commits = (listed.commits ?? []) as Array<{
      id: string;
      moment: number;
      note: string;
      created_at: number;
    }>;
    // 最新的排在最前（核心按 created_at 升序给）：首次加载默认选中最近一个回滚点，
    // 之后 fillSelect 会保留用户上一次的选择（列在下面的不会因为新提交出现而被顶掉）
    fillSelect(
      select,
      [...commits].reverse().map((item) => [
        item.id,
        `${stamp(item.created_at)}｜世界 ${item.moment} 秒｜${item.note || "（无备注）"}｜${item.id.slice(-6)}`,
      ]),
    );
    // 空列表照实说：不伪造回滚点，也不假装「暂无可回滚内容」
    commitsNote(commits.length ? "" : "还没有回滚点：提交由自动提交与退出补做产生");
  } catch (error) {
    commitsNote(String(error), true);
  }
}

/// 回滚（§七，覆盖语义）：先把后果写清楚再问一次（照现有破坏性操作的披露风格）。
/// 参数名取自核心：ops.py `_version_rollback` 收 instance_id / timeline_id / commit_id / confirm。
async function rollbackToCommit(): Promise<string> {
  if (!mgmt) throw new Error("管理面未连接");
  const select = $<HTMLSelectElement>("commit-select");
  const commitId = select.value;
  if (!world.instanceId || !world.timeline) throw new Error("先选一个实例与时间线");
  if (!commitId) throw new Error("还没有回滚点：提交由自动提交与退出补做产生");
  const label = select.selectedOptions[0]?.textContent ?? commitId;
  const ok = window.confirm(
    `将「${timelineName(world.timeline)}」回滚到 ${label}？\n` +
      "· 回滚是覆盖语义：将回滚到该提交，之后的世界时间与事件会按回滚语义处理，这条线的有效历史退到该提交\n" +
      "· 已经投递到外部平台的回复收不回来（核心会回报条数）\n" +
      "· 本线此前若已冻结，回滚后仍停在回滚点；要对话需显式激活\n" +
      "继续？",
  );
  if (!ok) return "已取消，未回滚";
  const result = (await mgmt.call("runtime.rollback", {
    instance_id: world.instanceId,
    timeline_id: world.timeline,
    commit_id: commitId,
    confirm: true,
  })) as unknown as {
    commit?: { id?: string };
    world?: number;
    generation?: number;
    voided_inputs?: number;
    cancelled_replies?: number;
    delivered_replies_kept?: number;
    warning?: string;
  };
  const delivered = Number(result.delivered_replies_kept ?? 0);
  return (
    `已回滚到 ${result.commit?.id ?? commitId}：世界 ${result.world} 秒（世代 ${result.generation}），` +
    `作废在途输入 ${result.voided_inputs ?? 0} 条 / 取消未投递回复 ${result.cancelled_replies ?? 0} 条` +
    (delivered ? `，已投递回复 ${delivered} 条收不回` : "") +
    `。${result.warning ?? ""}`
  );
}

let mgmtRebuilding = false;

/// 「重启核心」路径正在跑：它自己会连，别的重建路径让开（管理令牌是一次性的，不许两个人抢）
let restartInFlight = false;

/// 上一次认证用掉的（一次性）管理面令牌：只有核心重启换了新令牌才值得重建连接
let mgmtTokenUsed = "";

/// 管理面断开（核心重启 / 网络断）后重建连接：重新握手拿新的一次性令牌，并重载世界数据与聊天通道。
/// 与「重启核心」按钮路径互斥：那条路自己会连，重复抢令牌只会让两边都认证失败。
async function rebuildMgmt(): Promise<void> {
  if (!mgmt || mgmtRebuilding || restartInFlight) return;
  mgmtRebuilding = true;
  const mine = reconnectToken;
  try {
    const status = await waitForCore();
    if (reconnectToken !== mine || restartInFlight) return; // 期间有别的连接流程接管
    if (String(status.mgmt ?? "") === mgmtTokenUsed) return; // 令牌没换 = 还是这个核心，没有可换的凭据
    if (status.state !== "ready" && status.state !== "compatibility_blocked") return;
    await connectChat(status);
  } catch (error) {
    setStatus(`管理面重建失败：${error}${logHint()}`, "bad");
    showRestart();
  } finally {
    mgmtRebuilding = false;
  }
}

async function clockAction(action: () => Promise<string>, pair?: { instance_id: string; timeline_id: string }): Promise<void> {
  try {
    const message = await action();
    await refreshClock(pair?.instance_id, pair?.timeline_id);
    await refreshClockChip();
    groupNote("clock-note", message);
  } catch (error) {
    groupNote("clock-note", String(error), true);
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
    reportNote(kind === "card" ? "card-note" : "pkg-note", `草稿保存失败：${error}`, true);
  }
}

/// 世界管理面动作的统一封装：结果写进本组的槽（就近），只有跨组 / 严重事件才写页顶
async function worldAction(action: () => Promise<string | void>, slotId = "world-note"): Promise<void> {
  try {
    const message = await action();
    await loadWorld();
    if (message) reportNote(slotId, message);
  } catch (error) {
    reportNote(slotId, String(error), true);
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

/* ---------- 导入（世界包 / 角色卡）与不兼容实例转换：入口在壳，校验与落盘在核心 ---------- */

/// 创作目录（对话框初始目录）：世界包与角色卡都放在这里，核心的列表 op 顺手带回来。
async function creationDir(): Promise<string> {
  if (!mgmt) return "";
  try {
    return String((await mgmt.call("world.package.list"))?.dir ?? "");
  } catch {
    return "";
  }
}

/// 实例下拉里的兼容性标记：列表层就能看出哪条开不了，不用先选中（§7.6）。
function compatibilityMark(state: unknown): string {
  if (state === "blocked") return "｜⚠ 不兼容（需转换或缺转换器）";
  if (state === "convertible") return "｜⚠ 需转换";
  return "";
}

/// 原生文件对话框：复用壳的 pick_file（Tauri 命令，跑在阻塞线程池上），只换标题与初始目录与过滤器。
/// 核心只认绝对路径，所以路径授权交给系统对话框，壳不自己拼路径（§3.2）。
async function pickImportFile(title: string, filter: string): Promise<string | null> {
  return await invoke<string | null>("pick_file", { dir: await creationDir(), title, filter });
}

/// 一次导入：核心拒绝同名覆盖，除非用户显式确认（force）。
/// 失败时把核心给的原因原样写进本组提示（未落盘 / 超过加载限额 / …），不自己改写成 internal。
async function importInto(
  op: string,
  args: Record<string, unknown>,
  what: string,
  here: string,
): Promise<Record<string, unknown> | null> {
  if (!mgmt) throw new Error("管理面未连接");
  try {
    return await mgmt.call(op, args);
  } catch (error) {
    const text = String(error);
    if (!text.includes("如需覆盖请显式确认")) {
      $(here).textContent = text;
      return null;
    }
    if (!window.confirm(`${text}\n\n用导入件覆盖同名${what}？原有文件会被替换。`)) {
      $(here).textContent = `已取消，未覆盖：${text}`;
      return null;
    }
    return await mgmt.call(op, { ...args, force: true });
  }
}

/// 导入世界包（§3.2 / §7.5）：外部文件 → 结构校验通过才落创作目录，不过不落盘。
async function importPackage(): Promise<string> {
  const picked = await pickImportFile(
    "选择要导入的世界包（*.json）",
    "世界包 (*.json)|*.json|所有文件 (*.*)|*.*",
  );
  if (!picked) return "已取消选择";
  $("pkg-errors").textContent = "";
  const result = await importInto("world.package.import", { source_path: picked }, "世界包", "pkg-errors");
  if (!result) return "导入未完成：原因见下方错误栏";
  await loadWorld();
  $<HTMLSelectElement>("pkg-select").value = String(result.imported ?? "");
  return `已导入 ${result.imported}（${result.name || "未命名"}）${result.replaced ? "，覆盖了同名文件" : ""}`;
}

/// 导入角色卡：渠道 / 史料引用要对着归属包做联合校验，所以没有归属包就不给导入（§3.2 / CHARACTER_CARD §5）。
async function importCard(): Promise<string> {
  const pkg = $<HTMLSelectElement>("card-package-select").value;
  if (!pkg) throw new Error("先选归属世界包：联合校验要用它");
  const picked = await pickImportFile(
    "选择要导入的角色卡（*.json）",
    "角色卡 (*.json)|*.json|所有文件 (*.*)|*.*",
  );
  if (!picked) return "已取消选择";
  $("card-errors").textContent = "";
  const result = await importInto(
    "world.card.import",
    { source_path: picked, package_path: pkg },
    "角色卡",
    "card-errors",
  );
  if (!result) return "导入未完成：原因见下方错误栏";
  await loadWorld();
  $<HTMLSelectElement>("card-select").value = String(result.imported ?? "");
  return `已导入 ${result.imported}（${result.name || "未命名"}，对照 ${
    result.validated_against || pkg
  } 联合校验）${result.replaced ? "，覆盖了同名文件" : ""}`;
}

/// 归属包没选时「导入角色卡…」不给点：联合校验必须要有包。
function renderCardImportGate(): void {
  const pkg = $<HTMLSelectElement>("card-package-select").value;
  $<HTMLButtonElement>("card-import").disabled = !pkg;
  $("card-import-note").textContent = pkg
    ? ""
    : "先选归属世界包：导入角色卡要对着包做联合校验（渠道 / 史料引用）";
}

/// 不兼容实例的转换入口（§7.6）：先问核心要「需确认 + 原因」，用户确认后才转换；
/// 没有可信转换器就停在核心给的原因上（不「尽量加载」）。可恢复副本由核心留、路径照回。
async function convertSelectedInstance(): Promise<string> {
  if (!mgmt) throw new Error("管理面未连接");
  const id = $<HTMLSelectElement>("inst-select").value;
  if (!id) throw new Error("先选实例");
  const first = (await mgmt.call("instance.convert", { instance_id: id, confirmed: false })).convert as
    | ConvertResult
    | undefined;
  if (!first) throw new Error("核心未回转换结果");
  if (!first.needs_confirmation) {
    // 结果交给动作落槽（worldAction 在刷新之后回填）：这里不自己写槽，免得被随后的实例重渲染盖掉
    return [first.reason, first.hint].filter(Boolean).join("；");
  }
  $("inst-convert-note").textContent = String(first.reason ?? "");
  const ok = window.confirm(
    `${first.reason}\n\n` +
      "· 转换只在副本上做：通过完整校验才发布，原实例在失败时一字不动\n" +
      "· 转换前先留一份可恢复副本，成功后线先冻结、由你明确激活\n" +
      "继续？",
  );
  if (!ok) return "已取消，未转换";
  const done = (await mgmt.call("instance.convert", { instance_id: id, confirmed: true })).convert as
    | ConvertResult
    | undefined;
  if (!done?.converted) {
    $("inst-convert-note").textContent =
      [done?.reason, done?.hint].filter(Boolean).join("；") || "转换未完成，原实例保留";
    return "";
  }
  $("inst-convert-note").textContent = "";
  return `已转换 ${done.from} → ${done.to}；可恢复副本：${done.safety || "（核心未回路径）"}`;
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
  $("world-note-jump").addEventListener("click", () => jumpToWorldGroup());
  $("pkg-import").addEventListener("click", () => void worldAction(importPackage, "pkg-note"));
  $("card-import").addEventListener("click", () => void worldAction(importCard, "card-import-note"));
  $<HTMLSelectElement>("card-package-select").addEventListener("change", () =>
    renderCardImportGate(),
  );
  $("inst-convert").addEventListener("click", () =>
    void worldAction(convertSelectedInstance, "inst-convert-note"),
  );
  $("card-add").addEventListener("click", () => void worldAction(addCharacter, "card-add-result"));
  $("draft-continue").addEventListener("click", () => void worldAction(continueDraft, "draft-note"));
  $("draft-discard").addEventListener("click", () => void worldAction(discardDraft, "draft-note"));
  $("rollback").addEventListener("click", () => void worldAction(rollbackToCommit, "rollback-note"));
  $<HTMLSelectElement>("inst-select").addEventListener("change", (event) => {
    $("inst-delete-note").textContent = ""; // 换实例：删除行的提示复位（renderDeleteGate 会写回保留清单 + 标识尾段）
    commitsNote(""); // 回滚行的结果也不跨实例残留（commitsNote 不会盖掉正在显示的动作结果）
    void showInstance((event.target as HTMLSelectElement).value).then(() => renderDeleteGate());
  });
  $<HTMLInputElement>("inst-delete-name").addEventListener("input", () => renderDeleteGate());
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

  // 时钟显示：世界在走，界面每 2 秒跟一次。
  // 顶栏时钟在聊天页也要看得见（§3.1），所以这条轮询不再只在管理页可见时跑：
  // 顶栏跟当前会话那条线；管理页可见时再额外跟一次正在查看的实例。
  setInterval(() => {
    void refreshClockChip();
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
    }, "pkg-note"),
  );

  $("pkg-template").addEventListener("click", () =>
    void worldAction(async () => {
      const file = $<HTMLInputElement>("pkg-file").value.trim();
      if (!file) throw new Error("先填一个文件名");
      const created = await mgmt!.call("world.package.template", { name: file.replace(/\.json$/, "") });
      await mgmt!.call("world.package.save", { path: file, package: created.package });
      showErrors("pkg-errors", created.errors as string[]);
      return `已写入 ${file}（骨架还需填内容）`;
    }, "pkg-note"),
  );

  $("pkg-generate").addEventListener("click", () =>
    void worldAction(async () => {
      if (generateBlocked("package", "pkg-note")) return ""; // 两组互斥：本组连点 / 另一组在跑都拦下
      const brief = $<HTMLInputElement>("pkg-brief").value.trim();
      if (!brief) throw new Error("先写一段世界描述");
      const file = $<HTMLInputElement>("pkg-file").value.trim() || "world.json";
      const settings = (await mgmt!.call("settings.get")) as unknown as SettingsPayload;
      if (apiKeyBlocked(settings, "pkg-note")) return ""; // 没配 Key：不弹确认框，也不发起调用
      const ok = window.confirm(
        `将向 ${settings.llm.model}（${settings.llm.base_url}）发送你填写的世界描述与生成上下文，` +
          `预计调用 3–6 次（含重试，上限 ${GENERATE_LIMIT.package} 次），最长等 ${GENERATE_TIMEOUT_MS / 60000} 分钟；` +
          `预算：${await budgetLine("package")}；` +
          "过程中无法取消（核心没有取消 op，只能等它结束或失败）。用量在完成后显示，继续？",
      );
      if (!ok) return "已取消，未发送任何内容";
      const started = Date.now();
      setGenerateGate("package", true);
      startProgress("package", "pkg-note");
      let result: Record<string, unknown>;
      try {
        result = await mgmt!.call(
          "world.package.generate",
          { brief, name: $<HTMLInputElement>("pkg-name").value.trim() || file.replace(/\.json$/, "") },
          GENERATE_TIMEOUT_MS,
        );
      } finally {
        stopProgress("package");
        setGenerateGate("package", false);
      }
      const errors = (result.errors ?? []) as string[];
      const usage = result.usage as { calls?: number; limit?: number; paused?: boolean } | undefined;
      const cost =
        `调用 ${usage?.calls ?? "?"}/${usage?.limit ?? GENERATE_LIMIT.package} 次 · ` +
        `用时 ${elapsedLabel(Date.now() - started)} / 上限 ${elapsedLabel(GENERATE_TIMEOUT_MS)}`;
      if (errors.length) {
        showErrors("pkg-errors", errors);
        await saveDraft(file, "package", result.candidate, errors);
        return `生成未通过校验，已存为草稿（${cost}）`;
      }
      await mgmt!.call("world.package.save", { path: file, package: result.candidate });
      showErrors("pkg-errors", []);
      return `已生成并写入 ${file}（${cost}）`;
    }, "pkg-note"),
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
    }, "card-select-note"),
  );

  $("card-generate").addEventListener("click", () =>
    void worldAction(async () => {
      if (generateBlocked("card", "card-note")) return ""; // 两组互斥：本组连点 / 另一组在跑都拦下
      const pkg = $<HTMLSelectElement>("pkg-select").value;
      const brief = $<HTMLInputElement>("card-brief").value.trim();
      const file = $<HTMLInputElement>("card-file").value.trim() || "card.json";
      if (!pkg) throw new Error("先选一个世界包");
      if (!brief) throw new Error("先写一段角色描述");
      const settings = (await mgmt!.call("settings.get")) as unknown as SettingsPayload;
      if (apiKeyBlocked(settings, "card-note")) return ""; // 没配 Key：不弹确认框，也不发起调用（与世界包共用同一道闸）
      const ok = window.confirm(
        `将向 ${settings.llm.model}（${settings.llm.base_url}）发送角色描述与目标世界包，` +
          `预计调用 1–2 次（上限 ${GENERATE_LIMIT.card} 次），最长等 ${GENERATE_TIMEOUT_MS / 60000} 分钟；` +
          `预算：${await budgetLine("card")}；` +
          "过程中无法取消（核心没有取消 op，只能等它结束或失败）。继续？",
      );
      if (!ok) return "已取消，未发送任何内容";
      const started = Date.now();
      setGenerateGate("card", true);
      startProgress("card", "card-note");
      let result: Record<string, unknown>;
      try {
        result = await mgmt!.call(
          "world.card.generate",
          { package_path: pkg, brief },
          GENERATE_TIMEOUT_MS,
        );
      } finally {
        stopProgress("card");
        setGenerateGate("card", false);
      }
      const errors = (result.errors ?? []) as string[];
      const usage = result.usage as { calls?: number; limit?: number } | undefined;
      const cost =
        `调用 ${usage?.calls ?? "?"}/${usage?.limit ?? GENERATE_LIMIT.card} 次 · ` +
        `用时 ${elapsedLabel(Date.now() - started)} / 上限 ${elapsedLabel(GENERATE_TIMEOUT_MS)}`;
      if (errors.length) {
        showErrors("card-errors", errors);
        await saveDraft(file, "card", result.candidate, errors);
        return `生成未通过校验，已存为草稿（${cost}）`;
      }
      await mgmt!.call("world.card.save", { card_path: file, card: result.candidate });
      showErrors("card-errors", []);
      return `已生成并写入 ${file}（仍需确认；${cost}）`;
    }, "card-note"),
  );

  $("card-confirm").addEventListener("click", () =>
    void worldAction(async () => {
      const pkg = $<HTMLSelectElement>("pkg-select").value;
      const card = $<HTMLSelectElement>("card-select").value;
      if (!pkg || !card) throw new Error("先选世界包与角色卡");
      await mgmt!.call("world.card.confirm", { package_path: pkg, card_path: card });
      showErrors("card-errors", []);
      return `${card} 已确认，可用于创建实例`;
    }, "card-select-note"),
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
    }, "inst-create-note"),
  );

  $("inst-rename-btn").addEventListener("click", () =>
    void worldAction(async () => {
      const id = $<HTMLSelectElement>("inst-select").value;
      const name = $<HTMLInputElement>("inst-rename-input").value.trim();
      if (!id || !name) throw new Error("先选实例并填新名称");
      await mgmt!.call("instance.rename", { id, name });
      return `已重命名为「${name}」`;
    }, "inst-note"),
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
    }, "inst-note"),
  );

  $("inst-import").addEventListener("click", () =>
    void worldAction(async () => {
      const file = $<HTMLSelectElement>("import-select").value;
      if (!file) throw new Error("创作目录里没有导出件");
      const result = await mgmt!.call("instance.import", { path: file });
      const info = result.instance as unknown as InstanceEntry;
      return `已导入为「${info.name}」（默认冻结）`;
    }, "inst-import-note"),
  );

  /// 删除（§3.2）：独立一行 + 键入实例名确认；结果与失败都写这一行自己的槽（失败也走 #inst-delete-note）
  $("inst-delete").addEventListener("click", () =>
    void worldAction(async () => {
      const id = $<HTMLSelectElement>("inst-select").value;
      const name = instanceNameOf(id);
      if (!id || !name) throw new Error("先选实例");
      if ($<HTMLInputElement>("inst-delete-name").value.trim() !== name) {
        throw new Error(`未确认：请键入实例名「${name}」再删除`);
      }
      if (
        !window.confirm(
          `删除实例「${name}」（#${id.slice(-6)}）及其对话？此操作不可撤销。\n` +
            "· 将保留：世界包 / 角色卡 / 导出件（都不受影响）\n" +
            "· 随实例删除：它的时间线、会话、记忆与预算账本",
        )
      ) {
        return "";
      }
      await mgmt!.call("instance.delete", { id });
      $<HTMLInputElement>("inst-delete-name").value = "";
      return `已删除「${name}」`;
    }, "inst-delete-note"),
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
  bindShortcuts();
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
  $("open-config-dir").addEventListener("click", () => void openConfigDir());
  $("settings-form").addEventListener("submit", (event) => void saveSettings(event));
  $("settings-reload").addEventListener("click", () => void loadSettings());
  // 设置面各组的可编辑表单（§3.3）：各自保存、各自就近回报；只写核心契约，不在壳里另存一份
  $("mem-form").addEventListener("submit", (event) => void saveMemoryForm(event));
  $("commit-form").addEventListener("submit", (event) => void saveCommitForm(event));
  $("backup-form").addEventListener("submit", (event) => void saveBackupForm(event));
  $<HTMLSelectElement>("set-mem-mode").addEventListener("change", () => renderMemModeGate());
  // 内建聊天开关（默认开）：状态在壳自己的设置里，停用后不建立聊天通道连接
  const chatBox = $<HTMLInputElement>("set-chat-enabled");
  chatBox.checked = chatEnabled();
  chatBox.addEventListener("change", () => void toggleBuiltinChat(chatBox.checked));
  $("chat-note").textContent = chatEnabled()
    ? "已启用：对话走内建通道（builtin）"
    : "已停用：不登记 / 不连接聊天通道，管理面保留";
  $("restart").addEventListener("click", () => void restartCore());
  // 顶栏降级 chip 的落点（§3.3）：全文召回是默认态，点它切到设置面「记忆向量化」组并聚焦首个可编辑控件
  $("degrade").addEventListener("click", () => openMemoryGroup());
  // 无人值守验收：把本窗口自己的管理面连接交给探针断言（不新增权限——页面本来就能调这些 op）
  (window as unknown as { __mgmtCall?: unknown }).__mgmtCall =
    (op: string, args: Record<string, unknown> = {}) => mgmt?.call(op, args);
  (window as unknown as { __opLog?: unknown }).__opLog = opLog;
  // 桌面提醒：开关（默认开）+ 入口按钮（系统通知的兜底入口，点它走同一条定位逻辑）
  const notifyBox = $<HTMLInputElement>("set-notify-enabled");
  notifyBox.checked = notifyEnabled();
  notifyBox.addEventListener("change", () => void setNotifyEnabled(notifyBox.checked));
  $("notice-open").addEventListener("click", () => {
    const next = notices.keys().next();
    if (!next.done) void openNotice(String(next.value));
  });
  renderNotices();
  // 通知被点击：壳把窗口带到前台后把提醒交给这里定位（隐藏到托盘时事件送不到，另有标志位轮询兜底）
  await listen<string>("notice-open", (event) => void openNotice(String(event.payload)));
  setInterval(() => {
    void invoke<string | null>("take_pending_notice")
      .then((pending) => (pending ? openNotice(pending) : undefined))
      .catch(() => undefined);
  }, 1000);
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
