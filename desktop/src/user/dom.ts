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

/** 字节数给人看（备份大小这类） */
export function sizeText(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value >= 10 || unit === 0 ? Math.round(value) : value.toFixed(1)} ${units[unit]}`;
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

/* ------------------------------------------------------------------ 页面骨架
 * 全站统一的三带：标题带 / 工具带 / 内容带。三者的外观在 user.css 里定死，
 * 各页只负责把内容塞进去（2026-10-08 视觉体系审查：此前每页各写各的标题与分区，
 * 标题纵向位置出现过 77/94/154 三种，返回按钮的语义也有四种）。
 */

/** ① 标题带：页面名 + 一句定位语 + 右侧主操作 */
export function pageHead(title: string, subtitle = "", actions: Child[] = []): HTMLElement {
  const head = el(
    "header",
    { class: "u-page-head" },
    el(
      "div",
      { class: "u-page-head-main" },
      el("h2", { class: "u-h2", text: title }),
      subtitle ? el("p", { class: "u-page-sub", text: subtitle }) : null,
    ),
  );
  if (actions.length) head.appendChild(el("div", { class: "u-page-head-actions" }, ...actions));
  return head;
}

export interface ToolItem {
  label: string;
  current?: boolean;
  onSelect: () => void;
}

/** ② 工具带：分区切换（选中态是一条下划线，与动作按钮、锚点胶囊区分开） */
export function tools(items: ToolItem[], ariaLabel = "分区"): HTMLElement {
  const nav = el("nav", { class: "u-tools", "aria-label": ariaLabel });
  for (const item of items) {
    const node = button(item.label, item.onSelect);
    if (item.current) node.setAttribute("aria-current", "page");
    nav.appendChild(node);
  }
  return nav;
}

/** 锚点胶囊：页面内跳转（小、灰底）——与分区、动作都不是一个形状 */
export function anchors(items: Array<{ label: string; onSelect: () => void }>, ariaLabel = "页内跳转"): HTMLElement {
  const nav = el("nav", { class: "u-anchors", "aria-label": ariaLabel });
  for (const item of items) nav.appendChild(button(item.label, item.onSelect));
  return nav;
}

/** 页面级分区（一级重量）：不描边、只给底色，用来把一大块内容分组 */
export function panel(title: string, ...children: Child[]): HTMLElement {
  const node = el("section", { class: "u-panel" });
  if (title) node.appendChild(el("h3", { class: "u-section-title", text: title }));
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
  // 没有的阶段 / 关联编号整行不出：摆一行「阶段 —」既占位又像是真有一条读数
  const rows: Array<[string, string]> = [
    ["模块", error.module],
    ["操作", error.action],
  ];
  if (error.stage) rows.push(["阶段", error.stage]);
  rows.push(["原因码", error.code]);
  rows.push(["可重试", error.retryable ? "是" : "否"]);
  if (error.requestId) rows.push(["关联编号", error.requestId]);
  rows.push(["发生时间", stamp(Date.now() / 1000)]);
  detail.appendChild(facts(rows));
  node.appendChild(detail);
  return node;
}

/** 主操作明确外观 + 动作名（不用「确定」这类空话） */
export function primary(label: string, onClick: () => void, opts: { disabled?: boolean; id?: string } = {}): HTMLButtonElement {
  return button(label, onClick, { class: "u-btn u-primary", ...opts });
}

/**
 * 对话框动作。`run` 返回 `false`（或异步解析为 `false`）= 这一步没成功，**不要关窗**：
 * 校验失败与请求失败写在窗内的提示才看得见（2026-10-07 可用性评审：以前是先 close 再 run，
 * 失败提示写进了已移除的节点，用户点了没反应）。返回 `void/true` 维持旧行为（执行后关窗）。
 */
export type DialogAction = {
  label: string;
  run: () => void | boolean | Promise<void | boolean>;
  primary?: boolean;
};

export function dialog(title: string, body: Child[], actions: DialogAction[]): { node: HTMLElement; close: () => void } {
  const overlay = el("div", { class: "u-dialog-backdrop" });
  const box = el("div", { class: "u-dialog", role: "dialog", "aria-modal": "true", "aria-label": title });
  const restore = document.activeElement as HTMLElement | null;
  box.appendChild(el("h3", { class: "u-dialog-title", text: title }));
  const content = el("div", { class: "u-dialog-body" });
  append(content, body);
  box.appendChild(content);
  const row = el("div", { class: "u-dialog-actions" });
  /**
   * 关掉模态要把焦点还给打开它的那个控件。
   * 为什么不能直接 `restore.focus()`：多数弹窗是从列表里点开的，关窗后列表会重画，
   * 原来那个节点已经从文档里摘下——对游离节点调 focus() 是空操作，焦点会掉回 body，
   * 读屏与键盘用户就丢失了位置（2026-10-08 无障碍走查实测）。退路是把焦点放到正文容器上。
   */
  const giveBackFocus = () => {
    if (restore && restore.isConnected && typeof restore.focus === "function") {
      restore.focus();
      return;
    }
    const host = document.querySelector<HTMLElement>("#u-main");
    if (host) {
      host.tabIndex = -1;
      host.focus();
    }
  };
  const close = () => {
    overlay.remove();
    document.removeEventListener("keydown", onKey);
    giveBackFocus();
  };
  for (const action of actions) {
    row.appendChild(
      button(action.label, () => {
        let result: void | boolean | Promise<void | boolean>;
        try {
          result = action.run();
        } catch {
          return; // 抛异常不关窗：错误信息由调用方写在窗内
        }
        if (result instanceof Promise) {
          void result
            .then((ok) => {
              if (ok !== false) close();
            })
            .catch(() => undefined); // 失败不关窗；错误信息由调用方写在窗内
          return;
        }
        if (result !== false) close();
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
    if (event.key === "Escape") {
      close();
      return;
    }
    // 焦点陷阱：Tab / Shift+Tab 只在窗内循环，不跑到被遮住的页面上
    if (event.key !== "Tab") return;
    const focusables = [...box.querySelectorAll<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )];
    if (!focusables.length) return;
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement as HTMLElement | null;
    if (!active || !box.contains(active)) {
      event.preventDefault();
      first.focus();
      return;
    }
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };
  // 回车不自动触发主操作：这里的主操作多数是「删除这个世界」「重命名」这类会改数据的动作，
  // 打开就聚焦在框上，一个多余的回车比多一次点击贵得多。回车在按钮上天然可用（Tab 到再按）。
  document.addEventListener("keydown", onKey);
  box.tabIndex = -1;
  /**
   * 「打开即入框」不能在 dialog() 里同步做：这个函数返回之后调用方才把 overlay 插进文档，
   * 对游离节点调 focus() 是空操作——焦点其实留在了被遮住的触发按钮上（2026-10-08 无障碍走查实测：
   * 弹窗打开后 document.activeElement 不在窗内）。所以等挂上去再聚焦：微任务够用，
   * 调用方要是异步追加，下一帧这次补上。
   */
  const focusBox = () => {
    if (box.isConnected) box.focus();
  };
  queueMicrotask(focusBox);
  requestAnimationFrame(focusBox);
  return { node: overlay, close };
}
