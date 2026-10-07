/*
 * 角色联络工作区（USER_INTERFACE_DESIGN §6）。
 *
 * 只展示：对话、角色公开身份、当前世界与线名、世界内时间、必要运行状态。
 * 没有角色活动监控、性格分数、记忆库或世界全知事件表。
 *
 * 发送纪律（§6.2）：点击发送立即保留原文并标「正在提交」；收到「已接收」的回执才清空输入并移入历史；
 * 确认前失败原文仍可编辑；结果未知先查这一条的结果，不重复当新消息发出。
 * 自己说过的那句话在收到回复后必须留在列表里（评审 P0-3），所以 pending 转成 messages 里的用户那一条。
 */

import type { AppContext, Pane } from "./app";
import type { ChannelEvent, ChannelState, Json } from "./api";
import { ChannelLink, uiError, type UiError } from "./api";
import {
  button,
  chip,
  dialog,
  el,
  errorCard,
  fill,
  link,
  panel,
  paragraph,
  primary,
  section,
  setNote,
  stamp,
} from "./dom";
import { stackBar } from "./graphics";
import { paneTitle } from "./app";

interface Message {
  role: "user" | "character" | "notice";
  text: string;
  seq: number;
  at: number;
  messageId: string;
  state: string;
  ref: string;
  /** 这行指向的入站原文（`reply_to`）：转交说明据此对上要展开的那句话（§6.3） */
  replyTo: string;
  parts: number[];
}

interface ContactSelection {
  instance_id: string;
  timeline_id: string;
  timeline_name: string;
  character_id: string;
  character_name: string;
}

/**
 * 正在流式到达的一条回复的**预览缓冲**（P0-7：`reply_delta` 增量，未固化）。
 *
 * 为什么单独存一份、不写进 `messages`：增量不作数——后验检查可能改字，内核也明确
 * 「最终正文以固化的 `reply` 帧为准」。所以它只画在待定气泡里，固化帧到达时整段替换。
 */
interface PreviewDraft {
  messageId: string;
  text: string;
  /** 已收到的最大段序：用来识别乱序 / 重复段（重复段不重复拼接） */
  index: number;
}

/**
 * 一条已经发出去、还没落定的入站请求。`at` 是发送时刻：等待态的读秒按它算，
 * 转进 `messages` 时也用它当这条消息的时间。
 */
interface PendingMessage {
  ref: string;
  text: string;
  state: string;
  at: number;
  error?: UiError;
}

export class ContactPane implements Pane {
  readonly id = "contact" as const;
  private link: ChannelLink | null = null;
  /** 连接失败的反馈卡（每次重试更新它，不新增） */
  private connectError: HTMLElement | null = null;
  private selection: ContactSelection | null = null;
  private messages: Message[] = [];
  private pending: PendingMessage[] = [];
  private thinking = false;
  private characters: Json[] = [];
  private timelines: Json[] = [];
  private worldName = "";
  private worldLabel = "";
  private stateChip: HTMLElement | null = null;
  /** 世界线运行状态（核心读数）：按钮文案按它取，不用两义标签 */
  private runState = "";
  private runBtn: HTMLButtonElement | null = null;
  private statusNote: HTMLElement | null = null;
  private listHost: HTMLElement | null = null;
  private composer: HTMLTextAreaElement | null = null;
  private sendButton: HTMLButtonElement | null = null;
  /** 「AI 未配置」的发送闸提示（只在未配置时显示，含去设置页的入口） */
  private aiGateHint: HTMLElement | null = null;
  /** 「连接没建立」的发送闸提示（就地给「重新连接」，评审 P1「断线不重连」） */
  private linkGateHint: HTMLElement | null = null;
  /** 连接的真实状态（来自 ChannelLink，不再用 `this.link` 非空当已连接） */
  private linkState: ChannelState = "idle";
  /** 这一轮首次连接是否已经完成过：用来区分「重连」与「刚打开」，只有重连才需要补历史 */
  private hasConnected = false;
  /** 等待态读秒：计时器每秒刷一次状态行，收到回复 / 失败 / 换对象时停掉（不能泄漏定时器） */
  private waitTimer: number | null = null;
  private waitSince = 0;
  private draftSlot: HTMLElement | null = null;
  private draftKey = "";
  /** 下一条发送是否带「只把这句话告诉她」的意图（§6.3）；发出后复位 */
  private asContact = false;
  private handoffHint: HTMLElement | null = null;
  private host: HTMLElement | null = null;
  private systemHost: HTMLElement | null = null;
  private clueHost: HTMLElement | null = null;
  private atBottom = true;
  private hasMore = false;
  private oldestSeq = 0;

  constructor(private readonly ctx: AppContext) {}

  async mount(host: HTMLElement): Promise<void> {
    this.host = host;
    const layout = el("div", { class: "u-contact" });
    const left = el("div", { class: "u-col u-col-left" });
    const center = el("div", { class: "u-col u-col-center" });
    const right = el("div", { class: "u-col u-col-right" });
    layout.appendChild(left);
    layout.appendChild(center);
    layout.appendChild(right);
    fill(host, layout);
    this.listHost = left;
    this.clueHost = right;
    // 三栏的顶边要对齐：中栏的第一个元素就是这张头部卡。
    // 原来最上面是一个 h2 页面标题，于是中栏比左右两栏各低一个标题的高度
    // （2026-10-08 视觉体系审查：「三栏读起来不平衡，中栏比两侧低」）。
    // 顶栏已经写着应用名与「角色联络」，这里不再重复一个页面标题。
    const header = el("div", { class: "u-contact-head" });
    const scroll = el("div", { class: "u-messages", tabindex: "0" });
    scroll.addEventListener("scroll", () => {
      this.atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 40;
      if (this.atBottom) this.hideNewHint();
      // 渲染集合有上界（P1-16）：滚到顶部就按需把更早的一页前插回来
      this.maybeLoadOlderOnScroll();
    });
    const newHint = button(
      "有新消息 ↓",
      () => {
        scroll.scrollTop = scroll.scrollHeight;
        this.atBottom = true;
        this.hideNewHint();
      },
      { class: "u-new-hint u-btn", id: "u-contact-new" },
    );
    newHint.hidden = true;
    this.newHint = newHint;
    const live = el("span", { class: "u-sr-only", role: "status", "aria-live": "polite" });
    this.liveHost = live;
    this.systemHost = el("div", { class: "u-system" });
    const composerNote = el("p", { class: "u-note" });
    this.statusNote = composerNote;
    this.draftSlot = el("p", { class: "u-note", id: "u-contact-draft", role: "status", "aria-live": "polite" });
    // 「只把这句话告诉她」的输入框附近提示（§6.3）：只在她这句话送达时出现，发送后复位
    this.handoffHint = el("p", { class: "u-note", id: "u-contact-handoff", role: "status", "aria-live": "polite" });
    // 发送闸的「AI 未配置」提示（审计 Q1 2.5#4）：与按钮同源，别等点了发送才报 llm_not_configured
    const aiGateHint = el("p", {
      class: "u-note u-note-pending",
      id: "u-contact-ai-gate",
      role: "status",
      "aria-live": "polite",
      hidden: true,
    });
    aiGateHint.appendChild(el("span", { text: "还没有配置 AI 服务，暂时不能发送。" }));
    aiGateHint.appendChild(link("去「设置 → AI 服务」配置", () => this.ctx.navigate({ pane: "settings", sub: "ai" })));
    this.aiGateHint = aiGateHint;
    // 连接闸的就地提示（评审 P1「断线不重连」）：未连上时不能只在状态行说一句，
    // 输入框旁边就要有可点的「重新连接」
    const linkGateHint = el("p", {
      class: "u-note u-note-bad",
      id: "u-contact-link-gate",
      role: "status",
      "aria-live": "polite",
      hidden: true,
    });
    linkGateHint.appendChild(el("span", { text: "连接没建立：" }));
    linkGateHint.appendChild(link("重新连接", () => void this.connect()));
    this.linkGateHint = linkGateHint;
    const textarea = el("textarea", {
      class: "u-input u-textarea",
      rows: "3",
      id: "u-contact-input",
      placeholder: "写下想对她说的话……",
      "aria-label": "联络输入",
    }) as HTMLTextAreaElement;
    const send = primary("发送", () => void this.send());
    send.id = "u-contact-send";
    const hint = el("p", { class: "u-note u-muted", text: "Enter 发送 · Shift+Enter 换行" });
    // 状态行与两道发送闸都放进输入区（.u-composer）里、紧挨着「发送」。
    // 为什么：审查说「已连接，可以开始联络」浮在中间的空洞里，「还没有配置 AI 服务…」用告警色
    // 放在页面中部，而真正按不动的「发送」在下面隔着一个输入框——「为什么按不动」必须和按钮在一起。
    // 顺序：输入框 → 状态 / 草稿 / 意图 → 两道闸 → 发送 → 键盘提示（闸紧邻按钮）。
    const form = el(
      "form",
      { class: "u-composer" },
      textarea,
      composerNote,
      this.draftSlot,
      this.handoffHint,
      aiGateHint,
      linkGateHint,
      el("div", { class: "u-col-actions" }, send),
      hint,
    );
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      void this.send();
    });
    textarea.addEventListener("keydown", (event) => {
      if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
        event.preventDefault();
        void this.send();
      }
      if (event.key === "s" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        void this.ctx.drafts.flush(this.draftKey);
      }
    });
    textarea.addEventListener("input", () => this.queueDraft());
    const more = link("加载更早的记录", () => void this.loadOlder());
    more.id = "u-contact-more";
    // 对话区要有容器：左右两栏都有框，中间原来是一个洞（2026-10-08 视觉体系审查）。
    // panel 是一级重量（只有底色、不描边），白色气泡落在底色上，边界一眼可见。
    // 「加载更早的记录」与「有新消息 ↓」都属于对话区，一并放进这个容器里。
    const conversation = panel("对话", el("div", { class: "u-row" }, more), scroll, newHint);
    // 内联的三条是布局必需，不是外观：.u-messages 要 flex:1 才滚得起来，而 .u-panel 默认按内容撑开；
    // position:relative 让「有新消息 ↓」按对话区定位——状态行收进输入区之后输入区变高，
    // 按整列定位的 96px 会盖到输入框上（user.css 是 Lead 的共享契约，不改，所以写在这个节点上）。
    conversation.classList.add("u-conversation");
    fill(center, header, conversation, live, form);
    this.composer = textarea;
    this.sendButton = send;
    this.scrollHost = scroll;
    this.headerHost = header;
    // 系统卡（说明 / 失败卡）属于**对话内容**，跟着消息一起滚（原来每帧重挂一次）：
    // 挂一次、常驻在滚动区末尾，消息节点再按「待定区之前」插到它前面（P1-16）。
    scroll.appendChild(this.systemHost);

    await this.resolveSelection();
  }

  private scrollHost: HTMLElement | null = null;
  private headerHost: HTMLElement | null = null;
  private newHint: HTMLButtonElement | null = null;
  private liveHost: HTMLElement | null = null;
  private rendered = 0;
  /**
   * 已加载消息的节点缓存（P1-16）：`messageId → 气泡节点`。
   * 以前每个事件都 `fill()` 重建整个列表，现在新消息只建自己的节点并按 messageId 插入；
   * 历史行没有 message_id 时按 `#seq` 合成键（`nodeKey`）。
   */
  private msgNodes = new Map<string, HTMLElement>();
  /** 已加载渲染集合里的固定消息键，按插入顺序：超界时从最旧一端移除节点 */
  private msgOrder: string[] = [];
  /** 待定（未落定）消息的节点：ref → 气泡 */
  private pendingNodes = new Map<string, HTMLElement>();
  /** 流式增量预览：messageId → 缓冲。只画在待定气泡里，固化帧到达即整段替换（P0-7） */
  private previews = new Map<string, PreviewDraft>();
  private previewNode: HTMLElement | null = null;
  private previewText: HTMLElement | null = null;
  /** 渲染集合的上界（§6.1 分页读取 + 返回保留位置）：超出时移除最旧节点，滚到顶再按需前插 */
  private readonly renderLimit = 200;
  /** 防止「滚到顶部按需加载」并发重入（同一页只取一次） */
  private loadingOlder = false;
  /** 首屏历史还没落定：这段窗口里不触发前插，免得和初始定位打架 */
  private loadingHistory = false;

  private hideNewHint(): void {
    if (this.newHint) this.newHint.hidden = true;
  }

  /* ---------------------------------------------------------------- 选择与连接 */

  private async resolveSelection(): Promise<void> {
    const stored = (this.ctx.prefs["sel.contact"] as ContactSelection | undefined) ?? undefined;
    const instances = this.ctx.instances();
    // 读不到世界列表 ≠ 「还没有世界」（评审 P1「读取失败≠空态」）：
    // 有错误读数就出错误卡 + 重试，别把已有世界的用户再押去装一遍样例
    if (!instances.length && this.ctx.instancesError) {
      this.renderReadFailure(this.ctx.instancesError);
      return;
    }
    const instance = instances.find((item) => item.id === stored?.instance_id) ?? instances[0];
    if (!instance) {
      this.renderEmpty();
      return;
    }
    const info = await this.ctx.api.instanceInfo(instance.id);
    this.characters = (info.characters as Json[]) ?? [];
    this.timelines = (info.timelines as Json[]) ?? [];
    this.worldName = String(instance.name ?? "");
    const timeline =
      this.timelines.find((item) => String(item.id) === String(stored?.timeline_id ?? "")) ??
      this.timelines.find((item) => String(item.state) !== "archived") ??
      this.timelines[0];
    const character =
      this.characters.find((item) => String(item.card_id) === String(stored?.character_id ?? "")) ?? this.characters[0];
    if (!character || !timeline) {
      this.renderEmpty("这个世界里还没有可联络的角色");
      return;
    }
    this.selection = {
      instance_id: String(instance.id),
      timeline_id: String(timeline.id),
      timeline_name: String(timeline.name ?? ""),
      character_id: String(character.card_id ?? ""),
      character_name: String(character.name ?? ""),
    };
    await this.ctx.setPrefs({ "sel.contact": this.selection });
    this.rememberContact();
    await this.connect();
  }

  /** §3.4 最近使用：选中角色确定后记一条（与写 sel.contact 同一处） */
  private rememberContact(): void {
    const selection = this.selection;
    if (!selection) return;
    this.ctx.rememberRecent({
      pane: "contact",
      label: `${this.worldName} · ${selection.character_name}`,
      key: `contact:${selection.instance_id}:${selection.timeline_id}:${selection.character_id}`,
    });
  }

  private async connect(): Promise<void> {
    const selection = this.selection;
    if (!selection) return;
    this.draftKey = `contact:${selection.instance_id}:${selection.timeline_id}:${selection.character_id}`;
    this.resetAsContact(); // 换对象/重连后不带着上一条的意图（§6.3）
    this.stopWaitClock(); // 换对象 / 重连不再显示上一条的读秒
    this.resetTranscript(); // 节点缓存与未定稿预览也不跨这条连接（P0-7 / P1-16）
    this.hasConnected = false; // 这次 open 的收尾在下面，connected 时不必再重载一次历史
    this.renderHeader();
    this.renderList();
    this.renderCluePanel();
    try {
      this.link?.close();
      this.link = new ChannelLink(this.ctx.endpoint);
      this.link.onEvent((event) => this.onEvent(event));
      this.link.onStateChange((state, note) => this.onLinkState(state, note));
      await this.link.open(this.ctx.api, selection.instance_id, selection.timeline_id, selection.character_id);
      this.setStatus("已连接，可以开始联络", "muted");
      this.connectError?.remove();
      this.connectError = null;
    } catch (error) {
      const info = uiError(error, {
        module: "角色联络",
        action: "连接这个角色",
        target: selection.character_name,
        unknown: "这条联络是否已经建立",
      });
      // 反复重试不该在时间线里堆一串同样的卡片：同一处失败更新原卡（§错误停留在发生处）
      this.connectError?.remove();
      this.connectError = errorCard(info, [{ label: "重新连接", run: () => void this.connect() }]);
      this.appendSystem(this.connectError);
      this.setStatus("连接没有建立", "bad");
    }
    await this.loadHistory();
    await this.loadDraft();
  }

  /**
   * 连接状态播报：状态行说人话，输入闸跟着开合。
   * 断线时必须当场改口（以前这里一直写着「已连接，可以开始联络」）。
   */
  private onLinkState(state: ChannelState, note: string): void {
    if (state === "idle") return; // 主动关闭（换对象 / 离开页面）不改写状态行
    const reconnect = state === "connected" && this.hasConnected;
    this.linkState = state;
    if (state === "connected") {
      this.hasConnected = true;
      this.setStatus("已连接，可以开始联络", "muted");
      // 重连成功后以核心记录为准补齐断线期间错过的回复（与 main.ts 的重连收尾同一做法）
      if (reconnect) void this.loadHistory();
    } else if (state === "reconnecting") {
      this.setStatus(note || "连接已断开，正在重连…", "pending");
    } else if (state === "failed" && this.hasConnected) {
      // 首次连接失败由 connect() 的错误卡说明（这里不抢话）
      this.setStatus("重连没有成功：核心可能已经退出。点下面的「重新连接」可以再试一次。", "bad");
    }
    this.updateGate();
  }

  private async loadDraft(): Promise<void> {
    const saved = await this.ctx.drafts.load(this.draftKey);
    if (saved?.text && this.composer) {
      this.composer.value = saved.text;
      setNote(this.draftSlot, `恢复未发送的内容（保存于 ${stamp(Number(saved.payload?.at ?? Date.now() / 1000))}）`, "muted");
    }
  }

  private queueDraft(): void {
    if (!this.composer) return;
    this.ctx.drafts.watch(
      this.draftKey,
      this.draftSlot,
      "contact",
      `${this.selection?.instance_id ?? ""}:${this.selection?.timeline_id ?? ""}:${this.selection?.character_id ?? ""}`,
      this.composer.value,
      { at: Date.now() / 1000 },
    );
  }

  /* ---------------------------------------------------------------- 渲染 */

  private renderEmpty(reason = "先选一个世界和角色"): void {
    fill(
      this.host as HTMLElement,
      el(
        "div",
        { class: "u-page" },
        el("h2", { class: "u-h2", text: paneTitle("contact") }),
        paragraph(reason),
        el(
          "div",
          { class: "u-row" },
          primary("选择 / 创建世界", () => this.ctx.navigate({ pane: "worlds" })),
          button("从样例世界开始", () => this.ctx.navigate({ pane: "onboarding" })),
        ),
      ),
    );
  }

  /**
   * 世界列表读失败：这是「读不到」，不是「没有世界」。
   * 所以给错误卡 + 重试，不给「从样例世界开始」那种会让人重复安装一遍的入口。
   */
  private renderReadFailure(reason: string): void {
    const info = uiError(new Error(reason), {
      module: "角色联络",
      action: "读取世界列表",
      target: "这台电脑上的世界",
      unknown: "这里有哪些世界",
    });
    fill(
      this.host as HTMLElement,
      el(
        "div",
        { class: "u-page" },
        el("h2", { class: "u-h2", text: paneTitle("contact") }),
        errorCard(info, [
          {
            label: "重试读取",
            run: () => {
              void (async () => {
                await this.ctx.reloadReadings();
                await this.resolveSelection();
              })();
            },
          },
        ]),
        paragraph("已创建的世界不会因此消失：这只是一次读取没有成功。"),
      ),
    );
  }

  private renderHeader(): void {
    const selection = this.selection;
    if (!selection || !this.headerHost) return;
    this.stateChip = chip("读取中…", "pending");
    fill(
      this.headerHost,
      el(
        "div",
        { class: "u-row" },
        el("strong", { text: `${this.worldName} / ${selection.timeline_name}` }),
        this.stateChip,
      ),
      el(
        "div",
        { class: "u-row" },
        button("世界线与版本记录", () => this.ctx.navigate({ pane: "worlds", sub: "timeline" })),
        button("换角色 / 换世界", () => void this.pickTarget()),
      ),
    );
    void this.refreshClock();
  }

  private async refreshClock(): Promise<void> {
    const selection = this.selection;
    if (!selection || !this.stateChip) return;
    try {
      const result = await this.ctx.api.clock(selection.instance_id, selection.timeline_id);
      const clock = (result.clock as Json) ?? {};
      const state = String(clock.state ?? "");
      const processed = Number(clock.processed_world ?? 0);
      const target = Number(clock.world_seconds ?? processed);
      this.stateChip.className = `u-chip u-chip-${state === "active" ? "ok" : state === "frozen" ? "pending" : "muted"}`;
      // 认不出的取值不上屏（内部枚举名对用户没有意义）
      this.stateChip.textContent = state === "active" ? "运行中" : state === "frozen" ? "已暂停" : "状态暂时读不出来";
      // 按钮按真实状态命名：以前写死「暂停 / 启动」，用户看不出点下去会发生哪个（2026-08-08 审计 P2-1）
      this.runState = state;
      if (this.runBtn) {
        this.runBtn.textContent = this.runLabel();
        this.runBtn.title = this.runLabel();
      }
      // 说世界内的时间，不说「已完成的世界时刻 129600000」这种原始数字（评审 P1「状态与术语」）。
      // `label` 是核心按世界历法给出的说法（如「纪元1年一月3日（上午，09:12）」）；
      // 冻结的线返回的 `label` 是「已冻结」（状态名，不是时刻），所以只认 active 的那份读数。
      const worldText = state === "active" && clock.label ? String(clock.label) : `进度点 #${processed}`;
      // 核心只给「已处理」与「按现实时间应该到的」两个数：还没追平时说清正在补齐
      const catching = Boolean(clock.catching_up) || target > processed;
      this.worldLabel = `世界内时间：${worldText}${catching ? "（正在补齐进度）" : ""}`;
      const factsHost = this.headerHost?.querySelector("#u-contact-world");
      if (factsHost) factsHost.textContent = this.worldLabel;
      else
        this.headerHost?.appendChild(el("p", { class: "u-hint", id: "u-contact-world", text: this.worldLabel }));
    } catch {
      this.stateChip.textContent = "状态未知";
    }
  }

  private renderList(): void {
    if (!this.listHost) return;
    const list = el("div", { class: "u-char-list" });
    for (const character of this.characters) {
      const active = String(character.card_id) === this.selection?.character_id;
      const node = button(
        `${String(character.name ?? "")}${character.occupation ? ` · ${String(character.occupation)}` : ""}`,
        () => void this.switchCharacter(String(character.card_id ?? "")),
        { class: `u-char ${active ? "u-char-active" : ""}` },
      );
      node.dataset.card = String(character.card_id ?? "");
      list.appendChild(node);
    }
    const runBtn = button(this.runLabel(), () => void this.toggleRun(), {
      title: this.runLabel(),
      id: "u-contact-run",
    });
    this.runBtn = runBtn;
    fill(
      this.listHost,
      section("角色", list),
      section(
        "这个世界",
        el(
          "div",
          { class: "u-row" },
          button("打开世界", () => this.ctx.navigate({ pane: "worlds", sub: "detail" })),
          runBtn,
        ),
        el("p", { class: "u-hint", text: "暂停只停这条世界线：她不再往后生活；已经发生过的事不会变。" }),
      ),
    );
  }

  /** 运行按钮的文案按真实状态取（读不到状态时如实说，不留一个两义的标签） */
  private runLabel(): string {
    if (this.runState === "active") return "暂停这条世界线";
    if (this.runState === "frozen") return "启动这条世界线";
    return "切换运行状态（状态没读到）";
  }

  private renderCluePanel(): void {
    if (!this.clueHost) return;
    // 说过多少 / 还留着多少：一句话读不出比例，用一段条表示（内容依然不上屏）
    const summary = el("div", { id: "u-clue-bar" });
    const clues = el("ol", { class: "u-list", id: "u-clues" });
    fill(
      this.clueHost,
      section(
        "已讲过的线索",
        summary,
        clues,
        el(
          "div",
          { class: "u-row" },
          button("刷新线索", () => void this.loadClues()),
          primary("转述给另一位角色", () => void this.startDisclosure()),
        ),
        el("p", { class: "u-hint", text: "只列她已经讲出口的内容；没讲出口的只有一个记号，不带正文。" }),
      ),
    );
    void this.loadClues();
  }

  private async loadClues(): Promise<void> {
    const selection = this.selection;
    const host = this.clueHost?.querySelector("#u-clues");
    const bar = this.clueHost?.querySelector("#u-clue-bar");
    if (!selection || !host) return;
    try {
      const result = await this.ctx.api.narrativeMap(selection.instance_id, selection.timeline_id, selection.character_id);
      const map = (result.map as Json) ?? {};
      const nodes = (map.nodes as Json[]) ?? [];
      // 图谱给的字段是 kind / label（见 runtime.narrative.map_payload）——按 stage / text 取会永远空
      const spoken = nodes.filter((item) => String(item.kind ?? "") === "spoken");
      const held = nodes.filter((item) => String(item.kind ?? "") !== "spoken");
      if (bar) {
        // 讲过多少 / 还留多少：一句话读不出比例，画成一段条（图例带数值，不看颜色也能读）
        const graph = stackBar([
          { label: "已经讲出口", value: spoken.length, tone: "ok" },
          { label: "还没讲出口", value: held.length, tone: "muted" },
        ]);
        // 两个数都是 0 时不画空条（graphics.ts 的纪律：不画空数据）——空条会被读成「比例是 0」，
        // 而这里其实是「还没有可统计的内容」，退回一句文字
        fill(bar, graph ?? el("span", { class: "u-hint", text: "还没有线索：讲过与没讲过的都是空的。" }));
      }
      fill(
        host,
        ...spoken.map((item) =>
          el(
            "li",
            {},
            button(
              String(item.label ?? "（这条内容已经不在当前记录里）"),
              () => void this.focusMessage(String(item.message_id ?? "")),
              { class: "u-btn u-ghost" },
            ),
          ),
        ),
        held.length
          ? el("li", { class: "u-hint", text: `另有 ${held.length} 件她还没讲出口的事（内容不在这里显示）` })
          : null,
      );
      if (!spoken.length) host.appendChild(el("li", { class: "u-hint", text: "她还没讲过什么。" }));
    } catch (error) {
      const info = uiError(error, { module: "角色联络", action: "读取线索" });
      fill(host, el("li", { class: "u-note u-note-bad", text: info.message }));
    }
  }

  private async focusMessage(messageId: string): Promise<void> {
    if (!this.scrollHost) return;
    const node = this.scrollHost.querySelector(`[data-message="${messageId}"]`);
    if (node) {
      node.scrollIntoView({ block: "center" });
      node.classList.add("u-msg-highlight");
      window.setTimeout(() => node.classList.remove("u-msg-highlight"), 1600);
      return;
    }
    this.setStatus("这条内容在更早的记录里，正在读取…", "pending");
    await this.loadOlder(true);
    const found = this.scrollHost.querySelector(`[data-message="${messageId}"]`);
    found?.scrollIntoView({ block: "center" });
  }

  /* ---------------------------------------------------------------- 历史与消息 */

  /**
   * 读最近一页历史。P1-16：**只追加新出现的消息**，不再整体重建列表。
   *
   * 挂载、重连、查询结果这三处都会走到这里，而它们的语义都是「以核心记录为准补齐」：
   * 所以按 messageId 取差集，只创建并插入差集里的节点（已画过的原样留着）。
   */
  private async loadHistory(): Promise<void> {
    const selection = this.selection;
    if (!selection) return;
    this.loadingHistory = true;
    try {
      const session = (await this.ctx.api.sessionEnsure(selection.instance_id, selection.timeline_id, selection.character_id))
        .session as Json;
      const page = await this.ctx.api.history(String(session.id), undefined, 50);
      const rows = (page.messages as Json[]) ?? [];
      // 核心记录里已经有那一条入站（queued / processing 也进历史），但 pending 里还在等：
      // 保留 pending 那个带读秒的气泡，别再画第二个「我说的那句话」
      const waiting = new Set(this.pending.map((item) => item.ref));
      const fresh = rows
        .map((row) => this.fromRow(row))
        .filter((message) => !(message.role === "user" && message.ref && waiting.has(message.ref)));
      this.hasMore = Boolean(page.has_more);
      this.oldestSeq = Number(page.next_before_seq ?? 0);
      // 差集：老消息在前、新消息在后，按顺序补齐（已在渲染集合里的键跳过）
      for (const message of fresh) {
        const key = this.nodeKey(message);
        if (this.msgNodes.has(key)) continue;
        this.messages.push(message);
        this.insertMessageNode(message, "end");
      }
      this.renderMessages();
    } finally {
      this.loadingHistory = false;
    }
  }

  /**
   * 读更早的一页（§6.1 分页读取）：只**前插**旧页的节点，不动已画好的那些。
   *
   * 滚动锚点用 `scrollHeight - scrollTop` 重算（P1-16）：前插会改变 scrollHeight，
   * 补回同样的差值就等于「视口顶部那条还在原处」，而不是整体重建后靠强制滚动找位置。
   * 滚到顶部时按需加载也是从这里进来的。
   */
  private async loadOlder(keepAnchor = false): Promise<void> {
    const selection = this.selection;
    if (!selection || !this.hasMore) return;
    const session = (await this.ctx.api.sessionEnsure(selection.instance_id, selection.timeline_id, selection.character_id))
      .session as Json;
    const page = await this.ctx.api.history(String(session.id), this.oldestSeq, 50);
    const rows = (page.messages as Json[]) ?? [];
    const older = rows.map((row) => this.fromRow(row));
    this.hasMore = Boolean(page.has_more);
    this.oldestSeq = Number(page.next_before_seq ?? 0);
    this.messages = [...older, ...this.messages];
    const host = this.scrollHost;
    const anchor = host ? host.scrollHeight - host.scrollTop : 0;
    // 倒序前插：每一页内部仍按时间正序（从这页最后一条往前插到最前面）
    for (let index = older.length - 1; index >= 0; index -= 1) this.insertMessageNode(older[index], "start");
    this.trimRendered();
    if (host) host.scrollTop = host.scrollHeight - anchor; // 锚点重算：前插多少补回多少
    this.renderMessages(keepAnchor === false && this.atBottom);
  }

  /**
   * 滚动到（接近）顶部时按需加载更早的一页（P1-16 的上界配套）：
   * 渲染集合有上界，被裁掉的旧消息靠这里按需取回，不必一开机就把全部历史画出来。
   */
  private maybeLoadOlderOnScroll(): void {
    const host = this.scrollHost;
    if (!host || !this.hasMore || this.loadingOlder || !this.selection) return;
    if (this.loadingHistory) return; // 首屏还没落定：滚动位置还在底部摆，别急着前插
    if (host.scrollTop > 24) return;
    this.loadingOlder = true;
    void this.loadOlder(true).finally(() => {
      this.loadingOlder = false;
    });
  }

  private fromRow(row: Json): Message {
    const parts = (row.parts as string[][] | null) ?? null;
    return {
      role: String(row.role ?? "user") as Message["role"],
      text: String(row.text ?? (parts ? parts.flat().join("") : "")),
      seq: Number(row.seq ?? 0),
      at: Number(row.created_at ?? 0),
      messageId: String(row.message_id ?? row.reply_message_id ?? ""),
      state: String(row.state ?? ""),
      ref: String(row.env_id ?? ""),
      replyTo: String(row.reply_to ?? ""),
      parts: [],
    };
  }

  /**
   * 渲染集合里的键（P1-16 要求按 messageId 增量追加）：
   *   1. 有 message_id 就用它；
   *   2. 落定的「我说的那句话」用入站 `ref`（同一 ref 只落一次，稳定可复算）；
   *   3. 其余历史行用 `#seq` 兜底（核心给的历史行带 seq，同页内唯一）。
   */
  private nodeKey(message: Message): string {
    if (message.messageId) return `m:${message.messageId}`;
    if (message.ref) return `r:${message.ref}`;
    return `s:${message.seq}:${message.role}`;
  }

  /** 待定区的插入锚点：第一条 pending 气泡 / 未固化预览；都没有就落到系统卡之前 */
  private pendingAnchor(): Node | null {
    for (const node of this.pendingNodes.values()) return node;
    if (this.previewNode) return this.previewNode;
    return this.systemHost;
  }

  /**
   * 往滚动区里插节点：锚点必须**确实是**它的子节点，否则退化为追加。
   *
   * 锚点可能已经被摘掉（待定气泡落定、预览被清、系统卡尚未挂上）：那时 `insertBefore`
   * 会抛 `Failed to execute 'insertBefore' on 'Node'`，把整条消息链打断——这里就地兜住。
   */
  private insertInto(host: HTMLElement, node: HTMLElement, anchor: Node | null): void {
    if (anchor && anchor.parentNode === host) host.insertBefore(node, anchor);
    else host.appendChild(node);
  }

  /**
   * 把一条固定消息插进列表（不重建）。
   * `end` = 追加到待定区之前（新消息）；`start` = 插到列表最前（旧页前插）；
   * `pending` = 接在待定气泡原来的位置上（这一条刚刚落定）。
   */
  private insertMessageNode(message: Message, where: "start" | "end" | "pending"): HTMLElement | null {
    const host = this.scrollHost;
    if (!host) return null;
    const key = this.nodeKey(message);
    const existing = this.msgNodes.get(key);
    if (existing) return existing; // 同一个键只画一次（重复投递 / 历史与流各来一份）
    const node = this.renderMessage(message, key);
    if (where === "start") {
      if (host.firstChild) host.insertBefore(node, host.firstChild);
      else host.appendChild(node);
      this.msgOrder.unshift(key);
    } else if (where === "pending" && this.pendingNodes.size) {
      // 这条待定气泡落定：新节点接在它原来的位置上（它是这条消息的「从此处开始」锚点）
      const seat = this.pendingNodes.values().next();
      if (!seat.done && seat.value.parentNode === host) seat.value.replaceWith(node);
      else this.insertInto(host, node, this.pendingAnchor());
      this.msgOrder.push(key);
    } else {
      // 新消息落在待定区之前：pending 是「还没落定」的那些，新固化消息不该排到它们后面
      this.insertInto(host, node, this.pendingAnchor());
      this.msgOrder.push(key);
    }
    this.msgNodes.set(key, node);
    this.emptySlot()?.remove();
    return node;
  }

  /** 空态提示节点（只应有一个，且只在真的一条都没有时出现） */
  private emptySlot(): HTMLElement | null {
    return this.scrollHost?.querySelector<HTMLElement>(".u-msg-empty") ?? null;
  }

  /** 渲染集合的上界（P1-16）：超界只移除**最旧**的节点（列表前端的那些），不重建剩下的 */
  private trimRendered(): void {
    while (this.msgOrder.length > this.renderLimit) {
      const key = this.msgOrder.shift();
      if (!key) break;
      this.msgNodes.get(key)?.remove();
      this.msgNodes.delete(key);
    }
  }

  /**
   * 渲染收尾：滚动锚点用 `scrollHeight - scrollTop` 重算（不再整体重建），
   * 「有新消息 ↓」只在内容真的变多、且用户不在底部时出现。
   */
  private renderMessages(force = false): void {
    if (!this.scrollHost) return;
    const wasAtBottom = this.atBottom;
    const before = this.scrollHeight();
    const wasTop = this.scrollHost.scrollTop;
    if (force || wasAtBottom) this.scrollHost.scrollTop = this.scrollHeight();
    else {
      // 不在底部：按新增高度平移滚动位置，视口内容原地不动；新内容只给一个可点的入口（§6.1）
      this.scrollHost.scrollTop = this.scrollHeight() - before + wasTop;
      if (this.totalRendered() > this.rendered && this.newHint) this.newHint.hidden = false;
    }
    const total = this.totalRendered();
    this.rendered = total;
    // 一条对话都没有、也没有系统卡时，在容器里居中给一句提示 + 一个动作：
    // 以前这里是一大片纯白，既看不出「这里会有内容」，也没有「从哪儿开始写」的出口
    // （2026-10-08 视觉体系审查：「中间栏是一大片空白，对话区没有容器」）。
    if (!total && !this.systemHost?.childElementCount && !this.emptySlot()) {
      this.scrollHost.appendChild(this.emptyConversation());
    }
    const more = this.host?.querySelector("#u-contact-more") as HTMLButtonElement | null;
    if (more) more.hidden = !this.hasMore;
    this.updateGate();
  }

  private scrollHeight(): number {
    return this.scrollHost?.scrollHeight ?? 0;
  }

  /** 渲染集合的条数（固定消息节点 + 待定气泡 + 预览），空态提示不算 */
  private totalRendered(): number {
    return this.msgOrder.length + this.pendingNodes.size + (this.previewNode ? 1 : 0);
  }


  /** 空对话：容器内居中一句提示 + 一个动作（动作就是把光标放到输入框上） */
  private emptyConversation(): HTMLElement {
    const box = el(
      "div",
      { class: "u-msg-empty" },
      el("p", { class: "u-hint", text: "还没有对话：写下第一句话，她就会回你。" }),
      button("写第一句话", () => this.focusComposer(), { class: "u-btn u-ghost" }),
    );
    // 居中要自己给：user.css 是 Lead 的共享契约（不改），这里只调这一个节点的外观
    box.style.margin = "auto";
    box.style.textAlign = "center";
    box.style.display = "flex";
    box.style.flexDirection = "column";
    box.style.alignItems = "center";
    box.style.gap = "8px";
    return box;
  }

  /** 把光标放到输入框：空态与「保留原文继续编辑」共用同一处出口 */
  private focusComposer(): void {
    if (this.composer && !this.composer.disabled) {
      this.composer.focus();
      return;
    }
    // 连不上时输入框是禁用的，点了没反应：就近说清为什么，别让人以为按钮坏了
    this.setStatus("现在还不能写：先按输入框旁边的「重新连接」，或者去「设置 → AI 服务」把密钥填好。", "pending");
  }

  private renderMessage(message: Message, key = ""): HTMLElement {
    // 说明不冒充任何一方：既不进「我」的气泡，也不进她的气泡，统一是系统说明卡
    if (message.role === "notice") return this.noticeCard(message);
    const cls = message.role === "character" ? "u-bubble u-bubble-them" : "u-bubble u-bubble-me";
    const node = el("div", { class: cls, "data-message": message.messageId });
    // 历史行没有 message_id：给一个稳定的渲染键，增量更新时照旧能按节点对上
    if (!message.messageId && key) node.setAttribute("data-message-key", key);
    node.appendChild(el("p", { class: "u-bubble-text", text: message.text }));
    const meta = el("div", { class: "u-bubble-meta" });
    meta.appendChild(el("span", { text: stamp(message.at) }));
    if (message.role === "user") meta.appendChild(el("span", { text: stateText(message.state) }));
    if (message.role === "character" && this.characters.length > 1) {
      meta.appendChild(
        link("转述给另一位角色", () => void this.startDisclosure(message.messageId), "u-link u-link-small"),
      );
    }
    node.appendChild(meta);
    return node;
  }

  /**
   * 待定气泡：每个 ref 一个节点（P1-16 增量更新）。
   * 状态、错误卡这类变化就地重画这一个节点，不牵动整个列表——以前这里每个事件都重建全表。
   */
  private renderPending(item: PendingMessage): HTMLElement {
    const node = el("div", { class: "u-bubble u-bubble-me u-bubble-pending" });
    node.appendChild(el("p", { class: "u-bubble-text", text: item.text }));
    const meta = el("div", { class: "u-bubble-meta" });
    meta.appendChild(el("span", { text: stateText(item.state) }));
    node.appendChild(meta);
    if (item.error) {
      node.appendChild(
        errorCard(item.error, [
          { label: "保留原文继续编辑", run: () => this.restoreToComposer(item) },
          { label: "查询这条的结果", run: () => void this.queryResult(item.ref) },
        ]),
      );
    } else if (item.state === "unknown") {
      // 「结果待确认」只给两条路：等，或查这一条。
      // 不再劝「继续编辑原文重发」——每次重发在 ump.ts 里都是新的 env_id，等于一条新请求
      // （与文件头的发送纪律冲突：结果未知先查，不重复当新消息发出）。
      const note = el("p", { class: "u-note u-note-pending" });
      note.appendChild(el("span", { text: "还没收到确认：可以再等一会儿，或者" }));
      note.appendChild(link("查询这条的结果", () => void this.queryResult(item.ref)));
      note.appendChild(el("span", { text: "。这条已经发出去了，重发会变成一条新消息。" }));
      node.appendChild(note);
    }
    return node;
  }

  private restoreToComposer(item: { text: string }): void {
    if (!this.composer) return;
    this.composer.value = item.text;
    // 与空态的出口共用一处：连不上时这里也说清为什么光标进不去输入框
    this.focusComposer();
    this.queueDraft();
  }

  /* --------------------------------------------------- 流式增量预览（P0-7） */

  /**
   * 收到一段 `reply_delta`：并入这条 messageId 的预览缓冲，并把它画进**待定气泡**里。
   *
   * 三条纪律（CHANNEL_PLUGIN_SPEC §七 / USER_INTERFACE_DESIGN §6.2）：
   *   - 增量不是已固化正文：只画在 `u-bubble-preview` 这一处，绝不写进 `messages`；
   *   - 固化帧 `reply` 到达时整段替换（多批回复也照整段走），预览随即消失；
   *   - 段序只往前走：重复 / 乱序的旧段不重复拼接，免得预览跳字。
   */
  private applyDelta(messageId: string, index: number, text: string): void {
    if (!messageId || !text) return;
    const draft = this.previews.get(messageId) ?? { messageId, text: "", index: -1 };
    if (index <= draft.index) return;
    draft.text += text;
    draft.index = index;
    this.previews.set(messageId, draft);
    this.renderPreview(draft);
  }

  /** 预览节点就地更新文本（不重建列表）；用户不在底部时保持视口稳定 */
  private renderPreview(draft: PreviewDraft): void {
    const host = this.scrollHost;
    if (!host) return;
    if (!this.previewNode) {
      const node = el("div", {
        class: "u-bubble u-bubble-them u-bubble-preview",
        role: "status",
        // 增量逐段到达：这里逐字改文本会不停打断读屏，所以预览自己**不播报**
        // （固化完成由 `.u-sr-only` 的 liveHost 播「收到一条新回复」，与原来一致）
        "aria-live": "off",
      });
      const body = el("p", { class: "u-bubble-text" });
      node.appendChild(body);
      // 未固化要说清：这是她正在说、还没定稿的一句
      node.appendChild(el("p", { class: "u-note u-note-pending", text: "正在生成…（这段还没定稿）" }));
      this.previewText = body;
      this.previewNode = node;
      // 预览排在待定气泡之后、系统卡之前：它是这一轮正在到达的内容
      this.insertInto(host, node, this.systemHost);
    }
    if (this.previewText) this.previewText.textContent = draft.text;
    this.renderMessages();
  }

  /**
   * 丢一条预览：`messageId` 缺省表示「所有还没定稿的预览」。
   * 固化帧到达、这一轮失败、或换对象 / 重连时清理；**不**当作正文落进列表。
   */
  private clearPreview(messageId?: string): void {
    if (messageId === undefined) this.previews.clear();
    else this.previews.delete(messageId);
    if (!this.previews.size) {
      this.previewNode?.remove();
      this.previewNode = null;
      this.previewText = null;
      return;
    }
    // 还有别的在途回复：把节点换成剩下那条（后到的排在前面会更接近真实到达顺序，这里取第一条）
    const next = this.previews.values().next();
    if (!next.done && this.previewText) this.previewText.textContent = next.value.text;
  }

  /* ------------------------------------------------------- 入站的落定与查询 */

  /**
   * 把 pending 里的这一条转成 `messages` 里的用户记录，并移出 pending。
   *
   * 为什么必须转（评审 P0-3）：以前 reply 到达时直接把 pending 那项删掉、只 push 角色那条，
   * 于是用户刚发的话在回复出现的一刻就消失了。现在同一个 ref 只转一次，并把新节点插进列表。
   */
  private settleInbound(ref: string, state: string): void {
    const index = this.pending.findIndex((item) => item.ref === ref);
    if (index < 0) return;
    const item = this.pending[index];
    this.pending.splice(index, 1);
    const pendingNode = this.pendingNodes.get(ref);
    this.pendingNodes.delete(ref);
    if (!item.ref) {
      pendingNode?.remove();
      return;
    }
    if (this.messages.some((message) => message.role === "user" && message.ref === item.ref)) {
      pendingNode?.remove();
      return;
    }
    const message: Message = {
      role: "user",
      text: item.text,
      seq: 0,
      at: item.at,
      messageId: "",
      state,
      ref: item.ref,
      replyTo: "",
      parts: [],
    };
    this.messages.push(message);
    // 落定的「我说的那句话」接在待定气泡原来的位置上（nodeKey 用 ref，稳定可复算）
    this.insertMessageNode(message, "pending");
  }

  /**
   * 「查询这条的结果」：按这条请求的身份（`ref` = 入站 `env_id`）去会话历史里对那一行。
   *
   * 为什么不再调 `storyTurn`（评审 P1）：那是「这个角色最近一轮」的读数，与这条 ref 无关，
   * 而且以前把 `请求 xx 的状态：${state}` 这种内部话术写上了状态行。这里只查这一条，
   * 状态码一律走人话映射，认不出的不显示，也不显示 ref 片段。
   */
  private async queryResult(ref: string): Promise<void> {
    const selection = this.selection;
    if (!selection || !ref) return;
    try {
      const session = (await this.ctx.api.sessionEnsure(
        selection.instance_id,
        selection.timeline_id,
        selection.character_id,
      )).session as Json;
      const page = await this.ctx.api.history(String(session.id), undefined, 50);
      const rows = (page.messages as Json[]) ?? [];
      const mine = rows.find((row) => String(row.env_id ?? "") === ref);
      const answered = rows.some(
        (row) => String(row.role ?? "") === "character" && String(row.reply_to ?? "") === ref,
      );
      // 最近 50 条里没有这一条（很久以前发的）就退到「这个会话最近一次投递」，并说明是退而查的
      const latest = [...rows].reverse().find((row) => String(row.role ?? "") === "user");
      const fallback = Boolean(!mine && latest);
      const state = String((mine ?? latest)?.state ?? "");
      const text = deliveryText(state, answered);
      const prefix = fallback ? "这条在最近的记录里找不到，本会话最近一次投递：" : "这条消息：";
      this.setStatus(`${prefix}${text}`, deliveryKind(state, answered));
      // 已经确认有回复：把它从 pending 落进列表，并把核心记录里的回复读回来（不再让用户干等）
      if (mine && (state === "done" || state === "fixed")) {
        this.settleInbound(ref, state);
        this.stopWaitClock();
        await this.loadHistory();
      } else if (mine && (state === "queued" || state === "processing" || state === "accepted")) {
        // 查出来「还在处理」：把读秒接回去（可能刚从「结果待确认」回来），别让用户以为这一条断了
        const item = this.pending.find((entry) => entry.ref === ref);
        if (item && item.state === "unknown") {
          item.state = "accepted";
          this.refreshWaitClock();
          this.refreshPendingNode(item);
        }
      }
    } catch (error) {
      this.setStatus(uiError(error, { module: "角色联络", action: "查询这条的结果" }).message, "bad");
    }
  }

  /* ------------------------------------------------------- 等待态的读秒 */

  /**
   * 等待态的一行要有时长预期与已等秒数（评审 P1「等待与连接」）：
   * 30–120 秒是常态，看不到时间用户会以为程序卡死。计时器只负责这一行，
   * 收到回复 / 失败 / 换对象都必须停掉（否则定时器会一直挂着）。
   */
  private startWaitClock(): void {
    if (this.waitTimer !== null) return; // 已在读秒：不重置起点，连续等待的秒数才连贯
    const active = this.pending.filter((item) => item.state === "submitting" || item.state === "accepted");
    // 起点取「这条真正发出去的时刻」：重连、查询这些动作不该让已等的秒数归零
    this.waitSince = Math.round((active[0]?.at ?? Date.now() / 1000) * 1000);
    this.waitTimer = window.setInterval(() => this.renderWaiting(), 1000);
    this.renderWaiting();
  }

  private stopWaitClock(): void {
    if (this.waitTimer !== null) window.clearInterval(this.waitTimer);
    this.waitTimer = null;
    this.waitSince = 0;
  }

  /** 还有在等的请求就保持读秒，没有就停掉（每个改变 pending 的地方都调它） */
  private refreshWaitClock(): void {
    const waiting = this.pending.some((item) => item.state === "submitting" || item.state === "accepted");
    if (waiting) this.startWaitClock();
    else this.stopWaitClock();
  }

  private renderWaiting(): void {
    const submitting = this.pending.some((item) => item.state === "submitting");
    const waited = Math.max(0, Math.round((Date.now() - this.waitSince) / 1000));
    if (submitting) {
      this.setStatus(`正在提交…（已等 ${waited} 秒）`, "pending");
      return;
    }
    this.setStatus(
      `已接收，正在等待回应（她那边可能正处在休息时段，最长约 2 分钟；已等 ${waited} 秒）`,
      "pending",
    );
  }

  /* ---------------------------------------------------------------- 发送 */

  private async send(): Promise<void> {
    const selection = this.selection;
    const text = this.composer?.value.trim() ?? "";
    if (!selection || !text) return;
    // 与按钮同源的拒发：未配置 AI 服务时不白白发出去再吃 llm_not_configured，并说清去哪儿配
    if (this.aiGate() === "missing") {
      this.setStatus("还没有配置 AI 服务：先去「设置 → AI 服务」填好地址与密钥再发送；写下的内容会保留。", "pending");
      return;
    }
    if (this.thinking) {
      this.setStatus("还在等上一条的回应；可以把下一条先写好，她回完这一条才会收到下一条。", "pending");
    }
    let ref = "";
    try {
      if (!this.link) throw new Error("还没有连上这个角色：先点「重新连接」");
      ref = this.link.send(text, { asContact: this.asContact });
    } catch (error) {
      this.setStatus(uiError(error, { module: "角色联络", action: "发送" }).message, "bad");
      return;
    }
    // 意图只跟着这一条走：发出去就复位，下一次回到普通联络（§6.3）
    this.resetAsContact();
    this.pending.push({ ref, text, state: "submitting", at: Date.now() / 1000 });
    this.insertPendingNode(ref);
    if (this.composer) this.composer.value = "";
    await this.ctx.drafts.discard(this.draftKey);
    setNote(this.draftSlot, "", "muted");
    this.refreshWaitClock(); // 「正在提交…（已等 N 秒）」
    this.renderMessages();
    // 确认前失败原文仍可编辑：等不到确认就标「结果待确认」，并把出口收窄成
    // 「查这一条」与「再等等」（重发在新请求身份下会变成一条新消息，与发送纪律冲突）
    window.setTimeout(() => {
      const item = this.pending.find((entry) => entry.ref === ref);
      if (item && item.state === "submitting") {
        item.state = "unknown";
        this.refreshWaitClock();
        this.setStatus("这条还没收到确认：可以点「查询这条的结果」，或者再等一会儿。已经发出去了，不要重复发。", "pending");
        this.refreshPendingNode(item);
      }
    }, 8000);
  }

  /**
   * 新建一条待定气泡并插进列表（P1-16：只建这一个节点）。
   * 位置在系统卡之前、未定稿预览之后——它是刚刚发出去、还没落定的那一条。
   */
  private insertPendingNode(ref: string): void {
    const host = this.scrollHost;
    const item = this.pending.find((entry) => entry.ref === ref);
    if (!host || !item) return;
    const node = this.renderPending(item);
    // 待定气泡按发送顺序排在待定区末尾（系统卡之前）；后到的排最后，与真实发送顺序一致
    const anchor = this.previewNode ?? this.systemHost;
    host.insertBefore(node, anchor);
    this.pendingNodes.set(ref, node);
    this.emptySlot()?.remove();
  }

  /**
   * 就地把一条待定气泡重画成它的当前状态（确认 / 失败 / 结果待确认）。
   * 只换这一个节点：以前这类状态变化会把整张消息表重建一遍（P1-16）。
   */
  private refreshPendingNode(item: PendingMessage): void {
    const node = this.pendingNodes.get(item.ref);
    if (!node) {
      this.renderMessages();
      return;
    }
    const fresh = this.renderPending(item);
    node.replaceWith(fresh);
    this.pendingNodes.set(item.ref, fresh);
    this.renderMessages();
  }

  private onEvent(event: ChannelEvent): void {
    if (event.kind === "delta") {
      // 增量预览（P0-7）：只画在待定气泡里，不落进 `messages`
      this.applyDelta(event.messageId, event.index, event.text);
      return;
    }
    if (event.kind === "reply") {
      // 顺序要紧：先把「我说的那句话」从 pending 落进列表，再追加她的回复（评审 P0-3）
      this.settleInbound(event.replyTo, "done");
      // 固化帧是唯一事实：整段替换预览（后验检查可能改过字），多批回复也按整段覆盖
      this.clearPreview(event.messageId);
      const existing = this.messages.find((item) => item.messageId === event.messageId);
      if (!existing) {
        const message: Message = {
          role: "character",
          text: event.parts.join(""),
          seq: 0,
          at: event.at,
          messageId: event.messageId,
          state: "fixed",
          ref: "",
          replyTo: event.replyTo,
          parts: [event.batchIndex],
        };
        this.messages.push(message);
        this.insertMessageNode(message, "end");
      } else if (!existing.parts.includes(event.batchIndex)) {
        existing.parts.push(event.batchIndex);
        existing.text += event.parts.join("");
        // 追加批次：找到这个 messageId 的节点，只换它的正文（不重建整个列表）
        const node = this.msgNodes.get(this.nodeKey(existing));
        const body = node?.querySelector<HTMLElement>(".u-bubble-text");
        if (body) body.textContent = existing.text;
      }
      this.thinking = false;
      this.link?.confirmDelivery(event.messageId, event.batchIndex, "accepted");
      this.renderMessages();
      if (this.liveHost) this.liveHost.textContent = "收到一条新回复"; // 读屏播报；不朗读全文、不逐秒播报
      this.setStatus("回复已保存", "ok");
      this.refreshWaitClock(); // 还有别的在等就接着读秒，没有就停掉计时器
      return;
    }
    if (event.kind === "notice") {
      // 转交说明意味着这一轮不会再给回复：把还在等待的那条先落进列表（同一时刻也停掉读秒），
      // 这样「我发的那句话」留在对话里，下面这张通知卡也能按 ref 精确对上原文。
      // 实时通知信封不带 reply_to（核心只给 reply 帧带），只能按转交说明的定性词认。
      if (this.isHandoffNotice(event.text)) {
        const waiting = [...this.pending]
          .reverse()
          .find((item) => item.state === "accepted" || item.state === "submitting" || item.state === "unknown");
        if (waiting) {
          this.settleInbound(waiting.ref, "cancelled");
          this.clearPreview();
        }
      }
      const message: Message = {
        role: "notice",
        text: event.text,
        seq: 0,
        at: event.at,
        messageId: event.messageId,
        state: "fixed",
        ref: "",
        replyTo: event.replyTo,
        parts: [],
      };
      this.messages.push(message);
      this.insertMessageNode(message, "end");
      // 说明也要回执（单批）：不回执的话这条永远算未确认，每次重连都会被当成待投递重发一遍
      this.link?.confirmDelivery(event.messageId, 0, "accepted");
      // 只经 messages 流渲染这一次；再补一张卡就是同一通知出现两份
      this.refreshWaitClock();
      this.renderMessages();
      return;
    }
    if (event.kind === "accepted") {
      const item = this.pending.find((entry) => entry.ref === event.ref);
      if (item) {
        if (event.state === "cancelled") {
          // 作废是终局（转交到别的工作区，或这一轮被回滚 / 重绑作废）：这一轮不会再有回复。
          // 落进列表，别留一个永远「等待中」的气泡（转交说明随后到达，按 ref 对上原文）
          this.settleInbound(event.ref, "cancelled");
          this.clearPreview();
          this.refreshWaitClock();
          this.renderMessages();
          return;
        }
        item.state = event.state === "failed" ? "failed" : "accepted";
        if (item.state === "accepted") this.setStatus("已接收，正在等待回应", "pending");
        if (item.state === "failed") {
          item.error = uiError(new Error("这次没有生成成功"), {
            module: "角色联络",
            action: "生成回复",
            done: "你的消息已经被核心保存",
            unknown: "她这一轮是否有回复",
          });
        }
        // 重复投递的已完结请求：核心回带 done，说明这一条早就有了回复——
        // 同样把它落进列表（同一个 ref 只转一次），免得界面看起来还在等
        if (event.state === "done" || event.state === "fixed") {
          this.settleInbound(event.ref, event.state);
        } else {
          this.refreshPendingNode(item);
        }
        this.refreshWaitClock();
        this.renderMessages();
      }
      return;
    }
    if (event.kind === "status") {
      this.thinking = event.state === "thinking";
      if (this.thinking) this.setStatus("已接收，正在等待回应", "pending");
      this.refreshWaitClock(); // 等待中的读秒跟着状态走
      return;
    }
    if (event.kind === "binding") {
      if (event.state === "revoked") {
        this.setStatus("这条联络刚刚在别处重新连接过（旧凭据已作废）：正在重新读取记录", "pending");
        void this.loadHistory();
      }
      return;
    }
    if (event.kind === "error") {
      const item = this.pending.find((entry) => entry.ref === event.ref);
      const info = uiError(new Error(event.message || event.code), {
        module: "角色联络",
        action: "这一轮生成",
        done: item ? "你的消息已经被核心保存" : "没有改动",
        unknown: "她这一轮是否有回复",
      });
      if (item) {
        item.state = "failed";
        item.error = info;
      }
      this.appendSystem(errorCard(info, [{ label: "重新获取回复", run: () => this.link?.retry(event.ref, "input") }]));
      this.refreshWaitClock();
      this.renderMessages();
    }
  }

  /** 转交类说明的固定出口：只有这些通知才带按钮与「尚未改变」那句（§6.3） */
  private static readonly handoffTargets: Array<{
    key: string;
    /** 与核心通知正文对齐的稳定短语（isekai_core/story/classify.py HANDOFF_NOTICES 开头的定性词）。
        改核心文案时这里必须一起改——匹配是「有没有按钮」的唯一依据（2026-10-07 探针实锤过）。
        这个字段是匹配用的键，不是给用户看的文案：行话（如「TRPG 行动」）由下面的 body 说成人话 */
    match: string;
    label: string;
    /** 上屏的正文：核心那份带行话（如「TRPG 行动」），这里换成人话；改核心文案时两处一起看 */
    body: string;
    pane: "worlds" | "writing" | "contact" | "trpg";
  }> = [
    {
      key: "creation",
      match: "创作请求",
      label: "草案",
      body: "这条是创作请求，普通对话没有执行它。要改世界请走创作流程：先看影响预览，确认之后才生效。",
      pane: "worlds",
    },
    {
      key: "version",
      match: "版本操作",
      label: "版本与恢复",
      body: "这条属于版本操作（保存分支 / 恢复版本 / 导入导出），已经交给版本流程。普通对话不会替你回滚或分叉。",
      pane: "worlds",
    },
    {
      key: "trpg",
      match: "TRPG 行动",
      label: "跑团",
      body: "这条属于跑团里的行动：行动要经过规则裁定，普通对话不会替你掷骰，也不给成功或失败结论。",
      pane: "trpg",
    },
  ];

  /** 这条说明是不是「转交」类（按核心的定性词认；核心的兜底话术也认） */
  private isHandoffNotice(text: string): boolean {
    return (
      ContactPane.handoffTargets.some((item) => text.includes(item.match)) || text.includes("不在普通联络里处理")
    );
  }

  /**
   * role=notice 的呈现：转交类带按钮，其余（追赶、离场等）同款卡但不带任何按钮。
   * live 与历史重载走同一条路径，所以两边的样子一致。
   */
  private noticeCard(message: Message): HTMLElement {
    const node = el("div", { class: "u-handoff", "data-message": message.messageId });
    const hit = ContactPane.handoffTargets.find((item) => message.text.includes(item.match));
    // 认得出是哪一类就说人话（核心正文里的行话不上屏）；认不出就照实显示核心原文
    node.appendChild(el("p", { text: hit ? hit.body : message.text }));
    if (hit) {
      node.appendChild(el("p", { class: "u-hint", text: "当前世界尚未因这次请求改变。" }));
      const row = el("div", { class: "u-row" });
      row.appendChild(primary(`查看${hit.label}`, () => this.ctx.navigate({ pane: hit.pane })));
      row.appendChild(button("继续联络", () => this.dismissHandoff(node)));
      row.appendChild(button("只把这句话告诉她", () => this.resendAsContact(message)));
      node.appendChild(row);
    } else if (this.isHandoffNotice(message.text)) {
      // 是转交类、但没对上任何一条固定短语（核心改过文案）：也绝不能出现
      // 「有通知、一个按钮都没有」的空白卡（评审 P1）。退一步说清这条要用户
      // 自己去对应的工作区处理，并至少留一个可点的出口。
      node.appendChild(
        el("p", {
          class: "u-hint",
          text: "这条通知属于别的处理环节（世界与素材 / 辅助写作 / 跑团），要你到那个工作区去处理；它没有改变当前世界。",
        }),
      );
      const row = el("div", { class: "u-row" });
      row.appendChild(primary("继续联络", () => this.dismissHandoff(node)));
      node.appendChild(row);
    }
    // 其余说明（世界还在追赶、离开期间的进展等）只是通报一句，本来就没有可点的动作：保持无按钮
    const meta = el("div", { class: "u-bubble-meta" });
    meta.appendChild(el("span", { text: stamp(message.at) }));
    node.appendChild(meta);
    return node;
  }

  private dismissHandoff(node: HTMLElement): void {
    node.remove();
  }

  /**
   * §6.3 的固定出口：把转交说明对应的原文展开回输入框供修改，并说清「只把这句话告诉她，
   * 不执行其中的操作」。确认后以新的联络请求提交（带 `as_contact`），原转交记录保留在历史与对话里。
   */
  private resendAsContact(notice: Message): void {
    const original = this.handoffOriginal(notice);
    if (!original) {
      // 找不到原文不猜、不崩：提示手动重发，仍走同一条受控入口
      this.setStatus("没找到这条通知对应的原文：请手动重发这句话（只把话告诉她，不执行其中的操作）", "pending");
      return;
    }
    this.restoreToComposer(original);
    this.asContact = true;
    setNote(this.handoffHint, "只把这句话告诉她，不执行其中的操作；可以改。", "pending");
    this.setStatus("只把这句话告诉她（不执行其中的操作），当作一条新的联络发出去", "pending");
  }

  /** 转交通知对应的原文：先按 `reply_to === env_id` 精确对，再退到该通知之前最近一条已作废的入站。 */
  private handoffOriginal(notice: Message): { text: string } | null {
    const ref = notice.replyTo;
    if (ref) {
      const fromHistory = this.messages.find((item) => item.role === "user" && item.ref === ref);
      if (fromHistory) return fromHistory;
      const fromPending = this.pending.find((item) => item.ref === ref);
      if (fromPending) return fromPending;
    }
    const index = this.messages.indexOf(notice);
    const before = index >= 0 ? this.messages.slice(0, index) : this.messages;
    for (let cursor = before.length - 1; cursor >= 0; cursor -= 1) {
      const item = before[cursor];
      if (item.role === "user" && item.state === "cancelled") return item;
    }
    // 实时流里作废的入站还没进历史：它在 pending 里等这一轮的结局，取最近一条
    const live = this.pending[this.pending.length - 1];
    return live ? { text: live.text } : null;
  }

  private resetAsContact(): void {
    this.asContact = false;
    setNote(this.handoffHint, "", "muted");
  }

  private appendSystem(node: HTMLElement): void {
    this.systemHost?.appendChild(node);
    if (this.systemHost && this.systemHost.childElementCount > 6) {
      this.systemHost.firstElementChild?.remove();
    }
    // 卡片现在长在对话里：在底部就跟着滚过去，别让它在屏幕外继续冒出来
    if (this.atBottom && this.scrollHost) this.scrollHost.scrollTop = this.scrollHost.scrollHeight;
  }

  /** 换对象 / 重连前清掉「上一条连接」的渲染账：节点缓存与未定稿预览都不跨对象 */
  private resetTranscript(): void {
    // 节点也要从 DOM 摘掉：只清缓存不摘节点，重连后 loadHistory 会按空缓存再插一遍，
    // 同一条消息就在列表里出现两份（原来的整体重建不会踩到这一点）
    for (const node of this.msgNodes.values()) node.remove();
    for (const node of this.pendingNodes.values()) node.remove();
    this.msgNodes.clear();
    this.msgOrder = [];
    this.pendingNodes.clear();
    this.previews.clear();
    // 预览节点在滚动区里：重连时也要摘下来，不然第二条连接会再插一个同样的气泡
    this.previewNode?.remove();
    this.previewNode = null;
    this.previewText = null;
    this.rendered = 0;
  }

  private setStatus(text: string, kind: "ok" | "bad" | "pending" | "muted"): void {
    setNote(this.statusNote, text, kind);
  }

  private updateGate(): void {
    const connected = Boolean(this.selection && this.link?.connected);
    const gate = this.aiGate();
    if (this.composer) this.composer.disabled = !connected;
    // 发送闸第三个维度：AI 未配置时按钮不可点（审计 Q1 2.5#4）。只认 readiness.ai.configured，
    // 已配置后的网络 / 生成失败不在这里拦——那些错误照旧在发送后由错误卡就近说明。
    // 「读数失败」不算未配置：那会把能用的用户挡在门外（评审 P1「读取失败≠空态」）。
    if (this.sendButton) this.sendButton.disabled = !connected || gate === "missing";
    if (this.aiGateHint) this.aiGateHint.hidden = !(connected && gate === "missing");
    // 连接闸：连不上时输入框旁边就地给「重新连接」。正在重连 / 首次连接途中不显示
    //（状态行已经在说「正在重连…」），避免同时出现两句互相打架的话。
    if (this.linkGateHint) {
      const idleOrFailed = this.linkState === "idle" || this.linkState === "failed";
      this.linkGateHint.hidden = !(this.selection && !connected && idleOrFailed);
    }
  }

  /**
   * AI 服务这条闸的三种结论：已配置 / 确实没配置 / 读数失败。
   * 分开的理由（评审 P1「读取失败≠空态」）：读数失败时不能断言「没有配置」——
   * 这时不拦发送，让核心在发送后照旧给出 llm_not_configured 那种就近说明。
   */
  private aiGate(): "ready" | "missing" | "unreadable" {
    if (this.ctx.readinessError) return "unreadable";
    const ai = (this.ctx.readiness.ai as Json | undefined) ?? {};
    return ai.configured ? "ready" : "missing";
  }

  /* ---------------------------------------------------------------- 操作 */

  private async switchCharacter(cardId: string): Promise<void> {
    if (!this.selection || cardId === this.selection.character_id) return;
    const character = this.characters.find((item) => String(item.card_id) === cardId);
    await this.ctx.drafts.flush(this.draftKey);
    this.selection = {
      ...this.selection,
      character_id: cardId,
      character_name: String(character?.name ?? ""),
    };
    await this.ctx.setPrefs({ "sel.contact": this.selection });
    this.rememberContact();
    this.messages = [];
    this.pending = [];
    fill(this.systemHost as HTMLElement);
    await this.connect();
  }

  private async pickTarget(): Promise<void> {
    const box = el("div", { class: "u-dialog-body" });
    const instances = this.ctx.instances();
    const instanceSelect = el("select", { class: "u-input" }) as HTMLSelectElement;
    for (const item of instances) instanceSelect.appendChild(el("option", { value: item.id, text: item.name }));
    if (this.selection) instanceSelect.value = this.selection.instance_id;
    const timelineSelect = el("select", { class: "u-input" }) as HTMLSelectElement;
    const characterSelect = el("select", { class: "u-input" }) as HTMLSelectElement;
    const loadInto = async (): Promise<void> => {
      const info = await this.ctx.api.instanceInfo(instanceSelect.value);
      fill(timelineSelect);
      for (const item of (info.timelines as Json[]) ?? []) {
        timelineSelect.appendChild(el("option", { value: String(item.id), text: `${String(item.name)}${String(item.state) === "archived" ? "（已归档）" : ""}` }));
      }
      fill(characterSelect);
      for (const item of (info.characters as Json[]) ?? []) {
        characterSelect.appendChild(el("option", { value: String(item.card_id), text: String(item.name) }));
      }
    };
    instanceSelect.addEventListener("change", () => void loadInto());
    await loadInto();
    box.appendChild(el("label", { class: "u-field" }, el("span", { class: "u-field-label", text: "世界" }), instanceSelect));
    box.appendChild(el("label", { class: "u-field" }, el("span", { class: "u-field-label", text: "世界线" }), timelineSelect));
    box.appendChild(el("label", { class: "u-field" }, el("span", { class: "u-field-label", text: "角色" }), characterSelect));
    const modal = dialog("换一个联络对象", [box], [
      {
        label: "切换",
        primary: true,
        run: () => {
          void (async () => {
            const info = await this.ctx.api.instanceInfo(instanceSelect.value);
            const timeline = ((info.timelines as Json[]) ?? []).find((item) => String(item.id) === timelineSelect.value);
            const character = ((info.characters as Json[]) ?? []).find(
              (item) => String(item.card_id) === characterSelect.value,
            );
            this.characters = (info.characters as Json[]) ?? [];
            this.timelines = (info.timelines as Json[]) ?? [];
            this.worldName = instances.find((item) => item.id === instanceSelect.value)?.name ?? "";
            this.selection = {
              instance_id: instanceSelect.value,
              timeline_id: timelineSelect.value,
              timeline_name: String(timeline?.name ?? ""),
              character_id: characterSelect.value,
              character_name: String(character?.name ?? ""),
            };
            await this.ctx.setPrefs({ "sel.contact": this.selection });
            this.rememberContact();
            this.messages = [];
            // 换对象必须把上一条的等待一起清掉：留着它，回复一到就会把「那个角色的话」
            // 落进这个角色的对话里（与 switchCharacter 同一处理）
            this.pending = [];
            await this.connect();
          })();
        },
      },
      { label: "取消", run: () => undefined },
    ]);
    document.body.appendChild(modal.node);
  }

  private async toggleRun(): Promise<void> {
    const selection = this.selection;
    if (!selection) return;
    try {
      const clock = await this.ctx.api.clock(selection.instance_id, selection.timeline_id);
      const state = String((clock.clock as Json)?.state ?? "");
      if (state === "active") {
        await this.ctx.api.freeze(selection.instance_id, selection.timeline_id);
        this.setStatus("这条世界线已暂停（暂停不等于退出程序）", "ok");
      } else {
        await this.ctx.api.activate(selection.instance_id, selection.timeline_id);
        this.setStatus("这条世界线已开始运行", "ok");
      }
      await this.refreshClock();
    } catch (error) {
      this.setStatus(uiError(error, { module: "角色联络", action: "暂停 / 启动世界线" }).message, "bad");
    }
  }

  /* ---------------------------------------------------------------- 转述 */

  private async startDisclosure(preselect = ""): Promise<void> {
    const selection = this.selection;
    if (!selection) return;
    const others = this.characters.filter((item) => String(item.card_id) !== selection.character_id);
    if (!others.length) {
      this.setStatus("这个线里只有一位角色：先在世界与素材里加入另一位", "pending");
      return;
    }
    const body = el("div", {});
    const receiver = el("select", { class: "u-input" }) as HTMLSelectElement;
    for (const item of others) receiver.appendChild(el("option", { value: String(item.card_id), text: String(item.name) }));
    const list = el("ol", { class: "u-list" });
    const picked = new Set<string>(preselect ? [preselect] : []);
    const reload = async (): Promise<void> => {
      fill(list, el("li", { class: "u-hint", text: "正在读取可以转述的内容…" }));
      try {
        const result = await this.ctx.api.discloseSuggest(
          selection.instance_id,
          selection.timeline_id,
          selection.character_id,
          receiver.value,
        );
        const candidates = ((result.candidates as Json[]) ?? []) as Json[];
        fill(list);
        if (!candidates.length) {
          list.appendChild(el("li", { class: "u-hint", text: "还没有可以转述的片段（她讲过的话都已授权过）。" }));
          return;
        }
        for (const item of candidates) {
          const ref = String(item.ref ?? "");
          const checkbox = el("input", { type: "checkbox", value: ref }) as HTMLInputElement;
          checkbox.checked = picked.has(ref);
          checkbox.addEventListener("change", () => (checkbox.checked ? picked.add(ref) : picked.delete(ref)));
          const line = el("li", {}, el("label", { class: "u-check" }, checkbox, el("span", { text: String(item.text ?? "") })));
          list.appendChild(line);
        }
      } catch (error) {
        fill(list, el("li", { class: "u-note u-note-bad", text: uiError(error, { module: "转述", action: "读取候选" }).message }));
      }
    };
    receiver.addEventListener("change", () => void reload());
    body.appendChild(
      paragraph("转述按整条消息授权：下面这些是她讲过、你已经在对话里看过的整条内容。授权不能单独撤回，恢复版本也不保证删除对方已经看到的消息。"),
    );
    body.appendChild(el("label", { class: "u-field" }, el("span", { class: "u-field-label", text: "转述给" }), receiver));
    body.appendChild(list);
    const modal = dialog("转述给她认识的人", [body], [
      {
        label: "确认授权",
        primary: true,
        run: () => {
          void (async () => {
            if (!picked.size) {
              this.setStatus("还没有选中要转述的内容", "pending");
              return;
            }
            try {
              const result = await this.ctx.api.discloseConfirm(
                selection.instance_id,
                selection.timeline_id,
                selection.character_id,
                receiver.value,
                [...picked],
              );
              const reused = Boolean((result as Json).reused);
              this.setStatus(reused ? "这个范围之前已经授权过：结果与原来一致" : "已授权这些整条消息", "ok");
              this.renderMessages();
            } catch (error) {
              this.appendSystem(errorCard(uiError(error, { module: "转述", action: "确认授权" })));
            }
          })();
        },
      },
      { label: "取消", run: () => undefined },
    ]);
    document.body.appendChild(modal.node);
    await reload();
  }

  unmount(): void {
    this.stopWaitClock(); // 离开页面必须停掉读秒，别留一个每秒跑一次的定时器
    void this.ctx.drafts.flush(this.draftKey);
    this.link?.close();
    this.link = null;
  }
}

function stateText(state: string): string {
  if (state === "submitting") return "正在提交";
  if (state === "unknown") return "结果待确认";
  if (state === "accepted" || state === "queued" || state === "processing") return "已接收，正在等待回应";
  if (state === "done" || state === "fixed") return "已回应";
  if (state === "failed") return "这一轮没有生成成功（可以重新获取回复）";
  // 转交（创作请求 / 版本操作 / 跑团行动）会让入站以 cancelled 收场：她没有回答这一轮。
  // 同一种状态也用于回滚 / 重绑作废，所以说法要同时容得下两种原因
  if (state === "cancelled") return "这一轮没有在这里回答（可能已转交到别的工作区，或这条已经作废）";
  return "状态待确认"; // 认不出的内部状态不上屏
}

/**
 * 「查询这条的结果」的人话映射：只认核心的投递 / 处理状态，认不出就不显示原始码。
 * `answered` 取自核心记录里有没有对这条的回复，比状态码本身更接近用户关心的事。
 */
function deliveryText(state: string, answered: boolean): string {
  if (answered || state === "done" || state === "fixed") return "已经收到回复";
  if (state === "queued" || state === "processing" || state === "accepted") return "已提交，正在等她回复";
  if (state === "failed") return "这一轮没有生成成功（你发的话本身已经存下来了）";
  if (state === "cancelled") return "这一轮没有在这里回答（可能已转交到别的工作区，或这条已经作废）";
  return "暂时读不出最新状态，过一会儿再查一次";
}

function deliveryKind(state: string, answered: boolean): "ok" | "bad" | "pending" | "muted" {
  if (answered || state === "done" || state === "fixed") return "ok";
  if (state === "failed") return "bad";
  if (state === "queued" || state === "processing" || state === "accepted") return "pending";
  return "muted";
}
