/*
 * 方案 B 原型:应用启动选择器
 * 
 * 启动时弹窗选择 Chat/Writer/GM 之一,选择后整个窗口只为该应用服务。
 * 
 * ponytail: 原型用 CSS 弹窗,正式版改 Tauri native dialog
 */

import type { AppContext } from "./app";
import { el } from "./dom";

export type AppMode = "chat" | "writer" | "gm";

interface AppChoice {
  mode: AppMode;
  icon: string;
  title: string;
  subtitle: string;
  recent?: string;
}

const APPS: AppChoice[] = [
  { mode: "chat", icon: "💬", title: "isekai Chat", subtitle: "与虚拟角色对话" },
  { mode: "writer", icon: "✍️", title: "isekai Writer", subtitle: "辅助写作和大纲管理" },
  { mode: "gm", icon: "🎲", title: "isekai GM", subtitle: "跑团主持和规则裁定" },
];

export class Launcher {
  private overlay: HTMLElement | null = null;

  constructor(private readonly ctx: AppContext) {}

  /** 显示应用选择器 */
  show(): void {
    const lastMode = this.ctx.prefs.app_mode as AppMode | undefined;
    
    // ponytail: 有上次选择且 < 5 秒前启动 = 直接进入,不弹窗(快速重启场景)
    if (lastMode && this.isRecentLaunch()) {
      this.launch(lastMode);
      return;
    }

    this.overlay = el("div", { class: "u-launcher-overlay" });
    const dialog = el("div", { class: "u-launcher" });
    dialog.appendChild(el("h1", { text: "选择应用", class: "u-h1" }));

    for (const app of APPS) {
      const card = el("button", { class: "u-launcher-card" });
      card.appendChild(el("div", { class: "u-launcher-icon", text: app.icon }));
      card.appendChild(el("h2", { text: app.title }));
      card.appendChild(el("p", { text: app.subtitle, class: "u-note" }));
      
      if (app.mode === lastMode) {
        card.classList.add("u-launcher-card-recent");
        card.appendChild(el("span", { text: "上次使用", class: "u-chip" }));
      }
      
      card.addEventListener("click", () => this.launch(app.mode));
      dialog.appendChild(card);
    }

    this.overlay.appendChild(dialog);
    document.body.appendChild(this.overlay);
  }

  /** 启动选中的应用 */
  private async launch(mode: AppMode): Promise<void> {
    await this.ctx.setPrefs({ app_mode: mode, app_launched_at: Date.now() });
    
    // 淡出动画
    if (this.overlay) {
      this.overlay.style.opacity = "0";
      await new Promise(resolve => setTimeout(resolve, 200));
      this.overlay.remove();
    }
    
    // 根据模式路由到对应页面
    if (mode === "chat") {
      this.ctx.navigate({ pane: "contact" });
    } else if (mode === "writer") {
      this.ctx.navigate({ pane: "writing" });
    } else if (mode === "gm") {
      this.ctx.navigate({ pane: "trpg" });
    }
  }

  /** 判断是否 5 秒内重启(避免反复弹窗) */
  private isRecentLaunch(): boolean {
    const last = Number(this.ctx.prefs.app_launched_at ?? 0);
    return Date.now() - last < 5000;
  }
}
