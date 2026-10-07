/*
 * 辅助写作工作区（USER_INTERFACE_DESIGN §7.1–§7.5）。
 *
 * 一起组织大纲、观察人物、比较推进方案，文字由你决定。
 * 四个分区在同一工作区内切换：大纲 / 当前素材 / 推进建议 / 文字草稿。
 * 三种结果三个动作：以此起草（留作文字）/ 另开分支试演 / 预览世界变化并确认应用到当前线。
 *
 * 两条界面层的硬规矩（§7.3 / §7.5）：
 *   - 建议只是建议：只有真实提交过的结果才显示「已进世界」，采用与提交是两步；
 *   - 正文锁定后，新生成永远另起一稿，世界恢复也不会改写它。
 * 跑团（U4）在 `trpg.ts` 里单独实现，这里只负责写作。
 */

import { invoke } from "@tauri-apps/api/core";
import { MgmtError } from "../ump";
import type { AppContext, Pane } from "./app";
import type { Json, UiError } from "./api";
import { uiError } from "./api";
import {
  bulletList,
  button,
  chip,
  dialog,
  el,
  errorCard,
  facts,
  field,
  fill,
  pageHead,
  panel,
  paragraph,
  primary,
  section,
  setNote,
  stamp,
  tools,
  type Child,
} from "./dom";
import { dotLine, meter, stackBar, type Seg } from "./graphics";

type Tab = "outline" | "material" | "advice" | "draft";

/**
 * 写作工作区自己的上下文（§3.3）：世界 / 世界线 / 大纲分别记住，形状照抄 `sel.contact`。
 * 只读自己这一个键，不跨工作区继承别的域的写入目标。
 */
interface WritingSelection {
  instance_id: string;
  timeline_id: string;
  timeline_name: string;
  outline_id: string;
  outline_name: string;
}

/**
 * 一次取数的结果（P1-16）：按 `(instance, timeline, outline)` 缓存。
 *
 * 为什么缓存：分区切换（大纲 / 素材 / 建议 / 草稿）只换 `tab` 再 `render()`，
 * 每次重取 `instance.info` + `wa.outline.list` + `wa.state`×2（两次参数完全相同）+ 候选；
 * 同一份读数在同一对象上不会自己变，只有「换对象」或「内容变更动作」才需要重取。
 */
interface WritingWorkspace {
  /** 世界维度的读数：只要实例没换就还能用 */
  info: Json | null;
  outlines: Json[];
  infoError: UiError | null;
  /** 对象维度的读数：换实例 / 时间线 / 大纲都必须重取 */
  state: Json | null;
  candidates: Json[];
  stateError: UiError | null;
  candidatesError: UiError | null;
}

/** 六类条目：界面说法 + 一句示例（§7.2） */
const LAYERS: Array<[string, string, string]> = [
  ["theme", "主题约束", "例：主题围绕记住与遗忘"],
  ["required_node", "必须做到", "例：这一章结束前，主角要知道那份告警"],
  ["forbidden", "禁止事项", "例：北堤不得再次崩塌（触发就是没有按大纲走）"],
  ["character_arc", "角色变化", "例：主角从不信人，变成愿意托付"],
  ["pacing", "节奏目标", "例：前三章都在北堤附近"],
  ["variable_material", "可变素材", "例：可以用盐价、碑文、旧账本"],
];

const STATUS_TEXT: Record<string, string> = {
  unstarted: "未开始",
  in_progress: "进行中",
  achieved: "已达成",
  deviated: "没有按大纲走",
  abandoned: "已放弃",
};

/** 条目状态的图例顺序与色调（与下面 rows 里的 chip 同一套说法） */
const STATUS_SEGS: Array<[string, Seg["tone"]]> = [
  ["unstarted", "muted"],
  ["in_progress", "pending"],
  ["achieved", "ok"],
  ["deviated", "bad"],
  ["abandoned", "muted"],
];

/** 一组条目的状态分布 → 一段比例条（六类各自一条，一眼看出哪类没有按大纲走） */
function statusBar(items: Json[], legend = false): HTMLElement | null {
  const segs: Seg[] = STATUS_SEGS.map(([status, tone]) => ({
    label: STATUS_TEXT[status] ?? status,
    tone,
    value: items.filter((item) => String(item.status) === status).length,
  }));
  return stackBar(segs, { legend });
}

function refs(value: string): string[] {
  return value.split(/[,，;；\s]+/).map((item) => item.trim()).filter(Boolean);
}

/** 追加一句回落说明（已有说明时接在后面，不互相覆盖） */
function hint(current: string, next: string): string {
  return current ? `${current} ${next}` : next;
}

export class WritingPane implements Pane {
  readonly id = "writing" as const;
  private host: HTMLElement | null = null;
  private note: HTMLElement | null = null;
  private tab: Tab = "outline";
  private instanceId = "";
  private timelineId = "";
  private cardId = "";
  private outlineId = "";
  private state: Json | null = null;
  private report: Json | null = null;
  private observed: Json | null = null;
  private observedAt = 0;
  private candidates: Json[] = [];
  private goal = "";
  private limit = 3;
  private draftId = "";
  private draftTitle = "";
  private draftBody = "";
  private draftKey = "";
  private autoTimer: number | null = null;
  /** 读大纲状态真失败时的错误（not_found 是真空态，不进这里）：渲染优先于「还没有绑定大纲」 */
  private stateError: UiError | null = null;
  /** 读建议真失败时的错误（not_found 是真无建议）：用到建议的地方要如实说读取失败 */
  private candidatesError: UiError | null = null;
  /** 上次选择读了但对象已不存在时，如实说明回落原因（不静默继承别的域） */
  private selectionHint = "";
  /** 上一次已写入「最近使用 / sel.writing」的 key：同一选择不重复写 */
  private lastRecallKey = "";
  /** 未配置 AI 这类可行动失败的错误卡（画在「推进建议」页首；重画时先清掉） */
  private aiActionError: HTMLElement | null = null;
  /** 本次会话里新建的试演线：核心清单是进入页面时读的，新线要立刻出现在本页选择里（P1-5） */
  private extraTimelines: Array<{ id: string; name: string; state: string }> = [];
  /** 正在编辑的正文是否还没成为草稿（「以此起草」直接打开的情形）：列表与「锁定正文」都据此说话 */
  private draftFromSuggestion = false;
  /** 按 `(instance, timeline, outline)` 缓存的取数结果（P1-16）：同一对象不重复取 */
  private workspace: WritingWorkspace | null = null;
  private workspaceKey = "";
  /** 客户端已读到的世界进度点（P1-16）：随「给我推进建议」一起给内核，省掉一次整轮观察 */
  private observedRevision = 0;

  constructor(private readonly ctx: AppContext) {}

  async mount(host: HTMLElement): Promise<void> {
    this.host = host;
    this.note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    fill(host, this.note);
    await this.adoptStoredSelection();
    await this.render();
  }

  unmount(): void {
    if (this.autoTimer !== null) window.clearTimeout(this.autoTimer);
    void this.flushDraft();
  }

  /* ------------------------------------------------------------ 骨架 */

  private async render(): Promise<void> {
    const host = this.host;
    if (!host || !this.note) return;
    fill(host, this.note);
    const instances = this.ctx.instances();
    if (!instances.length) {
      // 连世界都没有时也给标题带：这一页的「我在哪」不该因为空态而消失（同一次审查：标题纵向位置三种）
      host.appendChild(pageHead("辅助写作", "把大纲、素材、建议和正文放在一条线上"));
      const emptyWorld = panel(
        "还没有世界",
        paragraph("辅助写作挂在一个世界上：先创建，或从样例开始，再回来写。"),
        el(
          "div",
          { class: "u-row" },
          primary("从样例开始", () => this.ctx.navigate({ pane: "onboarding", sub: "sample" })),
          button("创建世界", () => this.ctx.navigate({ pane: "create" })),
        ),
      );
      emptyWorld.classList.add("u-fill");
      host.appendChild(emptyWorld);
      return;
    }
    if (!this.instanceId) this.instanceId = instances[0].id;
    try {
      let data = await this.loadWorkspace();
      const info = data.info;
      if (!info) throw data.infoError ?? new Error("读不到这个世界");
      let timelines = (info.timelines as Json[]) ?? [];
      let characters = (info.characters as Json[]) ?? [];
      // 存储的选择已在 adoptStoredSelection 里校验过；这里的回落只服务「换世界 / 选中的线没了」这类界面内变化
      if (!timelines.some((item) => String(item.id) === this.timelineId)) {
        this.timelineId = String(timelines[0]?.id ?? "");
      }
      if (!characters.some((item) => String(item.card_id) === this.cardId)) {
        this.cardId = String(characters[0]?.card_id ?? "");
      }
      let outlineList = data.outlines;
      if (!outlineList.some((item) => String(item.outline_id ?? item.id) === this.outlineId)) {
        this.outlineId = String(outlineList[0]?.outline_id ?? outlineList[0]?.id ?? "");
      }
      // 上面两个回落可能改掉了时间线 / 大纲：对象变了就按新键取一次（键没变就直接命中缓存，不重复取）
      data = await this.loadWorkspace();
      if (data.info) {
        timelines = (data.info.timelines as Json[]) ?? [];
        characters = (data.info.characters as Json[]) ?? [];
      }
      outlineList = data.outlines;
      this.rememberWriting(instances, timelines, outlineList);
      this.renderHead(instances);
      // 回落说明紧贴标题带（在工具带之前）：它说的是「这一页在用哪个世界」，不是某个分区的状态
      if (this.selectionHint) host.appendChild(paragraph(this.selectionHint, "u-hint"));
      // 工具带排在选择器之前：内容再长它也贴在视口顶部；排在中间时 sticky 没有可吸附的行程，等于没锚定
      this.renderTabs(host);
      this.renderHeader(instances, timelines, characters, outlineList);
      if (!this.state && !this.stateError) {
        // 空态只说一次、只点一次：下拉里只有「（还没有大纲）」时，header 那份下拉与状态
        // 已经说清「没绑」，这里不再重复整句解释与那排动作（2026-10-08 审查：同一件事说了三遍）
        const emptyOutline = panel(
          "这条世界线还没有大纲",
          paragraph("下拉里选一份已建好的，或点右上角「新建大纲…」。", "u-hint"),
        );
        emptyOutline.classList.add("u-fill");
        host.appendChild(emptyOutline);
      } else if (this.stateError) {
        // 读取失败优先：不许拿「还没有绑定大纲」冒充一次没读到的状态
        host.appendChild(errorCard(this.stateError, [{ label: "重试", run: () => this.retryRead() }]));
      }
      if (this.tab === "outline") this.renderOutline(host);
      else if (this.tab === "material") await this.renderMaterial(host);
      else if (this.tab === "advice") this.renderAdvice(host);
      else this.renderDraft(host);
    } catch (error) {
      host.appendChild(errorCard(uiError(error, { module: "辅助写作", action: "打开工作区" })));
    }
  }

  /**
   * 挂载时读自己的 `sel.writing`（§3.3）：本域选择为空才读；世界 / 时间线 / 大纲都还在才采用，
   * 少了任何一层都回落到既有默认并在页面上说明，不静默改用别的域的选择。
   */
  private async adoptStoredSelection(): Promise<void> {
    if (this.instanceId) return;
    const instances = this.ctx.instances();
    if (!instances.length) return;
    const stored = (this.ctx.prefs["sel.writing"] as Partial<WritingSelection> | undefined) ?? undefined;
    const storedInstance = String(stored?.instance_id ?? "");
    const instance = instances.find((item) => item.id === storedInstance) ?? instances[0];
    if (storedInstance && instance.id !== storedInstance) {
      this.selectionHint = "上次写作的世界已经不在本机：已回到第一个世界，请重新选择。";
    }
    this.instanceId = instance.id;
    try {
      // P1-16：挂载与 render() 共用同一份缓存，不再各自把 instance.info / wa.outline.list 取一遍
      const info = await this.loadWorkspace();
      const timelines = (info.info?.timelines as Json[]) ?? [];
      const storedTimeline = String(stored?.timeline_id ?? "");
      if (storedTimeline && timelines.some((item) => String(item.id) === storedTimeline)) this.timelineId = storedTimeline;
      else if (storedTimeline) this.selectionHint = hint(this.selectionHint, "上次的世界线已经不在这个世界：已回落到现有的第一条。");
      const outlines = info.outlines;
      const storedOutline = String(stored?.outline_id ?? "");
      if (storedOutline && outlines.some((item) => String(item.outline_id ?? item.id) === storedOutline)) this.outlineId = storedOutline;
      else if (storedOutline) this.selectionHint = hint(this.selectionHint, "上次的大纲已经不在了：已回落到现有的第一份。");
      // 选择落定后按新对象补齐对象维度的读数（同一个 key 只会取一次，分区切换不再重复）
      await this.loadWorkspace();
    } catch {
      // 读不到就先按世界默认渲染：render() 会用错误卡说明这次没读到什么
    }
  }

  /** 选中（世界 / 时间线 / 大纲）落定后写 `sel.writing` 与一条「最近使用」（§3.3 / §3.4） */
  private rememberWriting(
    instances: Array<{ id: string; name: string }>,
    timelines: Json[],
    outlines: Json[],
  ): void {
    if (!this.instanceId) return;
    const key = `writing:${this.instanceId}:${this.timelineId}:${this.outlineId}`;
    if (key === this.lastRecallKey) return;
    this.lastRecallKey = key;
    const timeline = timelines.find((item) => String(item.id) === this.timelineId);
    const outline = outlines.find((item) => String(item.outline_id ?? item.id) === this.outlineId);
    const selection: WritingSelection = {
      instance_id: this.instanceId,
      timeline_id: this.timelineId,
      timeline_name: String(timeline?.name ?? ""),
      outline_id: this.outlineId,
      outline_name: String(outline?.name ?? ""),
    };
    void this.ctx.setPrefs({ "sel.writing": selection });
    const world = instances.find((item) => item.id === this.instanceId)?.name ?? "";
    const line = String(timeline?.name ?? "");
    this.ctx.rememberRecent({ pane: "writing", label: `写作 · ${world}${line ? ` / ${line}` : ""}`, key });
  }

  /* ------------------------------------------------ 取数与缓存（P1-16） */

  /** 缓存键：对象三元组。世界维度的读数只看实例，对象维度看三样 */
  private workspaceKeyFor(instanceId: string, timelineId: string, outlineId: string): string {
    return `${instanceId}\u0000${timelineId}\u0000${outlineId}`;
  }

  /** 下一次取数必须重新拉取（换对象、或刚做过内容变更动作之后） */
  private invalidateWorkspace(): void {
    this.workspace = null;
    this.workspaceKey = "";
  }

  /**
   * 取这一页要的读数：`instance.info` + `wa.outline.list`（世界维度）+ `wa.state` 一次得到
   * state 与 candidates（对象维度）。同一个键只取一次，分区切换、挂载与 render 都不重复取。
   *
   * 为什么 `wa.state` 只调一次：以前 `loadState()` 与 `refreshCandidates()` 各自调一次，
   * 参数完全相同——同一份读数的两次网络往返（P1-16）。
   */
  private async loadWorkspace(): Promise<WritingWorkspace> {
    const key = this.workspaceKeyFor(this.instanceId, this.timelineId, this.outlineId);
    if (this.workspace && this.workspaceKey === key) return this.workspace;
    const fresh: WritingWorkspace = {
      info: null,
      outlines: [],
      infoError: null,
      state: null,
      candidates: [],
      stateError: null,
      candidatesError: null,
    };
    try {
      fresh.info = await this.ctx.api.instanceInfo(this.instanceId);
      fresh.outlines = ((await this.ctx.api.waOutlines()).outlines as Json[]) ?? [];
    } catch (error) {
      fresh.infoError = uiError(error, { module: "辅助写作", action: "读取世界与大纲清单" });
    }
    if (this.instanceId && this.timelineId) {
      try {
        // 只调一次 wa.state：state 与 public.candidates 同源
        const result = await this.ctx.api.waState(this.instanceId, this.timelineId, this.outlineId || "");
        fresh.state = (result.state as Json) ?? null;
        fresh.candidates = ((result.public as Json)?.candidates as Json[]) ?? [];
      } catch (error) {
        // 核心用 not_found「这条时间线上还没有绑定大纲」表示真空态：只有它走空态。
        // 核心断开 / 内部错误必须出错误态，不许显示成「还没有绑定大纲」。
        if (!(error instanceof MgmtError && error.code === "not_found")) {
          fresh.stateError = uiError(error, { module: "辅助写作", action: "读取大纲状态" });
          fresh.candidatesError = uiError(error, { module: "辅助写作", action: "读取推进建议" });
        }
      }
    }
    this.workspace = fresh;
    this.workspaceKey = key;
    this.state = fresh.state;
    this.stateError = fresh.stateError;
    this.candidates = fresh.candidates;
    this.candidatesError = fresh.candidatesError;
    return fresh;
  }

  /** 内容变更动作之后的重新取数：显式失效再取（不做自动探测）。分区切换不走这里 */
  private async reloadWorkspace(): Promise<void> {
    this.invalidateWorkspace();
    await this.loadWorkspace();
  }

  /** 读取失败后的「重试」：显式失效再重取，不然重试还是命中那份坏读数 */
  private retryRead(): void {
    void (async () => {
      await this.reloadWorkspace();
      await this.render();
    })();
  }

  /**
   * 记下客户端刚读到的观察 revision（P1-16）：取世界时钟的已处理水位。
   *
   * 读数失败就不传（保持 0）——宁可让内核自己重跑一次观察，也不给一个编造的水位。
   */
  private async refreshObservedRevision(): Promise<void> {
    if (!this.instanceId || !this.timelineId) return;
    try {
      const view = await this.ctx.api.clock(this.instanceId, this.timelineId);
      const row = (view.clock as Json) ?? view;
      this.observedRevision = Math.max(0, Math.round(Number(row.processed_world ?? 0)));
    } catch {
      this.observedRevision = 0;
    }
  }

  /**
   * ① 标题带（每页固定第一条）：页面名 + 一句定位语 + 右侧主操作。
   * 两个动作在整页只出现这一次——以前盒子里一排、盒子下面又一排，用户不知道该按哪一个
   * （2026-10-08 视觉体系审查第 7 节）。
   */
  private renderHead(instances: Array<{ id: string; name: string }>): void {
    const host = this.host!;
    const world = instances.find((item) => item.id === this.instanceId)?.name ?? "";
    const actions: Child[] = [
      primary("新建大纲…", () => void this.newOutline()),
      // 按钮名固定成同一套：页内提示说的「绑定 / 更新绑定」必须与屏幕上的字逐字对上
      button("绑定 / 更新绑定", () => void this.bindOutline()),
    ];
    // 绑的是哪个世界是定位信息：留在标题带，换分区也不会跟丢
    if (world) actions.push(chip(`世界：${world}`, "muted"));
    host.appendChild(
      pageHead(
        "辅助写作",
        "把大纲、素材、建议和正文放在一条线上；在跑团里打开时这里是主持视图，结果默认不发给玩家。",
        actions,
      ),
    );
  }

  /** 绑定状态（谁绑着、章节、进度）与两条空态说明：③ 内容带的第一块 */
  private renderHeader(
    instances: Array<{ id: string; name: string }>,
    timelines: Json[],
    characters: Json[],
    outlines: Json[],
  ): void {
    const host = this.host!;
    // 本页新开的试演线立刻进选择：不并进来的话，用户回来在这页找不到刚建的那条线（P1-5）
    const known = new Set(timelines.map((item) => String(item.id)));
    const timelineOptions = [...timelines, ...this.extraTimelines.filter((item) => !known.has(item.id))];
    const picker = (
      label: string,
      id: string,
      options: Array<[string, string]>,
      current: string,
      onPick: (value: string) => void,
    ): HTMLElement => {
      const select = el("select", { class: "u-input", id }) as HTMLSelectElement;
      for (const [value, text] of options) select.appendChild(el("option", { value, text }));
      select.value = current;
      select.addEventListener("change", () => onPick(select.value));
      return field(label, select);
    };
    const state = this.state;
    host.appendChild(
      panel(
        // 分区标题给人话：内容少时这一块也不会只剩一个空盒子
        "这条线上在写什么",
        el(
          "div",
          { class: "u-row u-row-wrap" },
          picker("世界", "u-wa-instance", instances.map((item) => [item.id, item.name]), this.instanceId, (value) => {
            this.instanceId = value;
            this.cardId = "";
            this.timelineId = "";
            this.observed = null;
            void this.render();
          }),
          picker(
            "世界线",
            "u-wa-timeline",
            timelineOptions.map((item) => [
              String(item.id),
              `${String(item.name)}（${String(item.state) === "active" ? "运行中" : "暂停"}）`,
            ]),
            this.timelineId,
            (value) => {
              this.timelineId = value;
              void this.render();
            },
          ),
          picker("观察角色", "u-wa-card", characters.map((item) => [String(item.card_id), String(item.name)]), this.cardId, (value) => {
            this.cardId = value;
            this.observed = null;
            void this.render();
          }),
          picker(
            "大纲",
            "u-wa-outline",
            outlines.length
              ? outlines.map((item) => [String(item.outline_id ?? item.id), String(item.name ?? "")])
              : [["", "（还没有大纲）"]],
            this.outlineId,
            (value) => {
              this.outlineId = value;
              void this.render();
            },
          ),
        ),
        // 绑定状态紧跟选择器：选完就能看见「绑没绑上、绑的是哪一份」
        el(
          "div",
          { class: "u-row" },
          state
            ? chip(`已绑定：${String(state.outline_name ?? "")}`, "ok")
            : this.stateError
              ? chip("大纲状态读取失败", "bad")
              : chip("未绑定", "pending"),
          state
            ? paragraph(
                `章节：${String(state.chapter || "（未命名）")}｜世界已推进到第 ${Number(state.evaluated_world ?? 0)} 个进度点`,
                "u-hint",
              )
            : null,
        ),
        // 下拉为空时补一句空态：只有「（还没有…）」的选项，用户不知道该去哪儿建（P2）
        timelines.length
          ? null
          : paragraph("这个世界还没有世界线：到「世界与素材」新建一条，再回来绑定大纲。", "u-hint"),
        characters.length
          ? null
          : paragraph("这个世界还没有角色卡：到「世界与素材」加一个角色，再回来观察它的素材。", "u-hint"),
      ),
    );
  }

  /**
   * ② 工具带（分区切换）：用 `tools` 而不是自己拼 `.u-crumbs`。
   * 形状就是语义——分区是「下划线 + 当前项加重」，与动作按钮、页内锚点不再同形
   * （2026-10-08 审查根因 2：一套外观对应三种交互）。
   */
  private renderTabs(host: HTMLElement): void {
    const entries: Array<[Tab, string]> = [
      ["outline", "大纲"],
      ["material", "当前素材"],
      ["advice", "推进建议"],
      ["draft", "文字草稿"],
    ];
    host.appendChild(
      tools(
        entries.map(([id, label]) => ({
          label,
          current: id === this.tab,
          onSelect: () => {
            this.tab = id;
            void this.render();
          },
        })),
        "写作分区",
      ),
    );
  }

  /* ------------------------------------------------------------ ① 大纲（§7.2） */

  private renderOutline(host: HTMLElement): void {
    if (!this.state) {
      // 大纲页空态已经在上面说过一次（「这条世界线还没有大纲」），这里不再说第二遍：
      // 同一件事说两遍正是这次审查要消掉的东西（2026-10-08 审查第 7 节）。
      // 读取失败则由调用方出错误卡 + 重试，也不在这里补话。
      return;
    }
    const items = ((this.state.items as Json[]) ?? []).slice();
    const achieved = items.filter((item) => String(item.status) === "achieved").length;
    host.appendChild(
      panel(
        "进度概览",
        paragraph(
          `这条线上共 ${items.length} 条约束与目标。状态是记下来的事实，不是会自动涨的进度条：没有按大纲走的会被留着，不会被抹掉。`,
          "u-hint",
        ),
        // 一句话说不清「六类里哪一类没有按大纲走」：给一条比例条，图例带数值
        statusBar(items, true) ?? paragraph("这份大纲还没有条目。", "u-hint"),
        // 再给一个读数：一眼看出走了几分之几，不用去数条目
        items.length ? meter(achieved, items.length, "已达成的条目") : null,
      ),
    );
    const groups = el("div", { class: "u-cards" });
    for (const [layer, label, example] of LAYERS) {
      const mine = items.filter((item) => String(item.layer) === layer);
      const card = el("article", { class: "u-card" });
      card.appendChild(el("h3", { text: `${label}（${mine.length}）` }));
      const bar = statusBar(mine);
      if (bar) card.appendChild(bar);
      card.appendChild(paragraph(example, "u-hint"));
      for (const item of mine) {
        const status = String(item.status);
        const row = el("div", { class: "u-row-line" });
        row.appendChild(el("span", { class: "u-grow", text: String(item.title || item.statement || item.id) }));
        row.appendChild(
          chip(
            STATUS_TEXT[status] ?? status,
            status === "achieved" ? "ok" : status === "deviated" || status === "abandoned" ? "bad" : status === "in_progress" ? "pending" : "muted",
          ),
        );
        row.appendChild(button("决定…", () => void this.decideItem(item)));
        card.appendChild(row);
        if (item.scope === "world") card.appendChild(paragraph("范围：整个世界（所有世界线）", "u-hint"));
        if (item.reason) card.appendChild(paragraph(`理由：${String(item.reason)}`, "u-hint"));
        const evidence = (item.evidence_refs as string[]) ?? [];
        if (evidence.length) card.appendChild(paragraph(`依据：${evidence.join("、")}`, "u-hint"));
      }
      if (!mine.length) card.appendChild(paragraph("这一类还没有条目。", "u-hint"));
      groups.appendChild(card);
    }
    host.appendChild(groups);
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        primary("检查大纲", () => void this.evaluate()),
        button("加一条…", () => void this.addItem()),
      ),
    );
    const report = this.report;
    if (report) {
      const gaps = (report.gaps as Json[]) ?? [];
      const deviations = (report.deviations as Json[]) ?? [];
      const evidence = (report.evidence as Json[]) ?? [];
      host.appendChild(
        section(
          "检查结果",
          el(
            "div",
            { class: "u-row" },
            chip(`缺口 ${gaps.length}`, gaps.length ? "bad" : "ok"),
            chip(`没有按大纲走 ${deviations.length}`, deviations.length ? "bad" : "ok"),
            chip(`可用依据 ${evidence.length}`, evidence.length ? "ok" : "muted"),
          ),
          gaps.length ? bulletList(gaps.map((item) => `缺口：${String(item.detail ?? item)}`), "u-list") : paragraph("硬约束没有缺口。", "u-hint"),
          deviations.length
            ? bulletList(deviations.map((item) => `没有按大纲走：${String(item.detail ?? item)}｜需要：${String(item.need ?? "")}`), "u-list")
            : paragraph("没有这类问题。", "u-hint"),
          evidence.length
            ? paragraph(`世界里已经有 ${evidence.length} 条可用依据，标记达成时可以直接引用。`, "u-hint")
            : paragraph("世界里暂时没有能对上条目的依据：必要时先提出一条世界变化建议。", "u-hint"),
          paragraph("缺口 = 还缺什么；依据 = 世界对得上的东西；决定 = 你来定。三者分开看，不用一个进度条代替。", "u-hint"),
        ),
      );
    }
  }

  private async evaluate(): Promise<void> {
    if (!this.state) {
      setNote(this.note, "先绑定一份大纲", "bad");
      return;
    }
    setNote(this.note, "正在检查…", "pending");
    try {
      const result = await this.ctx.api.waEvaluate(this.instanceId, this.timelineId, this.outlineId);
      this.report = result;
      this.state = { ...(this.state ?? {}), items: result.items };
      setNote(
        this.note,
        `检查完成：${((result.gaps as Json[]) ?? []).length} 条缺口、${((result.deviations as Json[]) ?? []).length} 条没有按大纲走`,
        "ok",
      );
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "大纲", action: "检查" }).message, "bad");
    }
  }

  private async decideItem(item: Json): Promise<void> {
    const layer = String(item.layer);
    const status = String(item.status);
    const choices: Array<[string, string]> =
      status === "unstarted"
        ? [["in_progress", "开始推进"], ["deviated", "直接记为没有按大纲走"], ["abandoned", "放弃"]]
        : status === "in_progress"
          ? [["achieved", "标记达成"], ["deviated", "接受这次没有按大纲走"], ["abandoned", "放弃"]]
          : status === "deviated"
            ? [["in_progress", "回到推进"], ["achieved", "标记达成"], ["abandoned", "放弃"]]
            : [["deviated", "接受这次没有按大纲走"]];
    if (layer === "forbidden") {
      choices.length = 0;
      choices.push(["deviated", "已触发（记为没有按大纲走）"], ["abandoned", "放弃这条禁止事项"]);
    }
    const target = el("select", { class: "u-input", id: "u-wa-target" }) as HTMLSelectElement;
    for (const [value, text] of choices) target.appendChild(el("option", { value, text }));
    const reason = el("textarea", {
      class: "u-textarea", rows: "2", id: "u-wa-reason", placeholder: "为什么这么定（必填）",
    }) as HTMLTextAreaElement;
    const evidence = el("input", {
      class: "u-input", id: "u-wa-evidence", placeholder: "世界观里的依据引用（逗号分隔）",
    }) as HTMLInputElement;
    const evidenceField = field("依据", evidence);
    const sync = (): void => {
      const needed = target.value === "achieved";
      evidenceField.style.display = needed ? "" : "none";
    };
    target.addEventListener("change", sync);
    sync();
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const modal = dialog(
      `决定「${String(item.title || item.statement || item.id)}」`,
      [
        facts([
          ["条目", String(item.title || item.statement || item.id)],
          ["当前状态", STATUS_TEXT[status] ?? status],
        ]),
        field("改成", target),
        evidenceField,
        field("理由", reason),
        paragraph("严格目标要带当前线可追溯的依据才能标记达成；自然语言判据不代替世界证据。开始的推进不需要依据。", "u-hint"),
        note,
      ],
      [
        {
          label: "记下这个决定",
          // 失败返回 false：`dialog` 因此不关窗，校验与错误提示留在用户正看着的这张窗里（P0-1）
          run: async () => {
            if (!reason.value.trim()) {
              setNote(note, "决定必须写理由：没有按大纲走可以被接受，不能被静默掩盖", "bad");
              return false;
            }
            try {
              const result = await this.ctx.api.waItemDecide({
                instance_id: this.instanceId,
                timeline_id: this.timelineId,
                outline_id: this.outlineId,
                item_id: String(item.id),
                status: target.value,
                reason: reason.value.trim(),
                evidence_refs: refs(evidence.value),
              });
              this.state = (result.state as Json) ?? this.state;
              setNote(this.note, `已记下决定：${String(item.title || item.id)} → ${STATUS_TEXT[target.value] ?? target.value}`, "ok");
              await this.render();
              return true;
            } catch (error) {
              setNote(note, uiError(error, { module: "大纲", action: "记下决定" }).message, "bad");
              return false;
            }
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  private async addItem(): Promise<void> {
    if (!this.outlineId) {
      setNote(this.note, "先新建或选一份大纲", "bad");
      return;
    }
    const layer = el("select", { class: "u-input", id: "u-wa-item-layer" }) as HTMLSelectElement;
    for (const [id, label] of LAYERS) layer.appendChild(el("option", { value: id, text: label }));
    const title = el("input", { class: "u-input", id: "u-wa-item-title", placeholder: "短标题" }) as HTMLInputElement;
    const statement = el("textarea", {
      class: "u-textarea", rows: "2", id: "u-wa-item-statement", placeholder: "希望发生 / 保持 / 避免什么",
    }) as HTMLTextAreaElement;
    const criteria = el("input", { class: "u-input", id: "u-wa-item-criteria", placeholder: "怎样算满足" }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const modal = dialog(
      "加一条大纲条目",
      [
        field("属于哪一类", layer),
        field("标题", title),
        field("希望发生 / 保持 / 避免什么", statement),
        field("怎样算满足", criteria),
        paragraph("先写这两句就够：范围、强度、前置与截止时刻保存后可以再补。", "u-hint"),
        note,
      ],
      [
        {
          label: "加进大纲",
          // 失败返回 false：条目没存进去时弹窗要留着，重试不用重新填一遍（P0-1）
          run: async () => {
            if (!statement.value.trim()) {
              setNote(note, "至少要写清楚这一条要求什么", "bad");
              return false;
            }
            try {
              // 改的是这条线真正绑着的那份大纲：下拉里选中的可能是另一份，别把条目加错地方
              const editId = String((this.state?.outline_id as string | undefined) ?? "") || this.outlineId;
              const current = await this.ctx.api.waOutlineGet(editId);
              const outline = ((current.outline as Json) ?? {}) as Json;
              const items = [...((outline.items as Json[]) ?? [])];
              items.push({
                id: `it-${Date.now().toString(36)}`,
                layer: layer.value,
                title: title.value.trim() || "条目",
                statement: statement.value.trim(),
                scope: "timeline",
                success_criteria: criteria.value.trim(),
              });
              await this.ctx.api.waOutlineSave({ ...outline, items });
              setNote(this.note, "已加进大纲；点「绑定 / 更新绑定」之后它才会进入进度", "ok");
              await this.render();
              return true;
            } catch (error) {
              setNote(note, uiError(error, { module: "大纲", action: "加条目" }).message, "bad");
              return false;
            }
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  private async newOutline(): Promise<void> {
    const name = el("input", { class: "u-input", id: "u-wa-outline-name", placeholder: "例如：北堤故事" }) as HTMLInputElement;
    const statement = el("textarea", {
      class: "u-textarea", rows: "2", id: "u-wa-outline-theme", placeholder: "这个故事想做到什么（首条主题约束）",
    }) as HTMLTextAreaElement;
    const criteria = el("input", { class: "u-input", id: "u-wa-outline-criteria", placeholder: "怎样算满足" }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const modal = dialog(
      "新建大纲",
      [
        field("名称", name),
        field("首条主题约束", statement),
        field("判据", criteria),
        paragraph("大纲是约束与目标，不是已经发生的事实：保存后第二条条目起点还是「未开始」。", "u-hint"),
        note,
      ],
      [
        {
          label: "保存大纲",
          // 失败返回 false：保存没成时弹窗不关，窗内提示才看得见（P0-1）
          run: async () => {
            if (!name.value.trim() || !statement.value.trim()) {
              setNote(note, "名称与主题约束都要写", "bad");
              return false;
            }
            try {
              const outline = {
                id: `ol-${Date.now().toString(36)}`,
                name: name.value.trim(),
                items: [
                  {
                    id: `it-${Date.now().toString(36)}`,
                    layer: "theme",
                    title: "主题约束",
                    statement: statement.value.trim(),
                    scope: "world",
                    success_criteria: criteria.value.trim(),
                  },
                ],
              };
              await this.ctx.api.waOutlineSave(outline);
              this.outlineId = outline.id;
              setNote(this.note, `大纲「${outline.name}」已保存：点「绑定 / 更新绑定」把它绑到这条线`, "ok");
              await this.render();
              return true;
            } catch (error) {
              setNote(note, uiError(error, { module: "大纲", action: "保存" }).message, "bad");
              return false;
            }
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  private async bindOutline(): Promise<void> {
    if (!this.outlineId) {
      setNote(this.note, "先选一份大纲（或新建一份）", "bad");
      return;
    }
    if (this.stateError && !this.state) {
      // 读不到状态时不能按「首次绑定」处理：先重试读取，再决定绑定还是更新
        setNote(this.note, "这条世界线的大纲状态还没读到：先重试读取，再决定绑定还是更新", "bad");
      return;
    }
    const bound = Boolean(this.state);
    const chapter = el("input", {
      class: "u-input", id: "u-wa-chapter", value: String(this.state?.chapter ?? "第一章"),
    }) as HTMLInputElement;
    const observers = el("input", {
      class: "u-input", id: "u-wa-observers", value: this.cardId, placeholder: "观察角色（可留空）",
    }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const modal = dialog(
      "绑定 / 更新绑定",
      [
        paragraph(
          bound
            ? "这里只改观察者、章节标签与绑定信息：这条线上的进度原样保留。"
            : "首次绑定让条目从「未开始」起算；已经绑定过的大纲只会更新观察者与章节。",
          "u-hint",
        ),
        field("章节标签", chapter),
        field("观察角色", observers),
        note,
      ],
      [
        {
          label: bound ? "保存这些选择" : "绑定",
          // 失败返回 false：绑定没成时弹窗不关，用户原地看到原因再改（P0-1）
          run: async () => {
            try {
              const result = await this.ctx.api.waBind({
                instance_id: this.instanceId,
                timeline_id: this.timelineId,
                outline_id: this.outlineId,
                observers: refs(observers.value),
                chapter: chapter.value.trim(),
              });
              this.state = (result.state as Json) ?? null;
              setNote(
                this.note,
                result.updated ? "已更新观察者 / 章节：进度原样保留" : "已绑定：条目从未开始起算",
                "ok",
              );
              await this.render();
              return true;
            } catch (error) {
              setNote(note, uiError(error, { module: "大纲", action: bound ? "更新绑定" : "绑定" }).message, "bad");
              return false;
            }
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  /* ------------------------------------------------------------ ② 当前素材（§7.3 上半） */

  private async renderMaterial(host: HTMLElement): Promise<void> {
    host.appendChild(
      panel(
        "只给所选角色合法可知的材料",
        // 动作与读数分行：以前动作和一句说明挤在同一行里，读起来像同一句话的两个部分
        el(
          "div",
          { class: "u-row u-row-wrap" },
          primary("读取当前素材", () => void this.loadMaterial()),
          paragraph(
            this.observed
              ? `读取于 ${stamp(this.observedAt)}｜世界时刻 ${String(this.observed.world_time ?? "")}；冻结或追赶时这里只作历史参考。`
              : "亲历 / 看到 / 听说 / 推测都带来源标签。",
            "u-hint",
          ),
        ),
      ),
    );
    if (!this.observed) return;
    const view = ((this.observed.player_view as Json) ?? {}) as Json;
    const next = ((this.observed.next_step as Json) ?? {}) as Json;
    const materials = (view.materials as Json[]) ?? [];
    const rows = el("div", { class: "u-rows" });
    for (const item of materials.slice(0, 40)) {
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: String(item.text ?? item.summary ?? "") }));
      row.appendChild(chip(String(item.source_label ?? item.source ?? item.kind ?? "材料"), "muted"));
      rows.appendChild(row);
    }
    if (!materials.length) rows.appendChild(paragraph("这个角色目前没有能想起来的材料。", "u-hint"));
    host.appendChild(
      section("这个角色知道的（来源标签在右）", materials.length ? dotLine(`${materials.length} 条能想起来的材料`, "ok") : null, rows),
    );
    const experiences = (view.experiences as Json[]) ?? [];
    const claims = (view.claims as Json[]) ?? [];
    host.appendChild(
      section(
        "这个角色的经历与说法",
        experiences.length || claims.length
          ? bulletList(
              [
                ...experiences.map((item) => `亲历：${String(item.summary ?? item.text ?? "")}`),
                ...claims.map((item) => `听说：${String(item.text ?? item.summary ?? "")}`),
              ].slice(0, 24),
              "u-list",
            )
          : paragraph("暂时没有可列的经历与说法。", "u-hint"),
      ),
    );
    host.appendChild(
      section(
        "作者依据（不给角色 / 玩家看）",
        bulletList(
          [
            ...((next.gaps as Json[]) ?? []).map((item) => `缺口：${String(item.detail ?? item)}`),
            ...((next.deviations as Json[]) ?? []).map((item) => `没有按大纲走：${String(item.detail ?? item)}`),
          ].slice(0, 12),
          "u-list",
        ),
        paragraph("作者身份不等于能读整个世界真值：这里只有这个角色合法知道的东西，加上你这份大纲的缺口与没有按大纲走的地方。", "u-hint"),
      ),
    );
  }

  private async loadMaterial(): Promise<void> {
    if (!this.cardId) {
      setNote(this.note, "先选一个观察角色", "bad");
      return;
    }
    setNote(this.note, "正在读取（按当前完成进度）…", "pending");
    try {
      const result = await this.ctx.api.waObserve({
        instance_id: this.instanceId,
        timeline_id: this.timelineId,
        observer_id: this.cardId,
        outline_id: this.outlineId,
        audience: "author",
      });
      this.observed = result;
      this.observedAt = Date.now() / 1000;
      // 观察结果自带这次依据的水位（内核 `wa.observe` 的 observed_revision）：记下来，
      // 下一次「给我推进建议」带上它，内核就不必重跑整轮世界观察（P1-16）
      const revision = Math.round(Number(result.observed_revision ?? result.world_time ?? 0));
      if (revision > 0) this.observedRevision = revision;
      setNote(
        this.note,
        String(result.status) === "ok" ? "当前素材已读取" : `现在读不到当前素材：${String(result.reason ?? result.status)}（旧材料只作历史参考）`,
        String(result.status) === "ok" ? "ok" : "pending",
      );
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "当前素材", action: "读取" }).message, "bad");
    }
  }

  /* ------------------------------------------------------------ ③ 推进建议（§7.3 下半 + 三个动作） */

  private renderAdvice(host: HTMLElement): void {
    // 未配置 AI 这类「有下一步可做」的失败：页首出一张带动作的错误卡，不只写一行提示（P1-7）
    if (this.aiActionError) host.appendChild(this.aiActionError);
    const goal = el("textarea", {
      class: "u-textarea", rows: "2", id: "u-wa-goal", placeholder: "这一章想让故事走到哪一步（可留空）",
    }) as HTMLTextAreaElement;
    goal.value = this.goal;
    goal.addEventListener("input", () => {
      this.goal = goal.value;
    });
    const limit = el("input", {
      class: "u-input u-input-narrow", type: "number", min: "1", max: "5", value: String(this.limit), id: "u-wa-limit",
    }) as HTMLInputElement;
    limit.addEventListener("input", () => {
      const value = Number(limit.value || 3);
      this.limit = Math.min(5, Math.max(1, Number.isFinite(value) ? Math.round(value) : 3));
    });
    host.appendChild(
      panel(
        "要一组建议",
        el(
          "div",
          { class: "u-row" },
          goal,
          field("建议数量（1–5）", limit),
          primary("给我推进建议", () => void this.suggest()),
        ),
        paragraph(
          "建议只是还没写进世界的稿子：不写世界、不推状态。改世界要走「预览世界变化 → 确认应用」；只想留作文字就「以此起草」。",
          "u-hint",
        ),
      ),
    );
    // 「有几条还没定、几条已经进世界」是一段比例：画出来比数候选卡快（审查：几乎零图形，全靠文字顶）
    const committedCount = this.candidates.filter((item) => Boolean(item.effective)).length;
    const pendingCount = Math.max(0, this.candidates.length - committedCount);
    const compared = this.candidates.length
      ? stackBar([
          { label: "还没定 / 还没进世界", value: pendingCount, tone: "pending" },
          { label: "已进世界", value: committedCount, tone: "ok" },
        ])
      : null;
    const rows = el("div", { class: "u-rows" });
    // 依据三条：先给 ✓/✗ 一眼看全不全，原文照旧在旁边（缺哪条比缺什么更该先看见）
    const basisRow = (name: string, value: string): HTMLElement =>
      el(
        "div",
        { class: "u-row" },
        el("span", { class: `u-check-glyph ${value ? "u-tone-ok" : "u-tone-bad"}`, text: value ? "✓" : "✗" }),
        el("span", { class: "u-hint", text: name }),
        el("span", { class: "u-grow", text: value || "（缺）" }),
      );
    for (const item of this.candidates) {
      const card = el("article", { class: "u-card" });
      card.appendChild(el("h3", { text: String(item.title || item.summary || item.id) }));
      const basis = ((item.basis as Json) ?? {}) as Json;
      card.appendChild(
        facts([
          ["方案", String(item.summary ?? "")],
          ["待定问题", ((item.unsolved as string[]) ?? []).join("、") || "（没有列出的待定问题）"],
          ["是否改世界", item.has_world_change ? "含世界变化（要预览后确认）" : "不改世界（只作文字）"],
          ["能不能给玩家看", String(item.audience) === "player" ? "可给玩家看" : "只给你 / 主持人看"],
        ]),
      );
      card.appendChild(
        el(
          "div",
          { class: "u-rows" },
          basisRow("依据·事实", String(basis.fact ?? "")),
          basisRow("依据·因果", String(basis.causality ?? "")),
          basisRow("依据·大纲", String(basis.outline ?? "")),
        ),
      );
      card.appendChild(
        el(
          "div",
          { class: "u-row" },
          chip(STATUS_TEXT[String(item.status)] ?? String(item.status), item.uncommitted ? "pending" : "muted"),
          item.locked ? chip("正文已锁定", "ok") : null,
          // 「提交依据」这种内部说法换成用户能问出口的三个问题：进世界了吗、有据可查吗、我批了吗
          item.effective
            ? chip("已进世界", "ok")
            : String(item.status) === "committed"
              ? chip("缺少可作为依据的记录（不显示为已生效）", "bad")
              : null,
        ),
      );
      if (item.must_not_imply) card.appendChild(paragraph(String(item.must_not_imply), "u-hint"));
      const actions = el("div", { class: "u-row" });
      actions.appendChild(button("以此起草", () => this.startDraft(item)));
      if (item.has_world_change) {
        actions.appendChild(button("预览世界变化", () => void this.previewChange(item)));
        actions.appendChild(button("另开分支试演", () => void this.trialBranch(item)));
      } else {
        actions.appendChild(button("采用这段文字", () => void this.decideCandidate(item, "approved", String(item.text ?? item.summary ?? ""))));
      }
      actions.appendChild(button("拒绝", () => void this.decideCandidate(item, "rejected")));
      card.appendChild(actions);
      rows.appendChild(card);
    }
    if (!this.candidates.length) {
      if (this.candidatesError) {
        // 读失败与「没有建议」不可分：有错就出错误卡 + 重试
        rows.appendChild(errorCard(this.candidatesError, [{ label: "重试", run: () => this.retryRead() }]));
      } else {
        rows.appendChild(paragraph("还没有建议：点上面的按钮要一组；这一次生成不会覆盖已经采用的内容。", "u-hint"));
      }
    }
    host.appendChild(section("待决定的建议", compared, rows));
  }

  /** 是否已配好 AI 服务：判定与 contact.ts / home.ts 同一来源（`readiness.ai.configured`），不新造 */
  private aiReady(): boolean {
    return Boolean(((this.ctx.readiness.ai as Json | undefined) ?? {}).configured);
  }

  /**
   * 「有下一步可做」的失败升级成错误卡 + 按钮（P1-7）：
   * 未配置 AI 是典型——只写一行「还没有配置 AI 服务」，用户不知道去哪配。
   */
  private showActionError(message: string, action: string, retry: () => void): void {
    const info: UiError = {
      module: "辅助写作",
      action,
      target: "",
      stage: "",
      code: "llm_not_configured",
      message,
      retryable: true,
      done: "没有任何改动，世界里什么都没发生",
      unknown: "这次操作是否已生效",
      field: "",
      requestId: "",
    };
    const actions = [{ label: "去设置 AI 服务", run: () => this.ctx.navigate({ pane: "settings", sub: "ai" }) }];
    // AI 已经配好时再给「重试」：再点一次有意义，不然只是重复同一句错误
    if (this.aiReady()) actions.unshift({ label: "重试", run: retry });
    this.aiActionError = errorCard(info, actions);
  }

  private async suggest(): Promise<void> {
    if (!this.state) {
      setNote(
        this.note,
        this.stateError ? "这条世界线的大纲状态还没读到：先重试读取，再要建议" : "先绑定一份大纲：建议要挂在大纲与角色上",
        "bad",
      );
      return;
    }
    if (!this.aiReady()) {
      // 未配置 AI：别等请求失败才说，直接给可点的下一步，省掉一次白等的等待
      this.aiActionError = null;
      this.showActionError("还没有配置 AI 服务，所以没法生成推进建议", "生成推进建议", () => void this.suggest());
      setNote(this.note, "还没有配置 AI 服务：配好后再回来要建议", "bad");
      await this.render();
      return;
    }
    this.aiActionError = null;
    setNote(this.note, "正在要一组建议…", "pending");
    try {
      // 先补一次「我已读到的水位」（读不到就是 0，不编造）；带上它内核就能复用已有投影（P1-16）
      await this.refreshObservedRevision();
      const result = await this.ctx.api.waSuggest({
        instance_id: this.instanceId,
        timeline_id: this.timelineId,
        outline: this.outlineId,
        observer: this.cardId,
        goal: this.goal,
        limit: this.limit,
        // P1-16：客户端刚读到的观察 revision。**需要内核配合**：`wa.suggest` 目前没有这个参数
        // （`WritingService.suggest` 每次自行 `observe()` 重跑整轮世界观察），内核侧另一路实现后
        // 这个字段才会生效；在此之前多带的参数被忽略，行为与现在一致。
        ...(this.observedRevision ? { observed_revision: this.observedRevision } : {}),
      });
      const status = String(result.status);
      const made = (result.candidates as Json[]) ?? [];
      if (status === "unparsable") {
        setNote(
          this.note,
          `没有取得可用输出：${String(result.reason ?? "")}｜模型原文：${String(result.raw_excerpt ?? "").slice(0, 80)}`,
          "bad",
        );
      } else if (status === "ok" && !made.length) {
        setNote(this.note, `这一轮确实没有建议：${String(result.reason ?? "模型没有给出方案")}`, "pending");
      } else if (status === "ok") {
        setNote(this.note, `拿到 ${made.length} 条建议（还没写进世界，不影响它）`, "ok");
      } else {
        setNote(this.note, `这次没有拿到建议：${String(result.reason ?? status)}`, "bad");
      }
      this.invalidateWorkspace(); // 内容变更动作：显式失效，render() 会按新对象重取
      this.tab = "advice";
      await this.render();
    } catch (error) {
      const info = uiError(error, { module: "推进建议", action: "要一组建议" });
      setNote(this.note, info.message, "bad");
      // 核心也会以 llm_not_configured 拒掉这次调用：同样升级成带「去设置 AI 服务」的错误卡
      if (info.code === "llm_not_configured" || !this.aiReady()) {
        this.showActionError(info.message, "生成推进建议", () => void this.suggest());
        await this.render();
      }
    }
  }

  private async decideCandidate(item: Json, status: string, text = ""): Promise<void> {
    setNote(this.note, "正在记下你的决定…", "pending");
    try {
      const result = await this.ctx.api.waCandidateDecide(
        { candidate_id: String(item.id), status, reason: status === "approved" ? "作者采用" : "作者拒绝", text },
        this.instanceId,
        this.timelineId,
      );
      const candidate = ((result.candidate as Json) ?? {}) as Json;
      setNote(
        this.note,
        status !== "approved"
          ? "已拒绝：这条不会进世界"
          : candidate.has_world_change
            ? "已采用；它含世界变化，还没进世界——要走「预览世界变化 → 确认应用」才会提交"
            : "已采用：它只是一段文字，不改世界",
        status === "approved" && candidate.has_world_change ? "pending" : "ok",
      );
      this.invalidateWorkspace(); // 内容变更动作：显式失效，render() 会按新对象重取
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "推进建议", action: "记下决定" }).message, "bad");
    }
  }

  private startDraft(item: Json): void {
    this.draftId = String(item.id);
    this.draftTitle = String(item.title || "文字草稿");
    this.draftBody = String(item.text || item.summary || "");
    this.draftKey = `text:${this.instanceId}:${item.id}`;
    // 这份内容还没成为「文字草稿」：草稿列表要显示一条「正在编辑（未保存）」占位（P1-10）
    this.draftFromSuggestion = !this.candidates.some(
      (entry) => String(entry.id) === this.draftId && String(entry.kind) === "text",
    );
    this.tab = "draft";
    void this.render();
  }

  private async previewChange(item: Json): Promise<void> {
    const changes = (item.changes as Json[]) ?? [];
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const modal = dialog(
      "预览世界变化",
      [
        paragraph(`这条建议：${String(item.title || item.summary || "")}`),
        changes.length
          ? bulletList(
              changes.map(
                (change) =>
                  `对象：${((change.target_refs as string[]) ?? []).join("、") || "（未注明）"}｜变化：${String(change.kind)}（${String(change.operation)}）→ ${String(change.value ?? "")}`,
              ),
              "u-list",
            )
          : paragraph("这条建议没有附带世界变化：它只能作为文字采用。", "u-hint"),
        paragraph(
          "预览只列要发生什么，不改世界。确认应用 = 采用这条建议并提交到这条世界线（会留下本次提交记录）；采用与提交是两步，界面一起做，缺一步都不算生效。",
          "u-hint",
        ),
        note,
      ],
      [
        {
          label: "确认应用到这条世界线",
          // 失败返回 false：建议没进世界时窗要留着，理由写在窗内（P0-1）
          run: async () => {
            if (!changes.length) {
              setNote(note, "这条建议没有世界变化可用", "bad");
              return false;
            }
            try {
              // 先「采用」（approved），再提交：核心要求只有已批准的建议才能进世界
              if (String(item.status) !== "approved") {
                const adopted = await this.ctx.api.waCandidateDecide(
                  { candidate_id: String(item.id), status: "approved", reason: "作者确认应用", text: String(item.text ?? "") },
                  this.instanceId,
                  this.timelineId,
                );
                // 决定接口返回的是建议本身（不带顶层 status）：采用成没成看它自己那一个
                if (String((adopted.candidate as Json)?.status ?? "") !== "approved") {
                  setNote(note, `没能采用这条建议：${String((adopted.candidate as Json)?.reason ?? "核心没有采用它")}`, "bad");
                  return false;
                }
              }
              const result = await this.ctx.api.waCandidateCommit(String(item.id), this.instanceId, this.timelineId);
              const status = String(result.status);
              if (status === "ok" || status === "duplicate") {
                setNote(
                  this.note,
                  status === "duplicate" ? "这份变化之前已经提交过：世界没有再变一次" : "已提交：世界里已经按它变化",
                  "ok",
                );
                this.invalidateWorkspace(); // 内容变更动作：显式失效，render() 会按新对象重取
                await this.render();
                return true;
              }
              setNote(note, `没有生效：${String(result.reason ?? status)}（这条建议还在，可以检查后重来）`, "bad");
              return false;
            } catch (error) {
              setNote(note, uiError(error, { module: "世界变化", action: "确认应用", done: "这条建议还在，没有被改写" }).message, "bad");
              return false;
            }
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  private async trialBranch(item?: Json): Promise<void> {
    const name = el("input", { class: "u-input", id: "u-wa-branch-name", placeholder: "例如：试演·封堤" }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    let head = "";
    try {
      const commits = await this.ctx.api.commits(this.instanceId, this.timelineId);
      head = String(((commits.commits as Json[]) ?? [])[0]?.id ?? "");
    } catch {
      head = "";
    }
    // 来源版本写人话：内部版本号只收进技术详情，正文里说「用了哪个时间点的版本」
    const headRow = el("p", { class: "u-hint", text: head ? "来源版本：这条线最近保存的那个版本" : "" });
    const modal = dialog(
      "试演（不落线）",
      [
        paragraph(
          item
            ? `要试演的是「${String(item.title || item.summary || "这条建议")}」：先在**当前线**上看一遍会发生什么（只读预览，不建线、不动世界）；只有你点「应用于试演线」时才真正另开一条线去落它。`
            : "先在当前线看一遍会发生什么（只读预览，不建线、不动世界）；点「应用于试演线」时才另开一条线去落它。",
        ),
        field("新世界线名称（应用时才用到）", name),
        head
          ? headRow
          : paragraph("这条线还没有保存过版本：先点下面的「保存当前版本点」，再从它分支。", "u-hint"),
        note,
      ],
      [
        // 用户不必为了「试演」先跑去别的页面存版本点：这一步就放在窗里（P1-5）
        ...(head
          ? []
          : [
              {
                label: "保存当前版本点",
                // 保存失败返回 false：不关窗，原因写在窗内；成功则就地补上版本点并留在窗里继续分支
                run: async (): Promise<boolean> => {
                  try {
                    // runtime.commit 回的是 { commit: {...} }：编号在 commit.id 上（不是 commits 列表）
                    const saved = await this.ctx.api.saveVersion(this.instanceId, this.timelineId, "试演前保存当前进度");
                    head = String((saved.commit as Json | undefined)?.id ?? saved.commit_id ?? "");
                    if (!head) {
                      // 回包里没有编号就再读一次提交列表：拿到就继续，拿不到就如实说保存没成功
                      const again = await this.ctx.api.commits(this.instanceId, this.timelineId);
                      head = String(((again.commits as Json[]) ?? [])[0]?.id ?? "");
                    }
                    if (!head) {
                      setNote(note, "版本点没有保存成功：请重试一次", "bad");
                      return false;
                    }
                    this.ctx.rememberRecent({
                      pane: "writing",
                      label: `写作 · 已保存的版本（${this.draftTitle || "当前正文"}）`,
                      key: `writing:${this.instanceId}:${this.timelineId}:versions`,
                    });
                    setNote(note, "已保存当前版本点：现在可以「建立暂停分支」了", "ok");
                    return false; // 步骤还没走完：留着窗让用户点下一步
                  } catch (error) {
                    setNote(note, uiError(error, { module: "试演", action: "保存当前版本点" }).message, "bad");
                    return false;
                  }
                },
              },
            ]),
        {
          label: "得到试演预览（不建线）",
          // 只读预览：`runtime.change.preview` 不写世界、不建线（P2-13）
          run: async () => {
            const changes = (item?.changes as Json[]) ?? [];
            if (!changes.length) {
              setNote(note, "这条建议没有世界变化可试演：它只能作为文字采用", "bad");
              return false;
            }
            try {
              const preview = await this.ctx.api.changePreview(changes, this.instanceId, this.timelineId);
              const rejected = ((preview.rejected_candidates as Json[]) ?? []).map(
                (entry) => `${String(entry.id)}：${String(entry.reason)}`,
              );
              const conflicts = ((preview.conflicts as Json[]) ?? []).map((entry) => String(entry.kind));
              if (rejected.length || conflicts.length) {
                setNote(
                  note,
                  `这条建议在预览里没通过：${[...rejected, ...conflicts.map((kind) => `版本冲突（${kind}）`)].join("；")}。没有建线，也没有改动世界。`,
                  "bad",
                );
                return false;
              }
              setNote(
                note,
                `预览通过：这条线现在可以落这条变化（还剩「应用于试演线」这一步）。试演本身不落线——上面预览没有新建任何时间线，也没有改动世界。`,
                "ok",
              );
              return false;
            } catch (error) {
              setNote(note, uiError(error, { module: "试演", action: "预览世界变化" }).message, "bad");
              return false;
            }
          },
        },
        {
          label: "应用于试演线（此时才建线）",
          // 失败返回 false：分支没建起来时窗不关，原因看得见（P1-5 与 P0-1）
          run: async () => {
            if (!head) {
              // 已经有「保存当前版本点」按钮：提示直接点名屏幕上的字，不再让用户去别的页面找
              setNote(note, "先点左边的「保存当前版本点」，再从它分支", "bad");
              return false;
            }
            try {
              const result = await this.ctx.api.waBranch({
                instance_id: this.instanceId,
                timeline_id: this.timelineId,
                commit_id: head,
                name: name.value.trim() || "试演线",
              });
              const timeline = (result.timeline as Json) ?? {};
              const newId = String(timeline.id ?? "");
              const newName = String(timeline.name ?? name.value.trim() ?? "试演线");
              if (newId) {
                // 记进本页选择：用户回来还能在这页的下拉里找到刚建的试演线（P1-5）
                this.extraTimelines = [...this.extraTimelines, { id: newId, name: newName, state: "paused" }];
                this.timelineId = newId;
                this.ctx.rememberRecent({
                  pane: "writing",
                  label: `写作 · 试演线「${newName}」`,
                  key: `writing:${this.instanceId}:${newId}`,
                });
              }
              setNote(
                this.note,
                `已建线并切到「${newName}」：这条建议现在在这条试演线上落；主线不受影响，也不提供把两条线合回一起。建线只发生在你点这一下之后。`,
                "ok",
              );
              await this.render();
              return true;
            } catch (error) {
              setNote(note, uiError(error, { module: "试演", action: "建立试演线" }).message, "bad");
              return false;
            }
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  /* ------------------------------------------------------------ ④ 文字草稿（§7.5） */

  private renderDraft(host: HTMLElement): void {
    const drafts = this.candidates.filter((item) => String(item.kind) === "text");
    const list = el("div", { class: "u-rows" });
    // 「以此起草」打开的内容还没成为草稿：列表先给它一条占位行，不然这里写「还没有文字草稿」（P1-10）
    const editingUnsaved =
      Boolean(this.draftId) && this.draftFromSuggestion && !drafts.some((item) => String(item.id) === this.draftId);
    if (editingUnsaved) {
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: `${this.draftTitle || "正在编辑的正文"}（正在编辑，未保存）` }));
      row.appendChild(chip("未保存", "pending"));
      row.appendChild(button("继续编辑", () => void this.render()));
      list.appendChild(row);
    }
    for (const item of drafts) {
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: String(item.title || "（没有标题的草稿）") }));
      row.appendChild(
        chip(
          item.locked ? "已锁定" : STATUS_TEXT[String(item.status)] ?? String(item.status),
          item.locked ? "ok" : "muted",
        ),
      );
      row.appendChild(
        button("打开", () => {
          this.startDraft(item);
        }),
      );
      list.appendChild(row);
    }
    if (!drafts.length && !editingUnsaved) {
      if (this.candidatesError) {
        list.appendChild(errorCard(this.candidatesError, [{ label: "重试", run: () => this.retryRead() }]));
      } else {
        list.appendChild(paragraph("还没有文字草稿：在「推进建议」里点「以此起草」，或直接在下面新建一份。", "u-hint"));
      }
    }
    host.appendChild(
      panel(
        "草稿列表",
        list,
        button("新建空白草稿", () =>
          this.startDraft({ id: `draft-${Date.now().toString(36)}`, title: "新草稿", text: "", kind: "text", status: "proposed" }),
        ),
      ),
    );

    const title = el("input", { class: "u-input", id: "u-wa-draft-title", value: this.draftTitle }) as HTMLInputElement;
    const body = el("textarea", {
      class: "u-textarea u-textarea-tall", rows: "12", id: "u-wa-draft-body", placeholder: "写正文（可留 Markdown）",
    }) as HTMLTextAreaElement;
    body.value = this.draftBody;
    title.addEventListener("input", () => {
      this.draftTitle = title.value;
      this.queueAutosave();
    });
    body.addEventListener("input", () => {
      this.draftBody = body.value;
      this.queueAutosave();
    });
    const current = drafts.find((item) => String(item.id) === this.draftId);
    const locked = Boolean(current?.locked);
    // 没保存过的内容没有可锁定的稿子：先说清「要先保存」，别让用户点了只得到「没有该草稿」（P1-3）
    const savedDraft = Boolean(current);
    const unsaved = Boolean(this.draftId) && !savedDraft;
    const status = el("p", {
      class: "u-note",
      role: "status",
      "aria-live": "polite",
      text: this.draftId
        ? unsaved
          ? `正在编辑「${this.draftTitle || "（没有标题）"}」：还没保存过，点「保存文字草稿」才会进草稿列表`
          : `正在编辑「${this.draftTitle || "（没有标题）"}」${locked ? "（已锁定）" : ""}`
        : "还没有选中的草稿",
    });
    host.appendChild(
      panel(
        "正文",
        field("标题", title),
        field("正文", body),
        el(
          "div",
          { class: "u-row" },
          primary("保存文字草稿", () => void this.saveDraft()),
          locked
            ? button("解锁并编辑", () => void this.lockDraft(false))
            : button("锁定正文", () => void this.lockDraft(true), { disabled: unsaved }),
          button("另存为新稿", () => void this.copyDraft()),
          button("导出所选文字…", () => void this.exportDraft()),
        ),
        unsaved
          ? paragraph("「锁定正文」要先把这份内容保存成草稿：先点「保存文字草稿」。", "u-hint")
          : null,
        paragraph(
          "保存文字不等于世界已改变。连续保存会就地更新同一份草稿；要留一份新的，用「另存为新稿」。锁定后新生成永远另起一稿，世界恢复也不会改写它。",
          "u-hint",
        ),
        paragraph(
          `导出范围：只导出标题与正文，导出文件是 Markdown（.md）；依据、幕后材料与大纲都不在导出内容里。`,
          "u-hint",
        ),
        status,
      ),
    );
    if (this.savedNotice) host.appendChild(paragraph(this.savedNotice, "u-hint"));
  }

  private savedNotice = "";

  private queueAutosave(): void {
    if (this.autoTimer !== null) window.clearTimeout(this.autoTimer);
    this.autoTimer = window.setTimeout(() => void this.flushDraft(), 1000);
  }

  private async flushDraft(): Promise<void> {
    if (!this.draftKey || !this.instanceId) return;
    try {
      await this.ctx.api.draftSave(this.draftKey, "writing", this.draftTitle, this.draftBody, {
        title: this.draftTitle,
        body: this.draftBody,
        candidate: this.draftId,
      });
      this.savedNotice = `已自动保存界面草稿（${stamp(Date.now() / 1000)}）`;
    } catch {
      /* 自动失败不打断写作：显式保存时会再报一次 */
    }
  }

  private async saveDraft(): Promise<void> {
    if (!this.instanceId) return;
    if (!this.draftBody.trim()) {
      setNote(this.note, "正文还是空的：先写点东西再保存", "bad");
      return;
    }
    if (!this.outlineId) {
      // 保存要挂在大纲上：没绑大纲时说清先做哪一步，别等核心报 not_found
      setNote(this.note, "先绑定一份大纲：文字草稿要挂在大纲上，才能进草稿列表", "bad");
      return;
    }
    const current = this.candidates.find((item) => String(item.id) === this.draftId);
    // 已锁定 / 已提交的稿子不动：只有「还是 proposed 的同一份」才就地覆盖（§7.5、§11.1）
    const fresh = !current || Boolean(current.locked) || String(current.status) !== "proposed";
    // 就地覆盖的目标：正在编辑的这一份（哪怕它只是「以此起草」打开的、还没保存过）
    const targetId = !fresh ? this.draftId : `${this.draftId || "draft"}-${Date.now().toString(36)}`;
    setNote(this.note, "正在保存文字草稿…", "pending");
    try {
      await this.ctx.api.waCandidatePropose(
        {
          candidate_id: targetId,
          kind: "text",
          title: this.draftTitle || "未命名草稿",
          summary: this.draftBody.replace(/\s+/g, " ").slice(0, 60),
          text: this.draftBody,
          audience: "author",
          basis: { fact: "", causality: "", outline: "" },
        },
        this.instanceId,
        this.timelineId,
        this.outlineId,
      );
      await this.ctx.api.waCandidateDecide(
        { candidate_id: targetId, status: "approved", reason: "保存文字草稿", text: this.draftBody },
        this.instanceId,
        this.timelineId,
      );
      await this.flushDraft();
      const name = this.draftTitle || "未命名草稿";
      const replaced = targetId === this.draftId;
      this.draftId = targetId;
      this.draftFromSuggestion = false;
      this.invalidateWorkspace(); // 内容变更动作：显式失效，render() 会按新对象重取
      // 提示说清「这份被更新了」还是「新存了一份」：不印内部编号，用户要认的是标题
      setNote(
        this.note,
        replaced
          ? `已保存「${name}」：连续保存更新的是这一份，不等于世界已改变`
          : `已另存为「${name}」：新的那一份进草稿列表，不等于世界已改变`,
        "ok",
      );
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "文字草稿", action: "保存", done: "文字还在编辑区里，没有丢" }).message, "bad");
    }
  }

  private async lockDraft(locked: boolean): Promise<void> {
    if (!this.draftId) {
      setNote(this.note, "先打开或保存一份草稿", "bad");
      return;
    }
    // 没保存过的内容锁不了：核心按草稿编号找它，找不到只会回「没有该草稿」（P1-3）
    if (locked && !this.candidates.some((item) => String(item.id) === this.draftId)) {
      setNote(this.note, "这份内容还没保存过：先点「保存文字草稿」，再点「锁定正文」", "bad");
      return;
    }
    try {
      const result = await this.ctx.api.waTextLock(locked, this.instanceId, this.timelineId, this.draftId);
      const candidate = ((result.candidate as Json) ?? {}) as Json;
      setNote(
        this.note,
        locked
          ? `正文已锁定（${stamp(Number(candidate.locked_at ?? Date.now() / 1000))}）：后续生成与世界恢复都不会覆盖它`
          : "已解锁：现在可以改这份稿子（改完记得再保存）",
        "ok",
      );
      this.invalidateWorkspace(); // 内容变更动作：显式失效，render() 会按新对象重取
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "文字草稿", action: locked ? "锁定" : "解锁" }).message, "bad");
    }
  }

  private async copyDraft(): Promise<void> {
    this.draftId = `${this.draftId || "draft"}-copy-${Date.now().toString(36)}`;
    this.draftTitle = `${this.draftTitle || "草稿"}（副本）`;
    this.draftKey = `text:${this.instanceId}:${this.draftId}`;
    // 副本还没保存过：让草稿列表给它一条「正在编辑（未保存）」占位，也让它先保存再锁定
    this.draftFromSuggestion = true;
    setNote(this.note, "已另存为新稿：这是还没保存的副本，点「保存文字草稿」才会进草稿列表；原来那份不动", "muted");
    await this.render();
  }

  private async exportDraft(): Promise<void> {
    if (!this.draftBody.trim()) {
      setNote(this.note, "没有可导出的正文", "bad");
      return;
    }
    try {
      await this.flushDraft();
      const path = await invoke<string | null>("save_text_file", {
        title: "导出所选文字",
        name: `${(this.draftTitle || "草稿").replace(/[\\/:*?"<>|]/g, "_")}.md`,
        text: `# ${this.draftTitle || "未命名草稿"}\n\n${this.draftBody}\n`,
      });
      if (!path) {
        setNote(this.note, "已取消导出", "muted");
        return;
      }
      setNote(this.note, `已导出：${path}（Markdown 文件，只有标题与正文，不含依据、幕后材料与大纲）`, "ok");
    } catch (error) {
      setNote(this.note, uiError(error, { module: "文字草稿", action: "导出" }).message, "bad");
    }
  }
}
