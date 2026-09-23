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
import { button, el, errorCard, facts, field, fill, paragraph, primary, section, setNote, stamp } from "./dom";

type StepId = "check" | "ai" | "task" | "material" | "start";

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

const PRESET_SERVICES: Array<{ id: string; label: string; base_url: string; model: string; key_url: string }> = [
  {
    id: "deepseek",
    label: "DeepSeek",
    base_url: "https://api.deepseek.com",
    model: "deepseek-v4-flash",
    key_url: "https://platform.deepseek.com/api_keys",
  },
  { id: "other", label: "其他兼容服务（OpenAI 兼容接口）", base_url: "", model: "", key_url: "" },
];

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
   * 点名的那一步之前，前置（本机检查 / 连接 AI）都过完了才直接落到点名那一步；
   * 前置还缺就按向导顺序从第一步走——配置好的用户不该被「从样例世界开始」扔回本机检查，
   * 没配置完的用户也不该被跳过配置。
   */
  private entryStep(asked: StepId): StepId {
    const readiness = this.ctx.readiness ?? {};
    const configured = Boolean((readiness.ai as Json | undefined)?.configured);
    return readiness.ready && configured ? asked : "check";
  }

  private async render(): Promise<void> {
    const host = this.root;
    if (!host) return;
    const page = el("div", { class: "u-page" });
    page.appendChild(el("h2", { class: "u-h2", text: "首次设置" }));
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

  private stepper(): HTMLElement {
    const list = el("ol", { class: "u-steps" });
    const index = STEPS.findIndex((item) => item.id === this.step);
    STEPS.forEach((item, position) => {
      const state = position === index ? "current" : position < index ? "done" : "todo";
      list.appendChild(el("li", { class: `u-step u-step-${state}`, text: item.label }));
    });
    return list;
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
    host.appendChild(
      section(
        "本机环境",
        paragraph("先确认程序和数据位置可用；这一步不涉及 AI 密钥，也不写任何东西。"),
        facts(checks.map((item) => [String(item.label), `${item.ok ? "通过" : "需要处理"}：${String(item.detail ?? "")}`])),
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
        button("重新检查", () => void this.recheck()),
      ),
    );
    // 已有开发版数据的用户（§3.2）：从旧目录整份搬过来，而不是手动拷文件
    host.appendChild(migrateCard(this.ctx));
  }

  private async recheck(): Promise<void> {
    await this.ctx.refresh();
    await this.render();
  }

  /* ---------------------------------------------------------------- ② 连接 AI */

  private renderAi(host: HTMLElement): void {
    const ai = ((this.ctx.readiness.ai as Json) ?? {}) as Json;
    const saved = ((this.ctx.settings.llm as Json) ?? {}) as Json;
    const service = el("select", { class: "u-input", id: "onb-service" }) as HTMLSelectElement;
    for (const item of PRESET_SERVICES) {
      service.appendChild(el("option", { value: item.id, text: item.label }));
    }
    service.value = String(this.ctx.prefs["onboard.service"] ?? (saved.base_url === PRESET_SERVICES[0].base_url ? "deepseek" : "other"));
    const baseUrl = el("input", { class: "u-input", id: "onb-base-url", value: String(saved.base_url ?? "") }) as HTMLInputElement;
    const model = el("input", { class: "u-input", id: "onb-model", value: String(saved.model ?? ""), placeholder: "推荐模型" }) as HTMLInputElement;
    const key = el("input", { class: "u-input", id: "onb-key", type: "password", autocomplete: "off", placeholder: String(ai.api_key_set ? `已设置（${String(ai.api_key_masked)}）；留空表示不改` : "粘贴访问密钥") }) as HTMLInputElement;
    const timeout = el("input", { class: "u-input", id: "onb-timeout", type: "number", min: "1", value: String(saved.timeout_s ?? 60) }) as HTMLInputElement;
    const maxTokens = el("input", { class: "u-input", id: "onb-max-tokens", type: "number", min: "1", value: String(saved.max_tokens ?? 1024) }) as HTMLInputElement;
    const temperature = el("input", { class: "u-input", id: "onb-temp", type: "number", min: "0", max: "2", step: "0.1", value: String(saved.temperature ?? 0.8) }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const results = el("div", { class: "u-test-results" });

    const advanced = el(
      "details",
      { class: "u-advanced" },
      el("summary", { text: "高级选项：服务地址、等待时间、单次输出长度、生成随机程度" }),
      field("服务地址（接口地址，不是聊天网页网址）", baseUrl),
      field("等待时间（秒）", timeout),
      field("单次输出长度（token）", maxTokens),
      field("生成随机程度（0–2）", temperature),
    );

    const applyPreset = () => {
      const preset = PRESET_SERVICES.find((item) => item.id === service.value) ?? PRESET_SERVICES[0];
      if (preset.base_url) baseUrl.value = preset.base_url;
      if (preset.model) model.value = preset.model;
      const hint = host.querySelector("#onb-key-hint");
      if (hint) {
        hint.textContent = preset.key_url
          ? `密钥在服务的官方密钥管理页取得（网页聊天账号登录与接口密钥可能不是一回事）。`
          : "自定义服务：地址、模型与密钥都按服务提供方的说明填写。";
      }
    };
    service.addEventListener("change", () => {
      void this.ctx.setPrefs({ "onboard.service": service.value });
      applyPreset();
    });

    const collect = (): Json => ({
      base_url: baseUrl.value.trim(),
      model: model.value.trim(),
      ...(key.value.trim() ? { api_key: key.value.trim() } : {}),
      timeout_s: Number(timeout.value || 60),
      max_tokens: Number(maxTokens.value || 1024),
      temperature: Number(temperature.value || 0),
    });

    const save = async (verified: boolean): Promise<boolean> => {
      try {
        const payload = collect();
        if (!payload.base_url || !payload.model) {
          setNote(note, "服务地址与模型是必填项", "bad");
          return false;
        }
        if (baseUrl.value.trim() && !/^https?:\/\//.test(baseUrl.value.trim())) {
          setNote(note, "服务地址要以 http:// 或 https:// 开头（这是接口地址，不是聊天网页网址）", "bad");
          return false;
        }
        await this.ctx.api.saveSettings({ llm: payload });
        await this.ctx.refresh();
        await this.ctx.setPrefs({
          "ai.tested_at": verified ? Date.now() / 1000 : this.ctx.prefs["ai.tested_at"] ?? 0,
          "ai.verified": verified,
        });
        setNote(note, verified ? "已保存，基础能力已验证" : "已保存未验证的配置（新生成仍会按运行时错误提示）", verified ? "ok" : "pending");
        key.value = "";
        return true;
      } catch (error) {
        const info = uiError(error, { module: "设置", action: "保存 AI 配置" });
        results.appendChild(errorCard(info, [{ label: "返回继续编辑", run: () => key.focus() }]));
        return false;
      }
    };

    const runTest = async (): Promise<void> => {
      if (this.busy) return;
      this.busy = true;
      fill(results);
      setNote(note, "正在测试：检查地址 / 验证访问 / 检查回复格式…", "pending");
      try {
        const result = await this.ctx.api.testAi(collect());
        const checks = (result.checks as Json[]) ?? [];
        fill(
          results,
          facts(
            checks.map((item) => [
              String(item.label),
              `${item.ok ? "通过" : "未通过"}：${String(item.detail ?? "")}`,
            ]),
          ),
          el(
            "p",
            { class: "u-hint" },
            `服务 ${String(result.service ?? "")}｜模型 ${String(result.model ?? "")}`
              + (result.key_set ? `｜密钥 ${String(result.api_key_masked ?? "")}` : "")
              + `｜用时 ${String(result.duration_ms ?? 0)} 毫秒｜调用 ${String(result.calls ?? 0)} 次（上限 ${String(result.call_budget ?? 4)}）`,
          ),
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
        this.busy = false;
      }
    };

    host.appendChild(
      section(
        "连接 AI",
        paragraph("生成所需的文字会发送给你选择的服务。测试只发送两段固定测试文字，不发送你的世界与聊天记录，可能产生少量用量。"),
        field("服务", service),
        el("p", { class: "u-hint", id: "onb-key-hint", text: "密钥在服务的官方密钥管理页取得。" }),
        field("模型", model, "名称从服务提供方取得；写错不会被自动纠正"),
        field("访问密钥", key),
        advanced,
        el(
          "div",
          { class: "u-row" },
          primary("测试并保存", () => void runTest()),
          button("稍后配置，先整理素材", () => void this.goto("task")),
        ),
        note,
        results,
      ),
    );
    applyPreset();
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
      choose("writer", "辅助写作", "整理大纲、观察角色能知道的事、比较下一步方案，保存文字草稿。", missing("还缺：世界与观察角色（这一步会建）"), "开始写作"),
      choose("gm", "进行跑团", "选规则与角色，声明行动，确认后得到裁定与后果。", "还缺：一份已登记的规则插件（在跑团页登记）", "开始跑团"),
    );
    host.appendChild(
      section(
        "选择第一件事",
        paragraph("任务选择不创建第三套项目数据：世界、时间线、大纲与战役仍由各自模块管理。"),
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
      results.appendChild(errorCard(uiError(error, { module: "样例", action: "读取随程序样例" })));
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
          "创建后将以默认速度开始运行；退出后再次启动会补算，暂停则不会。",
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
          unknown: "时间线是否已经运行",
        });
        details.appendChild(errorCard(info, [{ label: "重试启动", run: () => void begin(true) }]));
      }
    };

    host.appendChild(
      section(
        "开始使用",
        paragraph(
          `世界「${world}」已经创建；这个世界设定与角色设定已经固化，之后改模板不会追溯改写它。`,
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
