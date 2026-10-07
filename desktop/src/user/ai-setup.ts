/*
 * AI 服务配置的共用块：首次设置向导与设置页共用同一份控件与校验。
 *
 * 为什么单独一个文件（2026-10-07 普通用户可用性评审 P0-1/P1-1）：
 * 两处各写一遍必然分叉——向导里有地址格式校验、设置页没有；预置服务的密钥申请地址
 * 定义了却从没渲染给用户。用户的目标只有一句话：**从零拿到一个能用的密钥**，
 * 所以这里把「去哪申请、抄哪个模型名、地址要不要带 /v1」全部摆在字段旁边。
 */

import { invoke } from "@tauri-apps/api/core";

import type { Json } from "./api";
import { button, el, field, paragraph } from "./dom";

export interface PresetService {
  id: string;
  label: string;
  base_url: string;
  model: string;
  /** 密钥申请页；空串 = 自定义服务，界面只给通用说明 */
  key_url: string;
  /** 从注册到拿到密钥的四步（说人话，不含行话） */
  steps: string;
}

export const PRESET_SERVICES: PresetService[] = [
  {
    id: "deepseek",
    label: "DeepSeek（推荐：地址和模型名已经填好，只要补密钥）",
    base_url: "https://api.deepseek.com",
    model: "deepseek-v4-flash",
    key_url: "https://platform.deepseek.com/api_keys",
    steps: "注册并登录 → 充值或领取免费额度 → 在「API keys」页点创建 → 把那一整串复制过来（不要带空格或引号）",
  },
  {
    id: "other",
    label: "其他兼容服务（OpenAI 兼容接口）",
    base_url: "",
    model: "",
    key_url: "",
    steps:
      "地址、模型名与密钥都按服务提供方给的说明填写。地址要以 http:// 或 https:// 开头，"
      + "而且多数兼容服务要带 /v1（例如 https://api.openai.com/v1，漏了会报「找不到地址或模型」）",
  },
];

/** 打开外部链接；这台机器不让打开时退化为「复制地址」，不让用户卡在一句话上 */
export async function openExternal(
  url: string,
  notify: (text: string, kind: "ok" | "bad" | "muted") => void,
): Promise<void> {
  try {
    await invoke("open_url", { url });
  } catch {
    try {
      await navigator.clipboard.writeText(url);
      notify("没能直接打开浏览器：地址已经复制，粘贴到浏览器地址栏就能打开。", "bad");
    } catch {
      notify(`没能打开浏览器，请手动访问：${url}`, "bad");
    }
  }
}

export interface AiSetupOptions {
  /** 已保存的 llm 配置（设置页与向导都读它） */
  saved: Json;
  apiKeySet: boolean;
  apiKeyMasked?: string;
  servicePref?: string;
  onServicePref?: (id: string) => void;
  notify?: (text: string, kind: "ok" | "bad" | "muted") => void;
}

export interface AiSetup {
  /** 直接 append 进表单的控件块（顺序即界面顺序） */
  nodes: HTMLElement[];
  service: HTMLSelectElement;
  baseUrl: HTMLInputElement;
  model: HTMLInputElement;
  key: HTMLInputElement;
  timeout: HTMLInputElement;
  maxTokens: HTMLInputElement;
  temperature: HTMLInputElement;
  keyHelp: HTMLElement;
  advanced: HTMLDetailsElement;
  applyPreset(): void;
  collect(): Json;
  /** 校验（返回一句人话；同时已写进提示行），通过返回 null */
  validate(note: HTMLElement): string | null;
}

export function aiSetup(options: AiSetupOptions): AiSetup {
  const saved = options.saved ?? {};
  const service = el("select", { class: "u-input", id: "ai-service" }) as HTMLSelectElement;
  for (const item of PRESET_SERVICES) service.appendChild(el("option", { value: item.id, text: item.label }));
  const presetFor = (id: string) => PRESET_SERVICES.find((item) => item.id === id) ?? PRESET_SERVICES[0];
  const prefId = options.servicePref;
  service.value = PRESET_SERVICES.some((item) => item.id === prefId)
    ? String(prefId)
    : saved.base_url && String(saved.base_url) !== PRESET_SERVICES[0].base_url
      ? "other"
      : "deepseek";

  const baseUrl = el("input", {
    class: "u-input",
    id: "ai-base-url",
    value: String(saved.base_url ?? ""),
    placeholder: "https://api.example.com/v1",
  }) as HTMLInputElement;
  const model = el("input", {
    class: "u-input",
    id: "ai-model",
    value: String(saved.model ?? ""),
    placeholder: "例如 deepseek-v4-flash",
  }) as HTMLInputElement;
  const key = el("input", {
    class: "u-input",
    id: "ai-key",
    type: "password",
    autocomplete: "off",
    placeholder: String(
      options.apiKeySet
        ? `已设置（${String(options.apiKeyMasked ?? "")}）；留空表示不改`
        : "把申请到的密钥整串粘贴到这里",
    ),
  }) as HTMLInputElement;
  const timeout = el("input", { class: "u-input", id: "ai-timeout", type: "number", min: "1", value: String(saved.timeout_s ?? 60) }) as HTMLInputElement;
  const maxTokens = el("input", { class: "u-input", id: "ai-max-tokens", type: "number", min: "1", value: String(saved.max_tokens ?? 1024) }) as HTMLInputElement;
  const temperature = el("input", { class: "u-input", id: "ai-temp", type: "number", min: "0", max: "2", step: "0.1", value: String(saved.temperature ?? 0.8) }) as HTMLInputElement;

  const keyHelp = el("div", { class: "u-key-help", id: "ai-key-help" });
  const advanced = el(
    "details",
    { class: "u-advanced" },
    el("summary", { text: "高级选项（一般不用改）：服务地址、等待时间、输出长度、随机程度" }),
    field("服务地址", baseUrl, "接口地址，不是聊天网页的网址。多数兼容服务要带 /v1，例如 https://api.openai.com/v1"),
    field("等待时间（秒）", timeout),
    field("单次输出长度（token）", maxTokens),
    field("生成随机程度（0–2）", temperature),
  ) as HTMLDetailsElement;

  const applyPreset = (): void => {
    const preset = presetFor(service.value);
    if (preset.base_url) baseUrl.value = preset.base_url;
    if (preset.model) model.value = preset.model;
    fillKeyHelp(preset);
  };

  function fillKeyHelp(preset: PresetService): void {
    const rows: (HTMLElement | null)[] = [
      paragraph(`密钥在哪申请：${preset.steps}`, "u-hint"),
      preset.key_url
        ? el(
            "div",
            { class: "u-row" },
            // 主按钮位留给这一格的「保存/测试」：申请页是外链，用描边按钮（一格里两个实心黑按钮会打架）
            button("打开密钥申请页", () => {
              void openExternal(preset.key_url, options.notify ?? (() => undefined));
            }),
            button("只复制地址", () => {
              void navigator.clipboard
                .writeText(preset.key_url)
                .then(() => (options.notify ?? (() => undefined))("密钥申请页地址已复制", "ok"))
                .catch(() => (options.notify ?? (() => undefined))(`请手动访问：${preset.key_url}`, "bad"));
            }),
          )
        : null,
      preset.key_url
        ? paragraph(`申请页地址：${preset.key_url}`, "u-hint u-selectable")
        : paragraph("自定义服务：密钥在服务提供方的控制台里创建，通常是「API Keys」这类页面。", "u-hint"),
      paragraph("网页聊天账号登录 ≠ 接口密钥：能聊天不代表有密钥，密钥要在控制台里单独创建。", "u-hint"),
    ];
    fill(keyHelp, ...rows);
  }

  service.addEventListener("change", () => {
    options.onServicePref?.(service.value);
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

  const validate = (note: HTMLElement): string | null => {
    const base = baseUrl.value.trim();
    const modelName = model.value.trim();
    if (!base) {
      advanced.open = true; // 提示点名的字段必须先看得见
      const message = "还差「服务地址」：展开上面的「高级选项」填进去（多数兼容服务要带 /v1）。";
      setNote(note, message, "bad");
      return message;
    }
    if (!/^https?:\/\//.test(base)) {
      advanced.open = true;
      const message = "服务地址要以 http:// 或 https:// 开头——这是接口地址，不是聊天网页的网址。";
      setNote(note, message, "bad");
      return message;
    }
    if (!modelName) {
      const message = "还差「模型」：照着服务提供方给的模型名填（用上面的预置服务时已经填好了）。";
      setNote(note, message, "bad");
      return message;
    }
    return null;
  };

  applyPreset();
  const nodes: HTMLElement[] = [
    field("服务", service),
    keyHelp,
    field("模型", model, "照抄服务提供方给的模型名；预置服务已填好，写错不会被自动纠正"),
    field("访问密钥", key, "留空表示不改动已保存的密钥"),
    advanced,
  ];
  return { nodes, service, baseUrl, model, key, timeout, maxTokens, temperature, keyHelp, advanced, applyPreset, collect, validate };
}

// 与 dom.setNote 同一行为，这里只为了不改动调用方的 import 形状
function fill(node: HTMLElement, ...children: (Node | string | null)[]): void {
  while (node.firstChild) node.removeChild(node.firstChild);
  for (const child of children) {
    if (child === null) continue;
    node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
  }
}

function setNote(node: HTMLElement, text: string, kind: "ok" | "bad" | "pending" | "muted"): void {
  node.textContent = text;
  node.className = `u-note u-note-${kind}`;
}
