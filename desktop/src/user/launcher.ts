/*
 * 方案 B 原型:应用启动选择器
 * 
 * 启动时弹窗选择 Chat/Writer/GM 之一,选择后整个窗口只为该应用服务。
 * 
 * ponytail: 原型用 CSS 弹窗,正式版改 Tauri native dialog
 */

import type { AppContext, Route } from "./app";
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
  // 标题用中文（与首页/顶栏同一套名字），英文名留在副标题里——两套叫法指同一件事时，
  // 用户至少要能对上号；「裁定」这类行话也换成能猜到的说法。
  { mode: "chat", icon: "💬", title: "角色联络", subtitle: "isekai Chat · 选一个角色和她对话" },
  { mode: "writer", icon: "✍️", title: "辅助写作", subtitle: "isekai Writer · 整理大纲、保存文字草稿" },
  { mode: "gm", icon: "🎲", title: "跑团", subtitle: "isekai GM · 声明行动，得到结果" },
];

/** 三个模式各自的落点（与 `app.ts` 的 appPane 一致；这里单独放一份避免 app ⇄ launcher 循环运行引用） */
const MODE_PANE: Record<AppMode, "contact" | "writing" | "trpg"> = {
  chat: "contact",
  writer: "writing",
  gm: "trpg",
};

export class Launcher {
  private overlay: HTMLElement | null = null;
  /** 「可退出」形态的上一个落点：从应用里点「返回选择应用」时为当前路由；首启为 null（三选一不变） */
  private previous: Route | null = null;
  /** Esc 退路只在「可退出」形态挂着，关闭时必须摘掉（否则会留下指向旧路由的一次性监听） */
  private escHandler: ((event: KeyboardEvent) => void) | null = null;

  constructor(private readonly ctx: AppContext) {}

  /**
   * 显示应用选择器。
   * `auto` = 启动时的自动弹窗：刚选过应用就直接进去，不重复问（快速重启场景）。
   * 用户自己点「返回选择应用」走的是 `auto=false`——那是明确意图，不能被「刚选过」吞掉。
   *
   * 从应用里返回时（`auto=false` 且已经选过应用），`ctx.route` 就是上一个路由：
   * 这时浮层可退出（Esc / 点空白回到那里）。首位启动没有上一个应用路由，保持「必须选一个」。
   */
  show(auto = false): void {
    const lastMode = this.ctx.prefs.app_mode as AppMode | undefined;

    if (auto && lastMode && this.isRecentLaunch()) {
      this.launch(lastMode);
      return;
    }

    // 首次设置走完之后，这一屏永远留一条退路（Esc / 点空白回刚才的页面）：
    // 只有「第一次打开、什么都还没建」时才必须先选一个。
    const onboardDone = Boolean(this.ctx.prefs["onboard.done"]);
    this.previous = lastMode || onboardDone ? this.ctx.route : null;

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

    if (this.previous) {
      // 有退路就说出来，别让「浮层只能三选一」的旧印象把人困住（审计 Q2④#7）
      dialog.appendChild(el("p", { class: "u-note", text: "按 Esc 或点击空白处返回刚才的页面" }));
      this.overlay.addEventListener("click", (event) => {
        if (event.target === this.overlay) this.dismiss();
      });
      this.escHandler = (event: KeyboardEvent) => {
        if (event.key === "Escape") this.dismiss();
      };
      document.addEventListener("keydown", this.escHandler);
    }

    this.overlay.appendChild(dialog);
    document.body.appendChild(this.overlay);
  }

  /** 摘掉监听并移除浮层（不导航、不启动） */
  private close(): void {
    if (this.escHandler) {
      document.removeEventListener("keydown", this.escHandler);
      this.escHandler = null;
    }
    this.overlay?.remove();
    this.overlay = null;
  }

  /** 「可退出」形态的退路：回到上一个路由；不改 `app_mode`，也不写「最近使用」 */
  private dismiss(): void {
    const previous = this.previous;
    this.close();
    if (previous) this.ctx.navigate(previous);
  }

  /** 启动选中的应用 */
  private async launch(mode: AppMode): Promise<void> {
    const app = APPS.find((item) => item.mode === mode);
    await this.ctx.setPrefs({ app_mode: mode, app_launched_at: Date.now() });
    // §3.4 最近使用：应用名当 label，key 只认模式（同一应用只留一条）
    this.ctx.rememberRecent({ pane: MODE_PANE[mode], label: app?.title ?? mode, key: `launch:${mode}` });
    
    // 淡出动画
    const overlay = this.overlay;
    if (overlay) {
      overlay.style.opacity = "0";
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    this.close();
    
    // 根据模式路由到对应页面
    this.ctx.navigate({ pane: MODE_PANE[mode] });
  }

  /** 判断是否 5 秒内重启(避免反复弹窗) */
  private isRecentLaunch(): boolean {
    const last = Number(this.ctx.prefs.app_launched_at ?? 0);
    return Date.now() - last < 5000;
  }
}
