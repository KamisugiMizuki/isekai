/*
 * 跑团工作区（USER_INTERFACE_DESIGN §8.1–§8.5）。
 *
 * 三块：继续 / 新建战役（§8.1）、场景与行动（§8.2–8.3）、主持面（§8.4）。
 * 一条硬规矩贯穿：没有真实裁定就不显示骰点、不说成功；世界变化只有真实提交过才算发生。
 * 规则与「外部聊天通道」分开：规则走 rules.* 登记簿（§8.5），不碰通道插件安装。
 */

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
} from "./dom";
import { flowRail, type FlowStep } from "./graphics";

type View = "list" | "create" | "play";

/** 一次行动在界面上走到哪一步（§8.2/§8.3）；写死的是流程，不是核心状态名 */
const ACTION_STEPS: FlowStep[] = [
  { label: "写下行动", hint: "行动、行动者与目标齐了才能打开确认卡" },
  { label: "确认卡齐备", hint: "改任何关键项都会让旧确认失效" },
  { label: "规则裁定", hint: "不取消后自动重掷；切页不会发第二次裁定" },
  { label: "写入世界", hint: "规则状态与世界后果同批成功或同批失败" },
];

/** 结果的固定顺序（§8.3）：行动结果 → 世界后果 → 谁知道 → 下一步 */
const RESULT_STEPS: FlowStep[] = [
  { label: "行动结果" },
  { label: "实际世界后果" },
  { label: "相关角色能知道的内容" },
  { label: "下一步" },
];

const STATUS_TEXT: Record<string, string> = {
  preparing: "准备中",
  active: "进行中",
  waiting: "等待处理",
  paused: "已暂停",
  blocked: "已阻断",
  archived: "已归档",
};

const NEXT_KIND: Record<string, string> = {
  instant: "即时",
  continuous: "持续",
  opposed: "对抗",
  world: "世界过程",
};

function shortId(value: string): string {
  return value.length > 10 ? `${value.slice(0, 10)}…` : value;
}

/** 名称化显示：有名字给名字，没有就说清「这份还没有名字」，不把内部标识当标题。 */
function displayName(name: unknown, id: string, fallback: string): string {
  const text = String(name ?? "").trim();
  if (text) return text;
  return id ? `${fallback}（${shortId(id)}）` : fallback;
}

export class TrpgPane implements Pane {
  readonly id = "trpg";
  private host: HTMLElement | null = null;
  private note: HTMLElement | null = null;
  private view: View = "list";
  private campaigns: Json[] = [];
  private plugins: Json[] = [];

  // 战役进行中的状态
  private instanceId = "";
  private timelineId = "";
  private campaignId = "";
  private characterId = "";
  private actorOptions: string[] = [];
  private mode: "player" | "gm" = "player";
  private workspace: Json | null = null;
  private bundle: Json | null = null;
  private actionText = "";
  private actionId = "";
  private actorId = "";
  private targetText = "";
  private methodText = "";
  private lastResults: Json | null = null;

  // 新建战役的表单
  private draft = {
    name: "",
    instanceId: "",
    timelineId: "",
    rulesetId: "",
    rulesetVersion: "",
    manifest: "",
    participants: [] as string[],
    sceneName: "",
    sceneBrief: "",
    sceneKind: "exploration",
    sceneLocation: "",
    scenePrivate: "",
  };

  constructor(private readonly ctx: AppContext) {}

  async mount(host: HTMLElement): Promise<void> {
    this.host = host;
    this.note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    fill(host, this.note);
    await this.render();
  }

  /* ------------------------------------------------------------ 骨架 */

  private async render(): Promise<void> {
    const host = this.host;
    if (!host || !this.note) return;
    fill(host, this.note);
    try {
      if (this.view === "list") await this.renderList(host);
      else if (this.view === "create") await this.renderCreate(host);
      else this.renderPlay(host);
    } catch (error) {
      host.appendChild(errorCard(uiError(error, { module: "跑团", action: "打开工作区" })));
    }
  }

  private async loadCampaigns(): Promise<void> {
    const rows: Json[] = [];
    for (const instance of this.ctx.instances()) {
      try {
        const result = await this.ctx.api.trpgCampaigns(instance.id);
        for (const item of ((result.campaigns as Json[]) ?? [])) {
          rows.push({ ...item, instance_name: instance.name });
        }
      } catch {
        /* 单个世界读不到不影响别的世界 */
      }
    }
    this.campaigns = rows;
  }

  private async loadPlugins(): Promise<void> {
    try {
      const result = await this.ctx.api.rulesList();
      this.plugins = (result.plugins as Json[]) ?? [];
    } catch {
      this.plugins = [];
    }
  }

  /* ------------------------------------------------------------ §8.1 继续 / 新建 */

  private async renderList(host: HTMLElement): Promise<void> {
    if (!this.plugins.length) await this.loadPlugins();
    if (!this.campaigns.length) await this.loadCampaigns();
    const rows = el("div", { class: "u-rows" });
    for (const item of this.campaigns) {
      const head = el("div", { class: "u-row-line" });
      head.appendChild(
        el("span", {
          class: "u-grow",
          text: displayName(item.name, String(item.campaign_id), "未命名战役"),
        }),
      );
      head.appendChild(chip(STATUS_TEXT[String(item.status)] ?? String(item.status), "muted"));
      head.appendChild(button("继续", () => void this.open(String(item.instance_id), String(item.timeline_id), String(item.campaign_id))));
      rows.appendChild(head);
      rows.appendChild(
        paragraph(
          `${String(item.instance_name ?? "")}｜${String(item.timeline_id)}｜规则 ${String(item.ruleset_id || "（未声明）")} ${String(item.ruleset_version || "")}｜主持模式 ${String(item.host_mode ?? "")}`,
          "u-hint",
        ),
      );
    }
    if (!this.campaigns.length) {
      rows.appendChild(paragraph("还没有战役。可以新建一局，或用随发行的样例材料先跑一局。", "u-hint"));
    }
    host.appendChild(
      section(
        "继续已有战役",
        rows,
        el(
          "div",
          { class: "u-row" },
          primary("新建战役", () => {
            this.view = "create";
            void this.render();
          }),
          button("从样例开始", () => void this.startFromSample()),
        ),
        paragraph(
          "玩家 / 主持是进入后的操作视图，不是账号或联网权限；首版新建固定采用辅助裁定。",
          "u-hint",
        ),
      ),
    );
    const pluginRows = el("div", { class: "u-rows" });
    for (const item of this.plugins) {
      const line = el("div", { class: "u-row-line" });
      line.appendChild(el("span", { class: "u-grow", text: `${String(item.name || item.ruleset_id)} ${String(item.ruleset_version)}` }));
      line.appendChild(chip(String(item.status_text ?? item.status), String(item.status) === "available" ? "ok" : "muted"));
      if (item.referenced_count) line.appendChild(chip(`被 ${Number(item.referenced_count)} 局使用`, "muted"));
      pluginRows.appendChild(line);
    }
    if (!this.plugins.length) pluginRows.appendChild(paragraph("本机还没有登记规则插件。", "u-hint"));
    host.appendChild(
      section(
        "本机规则",
        pluginRows,
        el(
          "div",
          { class: "u-row" },
          button("从本机选择规则目录…", () => void this.addRuleFromDisk()),
          button("打开设置里的扩展页", () => this.ctx.navigate({ pane: "settings", sub: "extensions" })),
        ),
        paragraph("规则与「外部聊天通道」是两件事：这里登记的是跑团规则插件，通道在设置里单独管。", "u-hint"),
      ),
    );
  }

  private async startFromSample(): Promise<void> {
    setNote(this.note, "正在准备样例战役材料…", "pending");
    try {
      if (!this.plugins.length) await this.loadPlugins();
      const sample = this.plugins.find((item) => String(item.status) === "available");
      const instances = this.ctx.instances();
      if (!instances.length) {
        setNote(this.note, "还没有世界：先从样例世界开始，再回来建战役", "bad");
        this.ctx.navigate({ pane: "onboarding", sub: "sample" });
        return;
      }
      if (!sample) {
        setNote(this.note, "本机还没有可用的规则插件：先「从本机选择规则目录」登记一份（随发行的潮汐样例在 examples/tide_rules_plugin）", "bad");
        return;
      }
      this.draft = {
        ...this.draft,
        name: "样例战役",
        instanceId: instances[0].id,
        rulesetId: String(sample.ruleset_id ?? ""),
        rulesetVersion: String(sample.ruleset_version ?? ""),
        manifest: String(sample.manifest_path ?? ""),
      };
      this.view = "create";
      setNote(this.note, "已按样例预填：规则与规则版本来自本机登记，世界用第一个世界（可在向导里改）", "muted");
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "跑团", action: "准备样例" }).message, "bad");
    }
  }

  private async addRuleFromDisk(): Promise<void> {
    setNote(this.note, "正在打开目录选择…", "pending");
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const picked = await invoke<string | null>("pick_dir", { title: "选择规则插件所在目录" });
      if (!picked) {
        setNote(this.note, "已取消选择", "muted");
        return;
      }
      const scanned = await this.ctx.api.rulesScan(picked);
      const candidates = (scanned.candidates as Json[]) ?? [];
      if (!candidates.length) {
        setNote(this.note, `这个位置没有找到规则插件清单：${String(scanned.reason ?? "")}`, "bad");
        return;
      }
      const item = candidates[0];
      const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
      const modal = dialog(
        "添加并启用规则插件",
        [
          facts([
            ["来源目录", picked],
            ["名称", String(item.name ?? "")],
            ["规则标识与版本", `${String(item.ruleset_id ?? "")} ${String(item.ruleset_version ?? "")}`],
            ["协议", String(item.protocol ?? "")],
            ["入口", ((item.entry as string[]) ?? []).join(" ")],
            ["状态", String(item.status ?? "")],
          ]),
          paragraph("登记之后核心会在裁定阶段把这份规则作为本地扩展程序运行；进程隔离不是完整安全沙箱。选择文件本身不执行它。", "u-hint"),
          note,
        ],
        [
          {
            label: "添加并启用",
            run: () => {
              void (async () => {
                try {
                  const result = await this.ctx.api.rulesRegister(String(item.manifest_path ?? picked));
                  await this.loadPlugins();
                  setNote(this.note, `已登记并启用：${String((result.plugin as Json)?.name ?? "")}（新建战役时可选）`, "ok");
                  await this.render();
                } catch (error) {
                  setNote(note, uiError(error, { module: "规则登记", action: "添加并启用" }).message, "bad");
                }
              })();
            },
          },
          { label: "取消", run: () => undefined },
        ],
      );
      document.body.appendChild(modal.node);
    } catch (error) {
      setNote(this.note, uiError(error, { module: "规则登记", action: "选择规则目录" }).message, "bad");
    }
  }

  /* ------------------------------------------------------------ §8.1 新建向导 */

  private async renderCreate(host: HTMLElement): Promise<void> {
    const instances = this.ctx.instances();
    if (!instances.length) {
      host.appendChild(
        section(
          "先有一个世界",
          paragraph("战役要挂在一个世界上：先创建或从样例开始。"),
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
    if (!this.plugins.length) await this.loadPlugins();
    const instanceId = this.draft.instanceId || instances[0].id;
    this.draft.instanceId = instanceId;
    const info = await this.ctx.api.instanceInfo(instanceId);
    const timelines = (info.timelines as Json[]) ?? [];
    const characters = (info.characters as Json[]) ?? [];
    if (!timelines.some((item) => String(item.id) === this.draft.timelineId)) {
      this.draft.timelineId = String(timelines[0]?.id ?? "");
    }
    const timeline = timelines.find((item) => String(item.id) === this.draft.timelineId) ?? {};

    const name = el("input", { class: "u-input", id: "u-trpg-name", value: this.draft.name, placeholder: "例如：灰潮纪·北堤调查" }) as HTMLInputElement;
    name.addEventListener("input", () => {
      this.draft.name = name.value;
    });
    const instancePicker = el("select", { class: "u-input", id: "u-trpg-instance" }) as HTMLSelectElement;
    for (const item of instances) instancePicker.appendChild(el("option", { value: item.id, text: item.name }));
    instancePicker.value = instanceId;
    instancePicker.addEventListener("change", () => {
      this.draft.instanceId = instancePicker.value;
      this.draft.timelineId = "";
      this.draft.participants = [];
      void this.render();
    });
    const timelinePicker = el("select", { class: "u-input", id: "u-trpg-timeline" }) as HTMLSelectElement;
    for (const item of timelines) {
      timelinePicker.appendChild(
        el("option", {
          value: String(item.id),
          text: `${String(item.name)}（${String(item.state) === "active" ? "运行中" : "暂停"}）`,
        }),
      );
    }
    timelinePicker.value = this.draft.timelineId;
    timelinePicker.addEventListener("change", () => {
      this.draft.timelineId = timelinePicker.value;
      void this.render();
    });

    const rulePicker = el("select", { class: "u-input", id: "u-trpg-rule" }) as HTMLSelectElement;
    const usable = this.plugins.filter((item) => String(item.status) === "available");
    for (const item of usable) {
      rulePicker.appendChild(
        el("option", {
          value: `${String(item.manifest_path)}|${String(item.ruleset_id)}|${String(item.ruleset_version)}`,
          text: `${String(item.name)} ${String(item.ruleset_version)}`,
        }),
      );
    }
    const currentRule = `${this.draft.manifest}|${this.draft.rulesetId}|${this.draft.rulesetVersion}`;
    if ([...rulePicker.options].some((option) => option.value === currentRule)) rulePicker.value = currentRule;
    // 下拉里已经显示一条规则，就用它：表单显示什么，创建时就用什么（不然用户会以为选了）
    if (!this.draft.rulesetId && usable.length) {
      const first = usable[0];
      this.draft.manifest = String(first.manifest_path ?? "");
      this.draft.rulesetId = String(first.ruleset_id ?? "");
      this.draft.rulesetVersion = String(first.ruleset_version ?? "");
    }
    rulePicker.addEventListener("change", () => {
      const [manifest, rulesetId, version] = rulePicker.value.split("|");
      this.draft.manifest = manifest;
      this.draft.rulesetId = rulesetId;
      this.draft.rulesetVersion = version;
      void this.render();
    });
    if (!usable.length && this.draft.rulesetId) {
      rulePicker.appendChild(
        el("option", { value: currentRule, text: `${this.draft.rulesetId} ${this.draft.rulesetVersion}（未启用）` }),
      );
      rulePicker.value = currentRule;
    }

    // 角色多选（勾选=参与本战役的玩家角色）
    const charBox = el("div", { class: "u-rows" });
    for (const card of characters) {
      const id = String(card.card_id);
      const row = el("label", { class: "u-row-line" });
      const box = el("input", { type: "checkbox", id: `u-trpg-pc-${id}` }) as HTMLInputElement;
      box.checked = this.draft.participants.includes(id);
      box.addEventListener("change", () => {
        this.draft.participants = box.checked
          ? [...this.draft.participants, id]
          : this.draft.participants.filter((item) => item !== id);
      });
      row.appendChild(box);
      row.appendChild(el("span", { class: "u-grow", text: `${String(card.name)}${card.occupation ? ` · ${String(card.occupation)}` : ""}` }));
      charBox.appendChild(row);
    }
    if (!characters.length) charBox.appendChild(paragraph("这个世界里还没有可用角色卡。", "u-hint"));

    const sceneName = el("input", { class: "u-input", id: "u-trpg-scene-name", value: this.draft.sceneName, placeholder: "例如：夜里的北堤" }) as HTMLInputElement;
    const sceneBrief = el("textarea", { class: "u-textarea", rows: "2", id: "u-trpg-scene-brief", placeholder: "公开简介（玩家可见）" }) as HTMLTextAreaElement;
    sceneBrief.value = this.draft.sceneBrief;
    const sceneKind = el("select", { class: "u-input", id: "u-trpg-scene-kind" }) as HTMLSelectElement;
    for (const kind of ["exploration", "conflict", "social", "downtime"]) {
      sceneKind.appendChild(el("option", { value: kind, text: kind }));
    }
    sceneKind.value = this.draft.sceneKind;
    const sceneLocation = el("input", { class: "u-input", id: "u-trpg-scene-location", value: this.draft.sceneLocation, placeholder: "地点（世界里的登记对象标识或名称）" }) as HTMLInputElement;
    const scenePrivate = el("textarea", { class: "u-textarea", rows: "2", id: "u-trpg-scene-private", placeholder: "仅主持说明（默认不公开）" }) as HTMLTextAreaElement;
    scenePrivate.value = this.draft.scenePrivate;
    for (const [node, key] of [[sceneName, "sceneName"], [sceneBrief, "sceneBrief"], [sceneLocation, "sceneLocation"], [scenePrivate, "scenePrivate"]] as Array<[HTMLElement, keyof typeof this.draft]>) {
      node.addEventListener("input", () => {
        (this.draft as Record<string, unknown>)[key as string] = (node as HTMLInputElement).value;
      });
    }
    sceneKind.addEventListener("change", () => {
      this.draft.sceneKind = sceneKind.value;
    });

    host.appendChild(
      section(
        "新建战役",
        paragraph("按顺序走完五项就能开始：名称 → 世界与时间线 → 规则 → 角色 → 开场场景。中间任何一步都可以先「保存为准备中」。"),
        field("战役名称", name),
        field("世界", instancePicker),
        field("时间线", timelinePicker),
        String(timeline.state) === "active"
          ? paragraph(
              "这条时间线正在运行：同线的联络与其他战役都会受影响。默认建议另开一条跑团时间线（新线先暂停）。",
              "u-hint",
            )
          : paragraph("新分支与新时间线都先暂停；「开始战役」会同时启动这条线。", "u-hint"),
        String(timeline.state) === "active"
          ? button("另开一条跑团时间线…", () => void this.forkTimelineForPlay())
          : null,
        field("规则与版本", rulePicker),
        usable.length
          ? paragraph("登记过的规则都在这里；规则名与版本分别登记，缺失或不适配的不会出现在这一栏。", "u-hint")
          : paragraph("本机还没有可用规则：先「从本机选择规则目录」登记一份（样例规则在 examples/tide_rules_plugin）。", "u-hint"),
        button("从本机选择规则目录…", () => void this.addRuleFromDisk()),
        paragraph(
          "角色属性来自规则插件自己声明的初始化材料；没有适配的插件时这里不做自动建卡，只按已登记的角色参与。",
          "u-hint",
        ),
        charBox,
        section("开场场景", field("场景名称", sceneName), field("公开简介", sceneBrief), field("场景类型", sceneKind), field("地点", sceneLocation), field("仅主持说明", scenePrivate)),
        (() => {
          const box = el("div", { id: "u-trpg-summary" });
          const paint = (): void => {
            fill(
              box,
              facts([
                ["战役名称", displayName(this.draft.name, "", "（还没写名字）")],
                ["世界", instances.find((item) => item.id === this.draft.instanceId)?.name ?? ""],
                ["时间线", `${String(this.draft.timelineId)}`],
                ["规则与版本", this.draft.rulesetId ? `${String(this.draft.rulesetId)} ${String(this.draft.rulesetVersion)}` : "（还没选）"],
                ["活动角色", this.draft.participants.length ? this.draft.participants.join("、") : "（还没选）"],
                ["开场场景", displayName(this.draft.sceneName, "", "（还没写名字）")],
              ]),
            );
          };
          paint();
          for (const node of [name, sceneName]) node.addEventListener("input", paint);
          return section("创建摘要", box, paragraph("这段话就是新建战役的结果，确认前不会有任何世界变化。", "u-hint"));
        })(),
        el(
          "div",
          { class: "u-row" },
          primary("开始战役", () => void this.createCampaign("active")),
          button("保存为准备中", () => void this.createCampaign("preparing")),
          button("返回", () => {
            this.view = "list";
            void this.render();
          }),
        ),
      ),
    );
  }

  private async forkTimelineForPlay(): Promise<void> {
    try {
      const commits = await this.ctx.api.commits(this.draft.instanceId, this.draft.timelineId);
      const head = String(((commits.commits as Json[]) ?? [])[0]?.id ?? "");
      if (!head) {
        setNote(this.note, "这条线还没有版本点：先在世界与素材里保存一个，再从它另开跑团线", "bad");
        return;
      }
      const result = await this.ctx.api.waBranch({
        instance_id: this.draft.instanceId,
        timeline_id: this.draft.timelineId,
        commit_id: head,
        name: "跑团线",
      });
      const timeline = (result.timeline as Json) ?? {};
      this.draft.timelineId = String(timeline.id ?? "");
      setNote(this.note, `已另开一条跑团时间线「${String(timeline.name ?? "")}」（暂停，开始战役时启动）`, "ok");
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "跑团", action: "另开时间线" }).message, "bad");
    }
  }

  private async createCampaign(status: string): Promise<void> {
    if (!this.draft.name.trim()) {
      setNote(this.note, "战役要有名字：它保存在战役自己的元数据里，之后可改", "bad");
      return;
    }
    if (!this.draft.rulesetId) {
      setNote(this.note, "先选规则与版本：核心只在规则版本、参与者、初始状态和场景都合法时才允许开始", "bad");
      return;
    }
    setNote(this.note, status === "active" ? "正在创建战役…" : "正在保存准备中的战役…", "pending");
    try {
      const created = await this.ctx.api.trpgCampaignCreate({
        instance_id: this.draft.instanceId,
        timeline_id: this.draft.timelineId,
        name: this.draft.name.trim(),
        ruleset_id: this.draft.rulesetId,
        ruleset_version: this.draft.rulesetVersion,
        plugin_manifest: this.draft.manifest,
        participants: this.draft.participants,
        status,
        note: "界面新建",
        scene: this.draft.sceneName.trim() || this.draft.sceneBrief.trim()
          ? {
              kind: this.draft.sceneKind,
              name: this.draft.sceneName.trim(),
              brief: this.draft.sceneBrief.trim(),
              location_refs: this.draft.sceneLocation.trim() ? [this.draft.sceneLocation.trim()] : [],
              participants: this.draft.participants,
              private_views: this.draft.scenePrivate.trim() ? { gm_only: this.draft.scenePrivate.trim() } : {},
            }
          : null,
      });
      if (status === "active" && String(this.draft.timelineId) && String((this.campaigns[0] ?? {}).status ?? "") !== "") {
        // 开始战役同时明确启动目标线（§8.1）：冻结线要显式激活，不然世界不动
        try {
          await this.ctx.api.activate(this.draft.instanceId, this.draft.timelineId);
        } catch {
          /* 已经是运行中的线：不影响战役本身 */
        }
      }
      setNote(
        this.note,
        status === "active"
          ? `战役「${String(created.name ?? this.draft.name)}」已开始：${String(created.status ?? "")}`
          : `已保存为准备中：${String(created.name ?? this.draft.name)}（核心只在规则版本、参与者、初始状态与场景都合法时才允许开始）`,
        "ok",
      );
      await this.open(this.draft.instanceId, this.draft.timelineId, String(created.campaign_id));
    } catch (error) {
      setNote(
        this.note,
        uiError(error, {
          module: "跑团",
          action: status === "active" ? "开始战役" : "保存为准备中",
          done: "世界没有被删改；准备记录可以再存一次",
        }).message,
        "bad",
      );
    }
  }

  /* ------------------------------------------------------------ §8.2–8.3 场景与行动 */

  private async open(instanceId: string, timelineId: string, campaignId: string): Promise<void> {
    this.instanceId = instanceId;
    this.timelineId = timelineId;
    this.campaignId = campaignId;
    this.view = "play";
    this.workspace = null;
    if (!this.characterId) {
      // 当前角色 = 这局的活动角色（缺了行动者，行动只能停在「补充行动」）
      try {
        const info = await this.ctx.api.trpgCampaignInfo(instanceId, timelineId, campaignId);
        const participants = (info.participants as string[]) ?? [];
        this.actorOptions = participants.map((item) => String(item));
        this.characterId = String(participants[0] ?? "");
      } catch {
        this.characterId = "";
      }
      if (!this.characterId) {
        try {
          const world = await this.ctx.api.instanceInfo(instanceId);
          const cards = ((world.characters as Json[]) ?? []).map((item) => String(item.card_id));
          if (!this.actorOptions.length) this.actorOptions = cards;
          this.characterId = String(cards[0] ?? "");
        } catch {
          this.characterId = "";
        }
      }
    }
    await this.refresh();
  }

  private async refresh(): Promise<void> {
    setNote(this.note, "正在读取局面…", "pending");
    try {
      const args: Json = {
        instance_id: this.instanceId,
        timeline_id: this.timelineId,
        campaign_id: this.campaignId,
        mode: this.mode,
      };
      if (this.characterId) args.character_id = this.characterId;
      if (this.workspace) args.workspace = this.workspace;
      const result = await this.ctx.api.trpgClient("enter", args);
      this.bundle = result;
      this.workspace = (result.workspace as Json) ?? this.workspace;
      const gates = ((result.faces as Json)?.gates as Json) ?? {};
      const violations = (gates.violations as Json[]) ?? [];
      setNote(
        this.note,
        violations.length ? `这一面有 ${violations.length} 处不该出现的内容，已按受众契约拦下` : "局面已读取",
        violations.length ? "bad" : "ok",
      );
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "跑团", action: "读取局面" }).message, "bad");
    }
  }

  private renderPlay(host: HTMLElement): void {
    const bundle = this.bundle;
    const faces = ((bundle?.faces as Json) ?? {}) as Json;
    const campaign = (faces.campaign as Json) ?? {};
    const scene = (faces.scene as Json) ?? {};
    const next = (faces.next as Json) ?? {};
    host.appendChild(
      el(
        "div",
        { class: "u-row u-row-wrap" },
        el("h2", { class: "u-h2 u-grow", text: displayName(campaign.display_name ?? campaign.name, this.campaignId, "未命名战役") }),
        chip(`视角：${this.mode === "gm" ? "主持" : "玩家"}`, "muted"),
        chip(STATUS_TEXT[String(campaign.status)] ?? String(campaign.status), "pending"),
        button("主持准备", () => void this.switchMode(this.mode === "gm" ? "player" : "gm")),
        button("刷新局面", () => void this.refresh()),
        button("返回战役列表", () => {
          this.view = "list";
          this.campaigns = [];
          void this.render();
        }),
      ),
    );
    host.appendChild(
      paragraph(
        `时间线 ${this.timelineId}｜世界时刻 ${String(((faces.world as Json) ?? {}).revision ?? "")}；世界时间与现实时间分开看。`,
        "u-hint",
      ),
    );
    // 现在走到哪一步：核心只给阶段名，用一条轨把「写下行动 → 确认卡 → 裁定 → 写入世界」画出来
    const rail = this.actionRail(bundle);
    if (rail) host.appendChild(rail);
    host.appendChild(
      section(
        "当前场景",
        facts([
          [
            "场景名",
            String(
              (scene.scene as Json)?.scene_id
                ? displayName((scene.scene as Json)?.title, String((scene.scene as Json)?.scene_id), "未命名场景")
                : "（还没有开场场景）",
            ),
          ],
          ["场地", ((scene.scene as Json)?.location_refs as string[])?.join("、") || "（未注明）"],
          ["在场者", ((scene.scene as Json)?.participants as string[])?.join("、") || "（未注明）"],
          ["推进节拍", NEXT_KIND[String((scene.scene as Json)?.advance_mode ?? "")] ?? String((scene.scene as Json)?.advance_mode ?? "")],
        ]),
        paragraph(String((scene.scene as Json)?.description ?? "") || "（没有公开简介）", "u-hint"),
        bulletList(((scene.public_facts as Json[]) ?? []).map((item) => `已知：${String(item.text ?? item.summary ?? item)}`), "u-list"),
        bulletList(((scene.unknowns as Json[]) ?? []).map((item) => `未知：${String(item.text ?? "")}`).slice(0, 6), "u-list"),
        bulletList(((scene.risks as Json[]) ?? []).map((item) => `风险：${String(item.text ?? item.summary ?? item)}`).slice(0, 6), "u-list"),
      ),
    );
    host.appendChild(
      section(
        "待处理",
        paragraph(`${String(next.next ?? "")}${next.locks_reason ? `（${String(next.locks_reason)}）` : ""}`),
        scene.choice_count
          ? this.renderChoice((scene.next_choice as Json) ?? {})
          : paragraph("没有等待处理的选择。", "u-hint"),
        ((scene.unfinished_actions as Json[]) ?? []).length
          ? bulletList(
              ((scene.unfinished_actions as Json[]) ?? []).map(
                (item) => `行动 ${shortId(String(item.action_id))}：${String((item.state as Json)?.label ?? item.status)}`,
              ),
              "u-list",
            )
          : paragraph("没有未完成的行动。", "u-hint"),
      ),
    );
    host.appendChild(
      section(
        "过去结果",
        ((scene.action_results as Json[]) ?? []).length
          ? bulletList(
              ((scene.action_results as Json[]) ?? []).map(
                (item) => `行动 ${shortId(String(item.action_id))}：${String((item.state as Json)?.label ?? item.status)}`,
              ),
              "u-list",
            )
          : paragraph("这个场景还没有走过的行动。", "u-hint"),
        // 结果的层次是一句话说不清的东西：按固定顺序画出来，再说一句「没提交成功就不算发生」
        flowRail(RESULT_STEPS, -1),
        paragraph("结果按这个顺序出现；世界后果没提交成功时不会写成已经发生，也不跳级。", "u-hint"),
      ),
    );
    const actor = el("select", { class: "u-input", id: "u-trpg-actor" }) as HTMLSelectElement;
    const actorOptions = this.actorOptions.length ? this.actorOptions : [this.characterId].filter(Boolean);
    for (const id of actorOptions) actor.appendChild(el("option", { value: id, text: id }));
    const preActor = this.actorId || this.characterId;
    if (preActor && actorOptions.includes(preActor)) actor.value = preActor;
    actor.addEventListener("change", () => {
      this.actorId = actor.value;
    });
    const targetInput = el("input", { class: "u-input", id: "u-trpg-target", placeholder: "已知对象（可留空）" }) as HTMLInputElement;
    targetInput.value = this.targetText;
    targetInput.addEventListener("input", () => {
      this.targetText = targetInput.value;
    });
    const methodInput = el("input", { class: "u-input", id: "u-trpg-method", placeholder: "怎么做（可留空）" }) as HTMLInputElement;
    methodInput.value = this.methodText;
    methodInput.addEventListener("input", () => {
      this.methodText = methodInput.value;
    });
    host.appendChild(
      section(
        "我想……",
        field(
          "行动",
          el("textarea", {
            class: "u-textarea", rows: "2", id: "u-trpg-intent", placeholder: "用一句话说清你想做什么",
            value: this.actionText,
          }) as HTMLTextAreaElement,
        ),
        el(
          "div",
          { class: "u-row u-row-wrap" },
          field("行动者", actor),
          field("目标", targetInput),
          field("方法", methodInput),
        ),
        paragraph("行动者与目标取自当前局面；补充信息只填空项，你写过的不会被静默替换。", "u-hint"),
        el(
          "div",
          { class: "u-row" },
          primary("查看行动确认卡", () => void this.declareAction(false)),
          button("确认并裁定", () => void this.declareAction(true)),
        ),
        this.draftCard(),
      ),
    );
    this.renderActions(host, bundle ?? {});
    if (this.mode === "gm") this.renderGm(host, faces);
  }

  /** 行动走到哪一步（§8.2/§8.3）：按当前局面判断，不拿核心的阶段名当进度 */
  private actionRail(bundle: Json | null): HTMLElement | null {
    if (!bundle) return null;
    const faces = ((bundle.faces as Json) ?? {}) as Json;
    const scene = (faces.scene as Json) ?? {};
    const draft = (bundle.draft as Json) ?? null;
    const gaps = ((draft?.gaps as string[]) ?? []).length;
    const unfinished = ((scene.unfinished_actions as Json[]) ?? []).length;
    const current = bundle.committed ? 3 : unfinished ? 2 : draft ? (gaps ? 0 : 1) : 0;
    return flowRail(ACTION_STEPS, current);
  }

  /** 行动确认卡（§8.2/§8.3）：行动者 / 目标 / 方法 / 已知代价 / 缺什么，缺了就不给确认。 */
  private draftCard(): HTMLElement {
    const draft = ((this.bundle?.draft as Json) ?? null) as Json | null;
    if (!draft) {
      return section(
        "行动确认卡",
        paragraph("还没有草稿：写下行动、填上行动者与目标，点「查看行动确认卡」。", "u-hint"),
      );
    }
    const fields = ((draft.fields as Json) ?? {}) as Json;
    const sources = ((draft.sources as Json) ?? {}) as Json;
    const gaps = ((draft.gaps as string[]) ?? []).map((item) => String(item));
    const risks = ((draft.risks as string[]) ?? []).map((item) => String(item));
    const sourceText = (key: string): string => {
      const where = String(sources[key] ?? "");
      return where === "user" ? "你填的" : where === "model" ? "由 AI 补的" : where === "uncertain" ? "待你确认" : "";
    };
    return section(
      "行动确认卡",
      facts([
        ["行动者", `${String(fields.actor ?? "") || "（未定）"}${sourceText("actor") ? `｜${sourceText("actor")}` : ""}`],
        ["目标", `${String(fields.target ?? "") || "（未定）"}${sourceText("target") ? `｜${sourceText("target")}` : ""}`],
        ["方法", `${String(fields.method ?? "") || "（未定）"}${sourceText("method") ? `｜${sourceText("method")}` : ""}`],
        ["打算", String(fields.intent ?? "") || "（未定）"],
        ["已知代价", risks.length ? "由规则裁定（没有真实裁定就不给估计）" : "（未列出）"],
      ]),
      gaps.length
        ? bulletList(gaps.map((item) => `缺：${item}`), "u-list")
        : paragraph("确认卡齐了：可以点「确认并裁定」。", "u-hint"),
      paragraph(
        String(this.bundle?.blocked ?? "") || "没有真实裁定之前，这里不会出现骰点或成功字样，也不会提前写成世界已经改变。",
        "u-hint",
      ),
    );
  }

  private renderChoice(choice: Json): HTMLElement {
    const box = el("article", { class: "u-card" });
    box.appendChild(el("h3", { text: `请先处理这项选择：${String(choice.prompt ?? choice.title ?? "选择")}` }));
    const options = ((choice.choices as Json[]) ?? []) as Json[];
    const picker = el("select", { class: "u-input", id: "u-trpg-choice" }) as HTMLSelectElement;
    for (const option of options) {
      picker.appendChild(
        el("option", { value: String(option.id ?? option.value ?? ""), text: String(option.label ?? option.text ?? option.id ?? "") }),
      );
    }
    box.appendChild(field("选项", picker));
    box.appendChild(paragraph("选择会以回执锁定；双击或断线重连不会重复消费。选定后重新读取局面，再声明后续行动。", "u-hint"));
    box.appendChild(
      button("确认选择", () => {
        void (async () => {
          try {
            const result = await this.ctx.api.trpgClient("choice", {
              instance_id: this.instanceId, timeline_id: this.timelineId, campaign_id: this.campaignId,
              mode: this.mode, workspace: this.workspace,
              choice_id: String(choice.choice_id ?? ""), selection: picker.value,
            });
            this.workspace = (result.workspace as Json) ?? this.workspace;
            this.bundle = result;
            setNote(this.note, "选择已记录；当前选择登记不保证自动执行它对应的世界后果", "muted");
            await this.refresh();
          } catch (error) {
            setNote(this.note, uiError(error, { module: "跑团", action: "确认选择" }).message, "bad");
          }
        })();
      }),
    );
    return box;
  }

  private async declareAction(confirm: boolean): Promise<void> {
    const text = String((this.host?.querySelector("#u-trpg-intent") as HTMLTextAreaElement | null)?.value ?? "").trim();
    this.actionText = text;
    if (!text && !this.lastResults) {
      setNote(this.note, "先写下你想做什么", "bad");
      return;
    }
    setNote(this.note, confirm ? "正在确认并裁定…" : "正在整理行动确认卡…", "pending");
    try {
      const actorId = String(
        (this.host?.querySelector("#u-trpg-actor") as HTMLSelectElement | null)?.value ?? this.actorId,
      );
      const target = String(
        (this.host?.querySelector("#u-trpg-target") as HTMLInputElement | null)?.value ?? this.targetText,
      ).trim();
      const method = String(
        (this.host?.querySelector("#u-trpg-method") as HTMLInputElement | null)?.value ?? this.methodText,
      ).trim();
      this.actorId = actorId || this.actorId;
      this.targetText = target;
      this.methodText = method;
      // 确认卡的字段名是 actor / target / method / intent（draft.CARD_FIELDS），
      // 缺 target 与 intent 就打不开卡——别拿行动 op 的 actor_id / target_refs 去顶
      const fields: Json = {};
      if (actorId) fields.actor = actorId;
      if (target) fields.target = target;
      if (method) fields.method = method;
      if (text) fields.intent = text;
      const result = await this.ctx.api.trpgClient("act", {
        instance_id: this.instanceId, timeline_id: this.timelineId, campaign_id: this.campaignId,
        mode: this.mode, workspace: this.workspace, text, confirm,
        ...(this.actionId ? { action_id: this.actionId } : {}),
        ...(Object.keys(fields).length ? { fields } : {}),
      });
      this.workspace = (result.workspace as Json) ?? this.workspace;
      const nextActionId = String(
        (result.action_id as string) ?? ((result.faces as Json)?.next as Json)?.action_id ?? "",
      );
      if (nextActionId) this.actionId = nextActionId;
      this.bundle = result;
      this.lastResults = result;
      const stage = String(result.stage ?? "");
      // 「阶段」这个词对用户没意义：把「现在停在哪一步」说人话，理由用核心给的原文
      const committed = Boolean(result.committed);
      const rail = this.actionRail(result);
      const stopped = String(rail?.querySelector(".u-rail-current .u-rail-label")?.textContent ?? "") || stage;
      const why = String(result.skipped ?? "") || String(((result.errors as string[]) ?? [])[0] ?? "");
      setNote(
        this.note,
        confirm
          ? committed
            ? "裁定与后果已固化；世界里已经按它变化"
            : `规则给了结果，世界还没变：这一轮停在「${stopped}」${why ? `——${why}` : "（没有真实裁定就不显示骰点，也不说成功）"}`
          : `行动确认卡已就绪（阶段 ${stage}）`,
        confirm ? (committed ? "ok" : "pending") : "muted",
      );
      await this.render();
    } catch (error) {
      setNote(
        this.note,
        uiError(error, { module: "跑团", action: confirm ? "确认并裁定" : "整理行动" }).message,
        "bad",
      );
    }
  }

  /** §8.3：按当前阶段给主动作 + 操作保证（表驱动，别让用户猜输入框为什么没反应）。 */
  private renderActions(host: HTMLElement, bundle: Json): void {
    const stage = String(bundle.stage ?? "");
    const rows: Array<[string, string]> = [];
    const push = (missing: boolean, name: string, guarantee: string): void => {
      if (missing) rows.push([name, guarantee]);
    };
    push(stage === "needs_input", "补充行动", "模型只补空项；你填过的行动者、目标与方法不被静默替换");
    push(stage === "awaiting_confirmation", "确认并裁定", "改任何关键项都会让旧确认失效");
    push(stage === "submitting" || stage === "resolving", "查看阶段", "不取消后自动重掷；切页不会发第二次裁定");
    push(stage === "unknown", "查询原结果", "同操作身份核验；结果未明时不重新裁定");
    push(stage === "plugin_failed", "恢复已保存结果 / 重新裁定", "有结果先恢复；重做可能改变随机结果，需要你明确确认");
    push(Boolean((bundle.faces as Json)?.stale), "重新读取并确认", "旧结果留作记录，重新检查后生成新的确认卡");
    if (!rows.length) {
      host.appendChild(
        section("这一步能做什么", paragraph("按上面的按钮走：声明行动 → 查看确认卡 → 确认并裁定；有待选择时先处理选择。", "u-hint")),
      );
      return;
    }
    host.appendChild(
      section(
        "这一步能做什么",
        bulletList(rows.map(([name, guarantee]) => `${name}：${guarantee}`), "u-list"),
        paragraph("这些是当前阶段允许的动作；不允许的动作不显示，避免用按钮绕过校验。", "u-hint"),
      ),
    );
  }

  /* ------------------------------------------------------------ §8.4 主持面 */

  private async switchMode(mode: "player" | "gm"): Promise<void> {
    try {
      const result = await this.ctx.api.trpgClient("switch", {
        instance_id: this.instanceId, timeline_id: this.timelineId, campaign_id: this.campaignId,
        mode, workspace: this.workspace, character_id: this.characterId,
      });
      this.mode = mode;
      this.workspace = (result.workspace as Json) ?? this.workspace;
      const faces = ((result.faces as Json) ?? {}) as Json;
      if (mode === "gm" && !faces.gm) {
        // 切换没带回主持面：老老实实再读一次局面，而不是拿玩家面冒充主持面
        await this.refresh();
        return;
      }
      this.bundle = result;
      setNote(
        this.note,
        mode === "gm"
          ? "已切到主持视图：这里能看到待审与规则依据，但发布出去的仍是按受众裁过的表达"
          : "已切回玩家视图：受众投影重新取过，主持缓存已丢",
        "muted",
      );
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "跑团", action: "切换视角" }).message, "bad");
    }
  }

  private renderGm(host: HTMLElement, faces: Json): void {
    const gm = (faces.gm as Json) ?? {};
    const queue = (gm.pending as Json[]) ?? [];
    const rows = el("div", { class: "u-rows" });
    for (const item of queue) {
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: `行动 ${shortId(String(item.action_id))}｜${String(item.status ?? "")}` }));
      const canApprove = Boolean(item.committable);
      row.appendChild(
        canApprove
          ? button("批准并提交", () => void this.reviewAction(String(item.action_id ?? ""), "approve"))
          : chip("材料不足：只能补充、修改或拒绝", "muted"),
      );
      row.appendChild(button("暂存待审", () => void this.reviewAction(String(item.action_id ?? ""), "hold")));
      row.appendChild(button("拒绝", () => void this.reviewAction(String(item.action_id ?? ""), "reject")));
      rows.appendChild(row);
    }
    if (!queue.length) rows.appendChild(paragraph("没有待审行动。", "u-hint"));
    host.appendChild(
      section(
        "主持准备 · 待处理行动",
        rows,
        paragraph("只有仍有效、且有可提交载荷的结果才显示「批准并提交」；没有万能批准按钮。", "u-hint"),
      ),
    );
    host.appendChild(
      section(
        "直接变化",
        paragraph("要直接改写世界，走「预览 → 确认」这条路：预览不改世界，确认后才落成事实。"),
        el(
          "div",
          { class: "u-row" },
          button("打开变化预览…", () => void this.gmChange()),
          button("交给写作助手构思", () => this.ctx.navigate({ pane: "writing" })),
        ),
      ),
    );
    host.appendChild(
      section(
        "场景准备",
        paragraph("新场景的开场材料（名称 / 公开简介 / 地点 / 在场者 / 风险 / 可行动作）与「仅主持」说明分开填。"),
        button("准备下一个场景…", () => void this.openSceneForm()),
      ),
    );
    host.appendChild(
      section(
        "暂停战役",
        paragraph("暂停默认只停这一局的行动；世界仍可能继续运行。同线联络与其他战役会受影响，是否连时间线一起暂停要单独选。"),
        el(
          "div",
          { class: "u-row" },
          button("只暂停战役", () => void this.setCampaignStatus("paused")),
          button("暂停战役并暂停这条时间线", () => void this.pauseWithTimeline()),
        ),
      ),
    );
  }

  private async reviewAction(actionId: string, decision: string): Promise<void> {
    try {
      const result = await this.ctx.api.trpgClient("review", {
        instance_id: this.instanceId, timeline_id: this.timelineId, campaign_id: this.campaignId,
        mode: "gm", workspace: this.workspace, action_id: actionId, decision,
      });
      this.workspace = (result.workspace as Json) ?? this.workspace;
      this.bundle = result;
      setNote(this.note, `已记下主持决定：${decision}`, "ok");
      await this.refresh();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "主持准备", action: "记下决定" }).message, "bad");
    }
  }

  private async gmChange(): Promise<void> {
    const kind = el("input", { class: "u-input", id: "u-trpg-change-kind", placeholder: "变化类别（如 condition / state_change）" }) as HTMLInputElement;
    const target = el("input", { class: "u-input", id: "u-trpg-change-target", placeholder: "对象（世界里的登记标识或名称）" }) as HTMLInputElement;
    const value = el("input", { class: "u-input", id: "u-trpg-change-value", placeholder: "变化后的值" }) as HTMLInputElement;
    const reason = el("input", { class: "u-input", id: "u-trpg-change-reason", placeholder: "为什么这么改（必填）" }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const modal = dialog(
      "直接变化",
      [
        field("变化类别", kind),
        field("对象", target),
        field("变化后的值", value),
        field("理由", reason),
        paragraph("先预览：预览不改世界；确认后走联合提交，世界与规则状态同批成功或同批失败。", "u-hint"),
        note,
      ],
      [
        {
          label: "预览",
          run: () => {
            void (async () => {
              try {
                const result = await this.ctx.api.trpgClient("gm_change", {
                  instance_id: this.instanceId, timeline_id: this.timelineId, campaign_id: this.campaignId,
                  mode: "gm", workspace: this.workspace, preview_only: true,
                  form: {
                    target_ref: target.value.trim(), kind: kind.value.trim(), value: value.value.trim(),
                    reason: reason.value.trim(), audience: "gm_only",
                    idempotency_key: `ui-${Date.now().toString(36)}`,
                  },
                });
                setNote(this.note, `预览完成：${String(result.stage ?? "")}（世界未被改动）`, "muted");
                this.bundle = result;
                await this.render();
              } catch (error) {
                setNote(note, uiError(error, { module: "直接变化", action: "预览" }).message, "bad");
              }
            })();
          },
        },
        {
          label: "确认提交",
          run: () => {
            void (async () => {
              try {
                const result = await this.ctx.api.trpgClient("gm_change", {
                  instance_id: this.instanceId, timeline_id: this.timelineId, campaign_id: this.campaignId,
                  mode: "gm", workspace: this.workspace,
                  form: {
                    target_ref: target.value.trim(), kind: kind.value.trim(), value: value.value.trim(),
                    reason: reason.value.trim(), audience: "public_party",
                    idempotency_key: `ui-${Date.now().toString(36)}`,
                  },
                });
                this.workspace = (result.workspace as Json) ?? this.workspace;
                this.bundle = result;
                setNote(this.note, "直接变化已提交：世界里已按它变化（同一批成功或同批失败）", "ok");
                await this.refresh();
              } catch (error) {
                setNote(note, uiError(error, { module: "直接变化", action: "确认提交" }).message, "bad");
              }
            })();
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  private async openSceneForm(): Promise<void> {
    const name = el("input", { class: "u-input", id: "u-trpg-newscene-name", placeholder: "场景名称" }) as HTMLInputElement;
    const brief = el("textarea", { class: "u-textarea", rows: "2", id: "u-trpg-newscene-brief", placeholder: "公开简介" }) as HTMLTextAreaElement;
    const location = el("input", { class: "u-input", id: "u-trpg-newscene-location", placeholder: "地点（登记对象）" }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const modal = dialog(
      "准备下一个场景",
      [
        field("场景名称", name),
        field("公开简介", brief),
        field("地点", location),
        paragraph("录入场景描述本身不改变世界；真的有世界变化时另外走「直接变化」的预览与确认。", "u-hint"),
        note,
      ],
      [
        {
          label: "开这个场景",
          run: () => {
            void (async () => {
              try {
                await this.ctx.api.trpgSceneOpen({
                  instance_id: this.instanceId, timeline_id: this.timelineId, campaign_id: this.campaignId,
                  name: name.value.trim(), brief: brief.value.trim(),
                  location_refs: location.value.trim() ? [location.value.trim()] : [],
                  participants: [],
                });
                setNote(this.note, "新场景已开；这是战役的编排态，不是世界变化", "ok");
                await this.refresh();
              } catch (error) {
                setNote(note, uiError(error, { module: "场景准备", action: "开新场景" }).message, "bad");
              }
            })();
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  private async setCampaignStatus(status: string): Promise<void> {
    try {
      const result = await this.ctx.api.trpgCampaignStatus(this.instanceId, this.timelineId, this.campaignId, status);
      setNote(
        this.note,
        status === "paused"
          ? "战役已暂停：世界仍可能继续运行；要连时间线一起停，用旁边那个按钮"
          : `战役状态：${String((result as Json).status ?? status)}`,
        "ok",
      );
      await this.refresh();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "跑团", action: "改战役状态" }).message, "bad");
    }
  }

  private async pauseWithTimeline(): Promise<void> {
    await this.setCampaignStatus("paused");
    try {
      await this.ctx.api.freeze(this.instanceId, this.timelineId);
      setNote(this.note, "两步都成功：战役已暂停，这条时间线也已冻结（同线联络与其它战役同样受影响）", "ok");
    } catch (error) {
      setNote(
        this.note,
        `战役已暂停，但时间线没有停：${uiError(error, { module: "跑团", action: "暂停时间线" }).message}`,
        "bad",
      );
    }
  }
}
