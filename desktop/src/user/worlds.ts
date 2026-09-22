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
  link,
  paragraph,
  primary,
  section,
  setNote,
  stamp,
} from "./dom";

/** 时间线状态的人话（核心给的是内部状态名） */
const STATE_TEXT: Record<string, string> = {
  active: "运行中",
  frozen: "已暂停",
  archived: "已归档",
};

export class WorldsPane implements Pane {
  readonly id = "worlds" as const;
  private view: "list" | "detail" | "change" = "list";
  private current: InstanceEntry | null = null;
  private note: HTMLElement | null = null;
  /** 动作结果：rerender 会重建反馈槽，先把话记下来，渲染完再写回 */
  private pendingNote: { text: string; kind: "ok" | "bad" | "pending" | "muted" } | null = null;
  private showArchived = false;

  constructor(private readonly ctx: AppContext) {}

  async mount(host: HTMLElement): Promise<void> {
    if (this.ctx.route.sub === "detail" && this.ctx.instances().length) {
      this.view = "detail";
      this.current = this.ctx.instances()[0];
    }
    await this.render(host);
  }

  private async render(host: HTMLElement): Promise<void> {
    await this.ctx.refresh();
    const page = el("div", { class: "u-page" });
    page.appendChild(el("h2", { class: "u-h2", text: "世界与素材" }));
    page.appendChild(this.tabs());
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
    else await this.renderList(body);
  }

  private tabs(): HTMLElement {
    return el(
      "div",
      { class: "u-row u-tabs" },
      button("我的世界", () => {
        this.view = "list";
        void this.rerender();
      }, { class: this.view === "list" ? "u-btn u-primary" : "u-btn" }),
      button("世界设定 / 角色卡", () => void this.renderAssetsInline()),
      button("导入", () => void this.importFlow()),
      primary("从样例开始", () => this.ctx.navigate({ pane: "onboarding", sub: "sample" })),
    );
  }

  /** 写一条会活过这次重渲染的结果说明 */
  private flash(text: string, kind: "ok" | "bad" | "pending" | "muted" = "ok"): void {
    this.pendingNote = { text, kind };
    setNote(this.note, text, kind);
  }

  private async rerender(): Promise<void> {
    const host = document.querySelector("#u-main") as HTMLElement | null;
    if (host) await this.render(host);
  }

  /* ---------------------------------------------------------------- 我的世界 */

  private async renderList(host: HTMLElement): Promise<void> {
    const instances = this.ctx.instances();
    if (!instances.length) {
      host.appendChild(
        section(
          "这里保存你的世界和创作材料",
          paragraph("还没有世界。可以从随程序提供的样例开始，或创建自己的世界。"),
          el(
            "div",
            { class: "u-row" },
            primary("从样例开始", () => this.ctx.navigate({ pane: "onboarding", sub: "sample" })),
            button("创建自己的世界", () => this.ctx.navigate({ pane: "onboarding", sub: "own" })),
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
          el("th", { text: "时间线" }),
          el("th", { text: "状态" }),
          el("th", { text: "创建时间" }),
          el("th", { text: "操作" }),
        ),
      ),
    );
    const body = el("tbody", {});
    for (const row of rows) {
      const state = row.running.includes("active") ? "有正在运行的线" : row.running.includes("frozen") ? "全部暂停" : "已归档";
      const tr = el("tr", {});
      tr.appendChild(el("td", { text: row.instance.name }));
      tr.appendChild(el("td", { text: String(row.instance.original_name ?? "") }));
      tr.appendChild(el("td", { text: `${row.timelines.length} 条 / ${row.characters.length} 位角色` }));
      tr.appendChild(el("td", {}, chip(state, state.includes("运行") ? "ok" : "pending")));
      tr.appendChild(el("td", { text: stamp(Number(row.instance.created_at ?? 0)) }));
      tr.appendChild(
        el(
          "td",
          {},
          el(
            "div",
            { class: "u-row" },
            link("打开", () => {
              this.current = row.instance;
              this.view = "detail";
              void this.rerender();
            }),
            link("重命名", () => void this.rename(row.instance)),
            link("导出", () => void this.exportInstance(row.instance)),
            link("删除", () => void this.deleteInstance(row.instance)),
          ),
        ),
      );
      body.appendChild(tr);
    }
    table.appendChild(body);
    host.appendChild(section("我的世界", table));
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
        paragraph("删除后这个世界的对话、时间线、记忆与草稿都会消失，不能撤销。"),
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
    const instance = this.current;
    if (!instance) {
      this.view = "list";
      await this.renderList(host);
      return;
    }
    const info = await this.ctx.api.instanceInfo(instance.id);
    const timelines = (info.timelines as Json[]) ?? [];
    const characters = (info.characters as Json[]) ?? [];
    const commits = (info.commits as Json[]) ?? [];
    host.appendChild(
      section(
        instance.name,
        el(
          "div",
          { class: "u-row" },
          button("返回世界列表", () => {
            this.view = "list";
            void this.rerender();
          }),
          primary("联络角色", () => this.ctx.navigate({ pane: "contact" })),
          button("在此写作", () => this.ctx.navigate({ pane: "writing" })),
          button("在此跑团", () => this.ctx.navigate({ pane: "trpg" })),
          button("尝试世界变化…", () => {
            this.view = "change";
            void this.rerender();
          }),
        ),
        facts([
          ["来源设定", String(instance.original_name ?? "")],
          ["兼容性", String(instance.compatibility ?? "")],
          ["创建时间", stamp(Number(instance.created_at ?? 0))],
        ]),
      ),
    );
    host.appendChild(
      section(
        "角色（只列公开身份）",
        bulletList(
          characters.map((item) => `${String(item.name ?? "")}${item.occupation ? ` · ${String(item.occupation)}` : ""}`),
          "u-list",
        ),
        el(
          "div",
          { class: "u-row" },
          button("加入角色…", () => setNote(this.note, "加入角色属于「角色卡导入并向这条线补入」：在第 5.4 节流程落地前，请用核心命令行完成", "pending")),
        ),
      ),
    );
    host.appendChild(this.timelineSection(timelines));
    host.appendChild(this.versionSection(commits));
    host.appendChild(
      section(
        "数据位置",
        el(
          "div",
          { class: "u-row" },
          button("打开创作目录", () => void openDir("packages", this.ctx.api)),
          button("打开数据目录", () => void openDir("data", this.ctx.api)),
        ),
      ),
    );
  }

  private timelineSection(timelines: Json[]): HTMLElement {
    const rows = el("div", { class: "u-rows" });
    const archived = timelines.filter((item) => String(item.state) === "archived");
    const visible = this.showArchived ? timelines : timelines.filter((item) => String(item.state) !== "archived");
    if (!visible.length) rows.appendChild(el("p", { class: "u-hint", text: "没有可显示的时间线（已归档的可以点下面的开关查看）。" }));
    for (const timeline of visible) {
      const id = String(timeline.id);
      const state = String(timeline.state ?? "");
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: `${String(timeline.name ?? id)}` }));
      row.appendChild(chip(STATE_TEXT[state] ?? state, state === "active" ? "ok" : "pending"));
      if (state !== "archived") {
        row.appendChild(
          button(state === "active" ? "暂停" : "启动", () => {
            void (async () => {
              try {
                if (state === "active") await this.ctx.api.freeze(this.current?.id ?? "", id);
                else await this.ctx.api.activate(this.current?.id ?? "", id);
                this.flash(state === "active" ? "已暂停这条线（暂停不等于退出程序）" : "已启动这条线");
                await this.rerender();
              } catch (error) {
                setNote(this.note, uiError(error, { module: "时间线", action: "运行 / 暂停" }).message, "bad");
              }
            })();
          }),
        );
        row.appendChild(button("改名", () => void this.renameTimeline(id, String(timeline.name ?? ""))));
        row.appendChild(button("归档", () => void this.archiveTimeline(id)));
      }
      row.appendChild(button("删除…", () => void this.deleteTimeline(id, String(timeline.name ?? id))));
      rows.appendChild(row);
    }
    const rate = el("input", { class: "u-input u-input-narrow", type: "number", min: "1", value: "1" }) as HTMLInputElement;
    return section(
      "时间线",
      rows,
      el(
        "div",
        { class: "u-row" },
        el("span", { class: "u-hint", text: "世界速度（世界秒 / 现实秒）：" }),
        rate,
        button("设为这个速度", () => {
          void (async () => {
            const active = timelines.find((item) => String(item.state) === "active") ?? timelines[0];
            try {
              await this.ctx.api.setRate(this.current?.id ?? "", String(active?.id ?? ""), Number(rate.value || 1));
              this.flash("速度已设定（生效以世界钟为准）");
            } catch (error) {
              setNote(this.note, uiError(error, { module: "时间线", action: "设置速度" }).message, "bad");
            }
          })();
        }),
        archived.length || this.showArchived
          ? button(this.showArchived ? `隐藏已归档（${archived.length}）` : `显示已归档（${archived.length}）`, () => {
              this.showArchived = !this.showArchived;
              void this.rerender();
            })
          : null,
      ),
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
      paragraph("删除这条时间线：它的会话、记忆、版本点与派生素材一起消失，不能撤销。"),
      paragraph("不受影响：世界设定、角色卡、其他时间线，以及被其他线引用的提交。", "u-hint"),
      field(`键入名称确认（${name}）`, input),
      note,
    );
    const modal = dialog(`删除时间线「${name}」？`, [body], [
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
              this.flash(`已删除时间线「${name}」`);
              await this.rerender();
            } catch (error) {
              // 最后一条线删不掉：按核心给的实际原因说，不笼统报“失败”
              setNote(this.note, uiError(error, { module: "时间线", action: "删除" }).message, "bad");
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
      host.appendChild(errorCard(uiError(error, { module: "尝试世界变化", action: "读取可选对象" })));
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
    const name = el("input", { class: "u-input", id: "u-change-name", placeholder: "新时间线名称（可留空）" }) as HTMLInputElement;
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
            ["草案编号", draftId],
            ["意图", String(draft.intent ?? "")],
            ["生效", String(draft.when) === "scheduled" ? `预约到世界时刻 ${Number(draft.at_world ?? 0)}` : "现在"],
          ]),
          bulletList(
            effects.map(
              (item) =>
                `${kindLabels.get(String(item.kind)) ?? String(item.kind)} → ${
                  targetLabels.get(String(item.target)) ?? String(item.target)
                }${item.value ? `：${String(item.value)}` : ""}（${String(item.expiry)}）`,
            ),
            "u-list",
          ),
          paragraph("确认后会从这条线的来源版本另开一条新线，新线默认暂停；原线保持原样。", "u-hint"),
          field("新时间线名称", name),
          el(
            "div",
            { class: "u-row" },
            primary("确认并新建时间线", () => void confirm()),
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
      setNote(draftNote, "正在新建时间线…", "pending");
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
          unknown: "新时间线是否已经建立",
        });
        result.appendChild(errorCard(info, [{ label: "重新确认", run: () => void confirm() }]));
      }
    };

    host.appendChild(
      section(
        "尝试世界变化",
        paragraph(
          "只在当前局势内改变事实：不能改写过去、也不能改世界公理（那些要改世界设定并新建世界）。表单里的对象都来自这个世界已经登记的内容。",
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
          primary("查看草案", () => void viewDraft()),
          button("返回世界详情", () => {
            this.view = "detail";
            void this.rerender();
          }),
        ),
        draftNote,
        result,
      ),
    );
  }

  private async renameTimeline(timelineId: string, current: string): Promise<void> {
    const input = el("input", { class: "u-input", value: current }) as HTMLInputElement;
    const modal = dialog("时间线名称", [field("新名称", input)], [
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
              setNote(this.note, uiError(error, { module: "时间线", action: "改名" }).message, "bad");
            }
          })();
        },
      },
      { label: "取消", run: () => undefined },
    ]);
    document.body.appendChild(modal.node);
  }

  private async archiveTimeline(timelineId: string): Promise<void> {
    try {
      await this.ctx.api.archiveTimeline(this.current?.id ?? "", timelineId);
      this.flash("已归档（先暂停，数据保留；要继续这条路线可以从它的版本另开分支）");
      await this.rerender();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "时间线", action: "归档" }).message, "bad");
    }
  }

  private versionSection(commits: Json[]): HTMLElement {
    const list = el("ol", { class: "u-list" });
    for (const commit of commits.slice(0, 20)) {
      const line = el("li", {});
      line.appendChild(
        el("span", {
          text: `${stamp(Number(commit.created_at ?? 0))}｜世界进度 ${Number(commit.moment ?? 0)}｜${String(commit.kind ?? "")}${commit.note ? `｜${String(commit.note)}` : ""}`,
        }),
      );
      const row = el("div", { class: "u-row" });
      row.appendChild(button("从这里另开分支", () => void this.fork(commit)));
      row.appendChild(button("恢复到此版本…", () => void this.restore(commit)));
      line.appendChild(row);
      list.appendChild(line);
    }
    if (!commits.length) list.appendChild(el("li", { class: "u-hint", text: "尚无可用版本：运行中会自动留下版本点，也可以手动保存一个。" }));
    return section(
      "版本记录",
      el(
        "div",
        { class: "u-row" },
        primary("保存当前版本", () => void this.saveVersion()),
      ),
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
    const name = el("input", { class: "u-input", placeholder: "新时间线名称" }) as HTMLInputElement;
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

  private async restore(commit: Json): Promise<void> {
    const timelineId = String(commit.timeline_id ?? "");
    const instanceId = this.current?.id ?? "";
    const body = el("div", {});
    const note = el("p", { class: "u-note" });
    try {
      const preview = await this.ctx.api.storyRestore(instanceId, timelineId, String(commit.id ?? ""), false, false);
      const coverage = (preview.coverage as Json) ?? {};
      body.appendChild(
        facts([
          ["回到的世界时刻", String(coverage.to_world ?? "—")],
          ["现在", String(coverage.now_world ?? "—")],
          ["这条线已投递的回复总数", String(coverage.delivered_replies ?? 0)],
        ]),
      );
      body.appendChild(paragraph(String(preview.warning ?? "")));
      body.appendChild(paragraph(String(preview.reason ?? ""), "u-hint"));
    } catch (error) {
      body.appendChild(errorCard(uiError(error, { module: "版本", action: "恢复到此版本" })));
    }
    body.appendChild(
      paragraph("恢复是覆盖操作：先保存当前进展（保存为分支或导出），确认页才能体现「当前进展已保留」。", "u-hint"),
    );
    const keepFirst = button("先保存当前进展", () => {
      void (async () => {
        try {
          const timeline = await this.firstTimeline();
          await this.ctx.api.saveVersion(instanceId, timeline, "恢复前保留");
          await this.ctx.api.forkTimeline(instanceId, timeline, String(commit.id ?? ""), `恢复前保留 ${stamp(Date.now() / 1000)}`);
          setNote(note, "已把当前进展保存为保留分支", "ok");
        } catch (error) {
          setNote(note, uiError(error, { module: "版本", action: "保存当前进展" }).message, "bad");
        }
      })();
    });
    const modal = dialog("恢复到此版本", [body, keepFirst, note], [
      { label: "取消", run: () => undefined },
    ]);
    document.body.appendChild(modal.node);
  }

  /* ---------------------------------------------------------------- 素材与导入 */

  private async renderAssetsInline(): Promise<void> {
    const host = document.querySelector("#u-main") as HTMLElement | null;
    if (!host) return;
    const packages = await this.ctx.api.packages();
    const cards = await this.ctx.api.cards();
    const drafts = await this.ctx.api.draftList("world");
    const page = el("div", { class: "u-page" });
    page.appendChild(el("h2", { class: "u-h2", text: "世界设定与角色卡" }));
    page.appendChild(this.tabs());
    const pkgList = ((packages.packages as Json[]) ?? []).map((item) => {
      const ok = Boolean(item.valid);
      return `${String(item.name ?? item.file)}：${ok ? "可用于创建" : `需要检查（${((item.errors as string[]) ?? []).slice(0, 2).join("；")}）`}`;
    });
    const cardList = ((cards.cards as Json[]) ?? []).map(
      (item) => `${String(item.name ?? item.file)}：${item.confirmed ? "已审定" : "未确认（不能用于创建）"}`,
    );
    page.appendChild(section("世界设定", bulletList(pkgList.length ? pkgList : ["还没有世界设定"], "u-list")));
    page.appendChild(section("角色卡", bulletList(cardList.length ? cardList : ["还没有角色卡"], "u-list")));
    page.appendChild(
      section(
        "未完成内容",
        bulletList(
          ((drafts.drafts as Json[]) ?? []).map((item) => `${String(item.target ?? "")}（${stamp(Number(item.updated_at ?? 0))}）`),
          "u-list",
        ),
      ),
    );
    page.appendChild(
      section(
        "创作工作区",
        paragraph(
          "结构化表单、锁定与分段重跑的工作区按实施顺序在 U2 落地；现在创建世界请走「从样例开始」，或使用核心命令行。",
          "u-hint",
        ),
        el(
          "div",
          { class: "u-row" },
          primary("从样例开始", () => this.ctx.navigate({ pane: "onboarding", sub: "sample" })),
          button("返回世界列表", () => {
            this.view = "list";
            void this.rerender();
          }),
        ),
      ),
    );
    fill(host, page);
  }

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
      const payload = JSON.parse(await invoke<string>("read_text_file", { path: picked }));
      const isCard = Boolean((payload as Json).identity);
      if (isCard) {
        const packages = await this.ctx.api.packages();
        const options = el("select", { class: "u-input" }) as HTMLSelectElement;
        for (const item of (packages.packages as Json[]) ?? []) {
          options.appendChild(el("option", { value: String(item.file), text: String(item.name ?? item.file) }));
        }
        const modal = dialog(
          "这张角色卡属于哪个世界设定？",
          [paragraph("角色卡里的渠道与史料引用要跟着世界设定一起检查。"), field("世界设定", options)],
          [
            {
              label: "检查并导入",
              primary: true,
              run: () => {
                void (async () => {
                  try {
                    const result = await this.ctx.api.call("world.card.import", {
                      source_path: picked,
                      package_path: options.value,
                    });
                    this.flash(`已导入角色卡「${String(result.name ?? result.imported ?? "")}」（未审定，需确认后才能用于创建）`);
                  } catch (error) {
                    setNote(this.note, uiError(error, { module: "导入", action: "导入角色卡" }).message, "bad");
                  }
                })();
              },
            },
            { label: "取消", run: () => undefined },
          ],
        );
        document.body.appendChild(modal.node);
        return;
      }
      const result = await this.ctx.api.call("world.package.import", { source_path: picked });
      this.flash(`已导入世界设定「${String(result.name || result.imported || "")}」`);
    } catch (error) {
      setNote(this.note, uiError(error, { module: "导入", action: "导入文件" }).message, "bad");
    }
  }
}
