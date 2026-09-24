/*
 * 角色联络工作区（USER_INTERFACE_DESIGN §6）。
 *
 * 只展示：对话、角色公开身份、当前世界与线名、已完成的世界时刻、必要运行状态。
 * 没有角色活动监控、性格分数、记忆库或世界全知事件表。
 *
 * 发送纪律（§6.2）：点击发送立即保留原文并标「正在提交」；收到受理回执才清空输入并移入历史；
 * 受理前失败原文仍可编辑；受理结果未知先查结果，不重复当新消息发出。
 */

import type { AppContext, Pane } from "./app";
import type { ChannelEvent, Json } from "./api";
import { ChannelLink, uiError, type UiError } from "./api";
import {
  button,
  chip,
  dialog,
  el,
  errorCard,
  fill,
  link,
  paragraph,
  primary,
  section,
  setNote,
  stamp,
} from "./dom";
import { paneTitle } from "./app";

interface Message {
  role: "user" | "character" | "notice";
  text: string;
  seq: number;
  at: number;
  messageId: string;
  state: string;
  ref: string;
  parts: number[];
}

interface ContactSelection {
  instance_id: string;
  timeline_id: string;
  timeline_name: string;
  character_id: string;
  character_name: string;
}

export class ContactPane implements Pane {
  readonly id = "contact" as const;
  private link: ChannelLink | null = null;
  /** 连接失败的反馈卡（每次重试更新它，不新增） */
  private connectError: HTMLElement | null = null;
  private selection: ContactSelection | null = null;
  private messages: Message[] = [];
  private pending: Array<{ ref: string; text: string; state: string; error?: UiError }> = [];
  private thinking = false;
  private characters: Json[] = [];
  private timelines: Json[] = [];
  private worldName = "";
  private worldLabel = "";
  private stateChip: HTMLElement | null = null;
  private statusNote: HTMLElement | null = null;
  private listHost: HTMLElement | null = null;
  private composer: HTMLTextAreaElement | null = null;
  private sendButton: HTMLButtonElement | null = null;
  private draftSlot: HTMLElement | null = null;
  private draftKey = "";
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
    const heading = el("h2", { class: "u-h2", text: paneTitle("contact") });
    const header = el("div", { class: "u-contact-head" });
    const scroll = el("div", { class: "u-messages", tabindex: "0" });
    scroll.addEventListener("scroll", () => {
      this.atBottom = scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 40;
      if (this.atBottom) this.hideNewHint();
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
    const form = el("form", { class: "u-composer" }, textarea, el("div", { class: "u-col-actions" }, send), hint);
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
    fill(
      center,
      heading,
      header,
      el("div", { class: "u-row" }, more),
      scroll,
      newHint,
      live,
      this.systemHost,
      composerNote,
      this.draftSlot,
      form,
    );
    this.composer = textarea;
    this.sendButton = send;
    this.scrollHost = scroll;
    this.headerHost = header;

    await this.resolveSelection();
  }

  private scrollHost: HTMLElement | null = null;
  private headerHost: HTMLElement | null = null;
  private newHint: HTMLButtonElement | null = null;
  private liveHost: HTMLElement | null = null;
  private rendered = 0;

  private hideNewHint(): void {
    if (this.newHint) this.newHint.hidden = true;
  }

  /* ---------------------------------------------------------------- 选择与连接 */

  private async resolveSelection(): Promise<void> {
    const stored = (this.ctx.prefs["sel.contact"] as ContactSelection | undefined) ?? undefined;
    const instances = this.ctx.instances();
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
    await this.connect();
  }

  private async connect(): Promise<void> {
    const selection = this.selection;
    if (!selection) return;
    this.draftKey = `contact:${selection.instance_id}:${selection.timeline_id}:${selection.character_id}`;
    this.renderHeader();
    this.renderList();
    this.renderCluePanel();
    try {
      this.link?.close();
      this.link = new ChannelLink(this.ctx.endpoint);
      this.link.onEvent((event) => this.onEvent(event));
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
        button("时间线与版本", () => this.ctx.navigate({ pane: "worlds", sub: "timeline" })),
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
      const world = Number(clock.processed_world ?? 0);
      this.stateChip.className = `u-chip u-chip-${state === "active" ? "ok" : state === "frozen" ? "pending" : "muted"}`;
      this.stateChip.textContent = state === "active" ? "运行中" : state === "frozen" ? "已暂停" : state;
      const label = Number(clock.target_world ?? 0) > world ? `（正在补齐到 ${Number(clock.target_world ?? 0)}）` : "";
      this.worldLabel = `已完成的世界时刻 ${world}${label}`;
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
    fill(
      this.listHost,
      section("角色", list),
      section(
        "这个世界",
        el(
          "div",
          { class: "u-row" },
          button("打开世界", () => this.ctx.navigate({ pane: "worlds", sub: "detail" })),
          button("暂停 / 启动", () => void this.toggleRun()),
        ),
      ),
    );
  }

  private renderCluePanel(): void {
    if (!this.clueHost) return;
    const clues = el("ol", { class: "u-list", id: "u-clues" });
    fill(
      this.clueHost,
      section(
        "已讲过的线索",
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
    if (!selection || !host) return;
    try {
      const result = await this.ctx.api.narrativeMap(selection.instance_id, selection.timeline_id, selection.character_id);
      const map = (result.map as Json) ?? {};
      const nodes = (map.nodes as Json[]) ?? [];
      const spoken = nodes.filter((item) => String(item.stage ?? "") === "spoken");
      const held = nodes.filter((item) => String(item.stage ?? "") !== "spoken");
      fill(
        host,
        ...spoken.map((item) =>
          el(
            "li",
            {},
            button(String(item.text ?? "（这条内容已经不在当前记录里）"), () => void this.focusMessage(String(item.message_id ?? "")), {
              class: "u-btn u-ghost",
            }),
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

  private async loadHistory(): Promise<void> {
    const selection = this.selection;
    if (!selection) return;
    const session = (await this.ctx.api.sessionEnsure(selection.instance_id, selection.timeline_id, selection.character_id))
      .session as Json;
    const page = await this.ctx.api.history(String(session.id), undefined, 50);
    const rows = (page.messages as Json[]) ?? [];
    this.messages = rows.map((row) => this.fromRow(row));
    this.hasMore = Boolean(page.has_more);
    this.oldestSeq = Number(page.next_before_seq ?? 0);
    this.renderMessages(true);
  }

  private async loadOlder(keepAnchor = false): Promise<void> {
    const selection = this.selection;
    if (!selection || !this.hasMore) return;
    const session = (await this.ctx.api.sessionEnsure(selection.instance_id, selection.timeline_id, selection.character_id))
      .session as Json;
    const page = await this.ctx.api.history(String(session.id), this.oldestSeq, 50);
    const rows = (page.messages as Json[]) ?? [];
    this.messages = [...rows.map((row) => this.fromRow(row)), ...this.messages];
    this.hasMore = Boolean(page.has_more);
    this.oldestSeq = Number(page.next_before_seq ?? 0);
    this.renderMessages(!keepAnchor);
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
      parts: [],
    };
  }

  private renderMessages(force = false): void {
    if (!this.scrollHost) return;
    const wasAtBottom = this.atBottom;
    const keep = this.scrollHost.scrollHeight - this.scrollHost.scrollTop;
    fill(
      this.scrollHost,
      ...this.messages.map((message) => this.renderMessage(message)),
      ...this.pending.map((item) => this.renderPending(item)),
    );
    const total = this.messages.length + this.pending.length;
    if (force || wasAtBottom) this.scrollHost.scrollTop = this.scrollHost.scrollHeight;
    else {
      this.scrollHost.scrollTop = this.scrollHost.scrollHeight - keep;
      // 用户正在看旧记录：新内容不强制拉到底，给一个可点的入口（§6.1）
      if (total > this.rendered && this.newHint) this.newHint.hidden = false;
    }
    this.rendered = total;
    const more = this.host?.querySelector("#u-contact-more") as HTMLButtonElement | null;
    if (more) more.hidden = !this.hasMore;
    this.updateGate();
  }

  private renderMessage(message: Message): HTMLElement {
    const cls = message.role === "character" ? "u-bubble u-bubble-them" : "u-bubble u-bubble-me";
    const node = el("div", { class: cls, "data-message": message.messageId });
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

  private renderPending(item: { ref: string; text: string; state: string; error?: UiError }): HTMLElement {
    const node = el("div", { class: "u-bubble u-bubble-me u-bubble-pending" });
    node.appendChild(el("p", { class: "u-bubble-text", text: item.text }));
    const meta = el("div", { class: "u-bubble-meta" });
    meta.appendChild(el("span", { text: stateText(item.state) }));
    node.appendChild(meta);
    if (item.error) {
      node.appendChild(
        errorCard(item.error, [
          { label: "保留原文继续编辑", run: () => this.restoreToComposer(item) },
          { label: "查询原结果", run: () => void this.queryResult(item.ref) },
        ]),
      );
    }
    return node;
  }

  private restoreToComposer(item: { text: string }): void {
    if (!this.composer) return;
    this.composer.value = item.text;
    this.composer.focus();
    this.queueDraft();
  }

  private async queryResult(ref: string): Promise<void> {
    try {
      const result = await this.ctx.api.storyTurn(
        this.selection?.instance_id ?? "",
        this.selection?.timeline_id ?? "",
        this.selection?.character_id ?? "",
      );
      const state = String(result.product_state ?? "");
      this.setStatus(`请求 ${ref.slice(-6)} 的状态：${state}`, state === "expressed" ? "ok" : "pending");
    } catch (error) {
      this.setStatus(uiError(error, { module: "角色联络", action: "查询原结果" }).message, "bad");
    }
  }

  /* ---------------------------------------------------------------- 发送 */

  private async send(): Promise<void> {
    const selection = this.selection;
    const text = this.composer?.value.trim() ?? "";
    if (!selection || !text || !this.link) return;
    if (this.thinking) {
      this.setStatus("还在等上一条的回应；可以先写下一条，发送节拍由会话层排队", "pending");
    }
    let ref = "";
    try {
      ref = this.link.send(text);
    } catch (error) {
      this.setStatus(uiError(error, { module: "角色联络", action: "发送" }).message, "bad");
      return;
    }
    this.pending.push({ ref, text, state: "submitting" });
    if (this.composer) this.composer.value = "";
    await this.ctx.drafts.discard(this.draftKey);
    setNote(this.draftSlot, "", "muted");
    this.setStatus("正在提交…", "pending");
    this.renderMessages();
    // 受理前失败原文仍可编辑：短暂等待后仍无回执就标记「结果待确认」
    window.setTimeout(() => {
      const item = this.pending.find((entry) => entry.ref === ref);
      if (item && item.state === "submitting") {
        item.state = "unknown";
        this.setStatus("这条还没收到受理回执：可以查询原结果，或继续编辑原文重发", "pending");
        this.renderMessages();
      }
    }, 8000);
  }

  private onEvent(event: ChannelEvent): void {
    if (event.kind === "reply") {
      const existing = this.messages.find((item) => item.messageId === event.messageId);
      if (!existing) {
        this.messages.push({
          role: "character",
          text: event.parts.join(""),
          seq: 0,
          at: event.at,
          messageId: event.messageId,
          state: "fixed",
          ref: "",
          parts: [event.batchIndex],
        });
      } else if (!existing.parts.includes(event.batchIndex)) {
        existing.parts.push(event.batchIndex);
        existing.text += event.parts.join("");
      }
      const inbound = this.pending.find((item) => item.ref === event.replyTo);
      if (inbound) {
        inbound.state = "done";
        this.pending = this.pending.filter((item) => item.ref !== event.replyTo);
      }
      this.thinking = false;
      this.link?.confirmDelivery(event.messageId, event.batchIndex, "accepted");
      this.renderMessages();
      if (this.liveHost) this.liveHost.textContent = "收到一条新回复"; // 读屏播报；不朗读全文、不逐秒播报
      this.setStatus("回复已保存", "ok");
      return;
    }
    if (event.kind === "notice") {
      this.messages.push({
        role: "notice",
        text: event.text,
        seq: 0,
        at: event.at,
        messageId: event.messageId,
        state: "fixed",
        ref: "",
        parts: [],
      });
      // 说明也要回执（单批）：不回执的话这条永远算未确认，每次重连都会被当成待投递重发一遍
      this.link?.confirmDelivery(event.messageId, 0, "accepted");
      this.renderMessages();
      this.appendSystem(this.handoffCard(event.text));
      return;
    }
    if (event.kind === "accepted") {
      const item = this.pending.find((entry) => entry.ref === event.ref);
      if (item) {
        item.state = event.state === "failed" || event.state === "cancelled" ? "failed" : "accepted";
        if (item.state === "accepted") this.setStatus("已接收，正在等待回应", "pending");
        if (item.state === "failed") {
          item.error = uiError(new Error("这次没有生成成功"), {
            module: "角色联络",
            action: "生成回复",
            done: "你的消息已经被核心保存",
            unknown: "她这一轮是否有回复",
          });
        }
        this.renderMessages();
      }
      return;
    }
    if (event.kind === "status") {
      this.thinking = event.state === "thinking";
      if (this.thinking) this.setStatus("已接收，正在等待回应", "pending");
      return;
    }
    if (event.kind === "binding") {
      if (event.state === "revoked") {
        this.setStatus("这个绑定被换掉了：界面会重新读取历史", "pending");
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
      this.renderMessages();
    }
  }

  private handoffCard(text: string): HTMLElement {
    const targets: Array<{ key: string; label: string; pane: "worlds" | "writing" | "contact" }> = [
      { key: "creation", label: "尝试世界变化", pane: "worlds" },
      { key: "version", label: "版本与恢复", pane: "worlds" },
      { key: "trpg", label: "跑团行动", pane: "contact" },
    ];
    const hit = targets.find((item) => text.includes(item.label));
    const node = el("div", { class: "u-handoff" });
    node.appendChild(el("p", { text: text }));
    node.appendChild(el("p", { class: "u-hint", text: "当前世界尚未因这次请求改变。" }));
    const row = el("div", { class: "u-row" });
    if (hit) row.appendChild(primary(`查看${hit.label}`, () => this.ctx.navigate({ pane: hit.pane })));
    row.appendChild(button("继续联络", () => this.dismissHandoff(node)));
    row.appendChild(button("仅作为联络发送", () => this.dismissHandoff(node)));
    node.appendChild(row);
    return node;
  }

  private dismissHandoff(node: HTMLElement): void {
    node.remove();
  }

  private appendSystem(node: HTMLElement): void {
    this.systemHost?.appendChild(node);
    if (this.systemHost && this.systemHost.childElementCount > 6) {
      this.systemHost.firstElementChild?.remove();
    }
  }

  private setStatus(text: string, kind: "ok" | "bad" | "pending" | "muted"): void {
    setNote(this.statusNote, text, kind);
  }

  private updateGate(): void {
    const selection = this.selection;
    const disabled = !selection || !this.link;
    if (this.composer) this.composer.disabled = disabled;
    if (this.sendButton) this.sendButton.disabled = disabled;
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
    box.appendChild(el("label", { class: "u-field" }, el("span", { class: "u-field-label", text: "时间线" }), timelineSelect));
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
            this.messages = [];
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
      paragraph("转述按**完整消息**授权：下面这些是她讲过、你已经在对话里看过的整条内容。授权不能单独撤回，恢复版本也不保证删除对方已经看到的消息。"),
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
  if (state === "failed") return "没有成功（可以重试原请求）";
  return state;
}
