/*
 * 辅助写作工作区（USER_INTERFACE_DESIGN §7.1–§7.5）。
 *
 * 一起组织大纲、观察人物、比较推进方案，文字由你决定。
 * 四个分区在同一工作区内切换：大纲 / 当前素材 / 推进建议 / 文字草稿。
 * 三种结果三个动作：以此起草（留作文字）/ 另开分支试演 / 预览世界变化并确认应用到当前线。
 *
 * 两条界面层的硬规矩（§7.3 / §7.5）：
 *   - 候选是候选：只有真实提交结果才显示「已生效」，批准与提交是两步；
 *   - 正文锁定后，新生成永远另起一稿，世界恢复也不会改写它。
 * 跑团（U4）在 `trpg.ts` 里单独实现，这里只负责写作。
 */

import { invoke } from "@tauri-apps/api/core";
import type { AppContext, Pane } from "./app";
import type { Json } from "./api";
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
  paragraph,
  primary,
  section,
  setNote,
  stamp,
} from "./dom";

type Tab = "outline" | "material" | "advice" | "draft";

/** 六类条目：界面说法 + 一句示例（§7.2） */
const LAYERS: Array<[string, string, string]> = [
  ["theme", "主题约束", "例：主题围绕记住与遗忘"],
  ["required_node", "必达节点", "例：她在本章结束前知道那份告警"],
  ["forbidden", "禁止事项", "例：北堤不得再次崩塌（触发就是偏离）"],
  ["character_arc", "角色弧线", "例：她从不信人到愿意托付"],
  ["pacing", "节奏目标", "例：前三章都在北堤附近"],
  ["variable_material", "可变素材", "例：可以用盐价、碑文、旧账本"],
];

const STATUS_TEXT: Record<string, string> = {
  unstarted: "未开始",
  in_progress: "进行中",
  achieved: "已达成",
  deviated: "已偏离",
  abandoned: "已放弃",
};

function refs(value: string): string[] {
  return value.split(/[,，;；\s]+/).map((item) => item.trim()).filter(Boolean);
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
  private busy = false;

  constructor(private readonly ctx: AppContext) {}

  async mount(host: HTMLElement): Promise<void> {
    this.host = host;
    this.note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    fill(host, this.note);
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
      host.appendChild(
        section(
          "还没有世界",
          paragraph("辅助写作挂在一个世界上：先创建，或从样例开始，再回来写。"),
          el(
            "div",
            { class: "u-row" },
            primary("从样例开始", () => this.ctx.navigate({ pane: "onboarding", sub: "sample" })),
            button("创建世界", () => this.ctx.navigate({ pane: "create" })),
          ),
        ),
      );
      return;
    }
    if (!this.instanceId) this.instanceId = instances[0].id;
    try {
      const info = await this.ctx.api.instanceInfo(this.instanceId);
      const timelines = (info.timelines as Json[]) ?? [];
      const characters = (info.characters as Json[]) ?? [];
      if (!timelines.some((item) => String(item.id) === this.timelineId)) {
        this.timelineId = String(timelines[0]?.id ?? "");
      }
      if (!characters.some((item) => String(item.card_id) === this.cardId)) {
        this.cardId = String(characters[0]?.card_id ?? "");
      }
      const outlineList = ((await this.ctx.api.waOutlines()).outlines as Json[]) ?? [];
      if (!outlineList.some((item) => String(item.outline_id ?? item.id) === this.outlineId)) {
        this.outlineId = String(outlineList[0]?.outline_id ?? outlineList[0]?.id ?? "");
      }
      await this.loadState();
      await this.refreshCandidates();
      this.renderHeader(instances, timelines, characters, outlineList);
      this.renderTabs(host);
      if (!this.state) {
        host.appendChild(
          paragraph(
            "这条时间线上还没有绑定大纲：选一份大纲点「绑定到大纲」开始；已经绑定的线会保留各自进度。",
            "u-hint",
          ),
        );
      }
      if (this.tab === "outline") this.renderOutline(host);
      else if (this.tab === "material") await this.renderMaterial(host);
      else if (this.tab === "advice") this.renderAdvice(host);
      else this.renderDraft(host);
    } catch (error) {
      host.appendChild(errorCard(uiError(error, { module: "辅助写作", action: "打开工作区" })));
    }
  }

  private async loadState(): Promise<void> {
    this.state = null;
    this.busy = false;
    if (!this.instanceId || !this.timelineId || !this.outlineId) return;
    try {
      const result = await this.ctx.api.waState(this.instanceId, this.timelineId, this.outlineId);
      this.state = (result.state as Json) ?? null;
    } catch {
      this.busy = true;  // 尚未绑定：由界面如实说明，不当成故障
    }
  }

  private async refreshCandidates(): Promise<void> {
    if (!this.instanceId || !this.timelineId) {
      this.candidates = [];
      return;
    }
    try {
      const result = await this.ctx.api.waState(this.instanceId, this.timelineId, this.outlineId || "");
      this.candidates = ((result.public as Json)?.candidates as Json[]) ?? [];
    } catch {
      this.candidates = [];
    }
  }

  private renderHeader(
    instances: Array<{ id: string; name: string }>,
    timelines: Json[],
    characters: Json[],
    outlines: Json[],
  ): void {
    const host = this.host!;
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
    host.appendChild(
      section(
        `辅助写作 · ${instances.find((item) => item.id === this.instanceId)?.name ?? ""}`,
        paragraph(
          "一起组织大纲、观察人物、比较推进方案，文字由你决定。在跑团里打开时这是「主持准备」，结果默认不发给玩家。",
        ),
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
            "时间线",
            "u-wa-timeline",
            timelines.map((item) => [
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
        el(
          "div",
          { class: "u-row" },
          button("新建大纲…", () => void this.newOutline()),
          button(this.state ? "换观察者 / 章节标签…" : "绑定到大纲", () => void this.bindOutline()),
          this.state ? chip(`已绑定：${String(this.state.outline_name ?? "")}`, "ok") : chip("未绑定", "pending"),
          this.state
            ? paragraph(`章节：${String(this.state.chapter || "（未命名）")}｜评估水位 ${Number(this.state.evaluated_world ?? 0)}`, "u-hint")
            : null,
        ),
      ),
    );
  }

  private renderTabs(host: HTMLElement): void {
    const tabs = el("nav", { class: "u-crumbs", "aria-label": "写作分区" });
    const entries: Array<[Tab, string]> = [
      ["outline", "大纲"],
      ["material", "当前素材"],
      ["advice", "推进建议"],
      ["draft", "文字草稿"],
    ];
    for (const [id, label] of entries) {
      const node = button(label, () => {
        this.tab = id;
        void this.render();
      });
      node.dataset.tab = id;
      if (id === this.tab) {
        node.classList.add("u-nav-active");
        node.setAttribute("aria-current", "page");
      }
      tabs.appendChild(node);
    }
    host.appendChild(tabs);
  }

  /* ------------------------------------------------------------ ① 大纲（§7.2） */

  private renderOutline(host: HTMLElement): void {
    if (!this.state) {
      host.appendChild(el("div", { class: "u-row" }, primary("绑定到大纲", () => void this.bindOutline())));
      return;
    }
    const items = ((this.state.items as Json[]) ?? []).slice();
    const groups = el("div", { class: "u-cards" });
    for (const [layer, label, example] of LAYERS) {
      const mine = items.filter((item) => String(item.layer) === layer);
      const card = el("article", { class: "u-card" });
      card.appendChild(el("h3", { text: `${label}（${mine.length}）` }));
      card.appendChild(paragraph(example, "u-hint"));
      for (const item of mine) {
        const status = String(item.status);
        const row = el("div", { class: "u-row-line" });
        row.appendChild(el("span", { class: "u-grow", text: String(item.title || item.statement || item.id) }));
        row.appendChild(
          chip(
            STATUS_TEXT[status] ?? (this.busy ? status : status),
            status === "achieved" ? "ok" : status === "deviated" || status === "abandoned" ? "bad" : status === "in_progress" ? "pending" : "muted",
          ),
        );
        row.appendChild(button("决定…", () => void this.decideItem(item)));
        card.appendChild(row);
        if (item.scope === "world") card.appendChild(paragraph("范围：整个世界（跨时间线）", "u-hint"));
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
          gaps.length ? bulletList(gaps.map((item) => `缺口：${String(item.detail ?? item)}`), "u-list") : paragraph("硬约束没有缺口。", "u-hint"),
          deviations.length
            ? bulletList(deviations.map((item) => `偏离：${String(item.detail ?? item)}｜需要：${String(item.need ?? "")}`), "u-list")
            : paragraph("没有偏离项。", "u-hint"),
          evidence.length
            ? paragraph(`世界里已经有 ${evidence.length} 条可用依据，标记达成时可以直接引用。`, "u-hint")
            : paragraph("世界里暂时没有能对上条目的依据：必要时先提出世界变化候选。", "u-hint"),
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
        `检查完成：${((result.gaps as Json[]) ?? []).length} 条缺口、${((result.deviations as Json[]) ?? []).length} 条偏离`,
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
        ? [["in_progress", "开始推进"], ["deviated", "直接记偏离"], ["abandoned", "放弃"]]
        : status === "in_progress"
          ? [["achieved", "标记达成"], ["deviated", "接受偏离"], ["abandoned", "放弃"]]
          : status === "deviated"
            ? [["in_progress", "回到推进"], ["achieved", "标记达成"], ["abandoned", "放弃"]]
            : [["deviated", "接受偏离"]];
    if (layer === "forbidden") {
      choices.length = 0;
      choices.push(["deviated", "已触发（记偏离）"], ["abandoned", "放弃这条禁止事项"]);
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
          run: () => {
            void (async () => {
              if (!reason.value.trim()) {
                setNote(note, "决定必须写理由：偏离可以被接受，不能被静默掩盖", "bad");
                return;
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
              } catch (error) {
                setNote(note, uiError(error, { module: "大纲", action: "记下决定" }).message, "bad");
              }
            })();
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
          run: () => {
            void (async () => {
              if (!statement.value.trim()) {
                setNote(note, "至少要写清楚这一条要求什么", "bad");
                return;
              }
              try {
                const current = await this.ctx.api.waOutlineGet(this.outlineId);
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
                setNote(this.note, "已加进大纲；重新绑定这条线之后它才会进入进度", "ok");
                await this.render();
              } catch (error) {
                setNote(note, uiError(error, { module: "大纲", action: "加条目" }).message, "bad");
              }
            })();
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
          run: () => {
            void (async () => {
              if (!name.value.trim() || !statement.value.trim()) {
                setNote(note, "名称与主题约束都要写", "bad");
                return;
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
                setNote(this.note, `大纲「${outline.name}」已保存：点「绑定到大纲」把它绑到这条线`, "ok");
                await this.render();
              } catch (error) {
                setNote(note, uiError(error, { module: "大纲", action: "保存" }).message, "bad");
              }
            })();
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
    const bound = Boolean(this.state);
    const chapter = el("input", {
      class: "u-input", id: "u-wa-chapter", value: String(this.state?.chapter ?? "第一章"),
    }) as HTMLInputElement;
    const observers = el("input", {
      class: "u-input", id: "u-wa-observers", value: this.cardId, placeholder: "观察角色（可留空）",
    }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const modal = dialog(
      bound ? "换观察者 / 章节标签" : "绑定到大纲",
      [
        paragraph(
          bound
            ? "这里只改观察者、章节标签与绑定元数据：这条线上的进度原样保留。"
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
          run: () => {
            void (async () => {
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
              } catch (error) {
                setNote(note, uiError(error, { module: "大纲", action: bound ? "更新绑定" : "绑定" }).message, "bad");
              }
            })();
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
      el(
        "div",
        { class: "u-row" },
        primary("读取当前素材", () => void this.loadMaterial()),
        paragraph(
          this.observed
            ? `读取于 ${stamp(this.observedAt)}｜世界时刻 ${String(this.observed.world_time ?? "")}；冻结或追赶时这里只作历史参考。`
            : "只给所选角色在当前进度上合法可知的材料：亲历 / 看到 / 听说 / 推测都带来源标签。",
          "u-hint",
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
    host.appendChild(section("她知道的（来源标签在右）", rows));
    const experiences = (view.experiences as Json[]) ?? [];
    const claims = (view.claims as Json[]) ?? [];
    host.appendChild(
      section(
        "她的经历与说法",
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
            ...((next.deviations as Json[]) ?? []).map((item) => `偏离：${String(item.detail ?? item)}`),
          ].slice(0, 12),
          "u-list",
        ),
        paragraph("作者身份不等于能读整个世界真值：这里只有她合法知道的东西，加上你这份大纲的缺口与偏离。", "u-hint"),
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
      el(
        "div",
        { class: "u-row" },
        goal,
        field("建议数量（1–5）", limit),
        primary("给我推进建议", () => void this.suggest()),
      ),
    );
    host.appendChild(
      paragraph(
        "建议是候选：不写世界、不推状态。改世界要走「预览世界变化 → 确认应用」；只想留作文字就「以此起草」。",
        "u-hint",
      ),
    );
    const rows = el("div", { class: "u-rows" });
    for (const item of this.candidates) {
      const card = el("article", { class: "u-card" });
      card.appendChild(el("h3", { text: String(item.title || item.summary || item.id) }));
      const basis = ((item.basis as Json) ?? {}) as Json;
      card.appendChild(
        facts([
          ["方案", String(item.summary ?? "")],
          ["依据·事实", String(basis.fact || "（缺）")],
          ["依据·因果", String(basis.causality || "（缺）")],
          ["依据·大纲", String(basis.outline || "（缺）")],
          ["待定问题", ((item.unsolved as string[]) ?? []).join("、") || "（没有列出的待定问题）"],
          ["是否改世界", item.has_world_change ? "含世界变化（要预览后确认）" : "不改世界（只作文字）"],
          ["受众", String(item.audience) === "player" ? "可给玩家看" : "仅作者 / 主持人"],
        ]),
      );
      card.appendChild(
        el(
          "div",
          { class: "u-row" },
          chip(STATUS_TEXT[String(item.status)] ?? String(item.status), item.uncommitted ? "pending" : "muted"),
          item.locked ? chip("正文已锁定", "ok") : null,
          item.effective
            ? chip(`已生效 · 提交 ${String(item.effective_basis).slice(0, 10)}`, "ok")
            : String(item.status) === "committed"
              ? chip("缺少提交依据（不显示为已生效）", "bad")
              : null,
        ),
      );
      if (item.must_not_imply) card.appendChild(paragraph(String(item.must_not_imply), "u-hint"));
      const actions = el("div", { class: "u-row" });
      actions.appendChild(button("以此起草", () => this.startDraft(item)));
      if (item.has_world_change) {
        actions.appendChild(button("预览世界变化", () => void this.previewChange(item)));
        actions.appendChild(button("另开分支试演", () => void this.trialBranch()));
      } else {
        actions.appendChild(button("采用这段文字", () => void this.decideCandidate(item, "approved", String(item.text ?? item.summary ?? ""))));
      }
      actions.appendChild(button("拒绝", () => void this.decideCandidate(item, "rejected")));
      card.appendChild(actions);
      rows.appendChild(card);
    }
    if (!this.candidates.length) {
      rows.appendChild(paragraph("还没有建议：点上面的按钮要一组（每次生成各有独立标识，不会覆盖已采用的内容）。", "u-hint"));
    }
    host.appendChild(section("待决定的建议", rows));
  }

  private async suggest(): Promise<void> {
    if (!this.state) {
      setNote(this.note, "先绑定一份大纲：建议要挂在大纲与角色上", "bad");
      return;
    }
    setNote(this.note, "正在要一组建议（一次便宜调用）…", "pending");
    try {
      const result = await this.ctx.api.waSuggest({
        instance_id: this.instanceId,
        timeline_id: this.timelineId,
        outline: this.outlineId,
        observer: this.cardId,
        goal: this.goal,
        limit: this.limit,
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
        setNote(this.note, `拿到 ${made.length} 条建议（未采用，不影响世界）`, "ok");
      } else {
        setNote(this.note, `这次没有拿到建议：${String(result.reason ?? status)}`, "bad");
      }
      await this.refreshCandidates();
      this.tab = "advice";
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "推进建议", action: "要一组建议" }).message, "bad");
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
            ? "已采用；它含世界变化，尚未生效——要去「预览世界变化 → 确认应用」才会提交"
            : "已采用：它只是一段文字，不改世界",
        status === "approved" && candidate.has_world_change ? "pending" : "ok",
      );
      await this.refreshCandidates();
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "推进建议", action: "记下决定" }).message, "bad");
    }
  }

  private startDraft(item: Json): void {
    this.draftId = String(item.id);
    this.draftTitle = String(item.title || "未命名草稿");
    this.draftBody = String(item.text || item.summary || "");
    this.draftKey = `text:${this.instanceId}:${item.id}`;
    this.tab = "draft";
    void this.render();
  }

  private async previewChange(item: Json): Promise<void> {
    const changes = (item.changes as Json[]) ?? [];
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const modal = dialog(
      "预览世界变化",
      [
        paragraph(`候选：${String(item.title || item.summary || item.id)}`),
        changes.length
          ? bulletList(
              changes.map(
                (change) =>
                  `对象：${((change.target_refs as string[]) ?? []).join("、") || "（未注明）"}｜变化：${String(change.kind)}（${String(change.operation)}）→ ${String(change.value ?? "")}`,
              ),
              "u-list",
            )
          : paragraph("这条候选没有附带世界变化：它只能作为文字采用。", "u-hint"),
        paragraph(
          "预览只列要发生什么，不改世界。确认应用 = 采用这条候选并提交到当前时间线（会留下版本点）；采用与提交是两步，界面一起做，缺一步都不算生效。",
          "u-hint",
        ),
        note,
      ],
      [
        {
          label: "确认应用到此时间线",
          run: () => {
            void (async () => {
              if (!changes.length) {
                setNote(note, "这条候选没有世界变化可用", "bad");
                return;
              }
              try {
                // 先「采用」（approved），再提交：核心要求只有已批准的候选才能进世界
                if (String(item.status) !== "approved") {
                  const adopted = await this.ctx.api.waCandidateDecide(
                    { candidate_id: String(item.id), status: "approved", reason: "作者确认应用", text: String(item.text ?? "") },
                    this.instanceId,
                    this.timelineId,
                  );
                  // 决定接口返回的是候选本身（不带顶层 status）：采用成没成看候选上那一个
                  if (String((adopted.candidate as Json)?.status ?? "") !== "approved") {
                    setNote(note, `没能采用这条候选：${String((adopted.candidate as Json)?.reason ?? "核心没有采用它")}`, "bad");
                    return;
                  }
                }
                const result = await this.ctx.api.waCandidateCommit(String(item.id), this.instanceId, this.timelineId);
                const status = String(result.status);
                if (status === "ok" || status === "duplicate") {
                  setNote(
                    this.note,
                    `已生效（${status === "duplicate" ? "重复提交，世界未再变化" : "已提交"}）：提交标识 ${String(result.commit_id ?? result.revision ?? "").slice(0, 12) || "（无）"}`,
                    "ok",
                  );
                } else {
                  setNote(this.note, `没有生效：${status}｜${String(result.reason ?? "")}（候选还在，可以检查后重来）`, "bad");
                }
                await this.refreshCandidates();
                await this.render();
              } catch (error) {
                setNote(note, uiError(error, { module: "世界变化", action: "确认应用", done: "候选还在，没有被改写" }).message, "bad");
              }
            })();
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  private async trialBranch(): Promise<void> {
    const name = el("input", { class: "u-input", id: "u-wa-branch-name", placeholder: "例如：试演·封堤" }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    let head = "";
    try {
      const commits = await this.ctx.api.commits(this.instanceId, this.timelineId);
      head = String(((commits.commits as Json[]) ?? [])[0]?.id ?? "");
    } catch {
      head = "";
    }
    const modal = dialog(
      "另开分支试演",
      [
        paragraph("从当前版本另开一条暂停的新线，在这条线上试；主线不被污染（项目不提供世界线合并）。"),
        field("新时间线名称", name),
        paragraph(head ? `来源版本：${head.slice(0, 16)}` : "这条线还没有版本点：先在「世界与素材 → 版本记录」保存一个，再从它分支。", "u-hint"),
        note,
      ],
      [
        {
          label: "建立暂停分支",
          run: () => {
            void (async () => {
              if (!head) {
                setNote(note, "先保存一个版本点，再从它分支", "bad");
                return;
              }
              try {
                const result = await this.ctx.api.waBranch({
                  instance_id: this.instanceId,
                  timeline_id: this.timelineId,
                  commit_id: head,
                  name: name.value.trim() || "试演线",
                });
                setNote(
                  this.note,
                  `已建立暂停分支「${String(((result.timeline as Json) ?? {}).name ?? "")}」：到「世界与素材」启动它，再回来取得预览并应用；建立分支本身不算试演完成。`,
                  "ok",
                );
                await this.render();
              } catch (error) {
                setNote(note, uiError(error, { module: "试演", action: "建立分支" }).message, "bad");
              }
            })();
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
    for (const item of drafts) {
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: String(item.title || item.id) }));
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
    if (!drafts.length) {
      list.appendChild(paragraph("还没有文字草稿：在「推进建议」里点「以此起草」，或直接在下面新建一份。", "u-hint"));
    }
    host.appendChild(
      section(
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
    const status = el("p", {
      class: "u-note",
      role: "status",
      "aria-live": "polite",
      text: this.draftId ? `正在编辑：${this.draftId}${locked ? "（已锁定）" : ""}` : "还没有选中的草稿",
    });
    host.appendChild(
      section(
        "正文",
        field("标题", title),
        field("正文", body),
        el(
          "div",
          { class: "u-row" },
          primary("保存文字草稿", () => void this.saveDraft()),
          locked ? button("解锁并编辑", () => void this.lockDraft(false)) : button("锁定正文", () => void this.lockDraft(true)),
          button("复制为新稿", () => void this.copyDraft()),
          button("导出所选文字…", () => void this.exportDraft()),
        ),
        paragraph(
          "保存文字不等于世界已改变。锁定后新生成永远另起一稿，世界恢复也不会改写它；导出只含标题与正文。",
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
    const current = this.candidates.find((item) => String(item.id) === this.draftId);
    // 已采用 / 已锁定 / 已提交的稿子不动：保存成一份新的待审候选（§7.5、§11.1）
    const fresh = !current || Boolean(current.locked) || String(current.status) !== "proposed";
    const targetId = fresh ? `${this.draftId || "draft"}-${Date.now().toString(36)}` : this.draftId;
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
      this.draftId = targetId;
      await this.refreshCandidates();
      setNote(this.note, `文字草稿已保存（${targetId}）：不等于世界已改变`, "ok");
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
      await this.refreshCandidates();
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "文字草稿", action: locked ? "锁定" : "解锁" }).message, "bad");
    }
  }

  private async copyDraft(): Promise<void> {
    this.draftId = `${this.draftId || "draft"}-copy-${Date.now().toString(36)}`;
    this.draftTitle = `${this.draftTitle || "草稿"}（副本）`;
    this.draftKey = `text:${this.instanceId}:${this.draftId}`;
    setNote(this.note, "已复制为新稿：这是新的待保存草稿，原来那份不动", "muted");
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
      setNote(this.note, `已导出：${path}（只有标题与正文，不含依据与幕后材料）`, "ok");
    } catch (error) {
      setNote(this.note, uiError(error, { module: "文字草稿", action: "导出" }).message, "bad");
    }
  }
}
