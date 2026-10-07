/*
 * 世界与素材（USER_INTERFACE_DESIGN §5 的入口部分 + §9 时间线与版本）。
 *
 * 首版这一层给到：我的世界列表 / 世界详情（角色、时间线、运行与暂停、版本记录与恢复）/
 * 世界设定与角色卡的清单（看状态与能不能用）/ 导入导出。
 * 创作工作区（结构化表单、AI 生成、锁定）按实施顺序属 U2：这里不摆不可用的空壳按钮。
 */

import { invoke } from "@tauri-apps/api/core";
import type { AppContext, Pane } from "./app";
import { openDir } from "./app";
import type { InstanceEntry, Json } from "./api";
import { newRequestId, uiError } from "./api";
import {
  anchors,
  bulletList,
  button,
  chip,
  dialog,
  el,
  errorCard,
  facts,
  field,
  fill,
  humanDuration,
  link,
  pageHead,
  panel,
  paragraph,
  primary,
  section,
  setNote,
  stamp,
  tools,
} from "./dom";
import { branchGraph, flowRail, type BranchLane } from "./graphics";
// 校验问题翻人话：与创作工作区共用同一张分区/字段对照表（一处改、两处一致）
import { describeProblem, sectionsOf } from "./create";

/** 时间线状态的人话（核心给的是内部状态名） */
const STATE_TEXT: Record<string, string> = {
  active: "运行中",
  frozen: "已暂停",
  archived: "已归档",
};

/**
 * 世界列表那一格「状态」要按条数分情况说，不能只看有没有 running：
 * 没有时间线的新世界曾经显示成「已归档」，一条暂停一条归档会显示成「全部暂停」（2026-10-07 评审 P2）。
 * 返回 null = 真的一条线都没有，由调用方单独说「还没有世界线」。
 */
function worldStateText(timelines: Json[], running: string[]): { text: string; kind: "ok" | "pending" | "bad" | "muted" } | null {
  const total = timelines.length;
  if (!total) return null;
  // 条数以「按状态数」为准：世界钟读不到时 running 里是空串，那一档归到「读数不全」
  const states = timelines.map((item) => String(item.state ?? ""));
  const archived = states.filter((state) => state === "archived").length;
  const active = states.filter((state) => state === "active").length;
  const frozen = states.filter((state) => state === "frozen").length;
  const unreadable = running.filter((state) => !state).length;
  if (active) return { text: active === total ? `全部运行中（${total} 条）` : `有 ${active} 条运行中（共 ${total} 条）`, kind: "ok" };
  if (archived === total) return { text: `全部已归档（${archived} 条，可从版本另开分支）`, kind: "muted" };
  if (frozen === total) return { text: `全部暂停（${frozen} 条）`, kind: "pending" };
  const parts: string[] = [];
  if (frozen) parts.push(`${frozen} 条暂停`);
  if (archived) parts.push(`${archived} 条已归档`);
  if (unreadable) parts.push(`${unreadable} 条没读到状态`);
  return { text: parts.length ? parts.join("、") : "状态未知", kind: archived ? "muted" : "pending" };
}

/** 世界时刻（世界秒）→ 世界内日期：第 N 天 时:分（用户不看裸数字，评审第四节的「129600000」） */
function worldDayText(seconds: number, daySeconds = 86400): string {
  const value = Math.max(0, Math.round(seconds));
  const day = Math.floor(value / Math.max(1, daySeconds));
  const rest = value % Math.max(1, daySeconds);
  const hours = Math.floor(rest / 3600);
  const minutes = Math.floor((rest % 3600) / 60);
  return `第 ${day + 1} 天 ${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`;
}

/** 时间线的可读名（内部 id 只进「技术详情」） */
function timelineNameOf(timelines: Json[], timelineId: string): string {
  const hit = timelines.find((item) => String(item.id ?? "") === timelineId);
  return String(hit?.name ?? "") || "这条世界线";
}

/** 内部标识 / 文件名的折叠区：正文只留名字，技术细节要用时能展开（评审第六节） */
function techDetails(rows: Array<[string, string]>): HTMLElement {
  return el("details", { class: "u-error-detail" }, el("summary", { text: "技术详情" }), facts(rows.filter(([, value]) => Boolean(value))));
}

/**
 * 版本记录 → 分支图的泳道。主线在前、分叉按辈分排：`source_commit` 指到哪条线的提交，
 * 就说明这条线是从那里长出来的（平铺的列表看不出这层关系，所以要画）。
 */
function branchLanes(timelines: Json[], commits: Json[]): BranchLane[] {
  const laneOfCommit = new Map<string, string>();
  for (const commit of commits) laneOfCommit.set(String(commit.id ?? ""), String(commit.timeline_id ?? ""));
  const lanes: BranchLane[] = timelines.map((timeline) => {
    const id = String(timeline.id);
    return {
      id,
      name: String(timeline.name ?? id),
      state: String(timeline.state ?? ""),
      source: String(timeline.source_commit ?? "") || undefined,
      commits: commits
        .filter((commit) => String(commit.timeline_id ?? "") === id)
        .map((commit) => ({
          id: String(commit.id ?? ""),
          moment: Number(commit.moment ?? 0),
          kind: String(commit.kind ?? ""),
          title: `${stamp(Number(commit.created_at ?? 0))}｜${String(commit.kind ?? "")}${commit.note ? `｜${String(commit.note)}` : ""}`,
        })),
    };
  });
  const depthOf = (lane: BranchLane, seen = new Set<string>()): number => {
    if (seen.has(lane.id)) return 0;
    seen.add(lane.id);
    const parent = lane.source ? laneOfCommit.get(lane.source) : "";
    const next = lanes.find((item) => item.id === parent && item.id !== lane.id);
    return next ? 1 + depthOf(next, seen) : 0;
  };
  return lanes.sort((a, b) => depthOf(a) - depthOf(b) || String(a.id).localeCompare(String(b.id)));
}

/** 世界速度的人话：光看「世界秒 / 现实秒」的数字看不出这是多快 */
function rateText(rate: number): string {
  const value = Number.isFinite(rate) && rate > 0 ? rate : 1;
  if (value === 1) return "现实 1 分钟 ≈ 世界 1 分钟（一比一）";
  return `现实 1 分钟 ≈ 世界 ${humanDuration(value * 60)}`;
}

/** 效果持续到什么时候（核心给的是内部枚举） */
const EXPIRY_TEXT: Record<string, string> = {
  with_cause: "跨日后随原因解除",
  natural_recovery: "达到恢复条件后自行恢复",
  until_cleared: "保留到被明确解除",
};

/**
 * 兼容性的话（2026-10-08 视觉体系审查：`instance.compatibility` 是内部枚举，
 * 原来原样印在事实表里，用户读到的是 `compatible` 这种程序词）。
 * 三档判定来自核心 `instances.compatibility`：compatible / convertible / blocked；
 * 原始值与核心写的说明只进「技术详情」，正文只说「这个能不能直接打开」。
 */
const COMPAT_TEXT: Record<string, string> = {
  compatible: "兼容当前版本",
  convertible: "需转换后才能使用（见技术详情）",
  blocked: "不兼容（见技术详情）",
};

function compatibilityText(value: unknown): string {
  const raw = String(value ?? "");
  if (!raw) return "未标注";
  return COMPAT_TEXT[raw] ?? "不兼容（见技术详情）";
}

/**
 * 「加入角色」向导（§5.4）：目标线 → 卡片审定 → 加入时间与说明 → 确认。
 * 四步都留在世界详情页内，不新开窗口；每一步只问一件事，确认前把要发生的事列全。
 */
type JoinStep = "timeline" | "card" | "when" | "review";

const JOIN_STEPS: Array<{ id: JoinStep; label: string }> = [
  { id: "timeline", label: "目标线" },
  { id: "card", label: "确认角色卡" },
  { id: "when", label: "加入时间与说明" },
  { id: "review", label: "确认" },
];

export class WorldsPane implements Pane {
  readonly id = "worlds" as const;
  private view: "list" | "detail" | "change" | "join" = "list";
  /** 并列分栏的当前格（「我的世界」/「世界设定与角色卡」）：只由各自的渲染函数设置，切换时看得见 */
  private tab: "worlds" | "assets" | "detail" = "worlds";
  private current: InstanceEntry | null = null;
  private note: HTMLElement | null = null;
  /** 动作结果：rerender 会重建反馈槽，先把话记下来，渲染完再写回 */
  private pendingNote: { text: string; kind: "ok" | "bad" | "pending" | "muted" } | null = null;
  private showArchived = false;
  /** 加入角色向导的状态：选中的目标线 / 卡片文件 / 说明，以及本次向导固定的请求标识 */
  private joinStep: JoinStep = "timeline";
  private joinTimelineId = "";
  private joinCardFile = "";
  private joinNote = "";
  private joinRequestId = "";
  /** 当前世界详情里的线清单：版本区里的恢复对话框要用线名做确认短语（不再多读一次） */
  private timelinesCache: Json[] = [];

  constructor(private readonly ctx: AppContext) {}

  async mount(host: HTMLElement): Promise<void> {
    // 入口带的下标要认：`detail` / `timeline` = 直接开世界详情（时间线就在它里面），`import` = 直接选文件
    const sub = this.ctx.route.sub;
    if ((sub === "detail" || sub === "timeline") && this.ctx.instances().length) {
      this.view = "detail";
      // 从别的页面（如创建向导的「打开这个世界」）进来时，最近使用里记着该开哪一个；
      // 没有记录才退回第一条（旧行为）
      this.current = this.recentInstance() ?? this.ctx.instances()[0];
      this.rememberWorld(this.current);
    }
    await this.render(host);
    if (sub === "import") await this.importFlow();
  }

  /** 「最近使用」里记的这个世界（key 形如 `worlds:<id>`）：找不到就返回 null */
  private recentInstance(): InstanceEntry | null {
    const recent = this.ctx.prefs.recent;
    if (!Array.isArray(recent)) return null;
    for (const entry of recent as Array<Record<string, unknown>>) {
      const key = String(entry?.key ?? "");
      if (!key.startsWith("worlds:")) continue;
      const hit = this.ctx.instances().find((item) => `worlds:${item.id}` === key);
      if (hit) return hit;
    }
    return null;
  }

  private async render(host: HTMLElement): Promise<void> {
    // 「分区点击被初始渲染覆盖」的竞态：render() 第一件事是 await refresh()，用户完全可能在这期间点
    // 「世界设定 / 角色卡」；原来紧接着的 `this.tab = "worlds"` 会把这次点击抹掉，页面停在列表、
    // 选中态也回到「我的世界」（2026-10-08 探针实测：必须先等页面画完再点才正常）。
    const askedTab = this.tab;
    await this.ctx.refresh();
    if (askedTab !== "assets" && this.tab === "assets") return;
    // 没有选中世界时不可能停在详情类视图：先归位再决定骨架，
    // 否则标题带会写出「世界」这种空话。顺带把分区也归位：从详情回列表时
    // tab 还停在 "detail"，两个分区会都不高亮（工具带要说得出「你在哪一栏」）
    if (this.view !== "list" && !this.current) this.view = "list";
    if (this.view === "list") this.tab = "worlds";
    const page = el("div", { class: "u-page" });
    // 页面骨架：列表 / 素材视图是 标题带 + 工具带；详情类视图的标题是当前世界的名字
    // （顶栏已经写着「世界与素材」，正文再写一遍就成了三层标题 —— 2026-10-08 视觉体系审查）
    if (this.view === "list") {
      page.appendChild(this.listHead());
      page.appendChild(this.toolsBar());
    } else {
      page.appendChild(this.viewHead());
    }
    const body = el("div", { class: "u-pane-body" });
    page.appendChild(body);
    this.note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    page.appendChild(this.note);
    fill(host, page);
    if (this.pendingNote) {
      setNote(this.note, this.pendingNote.text, this.pendingNote.kind);
      this.pendingNote = null;
    }
    if (this.view === "detail" && this.current) await this.renderDetail(body);
    else if (this.view === "change" && this.current) await this.renderChange(body);
    else if (this.view === "join" && this.current) await this.renderJoin(body);
    else await this.renderList(body);
  }

  /** 列表 / 素材视图的标题带：入口动作（导入、从样例开始）放这里，不和分区同排 */
  private listHead(): HTMLElement {
    return pageHead("世界与素材", "世界、设定、角色卡和版本都在这", [
      button("导入已有内容…", () => void this.importFlow()),
      primary("从样例开始", () => this.ctx.navigate({ pane: "onboarding", sub: "sample" })),
    ]);
  }

  /**
   * 详情类视图的标题带：标题是当前世界的名字（正文不再重复页面名，也不再多套一层盒子标题）。
   * 「返回」降级成一枚安静的胶囊放在动作位 —— 它是导航，不是任务动作，以前和「联络角色／在此写作」
   * 并排，一屏里出现过两个返回目标（2026-10-08 视觉体系审查）。
   * 去处固定为「上一级视图」：详情 → 世界列表，变化 / 加入角色 → 世界详情。
   */
  private viewHead(): HTMLElement {
    const name = String(this.current?.name ?? "世界");
    const nested = this.view === "change" || this.view === "join";
    const back = anchors(
      [
        {
          label: nested ? "返回世界详情" : "返回世界列表",
          onSelect: () => {
            this.view = nested ? "detail" : "list";
            void this.rerender();
          },
        },
      ],
      "返回",
    );
    if (this.view === "change") return pageHead("尝试世界变化", `在「${name}」的当前局势内改变事实`, [back]);
    if (this.view === "join") {
      return pageHead(`给「${name}」加入角色`, "四步：目标线 → 确认角色卡 → 加入时间与说明 → 确认", [back]);
    }
    return pageHead(name, "这个世界的设定、角色、世界线与版本都在这一页", [back]);
  }

  /**
   * 工具带：分区切换。「我的世界」/「世界设定 / 角色卡」用下划线选中态，
   * 与动作按钮、锚点胶囊分开（2026-10-08 视觉体系审查：一套外观以前对应三种交互）。
   * 入口动作在标题带的动作位上，不再和分区同排。
   */
  private toolsBar(): HTMLElement {
    return tools(
      [
        {
          label: "我的世界",
          current: this.tab === "worlds",
          onSelect: () => {
            this.tab = "worlds";
            this.view = "list";
            void this.rerender();
          },
        },
        {
          label: "世界设定 / 角色卡",
          current: this.tab === "assets",
          onSelect: () => {
            this.tab = "assets";
            void this.renderAssetsInline();
          },
        },
      ],
      "世界与素材分区",
    );
  }

  /** 写一条会活过这次重渲染的结果说明 */
  private flash(text: string, kind: "ok" | "bad" | "pending" | "muted" = "ok"): void {
    this.pendingNote = { text, kind };
    setNote(this.note, text, kind);
  }

  /**
   * 重画当前这一格：在「世界设定 / 角色卡」分栏里点确认 / 丢弃之后必须还留在这栏，
   * 不能把人弹回「我的世界」列表（评审 P1）——所以按 tab 分流。
   */
  private async rerender(): Promise<void> {
    if (this.tab === "assets") {
      await this.renderAssetsInline();
      return;
    }
    const host = document.querySelector("#u-main") as HTMLElement | null;
    if (host) await this.render(host);
  }

  /* ---------------------------------------------------------------- 我的世界 */

  private async renderList(host: HTMLElement): Promise<void> {
    this.tab = "worlds";
    const instances = this.ctx.instances();
    if (!instances.length) {
      host.appendChild(
        section(
          "这里保存你的世界和创作材料",
          // 「从样例开始」在标题带的动作位上：同一屏里不做第二个同样的主操作
          paragraph("还没有世界。可以从本页上方的「从样例开始」拿一个样例，或创建自己的世界。"),
          el(
            "div",
            { class: "u-row" },
            primary("创建自己的世界", () => this.ctx.navigate({ pane: "create" })),
            button("导入已有内容", () => void this.importFlow()),
          ),
        ),
      );
      return;
    }
    const rows = await Promise.all(
      instances.map(async (instance) => {
        const info = await this.ctx.api.instanceInfo(instance.id);
        const timelines = (info.timelines as Json[]) ?? [];
        const characters = (info.characters as Json[]) ?? [];
        const running = await Promise.all(
          timelines.map(async (timeline) => {
            try {
              const clock = await this.ctx.api.clock(instance.id, String(timeline.id));
              return String((clock.clock as Json)?.state ?? "");
            } catch {
              return "";
            }
          }),
        );
        return { instance, timelines, characters, running };
      }),
    );
    const table = el("table", { class: "u-table" });
    table.appendChild(
      el(
        "thead",
        {},
        el(
          "tr",
          {},
          el("th", { text: "名称" }),
          el("th", { text: "来自哪个设定" }),
          el("th", { text: "世界线" }),
          el("th", { text: "状态" }),
          el("th", { text: "创建时间" }),
          el("th", { text: "操作" }),
        ),
      ),
    );
    const body = el("tbody", {});
    for (const row of rows) {
      const state = worldStateText(row.timelines, row.running);
      const tr = el("tr", {});
      tr.appendChild(el("td", { text: row.instance.name }));
      tr.appendChild(el("td", { text: String(row.instance.original_name ?? "") }));
      tr.appendChild(el("td", { text: `${row.timelines.length} 条 / ${row.characters.length} 位角色` }));
      tr.appendChild(
        el(
          "td",
          {},
          state
            ? chip(state.text, state.kind)
            : chip("还没有世界线", "pending"),
        ),
      );
      tr.appendChild(el("td", { text: stamp(Number(row.instance.created_at ?? 0)) }));
      tr.appendChild(
        el(
          "td",
          {},
          el(
            "div",
            { class: "u-row" },
            link("打开", () => this.openDetail(row.instance)),
            link("重命名", () => void this.rename(row.instance)),
            link("导出", () => void this.exportInstance(row.instance)),
            // 危险动作要和上面三个区分开：以前四颗同色同下划线、并排 38px 高（2026-10-08 审计 P1-2）
            link("删除", () => void this.deleteInstance(row.instance), "u-link u-link-danger"),
          ),
        ),
      );
      body.appendChild(tr);
    }
    table.appendChild(body);
    // 分区名已经是「我的世界」：卡片的标题再说一遍是重复，这里换成带条数的说法
    host.appendChild(section(`全部世界（${rows.length} 个）`, table));
  }

  private async rename(instance: InstanceEntry): Promise<void> {
    const input = el("input", { class: "u-input", value: instance.name }) as HTMLInputElement;
    const modal = dialog("重命名世界", [field("新名称", input)], [
      {
        label: "重命名",
        primary: true,
        run: () => {
          void (async () => {
            try {
              await this.ctx.api.renameInstance(instance.id, input.value.trim());
              this.flash(`已重命名为「${input.value.trim()}」`);
              await this.rerender();
            } catch (error) {
              setNote(this.note, uiError(error, { module: "世界与素材", action: "重命名世界" }).message, "bad");
            }
          })();
        },
      },
      { label: "取消", run: () => undefined },
    ]);
    document.body.appendChild(modal.node);
  }

  private async exportInstance(instance: InstanceEntry): Promise<void> {
    try {
      const safe = instance.name.replace(/[^\w\u4e00-\u9fa5-]/g, "_");
      const result = await this.ctx.api.exportInstance(instance.id, `${safe}.isekai.json`);
      setNote(this.note, `已导出 ${String(result.manifest ? safe : safe)} 存档（可在「导入」里回来）`, "ok");
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界与素材", action: "导出世界" }).message, "bad");
    }
  }

  private async deleteInstance(instance: InstanceEntry): Promise<void> {
    const input = el("input", { class: "u-input", placeholder: instance.name }) as HTMLInputElement;
    const modal = dialog(
      `删除世界「${instance.name}」？`,
      [
        paragraph("删除后这个世界的对话、世界线、记忆与草稿都会消失，不能撤销。"),
        paragraph("保留：世界设定、角色卡与导出件不受影响。", "u-hint"),
        field(`键入名称确认（${instance.name}）`, input),
      ],
      [
        {
          label: "删除这个世界",
          run: () => {
            void (async () => {
              if (input.value.trim() !== instance.name) {
                setNote(this.note, "名称没有对上，删除已取消", "bad");
                return;
              }
              try {
                await this.ctx.api.deleteInstance(instance.id, true);
                this.flash(`已删除「${instance.name}」`);
                this.view = "list";
                this.current = null;
                await this.rerender();
              } catch (error) {
                setNote(this.note, uiError(error, { module: "世界与素材", action: "删除世界" }).message, "bad");
              }
            })();
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  /* ---------------------------------------------------------------- 世界详情 */

  private async renderDetail(host: HTMLElement): Promise<void> {
    this.tab = "detail";
    const instance = this.current;
    if (!instance) {
      this.view = "list";
      await this.renderList(host);
      return;
    }
    const info = await this.ctx.api.instanceInfo(instance.id);
    // 兼容性以核心现算的这一份为准（instance.info 还带原因说明，列表行上没有）
    const meta = ((info.instance as Json) ?? {}) as Json;
    const timelines = (info.timelines as Json[]) ?? [];
    this.timelinesCache = timelines;
    const characters = (info.characters as Json[]) ?? [];
    const commits = (info.commits as Json[]) ?? [];
    // 进工作区时带的世界落点：第一条未归档的线；没有未归档的就退到第一条（§3.3）
    const firstTimelineId = String(
      timelines.find((item) => String(item.state) !== "archived")?.id ?? timelines[0]?.id ?? "",
    );
    // ① 概况：这是哪来的、能不能用、怎么进去。四块各自一个一级分区（panel），
    // 每区只留一个明确动作；以前 22 颗按钮平铺在 5 个同重量盒子里（2026-10-08 视觉体系审查）
    host.appendChild(
      panel(
        "概况",
        facts([
          ["来源设定", String(meta.original_name ?? instance.original_name ?? "")],
          // 内部枚举翻人话：原值进下面的「技术详情」
          ["兼容性", compatibilityText(meta.compatibility ?? instance.compatibility)],
          ["创建时间", stamp(Number(meta.created_at ?? instance.created_at ?? 0))],
          ["里面有什么", `${timelines.length} 条世界线 · ${characters.length} 位角色`],
        ]),
        // 任务动作排成一行且主次分明：联络是主操作，其余是同一批任务的次要入口
        el(
          "div",
          { class: "u-row" },
          primary("联络角色", () => this.openWorkspace("contact", instance, firstTimelineId)),
          button("在此写作", () => this.openWorkspace("writing", instance, firstTimelineId)),
          button("在此跑团", () => this.openWorkspace("trpg", instance, firstTimelineId)),
          button("尝试世界变化…", () => {
            this.view = "change";
            void this.rerender();
          }),
        ),
        el(
          "div",
          { class: "u-row" },
          button("打开创作目录", () => void openDir("packages", this.ctx.api)),
          button("打开数据目录", () => void openDir("data", this.ctx.api)),
        ),
        techDetails([
          ["兼容性（内部值）", String(meta.compatibility ?? instance.compatibility ?? "")],
          ["兼容性说明", String(meta.compatibility_note ?? "")],
          ["世界的内部标识", instance.id],
        ]),
      ),
    );
    // ② 角色
    host.appendChild(
      panel(
        "角色（只列公开身份）",
        bulletList(
          characters.map((item) => `${String(item.name ?? "")}${item.occupation ? ` · ${String(item.occupation)}` : ""}`),
          "u-list",
        ),
        el(
          "div",
          { class: "u-row" },
          button("加入角色…", () => this.openJoin()),
        ),
      ),
    );
    // ③ 世界线 ④ 版本
    host.appendChild(this.timelineSection(timelines));
    host.appendChild(this.versionSection(timelines, commits, Number((info.calendar as Json | undefined)?.day_seconds ?? 86400)));
  }

  /** 打开世界详情：切到详情视图，并记一条「最近使用」（§3.4：只存名称、身份、访问时间） */
  private openDetail(instance: InstanceEntry): void {
    this.current = instance;
    this.view = "detail";
    this.rememberWorld(instance);
    void this.rerender();
  }

  private rememberWorld(instance: InstanceEntry | null): void {
    if (!instance) return;
    this.ctx.rememberRecent({ pane: "worlds", label: `世界 · ${instance.name}`, key: `worlds:${instance.id}` });
  }

  /**
   * 从世界详情进三个工作区：先把「这个世界 + 第一条未归档时间线」写进目标工作区自己的选择（§3.3），
   * 目标页挂载时读到的就是这个世界；写入失败由 setPrefs 统一提示，不挡导航。
   */
  private openWorkspace(pane: "contact" | "writing" | "trpg", instance: InstanceEntry, timelineId: string): void {
    if (pane === "contact") {
      void this.ctx.setPrefs({
        "sel.contact": {
          instance_id: instance.id,
          timeline_id: timelineId,
          timeline_name: "",
          character_id: "",
          character_name: "",
        },
      });
    } else if (pane === "writing") {
      void this.ctx.setPrefs({
        "sel.writing": {
          instance_id: instance.id,
          timeline_id: timelineId,
          timeline_name: "",
          outline_id: "",
          outline_name: "",
        },
      });
    } else {
      void this.ctx.setPrefs({
        "sel.trpg": { instance_id: instance.id, timeline_id: timelineId, campaign_id: "", campaign_name: "" },
      });
    }
    this.ctx.navigate({ pane });
  }

  private timelineSection(timelines: Json[]): HTMLElement {
    const rows = el("div", { class: "u-rows" });
    const instanceId = this.current?.id ?? "";
    // 已归档的线默认藏起来：这里给一个不必先展开就能把它找回来的入口（归档不是终点）
    const archived = timelines.filter((item) => String(item.state) === "archived");
    const visible = this.showArchived ? timelines : timelines.filter((item) => String(item.state) !== "archived");
    if (!visible.length) rows.appendChild(el("p", { class: "u-hint", text: "没有可显示的世界线（已归档的可以点下面的开关查看）。" }));
    for (const timeline of visible) {
      const id = String(timeline.id);
      const state = String(timeline.state ?? "");
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: `${String(timeline.name ?? id)}` }));
      row.appendChild(chip(STATE_TEXT[state] ?? state, state === "active" ? "ok" : state === "archived" ? "muted" : "pending"));
      if (state !== "archived") {
        row.appendChild(
          button(state === "active" ? "暂停" : "启动", () => {
            void (async () => {
              try {
                if (state === "active") await this.ctx.api.freeze(instanceId, id);
                else await this.ctx.api.activate(instanceId, id);
                this.flash(state === "active" ? "已暂停这条线（暂停不等于退出程序）" : "已启动这条线");
                await this.rerender();
              } catch (error) {
                setNote(this.note, uiError(error, { module: "世界线", action: "运行 / 暂停" }).message, "bad");
              }
            })();
          }),
        );
        row.appendChild(button("改名", () => void this.renameTimeline(id, String(timeline.name ?? ""))));
        row.appendChild(button("归档…", () => void this.archiveTimeline(id, String(timeline.name ?? id))));
      }
      // 归档后可逆：核心没有「取消归档」这个 op，但 activate 会把状态写回 active（归档只是先冻结再置 archived），
      // 所以「恢复这条线」走的就是启动 —— 用了已存在的 op，没有新增协议
      if (state === "archived") row.appendChild(button("恢复这条线…", () => void this.unarchiveTimeline(id, String(timeline.name ?? id))));
      row.appendChild(button("删除…", () => void this.deleteTimeline(id, String(timeline.name ?? id))));
      rows.appendChild(row);
    }
    const rate = el("input", { class: "u-input u-input-narrow", type: "number", min: "1", value: "1" }) as HTMLInputElement;
    // 数字看不出多快：旁边常驻一句人话换算（跟着输入走）
    const rateNote = el("p", { class: "u-hint", id: "u-rate-note", text: rateText(1) });
    // 速度作用在一条具体的线上：不写名字，用户不知道改的是哪条（评审 P2）
    const rateTarget = timelines.find((item) => String(item.state) === "active") ?? timelines[0];
    const rateTargetName = String(rateTarget?.name ?? "") || (rateTarget ? "第一条世界线" : "（还没有世界线）");
    rate.addEventListener("input", () => {
      rateNote.textContent = rateText(Number(rate.value || 1));
    });
    return panel(
      "世界线",
      paragraph("同一个世界可以分出多条世界线：它们共享过去，之后各走各的，不会互相覆盖。", "u-hint"),
      rows,
      el(
        "div",
        { class: "u-row" },
        el("span", { class: "u-hint", text: `只改「${rateTargetName}」的速度（世界秒 / 现实秒）：` }),
        rate,
        button(`设为「${rateTargetName}」的速度`, () => {
          void (async () => {
            try {
              await this.ctx.api.setRate(instanceId, String(rateTarget?.id ?? ""), Number(rate.value || 1));
              this.flash(`已把「${rateTargetName}」的速度设为 ${Number(rate.value || 1)}（生效以世界钟为准）`);
            } catch (error) {
              setNote(this.note, uiError(error, { module: "世界线", action: "设置速度" }).message, "bad");
            }
          })();
        }),
        archived.length || this.showArchived
          ? button(this.showArchived ? `隐藏已归档（${archived.length} 条）` : `显示已归档（${archived.length} 条）`, () => {
              this.showArchived = !this.showArchived;
              void this.rerender();
            })
          : null,
      ),
      rateNote,
      paragraph("关闭窗口只是收起到托盘，世界会继续运行；想停下故事进展请点「暂停」。", "u-hint"),
    );
  }

  /** 删除一条线（§四 / §9.2）：说明影响 + 键入名称确认；最后一条线删不掉时给实际原因 */
  private async deleteTimeline(timelineId: string, name: string): Promise<void> {
    const instanceId = this.current?.id ?? "";
    const input = el("input", { class: "u-input", placeholder: name }) as HTMLInputElement;
    const note = el("p", { class: "u-note" });
    const body = el(
      "div",
      {},
      paragraph("删除这条世界线：它的会话、记忆、版本点与派生素材一起消失，不能撤销。"),
      paragraph("不受影响：世界设定、角色卡、其他世界线，以及被其他线引用的版本点。", "u-hint"),
      field(`键入名称确认（${name}）`, input),
      note,
    );
    const modal = dialog(`删除世界线「${name}」？`, [body], [
      {
        label: "删除这条线",
        run: () => {
          void (async () => {
            if (input.value.trim() !== name) {
              setNote(this.note, "名称没有对上，删除已取消", "bad");
              return;
            }
            try {
              await this.ctx.api.deleteTimeline(instanceId, timelineId, true);
              this.flash(`已删除世界线「${name}」`);
              await this.rerender();
            } catch (error) {
              // 最后一条线删不掉：按核心给的实际原因说，不笼统报“失败”
              setNote(this.note, uiError(error, { module: "世界线", action: "删除" }).message, "bad");
            }
          })();
        },
      },
      { label: "取消", run: () => undefined },
    ]);
    document.body.appendChild(modal.node);
  }

  /** 尝试世界变化（§9.3）：描述 → 草案（不改世界）→ 确认后从来源版本另开一条新线 */
  private async renderChange(host: HTMLElement): Promise<void> {
    const instance = this.current;
    if (!instance) {
      this.view = "list";
      await this.renderList(host);
      return;
    }
    const info = await this.ctx.api.instanceInfo(instance.id);
    const timelines = (info.timelines as Json[]) ?? [];
    const timeline = timelines.find((item) => String(item.state) !== "archived") ?? timelines[0];
    const timelineId = String(timeline?.id ?? "");
    let targets: Json = {};
    try {
      targets = await this.ctx.api.eventTargets(instance.id, timelineId);
    } catch (error) {
      host.appendChild(
        errorCard(
          uiError(error, {
            module: "尝试世界变化",
            action: "读取可选对象",
            done: "没有改动任何数据",
            unknown: "这次读取是否成功",
          }),
          [{ label: "重试读取", run: () => void this.rerender() }],
        ),
      );
      return;
    }
    const groups = (targets.groups as Record<string, Json[]>) ?? {};
    const kindLabels = new Map<string, string>(
      ((targets.effect_kinds as Json[]) ?? []).map((item) => [String(item.id), String(item.label)]),
    );
    const targetLabels = new Map<string, string>();
    for (const items of Object.values(groups)) {
      for (const item of items) targetLabels.set(String(item.id), String(item.label));
    }

    const intent = el("textarea", { class: "u-textarea", id: "u-change-intent", rows: "3", placeholder: "想改变的局势，一句话说清（会写进这条线的记录）" }) as HTMLTextAreaElement;
    const targetSelect = el("select", { class: "u-input", id: "u-change-target" }) as HTMLSelectElement;
    const groupNames: Record<string, string> = {
      character: "角色",
      entity: "地点与实体",
      environment: "环境类型",
      office: "制度职位",
      custom: "文化惯例",
      channel: "信息来源",
    };
    for (const [kind, items] of Object.entries(groups)) {
      if (!items.length) continue;
      const box = el("optgroup", { label: groupNames[kind] ?? kind });
      for (const item of items) box.appendChild(el("option", { value: String(item.id), text: String(item.label) }));
      targetSelect.appendChild(box);
    }
    const kindSelect = el("select", { class: "u-input", id: "u-change-kind" }) as HTMLSelectElement;
    for (const item of (targets.effect_kinds as Json[]) ?? []) {
      kindSelect.appendChild(el("option", { value: String(item.id), text: `${String(item.label)}（${String(item.id)}）` }));
    }
    const value = el("input", { class: "u-input", id: "u-change-value", placeholder: "新状态，如：堤上事务缠身，这几日走不开" }) as HTMLInputElement;
    const whenSelect = el("select", { class: "u-input" }) as HTMLSelectElement;
    whenSelect.appendChild(el("option", { value: "now", text: "现在（来源点已完成的那一刻）" }));
    whenSelect.appendChild(el("option", { value: "scheduled", text: "预约到以后的世界时刻" }));
    const atWorld = el("input", { class: "u-input u-input-narrow", type: "number", min: "0", value: String(Number(targets.world_seconds ?? 0)) }) as HTMLInputElement;
    atWorld.hidden = true;
    whenSelect.addEventListener("change", () => {
      atWorld.hidden = whenSelect.value !== "scheduled";
    });
    const expirySelect = el("select", { class: "u-input" }) as HTMLSelectElement;
    expirySelect.appendChild(el("option", { value: "with_cause", text: "随原因解除（跨日后自然结束）" }));
    expirySelect.appendChild(el("option", { value: "natural_recovery", text: "自行恢复（要写清恢复条件）" }));
    expirySelect.appendChild(el("option", { value: "until_cleared", text: "保留到被明确解除" }));
    const recovery = el("input", { class: "u-input", placeholder: "恢复条件，如：潮水退去" }) as HTMLInputElement;
    recovery.hidden = true;
    expirySelect.addEventListener("change", () => {
      recovery.hidden = expirySelect.value !== "natural_recovery";
    });
    const draftNote = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const result = el("div", {});
    const name = el("input", { class: "u-input", id: "u-change-name", placeholder: "新世界线名称（可留空）" }) as HTMLInputElement;
    let draftId = "";

    const payload = (): Json => {
      const effect: Json = {
        kind: kindSelect.value,
        target: targetSelect.value,
        expiry: expirySelect.value,
      };
      if (value.value.trim()) effect.value = value.value.trim();
      if (expirySelect.value === "natural_recovery" && recovery.value.trim()) effect.recovery = recovery.value.trim();
      const body: Json = { intent: intent.value.trim(), when: whenSelect.value, effects: [effect] };
      if (whenSelect.value === "scheduled") body.at_world = Number(atWorld.value || 0);
      return body;
    };

    const viewDraft = async (): Promise<void> => {
      fill(result);
      draftId = "";
      if (!intent.value.trim()) {
        setNote(draftNote, "先写一句「想改变什么」", "bad");
        return;
      }
      setNote(draftNote, "正在生成草案（只翻译与校验，不改世界）…", "pending");
      try {
        const outcome = await this.ctx.api.eventDraft(instance.id, timelineId, payload());
        if (outcome.accepted !== true) {
          setNote(draftNote, `这条变化目前无法表达成受支持的事件：${String(outcome.reason ?? "原因未给出")}`, "bad");
          return;
        }
        const draft = (outcome.draft as Json) ?? {};
        draftId = String(draft.draft_id ?? "");
        const effects = (draft.effects as Json[]) ?? [];
        fill(
          result,
          facts([
            ["意图", String(draft.intent ?? "")],
            ["生效", String(draft.when) === "scheduled" ? `预约到世界时刻 ${Number(draft.at_world ?? 0)}` : "现在"],
          ]),
          techDetails([["这份草案的内部编号", draftId]]),
          // 一条变化 = 什么变化 · 作用到谁 · 变成什么；串成一行比一句话好读
          el(
            "div",
            { class: "u-rows" },
            ...effects.map((item) =>
              el(
                "div",
                { class: "u-row u-row-wrap" },
                chip(kindLabels.get(String(item.kind)) ?? String(item.kind), "muted"),
                el("span", { class: "u-hint", text: "作用到" }),
                chip(targetLabels.get(String(item.target)) ?? String(item.target), "muted"),
                item.value ? el("span", { class: "u-hint", text: "→" }) : null,
                item.value ? chip(String(item.value), "ok") : null,
                el("span", { class: "u-hint", text: EXPIRY_TEXT[String(item.expiry)] ?? String(item.expiry) }),
              ),
            ),
          ),
          flowRail(
            [
              { label: "草案已生成", hint: "此刻世界没有任何变化" },
              { label: "等你确认", hint: "确认后从来源版本另开一条新线" },
              { label: "新线（暂停）", hint: "原线保持原样；要试演先启动那条新线" },
            ],
            1,
          ),
          paragraph("确认后会从这条线的来源版本另开一条新线，新线默认暂停；原线保持原样。", "u-hint"),
          field("新世界线名称", name),
          el(
            "div",
            { class: "u-row" },
            primary("确认并新建世界线", () => void confirm()),
          ),
        );
        setNote(draftNote, "草案已生成：确认前世界没有任何变化", "ok");
      } catch (error) {
        const info = uiError(error, { module: "尝试世界变化", action: "生成草案", done: "没有改动世界" });
        setNote(draftNote, info.message, "bad");
        result.appendChild(errorCard(info, [{ label: "重新生成草案", run: () => void viewDraft() }]));
      }
    };

    const confirm = async (): Promise<void> => {
      if (!draftId) return;
      setNote(draftNote, "正在新建世界线…", "pending");
      try {
        const done = await this.ctx.api.eventConfirm(instance.id, draftId, name.value.trim());
        const lineId = String(done.timeline_id ?? "");
        setNote(
          draftNote,
          `已建立新线（${lineId.slice(-6)}，暂停中）：原线保留，要试演先在上面「启动」这条新线`,
          "ok",
        );
        fill(result);
        this.view = "detail";
        this.flash(`已建立新线（${lineId.slice(-6)}，暂停中）：原线保留，要试演先在上面「启动」这条新线`);
        await this.rerender();
      } catch (error) {
        const info = uiError(error, {
          module: "尝试世界变化",
          action: "确认草案",
          done: "草案仍留着，可以重新确认",
          unknown: "新世界线是否已经建立",
        });
        result.appendChild(errorCard(info, [{ label: "重新确认", run: () => void confirm() }]));
      }
    };

    host.appendChild(
      // 标题已经在标题带上：正文这一块只分组，不再重复一遍「尝试世界变化」
      panel(
        "",
        paragraph(
          "只在当前局势内改变事实：不能改写过去、也不能改世界的基本设定（那些要改世界设定并新建世界）。表单里的对象都来自这个世界已经登记的内容。",
        ),
        field("想改变什么", intent),
        field("改变对象", targetSelect),
        field("变化种类", kindSelect),
        field("新状态", value),
        field("何时生效", whenSelect),
        field("世界时刻（预约用）", atWorld),
        field("持续到何时", expirySelect),
        field("恢复条件（自行恢复用）", recovery),
        el(
          "div",
          { class: "u-row" },
          // 「返回世界详情」在标题带上（导航只有一处），这里只留这一步的动作
          primary("查看草案", () => void viewDraft()),
        ),
        draftNote,
        result,
      ),
    );
  }

  /* ------------------------------------------------------------ 加入角色（§5.4） */

  /** 进向导：固定本次请求标识，选默认目标线（第一条未归档的线） */
  private openJoin(): void {
    this.view = "join";
    this.joinStep = "timeline";
    this.joinCardFile = "";
    this.joinNote = "";
    this.joinTimelineId = "";
    this.joinRequestId = newRequestId("card-add");
    void this.rerender();
  }

  /** 同一次向导里的重试共用同一个请求标识：重复提交由核心复用原结果（§5.4） */
  private ensureJoinRequestId(): string {
    if (!this.joinRequestId) this.joinRequestId = newRequestId("card-add");
    return this.joinRequestId;
  }

  private async renderJoin(host: HTMLElement): Promise<void> {
    const instance = this.current;
    if (!instance) {
      this.view = "list";
      await this.renderList(host);
      return;
    }
    const info = await this.ctx.api.instanceInfo(instance.id);
    const timelines = (info.timelines as Json[]) ?? [];
    // 详情页正在看的就是第一条线：向导默认落在它上面；换了目标线要重新审卡（审定结论跟着线走）
    const known = timelines.find((item) => String(item.id) === this.joinTimelineId);
    const fallback = timelines.find((item) => String(item.state) !== "archived") ?? timelines[0];
    this.joinTimelineId = String(known?.id ?? fallback?.id ?? "");
    const timeline = timelines.find((item) => String(item.id) === this.joinTimelineId) ?? timelines[0] ?? null;
    const timelineName = String(timeline?.name ?? "—");
    const timelineState = String(timeline?.state ?? "");
    const joined = new Set(((info.characters as Json[]) ?? []).map((item) => String(item.card_id ?? "")));
    const cardNote = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    let cards: Json[] = [];
    let cardsError: unknown = null;
    try {
      cards = ((await this.ctx.api.cards()).cards as Json[]) ?? [];
    } catch (error) {
      cardsError = error;
    }
    // 卡片文件不带稳定标识：逐张读出来核对「这张卡的角色是否已经有定义」（世界卡片列表只给文件名与确认状态）
    const cardIds = new Map<string, string>();
    if (!cardsError) {
      for (const item of cards.filter((entry) => Boolean(entry.confirmed))) {
        const file = String(item.file ?? "");
        if (!file) continue;
        try {
          const loaded = (await this.ctx.api.cardLoad(file)).card as Json;
          cardIds.set(file, String((loaded?.meta as Json | undefined)?.card_id ?? ""));
        } catch {
          cardIds.set(file, ""); // 读不出来的卡交给核心在校验时给实际原因
        }
      }
    }
    // 加入时间固定成「当前最后完成时刻」：能拿到世界钟就把那一刻写出来，拿不到只留说明
    let joinedWorld: number | null = null;
    let worldLabel = "";
    let worldError = false;
    if (timeline) {
      try {
        const clock = (await this.ctx.api.clock(instance.id, String(timeline.id))).clock as Json;
        joinedWorld = Number(clock?.processed_world ?? 0);
        worldLabel = String(clock?.label ?? "");
      } catch {
        worldError = true;
      }
    }
    const worldText = worldError
      ? "以这条线当前最后完成的时刻为准（此刻没读到世界钟，数值确认后由核心定）"
      : joinedWorld === null
        ? "以这条线当前最后完成的时刻为准"
        : `世界进度 ${joinedWorld}${worldLabel && worldLabel !== "已冻结" ? `（${worldLabel}）` : ""} —— 这条线已经走完的那一刻`;
    const stepBody = el("div", { class: "u-step-body" });
    const nav = this.joinStepper();
    const cardNames = new Map<string, string>(
      cards.map((item) => [String(item.file ?? ""), String(item.name ?? "")]),
    );
    const draw = (): void => {
      fill(stepBody);
      if (!timelines.length) {
        stepBody.appendChild(paragraph("这个世界还没有世界线：先在详情页建一条，再加入角色。", "u-hint"));
        return;
      }
      if (this.joinStep === "timeline") this.renderJoinTimeline(stepBody, timelines);
      else if (this.joinStep === "card") this.renderJoinCard(stepBody, cards, cardsError, cardIds, joined, cardNote);
      else if (this.joinStep === "when") this.renderJoinWhen(stepBody, timelineName, worldText, timelineState === "active");
      else this.renderJoinReview(stepBody, instance.name, timelineName, worldText, joinedWorld, cardNote, cardNames.get(this.joinCardFile) ?? "");
    };
    fill(
      host,
      // 标题带已经写着「给「x」加入角色」：正文只做一级分组，不重复标题；
      // 「返回世界详情」也在标题带上（导航一处，步骤内的「上一步」只管向导自己的步子）
      panel(
        "",
        paragraph("加入角色走独立向导：先选目标线，再挑一张已确认的卡，最后确认加入时间与说明。"),
        nav,
        stepBody,
      ),
    );
    draw();
  }

  private joinStepper(): HTMLElement {
    // 与「首次设置」「创建世界」共用同一个进度轨（编号圆点 + 连线 + 三态）。
    // 三处向导以前各写一套「文字 + 短下划线」，看不出走了几分之几，还像坏掉的链接
    // （2026-10-08 视觉体系审查根因 4）。编号由圆点承担，标签里的 ①②③④ 去掉。
    const index = JOIN_STEPS.findIndex((item) => item.id === this.joinStep);
    const rail = flowRail(
      JOIN_STEPS.map((item) => ({ label: item.label })),
      index,
    );
    if (rail) {
      rail.classList.add("u-wizard-rail");
      rail.setAttribute("aria-label", "加入角色步骤");
      return rail;
    }
    // 画不出来（步数越界）时保留文字版：宁可不画图，也不画一张会撒谎的图
    const list = el("ol", { class: "u-steps", "aria-label": "加入角色步骤" });
    JOIN_STEPS.forEach((item, position) => {
      const status = position === index ? "current" : position < index ? "done" : "todo";
      list.appendChild(el("li", { class: `u-step u-step-${status}`, text: item.label }));
    });
    return list;
  }

  /** 第 1 步：目标线（只对选中的那条线生效） */
  private renderJoinTimeline(host: HTMLElement, timelines: Json[]): void {
    const grid = el("div", { class: "u-cards" });
    for (const item of timelines) {
      const id = String(item.id);
      const state = String(item.state ?? "");
      const box = el("div", { class: "u-card" });
      const pick = el("input", { type: "radio", name: "u-join-timeline", value: id }) as HTMLInputElement;
      pick.checked = id === this.joinTimelineId;
      pick.addEventListener("change", () => {
        if (!pick.checked) return;
        // 换了线就重新选卡：上一步的结论属于上一条线
        this.joinTimelineId = id;
        this.joinCardFile = "";
        this.joinStep = "card";
        void this.rerender();
      });
      box.appendChild(el("label", { class: "u-field" }, el("span", { class: "u-field-label", text: String(item.name ?? "这条线") }), pick));
      box.appendChild(chip(STATE_TEXT[state] ?? state, state === "active" ? "ok" : state === "archived" ? "muted" : "pending"));
      // 线标识不进正文（评审第六节）：要认哪条线用名字就够了
      box.appendChild(techDetails([["这条线的内部标识", id]]));
      grid.appendChild(box);
    }
    host.appendChild(section("① 目标线", paragraph("只对选中的这条线生效：其它世界线不因这次加入而改变。"), grid));
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        primary("下一步：确认角色卡", () => {
          if (!this.joinTimelineId) return;
          this.joinStep = "card";
          void this.rerender();
        }),
      ),
    );
  }

  /** 第 2 步：确认角色卡（只列确认过的卡；已在这个世界里的置灰并注明） */
  private renderJoinCard(
    host: HTMLElement,
    cards: Json[],
    cardsError: unknown,
    cardIds: Map<string, string>,
    joined: Set<string>,
    note: HTMLElement,
  ): void {
    if (cardsError) {
      host.appendChild(
        errorCard(
          uiError(cardsError, {
            module: "世界管理",
            action: "读取角色卡",
            done: "没有改动任何数据",
            unknown: "这次读取是否成功",
          }),
          [{ label: "重新读取角色卡", run: () => void this.rerender() }],
        ),
      );
      return;
    }
    const confirmed = cards.filter((item) => Boolean(item.confirmed));
    const unconfirmed = cards.length - confirmed.length;
    const rows = el("div", { class: "u-rows" });
    let selectable = 0;
    for (const item of confirmed) {
      const file = String(item.file ?? "");
      const row = el("div", { class: "u-row-line" });
      const characterId = cardIds.get(file) ?? "";
      // 正文只留名字：文件名与内部标识收进这一行的「技术详情」（评审第六节）
      const technical = techDetails([
        ["素材文件", file],
        ["角色的内部标识", characterId],
      ]);
      // 已有不可变定义的卡不能再补：置灰而不是装成能点（核心也会拒绝同一标识的第二份定义）
      if (characterId && joined.has(characterId)) {
        row.appendChild(el("span", { class: "u-grow", text: String(item.name ?? "这张卡") }));
        row.appendChild(chip("已有定义", "muted"));
        row.appendChild(el("span", { class: "u-hint", text: "这个角色在这个世界里已经有不可变定义：补卡只增加角色，不能借同一标识改写已有角色卡" }));
        row.appendChild(technical);
        rows.appendChild(row);
        continue;
      }
      selectable += 1;
      row.appendChild(el("span", { class: "u-grow", text: String(item.name ?? "这张卡") }));
      if (item.race_id) row.appendChild(chip(String(item.race_id), "muted"));
      row.appendChild(button("选这张", () => {
        this.joinCardFile = file;
        this.joinStep = "when";
        void this.rerender();
      }));
      row.appendChild(technical);
      rows.appendChild(row);
    }
    if (!confirmed.length) {
      rows.appendChild(
        el("p", {
          class: "u-hint",
          text: cards.length ? "还没有确认过的角色卡：先在「世界设定 / 角色卡」里确认一张。" : "创作目录里还没有角色卡。",
        }),
      );
    }
    host.appendChild(
      section(
        "② 确认角色卡",
        paragraph("只能补入确认过的角色卡：还没确认的卡要先在「世界设定 / 角色卡」里点「确认」。"),
        rows,
        paragraph(unconfirmed ? `另有 ${unconfirmed} 张卡还没确认，未列出。` : "所选卡在加入时会按这个世界的设定重新检查。", "u-hint"),
      ),
    );
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        button("上一步", () => {
          this.joinStep = "timeline";
          void this.rerender();
        }),
        selectable
          ? null
          : button("去角色卡列表确认", () => void this.renderAssetsInline()),
        note,
      ),
    );
  }

  /** 第 3 步：加入时间与说明（时间固定为当前最后完成时刻；两个口径分开讲） */
  private renderJoinWhen(host: HTMLElement, timelineName: string, worldText: string, active: boolean): void {
    const textarea = el("textarea", {
      class: "u-textarea",
      id: "u-join-note",
      rows: "3",
      placeholder: "写清这次加入的来由（可留空）",
    }) as HTMLTextAreaElement;
    textarea.value = this.joinNote;
    textarea.addEventListener("input", () => {
      this.joinNote = textarea.value;
    });
    host.appendChild(
      section(
        "③ 加入时间与说明",
        facts([
          ["目标线", timelineName],
          ["加入时间", worldText],
          ["说明", this.joinNote.trim() || "（未填写）"],
        ]),
        field("说明（可留空）", textarea, "会写进这条线的加入记录，不自动编造她与其他人的关系。"),
        paragraph("「曾经存在」：她本来就活在这个世界里，个人史与既成历史照旧，不会因为这次加入被改写。"),
        paragraph(
          "「现在进入可联络集合」：从加入这一刻起，她出现在这条线的角色集合里，可以被选中联络；加入不会启动这条线。",
        ),
        active ? null : paragraph("这条线当前不是运行中：加入锚定它已经走到的那一刻，加入后它仍保持原状态。", "u-hint"),
      ),
    );
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        button("上一步", () => {
          this.joinStep = "card";
          void this.rerender();
        }),
        primary("下一步：确认", () => {
          if (!this.joinCardFile) {
            this.joinStep = "card";
            void this.rerender();
            return;
          }
          this.joinStep = "review";
          void this.rerender();
        }),
      ),
    );
  }

  /** 第 4 步：确认页（目标 / 角色 / 加入说明列全，确认后才写入） */
  private renderJoinReview(
    host: HTMLElement,
    worldName: string,
    timelineName: string,
    worldText: string,
    joinedWorld: number | null,
    note: HTMLElement,
    cardName: string,
  ): void {
    const file = this.joinCardFile;
    const result = el("div", { class: "u-step-body" });
    const submit = primary("确认加入", () => {
      void (async () => {
        if (!file) return;
        submit.disabled = true;
        setNote(note, "正在加入…", "pending");
        try {
          const outcome = await this.ctx.api.call(
            "runtime.card.add",
            {
              instance_id: this.current?.id ?? "",
              timeline_id: this.joinTimelineId,
              card_path: file,
              note: this.joinNote.trim(),
              // 同一次向导重试共用一个 request_id：核心复用原结果，不重复登记
              request_id: this.ensureJoinRequestId(),
              // 本轮固定锚定「当前最后完成时刻」；世界钟读不到就不传，由核心按同一口径取
              ...(joinedWorld === null ? {} : { joined_world: joinedWorld }),
            },
            60000,
          );
          const join = (outcome.join as Json) ?? {};
          const name = String(join.name ?? "");
          const label = String(join.joined_label ?? "");
          this.view = "detail";
          this.joinStep = "timeline";
          this.joinCardFile = "";
          this.joinNote = "";
          this.joinRequestId = "";
          this.flash(`已加入角色${name ? `「${name}」` : ""}${label ? `（${label} 起）` : ""}：只对「${timelineName}」生效`);
          await this.rerender();
        } catch (error) {
          setNote(note, uiError(error, { module: "世界管理", action: "加入角色" }).message, "bad");
          fill(result);
          result.appendChild(
            errorCard(uiError(error, { module: "世界管理", action: "加入角色", done: "角色定义未登记", unknown: "这条线是否已新增成员" }), [
              { label: "重新提交（复用同一请求）", run: () => submit.click() },
            ]),
          );
        } finally {
          submit.disabled = false;
        }
      })();
    });
    fill(
      result,
      facts([
        ["目标世界", worldName],
        ["目标线", timelineName],
        ["角色", cardName || "这张卡"],
        ["加入时间", worldText],
        ["加入说明", this.joinNote.trim() || "（未填写）"],
      ]),
      // 素材文件名与线标识只进「技术详情」：正文留名字（评审第六节）
      techDetails([
        ["素材文件", file],
        ["目标线标识", this.joinTimelineId],
      ]),
      paragraph("确认后只发生三处留痕：这个世界快照里的角色定义、这条线的成员资格、这条线的加入记录；其它世界线不受影响。"),
      paragraph("加入不启动这条线，也不编造她与其他角色的关系。"),
      el(
        "div",
        { class: "u-row" },
        submit,
        button("上一步", () => {
          this.joinStep = "when";
          void this.rerender();
        }),
      ),
      note,
    );
    host.appendChild(section("④ 确认", result));
  }

  private async renameTimeline(timelineId: string, current: string): Promise<void> {
    const input = el("input", { class: "u-input", value: current }) as HTMLInputElement;
    const modal = dialog("世界线名称", [field("新名称", input)], [
      {
        label: "保存名称",
        primary: true,
        run: () => {
          void (async () => {
            try {
              await this.ctx.api.renameTimeline(this.current?.id ?? "", timelineId, input.value.trim());
              this.flash("名称已更新");
              await this.rerender();
            } catch (error) {
              setNote(this.note, uiError(error, { module: "世界线", action: "改名" }).message, "bad");
            }
          })();
        },
      },
      { label: "取消", run: () => undefined },
    ]);
    document.body.appendChild(modal.node);
  }

  /**
   * 归档（评审 P1：原来一键生效、没有回头路）。
   * 归档 = 先暂停、数据保留；要确认一次，是因为它会让这条线从常用列表里消失。
   * 恢复入口在归档行上（见 unarchiveTimeline），所以这里如实说「以后怎么回来」。
   */
  private async archiveTimeline(timelineId: string, name: string): Promise<void> {
    const instanceId = this.current?.id ?? "";
    const note = el("p", { class: "u-note" });
    const modal = dialog(
      `归档世界线「${name}」？`,
      [
        paragraph("归档会先暂停这条线，然后把它从上面的列表里收起来。对话、记忆与版本都保留，不会删除。"),
        paragraph("以后想继续：在「显示已归档」里找到它，点「恢复这条线」就能接着走；也可以从它的任一个版本另开分支。", "u-hint"),
        note,
      ],
      [
        {
          label: "归档这条线",
          primary: true,
          run: async () => {
            try {
              await this.ctx.api.archiveTimeline(instanceId, timelineId);
              this.flash(`已归档「${name}」（数据保留；要接着走可在「显示已归档」里点「恢复这条线」）`);
              await this.rerender();
            } catch (error) {
              setNote(note, uiError(error, { module: "世界线", action: "归档", done: "这条线没有改动" }).message, "bad");
              return false; // 失败不关窗：提示写在窗内才看得见
            }
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  /** 恢复一条已归档的线：核心没有 unarchive，activate 会把状态写回 active（已确认过 store/runtime 语义） */
  private async unarchiveTimeline(timelineId: string, name: string): Promise<void> {
    const instanceId = this.current?.id ?? "";
    const note = el("p", { class: "u-note" });
    const modal = dialog(
      `恢复世界线「${name}」？`,
      [
        paragraph("恢复会把这条线重新启动：它从停下的那一刻接着走，归档期间世界没有推进。"),
        paragraph("如果同时运行的线已达上限，核心会说明上限是多少——那时先暂停一条再回来。", "u-hint"),
        note,
      ],
      [
        {
          label: "恢复并启动这条线",
          primary: true,
          run: async () => {
            try {
              await this.ctx.api.activate(instanceId, timelineId);
              this.flash(`已恢复「${name}」并开始运行`);
              await this.rerender();
            } catch (error) {
              setNote(note, uiError(error, { module: "世界线", action: "恢复已归档的线", done: "这条线仍是归档状态" }).message, "bad");
              return false;
            }
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  private versionSection(timelines: Json[], commits: Json[], daySeconds = 86400): HTMLElement {
    const list = el("ol", { class: "u-list" });
    for (const commit of commits.slice(0, 20)) {
      const line = el("li", {});
      line.dataset.commitRow = String(commit.id ?? "");
      // 版本点属于哪条线要说出来：只有列表时看不出这条记录长在哪条世界线上
      const lineName = timelineNameOf(timelines, String(commit.timeline_id ?? ""));
      line.appendChild(
        el("span", {
          // 裸数字（世界秒）对用户没有意义：换算成世界内日期（评审第四节的「129600000」）
          text: `${stamp(Number(commit.created_at ?? 0))}｜${lineName}｜${worldDayText(Number(commit.moment ?? 0), daySeconds)}｜${String(commit.kind ?? "")}${commit.note ? `｜${String(commit.note)}` : ""}`,
        }),
      );
      const row = el("div", { class: "u-row" });
      row.appendChild(button("从这里另开分支", () => void this.fork(commit)));
      row.appendChild(button("恢复到此版本…", () => void this.restore(commit, daySeconds)));
      line.appendChild(row);
      list.appendChild(line);
    }
    if (!commits.length) list.appendChild(el("li", { class: "u-hint", text: "尚无可用版本：运行中会自动留下版本点，也可以手动保存一个。" }));
    const graph = branchGraph(branchLanes(timelines, commits));
    return panel(
      "版本记录",
      el(
        "div",
        { class: "u-row" },
        primary("保存当前版本", () => void this.saveVersion()),
      ),
      graph,
      graph
        ? paragraph("一条世界线一条泳道，圆点按出现顺序排；虚线是「这条线从哪个版本分出来的」。点圆点会滚到下面那一行。", "u-hint")
        : null,
      list,
      paragraph("版本记录是给这个世界留的进度点；不是整份外部备份（备份在设置里）。", "u-hint"),
    );
  }

  private async saveVersion(): Promise<void> {
    const note = el("input", { class: "u-input", placeholder: "这一版的备注（可留空）" }) as HTMLInputElement;
    const modal = dialog("保存当前版本", [field("备注", note)], [
      {
        label: "保存版本",
        primary: true,
        run: () => {
          void (async () => {
            const timeline = await this.firstTimeline();
            try {
              await this.ctx.api.saveVersion(this.current?.id ?? "", timeline, note.value.trim());
              this.flash("已保存一个版本点");
              await this.rerender();
            } catch (error) {
              setNote(this.note, uiError(error, { module: "版本", action: "保存版本" }).message, "bad");
            }
          })();
        },
      },
      { label: "取消", run: () => undefined },
    ]);
    document.body.appendChild(modal.node);
  }

  private async firstTimeline(): Promise<string> {
    const info = await this.ctx.api.instanceInfo(this.current?.id ?? "");
    const timelines = (info.timelines as Json[]) ?? [];
    return String(timelines[0]?.id ?? "");
  }

  private async fork(commit: Json): Promise<void> {
    const name = el("input", { class: "u-input", placeholder: "新世界线名称" }) as HTMLInputElement;
    const modal = dialog(
      "从这个版本另开分支",
      [
        paragraph("分支继承到这儿的共同过去；原线继续保留，两条线之后互不回流。"),
        field("新线名称", name),
      ],
      [
        {
          label: "建立分支",
          primary: true,
          run: () => {
            void (async () => {
              try {
                await this.ctx.api.forkTimeline(
                  this.current?.id ?? "",
                  String(commit.timeline_id ?? ""),
                  String(commit.id ?? ""),
                  name.value.trim() || `分支 ${stamp(Number(commit.created_at ?? 0))}`,
                );
                this.flash("已建立分支（新线先处于暂停；要试演时再启动它）");
                await this.rerender();
              } catch (error) {
                setNote(this.note, uiError(error, { module: "版本", action: "另开分支" }).message, "bad");
              }
            })();
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  private async restore(commit: Json, daySeconds = 86400): Promise<void> {
    const timelineId = String(commit.timeline_id ?? "");
    const instanceId = this.current?.id ?? "";
    const body = el("div", {});
    const note = el("p", { class: "u-note" });
    // 「当前进展已保留」必须真的落下才算数：keepDone 是 saved=true 的唯一来源
    let keepDone = false;
    const timelineName = timelineNameOf(this.timelinesCache, timelineId);
    const confirmInput = el("input", { class: "u-input", placeholder: timelineName }) as HTMLInputElement;
    // 预读是只读的：读失败只影响这一块，重试就地重读，不必关掉整个对话框
    const previewHost = el("div", {});
    const loadPreview = async (): Promise<void> => {
      fill(previewHost);
      try {
        const preview = await this.ctx.api.storyRestore(instanceId, timelineId, String(commit.id ?? ""), false, false);
        const coverage = (preview.coverage as Json) ?? {};
        previewHost.appendChild(
          facts([
            ["回到的世界内日期", worldDayText(Number(coverage.to_world ?? 0), daySeconds)],
            ["现在", worldDayText(Number(coverage.now_world ?? 0), daySeconds)],
            ["会退回去的一段时间", humanDuration(Math.max(0, Number(coverage.now_world ?? 0) - Number(coverage.to_world ?? 0)))],
            ["这条线已投递的回复总数", String(coverage.delivered_replies ?? 0)],
          ]),
        );
        previewHost.appendChild(paragraph(String(preview.warning ?? "")));
        previewHost.appendChild(paragraph(String(preview.reason ?? ""), "u-hint"));
      } catch (error) {
        previewHost.appendChild(
          errorCard(
            uiError(error, {
              module: "版本",
              action: "恢复到此版本",
              done: "没有改动任何数据",
              unknown: "这次读取是否成功",
            }),
            [{ label: "重试读取", run: () => void loadPreview() }],
          ),
        );
      }
    };
    await loadPreview();
    body.appendChild(previewHost);
    body.appendChild(
      paragraph("恢复是覆盖操作：先保存当前进展（保存为分支或导出），确认页才能体现「当前进展已保留」。", "u-hint"),
    );
    const keepFirst = button("先保存当前进展", () => {
      void (async () => {
        try {
          const timeline = await this.firstTimeline();
          await this.ctx.api.saveVersion(instanceId, timeline, "恢复前保留");
          await this.ctx.api.forkTimeline(instanceId, timeline, String(commit.id ?? ""), `恢复前保留 ${stamp(Date.now() / 1000)}`);
          keepDone = true;
          setNote(note, "已把当前进展保存为保留分支（现在可以执行恢复了）", "ok");
        } catch (error) {
          setNote(note, uiError(error, { module: "版本", action: "保存当前进展", done: "当前进展还没有保存" }).message, "bad");
        }
      })();
    });
    body.appendChild(
      field(
        `键入这条世界线的名字确认（${timelineName}）`,
        confirmInput,
        "覆盖不可撤销：恢复后，这条线里那一时刻之后的进展不再属于它。",
      ),
    );
    body.appendChild(keepFirst);
    body.appendChild(note);
    const modal = dialog("恢复到此版本", [body], [
      {
        label: "覆盖当前线，从此版本继续",
        primary: true,
        run: async () => {
          // 二次确认：键入这条线的名字才放行（和「删除世界」同一套摩擦，别让覆盖比删除还轻）
          if (confirmInput.value.trim() !== timelineName) {
            setNote(note, `名字没有对上：请键入「${timelineName}」再执行`, "bad");
            return false;
          }
          // saved 的语义是对齐 storyRestore 的 saved 参数：只有真的保存过才为 true，
          // 没保存就执行会被核心按「未保存」挡回（状态 waiting），所以这里先拦住并说清原因
          if (!keepDone) {
            setNote(note, "先点「先保存当前进展」：这一步会把现在的进展做成保留分支，恢复才不算白丢", "bad");
            return false;
          }
          // 阶段一：只读预读，确认这一步真的会改世界；拿到覆盖范围再执行
          let coverage: Json = {};
          try {
            const preview = await this.ctx.api.storyRestore(instanceId, timelineId, String(commit.id ?? ""), false, false);
            coverage = (preview.coverage as Json) ?? {};
          } catch (error) {
            setNote(note, uiError(error, { module: "版本", action: "恢复到此版本", done: "没有改动任何数据", unknown: "能不能恢复" }).message, "bad");
            return false;
          }
          // 阶段二：带上 confirm + saved 真正执行；核心拒绝（waiting / rejected）时如实报，不静默
          try {
            const outcome = await this.ctx.api.storyRestore(instanceId, timelineId, String(commit.id ?? ""), true, keepDone);
            const status = String(outcome.status ?? "");
            if (status !== "ok" && status !== "") {
              setNote(note, `核心没有执行这次恢复：${String(outcome.reason ?? "原因未给出")}`, "bad");
              return false;
            }
            this.flash(`「${timelineName}」已回到所选版本（世界内第 ${Number(coverage.to_world ?? 0)} 秒那一刻）；之前投递出去的回复不保证能撤回`);
            await this.rerender();
          } catch (error) {
            setNote(
              note,
              uiError(error, {
                module: "版本",
                action: "恢复到此版本",
                done: keepDone ? "当前进展已保存为保留分支" : "没有保存当前进展",
                unknown: "这条线是否已经回到所选版本",
              }).message,
              "bad",
            );
            return false; // 失败不关窗：用户要看见真实原因
          }
        },
      },
      { label: "取消", run: () => undefined },
    ]);
    document.body.appendChild(modal.node);
  }

  /* ---------------------------------------------------------------- 素材与导入 */

  private async renderAssetsInline(): Promise<void> {
    const host = document.querySelector("#u-main") as HTMLElement | null;
    if (!host) return;
    this.tab = "assets";
    const packages = await this.ctx.api.packages();
    const cards = await this.ctx.api.cards();
    const drafts = await this.ctx.api.draftList("create");
    // 读接口期间用户可能已经点回「我的世界」：这次素材渲染作废，别把列表盖掉
    if (this.tab !== "assets") return;
    const page = el("div", { class: "u-page" });
    page.appendChild(this.listHead());
    page.appendChild(this.toolsBar());
    // 一级分区装三块二级内容：设定 / 角色卡 / 草稿是同一栏里的三类素材
    const assetsPanel = panel("世界设定与角色卡");

    const pkgRows = el("div", { class: "u-rows" });
    for (const item of (packages.packages as Json[]) ?? []) {
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: String(item.name ?? item.file) }));
      row.appendChild(chip(item.valid ? "可用于创建" : "需要检查", item.valid ? "ok" : "bad"));
      if (!item.valid) {
        // 内核给的是点分路径：先翻成人话再说（评审第六节：路径不上屏）
        const problems = ((item.errors as string[]) ?? []).slice(0, 3);
        row.appendChild(
          el(
            "span",
            {
              class: "u-hint",
              text: problems.length
                ? `还差：${problems.map((problem) => describeProblem(problem, sectionsOf({})).text).join("；")}`
                : "还没通过检查",
            },
          ),
        );
      }
      // 编辑已有设定 = 进创作工作区（表单 / 锁定 / 校验定位），不是改文件
      row.appendChild(button("编辑", () => this.ctx.navigate({ pane: "create", sub: `edit:${String(item.file)}` })));
      pkgRows.appendChild(row);
    }
    if (!pkgRows.childElementCount) pkgRows.appendChild(el("p", { class: "u-hint", text: "还没有世界设定。" }));
    assetsPanel.appendChild(
      section(
        "世界设定",
        pkgRows,
        el("div", { class: "u-row" }, primary("新建世界设定（进创作工作区）", () => this.ctx.navigate({ pane: "create" }))),
      ),
    );

    const cardRows = el("div", { class: "u-rows" });
    for (const item of (cards.cards as Json[]) ?? []) {
      const file = String(item.file ?? "");
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: String(item.name ?? file) }));
      row.appendChild(chip(item.confirmed ? "已确认，可用于创建" : "还没确认", item.confirmed ? "ok" : "pending"));
      if (!item.confirmed) {
        row.appendChild(
          button("确认", () => {
            void (async () => {
              try {
                await this.ctx.api.cardConfirm({ card_path: file });
                this.flash("角色卡已确认：可以用于创建");
                await this.rerender();
              } catch (error) {
                setNote(this.note, uiError(error, { module: "角色卡", action: "确认", done: "卡没有改动" }).message, "bad");
              }
            })();
          }),
        );
      }
      cardRows.appendChild(row);
    }
    if (!cardRows.childElementCount) cardRows.appendChild(el("p", { class: "u-hint", text: "还没有角色卡。" }));
    assetsPanel.appendChild(section("角色卡", cardRows));

    const draftRows = el("div", { class: "u-rows" });
    for (const item of (drafts.drafts as Json[]) ?? []) {
      const key = String(item.key ?? "");
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: `${String(item.target || key)}（${stamp(Number(item.updated_at ?? 0))}）` }));
      row.appendChild(chip("未完成", "muted"));
      row.appendChild(button("继续编辑", () => this.ctx.navigate({ pane: "create", sub: `draft:${key}` })));
      row.appendChild(
        button("丢弃…", () => {
          // 丢弃不可撤销，而删除世界要键入名称：这里的摩擦至少要有一次确认（评审 P1）
          const note = el("p", { class: "u-note" });
          const modal = dialog(
            "丢弃这份草稿？",
            [
              paragraph(`丢弃的是「${String(item.target || key)}」这份还没确认的草稿，不能撤销。`),
              paragraph("已经生成的正式世界设定不受影响；只是想再看一眼可以点「继续编辑」。", "u-hint"),
              note,
            ],
            [
              {
                label: "丢弃这份草稿",
                primary: true,
                run: async () => {
                  try {
                    await this.ctx.api.draftDiscard(key);
                    this.flash("已丢弃这份草稿（不能撤销）");
                    await this.rerender();
                  } catch (error) {
                    setNote(note, uiError(error, { module: "草稿", action: "丢弃", done: "草稿还在" }).message, "bad");
                    return false; // 失败不关窗
                  }
                },
              },
              { label: "取消", run: () => undefined },
            ],
          );
          document.body.appendChild(modal.node);
        }),
      );
      draftRows.appendChild(row);
    }
    if (!draftRows.childElementCount) draftRows.appendChild(el("p", { class: "u-hint", text: "没有未完成的设定草稿。" }));
    assetsPanel.appendChild(section("未完成内容（草稿）", draftRows, paragraph("草稿允许未通过校验；确认设定后才成为正式材料。", "u-hint")));
    page.appendChild(assetsPanel);
    // 「从这里开始」只留一个动作：从样例开始与返回列表分别在标题带和工具带上，
    // 同一屏里不重复同一件事（2026-10-08 视觉体系审查的「同一动作出现两次」）
    page.appendChild(
      section(
        "从这里开始",
        el(
          "div",
          { class: "u-row" },
          primary("创建世界", () => this.ctx.navigate({ pane: "create" })),
        ),
        paragraph("创建世界要用确认过（标着「可用于创建」）的设定与角色卡。", "u-hint"),
      ),
    );
    fill(host, page);
  }

  /** 核心的「同名已存在」拒绝：界面据此改问「覆盖 / 改名字」，而不是把一句话丢给用户 */
  private static readonly SAME_NAME_RE = /同名|已存在|already exists/;

  private async importFlow(): Promise<void> {
    try {
      const picked = await invoke<string | null>("pick_file", {
        dir: null,
        title: "选择要导入的文件（世界设定 / 角色卡 / 世界存档）",
        filter: "isekai 文件 (*.json)|*.json|所有文件 (*.*)|*.*",
      });
      if (!picked) {
        setNote(this.note, "已取消导入", "muted");
        return;
      }
      const name = picked.split(/[\\/]/).pop() ?? picked;
      if (name.endsWith(".isekai.json")) {
        const result = await this.ctx.api.importInstance(picked);
        const instance = result.instance as Json;
        this.flash(`已导入为「${String(instance.name ?? "")}」（新的独立副本，默认暂停）`);
        await this.ctx.refresh();
        await this.rerender();
        return;
      }
      let payload: Json;
      try {
        payload = JSON.parse(await invoke<string>("read_text_file", { path: picked })) as Json;
      } catch {
        // 坏文件不把 JSON.parse 的英文错误原样抛出（评审第六节）：只说人话 + 重新选择
        setNote(this.note, "这个文件不是本程序能读的设定或角色卡：请重新选择", "bad");
        const modal = dialog(
          "这个文件读不了",
          [
            paragraph("它不是本程序能读的世界设定或角色卡（可能不是 JSON、也可能选错了文件）。"),
            paragraph("要带进来的文件应该是「导出」出来的世界存档，或创作目录里的设定 / 角色卡。", "u-hint"),
          ],
          [
            { label: "重新选择文件", primary: true, run: () => void this.importFlow() },
            { label: "取消", run: () => undefined },
          ],
        );
        document.body.appendChild(modal.node);
        return;
      }
      if (payload.identity) {
        await this.importCard(picked);
        return;
      }
      if (payload.instance) {
        // 存档文件如果没带 `.isekai.json` 后缀，也按存档导入，不当成设定包
        const result = await this.ctx.api.importInstance(picked);
        const instance = (result.instance as Json) ?? {};
        this.flash(`已导入为「${String(instance.name ?? "")}」（新的独立副本，默认暂停）`);
        await this.ctx.refresh();
        await this.rerender();
        return;
      }
      await this.importPackage(picked);
    } catch (error) {
      setNote(this.note, uiError(error, { module: "导入", action: "导入文件" }).message, "bad");
    }
  }

  /**
   * 导入世界设定：核心在同名时要求显式确认覆盖（world.package.import 的 force），
   * 界面以前不传 force，于是那句要求变成一个走不通的提示（评审 P1）。
   * 这里把拒绝接住，问一次「覆盖同名设定 / 改用新名字」；改名走核心已有的 `name` 参数。
   */
  private async importPackage(picked: string, opts: { force?: boolean; name?: string } = {}): Promise<void> {
    const args: Json = { source_path: picked };
    if (opts.force) args.force = true;
    if (opts.name) args.name = opts.name;
    try {
      const result = await this.ctx.api.call("world.package.import", args);
      const label = String(result.imported ?? result.name ?? "");
      this.flash(
        opts.force && !opts.name
          ? `已覆盖同名世界设定并导入${label ? `「${label}」` : ""}`
          : `已导入世界设定${opts.name ? `并改名为「${opts.name}」` : label ? `「${label}」` : ""}`,
      );
      await this.rerender();
    } catch (error) {
      const info = uiError(error, { module: "导入", action: opts.force ? "覆盖导入世界设定" : "导入世界设定", done: "没有改动任何文件" });
      if (!opts.force && WorldsPane.SAME_NAME_RE.test(info.message)) {
        this.askOverwrite(picked);
        return;
      }
      setNote(this.note, info.message, "bad");
    }
  }

  /** 导入角色卡：同名时同样要一次显式覆盖确认（核查发现界面原来不传 force） */
  private async importCard(picked: string): Promise<void> {
    const packages = await this.ctx.api.packages();
    const options = el("select", { class: "u-input" }) as HTMLSelectElement;
    for (const item of (packages.packages as Json[]) ?? []) {
      options.appendChild(el("option", { value: String(item.file), text: String(item.name ?? item.file) }));
    }
    const note = el("p", { class: "u-note" });
    const send = async (useForce: boolean): Promise<boolean> => {
      try {
        const result = await this.ctx.api.call("world.card.import", {
          source_path: picked,
          package_path: options.value,
          ...(useForce ? { force: true } : {}),
        });
        this.flash(`已导入角色卡「${String(result.name ?? result.imported ?? "")}」（还没确认，需确认后才能用于创建）`);
        await this.rerender();
        return true;
      } catch (error) {
        const info = uiError(error, { module: "导入", action: "导入角色卡", done: "没有改动任何文件" });
        if (!useForce && WorldsPane.SAME_NAME_RE.test(info.message)) {
          setNote(note, "同名角色卡已存在：请点「覆盖同名角色卡」，或换一个世界设定再试", "bad");
          return false;
        }
        setNote(note, info.message, "bad");
        return false;
      }
    };
    const modal = dialog(
      "这张角色卡属于哪个世界设定？",
      [paragraph("角色卡里的信息来源与记载引用要跟着世界设定一起检查。"), field("世界设定", options), note],
      [
        {
          label: "检查并导入",
          primary: true,
          run: () => send(false),
        },
        // 核心要的是「显式确认覆盖」：这一颗专门用来把 force 传下去，不是摆设
        {
          label: "覆盖同名角色卡",
          run: () => send(true),
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  /** 「同名已存在」的一问：覆盖 or 改用新名字（两个分支都真的带 force / name 下去） */
  private askOverwrite(picked: string): void {
    const rename = el("input", { class: "u-input", placeholder: "新名字（留空 = 覆盖同名的那一份）" }) as HTMLInputElement;
    const note = el("p", { class: "u-note" });
    const modal = dialog(
      "同名世界设定已经存在",
      [
        paragraph("创作目录里已经有一份同名的世界设定。你要覆盖它，还是把新导入的改成另一个名字？"),
        paragraph("覆盖会让原来那份设定被这份文件替换；改用新名字则两份都在。", "u-hint"),
        field("换个名字（可选）", rename),
        note,
      ],
      [
        {
          label: "覆盖同名设定",
          primary: true,
          run: async () => {
            await this.importPackage(picked, { force: true });
            return true;
          },
        },
        {
          label: "改用新名字导入",
          run: async () => {
            const value = rename.value.trim();
            if (!value) {
              setNote(note, "先在上面写好新名字，再点这一颗", "bad");
              return false;
            }
            await this.importPackage(picked, { force: true, name: value });
            return true;
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }
}
