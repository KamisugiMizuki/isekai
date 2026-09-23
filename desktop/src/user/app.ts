/*
 * 正式界面外壳（`docs/user-interface/USER_INTERFACE_DESIGN.md` §3）：
 * 导航、顶栏、全局条幅、工作区路由、界面草稿保护、退出前保存。
 *
 * 一个桌面程序、一个主窗口、三个任务工作区；Core Debugging 从「帮助与诊断 → 高级调试」进入。
 * 这一层只管壳面：世界事实、会话与提交全在核心。
 */

import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { MgmtClient } from "../ump";
import { AppApi, uiError, type InstanceEntry, type Json } from "./api";
import { button, chip, clear, el, errorCard, facts, fill, paragraph, primary, setNote, stamp } from "./dom";
import { ContactPane } from "./contact";
import { HelpPane } from "./help";
import { HomePane } from "./home";
import { CreatePane } from "./create";
import { OnboardingPane } from "./onboarding";
import { SettingsPane } from "./settings";
import { WorldsPane } from "./worlds";
import { TrpgPane } from "./trpg";
import { WritingPane } from "./writing";
import { Launcher } from "./launcher";

export type PaneId = "home" | "contact" | "writing" | "trpg" | "worlds" | "settings" | "help" | "onboarding" | "create";

export interface Route {
  pane: PaneId;
  sub?: string;
}

export interface AppContext {
  api: AppApi;
  endpoint: string;
  /** 本机检查的最近一次读数（首屏与帮助页共用） */
  readiness: Json;
  /** 生效配置（含打码后的 Key） */
  settings: Json;
  prefs: Record<string, unknown>;
  setPrefs(patch: Record<string, unknown>): Promise<void>;
  route: Route;
  navigate(target: Route): void;
  banner(text: string, kind?: "ok" | "bad" | "pending" | "muted"): void;
  refresh(): Promise<void>;
  instances(): InstanceEntry[];
  openDebug(): void;
  drafts: DraftKeeper;
}

export interface Pane {
  id: PaneId;
  mount(host: HTMLElement): void | Promise<void>;
  unmount?(): void;
}

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

const PREF_KEYS = {
  onboard: "onboard.done",
  contact: "sel.contact",
  writing: "sel.writing",
  trpg: "sel.trpg",
  worlds: "sel.worlds",
  recent: "recent",
  textSize: "appearance.text_size",
  theme: "appearance.theme",
} as const;

/** 界面草稿的保存纪律（§3.5）：停止输入 1 秒保存；连续输入时最多 5 秒一次；切页/退出立即保存 */
export class DraftKeeper {
  private timers = new Map<string, number>();
  private lastSaved = new Map<string, number>();
  private payloads = new Map<string, { module: string; target: string; text: string; payload?: Json }>();
  private slots = new Map<string, HTMLElement>();
  private inFlight = new Set<string>();

  constructor(private readonly api: () => AppApi | null) {}

  watch(key: string, slot: HTMLElement | null, module: string, target: string, text: string, payload?: Json): void {
    this.payloads.set(key, { module, target, text, payload });
    if (slot) this.slots.set(key, slot);
    const now = Date.now();
    const last = this.lastSaved.get(key) ?? 0;
    const delay = now - last >= 5000 ? 0 : 1000;
    const existing = this.timers.get(key);
    if (existing) window.clearTimeout(existing);
    this.setNote(key, text ? "未保存…" : "已保存", "muted");
    const timer = window.setTimeout(() => void this.flush(key), Math.max(delay, 400));
    this.timers.set(key, timer);
  }

  private setNote(key: string, text: string, kind: "ok" | "bad" | "muted"): void {
    const slot = this.slots.get(key);
    if (!slot) return;
    setNote(slot, text, kind as "ok" | "bad" | "muted");
  }

  async flush(key?: string): Promise<void> {
    const keys = key ? [key] : [...this.payloads.keys()];
    for (const item of keys) {
      const payload = this.payloads.get(item);
      if (!payload) continue;
      const api = this.api();
      if (!api) {
        this.setNote(item, "保存失败：核心未连接（内容仍在窗口里，可复制）", "bad");
        continue;
      }
      const timer = this.timers.get(item);
      if (timer) {
        window.clearTimeout(timer);
        this.timers.delete(item);
      }
      this.inFlight.add(item);
      this.setNote(item, "正在保存…", "muted");
      try {
        await api.draftSave(item, payload.module, payload.target, payload.text, payload.payload);
        this.lastSaved.set(item, Date.now());
        this.setNote(item, `已保存 ${stamp(Date.now() / 1000)}`, "ok");
      } catch (error) {
        const info = uiError(error, { module: payload.module, action: "保存草稿", target: payload.target });
        this.setNote(item, `${info.message}（内容仍在窗口里，可复制）`, "bad");
      } finally {
        this.inFlight.delete(item);
      }
    }
  }

  /** 丢掉一份草稿（成功提交后调用） */
  async discard(key: string): Promise<void> {
    this.payloads.delete(key);
    const timer = this.timers.get(key);
    if (timer) window.clearTimeout(timer);
    const api = this.api();
    if (api) {
      try {
        await api.draftDiscard(key);
      } catch {
        /* 丢不掉不挡主流程：草稿会在首页「未完成内容」里继续显示 */
      }
    }
  }

  async load(key: string): Promise<{ text: string; payload: Json | null } | null> {
    const api = this.api();
    if (!api) return null;
    try {
      const result = await api.draftLoad(key);
      const draft = result.draft as Json;
      return { text: String(draft.text ?? ""), payload: (draft.payload as Json) ?? null };
    } catch {
      return null;
    }
  }
}

export class App {
  private mgmt: MgmtClient | null = null;
  private status: CoreStatus = { state: "starting" };
  private apiRef: AppApi | null = null;
  private current: Pane | null = null;
  private route: Route = { pane: "home" };
  private prefs: Record<string, unknown> = {};
  private readiness: Json = {};
  private settings: Json = {};
  private instanceCache: InstanceEntry[] = [];
  private exitHandshake = false;
  private navHost!: HTMLElement;
  private topHost!: HTMLElement;
  private mainHost!: HTMLElement;
  private bannerHost!: HTMLElement;
  private toastHost!: HTMLElement;
  readonly drafts = new DraftKeeper(() => this.apiRef);

  constructor(private readonly root: HTMLElement) {}

  /* ---------------------------------------------------------------- 启动 */

  /** 无人值守验收用：当前路由 / 连接 / 偏好（页面本来就能读这些，不新增权限） */
  get probeState(): { route: Route; connected: boolean; prefs: Record<string, unknown>; navLog: string[] } {
    return { route: this.route, connected: Boolean(this.apiRef), prefs: this.prefs, navLog: this.navLog };
  }

  /** 导航轨迹（最近 50 条）：卡住时看得出是谁把哪一页盖到哪一页 */
  private navLog: string[] = [];

  /** 探针与调试入口用的连接读取（只读） */
  get api(): AppApi | null {
    return this.apiRef;
  }

  async boot(): Promise<void> {
    (window as unknown as { __uiApp?: App }) .__uiApp = this;
    openDirNotify = (text, kind) => this.toast(text, kind);
    this.build();
    this.prefs = await this.loadPrefs();
    this.applyAppearance();
    await this.startExitHandshake();
    void listen<CoreStatus>("core-status", (event) => {
      this.status = event.payload;
      void this.onCoreStatus();
    });
    void listen<string>("notice-open", (event) => this.onNotice(event.payload));
    window.setInterval(() => {
      void invoke<string | null>("take_pending_notice")
        .then((pending) => (pending ? this.onNotice(pending) : undefined))
        .catch(() => undefined);
    }, 1500);
    this.status = await this.waitForCore();
    await this.onCoreStatus();
    
    // ponytail: 原型启动选择器，只在首次（还没完成首次设置）或没选过应用时显示
    if (!this.prefs.app_mode || !this.prefs[PREF_KEYS.onboard]) {
      const launcher = new Launcher(this.context());
      launcher.show(true);   // 启动时的自动弹窗：刚选过就不打扰
    }
  }

  private build(): void {
    clear(this.root);
    this.root.className = "u-app";
    // ponytail: 方案 B 删除左侧导航栏和 brand,顶部添加返回启动器按钮
    this.navHost = el("div", { hidden: true }); // 保留引用避免其他代码崩溃

    this.topHost = el("header", { class: "u-topbar u-topbar-focused" });
    this.bannerHost = el("div", { class: "u-banner hidden", role: "status", "aria-live": "polite" });
    this.mainHost = el("main", { class: "u-main u-main-focused", id: "u-main" });
    this.toastHost = el("div", { class: "u-toasts", role: "status", "aria-live": "polite" });
    const content = el("section", { class: "u-content u-content-focused" }, this.topHost, this.bannerHost, this.mainHost);
    this.root.appendChild(el("div", { class: "u-frame u-frame-focused" }, content, this.toastHost));
  }

  /* ---------------------------------------------------------------- 顶栏 / 条幅 */

  /** 上一级页面：应用三页（联络 / 写作 / 跑团）与首页之上是启动选择器，其余页面挂在当前应用下。 */
  private parentTarget(): Route | null {
    if (APP_ROOT_PANES.has(this.route.pane)) return null;
    return { pane: appPane(String(this.prefs.app_mode ?? "chat")) };
  }

  private renderTop(): void {
    const title = this.current ? paneTitle(this.route.pane) : "";
    const mode = String(this.prefs.app_mode ?? "chat");
    const atAppRoot = Boolean(this.current) && this.route.pane === appPane(mode);
    const parent = this.current ? this.parentTarget() : null;

    // 返回按钮写清去处（原来只有一个应用图标，点下去回哪儿看不出来）
    const backLabel = parent ? `← 返回${paneTitle(parent.pane)}` : "← 返回选择应用";
    const backBtn = button(backLabel, () => {
      if (parent) this.navigate(parent);
      else new Launcher(this.context()).show();
    }, { class: "u-btn u-btn-back", id: "u-back", title: backLabel });
    backBtn.setAttribute("aria-label", backLabel);

    const moreBtn = button("⋯", () => this.showMoreMenu(), { class: "u-btn u-ghost", id: "u-more-btn", title: "更多选项" });
    moreBtn.setAttribute("aria-label", "更多选项");

    fill(
      this.topHost,
      el("div", { class: "u-title" }, 
        backBtn,
        el("strong", { text: atAppRoot ? `${appIcon(mode)} ${title}` : title }),
      ),
      el(
        "div",
        { class: "u-row" },
        chip(coreStatusText(this.status), this.status.state === "ready" ? "ok" : this.status.state === "starting" ? "pending" : "bad"),
        moreBtn,
      ),
    );
  }
  
  private showMoreMenu(): void {
    const menu = el("div", { class: "u-more-menu", role: "menu" });
    const backdrop = el("div", { class: "u-more-backdrop" });
    const items = [
      { label: "首页", action: () => this.navigate({ pane: "home" }) },
      { label: "设置", action: () => this.navigate({ pane: "settings" }) },
      { label: "帮助与诊断", action: () => this.navigate({ pane: "help" }) },
      { label: "世界管理", action: () => this.navigate({ pane: "worlds" }) },
      { label: "返回选择应用", action: () => new Launcher(this.context()).show() },
    ];
    
    // 点条目先关菜单再执行动作（原来只处理「点空白关闭」，浮层会压在新页面上）
    for (const item of items) {
      const btn = button(item.label, () => {
        backdrop.remove();
        item.action();
      }, { class: "u-more-menu-item" });
      btn.setAttribute("role", "menuitem");
      menu.appendChild(btn);
    }
    
    backdrop.addEventListener("click", () => backdrop.remove());
    backdrop.appendChild(menu);
    document.body.appendChild(backdrop);
    
    // 焦点管理
    (menu.firstElementChild as HTMLElement)?.focus();
    backdrop.addEventListener("keydown", (e) => {
      if (e.key === "Escape") backdrop.remove();
    });
  }

  banner(text: string, kind: "ok" | "bad" | "pending" | "muted" = "muted"): void {
    if (!text) {
      this.bannerHost.className = "u-banner hidden";
      this.bannerHost.textContent = "";
      return;
    }
    this.bannerHost.className = `u-banner u-banner-${kind}`;
    this.bannerHost.textContent = text;
  }

  toast(text: string, kind: "ok" | "bad" | "muted" = "muted"): void {
    const node = el("div", { class: `u-toast u-toast-${kind}`, text });
    this.toastHost.appendChild(node);
    window.setTimeout(() => node.remove(), 6000);
  }

  /* ---------------------------------------------------------------- 连接 */

  private async waitForCore(): Promise<CoreStatus> {
    for (let attempt = 0; attempt < 200; attempt += 1) {
      try {
        const status = await invoke<CoreStatus>("core_status");
        this.status = status;
        if (status.state !== "starting") return status;
      } catch {
        /* 非 Tauri 环境：直接走默认状态 */
        return this.status;
      }
      await new Promise((resolve) => setTimeout(resolve, 150));
    }
    return this.status;
  }

  private async onCoreStatus(): Promise<void> {
    const ready = this.status.state === "ready" || this.status.state === "compatibility_blocked";
    if (ready && !this.apiRef) {
      await this.connect();
    } else if (!ready && this.apiRef) {
      // 核心掉了：连接作废，界面退回启动页（已显示内容标为缓存，不报保存成功）
      this.apiRef = null;
      this.mgmt?.close();
      this.mgmt = null;
    }
    this.renderTop();
    if (!this.apiRef) {
      this.renderStartup();
      if (this.current) {
        this.current.unmount?.();
        this.current = null;
      }
    } else if (!this.current) {
      // 走同一条导航链：与用户点击串行，不并发挂载同一块画布
      this.navigate(this.route);
    }
  }

  private async connect(): Promise<void> {
    if (!this.status.endpoint || !this.status.mgmt) return;
    this.mgmt = new MgmtClient(this.status.endpoint, this.status.mgmt);
    await this.mgmt.connect();
    this.apiRef = new AppApi(this.mgmt);
    await this.refresh();
  }

  async refresh(): Promise<void> {
    if (!this.apiRef) return;
    const api = this.apiRef;
    try {
      const [readiness, settings, instances] = await Promise.all([
        api.readiness(),
        api.settings(),
        api.instances(),
      ]);
      this.readiness = readiness;
      this.settings = settings;
      this.instanceCache = ((instances.instances as InstanceEntry[]) ?? []).slice();
    } catch (error) {
      this.banner(uiError(error, { module: "本机", action: "读取状态" }).message, "bad");
    }
    this.renderTop();
  }

  /** 只重读世界列表（切页时用；读不到就保留已有缓存，页面自己会给出错误卡） */
  private async refreshInstances(): Promise<void> {
    if (!this.apiRef) return;
    try {
      const instances = await this.apiRef.instances();
      this.instanceCache = ((instances.instances as InstanceEntry[]) ?? []).slice();
    } catch {
      /* 保留已有缓存 */
    }
  }

  /* ---------------------------------------------------------------- 路由 */

  /** 导航串行化：并发点两个入口时不能交错（交错会留下「路由是新的、内容还是旧的」） */
  private navChain: Promise<void> = Promise.resolve();

  navigate(target: Route): void {
    this.navChain = this.navChain
      .then(() => this.open(target))
      .catch((error) => {
        // 页面自己起不来也要说清楚：留一张错误卡 + 重试入口，不留空白页
        const info = uiError(error, { module: "界面", action: "打开这个页面" });
        clear(this.mainHost);
        this.mainHost.appendChild(errorCard(info, [{ label: "重试打开", run: () => this.navigate(target) }]));
        this.toast(info.message, "bad");
      });
  }

  private async open(target: Route): Promise<void> {
    if (this.current && target.pane === this.route.pane && !target.sub) return;
    const token = ++this.navToken;
    this.navLog.push(`${new Date().toISOString().slice(11, 19)} → ${target.pane}${target.sub ? "/" + target.sub : ""}`);
    if (this.navLog.length > 50) this.navLog.shift();
    await this.drafts.flush(); // 切页触发一次立即保存（§3.5）
    if (token !== this.navToken) return; // 已经有更新的导航接管这次切换
    // 世界列表不靠启动时那一份缓存：本程序之外也可能改过数据（重开一页就重新读一次）
    await this.refreshInstances();
    if (token !== this.navToken) return;
    this.current?.unmount?.();
    this.current = null;
    this.route = target;
    clear(this.mainHost);
    if (!this.apiRef) {
      this.renderStartup();
      return;
    }
    const ctx = this.context();
    let pane: Pane;
    switch (target.pane) {
      case "onboarding":
        // 入口带着要看的那一步来（`sub:"sample"` = 直接用样例开始）；丢掉它就得从「本机检查」重走
        pane = new OnboardingPane(ctx, target.sub);
        break;
      case "create":
        pane = new CreatePane(ctx);
        break;
      case "contact":
        pane = new ContactPane(ctx);
        break;
      case "writing":
        pane = new WritingPane(ctx);
        break;
      case "worlds":
        pane = new WorldsPane(ctx);
        break;
      case "settings":
        pane = new SettingsPane(ctx);
        break;
      case "help":
        pane = new HelpPane(ctx);
        break;
      case "trpg":
        pane = new TrpgPane(ctx);
        break;
      default:
        pane = new HomePane(ctx);
    }
    // 每次导航给这一页一个自己的容器：晚到的挂载只会画进已卸下的节点，盖不到当前页面
    const container = el("section", { class: "u-pane" });
    this.mainHost.appendChild(container);
    this.current = pane;
    this.renderNav();
    await pane.mount(container);
    if (token !== this.navToken) {
      pane.unmount?.();
      return;
    }
    this.renderTop();
  }

  private navToken = 0;

  private renderNav(): void {
    for (const node of this.navHost.querySelectorAll("button")) {
      const id = node.dataset.pane;
      node.classList.toggle("u-nav-active", id === this.route.pane);
      node.setAttribute("aria-current", id === this.route.pane ? "page" : "false");
    }
  }

  private context(): AppContext {
    return {
      api: this.apiRef as AppApi,
      endpoint: this.status.endpoint ?? "",
      readiness: this.readiness,
      settings: this.settings,
      prefs: this.prefs,
      setPrefs: (patch) => this.setPrefs(patch),
      route: this.route,
      navigate: (target) => this.navigate(target),
      banner: (text, kind) => this.banner(text, kind),
      refresh: () => this.refresh(),
      instances: () => this.instanceCache,
      openDebug: () => this.openDebug(),
      drafts: this.drafts,
    };
  }

  /* ---------------------------------------------------------------- 偏好 */

  private async loadPrefs(): Promise<Record<string, unknown>> {
    try {
      return await invoke<Record<string, unknown>>("shell_settings");
    } catch {
      return {};
    }
  }

  private async setPrefs(patch: Record<string, unknown>): Promise<void> {
    // 就地合并：各面板在挂载时拿到的 ctx.prefs 是同一个对象，换新对象会让它们读到旧值
    Object.assign(this.prefs, patch);
    this.applyAppearance();
    for (const [key, value] of Object.entries(patch)) {
      try {
        await invoke("shell_setting_set", { key, value });
      } catch (error) {
        this.toast(`偏好保存失败：${String(error)}`, "bad");
      }
    }
  }

  private applyAppearance(): void {
    const theme = String(this.prefs[PREF_KEYS.theme] ?? "system");
    const size = String(this.prefs[PREF_KEYS.textSize] ?? "100");
    document.documentElement.dataset.theme = theme;
    document.documentElement.dataset.textSize = size;
  }

  /* ---------------------------------------------------------------- 启动页 / 调试入口 */

  private renderStartup(): void {
    const state = this.status.state;
    const bad = state !== "starting";
    const box = el("div", { class: "u-page" });
    box.appendChild(el("h2", { text: bad ? "后台服务没有就绪" : "正在启动后台服务…" }));
    box.appendChild(
      paragraph(
        bad
          ? `核心状态：${state}${this.status.error ? `（${this.status.error}）` : ""}`
          : "第一次启动会检查本机环境；这一步不需要你操作。",
        "u-p",
      ),
    );
    const row = el("div", { class: "u-row" });
    if (bad) {
      row.appendChild(
        primary("重启后台服务", () => {
          void invoke("core_restart").then(() => window.setTimeout(() => void this.onCoreStatus(), 800));
        }),
      );
      row.appendChild(button("打开日志目录", () => void openDir("logs")));
      row.appendChild(button("复制诊断信息", () => void this.copyDiagnostics()));
    }
    box.appendChild(row);
    box.appendChild(
      facts([
        ["核心状态", state],
        ["原因", this.status.error ?? "—"],
        ["程序版本", String(this.status.app ?? "—")],
        ["数据格式", String(this.status.data_format ?? "—")],
        ["规则版本", String(this.status.rules ?? "—")],
      ]),
    );
    fill(this.mainHost, box);
  }

  async copyDiagnostics(): Promise<void> {
    // 允许清单（§8）：版本、路径类别、错误码与阶段；不含密钥、正文与提示词
    const lines = [
      `isekai 诊断 ${stamp(Date.now() / 1000)}`,
      `核心状态：${this.status.state} / ${this.status.error ?? "-"}`,
      `程序版本：${String(this.status.app ?? "-")}｜数据格式：${String(this.status.data_format ?? "-")}｜规则：${String(this.status.rules ?? "-")}`,
      `数据目录类别：本机应用数据目录`,
      `本机检查：${(this.readiness.checks as Json[] | undefined)
        ?.map((item) => `${item.key}=${item.ok ? "ok" : "bad"}`)
        .join(" ") ?? "未检查"}`,
      `AI 服务：${String((this.readiness.ai as Json | undefined)?.base_url ?? "-")}｜模型：${String(
        (this.readiness.ai as Json | undefined)?.model ?? "-",
      )}｜密钥：${(this.readiness.ai as Json | undefined)?.api_key_set ? "已设置" : "未设置"}`,
      "（未包含密钥、聊天正文、记忆与世界内部数据）",
    ];
    try {
      await navigator.clipboard.writeText(lines.join("\n"));
      this.toast("诊断信息已复制（可先预览再粘贴）", "ok");
    } catch (error) {
      this.toast(`复制失败：${String(error)}`, "bad");
    }
  }

  openDebug(): void {
    void this.drafts.flush().then(() => {
      // 让出连接：调试壳要自己连 UMP（同一条通道不能挂两条连接），管理连接继续共用
      this.current?.unmount?.();
      this.current = null;
      this.banner("已进入高级调试：普通工作区暂停使用连接；返回请点「回到正式界面」", "pending");
      void import("../main").then(({ initDebugShell }) => {
        document.body.dataset.mode = "debug";
        void initDebugShell({ mgmt: this.mgmt, onExit: () => this.leaveDebug() });
      });
    });
  }

  private leaveDebug(): void {
    document.body.dataset.mode = "user";
    this.banner("", "muted");
    void this.onCoreStatus();
  }

  /* ---------------------------------------------------------------- 提醒 */

  private async onNotice(noticeId: string): Promise<void> {
    if (!this.apiRef) return;
    try {
      const result = await this.apiRef.notices();
      const list = (result.notices as Json[]) ?? [];
      const hit = list.find((item) => String(item.id ?? "") === noticeId) ?? list[list.length - 1];
      if (!hit) return;
      this.toast(`提醒：${String(hit.title ?? hit.id ?? "新的提醒")}`, "ok");
      this.navigate({ pane: "contact" });
    } catch {
      /* 提醒定位失败不影响主流程 */
    }
  }

  /* ---------------------------------------------------------------- 退出握手 */

  private async startExitHandshake(): Promise<void> {
    window.setInterval(() => {
      if (this.exitHandshake) return;
      void invoke<boolean>("exit_pending")
        .then((pending) => (pending ? this.flushAndExit() : undefined))
        .catch(() => undefined);
    }, 600);
  }

  private async flushAndExit(): Promise<void> {
    if (this.exitHandshake) return;
    this.exitHandshake = true;
    this.banner("正在保存并退出…", "pending");
    await this.drafts.flush();
    if (!this.apiRef) {
      await invoke("exit_ready", { detail: "管理面未连接：按已有持久化水位退出", saved: false });
      return;
    }
    try {
      const result = await this.apiRef.call("app.shutdown", {}, 10000);
      const saved = result.saved as { ok?: boolean; bytes?: number } | undefined;
      await invoke("exit_ready", {
        detail: saved?.ok ? `退出前备份完成（一致水位）` : "退出前备份未通过完整性校验",
        saved: Boolean(saved?.ok),
      });
    } catch (error) {
      await invoke("exit_ready", { detail: `退出前保存失败：${String(error)}`, saved: false });
    }
  }
}

/** 应用根页（它们的上一级是启动选择器）；首页是通用落点，同样归在根这一层。 */
const APP_ROOT_PANES: ReadonlySet<PaneId> = new Set<PaneId>(["home", "contact", "writing", "trpg"]);

/** 启动选择器的三个模式 → 对应的应用根页（与 launcher.launch 的路由一致） */
export function appPane(mode: string): PaneId {
  if (mode === "writer") return "writing";
  if (mode === "gm") return "trpg";
  return "contact";
}

export function appIcon(mode: string): string {
  if (mode === "writer") return "✍️";
  if (mode === "gm") return "🎲";
  return "💬";
}

/** 页面标题（顶栏与页内标题共用）。方案 B 删左侧导航后不能再从导航项取，用一张表。 */
const PANE_TITLES: Partial<Record<PaneId, string>> = {
  home: "首页",
  contact: "角色联络",
  writing: "辅助写作",
  trpg: "跑团",
  worlds: "世界与素材",
  settings: "设置",
  help: "帮助与诊断",
  onboarding: "首次设置",
  create: "创建世界",
};

export function paneTitle(id: PaneId): string {
  return PANE_TITLES[id] ?? "首页";
}

function coreStatusText(status: CoreStatus): string {
  if (status.state === "ready") return "后台服务运行中";
  if (status.state === "starting") return "后台服务启动中…";
  if (status.state === "compatibility_blocked") return "版本兼容性阻断";
  if (status.state === "persistence_blocked") return "存储不可写";
  return `后台服务未就绪（${status.state}）`;
}

/** 目录打开失败的反馈通道：App 启动时接上（就近反馈，不用原生弹窗） */
let openDirNotify: ((text: string, kind: "ok" | "bad" | "muted") => void) | null = null;

export async function openDir(kind: "logs" | "data" | "backups" | "packages", api?: AppApi | null): Promise<void> {
  try {
    if (kind === "logs") {
      const path = await invoke<string>("log_dir");
      await invoke("open_dir", { path });
      return;
    }
    const client = api ?? null;
    const readiness = client ? await client.readiness() : null;
    const paths = ((readiness?.paths as Json | undefined) ?? {}) as Json;
    const path = String(paths[kind === "data" ? "data" : kind === "backups" ? "backups" : "packages"] ?? "");
    if (!path) {
      fail("当前没有拿到这个位置（核心没有连接）");
      return;
    }
    await invoke("open_dir", { path });
  } catch (error) {
    fail(`打开目录失败：${String(error)}`);
  }
}

function fail(text: string): void {
  if (openDirNotify) openDirNotify(text, "bad");
  else window.alert(text);
}

export function errorBox(error: unknown, module: string, action: string): HTMLElement {
  const info = uiError(error, { module, action });
  return errorCard(info, [{ label: "重试", run: () => window.location.reload() }]);
}
