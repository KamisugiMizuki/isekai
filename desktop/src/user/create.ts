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
  pageHead,
  panel,
  paragraph,
  primary,
  section,
  setNote,
} from "./dom";
import { flowRail, stackBar } from "./graphics";

type Step = "source" | "world" | "cards" | "review" | "create" | "start";

/**
 * 向导的六步：进度轨与标题带共用一份名字（只写一遍，免得两处说成两件事）。
 * 后五步同时也是页面里的一级分区（panel）标题。
 */
const STEP_LABELS: Array<[Step, string]> = [
  ["source", "选择来源"],
  ["world", "世界设定"],
  ["cards", "准备角色"],
  ["review", "检查与确认"],
  ["create", "创建世界"],
  ["start", "开始方式"],
];

/** 分区名的用户说法（内核用点分路径，界面不暴露路径本身） */
const SECTION_LABELS: Record<string, string> = {
  // `world` 这两个标量没有自己的条目列表，但校验问题会点名它们（评审 P0-4）
  world: "世界本身",
  "world.axioms": "世界的基本设定",
  "world.institutions": "制度与职位",
  "world.customs": "惯例",
  "world.lexicon.terms": "命名语汇",
  "environment.types": "环境状态",
  sources: "信息来源",
  canon: "世界里真实发生的",
  narratives: "她听说的版本",
  entities: "人物名册",
  races: "种族",
  historiography: "来源与记载",
  "events.families": "事件族",
  "events.calendar": "节庆",
  life: "生活安排",
  roles: "可扮演的角色类型",
  "comms.mechanisms": "联络方式",
  "initial_state.mysteries": "谜题",
  "initial_state.rumors": "流言",
  "initial_state.events": "开场事件",
  "calendar.months": "月份",
  "calendar.segments": "时段",
};

/**
 * 固定入口的分区（评审 P0-4）：核心把地理、社会结构、命名语汇、人物名册与史料都判为必填/阻断项，
 * 而空数组分区进不了分区目录（原来的目录只收录「非空数组且每项有 id」的条目），
 * 于是「自己填写」这条路永远过不了校验。这里把空格子的落点和第一条的骨架写死，
 * 「添加一条」不再要求分区里已经有一条。
 *
 * 骨架里的关联字段写成 `@ref:<顶层数组>`：添加时替换成包里真实存在的那个 id
 * （写死 src-1 这类会在这份设定换过标识时变成悬空引用）。
 */
const DECLARED_SECTIONS: Array<{ path: string; item: Json }> = [
  { path: "world.lexicon.terms", item: { term: "", meaning: "" } },
  { path: "environment.types", item: { id: "env-1", name: "天气", initial: "晴", unit: "天", values: ["晴", "雨"], observe: "抬头可见", expiry: "自然转晴", scope: "整个地区", sources: ["@ref:sources"] } },
  { path: "entities", item: { id: "ent-1", name: "", kind: "person", race_id: "@ref:races" } },
  { path: "historiography", item: { id: "hist-1", title: "", contributors: [{ name: "", role: "记录", period: "初期" }], coverage: { from: 0, to: 0 }, entries: ["@ref:canon"] } },
  { path: "initial_state.mysteries", item: { id: "my-1", question: "", refs: ["@ref:canon"] } },
  { path: "initial_state.rumors", item: { id: "ru-1", text: "", source_id: "@ref:sources", canon_ref: "@ref:canon", obtain: [], confidence: "believed" } },
  { path: "initial_state.events", item: { id: "ev-1", summary: "", family: "@ref:events.families" } },
];

/** `@ref:<点分路径>` → 包里第一个真实存在的 id（没有就留空，不做悬空引用） */
function firstIdAt(pkg: Json, path: string): string {
  let node: unknown = pkg;
  for (const key of path.split(".")) {
    if (!node || typeof node !== "object") return "";
    node = (node as Json)[key];
  }
  if (!Array.isArray(node)) return "";
  const hit = node.find((item) => item && typeof item === "object" && item.id);
  return String((hit as Json | undefined)?.id ?? "");
}

/** 把骨架里的 @ref 占位换成真实的 id；空串的关联字段整个去掉（核心只认已登记的标识） */
function resolveSeedRefs(value: unknown, pkg: Json): unknown {
  if (typeof value === "string" && value.startsWith("@ref:")) return firstIdAt(pkg, value.slice(5));
  if (Array.isArray(value)) return value.map((item) => resolveSeedRefs(item, pkg));
  if (value && typeof value === "object") {
    const out: Json = {};
    for (const [key, item] of Object.entries(value as Json)) {
      const resolved = resolveSeedRefs(item, pkg);
      if (resolved === "") continue;
      out[key] = resolved;
    }
    return out;
  }
  return value;
}

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
  activity: "活动",
  unit: "单位",
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
  window: "时间窗",
  preconditions: "前提",
  effect: "效果",
  mode: "性格设定",
  sources: "依据来源",
  routine_note: "作息说明",
  appearance: "外貌",
  // 自己填写时要认得出的几个字段（评审 P0-4 的必填项）
  geography: "世界地理与空间边界",
  society: "社会结构",
  term: "词条",
};

/** 分区固定入口里那几个标量：核心判为必填，界面上以前根本没有输入控件 */
const WORLD_SCALARS: Array<{ key: string; label: string; hint: string }> = [
  { key: "geography", label: "世界地理与空间边界", hint: "这块大陆/城邦在哪、边界是什么、别人怎么到达（必填）" },
  { key: "society", label: "社会结构", hint: "谁在上谁在下、靠什么维系、普通人一天怎么过（必填）" },
];

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

/**
 * 允许值的中文短标签（评审第六节：英文枚举不上屏）。
 * 内部值仍然只放 `value`：核心认的是 person / believed / sparse / true 这些原值。
 */
const CHOICE_LABELS: Record<string, Record<string, string>> = {
  kind: { person: "人物", place: "地点", thing: "物件", organization: "组织" },
  confidence: { true: "确实如此", believed: "她相信是真的", disputed: "有争议", false: "不实" },
  density: { sparse: "稀疏", normal: "常规", dense: "密集" },
  sleep: { true: "会睡觉", false: "不睡觉" },
};

/** 下拉里一项的中文说法（没有登记就退回原值，不装作认出来了） */
function choiceLabel(key: string, value: string): string {
  return CHOICE_LABELS[key]?.[value] ?? value;
}

/** 参数层旋钮（与核心生成器的键同名；这里是生成要求，不是硬约束） */
const KNOB_TEXT: Array<[string, string]> = [
  ["genre", "体裁"],
  ["tone", "基调"],
  ["supernatural", "超自然在场度"],
  ["tech", "技术水平"],
  ["naming", "命名风格"],
  ["conflict", "冲突主线"],
  ["era_start", "纪元起点"],
  ["current_year", "当前年"],
  ["history_depth", "记载深度"],
];
const KNOB_COUNT: Array<[string, string]> = [
  ["axioms", "世界的基本设定"],
  ["regions", "区域"],
  ["institutions", "制度（含职位）"],
  ["customs", "惯例"],
  ["env_types", "环境类型"],
  ["races", "种族"],
  ["roles", "可扮演的角色类型"],
  ["lexicon", "用词表"],
  ["sources", "信息来源"],
  ["canon", "真实发生的"],
  ["narratives", "她听说的"],
  ["entities", "人物名册"],
  ["life", "生活安排"],
  ["families", "事件族"],
  ["festivals", "节庆"],
];
/** 段 → 顶层键（与核心 PACKAGE_SEGMENTS 同源：填段重跑要的是键列表）；段名只用于界面说法 */
const SEGMENT_KEYS: Array<[string, string[]]> = [
  ["设定核心", ["meta", "calendar", "world"]],
  ["来源与名册", ["sources", "canon", "narratives", "races", "entities"]],
  ["机制与现状", ["historiography", "environment", "events", "life", "roles", "comms", "initial_state"]],
];

const KNOB_LIST: Array<[string, string]> = [
  ["include", "必须出现"],
  ["exclude", "禁止出现"],
  ["homage", "可参考致敬"],
];

/** 创建向导的固定草稿键：同一时间只留一份「填到一半」的世界设定 */
const CREATE_DRAFT_KEY = "create:world";

interface Section {
  path: string;
  label: string;
  items: Json[];
}

/** 校验问题翻人话（世界与素材列表也要用同一套说法，免得一处说人话一处印点分路径） */
export { describeProblem };
export type { Section };

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
  initial_units: "性格设定",
  intents: "当前意图",
};

/** 包内全部稳定标识：新增条目的 id 不能和已存在的撞（核心按唯一性判定） */
function _allIds(pkg: Json): string[] {
  const out: string[] = [];
  const walk = (node: unknown): void => {
    if (!node || typeof node !== "object") return;
    if (Array.isArray(node)) {
      for (const item of node) walk(item);
      return;
    }
    for (const [key, value] of Object.entries(node as Json)) {
      if (key === "id" && typeof value === "string" && value) out.push(value);
      else walk(value);
    }
  };
  walk(pkg);
  return out;
}

/** 分区排序：按段排（设定核心 → 双轨与名册 → 机制与现状），段内按路径 */
function sectionOrder(path: string): number {
  const top = path.split(".")[0];
  const index = SEGMENT_KEYS.findIndex(([, keys]) => keys.includes(top));
  return index < 0 ? SEGMENT_KEYS.length : index;
}

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
  return out.sort((a, b) => sectionOrder(a.path) - sectionOrder(b.path) || a.path.localeCompare(b.path));
}

/** 固定入口分区的第一条骨架（没有登记就现造一条，至少给一个 id） */
function declaredItem(path: string): Json {
  const known = DECLARED_SECTIONS.find((item) => item.path === path);
  if (known) return JSON.parse(JSON.stringify(known.item)) as Json;
  return { id: `${path.split(".").pop() ?? "item"}-1` };
}

/** 分区目录要显示的清单：已收集到的分区 + 固定入口里还空着的分区（评审 P0-4：空格子也要有落点） */
function catalogOf(pkg: Json): Section[] {
  const collected = sectionsOf(pkg);
  const known = new Set(collected.map((item) => item.path));
  const extra: Section[] = DECLARED_SECTIONS.filter((item) => !known.has(item.path)).map((item) => ({
    path: item.path,
    label: SECTION_LABELS[item.path] ?? item.path.split(".").pop() ?? item.path,
    items: [],
  }));
  return [...collected, ...extra].sort((a, b) => sectionOrder(a.path) - sectionOrder(b.path) || a.path.localeCompare(b.path));
}

/** 按点分路径拿到数组本身，缺中间层就补出来（「添加一条」要能落在空分区上） */
function sectionRef(pkg: Json, path: string): Json[] | null {
  const keys = path.split(".");
  let node: Json = pkg;
  for (const key of keys.slice(0, -1)) {
    const next = node[key];
    if (!next || typeof next !== "object" || Array.isArray(next)) {
      const fresh: Json = {};
      node[key] = fresh;
      node = fresh;
      continue;
    }
    node = next as Json;
  }
  const last = keys[keys.length - 1];
  const list = node[last];
  if (Array.isArray(list)) return list as Json[];
  const fresh: Json[] = [];
  node[last] = fresh;
  return fresh;
}

/**
 * 校验问题原文里点名了哪个分区：只认「这一份里真实存在的分区」，
 * 否则 `canon_ref` 这种字段名会被误判成 `canon` 分区（取最长路径，`world.lexicon.terms` 优先于 `world`）。
 */
function sectionPathIn(problem: string, sections: Section[]): string {
  const paths = [...sections.map((item) => item.path), ...DECLARED_SECTIONS.map((item) => item.path)];
  return [...new Set(paths)]
    .filter(
      (path) =>
        problem.startsWith(`${path}:`) ||
        problem.startsWith(`${path}[`) ||
        problem.startsWith(`${path}.`) ||
        problem.includes(` ${path}`),
    )
    .sort((a, b) => b.length - a.length)[0] ?? "";
}

/** 问题原文 → 人话标题 + 落点（分区 › 条目 › 字段，三段都能对上的才写出来） */
function describeProblem(problem: string, sections: Section[]): { text: string; section: string; index: number; field: string } {
  const section = sectionPathIn(problem, sections);
  if (!section) return { text: problem, section: "", index: -1, field: "" };
  const rest = problem.slice(section.length);
  const sectionLabel = SECTION_LABELS[section] ?? section;
  const indexMatch = /^\[(\d+)\]/.exec(rest);
  const index = indexMatch ? Number(indexMatch[1]) : -1;
  const tail = indexMatch ? rest.slice(indexMatch[0].length) : rest;
  const fieldMatch = /^\.([\w.]+)/.exec(tail);
  const field = fieldMatch ? fieldMatch[1] : "";
  const reason = problem
    .slice(section.length + (indexMatch?.[0].length ?? 0) + (fieldMatch?.[0].length ?? 0) + 1)
    .replace(/^[:：]\s*/, "");
  const where = [
    sectionLabel,
    index >= 0 ? `第 ${index + 1} 条` : "",
    field ? `「${field.split(".").map((part) => label(part)).join(" › ")}」` : "",
  ]
    .filter(Boolean)
    .join(" › ");
  return { text: `${where}：${reason || "这一项没有填完整"}`, section, index, field };
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
    for (const option of CHOICES[key]) box.appendChild(el("option", { value: option, text: choiceLabel(key, option) }));
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
  /** 进度轨的宿主：它在固定骨架里，换一步只重画它（见 refreshRail） */
  private railHost: HTMLElement | null = null;
  private note: HTMLElement | null = null;
  /** 草稿状态行：DraftKeeper 往这里写「未保存 / 正在保存 / 已保存 / 保存失败」，不跟主 note 抢 */
  private draftSlot: HTMLElement | null = null;
  private candidate: Json | null = null;
  private locks: Record<string, string[]> = {};
  private errors: string[] = [];
  /** 这一份到底检查过没有：空缓存 ≠ 没有问题（评审：载入骨架后不能报「没有校验问题」） */
  private errorsChecked = false;
  /** 校验问题 → 分区路径的缓存（目录每一行都要问一遍同一批问题） */
  private problemSections: Map<string, string> | null = null;
  private usage: Json | null = null;
  private base: string = ""; // 素材标识（应用管理；用户只填显示名）
  /** 正在编辑的那份设定文件（空 = 新建）：确认时覆盖它，不再堆同名副本 */
  private savedPath = "";
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
  /** 刚建好的世界的第一条线：出口（去和角色联络 / 打开这个世界）都指向它 */
  private createdTimelineId = "";
  private busy = false;

  constructor(private readonly ctx: AppContext) {}

  mount(host: HTMLElement): void {
    this.root = el("div", { class: "u-create" });
    this.note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    this.draftSlot = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    // 骨架固定三带：标题带（页面名 + 一句定位语）→ 进度轨 → 内容。
    // 以前步骤条是一排和「动作」同形的按钮，既不说明走到哪一步，也学不到「这个形状 = 这个行为」
    // （2026-10-08 视觉体系审查根因 2/3）
    const railHost = el("div");
    // 用 .u-page 包起来：标题 / 进度轨 / 内容之间的间距与其它页同一套（以前这三段各靠默认外边距）
    fill(
      host,
      el("div", { class: "u-page" }, pageHead("创建世界", "从样例改，或从空白开始", []), railHost, this.note, this.draftSlot, this.root),
    );
    this.railHost = railHost;
    this.refreshRail();
    const sub = this.ctx.route.sub ?? "";
    if (sub.startsWith("edit:")) {
      void this.openExisting(sub.slice(5));
    } else if (sub.startsWith("draft:")) {
      void this.openDraft(sub.slice(6));
    } else {
      void this.render();
    }
  }

  /**
   * 真进度轨：带编号圆点 + 连线（走过的打勾、当前反白、没到的置灰），
   * 一眼看出走了几分之几 —— 文字加下划线说明不了这件事（同一次审查）。
   *
   * 走过的步骤点得回去（守卫仍是 `allowed`：没走到的步骤不放行，跳过校验会让人以为前面已经过了）。
   * flowRail 画的是 div（不可聚焦），这里补 role / tabindex / 键盘处理，鼠标、键盘与读屏都能用。
   */
  private stepper(): HTMLElement {
    const index = STEP_LABELS.findIndex(([id]) => id === this.step);
    const rail = flowRail(STEP_LABELS.map(([, text]) => ({ label: text })), Math.max(0, index));
    if (!rail) {
      // 画不出来（步骤表为空这类不可能的情况）也要说清在第几步：退回一行文字，不返回 null
      return el("p", { class: "u-hint u-wizard-rail", text: STEP_LABELS.map(([, text]) => text).join("　") });
    }
    rail.classList.add("u-wizard-rail"); // 探针 / 样式认这一类：向导的进度轨
    Array.from(rail.querySelectorAll<HTMLElement>(".u-rail-step")).forEach((node, position) => {
      const entry = STEP_LABELS[position];
      if (!entry) return;
      const [id, text] = entry;
      // 当前这一步不动：flowRail 已经给它标了 aria-current="step"
      if (id === this.step) return;
      node.setAttribute("role", "button");
      node.setAttribute("aria-label", `回到第 ${position + 1} 步：${text}`);
      if (!this.allowed(id)) {
        node.setAttribute("aria-disabled", "true");
        return;
      }
      node.tabIndex = 0;
      const jump = (): void => this.goto(id);
      node.addEventListener("click", jump);
      node.addEventListener("keydown", (event) => {
        if (event.key !== "Enter" && event.key !== " ") return;
        event.preventDefault();
        jump();
      });
    });
    return rail;
  }

  /** 重画进度轨（挂在固定骨架里，不随 this.root 一起被清掉） */
  private refreshRail(): void {
    if (this.railHost) fill(this.railHost, this.stepper());
  }

  /** 切页 / 退出：把在途草稿落盘（壳层导航前也会 flush 一次，这里补一道保险） */
  unmount(): void {
    void this.ctx.drafts.flush();
  }

  /**
   * 从「世界与素材」进来的：编辑一份已有设定。
   * 打开前会看一眼那份「填到一半」的草稿（向导只有一份草稿槽）：有就先问保留还是替换（评审 P1）。
   */
  private async openExisting(file: string): Promise<void> {
    if (!file) {
      void this.render();
      return;
    }
    setNote(this.note, "正在打开这份世界设定…", "pending");
    try {
      const loaded = await this.ctx.api.packageLoad(file);
      const start = (): void => {
        this.absorbPackage((loaded.package as Json) ?? {});
        this.base = file;
        this.savedPath = file;
        this.source = this.source || "manual";
        this.queueDraft();
        setNote(this.note, `正在编辑「${this.name}」：确认后会覆盖这份设定文件；已有的世界不会变`, "ok");
        this.step = "world";
        void this.render();
      };
      const pending = await this.ctx.drafts.load(CREATE_DRAFT_KEY);
      const hasDraft = Boolean(pending && (pending.text.trim() || Object.keys(pending.payload ?? {}).length));
      if (!hasDraft) {
        start();
        return;
      }
      const note = el("p", { class: "u-note" });
      const modal = dialog(
        "还有一份填到一半的世界设定",
        [
          paragraph(`草稿「${pending?.text || "未命名世界"}」还没确认。编辑这份已有设定会把它覆盖掉。`),
          paragraph("选「保留草稿」会先停下，让你回「世界与素材 → 未完成内容」把它处理掉。", "u-hint"),
          note,
        ],
        [
          {
            label: "替换草稿，继续编辑",
            primary: true,
            run: () => start(),
          },
          { label: "保留草稿（先不打开）", run: () => void this.render() },
        ],
      );
      document.body.appendChild(modal.node);
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
        // 草稿里存着上一次的检查结果：有记录才算「检查过」，空数组不代表没问题
        this.errorsChecked = this.errors.length > 0;
        this.problemSections = null;
        this.brief = String(payload.brief ?? "");
        this.knobs = (payload.knobs as Record<string, unknown>) ?? {};
        this.source = String(payload.source ?? "manual");
        this.savedPath = String(payload.savedPath ?? "");
        if (this.savedPath) this.base = this.savedPath;
      }
      this.queueDraft(); // absorbPackage 之后才补上 locks / errors / brief / knobs / source，这里重排一次
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

  /** 向导手里这份设定的快照：与 openDraft() 读回时消费的 payload 同形状 */
  private draftPayload(): Json {
    const pkg = this.candidate ?? {};
    if (this.candidate) {
      // 向导里的名字/简介要到「确认」那一步才写进 meta：现在同步进去，草稿读回时才认得出
      // （清空名字也会同步成空串，读回后不会被旧名字复活）
      const meta = (pkg.meta as Json) ?? {};
      meta.original_name = this.name;
      meta.display_name = this.name;
      meta.description = this.brief;
      pkg.meta = meta;
    }
    return {
      package: pkg,
      locks: this.locks,
      errors: this.errors,
      brief: this.brief,
      knobs: this.knobs,
      source: this.source,
      // 正在编辑的那份设定文件：草稿读回后仍要覆盖它，而不是又另存一份
      savedPath: this.savedPath,
    };
  }

  /**
   * 把当前进度交给 DraftKeeper（它负责防抖与「未保存 / 正在保存 / 已保存 / 保存失败」状态）。
   * 只在「有内容可恢复」时写：名字 / 简介 / 参数 / 设定对象全空就不产生空草稿
   * （首页与素材页的列表按 text 非空过滤，空草稿读回来也没东西可接着改）。
   *
   * 草稿槽上的名词由这里的 target 决定：草稿的自动保存不等于「设定已确认」，
   * 所以写清是「世界设定草稿」，别让用户以为正式设定文件已经生成（评审 P1）。
   */
  private queueDraft(): void {
    if (!this.candidate && !this.name.trim() && !this.brief.trim() && !Object.keys(this.knobs).length) return;
    const text = this.name.trim() || "未命名世界";
    this.ctx.drafts.watch(CREATE_DRAFT_KEY, this.draftSlot, "create", `世界设定草稿（${text}）`, text, this.draftPayload());
  }

  private async render(): Promise<void> {
    if (!this.root) return;
    // 进度轨要跟着步骤走（当前格反白、走过的打勾）：它挂在固定骨架里、不随 this.root 重画，
    // 所以每次渲染都重画一遍
    this.refreshRail();
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
      this.root.appendChild(
        errorCard(
          uiError(error, {
            module: "创建世界",
            action: "打开发这一步",
            done: "没有改动任何数据",
            unknown: "这一步是否已经读到需要的数据",
          }),
          [{ label: "重试打开这一步", run: () => void this.render() }],
        ),
      );
    }
  }

  private goto(step: Step): void {
    this.step = step;
    void this.render();
  }

  /* ------------------------------------------------------------ 步骤一：来源 */

  private renderSource(): void {
    // 每一步是一个一级分区（panel），里面才放二级卡片：以前所有 section 平铺，
    // 一屏里全是同重量的盒子，眼睛找不到落点（2026-10-08 视觉体系审查根因 1）
    const host = panel("来源");
    this.root!.appendChild(host);
    const make = (title: string, body: string, run: () => void): HTMLElement => {
      const card = el("article", { class: "u-card" });
      card.appendChild(el("h3", { text: title }));
      card.appendChild(paragraph(body));
      card.appendChild(primary("用这个", run));
      return card;
    };
    host.appendChild(paragraph("世界设定是底稿：先建一份设定，再由它创建出会保存进展的世界；改设定只影响以后创建的世界。"));
    host.appendChild(
      el(
        "div",
        { class: "u-cards" },
        make("让 AI 起草", "写一句想要的世界，AI 起草一份完整设定，你再逐条改。需要已配置 AI。", () => {
          this.source = "ai";
          this.candidate = null;
          this.queueDraft();
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
        ),        make("导入已有设定", "已经在别处有世界设定或角色卡：到「世界与素材 → 导入」带进来，再回来创建。", () =>
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
      const sample = items.find((item) => String(item.file ?? "").toLowerCase().startsWith("huichao"));
      if (!sample) {
        // 以前这里静默拿第一份设定兜底：用户以为在用灰潮纪，其实拿到的是别的东西（评审 P2）
        setNote(this.note, "样例（灰潮纪）还没有安装：先装一份，或改用「让 AI 起草 / 自己填写」", "bad");
        const note = el("p", { class: "u-note" });
        const modal = dialog(
          "样例还没安装",
          [
            paragraph("「用样例设定」要用随程序提供的灰潮纪，但这台机器上还没装它。"),
            paragraph("装的时候不会覆盖你自己的设定，装完回到这一步就能用。", "u-hint"),
            note,
          ],
          [
            {
              label: "去安装样例",
              primary: true,
              run: () => {
                setNote(note, "正在打开安装入口…", "pending");
                this.ctx.navigate({ pane: "onboarding", sub: "sample" });
              },
            },
            {
              label: "改用自己填写",
              run: () => {
                this.source = "manual";
                void this.startManual();
              },
            },
            { label: "取消", run: () => undefined },
          ],
        );
        document.body.appendChild(modal.node);
        return;
      }
      const loaded = await this.ctx.api.packageLoad(String(sample.file));
      this.absorbPackage((loaded.package as Json) ?? {});
      this.source = "sample";
      setNote(this.note, `已载入样例设定「${String(sample.name ?? sample.file)}」：改完确认成自己的一份`, "ok");
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
    this.errorsChecked = false; // 刚载入的这份还没检查过：不能报「没有校验问题」
    this.problemSections = null;
    const sections = catalogOf(pkg);
    this.section = sections[0]?.path ?? "";
    this.entryId = "";
    this.queueDraft();
  }

  private renderWorld(): void {
    const host = panel("世界设定");
    this.root!.appendChild(host);
    const pkg = this.candidate!;
    const nameInput = el("input", { class: "u-input", value: this.name, id: "u-create-name" }) as HTMLInputElement;
    nameInput.addEventListener("input", () => {
      this.name = nameInput.value;
      this.queueDraft();
    });
    const brief = el("textarea", { class: "u-textarea", rows: "3", id: "u-create-brief", placeholder: "一句话说清这个世界是什么样" }) as HTMLTextAreaElement;
    brief.value = this.brief;
    brief.addEventListener("input", () => {
      this.brief = brief.value;
      this.queueDraft();
    });

    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        button("返回来源", () => this.goto("source")),
        button("保存草稿", () => void this.saveDraft()),
        // 编辑已有设定时，想留一份新的就明确问一次（默认是覆盖原来那份）
        this.savedPath ? button("另存为新的一份…", () => void this.saveAsNew()) : null,
        primary("确认世界设定，去选角色", () => void this.confirmWorld()),
      ),
    );
    host.appendChild(
      paragraph(
        this.savedPath
          ? "草稿自动保存；点「确认世界设定」会覆盖这份设定文件，想留一份用「另存为新的一份」。"
          : "草稿自动保存；点「确认世界设定」才生成正式文件。",
        "u-hint",
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
    // 「锁定 = AI 不覆盖」这句要说全：下面条目行的勾选框与表单里的锁定状态都靠它解释
    host.appendChild(paragraph("锁定 = AI 不覆盖这一条；校验按最终内容判定，锁定不放宽要求。"));
    // 地理与社会结构没有条目列表可点，核心却判为必填：给两个固定输入口（评审 P0-4：不留走不通的路）
    host.appendChild(this.worldScalars(pkg));

    // 分区目录 + 当前分区的条目 + 条目表单
    const sections = catalogOf(pkg);
    const columns = el("div", { class: "u-create-body" });
    // 二级卡片：分区目录与条目列表各是一张浅描边卡（一级底色由上面的 panel 给）
    const catalog = section("分区目录", paragraph("每一行是一个分区，点「打开」看里面的条目，点「添加一条」加第一条。", "u-hint"));
    catalog.classList.add("u-create-catalog"); // 探针按这个类找目录行
    for (const item of sections) {
      const row = el("div", { class: "u-row-line" });
      row.appendChild(el("span", { class: "u-grow", text: item.label }));
      // 锁定进度与待处理项都是「要翻遍六个区才知道」的整体状态，直接摆在目录里
      const locked = (this.locks[item.path] ?? []).length;
      row.appendChild(
        el("span", { class: "u-hint", text: `${item.items.length} 条${locked ? ` · 已锁 ${locked}` : ""}` }),
      );
      const bad = this.errorsFor(item);
      if (bad) row.appendChild(chip(`${bad} 处待处理`, "bad"));
      const pick = button(item.path === this.section ? "在编辑" : "打开", () => {
        this.section = item.path;
        this.entryId = String(item.items[0]?.id ?? "");
        void this.render();
      });
      pick.dataset.section = item.path;
      row.appendChild(pick);
      // 空分区以前连「添加一条」都点不到（添加要求分区已存在）：这里给第一条的入口
      if (!item.items.length) {
        row.appendChild(button("添加一条", () => this.addEntry(item.path)));
      }
      const bar = stackBar(
        [
          { label: "已锁定", value: locked, tone: "accent" },
          { label: "未锁定", value: item.items.length - locked, tone: "muted" },
        ],
        { legend: false },
      );
      if (bar) {
        bar.style.flex = "1 1 100%";
        bar.title = `${item.label}：共 ${item.items.length} 条，已锁 ${locked} 条`;
        row.appendChild(bar);
      }
      catalog.appendChild(row);
    }
    if (!sections.length) {
      catalog.appendChild(paragraph("这份设定还没有条目：让 AI 起草，或用「添加一条」自己加。", "u-hint"));
    }
    columns.appendChild(catalog);

    const current = sections.find((item) => item.path === this.section) ?? sections[0];
    const entries = section(current ? `${current.label}（${current.items.length} 条）` : "当前分区");
    entries.classList.add("u-create-entries"); // 探针按这个类找条目行
    if (current) {
      // 没有 id 的条目（命名语汇这类）不进「条目」列表：它们按字段直接编辑
      const rows = current.items.filter((item) => item && typeof item === "object" && item.id);
      if (rows.length) {
        entries.appendChild(
          paragraph("最左边的勾选框是「锁定」：勾上以后，AI 重新生成不会覆盖这一条。", "u-hint"),
        );
      }
      for (const item of rows) {
        const row = el("div", { class: "u-row-line" });
        const tick = el("input", { type: "checkbox" }) as HTMLInputElement;
        tick.checked = (this.locks[current.path] ?? []).includes(String(item.id));
        tick.dataset.lock = String(item.id);
        // 没有文字标签的复选框用户不知道是什么（评审 P2）：给读屏与悬停都说清是「锁定」
        tick.setAttribute("aria-label", "锁定这一条（AI 不覆盖）");
        tick.title = "锁定这一条（AI 不覆盖）";
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

  /** 世界级的两个标量：核心判为必填，但界面上以前没有输入控件（评审 P0-4） */
  private worldScalars(pkg: Json): HTMLElement {
    const world = (pkg.world as Json) ?? {};
    pkg.world = world;
    const card = section("世界本身（这两项必填）");
    const refs = refIndex(pkg);
    for (const item of WORLD_SCALARS) {
      const current = world[item.key];
      const node = controlFor(item.key, current ?? "", refs, (next) => {
        world[item.key] = next;
        this.queueDraft();
      });
      node.dataset.scalar = item.key;
      card.appendChild(field(item.label, node, item.hint));
    }
    const terms = sectionsOf(pkg).find((entry) => entry.path === "world.lexicon.terms");
    const filled = (terms?.items.length ?? 0) > 0;
    card.appendChild(
      paragraph(
        filled
          ? "命名语汇在下面的「命名语汇」分区里逐条填。"
          : "「命名语汇」也是必填：到分区目录里点「命名语汇 → 添加一条」，至少填一个词条。",
        "u-hint",
      ),
    );
    return card;
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
    } else if (kind === "count") {
      if (raw === "") {
        delete this.knobs[key];
      } else {
        const value = Number(raw);
        if (Number.isInteger(value) && value >= 0) this.knobs[key] = value;
      }
    } else {
      const lines = raw.split("\n").map((item) => item.trim()).filter(Boolean);
      if (lines.length) this.knobs[key] = lines;
      else delete this.knobs[key];
    }
    this.queueDraft();
  }

  private entryTitle(item: Json): string {
    const main = item.name ?? item.text ?? item.statement ?? item.title ?? item.question ?? item.id;
    return `${String(main).slice(0, 48)}${String(item.id).length <= 8 ? "" : ""}`;
  }

  private entryForm(sectionRef: Section, item: Json): HTMLElement {
    const card = section(`正在编辑：${this.entryTitle(item)}`);
    card.classList.add("u-create-form"); // 探针按这个类找条目表单
    const locked = (this.locks[sectionRef.path] ?? []).includes(String(item.id));
    card.appendChild(
      el(
        "div",
        { class: "u-row" },
        chip(locked ? "已锁定（AI 不覆盖）" : "未锁定", locked ? "ok" : "muted"),
        button(locked ? "解锁并编辑" : "锁定此条", () => this.toggleLock(sectionRef.path, String(item.id), !locked)),
      ),
    );
    // 内部标识不摆在正文里（评审第六节）：要看时展开
    card.appendChild(
      el(
        "details",
        { class: "u-error-detail" },
        el("summary", { text: "技术详情" }),
        paragraph(`内部标识 ${String(item.id)}（系统生成，改名字不影响引用）`, "u-hint"),
      ),
    );
    const refs = refIndex(this.candidate!);
    for (const [key, value] of Object.entries(item)) {
      if (key === "id") continue;
      const node = controlFor(key, value, refs, (next) => {
        if (locked) return;
        item[key] = next;
        this.queueDraft();
      });
      card.appendChild(field(label(key), node));
    }
    if (locked) card.appendChild(paragraph("这一条已锁定：先点「解锁并编辑」才能改。", "u-hint"));
    return card;
  }

  private checkPanel(): HTMLElement {
    const card = section("检查与预览");
    card.classList.add("u-create-check");
    if (!this.errorsChecked) {
      // 空缓存不等于「没有问题」（评审：载入骨架后 this.errors = [] 被渲染成「这一份没有校验问题」）
      card.appendChild(paragraph("还没检查过：点「重新检查」看这一份还缺什么。", "u-hint"));
      card.appendChild(
        el("div", { class: "u-row" }, primary("重新检查", () => void this.validate())),
      );
    } else if (!this.errors.length) {
      card.appendChild(paragraph("刚检查过：这一份目前没有校验问题。", "u-hint"));
      card.appendChild(el("div", { class: "u-row" }, button("重新检查", () => void this.validate())));
    } else {
      card.appendChild(paragraph(`还有 ${this.errors.length} 项需要处理（点一条就能跳到它说的位置）：`));
      const list = el("ul", { class: "u-list" });
      for (const item of this.errors.slice(0, 20)) {
        const entry = describeProblem(item, catalogOf(this.candidate!));
        list.appendChild(
          el(
            "li",
            {},
            button(entry.text, () => this.locate(item), { class: "u-link" }),
          ),
        );
      }
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

  /** 这一段里有多少条校验问题（与 locate() 同一条匹配规则：先按分区路径，再按条目标识） */
  private errorsFor(sectionRef: Section): number {
    const ids = sectionRef.items.map((item) => String(item.id ?? "")).filter((id) => id.length > 2);
    const pkg = this.candidate!;
    return this.errors.filter(
      (problem) =>
        this.problemSection(problem, pkg) === sectionRef.path ||
        (ids.length > 0 && ids.some((id) => problem.includes(id))),
    ).length;
  }

  /**
   * 问题原文 → 分区路径（一次渲染里同一条问题只算一次：目录每一行都要问一遍）。
   * 键是问题原文：同一个 key 只会得到同一个答案。
   */
  private problemSection(problem: string, pkg: Json): string {
    const cache = (this.problemSections ??= new Map());
    const key = `${problem}`;
    const hit = cache.get(key);
    if (hit !== undefined) return hit;
    const section = sectionPathIn(problem, catalogOf(pkg));
    cache.set(key, section);
    return section;
  }

  /** 写校验结果：顺手把「问题 → 分区」的缓存清掉（它只对同一份结果有效） */
  private setErrors(list: string[]): void {
    this.errors = list.slice();
    this.errorsChecked = true;
    this.problemSections = null;
  }

  /**
   * 校验问题定位（评审：以前多数问题只能回一句「先看原文」）。
   * 落点顺序：问题里点名的条目 → 分区里第 N 条 → 分区本身；都对不上才说「先看问题清单」。
   */
  private locate(problem: string): void {
    const pkg = this.candidate!;
    const sections = catalogOf(pkg);
    const info = describeProblem(problem, sections);
    for (const item of sections) {
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
    if (info.section) {
      this.section = info.section;
      const target = sections.find((item) => item.path === info.section);
      const entry = info.index >= 0 ? target?.items[info.index] : target?.items[0];
      this.entryId = String(entry?.id ?? "");
      const label = SECTION_LABELS[info.section] ?? info.section;
      const where = entry ? `「${label} › ${this.entryTitle(entry)}」` : `「${label}」这个分区`;
      setNote(
        this.note,
        `已定位到${where}${entry ? "" : `（这一区目前 ${target?.items.length ?? 0} 条，要加一条点分区行上的「添加一条」）`}`,
        "ok",
      );
      void this.render();
      return;
    }
    setNote(this.note, `这条问题没有点名某个分区里的某一条，先看问题清单里的原文：${problem.slice(0, 120)}`, "pending");
  }

  private toggleLock(path: string, ident: string, on: boolean): void {
    const list = new Set(this.locks[path] ?? []);
    if (on) list.add(ident);
    else list.delete(ident);
    this.locks[path] = [...list];
    this.queueDraft();
    setNote(this.note, on ? "已锁定：AI 不会覆盖这一条" : "已解锁", "muted");
    void this.render();
  }

  private addEntry(path: string): void {
    const pkg = this.candidate!;
    const items = sectionRef(pkg, path);
    if (!items) return;
    // 空白骨架的第一条：优先照分区里已有的形状补字段，没有就照固定入口的骨架给
    // （「添加一条」不再要求分区里已经有一条 —— 评审 P0-4 的死路就堵在这里）
    const seed = items[0] ? { ...items[0] } : (resolveSeedRefs(declaredItem(path), pkg) as Json);
    const blank: Json = {};
    for (const [key, value] of Object.entries(seed)) {
      if (key === "id") continue;
      if (Array.isArray(value)) {
        // 数组是「引用」与「取值域」这类必填内容：新的一条也照抄一份，别留空数组去撞核心的阻断项
        blank[key] = JSON.parse(JSON.stringify(value));
        continue;
      }
      if (value && typeof value === "object") {
        blank[key] = JSON.parse(JSON.stringify(value));
        continue;
      }
      // 标量留空给用户填：seed 里的占位值（默认天气 / 默认角色标识）不该被当成用户写的内容
      blank[key] = typeof value === "boolean" ? false : typeof value === "number" ? 0 : "";
    }
    const taken = new Set(_allIds(pkg));
    let index = items.length + 1;
    let id = String(blank.id ?? `${path.split(".").pop()}-${index}`);
    while (taken.has(id)) id = `${path.split(".").pop()}-${(index += 1)}`;
    blank.id = id;
    items.push(blank);
    this.section = path;
    this.entryId = id;
    this.queueDraft();
    setNote(this.note, "已添加一条：填完记得点「重新检查」", "pending");
    void this.render();
  }

  private duplicateEntry(path: string, item: Json): void {
    const items = sectionRef(this.candidate!, path);
    if (!items) return;
    const prefix = String(item.id).split("-")[0] || "x";
    let index = items.length + 1;
    while (items.some((row) => String(row.id) === `${prefix}-副本${index}`)) index += 1;
    const copy: Json = { ...JSON.parse(JSON.stringify(item)), id: `${prefix}-副本${index}` };
    if (typeof copy.name === "string") copy.name = `${copy.name}（副本）`;
    items.push(copy);
    this.entryId = String(copy.id);
    this.queueDraft();
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
    const items = sectionRef(pkg, path);
    if (!items) return;
    const modal = dialog(
      `删除「${this.entryTitle(item)}」？`,
      [paragraph("没有被引用，删掉不影响其他条目。这一步不能撤销，但可以重新添加。")],
      [
        {
          label: "删除",
          run: () => {
            const index = items.findIndex((row) => String(row.id) === ident);
            if (index >= 0) items.splice(index, 1);
            if (this.entryId === ident) this.entryId = "";
            this.queueDraft();
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
      this.setErrors((result.errors as string[]) ?? []); // 起草自带一次校验：结果可以照实报
      this.usage = (result.usage as Json) ?? null;
      const meta = (this.candidate?.meta as Json) ?? {};
      if (!this.name) this.name = String(meta.original_name ?? "");
      this.section = catalogOf(this.candidate ?? {}).at(0)?.path ?? "";
      this.queueDraft();
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
      this.setErrors((result.errors as string[]) ?? []);
      this.usage = (result.usage as Json) ?? this.usage;
      this.queueDraft();
      setNote(this.note, "改完了：锁定的条目原样保留", "ok");
      void this.render();
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界设定", action: "按一句话修改", done: "候选没被采用" }).message, "bad");
    }
  }

  private async fillSection(): Promise<void> {
    if (!this.candidate || !this.section) return;
    const current = sectionsOf(this.candidate).find((item) => item.path === this.section);
    const segment = this.segmentOf(this.section);
    setNote(this.note, `正在重跑「${current?.label ?? this.section}」所在的那一段…`, "pending");
    try {
      const result = await this.ctx.api.packageFill({
        package: this.candidate,
        section: segment,
        knobs: this.knobs,
        locked: this.locks,
      });
      if (result.candidate) this.candidate = result.candidate as Json;
      this.setErrors((result.errors as string[]) ?? []);
      this.usage = (result.usage as Json) ?? this.usage;
      this.queueDraft();
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
      this.setErrors((result.errors as string[]) ?? []);
      this.queueDraft();
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
    // 走同一条草稿链（固定键 + 同一个状态槽）：保存状态由 DraftKeeper 写在草稿行上
    this.queueDraft();
    await this.ctx.drafts.flush(CREATE_DRAFT_KEY);
  }

  /* ------------------------------------------------------------ 确认 → 角色 */

  private async confirmWorld(): Promise<void> {
    if (!this.candidate) return;
    const pkg = this.candidate;
    const meta = (pkg.meta as Json) ?? {};
    const editing = Boolean(this.savedPath);
    if (this.name.trim()) {
      // 原始名称在首次确认时固定（核心语义）：改名字只改显示名，别把「来自哪个设定」改掉
      if (!editing || !String(meta.original_name ?? "").trim()) meta.original_name = this.name.trim();
      meta.display_name = this.name.trim();
    }
    if (this.brief.trim()) meta.description = this.brief.trim();
    pkg.meta = meta;
    this.queueDraft();
    setNote(this.note, "正在检查这份设定…", "pending");
    try {
      const checked = await this.ctx.api.packageValidate({ package: pkg });
      this.setErrors((checked.errors as string[]) ?? []);
      if (this.errors.length) {
        setNote(this.note, `还有 ${this.errors.length} 项没过：按问题清单改完再确认（可以先「保存草稿」）`, "bad");
        void this.render();
        return;
      }
      if (editing) {
        // 编辑已有设定就覆盖原文件：以前每次都 uniqueFile() 另存，反复确认堆出一串同名副本（评审 P1）
        await this.ctx.api.packageSave(this.savedPath, pkg);
        this.base = this.savedPath;
        setNote(this.note, `已更新「${this.name}」这份世界设定（改设定只影响以后创建的世界）`, "ok");
        this.goto("cards");
        return;
      }
      const file = await this.uniqueFile();
      const saved = await this.ctx.api.packageSave(file, pkg);
      this.base = String(saved.path ?? file);
      this.savedPath = this.base;
      setNote(this.note, `世界设定已确认：${this.name}（改设定只影响以后创建的世界）`, "ok");
      this.goto("cards");
    } catch (error) {
      setNote(this.note, uiError(error, { module: "世界设定", action: "确认设定", done: "没有覆盖任何材料" }).message, "bad");
    }
  }

  /** 另存为一份新设定：文件名先给用户看清再写（评审：要另存就明确问一次） */
  private async saveAsNew(): Promise<void> {
    if (!this.candidate) return;
    const file = await this.uniqueFile();
    const note = el("p", { class: "u-note" });
    const modal = dialog(
      "另存为一份新的世界设定",
      [
        paragraph(`这份内容会写进创作目录里的「${file}」，原来那份保持不动。`),
        paragraph("以后创建的世界会用新的这一份；已经创建的世界不受影响。", "u-hint"),
        note,
      ],
      [
        {
          label: "写进这个文件",
          primary: true,
          run: async () => {
            try {
              const saved = await this.ctx.api.packageSave(file, this.candidate as Json);
              this.base = String(saved.path ?? file);
              this.savedPath = this.base;
              setNote(this.note, `已另存为「${file}」：以后创建的世界用这一份`, "ok");
            } catch (error) {
              setNote(note, uiError(error, { module: "世界设定", action: "另存为", done: "没有改动任何文件" }).message, "bad");
              return false;
            }
          },
        },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
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
    const host = panel("准备角色");
    this.root!.appendChild(host);
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
      // 没有文字标签的复选框用户不知道是干什么的：给读屏与悬停都说清
      tick.setAttribute("aria-label", `把「${String(item.name ?? file)}」用于这一局`);
      tick.title = `把「${String(item.name ?? file)}」用于这一局`;
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
    const host = panel("准备角色");
    this.root!.appendChild(host);
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
        paragraph("起草会带着当前世界设定：角色卡里的信息来源、记载与联络方式都从这份设定里选。", "u-hint"),
      ),
    );
    if (!this.card) {
      host.appendChild(paragraph("还没有草稿：先写一句描述再起草，或者直接点「让 AI 起草」用世界设定补一份。"));
      return;
    }
    host.appendChild(this.cardForm());
    host.appendChild(this.cardLockPanel());
    const check = section("检查");
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
    const box = section("角色内容");
    box.classList.add("u-card-form"); // 探针按这个类找角色卡字段
    const card = this.card!;
    const refs = refIndex(this.candidate ?? {});
    const entries = Object.entries(card);
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
    const host = panel("检查与确认");
    this.root!.appendChild(host);
    const pkg = this.candidate!;
    const sections = sectionsOf(pkg);
    host.appendChild(
      el(
        "div",
        { class: "u-row" },
        button("返回角色", () => this.goto("cards")),
        // 动作名带上目标步骤：进度轨只回答「走到哪一步了」，不回答「点哪里去下一步」
        primary("下一步：创建世界", () => this.goto("create")),
      ),
    );
    host.appendChild(
      section(
        "创建摘要",
        facts([
          ["世界名", this.name || "（未命名）"],
          ["设定来源", this.source === "ai" ? "AI 起草" : this.source === "manual" ? "自己填写" : "样例改造"],
          ["设定内容", `${sections.length} 个分区、${sections.reduce((sum, item) => sum + item.items.length, 0)} 条`],
          ["角色", this.cardFiles.map((file) => file.replace(/\.card\.json$/, "")).join("、") || "（还没选）"],
          ["校验", this.errorsChecked ? (this.errors.length ? `${this.errors.length} 项待处理` : "通过") : "还没检查"],
        ]),
      ),
    );
    const nameInput = el("input", { class: "u-input", value: this.worldName || this.name, id: "u-create-worldname" }) as HTMLInputElement;
    nameInput.addEventListener("input", () => {
      this.worldName = nameInput.value;
    });
    host.appendChild(field("这个世界的名字（以后可以重命名）", nameInput));
    host.appendChild(
      paragraph("创建会把这份设定固定下来：以后改设定只影响新创建的世界，不会追溯改写这个已存在的世界。"),
    );
  }

  /* ------------------------------------------------------------ 步骤五 / 六：创建与开始 */

  private renderCreate(): void {
    const host = panel("创建");
    this.root!.appendChild(host);
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
      paragraph("创建分两步：先建世界（把设定固定下来），再按你的选择启动或先暂停；任一步失败都会保留已经完成的部分。"),
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
      // 正式设定已经落地：把向导草稿丢掉，别让它在「未完成内容」里当第二份残留
      await this.ctx.drafts.discard(CREATE_DRAFT_KEY);
      const instanceId = String(this.created.id ?? "");
      const info = instanceId ? await this.ctx.api.instanceInfo(instanceId) : {};
      const timelines = (info.timelines as Json[]) ?? [];
      const firstTimeline = timelines.find((item) => String(item.state) !== "archived") ?? timelines[0];
      this.createdTimelineId = String(firstTimeline?.id ?? "");
      // 出口指向刚建的世界：不写 sel.contact，「去和角色联络」会落到上次用过的那个世界（评审 P1）
      if (this.createdTimelineId) {
        await this.ctx.setPrefs({
          "sel.contact": {
            instance_id: instanceId,
            timeline_id: this.createdTimelineId,
            timeline_name: String(firstTimeline?.name ?? ""),
            character_id: "",
            character_name: "",
          },
        });
      }
      if (run && instanceId && firstTimeline) await this.ctx.api.activate(instanceId, String(firstTimeline.id), 1);
      setNote(
        this.note,
        run ? "世界已创建并开始运行" : "世界已创建（世界线处于暂停；想开始时到世界与素材里点「启动」）",
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

  /** 「去和角色联络」：带上刚建好的那个世界与世界线，别落到别的世界上 */
  private goContact(): void {
    const instanceId = String(this.created?.id ?? "");
    if (!instanceId) {
      this.ctx.navigate({ pane: "contact" });
      return;
    }
    void this.ctx.setPrefs({
      "sel.contact": {
        instance_id: instanceId,
        timeline_id: this.createdTimelineId,
        timeline_name: "",
        character_id: "",
        character_name: "",
      },
    });
    this.ctx.navigate({ pane: "contact" });
  }

  /** 「打开这个世界」：入口只认第一条世界，所以先把刚建的这个记成「最近使用」，再进详情 */
  private goWorlds(): void {
    const instanceId = String(this.created?.id ?? "");
    const name = String(this.created?.name ?? this.worldName ?? this.name);
    if (instanceId) {
      this.ctx.rememberRecent({ pane: "worlds", label: `世界 · ${name}`, key: `worlds:${instanceId}` });
    }
    this.ctx.navigate({ pane: "worlds", sub: "detail" });
  }

  private renderStart(): void {
    const host = panel("开始方式");
    this.root!.appendChild(host);
    const instanceId = String(this.created?.id ?? "");
    const name = String(this.created?.name ?? this.worldName ?? this.name);
    host.appendChild(
      section(
        `世界「${name}」已经建好`,
        facts([
          ["里面有什么", `${this.cardFiles.length} 位角色`],
          ["状态", "可以在世界与素材里看世界线与版本"],
        ]),
        el(
          "div",
          { class: "u-row" },
          primary("去和角色联络", () => this.goContact()),
          button("打开这个世界", () => this.goWorlds()),
          button("再创建一个世界", () => {
            this.step = "source";
            this.candidate = null;
            this.savedPath = "";
            this.base = "";
            this.cardFiles = [];
            this.created = null;
            this.createdTimelineId = "";
            this.usage = null;
            this.errors = [];
            this.errorsChecked = false;
            this.problemSections = null;
            this.locks = {};
            void this.render();
          }),
        ),
        paragraph("同一个设定可以创建多个世界；已有的世界不会因为改设定而变。", "u-hint"),
      ),
    );
    if (instanceId) {
      host.appendChild(
        el(
          "details",
          { class: "u-error-detail" },
          el("summary", { text: "技术详情" }),
          paragraph(`这个世界的内部标识：${instanceId}`, "u-hint"),
        ),
      );
    }
  }
}
