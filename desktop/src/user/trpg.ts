/*
 * 跑团工作区（USER_INTERFACE_DESIGN §8.1–§8.5）。
 *
 * 三块：继续 / 新建战役（§8.1）、场景与行动（§8.2–8.3）、主持视图（§8.4）。
 * 一条硬规矩贯穿：没有按规则算出来的真实结果就不显示骰点、不说成功；世界变化只有真实提交过才算发生。
 * 规则与「外部聊天通道」分开：规则走 rules.* 登记簿（§8.5），不碰通道插件安装。
 */

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
  tools,
} from "./dom";
import { dotLine, flowRail, type FlowStep } from "./graphics";

type View = "list" | "create" | "play";

/**
 * 跑团工作区自己的上下文（§3.3）：世界 / 世界线 / 战役分别记住，形状照抄 `sel.contact`。
 * `campaign_id` 为空表示只记了「世界 + 世界线」（从世界详情点「在此跑团」进入的情形）。
 */
interface TrpgSelection {
  instance_id: string;
  timeline_id: string;
  campaign_id: string;
  campaign_name: string;
}

/** 一次行动在界面上走到哪一步（§8.2/§8.3）；写死的是流程，不是核心状态名 */
const ACTION_STEPS: FlowStep[] = [
  { label: "写下行动", hint: "行动、行动者与目标齐了才能打开确认卡" },
  { label: "确认卡齐备", hint: "改任何关键项都会让旧确认失效" },
  { label: "按规则算结果", hint: "不取消后自动重掷；切页不会算第二次" },
  { label: "写入世界", hint: "规则状态与世界后果同批成功或同批失败" },
];

/** 场景类型的中文名：界面上不给英文枚举（P1-9）。值是提交给核心的原始标识，不能改 */
const SCENE_KINDS: Array<[string, string]> = [
  ["exploration", "探索"],
  ["conflict", "冲突"],
  ["social", "社交"],
  ["downtime", "日常 / 间隙"],
];

/**
 * 核心阶段名 → 人话（P1-9）：只在正文里出现，值仍然是原始标识。
 * 这里查不到才回落到原始字符串，免得核心新增阶段时界面变空。
 */
const STAGE_TEXT: Record<string, string> = {
  needs_input: "等待你补充信息",
  awaiting_confirmation: "等待你确认",
  submitting: "正在提交",
  resolving: "正在按规则计算",
  unknown: "结果还没确认",
  plugin_failed: "规则包出错",
  committed: "已经进入世界",
};

function stageText(stage: string): string {
  return STAGE_TEXT[stage] ?? stage;
}

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

/**
 * 技术详情折叠区（P1-9）：内部标识、规则状态编号这类东西收在这里，正文只留名字与人话。
 * 没有内容时返回 null，不摆一个空折叠块。
 */
function techDetails(rows: Array<[string, string]>): HTMLElement | null {
  const kept = rows.filter(([, value]) => value);
  if (!kept.length) return null;
  return el("details", { class: "u-hint" }, el("summary", { text: "技术详情（排错时才需要看）" }), facts(kept));
}

/** 规则清单里有没有「已登记但不能用」的项：用来区分「没登记」与「登记了但停用 / 缺依赖」（P1-6） */
function registeredUnusableHint(plugins: Json[], rulesetId: string): boolean {
  return plugins.some((item) => {
    const status = String(item.status);
    if (status === "available" || status === "unregistered") return false;
    // 传了具体规则就只认它；没传（在列表里拦人）就看有没有任何一条不能用的
    return !rulesetId || String(item.ruleset_id) === rulesetId;
  });
}

/** 可重试的界面错误：给「随发行样例规则」这类有明确下一步的失败用（替代只有一句话的死路提示）。 */
function actionable(message: string, action: string): UiError {
  return {
    module: "跑团",
    action,
    target: "",
    stage: "",
    code: "bundled_rules_unavailable",
    message,
    retryable: true,
    done: "没有任何改动",
    unknown: "这次操作是否已生效",
    field: "",
    requestId: "",
  };
}

export class TrpgPane implements Pane {
  readonly id = "trpg";
  private host: HTMLElement | null = null;
  private note: HTMLElement | null = null;
  private view: View = "list";
  private campaigns: Json[] = [];
  private plugins: Json[] = [];
  /** 读不到战役清单的世界数（0 = 全读到）；部分失败时保留已读到的行，界面上如实说失败的那部分 */
  private campaignsFailed = 0;
  /** 最近一次战役清单读取失败的原因：全失败时用它说明，不冒充「还没有战役」 */
  private campaignsError: UiError | null = null;
  /** 规则清单读取失败：与「本机还没有登记规则插件」的真空态分开，不混同 */
  private pluginsError: UiError | null = null;
  /** 「从样例开始」失败时挂出的可行动错误卡（重试后就地替换，避免越堆越多） */
  private sampleErrorCard: HTMLElement | null = null;

  // 战役进行中的状态
  private instanceId = "";
  private timelineId = "";
  private campaignId = "";
  private characterId = "";
  private actorOptions: string[] = [];
  private mode: "player" | "gm" = "player";
  private workspace: Json | null = null;
  /** 上次选择读了但对象已不存在时，如实说明回落原因（不静默继承别的域） */
  private selectionHint = "";
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
    await this.adoptStoredSelection();
    await this.render();
  }

  /* ------------------------------------------------------------ 骨架 */

  private async render(): Promise<void> {
    const host = this.host;
    if (!host || !this.note) return;
    fill(host, this.note, this.selectionHint ? paragraph(this.selectionHint, "u-hint") : null);
    this.sampleErrorCard = null;
    try {
      if (this.view === "list") await this.renderList(host);
      else if (this.view === "create") await this.renderCreate(host);
      else this.renderPlay(host);
    } catch (error) {
      host.appendChild(errorCard(uiError(error, { module: "跑团", action: "打开工作区" })));
    }
  }

  /**
   * 挂载时读自己的 `sel.trpg`（§3.3）：本域选择为空才读；世界 / 时间线 / 战役都还在才采用。
   * 只记了世界与时间线（`campaign_id` 为空）时，把它当作新建战役的默认落点，不强行进主持界面；
   * 战役已不在时回落到战役列表并说明，不静默换到别的世界。
   */
  private async adoptStoredSelection(): Promise<void> {
    if (this.instanceId) return;
    const stored = (this.ctx.prefs["sel.trpg"] as Partial<TrpgSelection> | undefined) ?? undefined;
    const storedInstance = String(stored?.instance_id ?? "");
    if (!storedInstance) return;
    const instance = this.ctx.instances().find((item) => item.id === storedInstance);
    if (!instance) {
      this.selectionHint = "上次跑团的世界已经不在本机：已回到战役列表，请重新选择。";
      return;
    }
    const timelineId = String(stored?.timeline_id ?? "");
    const campaignId = String(stored?.campaign_id ?? "");
    if (!campaignId) {
      this.draft.instanceId = instance.id;
      this.draft.timelineId = timelineId;
      this.selectionHint = `已带入上次的世界「${instance.name}」：新建战役默认用它；要进原有战役仍从列表选。`;
      return;
    }
    try {
      const result = await this.ctx.api.trpgCampaigns(instance.id);
      const hit = ((result.campaigns as Json[]) ?? []).find((item) => String(item.campaign_id) === campaignId);
      if (!hit) {
        this.draft.instanceId = instance.id;
        this.draft.timelineId = timelineId;
        this.selectionHint = "上次的战役已经不在这个世界：已回到战役列表，请重新选择。";
        return;
      }
      await this.open(instance.id, String(hit.timeline_id ?? timelineId), campaignId, String(hit.name ?? ""));
    } catch {
      this.draft.instanceId = instance.id;
      this.draft.timelineId = timelineId;
      this.selectionHint = "没能确认上次的战役是否还在：已回到战役列表（世界与世界线已带入新建战役）。";
    }
  }

  private async loadCampaigns(): Promise<void> {
    const rows: Json[] = [];
    let failed = 0;
    let firstError: UiError | null = null;
    for (const instance of this.ctx.instances()) {
      try {
        const result = await this.ctx.api.trpgCampaigns(instance.id);
        for (const item of ((result.campaigns as Json[]) ?? [])) {
          rows.push({ ...item, instance_name: instance.name });
        }
      } catch (error) {
        // 单个世界读不到不影响别的世界；但界面要如实说失败的那部分，不把失败冒充成没战役
        failed += 1;
        firstError = firstError ?? uiError(error, { module: "跑团", action: "读取战役清单", target: instance.name });
      }
    }
    this.campaigns = rows;
    this.campaignsFailed = failed;
    this.campaignsError = firstError;
  }

  private async loadPlugins(): Promise<void> {
    try {
      const result = await this.ctx.api.rulesList();
      this.plugins = (result.plugins as Json[]) ?? [];
      this.pluginsError = null;
    } catch (error) {
      this.plugins = [];
      this.pluginsError = uiError(error, { module: "跑团", action: "读取规则清单" });
    }
  }

  /** 规则清单读取失败后的重试：重跑读取再重画 */
  private async retryPlugins(): Promise<void> {
    await this.loadPlugins();
    await this.render();
  }

  /** 战役清单读取失败后的重试：只重跑清单读取，不假装整页都成功了 */
  private async retryCampaigns(): Promise<void> {
    await this.loadCampaigns();
    await this.render();
  }

  /** 读取失败的行内说明：固定说法 + 真实原因 + 「重试」，不让失败与空态看起来一样 */
  private loadFailure(label: string, error: UiError | null, retry: () => void): HTMLElement {
    return el(
      "div",
      { class: "u-row u-row-wrap" },
      el("span", { class: "u-grow u-hint", text: `${label}：${error?.message ?? "原因未明"}` }),
      button("重试", retry),
    );
  }

  /* ------------------------------------------------------------ §8.1 继续 / 新建 */

  private async renderList(host: HTMLElement): Promise<void> {
    const newCampaign = (): void => {
      this.view = "create";
      void this.render();
    };
    // ① 标题带：与其它页同一个纵向位置。有世界可挂时主操作才是「新建战役」——
    // 一个世界都没有时它点进去只会是个死向导，那种情形交给下面的空态说清先做什么
    host.appendChild(
      pageHead(
        "跑团",
        "声明行动，按规则得到结果；没算出真实结果就不说成功",
        this.ctx.instances().length ? [primary("新建战役", newCampaign)] : [],
      ),
    );
    // 连世界都没有：先说这件事，不然「新建战役」会点进一个没有世界可选的向导
    if (!this.ctx.instances().length) {
      const emptyWorld = panel(
        "先有一个世界",
        paragraph("战役要挂在一个世界上：先创建，或从样例开始。"),
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
    if (!this.plugins.length && !this.pluginsError) await this.loadPlugins();
    if (!this.campaigns.length && !this.campaignsFailed) await this.loadCampaigns();
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
      head.appendChild(
        button("继续", () =>
          void this.open(String(item.instance_id), String(item.timeline_id), String(item.campaign_id), String(item.name ?? "")),
        ),
      );
      rows.appendChild(head);
      // 正文只留人话：世界名 / 时间线名 / 规则名与版本 / 你是玩家还是主持；内部标识收进技术详情
      const plugin = this.plugins.find(
        (entry) =>
          String(entry.ruleset_id) === String(item.ruleset_id) &&
          String(entry.ruleset_version) === String(item.ruleset_version),
      );
      const rulesetName = String(plugin?.name ?? "") || String(item.ruleset_id || "（未声明）");
      rows.appendChild(
        paragraph(
          `${String(item.instance_name ?? "")}｜世界线 ${String(item.timeline_name ?? item.timeline_id ?? "")}`
            + `｜规则 ${rulesetName} ${String(item.ruleset_version || "")}`
            + `｜${String(item.host_mode ?? "") === "assisted" ? "辅助主持（AI 给建议，你来定）" : "主持模式未声明"}`,
          "u-hint",
        ),
      );
      const tech = techDetails([
        ["战役编号", String(item.campaign_id ?? "")],
        ["世界线编号", String(item.timeline_id ?? "")],
        ["规则标识", String(item.ruleset_id ?? "")],
      ]);
      if (tech) rows.appendChild(tech);
    }
    if (this.campaignsFailed && !this.campaigns.length) {
      // 一条都没读到：别让「读取失败」看起来像「还没有战役」
      rows.appendChild(this.loadFailure("战役清单读取失败", this.campaignsError, () => void this.retryCampaigns()));
    } else if (this.campaignsFailed) {
      // 部分成功：已读到的行照常显示，另起一行如实说失败的世界数
      rows.appendChild(
        this.loadFailure(`另有 ${this.campaignsFailed} 个世界读取失败`, this.campaignsError, () => void this.retryCampaigns()),
      );
    }
    // 三块内容各归一块一级分区：列表 / 本机规则 / 外部聊天扩展。以前它们是同重量的盒子平铺
    const emptyCampaigns = !this.campaigns.length && !this.campaignsFailed;
    const campaignsPanel = panel(
      "继续已有战役",
      rows,
      // 一条战役都没有时，这两个动作就在下面的空态里，这里不再重复摆一排
      emptyCampaigns
        ? null
        : el("div", { class: "u-row" }, button("从样例开始", () => void this.startFromSample())),
      // 空态：说明压成一行 + 两个按钮，并把这块撑到主要视口高度，不在 663px 的页面里只画 200px
      emptyCampaigns
        ? el(
            "div",
            { class: "u-rows" },
            paragraph("还没有战役：新建一局，或先用随发行的样例材料跑一局。", "u-hint"),
            el(
              "div",
              { class: "u-row" },
              primary("新建战役", newCampaign),
              button("从样例开始", () => void this.startFromSample()),
            ),
          )
        : null,
    );
    if (emptyCampaigns) campaignsPanel.classList.add("u-fill");
    host.appendChild(campaignsPanel);
    const pluginRows = el("div", { class: "u-rows" });
    for (const item of this.plugins) {
      const line = el("div", { class: "u-row-line" });
      line.appendChild(el("span", { class: "u-grow", text: `${String(item.name || item.ruleset_id)} ${String(item.ruleset_version)}` }));
      const status = String(item.status);
      line.appendChild(
        chip(
          // 停用 / 依赖缺失是两回事：前者用户自己能在设置里启用，后者要修规则本身（P1-6）
          status === "available" ? "已启用" : status === "disabled" ? "已登记但停用" : String(item.status_text ?? status),
          status === "available" ? "ok" : status === "disabled" ? "pending" : "bad",
        ),
      );
      if (item.referenced_count) line.appendChild(chip(`被 ${Number(item.referenced_count)} 局使用`, "muted"));
      if (status === "disabled") {
        // 已登记但停用：这里就能启用，不用把用户支去别的页面
        line.appendChild(button("启用这条规则", () => void this.enableRule(String(item.ruleset_id ?? ""), String(item.ruleset_version ?? ""))));
      }
      pluginRows.appendChild(line);
    }
    if (this.pluginsError && !this.plugins.length) {
      // 读取失败与「本机还没有登记规则」是两回事：有错就不能显示原空态
      pluginRows.appendChild(this.loadFailure("规则清单读取失败", this.pluginsError, () => void this.retryPlugins()));
    } else if (!this.plugins.length) {
      pluginRows.appendChild(paragraph("本机还没有登记规则：可以先登记随发行的样例规则，或从本机选择规则目录。", "u-hint"));
    }
    // 规则包与「外部聊天扩展」不是一件事：用两块的标题 + 各自一个动作说清，不再写一句解释
    // （2026-10-08 审查第 8 节：用句子解释本可以用布局表达的概念）
    host.appendChild(
      panel(
        "本机规则（跑团规则包）",
        this.plugins.length
          ? dotLine(`${this.plugins.filter((item) => String(item.status) === "available").length} 条可用 / 共 ${this.plugins.length} 条已登记`, "ok")
          : null,
        pluginRows,
        el(
          "div",
          { class: "u-row" },
          button("从本机选择规则目录…", () => void this.addRuleFromDisk()),
        ),
      ),
    );
    host.appendChild(
      panel(
        "外部聊天扩展",
        paragraph("聊天通道、机器人这类扩展与跑团规则分开管，在设置里单独装与停用。", "u-hint"),
        el(
          "div",
          { class: "u-row" },
          button("打开设置里的扩展页", () => this.ctx.navigate({ pane: "settings", sub: "extensions" })),
        ),
      ),
    );
  }

  /** 启用一条已登记但停用的规则（P1-6）：失败如实说，成功重读清单让它回到可选状态 */
  private async enableRule(rulesetId: string, rulesetVersion: string): Promise<void> {
    if (!rulesetId) return;
    setNote(this.note, "正在启用这条规则…", "pending");
    try {
      await this.ctx.api.rulesEnable(true, rulesetId, rulesetVersion);
      await this.loadPlugins();
      setNote(this.note, "这条规则已启用：新建战役时可以选它了", "ok");
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "跑团", action: "启用规则" }).message, "bad");
    }
  }

  private async startFromSample(): Promise<void> {
    setNote(this.note, "正在准备样例战役材料…", "pending");
    try {
      if (!this.plugins.length) await this.loadPlugins();
      const instances = this.ctx.instances();
      if (!instances.length) {
        setNote(this.note, "还没有世界：先从样例世界开始，再回来建战役", "bad");
        this.ctx.navigate({ pane: "onboarding", sub: "sample" });
        return;
      }
      if (!this.plugins.some((item) => String(item.status) === "available")) {
        // 本机还没有可用规则：走「一键登记随发行样例规则」，不再让用户自己去找目录
        await this.useBundledRule(true);
        return;
      }
      await this.prefillSampleCampaign();
    } catch (error) {
      this.sampleError(
        "准备样例战役材料没有完成",
        uiError(error, { module: "跑团", action: "准备样例" }),
        () => void this.startFromSample(),
      );
    }
  }

  /** 「从样例开始」的预填：用一个可用规则加第一个世界起一局，规则仍来自本机登记。 */
  private async prefillSampleCampaign(): Promise<void> {
    if (!this.plugins.length) await this.loadPlugins();
    const instances = this.ctx.instances();
    if (!instances.length) {
      setNote(this.note, "还没有世界：先从样例世界开始，再回来建战役", "bad");
      this.ctx.navigate({ pane: "onboarding", sub: "sample" });
      return;
    }
    const sample = this.plugins.find((item) => String(item.status) === "available");
    if (!sample) {
      this.sampleError(
        "本机仍然没有可用规则",
        actionable("可以重试登记随发行样例规则，或从本机选择规则目录登记自己的规则。", "准备样例战役"),
        () => void this.startFromSample(),
      );
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
  }

  /**
   * 一键登记随发行样例规则（§8.5）：只读找样例 → 先给一句确认文案 → 显式登记。
   * 只在用户点「从样例开始」或「登记随发行样例规则」之后走这里，不做启动时静默注册。
   */
  private async useBundledRule(thenPrefill: boolean): Promise<void> {
    setNote(this.note, "正在查找随发行样例规则…", "pending");
    let bundled: Json;
    try {
      bundled = await this.ctx.api.rulesBundled();
    } catch (error) {
      this.sampleError(
        "没能读取随发行样例规则",
        uiError(error, { module: "跑团", action: "查找随发行样例规则" }),
        () => void this.useBundledRule(thenPrefill),
      );
      return;
    }
    const candidates = ((bundled.candidates as Json[]) ?? []).filter(
      (item) => String(item.status) === "available",
    );
    if (!candidates.length) {
      this.sampleError(
        "随发行样例规则没有找到",
        actionable("这个安装包可能没有带样例规则。可以重试，或从本机选择规则目录登记自己的规则。", "查找随发行样例规则"),
        () => void this.useBundledRule(thenPrefill),
      );
      return;
    }
    const item = candidates[0];
    const manifestPath = String(item.manifest_path ?? "");
    setNote(this.note, "随发行样例规则已就绪：登记后本机可用。", "pending");
    const modal = dialog(
      "登记随发行样例规则",
      [
        // 字段名标错的地方一并修：state_schema 是「状态数据格式」，不是「规则状态」（评审第六节）
        facts([
          ["名称", String(item.name ?? "")],
          ["规则标识与版本", `${String(item.ruleset_id ?? "")} ${String(item.ruleset_version ?? "")}`],
        ]),
        paragraph("这是随发行附带的样例规则，登记后本机可用；之后可以在设置里停用或移除。选择清单本身不执行它。", "u-hint"),
        techDetails([
          ["状态数据格式", String(item.state_schema ?? "")],
          ["清单位置", manifestPath],
        ]),
      ],
      [
        {
          label: "登记并继续",
          primary: true,
          // 失败返回 false：登记没成时窗不关，窗内说清楚（P0-1）
          run: async (): Promise<boolean> => {
            try {
              await this.registerBundledSample(manifestPath, thenPrefill);
              return true;
            } catch {
              // registerBundledSample 自己已经写了失败提示（页内错误卡）：这里只负责不关窗
              setNote(this.note, "登记没有完成：按上面的说明重试，或换一个规则目录", "bad");
              return false;
            }
          },
        },
        { label: "取消", run: () => setNote(this.note, "已取消登记随发行样例规则", "muted") },
      ],
    );
    document.body.appendChild(modal.node);
  }

  /** 登记随发行样例规则；失败往外抛，由调用方决定是关窗还是留在窗里 */
  private async registerBundledSample(manifestPath: string, thenPrefill: boolean): Promise<void> {
    setNote(this.note, "正在登记随发行样例规则…", "pending");
    try {
      const result = await this.ctx.api.rulesRegister(manifestPath);
      await this.loadPlugins();
      if (thenPrefill) {
        await this.prefillSampleCampaign();
        return;
      }
      setNote(this.note, `已登记并启用：${String((result.plugin as Json)?.name ?? "随发行样例规则")}`, "ok");
      await this.render();
    } catch (error) {
      this.sampleError(
        "随发行样例规则登记失败",
        uiError(error, { module: "跑团", action: "登记随发行样例规则" }),
        () => void this.registerBundledSample(manifestPath, thenPrefill),
      );
      throw error;
    }
  }

  /** 可行动的错误：笔记一行摘要 + 错误卡带「重试」与「从本机选择规则目录」。 */
  private sampleError(noteText: string, detail: UiError, retry: () => void): void {
    setNote(this.note, noteText, "bad");
    const host = this.host;
    if (!host) return;
    this.sampleErrorCard?.remove();
    const card = errorCard(detail, [
      { label: "重试", run: retry },
      { label: "从本机选择规则目录…", run: () => void this.addRuleFromDisk() },
    ]);
    this.sampleErrorCard = card;
    host.appendChild(card);
  }

  private async addRuleFromDisk(): Promise<void> {
    setNote(this.note, "正在打开目录选择…", "pending");
    try {
      const { invoke } = await import("@tauri-apps/api/core");
      const picked = await invoke<string | null>("pick_dir", { title: "选择规则包所在目录" });
      if (!picked) {
        setNote(this.note, "已取消选择", "muted");
        return;
      }
      const scanned = await this.ctx.api.rulesScan(picked);
      const candidates = (scanned.candidates as Json[]) ?? [];
      if (!candidates.length) {
        setNote(this.note, `这个位置没有找到规则包清单：${String(scanned.reason ?? "")}`, "bad");
        return;
      }
      const item = candidates[0];
      const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
      const modal = dialog(
        "添加并启用规则",
        [
          facts([
            ["名称", String(item.name ?? "")],
            ["规则标识与版本", `${String(item.ruleset_id ?? "")} ${String(item.ruleset_version ?? "")}`],
            ["状态", String(item.status ?? "")],
          ]),
          paragraph("登记之后，规则会在按规则算结果时作为本地扩展程序运行；进程隔离不是完整安全沙箱。选择文件本身不执行它。", "u-hint"),
          techDetails([
            ["来源目录", picked],
            ["协议", String(item.protocol ?? "")],
            ["入口", ((item.entry as string[]) ?? []).join(" ")],
          ]),
          note,
        ],
        [
          {
            label: "添加并启用",
            // 失败返回 false：注册没成时窗不关，原因写在窗内（P0-1）
            run: async () => {
              try {
                const result = await this.ctx.api.rulesRegister(String(item.manifest_path ?? picked));
                await this.loadPlugins();
                setNote(this.note, `已登记并启用：${String((result.plugin as Json)?.name ?? "")}（新建战役时可选）`, "ok");
                await this.render();
                return true;
              } catch (error) {
                setNote(note, uiError(error, { module: "规则登记", action: "添加并启用" }).message, "bad");
                return false;
              }
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
    host.appendChild(
      pageHead("跑团", "声明行动，按规则得到结果；没算出真实结果就不说成功", [
        button("返回", () => {
          this.view = "list";
          void this.render();
        }),
      ]),
    );
    if (!instances.length) {
      const emptyWorld = panel(
        "先有一个世界",
        paragraph("战役要挂在一个世界上：先创建或从样例开始。"),
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
    // 已登记但停用 / 依赖缺失的规则：规则栏里要区分它们与「根本没登记」（P1-6）
    const registeredUnusable = this.plugins.filter((item) => {
      const status = String(item.status);
      return status !== "available" && status !== "unregistered";
    }).length;
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
    // 提交给核心的仍是原始标识（option 的 value），屏幕上只给中文（P1-9）
    for (const [kind, label] of SCENE_KINDS) {
      sceneKind.appendChild(el("option", { value: kind, text: label }));
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
      panel(
        "新建战役",
        // 一级只包一层（panel）：五个阶段各是一张二级浅描边卡片，顺序一眼可见，
        // 而不是五个同重量盒子平铺（2026-10-08 审查根因 1：全站只有一个视觉重量）
        paragraph("按顺序走完五项就能开始：中间任何一步都可以先「保存为准备中」。"),
        section("① 名称", field("战役名称", name)),
        section(
          "② 世界与世界线",
          field("世界", instancePicker),
          field("世界线", timelinePicker),
          String(timeline.state) === "active"
            ? paragraph(
                "这条世界线正在运行：同线的联络与其他战役都会受影响。默认建议另开一条跑团世界线（新线先暂停）。",
                "u-hint",
              )
            : paragraph("新分支与新世界线都先暂停；「开始战役」会同时启动这条线。", "u-hint"),
          String(timeline.state) === "active"
            ? button("另开一条跑团世界线…", () => void this.forkTimelineForPlay())
            : null,
        ),
        section(
          "③ 规则",
          field("规则与版本", rulePicker),
          // 「没登记」与「登记了但停用」是两种下一步：后者不该再劝用户去找规则目录（P1-6）
          usable.length
            ? paragraph(
                registeredUnusable
                  ? `还有 ${registeredUnusable} 条已登记的规则不能选：它们被停用或缺少依赖。要在这里用上，去设置里启用。`
                  : "登记过的规则都在这里；规则名与版本分别登记，缺失或不适配的不会出现在这一栏。",
                "u-hint",
              )
            : paragraph("本机还没有可用的规则：可以登记随发行样例规则，或从本机选择规则目录。", "u-hint"),
          el(
            "div",
            { class: "u-row" },
            button("从本机选择规则目录…", () => void this.addRuleFromDisk()),
            usable.length ? null : button("登记随发行样例规则", () => void this.useBundledRule(false)),
            registeredUnusable ? button("去设置里启用规则", () => this.ctx.navigate({ pane: "settings", sub: "extensions" })) : null,
          ),
        ),
        section(
          "④ 角色",
          paragraph(
            "角色属性来自规则自己声明的初始化材料；没有适配的规则时这里不做自动建卡，只按已登记的角色参与。",
            "u-hint",
          ),
          charBox,
        ),
        section(
          "⑤ 开场场景",
          field("场景名称", sceneName),
          field("公开简介", sceneBrief),
          field("场景类型", sceneKind),
          field("地点", sceneLocation),
          field("仅主持说明", scenePrivate),
        ),
        (() => {
          const box = el("div", { id: "u-trpg-summary" });
          const paint = (): void => {
            fill(
              box,
              facts([
                ["战役名称", displayName(this.draft.name, "", "（还没写名字）")],
                ["世界", instances.find((item) => item.id === this.draft.instanceId)?.name ?? ""],
                // 摘要里写世界线的名字：内部编号对用户没有意义（P1-9）
                ["世界线", String(this.draft.timelineId
                  ? timelines.find((entry) => String(entry.id) === this.draft.timelineId)?.name ?? ""
                  : "") || "（还没选）"],
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
        ),
      ),
    );
  }

  private async forkTimelineForPlay(): Promise<void> {
    try {
      const commits = await this.ctx.api.commits(this.draft.instanceId, this.draft.timelineId);
      let head = String(((commits.commits as Json[]) ?? [])[0]?.id ?? "");
      if (!head) {
        // 就地补上「保存版本点」：不让用户为了一步操作跑去另一个页面（与写作页同一套做法）
        const saved = await this.ctx.api.saveVersion(this.draft.instanceId, this.draft.timelineId, "另开跑团线前保存当前进度");
        // runtime.commit 回的是 { commit: {...} }：编号在 commit.id 上
        head = String((saved.commit as Json | undefined)?.id ?? saved.commit_id ?? "");
        if (!head) {
          const again = await this.ctx.api.commits(this.draft.instanceId, this.draft.timelineId);
          head = String(((again.commits as Json[]) ?? [])[0]?.id ?? "");
        }
        if (!head) {
          setNote(this.note, "这条线还没有可用的进度记录：这个版本没能保存成功，请重试一次", "bad");
          return;
        }
      }
      const result = await this.ctx.api.waBranch({
        instance_id: this.draft.instanceId,
        timeline_id: this.draft.timelineId,
        commit_id: head,
        name: "跑团线",
      });
      const timeline = (result.timeline as Json) ?? {};
      this.draft.timelineId = String(timeline.id ?? "");
      setNote(this.note, `已另开一条跑团世界线「${String(timeline.name ?? "")}」（暂停；点「开始战役」时会同时启动它）`, "ok");
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "跑团", action: "另开世界线" }).message, "bad");
    }
  }

  private async createCampaign(status: string): Promise<void> {
    if (!this.draft.name.trim()) {
      setNote(this.note, "战役要有名字：它保存在战役自己的元数据里，之后可改", "bad");
      return;
    }
    if (!this.draft.rulesetId) {
      setNote(
        this.note,
        registeredUnusableHint(this.plugins, this.draft.rulesetId)
          ? "这条规则已被停用或缺少依赖：先在下面点「去设置里启用规则」，或在上面选一条已启用的规则"
          : "先选规则与版本：核心只在规则版本、参与者、初始状态和场景都合法时才允许开始",
        "bad",
      );
      return;
    }
    setNote(this.note, status === "active" ? "正在创建战役…" : "正在保存准备中的战役…", "pending");
    let activateError: string | null = null;
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
      if (status === "active" && this.draft.timelineId) {
        // 「开始战役」承诺同时启动这条线：忘掉这一步会让战役建成、世界却不动（P0-2）
        activateError = await this.activateTimeline(this.draft.instanceId, this.draft.timelineId);
      }
      // 提示必须与真实状态一致：线没启动就不能说「战役已开始」
      if (status === "active") {
        setNote(
          this.note,
          activateError
            ? `战役「${String(created.name ?? this.draft.name)}」已建成，但这条世界线没有启动：${activateError}。世界里现在不会推进，请重试，或到「世界与素材」启动它。`
            : `战役「${String(created.name ?? this.draft.name)}」已开始，这条世界线也已启动`,
          activateError ? "bad" : "ok",
        );
      } else {
        setNote(
          this.note,
          `已保存为准备中：${String(created.name ?? this.draft.name)}（核心只在规则版本、参与者、初始状态与场景都合法时才允许开始）`,
          "ok",
        );
      }
      await this.open(this.draft.instanceId, this.draft.timelineId, String(created.campaign_id), String(created.name ?? this.draft.name));
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

  /**
   * 明确启动目标线（§8.1）：冻结的线要显式启动，不然世界里什么都不会动。
   * 返回 null 表示已经启动或启动成功；返回字符串表示启动失败的真实原因（由调用方如实报出）。
   */
  private async activateTimeline(instanceId: string, timelineId: string): Promise<string | null> {
    try {
      await this.ctx.api.activate(instanceId, timelineId);
      return null;
    } catch (error) {
      const message = uiError(error, { module: "跑团", action: "启动世界线" }).message;
      // 已经是运行中的线：核心会拒一次，这不算失败（它本来就是我们要的结果）
      if (/已在运行|正在运行|already|active/i.test(message)) return null;
      return message;
    }
  }

  /* ------------------------------------------------------------ §8.2–8.3 场景与行动 */

  private async open(instanceId: string, timelineId: string, campaignId: string, campaignName = ""): Promise<void> {
    this.instanceId = instanceId;
    this.timelineId = timelineId;
    this.campaignId = campaignId;
    this.view = "play";
    this.workspace = null;
    // 进入某战役就记住它（§3.3）+ 记一条最近使用（§3.4）
    this.rememberCampaign(campaignName);
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

  /** 进入某战役后写 `sel.trpg` 与一条「最近使用」（§3.3 / §3.4）；写失败静默，不挡读取局面 */
  private rememberCampaign(name = ""): void {
    if (!this.instanceId || !this.campaignId) return;
    const instance = this.ctx.instances().find((item) => item.id === this.instanceId);
    const campaign = this.campaigns.find((item) => String(item.campaign_id) === this.campaignId);
    const campaignName = name || displayName(campaign?.name, this.campaignId, "未命名战役");
    const selection: TrpgSelection = {
      instance_id: this.instanceId,
      timeline_id: this.timelineId,
      campaign_id: this.campaignId,
      campaign_name: campaignName,
    };
    void this.ctx.setPrefs({ "sel.trpg": selection });
    this.ctx.rememberRecent({
      pane: "trpg",
      label: `跑团 · ${String(instance?.name ?? "")} / ${campaignName}`,
      key: `trpg:${this.instanceId}:${this.timelineId}:${this.campaignId}`,
    });
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
        violations.length ? `这个视图里有 ${violations.length} 处不该出现的内容，已按「能不能给玩家看」的约定拦下` : "局面已读取",
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
    // ① 标题带：战役名 + 一句定位语。世界时间与现实时间分开这件事收进定位语，不再单占一行
    host.appendChild(
      pageHead(
        displayName(campaign.display_name ?? campaign.name, this.campaignId, "未命名战役"),
        `当前是${this.mode === "gm" ? "主持视图" : "玩家视图"}：这里是故事里的进度，不是真实钟表时间`
          + `｜${STATUS_TEXT[String(campaign.status)] ?? String(campaign.status)}`,
        [
          button("刷新局面", () => void this.refresh()),
          button("返回战役列表", () => {
            this.view = "list";
            this.campaigns = [];
            // 清单要按离开后的世界重新读：连失败标记一起清掉，避免拿旧失败冒充本次结果
            this.campaignsFailed = 0;
            this.campaignsError = null;
            void this.render();
          }),
        ],
      ),
    );
    // ② 工具带：玩家视图 / 主持视图就是这一页的两个看法。用选中态表达「你现在在哪一面」，
    // 不再要求用户读一句话再自己换算（2026-10-08 审查第 8 节）
    host.appendChild(
      tools(
        [
          { label: "玩家视图", current: this.mode === "player", onSelect: () => void this.switchMode("player") },
          { label: "主持视图", current: this.mode === "gm", onSelect: () => void this.switchMode("gm") },
        ],
        "跑团视图",
      ),
    );
    // 世界线编号与世界内进度（无单位的大数字）收进技术详情：正文里对用户没有意义（P1-9）
    const world = (faces.world as Json) ?? {};
    const worldTech = techDetails([
      ["世界线编号", this.timelineId],
      ["世界内进度", String(world.revision ?? "") ? `已推进 ${String(world.revision)} 个进度点` : ""],
    ]);
    host.appendChild(
      el(
        "div",
        { class: "u-row u-row-wrap" },
        chip(`当前是${this.mode === "gm" ? "主持视图" : "玩家视图"}`, "muted"),
        chip(STATUS_TEXT[String(campaign.status)] ?? String(campaign.status), "pending"),
        worldTech,
      ),
    );
    // 现在走到哪一步：核心只给阶段名，用一条轨把「写下行动 → 确认卡 → 按规则算结果 → 写入世界」画出来
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
          ["节奏", NEXT_KIND[String((scene.scene as Json)?.advance_mode ?? "")] ?? String((scene.scene as Json)?.advance_mode ?? "")],
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
              // 按顺序编号：行动的内部编号对用户没有意义（P1-9）
              ((scene.unfinished_actions as Json[]) ?? []).map(
                (item, index) => `行动 ${index + 1}：${String((item.state as Json)?.label ?? stageText(String(item.status ?? "")))}`,
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
                (item, index) => `行动 ${index + 1}：${String((item.state as Json)?.label ?? stageText(String(item.status ?? "")))}`,
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
    // 行动正文同样要边打边记：只在提交时读一次的话，打字中途任何一次重画都会把它回填成旧值
    const intentInput = el("textarea", {
      class: "u-textarea", rows: "2", id: "u-trpg-intent", placeholder: "用一句话说清你想做什么",
    }) as HTMLTextAreaElement;
    intentInput.value = this.actionText;
    intentInput.addEventListener("input", () => {
      this.actionText = intentInput.value;
    });
    host.appendChild(
      section(
        "我想……",
        field("行动", intentInput),
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
          primary("查看这次行动", () => void this.declareAction(false)),
          button("确认并算结果", () => void this.declareAction(true)),
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

  /** 这次行动的表（§8.2/§8.3）：行动者 / 目标 / 方法 / 已知代价 / 缺什么，缺了就不给确认。 */
  private draftCard(): HTMLElement {
    const draft = ((this.bundle?.draft as Json) ?? null) as Json | null;
    if (!draft) {
      return section(
        "这次行动",
        paragraph("还没有可确认的行动：写下行动、填上行动者与目标，点「查看这次行动」。", "u-hint"),
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
      "这次行动",
      facts([
        ["行动者", `${String(fields.actor ?? "") || "（未定）"}${sourceText("actor") ? `｜${sourceText("actor")}` : ""}`],
        ["目标", `${String(fields.target ?? "") || "（未定）"}${sourceText("target") ? `｜${sourceText("target")}` : ""}`],
        ["方法", `${String(fields.method ?? "") || "（未定）"}${sourceText("method") ? `｜${sourceText("method")}` : ""}`],
        ["打算", String(fields.intent ?? "") || "（未定）"],
        ["已知代价", risks.length ? "由规则算出（没有真实结果就不给估计）" : "（未列出）"],
      ]),
      gaps.length
        ? bulletList(gaps.map((item) => `缺：${item}`), "u-list")
        : paragraph("这几项齐了：可以点「确认并算结果」。", "u-hint"),
      paragraph(
        String(this.bundle?.blocked ?? "") || "没有按规则算出真实结果之前，这里不会出现骰点或「成功」，也不会提前写成世界已经改变。",
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
    setNote(this.note, confirm ? "正在确认并算结果…" : "正在整理这次行动…", "pending");
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
      const stopped = String(rail?.querySelector(".u-rail-current .u-rail-label")?.textContent ?? "") || stageText(stage);
      const why = String(result.skipped ?? "") || String(((result.errors as string[]) ?? [])[0] ?? "");
      setNote(
        this.note,
        confirm
          ? committed
            ? "结果已经写进世界：这次变化已经生效"
            : `规则给了结果，世界还没变：这一轮停在「${stopped}」${why ? `——${why}` : "（没有真实结果就不显示骰点，也不说成功）"}`
          : `这次行动可以确认了（现在停在「${stageText(stage)}」）`,
        confirm ? (committed ? "ok" : "pending") : "muted",
      );
      await this.render();
    } catch (error) {
      setNote(
        this.note,
        uiError(error, { module: "跑团", action: confirm ? "确认并算结果" : "整理这次行动" }).message,
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
    // 阶段名只用来判断，不直接上屏：正文里一律走 stageText 的中文（P1-9）
    push(stage === "needs_input", "补充行动", "AI 只补空项；你填过的行动者、目标与方法不被静默替换");
    push(stage === "awaiting_confirmation", "确认并算结果", "改任何关键项都会让旧确认失效");
    push(stage === "submitting" || stage === "resolving", `查看进度（${stageText(stage)}）`, "不取消后自动重掷；切页不会算第二次");
    push(stage === "unknown", "查询原结果", "用同一次操作去核对；结果还没确认时不重新算一次");
    push(stage === "plugin_failed", "恢复已保存结果 / 重新算一次", "有结果先恢复；重做可能改变随机结果，需要你明确确认");
    push(Boolean((bundle.faces as Json)?.stale), "重新读取并确认", "旧结果留作记录，重新检查后生成新的行动确认");
    if (!rows.length) {
      host.appendChild(
        section("这一步能做什么", paragraph("按上面的按钮走：写下行动 → 查看这次行动 → 确认并算结果；有待选择时先处理选择。", "u-hint")),
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

  /* ------------------------------------------------------------ §8.4 主持视图 */

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
        // 切换没带回主持视图：老老实实再读一次局面，而不是拿玩家视图冒充主持视图
        await this.refresh();
        return;
      }
      this.bundle = result;
      setNote(
        this.note,
        mode === "gm"
          ? "已切到主持视图：这里能看到待审与规则依据，但发出去的内容仍是按能不能给玩家看裁过的"
          : "已切回玩家视图：给玩家看的内容重新取过，主持视图的缓存已丢",
        "muted",
      );
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "跑团", action: "切换视图" }).message, "bad");
    }
  }

  private renderGm(host: HTMLElement, faces: Json): void {
    const gm = (faces.gm as Json) ?? {};
    const queue = (gm.pending as Json[]) ?? [];
    const rows = el("div", { class: "u-rows" });
    for (const item of queue) {
      const row = el("div", { class: "u-row-line" });
      // 待审列表按顺序编号：行动的内部编号对用户没有意义（P1-9）
      row.appendChild(
        el("span", { class: "u-grow", text: `待审行动 ${rows.childElementCount + 1}｜${stageText(String(item.status ?? ""))}` }),
      );
      const canApprove = Boolean(item.committable);
      row.appendChild(
        canApprove
          ? button("批准并提交", () => void this.reviewAction(String(item.action_id ?? ""), "approve"))
          : chip("材料不足：只能补充、修改或拒绝", "muted"),
      );
      row.appendChild(button("先放一放", () => void this.reviewAction(String(item.action_id ?? ""), "hold")));
      row.appendChild(button("拒绝", () => void this.reviewAction(String(item.action_id ?? ""), "reject")));
      rows.appendChild(row);
    }
    if (!queue.length) rows.appendChild(paragraph("没有待审行动。", "u-hint"));
    host.appendChild(
      section(
        "主持视图 · 待处理行动",
        rows,
        paragraph("只有仍然有效、而且有东西可提交的结果才显示「批准并提交」；没有一键全批准。", "u-hint"),
      ),
    );
    host.appendChild(
      section(
        "直接变化",
        paragraph("要直接改写世界，走「预览 → 确认提交」这条路：预览不改世界，确认后才落成事实。"),
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
        paragraph("新场景的开场材料（名称 / 公开简介 / 地点 / 在场者 / 风险 / 可行动作）与只给主持人看的说明分开填。"),
        button("准备下一个场景…", () => void this.openSceneForm()),
      ),
    );
    host.appendChild(
      section(
        "暂停战役",
        paragraph("暂停默认只停这一局的行动；世界仍可能继续推进。同一条世界线上的联络与其他战役都会受影响，要不要连这条世界线一起暂停，要单独选。"),
        el(
          "div",
          { class: "u-row" },
          button("只暂停战役", () => void this.setCampaignStatus("paused")),
          button("暂停战役并暂停这条世界线", () => void this.pauseWithTimeline()),
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
      setNote(this.note, uiError(error, { module: "主持视图", action: "记下决定" }).message, "bad");
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
        paragraph("先预览：预览不改世界；确认提交时世界与规则状态同批成功或同批失败。", "u-hint"),
        note,
      ],
      [
        {
          label: "预览",
          // 失败返回 false：预览没成时窗不关，原因写在窗内（P0-1）
          run: async () => {
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
              setNote(this.note, `预览完成（世界未被改动）：现在停在「${stageText(String(result.stage ?? ""))}」`, "muted");
              this.bundle = result;
              await this.render();
              return true;
            } catch (error) {
              setNote(note, uiError(error, { module: "直接变化", action: "预览" }).message, "bad");
              return false;
            }
          },
        },
        {
          label: "确认提交",
          // 失败返回 false：提交没成时窗不关；理由必填，避免「点了没反应」（P0-1）
          run: async () => {
            if (!reason.value.trim()) {
              setNote(note, "要写清为什么这么改：改动会写进世界，理由要留在记录里", "bad");
              return false;
            }
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
              return true;
            } catch (error) {
              setNote(note, uiError(error, { module: "直接变化", action: "确认提交" }).message, "bad");
              return false;
            }
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
        paragraph("录入场景描述本身不改变世界；真的有世界变化时另外走「直接变化」的预览与确认提交。", "u-hint"),
        note,
      ],
      [
        {
          label: "开这个场景",
          // 失败返回 false：场景没开成时窗不关，原因写在窗内（P0-1）
          run: async () => {
            try {
              await this.ctx.api.trpgSceneOpen({
                instance_id: this.instanceId, timeline_id: this.timelineId, campaign_id: this.campaignId,
                name: name.value.trim(), brief: brief.value.trim(),
                location_refs: location.value.trim() ? [location.value.trim()] : [],
                participants: [],
              });
              setNote(this.note, "新场景已开；这是战役自己的安排，不是世界变化", "ok");
              await this.refresh();
              return true;
            } catch (error) {
              setNote(note, uiError(error, { module: "场景准备", action: "开新场景" }).message, "bad");
              return false;
            }
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
        status === "paused" && String((result as Json).status ?? status) === "paused"
          ? "战役已暂停：世界仍可能继续推进；要连这条世界线一起停，点「暂停战役并暂停这条世界线」"
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
      setNote(this.note, "两步都成功：战役已暂停，这条世界线也已暂停（同线的联络与其它战役同样受影响）", "ok");
    } catch (error) {
      setNote(
        this.note,
        `战役已暂停，但这条世界线没有停：${uiError(error, { module: "跑团", action: "暂停世界线" }).message}`,
        "bad",
      );
    }
  }
}
