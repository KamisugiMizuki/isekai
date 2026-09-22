/*
 * 正式界面的 DOM 基础件（无框架；类名前缀 u-，与调试壳的 #app 互不干扰）。
 *
 * 这里只做元素与文案：命名化控件（动作名当按钮文字）、常驻标签、
 * 就近反馈槽、错误卡（操作对象 / 直接原因 / 已完成范围 / 下一步）。
 */

import type { UiError } from "./api";

export type Child = Node | string | number | null | undefined | false;

export interface Attrs {
  class?: string;
  id?: string;
  title?: string;
  role?: string;
  text?: string;
  value?: string;
  type?: string;
  placeholder?: string;
  disabled?: boolean;
  hidden?: boolean;
  href?: string;
  /** data-* 与 aria-* 直接给（键名照写） */
  [key: string]: unknown;
}

export function el<K extends keyof HTMLElementTagNameMap>(
  tag: K,
  attrs: Attrs = {},
  ...children: Child[]
): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = String(value);
    else if (key === "text") node.textContent = String(value);
    else if (key === "hidden") node.hidden = Boolean(value);
    else if (key === "disabled") (node as HTMLButtonElement).disabled = Boolean(value);
    else if (key === "value") (node as HTMLInputElement).value = String(value);
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

export function append(node: Node, children: Child[]): void {
  for (const child of children) {
    if (child === null || child === undefined || child === false) continue;
    node.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
  }
}

export function clear(node: Element): void {
  while (node.firstChild) node.removeChild(node.firstChild);
}

export function fill(node: Element, ...children: Child[]): void {
  clear(node);
  append(node, children);
}

export function button(
  label: string,
  onClick: () => void,
  opts: { class?: string; disabled?: boolean; title?: string; id?: string } = {},
): HTMLButtonElement {
  const node = el("button", {
    type: "button",
    class: opts.class ?? "u-btn",
    title: opts.title ?? "",
    id: opts.id ?? "",
    disabled: opts.disabled ?? false,
    text: label,
  });
  node.addEventListener("click", onClick);
  return node;
}

export function link(label: string, onClick: () => void, cls = "u-link"): HTMLButtonElement {
  return button(label, onClick, { class: cls });
}

/** 常驻标签 + 控件（placeholder 不代替标签） */
export function field(label: string, control: HTMLElement, hint = ""): HTMLElement {
  const labelNode = el("label", { class: "u-field" }, el("span", { class: "u-field-label", text: label }), control);
  if (hint) labelNode.appendChild(el("span", { class: "u-hint", text: hint }));
  return labelNode;
}

export function section(title: string, ...children: Child[]): HTMLElement {
  const node = el("section", { class: "u-section" }, el("h3", { class: "u-section-title", text: title }));
  append(node, children);
  return node;
}

export function chip(label: string, kind: "ok" | "pending" | "bad" | "muted" = "muted"): HTMLElement {
  return el("span", { class: `u-chip u-chip-${kind}`, text: label, role: "status", "aria-live": "polite" });
}

export function facts(rows: Array<[string, string]>): HTMLElement {
  const node = el("dl", { class: "u-facts" });
  for (const [key, value] of rows) {
    node.appendChild(el("dt", { text: key }));
    node.appendChild(el("dd", { text: value || "—" }));
  }
  return node;
}

export function bulletList(items: Child[], cls = "u-list"): HTMLUListElement {
  const node = el("ul", { class: cls });
  for (const item of items) node.appendChild(el("li", {}, item));
  return node;
}

export function paragraph(textValue: string, cls = "u-p"): HTMLParagraphElement {
  return el("p", { class: cls, text: textValue });
}

/** 就近反馈槽：一行一个槽，槽只服务本行（不往页顶写） */
export function noteSlot(id: string): HTMLElement {
  return el("p", { class: "u-note", id, role: "status", "aria-live": "polite" });
}

export function setNote(node: HTMLElement | null, textValue: string, kind: "ok" | "bad" | "pending" | "muted" = "muted"): void {
  if (!node) return;
  node.textContent = textValue;
  node.className = `u-note u-note-${kind}`;
}

export function stamp(epochSeconds: number): string {
  if (!epochSeconds) return "";
  const date = new Date(epochSeconds * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export function humanDuration(seconds: number): string {
  const value = Math.max(0, Math.round(seconds));
  if (value < 60) return `${value} 秒`;
  if (value < 3600) return `${Math.round(value / 60)} 分钟`;
  if (value < 86400) return `${(value / 3600).toFixed(1)} 小时`;
  return `${(value / 86400).toFixed(1)} 天`;
}

/** 错误卡（ONBOARDING §5）：主信息 / 影响说明 / 处理动作 / 技术详情 */
export function errorCard(error: UiError, actions: Array<{ label: string; run: () => void }> = []): HTMLElement {
  const node = el("div", { class: "u-error", role: "alert" });
  node.appendChild(el("p", { class: "u-error-title", text: error.message }));
  const range = el("p", { class: "u-error-scope" });
  range.textContent = `已经完成：${error.done}；尚未确认：${error.unknown}。`;
  node.appendChild(range);
  if (error.target) node.appendChild(el("p", { class: "u-error-target", text: `对象：${error.target}` }));
  const row = el("div", { class: "u-row" });
  for (const action of actions) row.appendChild(button(action.label, action.run));
  if (row.childElementCount) node.appendChild(row);
  const detail = el("details", { class: "u-error-detail" }, el("summary", { text: "查看技术详情" }));
  detail.appendChild(
    facts([
      ["模块", error.module],
      ["操作", error.action],
      ["阶段", error.stage],
      ["原因码", error.code],
      ["可重试", error.retryable ? "是" : "否"],
      ["关联编号", error.requestId || "—"],
      ["发生时间", stamp(Date.now() / 1000)],
    ]),
  );
  node.appendChild(detail);
  return node;
}

/** 主操作明确外观 + 动作名（不用「确定」这类空话） */
export function primary(label: string, onClick: () => void, opts: { disabled?: boolean; id?: string } = {}): HTMLButtonElement {
  return button(label, onClick, { class: "u-btn u-primary", ...opts });
}

export function dialog(title: string, body: Child[], actions: Array<{ label: string; run: () => void; primary?: boolean }>): { node: HTMLElement; close: () => void } {
  const overlay = el("div", { class: "u-dialog-backdrop" });
  const box = el("div", { class: "u-dialog", role: "dialog", "aria-modal": "true", "aria-label": title });
  const restore = document.activeElement as HTMLElement | null;
  box.appendChild(el("h3", { class: "u-dialog-title", text: title }));
  const content = el("div", { class: "u-dialog-body" });
  append(content, body);
  box.appendChild(content);
  const row = el("div", { class: "u-dialog-actions" });
  const close = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
    restore?.focus?.(); // 关掉模态要把焦点还给打开它的那个控件
  };
  for (const action of actions) {
    row.appendChild(
      button(action.label, () => {
        close();
        action.run();
      }, { class: action.primary ? "u-btn u-primary" : "u-btn" }),
    );
  }
  if (!actions.length) row.appendChild(button("关闭", close));
  box.appendChild(row);
  overlay.appendChild(box);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });
  const onKey = (event: KeyboardEvent) => {
    if (event.key === "Escape") close();
  };
  document.addEventListener("keydown", onKey);
  box.tabIndex = -1;
  box.focus(); // 打开即入框：键盘 / 读屏用户不会掉在页面别处
  return { node: overlay, close };
}
