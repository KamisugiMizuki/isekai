/*
 * 首次设置向导（ONBOARDING_AND_RECOVERY §4）：本机检查 → 连接 AI → 选择第一件事
 * → 准备材料 → 开始使用。
 *
 * 每一步的通过条件都取自真实结果（不是「目录里有文件」）；失败留在本步，保留已填内容；
 * 未通过的能力测试不带绿色就绪，也不写进正式配置。
 */

import type { AppContext, Pane } from "./app";
import { appPane, paneTitle } from "./app";
import type { Json } from "./api";
import { uiError } from "./api";
import type { AppMode } from "./launcher";
import { migrateCard } from "./migrate";
import { aiSetup } from "./ai-setup";
import { button, el, errorCard, field, fill, pageHead, paragraph, primary, section, setNote, stamp } from "./dom";
import { checkList, flowRail } from "./graphics";

type StepId = "check" | "ai" | "task" | "material" | "start";

/**
 * 核心给的本机检查标签里还留着内部叫法（「受管目录可写」「单一写入者」）。
 * 与帮助页同一张表：同一件事在向导和帮助页必须是一个说法。
 */
const CHECK_LABELS: Record<string, string> = {
  受管目录可写: "数据文件夹可写",
  单一写入者: "只由这个程序写入",
  数据格式可读: "已有的内容能读",
};

function checkLabel(label: unknown): string {
  const text = String(label ?? "");
  return CHECK_LABELS[text] ?? text;
}

const STEPS: Array<{ id: StepId; label: string }> = [
  { id: "check", label: "本机检查" },
  { id: "ai", label: "连接 AI" },
  { id: "task", label: "选择任务" },
  { id: "material", label: "准备材料" },
  { id: "start", label: "开始使用" },
];

/** 入口带的下标 → 向导步骤：`sub:"sample"`（各处「从样例世界开始」）直接落到「准备材料」。 */
const SUB_STEPS: Record<string, StepId> = {
  sample: "material",
  check: "check",
  ai: "ai",
  task: "task",
  material: "material",
  start: "start",
};

export class OnboardingPane implements Pane {
  readonly id = "onboarding" as const;
  private step: StepId = "check";
  private busy = false;
  /** 向导页的根元素：每一步都渲染进它（不要拿「上一步的容器」当新宿主，会套娃） */
  private root: HTMLElement | null = null;

  constructor(private readonly ctx: AppContext, private readonly startAt?: string) {}

  async mount(host: HTMLElement): Promise<void> {
    const saved = String(this.ctx.prefs["onboard.step"] ?? "check") as StepId;
    // 入口指名的那一步优先（「从样例世界开始」不该把人扔回「本机检查」），其次才是上次停在哪
    const asked = SUB_STEPS[String(this.startAt ?? "")];
    this.step = asked ? this.entryStep(asked) : STEPS.some((item) => item.id === saved) ? saved : "check";
    if (this.step === "start") this.step = "material";
    this.root = host;
    await this.render();
  }

  /**
   * 点名的那一步之前，前置（本机检查）都过完了才直接落到点名那一步。
   *
   * AI 配置不再算「前置」（2026-10-07 可用性评审 P0-2）：安装样例、建世界、写设定全是本机操作，
   * 一个只想先看看样例的用户点「先看看样例，稍后再配置」，不该被送回「连接 AI」——
   * 那正是他想躲的一步。没配 AI 也能走到「准备材料」，配置随时能在设置里补。
   */
  private entryStep(asked: StepId): StepId {
    const readiness = this.ctx.readiness ?? {};
    return readiness.ready ? asked : "check";
  }

  private async render(): Promise<void> {
    const host = this.root;
    if (!host) return;
    const page = el("div", { class: "u-page" });
    // 标题带（页面名 + 一句定位语）：以前正文里另有一个 h2「首次设置」，
    // 与顶栏标题重了一遍（2026-10-08 视觉体系审查）
    page.appendChild(pageHead("首次设置", "五步就能开始；任何一步都可以跳过 AI", []));
    page.appendChild(this.stepper());
    const body = el("div", { class: "u-step-body" });
    page.appendChild(body);
    fill(host, page);
    switch (this.step) {
      case "ai":
        this.renderAi(body);
        break;
      case "task":
        this.renderTask(body);
        break;
      case "material":
        await this.renderMaterial(body);
        break;
      case "start":
        this.renderStartSection(body);
        break;
      default:
        this.renderCheck(body);
    }
  }

  /**
   * 真进度轨：带编号圆点 + 连线（以前是「文字 + 一排短下划线」，看不出那是链接还是分隔线、
   * 也看不出走了几分之几 —— 2026-10-08 视觉体系审查）。
   *
   * 圆点可以点着回到某一步：AI 这一步本来就能跳过，「只能往前走」会把回退变成谜题
   * （连接 AI 那一步以前没有返回上一颗）。flowRail 画的是 div（不可聚焦），
   * 这里补 role / tabindex / 键盘处理，让鼠标、键盘与读屏都能回到那一步。
   */
  private stepper(): HTMLElement {
    const index = STEPS.findIndex((item) => item.id === this.step);
    const rail = flowRail(STEPS.map((item) => ({ label: item.label })), Math.max(0, index));
    if (!rail) {
      // 画不出来（步骤表为空这类不可能的情况）也要说清在第几步：退回一行文字，不返回 null
      return el("p", { class: "u-hint u-wizard-rail", text: STEPS.map((item) => item.label).join("　") });
    }
    rail.classList.add("u-wizard-rail"); // 探针 / 样式认这一类：向导的进度轨
    Array.from(rail.querySelectorAll<HTMLElement>(".u-rail-step")).forEach((node, position) => {
      const target = STEPS[position];
      if (!target) return;
      // 当前这一步不动：flowRail 已经给它标了 aria-current="step"，
      // 再套一层「按不动的按钮」只会让读屏多念一句废话
      if (target.id === this.step) return;
      node.setAttribute("role", "button");
      node.setAttribute("aria-label", `回到第 ${position + 1} 步：${target.label}`);
      if (target.id === "start" && !this.ctx.prefs["onboard.instance"]) {
        // 「开始使用」还没有世界可指：点了只会看到空名字，不做假按钮
        node.setAttribute("aria-disabled", "true");
        return;
      }
      node.tabIndex = 0;
      const jump = (): void => void this.goto(target.id);
      node.addEventListener("click", jump);
      node.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        jump();
      });
    });
    return rail;
  }

  private async goto(step: StepId, mode?: AppMode): Promise<void> {
    this.step = step;
    await this.ctx.setPrefs({
      "onboard.step": step,
      "onboard.done": step === "start" ? true : this.ctx.prefs["onboard.done"] ?? false,
      // 选了第一件事就顺手把应用模式定下来：建完世界直接落进那个应用，不在向导里再问一遍
      ...(mode ? { app_mode: mode, "onboard.task": mode } : {}),
    });
    await this.render();
  }

  /* ---------------------------------------------------------------- ① 本机检查 */

  private renderCheck(host: HTMLElement): void {
    const readiness = this.ctx.readiness ?? {};
    const checks = (readiness.checks as Json[]) ?? [];
    const ok = Boolean(readiness.ready);
    // 首屏不摊技术项（评审 P1）：过了就一行「这台电脑可以运行」，细节收进折叠。
    // 用户此刻要的是「能不能开始」，不是「单一写入者」「记录格式」。
    host.appendChild(
      section(
        "本机环境",
        paragraph(ok ? "这台电脑可以运行，数据会放在你自己的用户目录里。" : "这台电脑还有一项没准备好，看下面的原因。"),
        paragraph("这一步不涉及 AI 密钥，也不写任何东西。", "u-hint"),
        el(
          "details",
          { class: "u-check-detail" },
          el("summary", { text: "查看检查详情（程序版本、数据位置、存储）" }),
          checkList(
            checks.map((item) => ({
              label: checkLabel(item.label),
              ok: Boolean(item.ok),
              detail: String(item.detail ?? ""),
            })),
          ),
        ),
      ),
    );
    if (!ok) {
      const broken = checks.filter((item) => !item.ok).map((item) => String(item.fix ?? item.detail ?? ""));
      host.appendChild(
        section(
          "需要先处理",
          el("ul", { class: "u-list" }, ...broken.map((text) => el("li", { text }))),
          el(
            "div",
            { class: "u-row" },
            button("重新检查", () => void this.recheck()),
            button("打开日志目录", () => void import("./app").then((m) => m.openDir("logs", this.ctx.api))),
            button("复制诊断信息", () => void copyDiagnostics(this.ctx)),
          ),
        ),
      );
      return;
    }
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        primary("继续：连接 AI", () => void this.goto("ai")),
        button("先跳过 AI，直接看样例", () => void this.goto("material")),
        button("重新检查", () => void this.recheck()),
      ),
    );
    // 已有开发版数据的用户（§3.2）：从旧目录整份搬过来，而不是手动拷文件。
    // 默认折叠：首启第一屏不该先问一个新手答不上来的问题（「开发版」「data/isekai.db」
    // 「通道凭据」——2026-10-08 审计 P1-1）。老用户知道自己在找什么，会点开。
    host.appendChild(
      el(
        "details",
        { class: "u-check-detail" },
        el("summary", { text: "以前用过 isekai 的开发版，想把里面的数据搬过来？" }),
        migrateCard(this.ctx),
      ),
    );
  }

  private async recheck(): Promise<void> {
    await this.ctx.refresh();
    await this.render();
  }

  /* ---------------------------------------------------------------- ② 连接 AI */

  private renderAi(host: HTMLElement): void {
    const ai = ((this.ctx.readiness.ai as Json) ?? {}) as Json;
    const saved = ((this.ctx.settings.llm as Json) ?? {}) as Json;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const results = el("div", { class: "u-test-results" });
    // 与设置页共用同一份控件与校验：两处各写一遍必然分叉（评审 P0-1 / P1-1）
    const setup = aiSetup({
      saved,
      apiKeySet: Boolean(ai.api_key_set),
      apiKeyMasked: String(ai.api_key_masked ?? ""),
      servicePref: String(this.ctx.prefs["onboard.service"] ?? ""),
      onServicePref: (id) => void this.ctx.setPrefs({ "onboard.service": id }),
      notify: (text, kind) => setNote(note, text, kind),
    });

    const save = async (verified: boolean): Promise<boolean> => {
      const invalid = setup.validate(note);
      if (invalid) return false;
      try {
        const payload = setup.collect();
        await this.ctx.api.saveSettings({ llm: payload });
        await this.ctx.refresh();
        await this.ctx.setPrefs({
          "ai.tested_at": verified ? Date.now() / 1000 : this.ctx.prefs["ai.tested_at"] ?? 0,
          "ai.verified": verified,
        });
        setNote(note, verified ? "已保存，基础能力已验证" : "已保存未验证的配置（新生成仍会按运行时错误提示）", verified ? "ok" : "pending");
        setup.key.value = "";
        return true;
      } catch (error) {
        const info = uiError(error, { module: "设置", action: "保存 AI 配置" });
        results.appendChild(errorCard(info, [{ label: "返回继续编辑", run: () => setup.key.focus() }]));
        return false;
      }
    };

    const runTest = async (): Promise<void> => {
      if (this.busy) return;
      if (setup.validate(note)) return;
      this.busy = true;
      fill(results);
      setNote(note, "正在测试，已等待 0 秒…", "pending");
      // 只显示真实阶段与已等待时间（ONBOARDING §4.2 / P2-14）：等待期间报秒数，
      // 完成后渲染内核返回的 `stages` 与最终检查，不用固定三行假装在跑。
      const progress = el("div", { class: "u-progress" });
      const live = el("p", { class: "u-hint", text: "正在测试，已等待 0 秒…" });
      progress.appendChild(live);
      results.appendChild(progress);
      const startedAt = Date.now();
      const timer = window.setInterval(() => {
        const waited = Math.floor((Date.now() - startedAt) / 1000);
        live.textContent = `正在测试，已等待 ${waited} 秒…`;
        setNote(note, `正在测试，已等待 ${waited} 秒…`, "pending");
      }, 1000);
      try {
        const result = await this.ctx.api.testAi(setup.collect());
        const stages = (result.stages as Json[]) ?? [];
        const checks = (result.checks as Json[]) ?? [];
        fill(progress);
        const stageItems = stages.map((item) => ({
          label: String(item.label),
          ok: Boolean(item.ok),
          detail: String(item.detail ?? ""),
        }));
        if (stageItems.length) results.appendChild(checkList(stageItems));
        results.appendChild(
          checkList(
            checks.map((item) => ({
              label: checkLabel(item.label),
              ok: Boolean(item.ok),
              detail: String(item.detail ?? ""),
            })),
          ),
        );
        results.appendChild(
          el("p", { class: "u-hint" }, `服务 ${String(result.service ?? "")}｜模型 ${String(result.model ?? "")}｜用时 ${String(result.duration_ms ?? 0)} 毫秒`),
        );
        if (result.ok) {
          const ok = await save(true);
          if (ok) await this.goto("task");
          return;
        }
        if (result.text_ok) {
          setNote(note, `基础文本可用，但${String(result.reason || "结构化输出未通过")}`, "bad");
        } else {
          setNote(note, String(result.reason || "这次测试没有成功"), "bad");
        }
        results.appendChild(
          el(
            "div",
            { class: "u-row" },
            button("重新测试连接", () => void runTest()),
            button("保存未验证配置", () => void save(false)),
          ),
        );
      } catch (error) {
        const info = uiError(error, { module: "设置", action: "测试 AI 连接" });
        setNote(note, info.message, "bad");
        results.appendChild(errorCard(info, [{ label: "重新测试连接", run: () => void runTest() }]));
      } finally {
        window.clearInterval(timer);
        this.busy = false;
      }
    };

    host.appendChild(
      section(
        "连接 AI",
        paragraph("生成所需的文字会发送给你选择的服务。测试只发送两段固定测试文字，不发送你的世界与聊天记录，可能产生少量用量。"),
        ...setup.nodes,
        el(
          "div",
          { class: "u-row" },
          primary("测试并保存", () => void runTest()),
          button("稍后配置，先整理素材", () => void this.goto("task")),
        ),
        paragraph("没有密钥也能先用：样例世界、写作与整理素材都在本机完成，随时回来补配置。", "u-hint"),
        note,
        results,
      ),
    );
  }

  /* ---------------------------------------------------------------- ③ 选择任务 */

  /**
   * 三条路都真的能走：写作（U3）与跑团（U4）界面已经落地，不再是「暂未开放」。
   * 选哪条记下应用模式，创建完世界直接落到那个应用（见 renderStartSection）。
   */
  private renderTask(host: HTMLElement): void {
    const instances = Number((((this.ctx.readiness ?? {}).first_run as Json) ?? {}).instances ?? 0);
    const aiNote = ((this.ctx.readiness ?? {}).ai as Json | undefined)?.configured ? "" : "；AI 可以稍后在设置里配";
    const missing = (text: string): string => (instances ? `已就绪${aiNote}` : text);
    const choose = (mode: AppMode, title: string, body: string, note: string, action: string): HTMLElement => {
      const card = el("article", { class: "u-card" });
      card.appendChild(el("h3", { text: title }));
      card.appendChild(paragraph(body));
      card.appendChild(el("p", { class: "u-hint", text: note }));
      card.appendChild(primary(action, () => void this.goto("material", mode)));
      return card;
    };
    const grid = el(
      "div",
      { class: "u-cards" },
      choose("chat", "与角色联络", "选一个世界和角色，和生活在其中的人对话。", missing("还缺：一个已创建的世界（这一步会建）"), "开始联络"),
      choose("writer", "辅助写作", "整理大纲、看看角色知道什么、比较下一步怎么写，保存文字草稿。", missing("还缺：一个世界和一个要观察的角色（这一步会建）"), "开始写作"),
      choose("gm", "进行跑团", "选一套规则和几个角色，声明行动，确认后得到结果。", "还缺：一套跑团规则（可以一键登记随程序附带的样例规则）", "开始跑团"),
    );
    host.appendChild(
      section(
        "选择第一件事",
        paragraph("三条路共用同一个世界，选哪条都能改。"),
        grid,
        el("div", { class: "u-row" }, button("返回", () => void this.goto("ai"))),
      ),
    );
  }

  /* ---------------------------------------------------------------- ④ 准备材料 */

  private async renderMaterial(host: HTMLElement): Promise<void> {
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const results = el("div", { class: "u-test-results" });
    let samples: Json[] = [];
    try {
      const listed = await this.ctx.api.samples();
      samples = (listed.samples as Json[]) ?? [];
    } catch (error) {
      results.appendChild(
        errorCard(
          uiError(error, {
            module: "样例",
            action: "读取随程序样例",
            done: "没有改动任何数据",
            unknown: "这次读取是否成功",
          }),
          [{ label: "重试读取样例", run: () => void this.render() }],
        ),
      );
    }
    if (!samples.length) {
      host.appendChild(
        section(
          "准备材料",
          paragraph("这个安装里没有找到随程序提供的样例世界。可以改用「世界与素材」里的创建向导。"),
          el("div", { class: "u-row" }, button("去世界与素材", () => this.ctx.navigate({ pane: "worlds" }))),
          results,
        ),
      );
      return;
    }
    const savedSample = String(this.ctx.prefs["onboard.sample"] ?? samples[0].id);
    const sampleSelect = el("select", { class: "u-input", id: "onb-sample" }) as HTMLSelectElement;
    for (const sample of samples) {
      sampleSelect.appendChild(el("option", { value: String(sample.id), text: String(sample.title ?? sample.id) }));
    }
    sampleSelect.value = sampleSelect.querySelector(`option[value="${savedSample}"]`) ? savedSample : String(samples[0].id);
    const intro = el("div", { class: "u-sample-intro" });
    const character = el("select", { class: "u-input", id: "onb-character" }) as HTMLSelectElement;
    const worldName = el("input", { class: "u-input", id: "onb-world-name" }) as HTMLInputElement;
    const savedCharacter = String(this.ctx.prefs["onboard.character"] ?? "");

    const currentSample = (): Json => samples.find((item) => String(item.id) === sampleSelect.value) ?? samples[0];

    const refreshSample = (): void => {
      const item = currentSample();
      fill(
        intro,
        paragraph(String(item.description ?? ""), "u-p"),
        paragraph(
          "创建后世界会自己往前走；关掉程序这段时间会在下次打开时补上，暂停就不会。",
          "u-hint",
        ),
      );
      worldName.value = worldName.value || String(item.title ?? "");
      fill(character);
      const cards = (item.cards as Json[]) ?? [];
      for (const card of cards) {
        character.appendChild(el("option", { value: String(card.file ?? ""), text: String(card.name ?? "") }));
      }
      if (savedCharacter && cards.some((card) => String(card.file) === savedCharacter)) character.value = savedCharacter;
    };
    sampleSelect.addEventListener("change", refreshSample);
    refreshSample();

    const start = async (): Promise<void> => {
      if (this.busy) return;
      this.busy = true;
      const sample = currentSample();
      const requestId = String(this.ctx.prefs["onboard.request"] ?? `onb-${Date.now().toString(36)}`);
      await this.ctx.setPrefs({ "onboard.request": requestId, "onboard.sample": sampleSelect.value, "onboard.character": character.value });
      setNote(note, "正在准备样例材料并创建世界…", "pending");
      try {
        const installed = await this.ctx.api.installSample(String(sample.id), `${requestId}:sample`);
        const created = await this.ctx.api.createInstance({
          package_path: String(installed.package_file ?? ""),
          card_paths: [character.value].filter(Boolean),
          display_name: worldName.value.trim() || undefined,
          request_id: `${requestId}:create`,
        });
        const instance = created.instance as Json;
        const info = await this.ctx.api.instanceInfo(String(instance.id));
        const timelines = (info.timelines as Json[]) ?? [];
        const timelineId = String(timelines[0]?.id ?? "");
        const characters = (info.characters as Json[]) ?? [];
        const picked = characters.find((item) => String((item as Json).name ?? "") === selectedName(character)) ?? characters[0];
        await this.ctx.setPrefs({
          "onboard.instance": String(instance.id),
          "onboard.timeline": timelineId,
          "onboard.character.id": String(picked?.card_id ?? ""),
          "onboard.character_name": String(picked?.name ?? ""),
          "onboard.world_name": String(instance.name ?? ""),
        });
        await this.ctx.refresh();
        setNote(note, `已创建「${String(instance.name ?? "")}」（默认暂停；下一步开始运行并联络）`, "ok");
        await this.goto("start");
        void info;
      } catch (error) {
        const info = uiError(error, {
          module: "准备材料",
          action: "创建世界",
          target: worldName.value,
          done: "随程序样例材料已复制到你的创作目录（可重复使用）",
          unknown: "世界是否创建成功",
        });
        setNote(note, "创建没有完成：材料已保留，可以按下面的原因处理后重试（重复点击不会创建第二个世界）", "bad");
        results.appendChild(
          errorCard(info, [
            { label: "重试创建", run: () => void start() },
            {
              label: "换一个世界名",
              run: () => {
                worldName.value = `${worldName.value}-2`;
                worldName.focus();
              },
            },
          ]),
        );
      } finally {
        this.busy = false;
      }
    };

    const selectedName = (node: HTMLSelectElement): string =>
      node.selectedOptions[0]?.textContent ?? "";

    host.appendChild(
      section(
        "准备材料",
        paragraph("用随程序提供的样例世界开始：只需要选一位角色，不需要自己拼装文件或填写标识。"),
        field("样例", sampleSelect),
        intro,
        field("想先和谁联络", character),
        field("世界名称", worldName),
        el(
          "div",
          { class: "u-row" },
          primary("创建并开始联络", () => void start()),
          button("返回", () => void this.goto("task")),
        ),
        note,
        results,
      ),
    );
  }

  /** ④ 之后的落地页：世界已经创建，选择「开始运行并联络」还是「只创建，暂不运行」 */
  private renderStartSection(host: HTMLElement): void {
    const instanceId = String(this.ctx.prefs["onboard.instance"] ?? "");
    const timelineId = String(this.ctx.prefs["onboard.timeline"] ?? "");
    const characterId = String(this.ctx.prefs["onboard.character.id"] ?? "");
    const name = String(this.ctx.prefs["onboard.character_name"] ?? "");
    const world = String(this.ctx.prefs["onboard.world_name"] ?? "");
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const details = el("div", { class: "u-test-results" });
    // 「选择第一件事」定的落点：联络 / 写作 / 跑团
    const target = appPane(String(this.ctx.prefs["onboard.task"] ?? this.ctx.prefs.app_mode ?? "chat"));

    const begin = async (activate: boolean): Promise<void> => {
      setNote(note, activate ? "正在启动这条世界线…" : "已创建，暂不运行", "pending");
      try {
        if (activate && instanceId && timelineId) {
          await this.ctx.api.activate(instanceId, timelineId, 1);
        }
        await this.ctx.setPrefs({
          "sel.contact": {
            instance_id: instanceId,
            timeline_name: world,
            timeline_id: timelineId,
            character_id: characterId,
            character_name: name,
          },
          "onboard.done": true,
        });
        setNote(note, activate ? `已开始运行，正在打开${paneTitle(target)}…` : "已创建，暂不运行；随时可以在世界详情里启动", "ok");
        this.ctx.navigate({ pane: target });
      } catch (error) {
        const info = uiError(error, {
          module: "开始使用",
          action: "启动世界",
          target: world,
          done: "世界已经创建（不会重复创建）",
          unknown: "世界线是否已经运行",
        });
        details.appendChild(errorCard(info, [{ label: "重试启动", run: () => void begin(true) }]));
      }
    };

    host.appendChild(
      section(
        "开始使用",
        paragraph(
          `世界「${world}」已经创建。它的设定与角色已经定稿：以后改模板也不会倒回去改这一个世界。`,
        ),
        el(
          "div",
          { class: "u-row" },
          primary("开始运行并联络", () => void begin(true)),
          button("只创建，暂不运行", () => void begin(false)),
        ),
        note,
        details,
      ),
    );
  }
}

async function copyDiagnostics(ctx: AppContext): Promise<void> {
  const readiness = ctx.readiness ?? {};
  const lines = [
    `isekai 本机检查 ${stamp(Date.now() / 1000)}`,
    `程序版本：${String(readiness.app ?? "-")}｜数据格式：${String(readiness.data_format ?? "-")}｜规则：${String(readiness.rules ?? "-")}`,
    ...(((readiness.checks as Json[]) ?? []).map((item) => `${String(item.label)}：${item.ok ? "通过" : String(item.detail ?? "")}`)),
    "（未包含密钥、聊天正文、记忆与世界内部数据）",
  ];
  try {
    await navigator.clipboard.writeText(lines.join("\n"));
  } catch {
    /* 剪贴板不可用时忽略：用户仍可在帮助页查看 */
  }
}
