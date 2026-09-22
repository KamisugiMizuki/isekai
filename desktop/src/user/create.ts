/*
 * 创建世界（USER_INTERFACE_DESIGN §5.2 / §5.3）。
 *
 * 顺序固定：选择来源 → 编辑世界设定 → 准备角色 → 检查与确认 → 创建世界 → 选择开始方式。
 * 两层控制：参数层给生成要求（体裁 / 计数 / 必含与禁忌），条目层是真正能改的表单
 * （点条目在详情表单里编辑，锁定 = AI 不覆盖），校验是最终硬边界。
 * 用户只填显示名，素材文件名由应用管理；stable id 由系统给，改名字不动引用身份。
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

type Step = "source" | "world" | "cards" | "review" | "create" | "start";

/** 分区名的用户说法（内核用点分路径，界面不暴露路径本身） */
const SECTION_LABELS: Record<string, string> = {
  "world.axioms": "世界公理",
  "world.institutions": "制度与职位",
  "world.customs": "惯例",
  "environment.types": "环境状态",
  sources: "信息来源",
  canon: "实情",
  narratives: "说法",
  entities: "人物与地点",
  races: "种族",
  historiography: "史料",
  "events.families": "事件族",
  "events.calendar": "节庆",
  life: "生活安排",
  roles: "角色位",
  "comms.mechanisms": "联络方式",
  "initial_state.mysteries": "谜题",
  "initial_state.rumors": "流言",
  "initial_state.events": "开场事件",
  "calendar.months": "月份",
  "calendar.segments": "时段",
};

const FIELD_LABELS: Record<string, string> = {
  name: "名称",
  text: "内容",
  statement: "实情",
  tags: "标签",
  kind: "类型",
  reach: "流传范围",
  source_id: "出处",
  canon_ref: "对应实情",
  obtain: "获知方式",
  confidence: "确信程度",
  race_id: "种族",
  born: "出生年份",
  died: "卒于",
  lifespan: "寿命（年）",
  mandate: "职责",
  scope: "管辖范围",
  succession: "继承方式",
  validity: "有效条件",
  offices: "职位",
  vacancy_policy: "空缺处理",
  applies_to: "适用对象",
  practice: "做法",
  basis: "依据",
  variation: "地区差异",
  forms: "形式",
  initial: "初始值",
  values: "允许值",
  observe: "观测条件",
  observers: "观测者",
  expiry: "失效方式",
  title: "标题",
  contributors: "编写者",
  written_at: "成书时刻",
  compiled_at: "汇编时刻",
  coverage: "覆盖范围",
  genre: "体裁",
  stance: "立场",
  entries: "收录条目",
  sleep: "作息类型",
  windows: "时段安排",
  description: "说明",
  life_template: "生活安排",
  channels: "联络方式",
  limits: "限制",
  month: "月份",
  day: "日",
  family: "事件族",
  templates: "事件模板",
  question: "问题",
  refs: "关联",
  density: "事件密度",
  start: "开始",
  end: "结束",
  days: "天数",
  office: "职位",
  holder: "在任者",
  policy: "处理方式",
  min_years: "最短",
  max_years: "最长",
  // 角色卡
  self_identity: "自我认同",
  gender: "性别",
  occupation: "职业",
  creator: "由谁创造",
  self_knowledge: "自知程度",
  ref_type: "引用类型",
  ref_id: "引用对象",
  obtained_at: "何时知道",
  mechanism_id: "联络方式",
  note: "备注",
  intent: "意图",
  semantic: "语义",
  driver: "驱动",
  strength: "强度",
  window: "窗口",
  preconditions: "前提",
  effect: "效果",
  mode: "认知模式",
  sources: "依据来源",
  routine_note: "作息说明",
  appearance: "外貌",
};

const LONG_KEYS = new Set([
  "text",
  "statement",
  "practice",
  "basis",
  "mandate",
  "scope",
  "succession",
  "validity",
  "variation",
  "description",
  "question",
  "coverage",
  "genre",
  "stance",
  "title",
  "vacancy_policy",
  "policy",
]);

const CHOICES: Record<string, string[]> = {
  kind: ["person", "place", "thing", "organization"],
  confidence: ["true", "believed", "disputed", "false"],
  density: ["sparse", "normal", "dense"],
  sleep: ["true", "false"],
};

/** 参数层旋钮（与核心生成器的键同名；这里是生成要求，不是硬约束） */
const KNOB_TEXT: Array<[string, string]> = [
  ["genre", "体裁"],
  ["tone", "基调"],
  ["supernatural", "超自然在场度"],
  ["tech", "技术水位"],
  ["naming", "命名风格"],
  ["conflict", "冲突主线"],
  ["era_start", "纪元起点"],
  ["current_year", "当前年"],
  ["history_depth", "史料深度"],
];
const KNOB_COUNT: Array<[string, string]> = [
  ["axioms", "世界公理"],
  ["regions", "区域"],
  ["institutions", "制度（含职位）"],
  ["customs", "惯例"],
  ["env_types", "环境类型"],
  ["races", "种族"],
  ["roles", "角色位"],
  ["lexicon", "用词表"],
  ["sources", "传本"],
  ["canon", "实情条目"],
  ["narratives", "说法条目"],
  ["entities", "登记实体"],
  ["life", "生活线模板"],
  ["families", "事件族"],
  ["festivals", "节庆"],
];
/** 段 → 顶层键（与核心 PACKAGE_SEGMENTS 同源：填段重跑要的是键列表） */
const SEGMENT_KEYS: Array<[string, string[]]> = [
  ["设定核心", ["meta", "calendar", "world"]],
  ["双轨与名册", ["sources", "canon", "narratives", "races", "entities"]],
  ["机制与现状", ["historiography", "environment", "events", "life", "roles", "comms", "initial_state"]],
];

const KNOB_LIST: Array<[string, string]> = [
  ["include", "必须出现"],
  ["exclude", "禁止出现"],
  ["homage", "可参考致敬"],
];

interface Section {
  path: string;
  label: string;
  items: Json[];
}

function label(key: string): string {
  const short = key.split(".").pop() ?? key;
  return FIELD_LABELS[short] ?? short;
}

/** 角色卡的分组标题（顶层键 → 用户说法） */
const GROUP_LABELS: Record<string, string> = {
  identity: "身份",
  background: "来历",
  first_contact: "初次接触",
  cognition: "认知",
  life_template: "作息",
  meta: "状态",
  channels: "信息来源",
  initial_knowledge: "初始知道的事",
  comms: "联络方式",
  initial_units: "性格单元",
  intents: "当前意图",
};

/** 分区目录：段内所有「有 id 的条目列表」（按路径升序，标签用用户说法） */
export function sectionsOf(pkg: Json): Section[] {
  const out: Section[] = [];
  const walk = (node: unknown, path: string, depth: number): void => {
    if (!node || typeof node !== "object" || depth > 2) return;
    if (Array.isArray(node)) {
      const items = node as Json[];
      if (items.length && items.every((item) => item && typeof item === "object" && item.id)) {
        out.push({ path, label: SECTION_LABELS[path] ?? path.split(".").pop() ?? path, items });
      }
      return;
    }
    for (const [key, value] of Object.entries(node as Json)) {
      if (key === "meta" || key === "identity") continue;
      walk(value, path ? `${path}.${key}` : key, depth + 1);
    }
  };
  walk(pkg, "", 0);
  // 目录按段排（设定核心 → 双轨与名册 → 机制与现状），段内按路径
  const order = (path: string): number => {
    const top = path.split(".")[0];
    const index = SEGMENT_KEYS.findIndex(([, keys]) => keys.includes(top));
    return index < 0 ? SEGMENT_KEYS.length : index;
  };
  return out.sort((a, b) => order(a.path) - order(b.path) || a.path.localeCompare(b.path));
}

/** 引用候选：把包内各个 id 的（id → 可读名）摊平，供关联字段选择 */
function refIndex(pkg: Json): Record<string, Array<{ id: string; label: string }>> {
  const out: Record<string, Array<{ id: string; label: string }>> = {};
  for (const section of sectionsOf(pkg)) {
    out[section.path] = section.items.map((item) => ({
      id: String(item.id),
      label: String(item.name ?? item.text ?? item.statement ?? item.title ?? item.id).slice(0, 40),
    }));
  }
  return out;
}

function refCandidates(key: string, refs: Record<string, Array<{ id: string; label: string }>>): Array<{ id: string; label: string }> {
  const pairs: Record<string, string> = {
    race_id: "races",
    source_id: "sources",
    canon_ref: "canon",
    life_template: "life",
    family: "events.families",
    refs: "entities",
    channel: "comms.mechanisms",
    mechanism_id: "comms.mechanisms",
  };
  return refs[pairs[key] ?? key] ?? [];
}

function isRefKey(key: string): boolean {
  return key.endsWith("_id") || key.endsWith("_ref") || key === "refs" || key === "family" || key === "life_template";
}

/** 一个字段的控件：类型由值本身决定（短文本 / 长文本 / 数字 / 开关 / 允许值下拉 / 关联选择） */
function controlFor(
  key: string,
  value: unknown,
  refs: Record<string, Array<{ id: string; label: string }>>,
  set: (next: unknown) => void,
): HTMLElement {
  if (isRefKey(key)) {
    const box = el("select", { class: "u-input" }) as HTMLSelectElement;
    box.appendChild(el("option", { value: "", text: "（不指定）" }));
    for (const item of refCandidates(key, refs)) {
      box.appendChild(el("option", { value: item.id, text: `${item.label}（${item.id.slice(0, 6)}）` }));
    }
    box.value = value === null || value === undefined ? "" : String(value);
    box.addEventListener("change", () => set(box.value || null));
    return box;
  }
  if (CHOICES[key]) {
    const box = el("select", { class: "u-input" }) as HTMLSelectElement;
    for (const option of CHOICES[key]) box.appendChild(el("option", { value: option, text: option }));
    if (value !== null && value !== undefined) box.value = String(value);
    box.addEventListener("change", () => set(key === "sleep" ? box.value === "true" : box.value));
    return box;
  }
  if (Array.isArray(value)) {
    const allStrings = value.every((item) => typeof item === "string");
    if (allStrings) {
      const area = el("textarea", { class: "u-textarea", rows: "2", placeholder: "一行一条" }) as HTMLTextAreaElement;
      area.value = (value as string[]).join("\n");
      area.addEventListener("input", () =>
        set(area.value.split("\n").map((item) => item.trim()).filter(Boolean)),
      );
      return area;
    }
    // 嵌套对象数组（职位 / 时段 / 模板…）：子条目表单，一层深
    return subList(key, value as Json[], set);
  }
  if (typeof value === "boolean") {
    const tick = el("input", { type: "checkbox" }) as HTMLInputElement;
    tick.checked = value;
    tick.addEventListener("change", () => set(tick.checked));
    return tick;
  }
  if (typeof value === "number") {
    const num = el("input", { class: "u-input u-input-narrow", type: "number", value: String(value) }) as HTMLInputElement;
    num.addEventListener("input", () => set(Number(num.value || 0)));
    return num;
  }
  if (value && typeof value === "object") {
    // 数值带单位一类的小对象（lifespan 等）：展开成两个数字
    const box = el("div", { class: "u-row" });
    const node = value as Json;
    for (const [sub, subValue] of Object.entries(node)) {
      if (typeof subValue !== "number") continue;
      const num = el("input", {
        class: "u-input u-input-narrow",
        type: "number",
        value: String(subValue),
        title: label(sub),
      }) as HTMLInputElement;
      num.addEventListener("input", () => set({ ...(node as Json), [sub]: Number(num.value || 0) }));
      box.appendChild(el("span", { class: "u-hint", text: label(sub) }));
      box.appendChild(num);
    }
    if (box.childElementCount) return box;
    return el("span", { class: "u-hint", text: "（这项结构复杂，先留原样）" });
  }
  const long = LONG_KEYS.has(key);
  const node = long
    ? (el("textarea", { class: "u-textarea", rows: "3" }) as HTMLTextAreaElement)
    : (el("input", { class: "u-input" }) as HTMLInputElement);
  node.value = value === null || value === undefined ? "" : String(value);
  node.addEventListener("input", () => set(node.value));
  return node;
}

/** 子条目列表（一层深）：每条给可编辑的标量字段，能加能删 */
function subList(key: string, items: Json[], set: (next: unknown) => void): HTMLElement {
  const box = el("div", { class: "u-sublist" });
  const state: Json[] = items.length ? items.map((item) => ({ ...item })) : [];
  const render = (): void => {
    fill(box);
    state.forEach((item, index) => {
      const row = el("div", { class: "u-card u-sublist-item" });
      row.appendChild(el("h4", { text: `${label(key)} ${index + 1}` }));
      for (const [sub, value] of Object.entries(item)) {
        if (typeof value !== "string" && typeof value !== "number") continue;
        const input = el("input", { class: "u-input", value: String(value) }) as HTMLInputElement;
        input.addEventListener("input", () => {
          item[sub] = typeof value === "number" ? Number(input.value || 0) : input.value;
          set([...state]);
        });
        row.appendChild(field(label(sub), input));
      }
      row.appendChild(
        button("删除这条", () => {
          state.splice(index, 1);
          set([...state]);
          render();
        }),
      );
      box.appendChild(row);
    });
    box.appendChild(
      button(`添加一条${label(key)}`, () => {
        state.push({});
        set([...state]);
        render();
      }),
    );
  };
  render();
  return box;
}

export class CreatePane implements Pane {
  readonly id = "create" as const;
  private step: Step = "source";
  private root: HTMLElement | null = null;
  private note: HTMLElement | null = null;
  private candidate: Json | null = null;
  private locks: Record<string, string[]> = {};
  private errors: string[] = [];
  private usage: Json | null = null;
  private base: string = ""; // 素材标识（应用管理；用户只填显示名）
  private name = "";
  private brief = "";
  private source = "";
  private knobs: Record<string, unknown> = {};
  private section = "";
  private entryId = "";
  private cardFiles: string[] = [];
  /** 角色卡工作区：列表 / 起草编辑 */
  private cardStep: "list" | "draft" = "list";
  private card: Json | null = null;
  private cardName = "";
  private cardBrief = "";
  private cardLocks: string[] = [];
  private cardErrors: string[] = [];
  private worldName = "";
  private created: Json | null = null;
  private busy = false;

  constructor(private readonly ctx: AppContext) {}

  mount(host: HTMLElement): void {
    this.root = el("div", { class: "u-create" });
    const nav = el("nav", { class: "u-crumbs", "aria-label": "创建步骤" });
    for (const [id, text] of [
      ["source", "选择来源"],
      ["world", "世界设定"],
      ["cards", "准备角色"],
      ["review", "检查与确认"],
      ["create", "创建世界"],
      ["start", "开始方式"],
    ] as Array<[Step, string]>) {
      const step = button(text, () => {
        // 只允许回到已经走到的步骤，避免跳过检查
        if (this.allowed(id)) {
          this.step = id;
          void this.render();
        }
      });
      step.disabled = id !== this.step && !this.allowed(id);
      step.dataset.step = id;
      if (id === this.step) step.classList.add("u-nav-active");
      nav.appendChild(step);
    }
    this.note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    fill(host, section("创建世界", nav), this.note, this.root);
    const sub = this.ctx.route.sub ?? "";
    if (sub.startsWith("edit:")) {
      void this.openExisting(sub.slice(5));
    } else if (sub.startsWith("draft:")) {
      void this.openDraft(sub.slice(6));
    } else {
      void this.render();
    }
  }

  /** 从「世界与素材」进来的：编辑一份已有设定（改完另存/覆盖都由确认那一步统一处理） */
  private async openExisting(file: string): Promise<void> {
    if (!file) {
      void this.render();
      return;
    }
    setNote(this.note, "正在打开这份世界设定…", "pending");
    try {
      const loaded = await this.ctx.api.packageLoad(file);
      this.absorbPackage((loaded.package as Json) ?? {});
      this.base = file;
      this.source = this.source || "manual";
      setNote(this.note, `正在编辑「${this.name}」：这份设定用于以后创建的世界，已有的世界不会变`, "ok");
      this.step = "world";
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界设定", action: "打开" }).message, "bad");
      void this.render();
    }
  }

  /** 从「未完成内容」进来的：接着改上次那份草稿（草稿不是正式设定） */
  private async openDraft(key: string): Promise<void> {
    if (!key) {
      void this.render();
      return;
    }
    setNote(this.note, "正在读回草稿…", "pending");
    try {
      const result = await this.ctx.api.draftLoad(key);
      const draft = (result.draft as Json) ?? {};
      const payload = (draft.payload as Json) ?? {};
      if (payload.package) {
        this.absorbPackage(payload.package as Json);
        this.locks = (payload.locks as Record<string, string[]>) ?? {};
        this.errors = ((payload.errors as string[]) ?? []).slice();
        this.brief = String(payload.brief ?? "");
        this.knobs = (payload.knobs as Record<string, unknown>) ?? {};
        this.source = String(payload.source ?? "manual");
      }
      setNote(this.note, "草稿已读回：接着改，确认后才成为正式设定", "ok");
      this.step = "world";
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界设定", action: "读回草稿" }).message, "bad");
      void this.render();
    }
  }

  private allowed(step: Step): boolean {
    if (step === "source") return true;
    if (step === "world") return Boolean(this.candidate);
    if (step === "cards") return Boolean(this.candidate) && !this.errors.length;
    if (step === "review") return this.cardFiles.length > 0 && !this.errors.length;
    if (step === "create") return this.cardFiles.length > 0 && !this.errors.length;
    return Boolean(this.created);
  }

  private async render(): Promise<void> {
    if (!this.root) return;
    fill(this.root);
    try {
      if (this.step === "source") this.renderSource();
      else if (this.step === "world") this.renderWorld();
      else if (this.step === "cards") {
        if (this.cardStep === "draft") this.renderCardEditor();
        else await this.renderCards();
      }
      else if (this.step === "review") this.renderReview();
      else if (this.step === "create") this.renderCreate();
      else this.renderStart();
    } catch (error) {
      this.root.appendChild(errorCard(uiError(error, { module: "创建世界", action: "打开发这一步" })));
    }
  }

  private goto(step: Step): void {
    this.step = step;
    void this.render();
  }

  /* ------------------------------------------------------------ 步骤一：来源 */

  private renderSource(): void {
    const host = this.root!;
    const make = (title: string, body: string, run: () => void): HTMLElement => {
      const card = el("article", { class: "u-card" });
      card.appendChild(el("h3", { text: title }));
      card.appendChild(paragraph(body));
      card.appendChild(primary("用这个", run));
      return card;
    };
    host.appendChild(paragraph("世界设定是创建世界的底稿：先建一份设定，再由它创建出会保存进展的世界。改设定只影响以后创建的世界。"));
    host.appendChild(
      el(
        "div",
        { class: "u-cards" },
        make("让 AI 起草", "写一句想要的世界，AI 起草一份完整设定，你再逐条改。需要已配置 AI。", () => {
          this.source = "ai";
          this.candidate = null;
          this.goto("world");
        }),
        make("自己填写", "从空白骨架开始，按分区一条条写。不调用 AI。", () => {
          this.source = "manual";
          void this.startManual();
        }),
        make(
          "用样例设定",
          "拿随程序提供的灰潮纪当底稿，改成自己的。",
          () => {
            this.source = "sample";
            void this.startFromSample();
          },
        ),
        make("导入已有设定", "已经在别处有世界设定或角色卡：到「世界与素材 → 导入」带进来，再回来创建。", () =>
          this.ctx.navigate({ pane: "worlds" }),
        ),
      ),
    );
    if (this.candidate) {
      host.appendChild(
        el(
          "div",
          { class: "u-row" },
          primary("继续编辑这份设定", () => this.goto("world")),
        ),
      );
    }
  }

  private async startManual(): Promise<void> {
    setNote(this.note, "正在建空白骨架…", "pending");
    try {
      const result = await this.ctx.api.packageTemplate(this.name || "未命名世界");
      this.absorbPackage((result.package as Json) ?? {});
      setNote(this.note, "空白骨架已就绪：按分区一条条填，或者用「按一句话修改」让 AI 补", "ok");
      this.goto("world");
    } catch (error) {
      setNote(this.note, uiError(error, { module: "创建世界", action: "建空白骨架" }).message, "bad");
    }
  }

  private async startFromSample(): Promise<void> {
    setNote(this.note, "正在取样例设定…", "pending");
    try {
      const list = await this.ctx.api.packages();
      const items = (list.packages as Json[]) ?? [];
      const sample = items.find((item) => String(item.file ?? "").startsWith("huichao")) ?? items[0];
      if (!sample) {
        setNote(this.note, "创作目录里还没有样例：先到「世界与素材 → 从样例开始」装一份", "bad");
        return;
      }
      const loaded = await this.ctx.api.packageLoad(String(sample.file));
      this.absorbPackage((loaded.package as Json) ?? {});
      setNote(this.note, `已载入样例设定「${String(sample.name ?? sample.file)}」：改完另存为自己的`, "ok");
      this.goto("world");
    } catch (error) {
      setNote(this.note, uiError(error, { module: "创建世界", action: "载入样例设定" }).message, "bad");
    }
  }

  /* ------------------------------------------------------------ 步骤二：世界设定 */

  private absorbPackage(pkg: Json): void {
    this.candidate = pkg;
    const meta = (pkg.meta as Json) ?? {};
    // 用户填过的名字 / 描述优先：候选只补空缺，不把用户输入冲掉
    if (!this.name.trim()) this.name = String(meta.original_name ?? meta.display_name ?? "");
    if (!this.brief.trim()) this.brief = String(meta.description ?? "");
    this.errors = [];
    const sections = sectionsOf(pkg);
    this.section = sections[0]?.path ?? "";
    this.entryId = "";
  }

  private renderWorld(): void {
    const host = this.root!;
    const pkg = this.candidate!;
    const nameInput = el("input", { class: "u-input", value: this.name, id: "u-create-name" }) as HTMLInputElement;
    nameInput.addEventListener("input", () => {
      this.name = nameInput.value;
    });
    const brief = el("textarea", { class: "u-textarea", rows: "3", id: "u-create-brief", placeholder: "一句话说清这个世界是什么样" }) as HTMLTextAreaElement;
    brief.value = this.brief;
    brief.addEventListener("input", () => {
      this.brief = brief.value;
    });

    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        button("返回来源", () => this.goto("source")),
        button("保存草稿", () => void this.saveDraft()),
        primary("确认世界设定，去选角色", () => void this.confirmWorld()),
      ),
    );
    host.appendChild(field("世界名", nameInput));
    if (this.source === "ai") host.appendChild(field("一句话描述（重新生成时用）", brief));
    host.appendChild(this.knobPanel());

    if (this.source === "ai") {
      const instruction = el("input", { class: "u-input", id: "u-create-instruction", placeholder: "例如：多加两个内陆城邦，去掉海神" }) as HTMLInputElement;
      host.appendChild(
        el(
          "div",
          { class: "u-row" },
          primary(this.usage ? "重新生成一版（整包）" : "让 AI 起草", () => void this.generate()),
          instruction,
          button("按这句话改未锁定条目", () => void this.revise(instruction.value)),
        ),
      );
    }
    host.appendChild(paragraph("锁定 = AI 不覆盖这一条；改锁定条目要先点「解锁」。校验始终按最终内容判定，锁定不放宽要求。"));

    // 分区目录 + 当前分区的条目 + 条目表单
    const sections = sectionsOf(pkg);
    const columns = el("div", { class: "u-create-body" });
    const catalog = el("div", { class: "u-card u-create-catalog" });
    catalog.appendChild(el("h3", { text: "分区目录" }));
    for (const item of sections) {
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: item.label }));
      row.appendChild(el("span", { class: "u-hint", text: `${item.items.length} 条` }));
      const pick = button(item.path === this.section ? "在编辑" : "打开", () => {
        this.section = item.path;
        this.entryId = String(item.items[0]?.id ?? "");
        void this.render();
      });
      pick.dataset.section = item.path;
      row.appendChild(pick);
      catalog.appendChild(row);
    }
    if (!sections.length) {
      catalog.appendChild(paragraph("这份设定还没有条目：让 AI 起草，或用「添加一条」自己加。", "u-hint"));
    }
    columns.appendChild(catalog);

    const current = sections.find((item) => item.path === this.section) ?? sections[0];
    const entries = el("div", { class: "u-card u-create-entries" });
    entries.appendChild(el("h3", { text: current ? `${current.label}（${current.items.length} 条）` : "当前分区" }));
    if (current) {
      for (const item of current.items) {
        const row = el("div", { class: "u-row-line" });
        const tick = el("input", { type: "checkbox" }) as HTMLInputElement;
        tick.checked = (this.locks[current.path] ?? []).includes(String(item.id));
        tick.dataset.lock = String(item.id);
        tick.addEventListener("change", () => this.toggleLock(current.path, String(item.id), tick.checked));
        row.appendChild(tick);
        row.appendChild(el("span", { class: "u-grow", text: this.entryTitle(item) }));
        if (String(item.id) === this.entryId) row.appendChild(chip("正在编辑", "ok"));
        const open = button("编辑", () => {
          this.entryId = String(item.id);
          void this.render();
        });
        open.dataset.edit = String(item.id);
        row.appendChild(open);
        row.appendChild(
          button("复制", () => {
            this.duplicateEntry(current.path, item);
          }),
        );
        row.appendChild(
          button("删除", () => void this.deleteEntry(current.path, item)),
        );
        entries.appendChild(row);
      }
      entries.appendChild(button("添加一条", () => this.addEntry(current.path)));
    }
    columns.appendChild(entries);
    host.appendChild(columns);

    if (current) {
      const target = current.items.find((item) => String(item.id) === this.entryId) ?? current.items[0];
      if (target) {
        this.entryId = String(target.id);
        host.appendChild(this.entryForm(current, target));
        const again = button(`重新生成本区（${current.label}）`, () => void this.fillSection());
        again.disabled = this.source !== "ai";
        host.appendChild(
          el(
            "div",
            { class: "u-row" },
            again,
            paragraph(this.source === "ai" ? "只重跑这个分区，锁定条目原样保留。" : "自己填写的设定不重跑分区；要 AI 补可以用「按一句话修改」。", "u-hint"),
          ),
        );
      }
    }
    host.appendChild(this.checkPanel());
  }

  private knobPanel(): HTMLElement {
    const box = el("details", { class: "u-knobs" });
    box.appendChild(el("summary", { text: "更多参数（生成要求，可留空）" }));
    const grid = el("div", { class: "u-knob-grid" });
    for (const [key, text] of KNOB_TEXT) {
      const input = el("input", { class: "u-input" }) as HTMLInputElement;
      const saved = this.knobs[key];
      input.value = saved === undefined ? "" : String(saved);
      input.dataset.knob = key;
      input.addEventListener("input", () => this.setKnob(key, input.value.trim(), "text"));
      grid.appendChild(field(text, input));
    }
    for (const [key, text] of KNOB_COUNT) {
      const input = el("input", { class: "u-input u-input-narrow", type: "number", min: "0" }) as HTMLInputElement;
      const saved = this.knobs[key];
      input.value = saved === undefined ? "" : String(saved);
      input.dataset.knob = key;
      input.addEventListener("input", () => this.setKnob(key, input.value.trim(), "count"));
      grid.appendChild(field(text, input));
    }
    for (const [key, text] of KNOB_LIST) {
      const area = el("textarea", { class: "u-textarea", rows: "2", placeholder: "一行一条" }) as HTMLTextAreaElement;
      const saved = this.knobs[key];
      area.value = Array.isArray(saved) ? (saved as string[]).join("\n") : "";
      area.dataset.knob = key;
      area.addEventListener("input", () => this.setKnob(key, area.value, "list"));
      grid.appendChild(field(text, area));
    }
    box.appendChild(grid);
    box.appendChild(paragraph("计数是最多生成多少条；填 0 也不会删掉校验要求必需的部分。", "u-hint"));
    return box;
  }

  private setKnob(key: string, raw: string, kind: "text" | "count" | "list"): void {
    if (kind === "text") {
      if (raw) this.knobs[key] = raw;
      else delete this.knobs[key];
      return;
    }
    if (kind === "count") {
      if (raw === "") {
        delete this.knobs[key];
        return;
      }
      const value = Number(raw);
      if (Number.isInteger(value) && value >= 0) this.knobs[key] = value;
      return;
    }
    const lines = raw.split("\n").map((item) => item.trim()).filter(Boolean);
    if (lines.length) this.knobs[key] = lines;
    else delete this.knobs[key];
  }

  private entryTitle(item: Json): string {
    const main = item.name ?? item.text ?? item.statement ?? item.title ?? item.question ?? item.id;
    return `${String(main).slice(0, 48)}${String(item.id).length <= 8 ? "" : ""}`;
  }

  private entryForm(sectionRef: Section, item: Json): HTMLElement {
    const card = el("div", { class: "u-card u-create-form" });
    const locked = (this.locks[sectionRef.path] ?? []).includes(String(item.id));
    card.appendChild(el("h3", { text: `正在编辑：${this.entryTitle(item)}` }));
    card.appendChild(
      el(
        "div",
        { class: "u-row" },
        chip(locked ? "已锁定（AI 不覆盖）" : "未锁定", locked ? "ok" : "muted"),
        button(locked ? "解锁并编辑" : "锁定此条", () => this.toggleLock(sectionRef.path, String(item.id), !locked)),
        paragraph(`稳定标识 ${String(item.id)}（系统生成，改名字不影响引用）`, "u-hint"),
      ),
    );
    const refs = refIndex(this.candidate!);
    for (const [key, value] of Object.entries(item)) {
      if (key === "id") continue;
      const node = controlFor(key, value, refs, (next) => {
        if (locked) return;
        item[key] = next;
      });
      card.appendChild(field(label(key), node));
    }
    if (locked) card.appendChild(paragraph("这一条已锁定：先点「解锁并编辑」才能改。", "u-hint"));
    return card;
  }

  private checkPanel(): HTMLElement {
    const card = el("div", { class: "u-card u-create-check" });
    card.appendChild(el("h3", { text: "检查与预览" }));
    if (!this.errors.length) {
      card.appendChild(paragraph("这一份目前没有校验问题。", "u-hint"));
    } else {
      card.appendChild(paragraph(`还有 ${this.errors.length} 项需要处理：`));
      const list = el("ul", { class: "u-list" });
      for (const item of this.errors.slice(0, 20)) list.appendChild(el("li", { text: item }));
      card.appendChild(list);
      card.appendChild(
        el(
          "div",
          { class: "u-row" },
          button("定位第一项", () => this.locate(this.errors[0] ?? "")),
          primary("重新检查", () => void this.validate()),
        ),
      );
    }
    if (this.usage) {
      card.appendChild(
        facts([
          ["本次调用", `${String(this.usage.calls ?? "?")}/${String(this.usage.limit ?? "?")} 次`],
          ["条目合计", `${sectionsOf(this.candidate!).reduce((sum, item) => sum + item.items.length, 0)} 条`],
        ]),
      );
    }
    return card;
  }

  /** 校验问题定位：把问题里提到的标识对到分区与条目上（对不上就明说） */
  private locate(problem: string): void {
    const pkg = this.candidate!;
    for (const item of sectionsOf(pkg)) {
      for (const entry of item.items) {
        if (entry.id && problem.includes(String(entry.id))) {
          this.section = item.path;
          this.entryId = String(entry.id);
          setNote(this.note, `已定位到「${item.label} › ${this.entryTitle(entry)}」`, "ok");
          void this.render();
          return;
        }
      }
    }
    setNote(this.note, `这条问题没有直接指向某个条目，先看原文：${problem.slice(0, 120)}`, "pending");
  }

  private toggleLock(path: string, ident: string, on: boolean): void {
    const list = new Set(this.locks[path] ?? []);
    if (on) list.add(ident);
    else list.delete(ident);
    this.locks[path] = [...list];
    setNote(this.note, on ? "已锁定：AI 不会覆盖这一条" : "已解锁", "muted");
    void this.render();
  }

  private addEntry(path: string): void {
    const pkg = this.candidate!;
    const sectionRef = sectionsOf(pkg).find((item) => item.path === path);
    if (!sectionRef) return;
    const prefix = String(sectionRef.items[0]?.id ?? "x-1").split("-")[0] || "x";
    let index = sectionRef.items.length + 1;
    while (sectionRef.items.some((item) => String(item.id) === `${prefix}-新${index}`)) index += 1;
    const blank: Json = { id: `${prefix}-新${index}` };
    for (const [key, value] of Object.entries(sectionRef.items[0] ?? {})) {
      if (key === "id") continue;
      blank[key] = Array.isArray(value) ? [] : typeof value === "number" ? 0 : typeof value === "boolean" ? value : "";
    }
    sectionRef.items.push(blank);
    this.entryId = String(blank.id);
    setNote(this.note, "已添加一条：填完记得重新检查", "pending");
    void this.render();
  }

  private duplicateEntry(path: string, item: Json): void {
    const sectionRef = sectionsOf(this.candidate!).find((row) => row.path === path);
    if (!sectionRef) return;
    const prefix = String(item.id).split("-")[0] || "x";
    let index = sectionRef.items.length + 1;
    while (sectionRef.items.some((row) => String(row.id) === `${prefix}-副本${index}`)) index += 1;
    const copy: Json = { ...JSON.parse(JSON.stringify(item)), id: `${prefix}-副本${index}` };
    if (typeof copy.name === "string") copy.name = `${copy.name}（副本）`;
    sectionRef.items.push(copy);
    this.entryId = String(copy.id);
    setNote(this.note, "已复制一条（新身份）：其他条目对原条目的引用不会跟着变", "muted");
    void this.render();
  }

  /** 删除前先看有没有被引用（§5.3）：有引用就拦住并说清谁在用 */
  private async deleteEntry(path: string, item: Json): Promise<void> {
    const pkg = this.candidate!;
    const ident = String(item.id);
    const references: string[] = [];
    const scan = (node: unknown, where: string): void => {
      if (!node || typeof node !== "object") return;
      if (Array.isArray(node)) {
        node.forEach((child, index) => scan(child, `${where}[${index}]`));
        return;
      }
      for (const [key, value] of Object.entries(node as Json)) {
        if (key === "id") continue;
        if (typeof value === "string" && value === ident) references.push(`${where}.${key}`);
        else if (Array.isArray(value) && value.includes(ident)) references.push(`${where}.${key}`);
        else scan(value, `${where}.${key}`);
      }
    };
    scan(pkg, "");
    if (references.length) {
      setNote(
        this.note,
        `「${this.entryTitle(item)}」还被 ${references.length} 处引用（${references.slice(0, 3).join("、")}）：先改掉引用再删，避免留下空引用`,
        "bad",
      );
      return;
    }
    const sectionRef = sectionsOf(pkg).find((row) => row.path === path);
    if (!sectionRef) return;
    const modal = dialog(
      `删除「${this.entryTitle(item)}」？`,
      [paragraph("没有被引用，删掉不影响其他条目。这一步不能撤销，但可以重新添加。")],
      [
        {
          label: "删除",
          run: () => {
            const index = sectionRef.items.findIndex((row) => String(row.id) === ident);
            if (index >= 0) sectionRef.items.splice(index, 1);
            if (this.entryId === ident) this.entryId = "";
            setNote(this.note, "已删除一条", "muted");
            void this.render();
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  }

  /* ------------------------------------------------------------ AI 与校验 */

  private aiPayload(): Json {
    const payload: Json = { brief: this.brief, name: this.name || "未命名世界", knobs: this.knobs };
    if (this.candidate && this.usage) {
      payload.base = this.candidate;
      payload.locked = this.locks;
    }
    return payload;
  }

  private async generate(): Promise<void> {
    if (this.busy) return;
    if (!this.brief.trim()) {
      setNote(this.note, "先写一句「这个世界是什么样」（参数可以先不动）", "bad");
      return;
    }
    this.busy = true;
    setNote(this.note, "正在起草（会调用你的 AI，可能要等一会儿）…", "pending");
    try {
      const result = await this.ctx.api.packageGenerate(this.aiPayload());
      if (result.candidate) this.candidate = result.candidate as Json;
      this.errors = ((result.errors as string[]) ?? []).slice();
      this.usage = (result.usage as Json) ?? null;
      const meta = (this.candidate?.meta as Json) ?? {};
      if (!this.name) this.name = String(meta.original_name ?? "");
      this.section = sectionsOf(this.candidate ?? {}).at(0)?.path ?? "";
      setNote(
        this.note,
        this.errors.length ? `起草完成，但还有 ${this.errors.length} 项要处理（已保留这一版）` : "起草完成：逐条看看，改完再确认",
        this.errors.length ? "pending" : "ok",
      );
      void this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界设定", action: "起草", done: "没有改动已保存的材料" }).message, "bad");
    } finally {
      this.busy = false;
    }
  }

  private async revise(instruction: string): Promise<void> {
    if (!this.candidate) return;
    if (!instruction.trim()) {
      setNote(this.note, "先写一句要改什么", "bad");
      return;
    }
    setNote(this.note, "正在按这句话改…", "pending");
    try {
      const result = await this.ctx.api.packageRevise({
        package: this.candidate,
        instruction: instruction.trim(),
        locked: this.locks,
      });
      if (result.candidate) this.candidate = result.candidate as Json;
      this.errors = ((result.errors as string[]) ?? []).slice();
      this.usage = (result.usage as Json) ?? this.usage;
      setNote(this.note, "改完了：锁定的条目原样保留", "ok");
      void this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界设定", action: "按一句话修改", done: "候选没被采用" }).message, "bad");
    }
  }

  private async fillSection(): Promise<void> {
    if (!this.candidate || !this.section) return;
    const sectionRef = sectionsOf(this.candidate).find((item) => item.path === this.section);
    const segment = this.segmentOf(this.section);
    setNote(this.note, `正在重跑「${sectionRef?.label ?? this.section}」所在的那一段…`, "pending");
    try {
      const result = await this.ctx.api.packageFill({
        package: this.candidate,
        section: segment,
        knobs: this.knobs,
        locked: this.locks,
      });
      if (result.candidate) this.candidate = result.candidate as Json;
      this.errors = ((result.errors as string[]) ?? []).slice();
      this.usage = (result.usage as Json) ?? this.usage;
      setNote(this.note, "这一段重跑完了：锁定条目保留，其他分区没动", "ok");
      void this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界设定", action: "重新生成本区", done: "旧稿保留" }).message, "bad");
    }
  }

  /** 分区 → 段（段级重跑按段走：核心要的是顶层键列表，段名只用于界面说法） */
  private segmentOf(path: string): string {
    const top = path.split(".")[0];
    const segment = SEGMENT_KEYS.find(([, keys]) => keys.includes(top)) ?? SEGMENT_KEYS[2];
    return segment[1].join(",");
  }

  private async validate(): Promise<void> {
    if (!this.candidate) return;
    setNote(this.note, "正在检查…", "pending");
    try {
      const result = await this.ctx.api.packageValidate({ package: this.candidate });
      this.errors = ((result.errors as string[]) ?? []).slice();
      setNote(this.note, this.errors.length ? `还有 ${this.errors.length} 项要处理` : "检查通过", this.errors.length ? "pending" : "ok");
      void this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界设定", action: "检查" }).message, "bad");
    }
  }

  private async saveDraft(): Promise<void> {
    if (!this.candidate) return;
    if (!this.name.trim()) {
      setNote(this.note, "先给这份设定起个名字，草稿也要有名字", "bad");
      return;
    }
    try {
      await this.ctx.api.draftSave(this.name.trim(), "create", this.section, this.name.trim(), {
        package: this.candidate,
        locks: this.locks,
        errors: this.errors,
        brief: this.brief,
        knobs: this.knobs,
        source: this.source,
      });
      setNote(this.note, "草稿已保存：下次回来能接着改（草稿不是正式设定）", "ok");
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界设定", action: "保存草稿" }).message, "bad");
    }
  }

  /* ------------------------------------------------------------ 确认 → 角色 */

  private async confirmWorld(): Promise<void> {
    if (!this.candidate) return;
    const pkg = this.candidate;
    const meta = (pkg.meta as Json) ?? {};
    if (this.name.trim()) {
      meta.original_name = this.name.trim();
      meta.display_name = this.name.trim();
    }
    if (this.brief.trim()) meta.description = this.brief.trim();
    pkg.meta = meta;
    setNote(this.note, "正在检查这份设定…", "pending");
    try {
      const checked = await this.ctx.api.packageValidate({ package: pkg });
      this.errors = ((checked.errors as string[]) ?? []).slice();
      if (this.errors.length) {
        setNote(this.note, `还有 ${this.errors.length} 项没过：按问题清单改完再确认（可以先「保存草稿」）`, "bad");
        void this.render();
        return;
      }
      const saved = await this.ctx.api.packageSave(await this.uniqueFile(), pkg);
      this.base = String(saved.path ?? "");
      setNote(this.note, `世界设定已确认：${this.name}（改设定只影响以后创建的世界）`, "ok");
      this.goto("cards");
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界设定", action: "确认设定", done: "没有覆盖任何材料" }).message, "bad");
    }
  }

  /** 素材文件名由应用管理：按显示名生成，撞名就加序号（用户不需要自己起文件名） */
  private async uniqueFile(): Promise<string> {
    const base = (this.name.trim() || "world").replace(/[^\w\u4e00-\u9fa5-]/g, "_").slice(0, 40) || "world";
    const list = await this.ctx.api.packages();
    const taken = new Set(((list.packages as Json[]) ?? []).map((item) => String(item.file ?? "")));
    let candidate = `${base}.json`;
    let index = 2;
    while (taken.has(candidate)) candidate = `${base}-${index++}.json`;
    return candidate;
  }

  /* ------------------------------------------------------------ 步骤三：角色 */

  private async renderCards(): Promise<void> {
    const host = this.root!;
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        button("返回世界设定", () => this.goto("world")),
        primary("去检查与确认", () => this.goto("review")),
      ),
    );
    const cards = await this.ctx.api.cards();
    const items = (cards.cards as Json[]) ?? [];
    host.appendChild(
      paragraph("角色卡必须在创建前确认过；未确认的先点「确认角色卡」（它会按世界设定检查一遍）。"),
    );
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        primary("新建角色卡（AI 起草）", () => this.openCardDraft()),
        button("从文件导入角色卡", () => this.ctx.navigate({ pane: "worlds" })),
      ),
    );
    if (!items.length) {
      host.appendChild(
        section(
          "还没有角色卡",
          paragraph("可以在这里起草一张，或到「世界与素材 → 导入」带一张进来。"),
        ),
      );
      return;
    }
    const rows = el("div", { class: "u-rows" });
    for (const item of items) {
      const file = String(item.file ?? "");
      const picked = this.cardFiles.includes(file);
      const row = el("div", { class: "u-row-line" });
      const tick = el("input", { type: "checkbox" }) as HTMLInputElement;
      tick.checked = picked;
      tick.dataset.card = file;
      tick.addEventListener("change", () => {
        if (tick.checked) this.cardFiles = [...new Set([...this.cardFiles, file])];
        else this.cardFiles = this.cardFiles.filter((name) => name !== file);
        void this.render();
      });
      row.appendChild(tick);
      row.appendChild(el("span", { class: "u-grow", text: String(item.name ?? file) }));
      row.appendChild(chip(item.confirmed ? "可用于创建" : "未确认", item.confirmed ? "ok" : "pending"));
      row.appendChild(button("编辑", () => void this.openCardFile(file)));
      if (!item.confirmed) {
        row.appendChild(button("确认角色卡", () => void this.confirmCard(file)));
      }
      rows.appendChild(row);
    }
    host.appendChild(section("选择这一局的角色", rows));
    if (this.cardFiles.length) {
      host.appendChild(paragraph(`已选 ${this.cardFiles.length} 张：${this.cardFiles.join("、")}`, "u-hint"));
    }
  }

  /** 角色卡工作区（§5.3）：起草 → 逐字段改/锁定 → 保存草稿或确认角色卡 */
  private async openCardDraft(): Promise<void> {
    this.cardStep = "draft";
    this.card = null;
    this.cardName = "";
    this.cardBrief = "";
    this.cardLocks = [];
    this.cardErrors = [];
    await this.render();
  }

  private async openCardFile(file: string): Promise<void> {
    setNote(this.note, "正在打开这张角色卡…", "pending");
    try {
      const loaded = await this.ctx.api.cardLoad(file);
      this.card = (loaded.card as Json) ?? {};
      const meta = (this.card.meta as Json) ?? {};
      this.cardName = String((this.card.identity as Json)?.name ?? file);
      this.cardErrors = [];
      this.cardStep = "draft";
      setNote(this.note, `正在编辑「${this.cardName}」：确认后修改才生效（${meta.confirmed ? "这张卡已经确认过" : "还没确认"}）`, "ok");
      await this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "角色卡", action: "打开" }).message, "bad");
    }
  }

  private renderCardEditor(): void {
    const host = this.root!;
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        button("返回角色列表", () => {
          this.cardStep = "list";
          void this.render();
        }),
        button("保存草稿", () => void this.saveCardDraft()),
        primary("确认角色卡", () => void this.confirmCardDraft()),
      ),
    );
    const name = el("input", { class: "u-input", id: "u-card-name", value: this.cardName }) as HTMLInputElement;
    name.addEventListener("input", () => {
      this.cardName = name.value;
    });
    const brief = el("textarea", { class: "u-textarea", rows: "3", id: "u-card-brief", placeholder: "她是谁、和这个世界什么关系（起草用）" }) as HTMLTextAreaElement;
    brief.value = this.cardBrief;
    brief.addEventListener("input", () => {
      this.cardBrief = brief.value;
    });
    host.appendChild(field("角色名", name));
    host.appendChild(field("一句话描述（起草用）", brief));
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        primary(this.card ? "重新起草（整卡）" : "让 AI 起草", () => void this.draftCard()),
        paragraph("起草会带着当前世界设定：角色卡里的来源、史料与联络方式都从这份设定里选。", "u-hint"),
      ),
    );
    if (!this.card) {
      host.appendChild(paragraph("还没有草稿：先写一句描述再起草，或者直接点「让 AI 起草」用世界设定补一份。"));
      return;
    }
    host.appendChild(this.cardForm());
    host.appendChild(this.cardLockPanel());
    const check = el("div", { class: "u-card" });
    check.appendChild(el("h3", { text: "检查" }));
    if (this.cardErrors.length) {
      check.appendChild(paragraph(`还有 ${this.cardErrors.length} 项要处理：`));
      check.appendChild(bulletList(this.cardErrors.slice(0, 12), "u-list"));
      check.appendChild(primary("重新检查", () => void this.validateCard()));
    } else {
      check.appendChild(paragraph("这张卡目前没有校验问题。", "u-hint"));
      check.appendChild(button("重新检查", () => void this.validateCard()));
    }
    host.appendChild(check);
  }

  /** 卡片表单：顶层标量 / 分组对象 / 列表（一层深）都渲染成控件 */
  private cardForm(): HTMLElement {
    const box = el("div", { class: "u-card u-card-form" });
    const card = this.card!;
    const refs = refIndex(this.candidate ?? {});
    const entries = Object.entries(card);
    box.appendChild(el("h3", { text: "角色内容" }));
    for (const [key, value] of entries) {
      if (key === "meta") continue;
      if (Array.isArray(value)) {
        box.appendChild(el("h4", { text: GROUP_LABELS[key] ?? label(key) }));
        box.appendChild(controlFor(key, value, refs, (next) => {
          card[key] = next;
        }));
        continue;
      }
      if (value && typeof value === "object") {
        const group = el("div", { class: "u-field-group" });
        group.appendChild(el("h4", { text: GROUP_LABELS[key] ?? label(key) }));
        for (const [sub, subValue] of Object.entries(value as Json)) {
          group.appendChild(
            field(
              label(sub),
              controlFor(`${key}.${sub}`, subValue, refs, (next) => {
                (card[key] as Json)[sub] = next;
              }),
            ),
          );
        }
        box.appendChild(group);
        continue;
      }
      box.appendChild(
        field(
          label(key),
          controlFor(key, value, refs, (next) => {
            card[key] = next;
          }),
        ),
      );
    }
    return box;
  }

  /** 字段级锁定：重跑时这些字段原样保留（核心的 locked_fields） */
  private cardLockPanel(): HTMLElement {
    const box = el("details", { class: "u-knobs" });
    box.appendChild(el("summary", { text: `锁定字段（重跑时不覆盖，已锁 ${this.cardLocks.length} 个）` }));
    const grid = el("div", { class: "u-knob-grid" });
    const paths: string[] = [];
    for (const [key, value] of Object.entries(this.card ?? {})) {
      if (key === "meta") continue;
      if (value && typeof value === "object" && !Array.isArray(value)) {
        for (const sub of Object.keys(value as Json)) paths.push(`${key}.${sub}`);
      } else {
        paths.push(key);
      }
    }
    for (const path of paths) {
      const line = el("label", { class: "u-check" });
      const tick = el("input", { type: "checkbox" }) as HTMLInputElement;
      tick.checked = this.cardLocks.includes(path);
      tick.addEventListener("change", () => {
        this.cardLocks = tick.checked
          ? [...new Set([...this.cardLocks, path])]
          : this.cardLocks.filter((item) => item !== path);
      });
      line.appendChild(tick);
      line.appendChild(el("span", { text: label(path) }));
      grid.appendChild(line);
    }
    box.appendChild(grid);
    box.appendChild(paragraph("锁定的字段在「重新起草」与重跑时保持原样；要改先在这里解锁。", "u-hint"));
    return box;
  }

  private async draftCard(): Promise<void> {
    if (!this.base) {
      setNote(this.note, "先确认世界设定：角色卡要按那份设定来写", "bad");
      return;
    }
    if (!this.cardBrief.trim() && !this.card) {
      setNote(this.note, "先写一句她是谁", "bad");
      return;
    }
    setNote(this.note, "正在起草角色卡…", "pending");
    try {
      const payload: Json = {
        package_path: String(this.base).split(/[\\/]/).pop() ?? this.base,
        brief: this.cardBrief.trim(),
        locked_fields: this.cardLocks,
      };
      if (this.card) payload.base = this.card;
      const result = await this.ctx.api.cardGenerate(payload);
      if (result.candidate) this.card = result.candidate as Json;
      this.cardErrors = ((result.errors as string[]) ?? []).slice();
      if (!this.cardName) this.cardName = String((this.card?.identity as Json)?.name ?? "");
      setNote(
        this.note,
        this.cardErrors.length ? `起草完成，还有 ${this.cardErrors.length} 项要处理` : "起草完成：逐项看看，改完再确认",
        this.cardErrors.length ? "pending" : "ok",
      );
      void this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "角色卡", action: "起草", done: "没有改动已保存的材料" }).message, "bad");
    }
  }

  private async validateCard(): Promise<void> {
    if (!this.card || !this.base) return;
    try {
      const result = await this.ctx.api.cardValidate({
        card: this.card,
        package_path: String(this.base).split(/[\\/]/).pop() ?? this.base,
      });
      this.cardErrors = ((result.errors as string[]) ?? []).slice();
      setNote(this.note, this.cardErrors.length ? `还有 ${this.cardErrors.length} 项要处理` : "检查通过", this.cardErrors.length ? "pending" : "ok");
      void this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "角色卡", action: "检查" }).message, "bad");
    }
  }

  private async saveCardDraft(): Promise<void> {
    if (!this.card) return;
    try {
      await this.ctx.api.draftSave(`card:${this.cardName || "未命名"}`, "create", this.cardName, this.cardName, {
        card: this.card,
        errors: this.cardErrors,
        brief: this.cardBrief,
        locks: this.cardLocks,
      });
      setNote(this.note, "草稿已保存：草稿不是正式角色卡，确认后才生效", "ok");
    } catch (error) {
      setNote(this.note, uiError(error, { module: "角色卡", action: "保存草稿" }).message, "bad");
    }
  }

  /** 确认角色卡：先落盘到创作目录，再按世界设定校验并标记「可用于创建」 */
  private async confirmCardDraft(): Promise<void> {
    if (!this.card || !this.base) return;
    const name = this.cardName.trim();
    if (!name) {
      setNote(this.note, "先给这张卡起个名字", "bad");
      return;
    }
    setNote(this.note, "正在检查并确认这张角色卡…", "pending");
    try {
      const file = await this.uniqueCardFile(name);
      await this.ctx.api.cardSave(file, this.card);
      await this.ctx.api.cardConfirm({
        card_path: file,
        package_path: String(this.base).split(/[\\/]/).pop() ?? this.base,
      });
      this.cardStep = "list";
      this.cardFiles = [...new Set([...this.cardFiles, file])];
      setNote(this.note, `角色卡「${name}」已确认：可以用于创建`, "ok");
      await this.render();
    } catch (error) {
      setNote(
        this.note,
        uiError(error, { module: "角色卡", action: "确认", done: "草稿还在，改完可以再试" }).message,
        "bad",
      );
    }
  }

  private async uniqueCardFile(name: string): Promise<string> {
    const base = name.replace(/[^\w\u4e00-\u9fa5-]/g, "_").slice(0, 40) || "card";
    const list = await this.ctx.api.cards();
    const taken = new Set(((list.cards as Json[]) ?? []).map((item) => String(item.file ?? "")));
    let candidate = `${base}.card.json`;
    let index = 2;
    while (taken.has(candidate)) candidate = `${base}-${index++}.card.json`;
    return candidate;
  }

  private async confirmCard(file: string): Promise<void> {
    setNote(this.note, "正在检查这张角色卡…", "pending");
    try {
      const pkgPath = this.base ? (String(this.base).split(/[\\/]/).pop() ?? "") : "";
      await this.ctx.api.cardConfirm(pkgPath ? { card_path: file, package_path: pkgPath } : { card_path: file });
      setNote(this.note, "角色卡已确认：可以用于创建", "ok");
      void this.render();
    } catch (error) {
      setNote(
        this.note,
        uiError(error, { module: "角色卡", action: "确认", done: "卡没有改动" }).message,
        "bad",
      );
    }
  }

  /* ------------------------------------------------------------ 步骤四：检查与确认 */

  private renderReview(): void {
    const host = this.root!;
    const pkg = this.candidate!;
    const sections = sectionsOf(pkg);
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        button("返回角色", () => this.goto("cards")),
        primary("创建世界", () => this.goto("create")),
      ),
    );
    host.appendChild(
      section(
        "创建摘要",
        facts([
          ["世界名", this.name || "（未命名）"],
          ["设定来源", this.source === "ai" ? "AI 起草" : this.source === "manual" ? "自己填写" : "样例改造"],
          ["设定内容", `${sections.length} 个分区、${sections.reduce((sum, item) => sum + item.items.length, 0)} 条`],
          ["角色", this.cardFiles.join("、") || "（还没选）"],
          ["校验", this.errors.length ? `${this.errors.length} 项待处理` : "通过"],
        ]),
      ),
    );
    const nameInput = el("input", { class: "u-input", value: this.worldName || this.name, id: "u-create-worldname" }) as HTMLInputElement;
    nameInput.addEventListener("input", () => {
      this.worldName = nameInput.value;
    });
    host.appendChild(field("这个世界的名字（以后可以重命名）", nameInput));
    host.appendChild(paragraph("创建会固化这份设定：以后改设定只影响新创建的世界，不会追溯改写这个已存在的世界。"));
  }

  /* ------------------------------------------------------------ 步骤五 / 六：创建与开始 */

  private renderCreate(): void {
    const host = this.root!;
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        primary("创建并开始使用", () => void this.create(true)),
        button("只创建，暂不运行", () => void this.create(false)),
        button("返回摘要", () => this.goto("review")),
      ),
    );
    host.appendChild(
      paragraph("创建分两步：先建世界（固化设定），再按你的选择启动或先暂停；任一步失败都会保留已经完成的部分。"),
    );
  }

  private async create(run: boolean): Promise<void> {
    if (!this.base) {
      setNote(this.note, "世界设定还没确认（先回上一步点「确认世界设定」）", "bad");
      return;
    }
    if (!this.cardFiles.length) {
      setNote(this.note, "还没有选角色", "bad");
      return;
    }
    const requestId = `ui-create-${this.base}-${this.cardFiles.join("+")}`;
    setNote(this.note, "正在创建世界…", "pending");
    try {
      const result = await this.ctx.api.createInstance({
        package_path: String(this.base).split(/[\\/]/).pop() ?? this.base,
        card_paths: this.cardFiles,
        display_name: this.worldName || this.name,
        request_id: requestId,
      });
      this.created = (result.instance as Json) ?? {};
      const instanceId = String(this.created.id ?? "");
      if (run && instanceId) {
        const info = await this.ctx.api.instanceInfo(instanceId);
        const timeline = ((info.timelines as Json[]) ?? [])[0];
        if (timeline) await this.ctx.api.activate(instanceId, String(timeline.id), 1);
      }
      setNote(
        this.note,
        run ? "世界已创建并开始运行" : "世界已创建（时间线处于暂停；想开始时到世界与素材里点「启动」）",
        "ok",
      );
      this.goto("start");
    } catch (error) {
      setNote(
        this.note,
        uiError(error, {
          module: "创建世界",
          action: run ? "创建并开始使用" : "只创建",
          done: "设定与角色卡都还在，可以重试",
          unknown: "世界是否已经建立",
        }).message,
        "bad",
      );
    }
  }

  private renderStart(): void {
    const host = this.root!;
    const instanceId = String(this.created?.id ?? "");
    const name = String(this.created?.name ?? this.worldName ?? this.name);
    host.appendChild(
      section(
        `世界「${name}」已经建好`,
        facts([
          ["里面有什么", `${this.cardFiles.length} 位角色`],
          ["状态", "可以在世界与素材里看时间线与版本"],
        ]),
        el(
          "div",
          { class: "u-row" },
          primary("去和角色联络", () => this.ctx.navigate({ pane: "contact" })),
          button("打开这个世界", () => this.ctx.navigate({ pane: "worlds" })),
          button("再创建一个世界", () => {
            this.step = "source";
            this.candidate = null;
            this.cardFiles = [];
            this.created = null;
            this.usage = null;
            this.errors = [];
            this.locks = {};
            void this.render();
          }),
        ),
        paragraph("同一个设定可以创建多个世界；已有的世界不会因为改设定而变。", "u-hint"),
      ),
    );
    if (instanceId) host.appendChild(paragraph(`实例标识：${instanceId}`, "u-hint"));
  }
}
