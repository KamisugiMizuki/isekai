/*
 * 首页（USER_INTERFACE_DESIGN §4）：最近使用、未完成草稿、三个任务入口。
 *
 * 无数据时只表达三件事：软件能做什么、当前还缺什么、推荐下一步。
 * 已有数据时优先「继续上次」，不编造动态摘要，也不建全局剧情看板。
 */

import type { AppContext, Pane } from "./app";
import { openDir } from "./app";
import type { Json } from "./api";
import { bulletList, button, el, facts, fill, paragraph, primary, section, stamp } from "./dom";

interface Recent {
  pane: string;
  label: string;
  at: number;
  key: string;
}

const TASKS: Array<{ pane: "contact" | "writing" | "trpg"; title: string; body: string }> = [
  { pane: "contact", title: "💬 与角色联络", body: "选一个角色,和她对话" },
  { pane: "writing", title: "✍️ 辅助写作", body: "整理大纲,保存文字草稿" },
  { pane: "trpg", title: "🎲 进行跑团", body: "声明行动,得到裁定" },
];

export class HomePane implements Pane {
  readonly id = "home" as const;

  constructor(private readonly ctx: AppContext) {}

  async mount(host: HTMLElement): Promise<void> {
    const readiness = this.ctx.readiness ?? {};
    const firstRun = ((readiness.first_run as Json) ?? {}) as Json;
    const ai = ((readiness.ai as Json) ?? {}) as Json;
    const instances = this.ctx.instances();
    const recent = ((this.ctx.prefs.recent as Recent[]) ?? []).slice(0, 5);
    const drafts = await this.draftList();

    const page = el("div", { class: "u-page" });
    page.appendChild(el("h2", { class: "u-h2", text: "从这里开始" }));

    if (!ai.configured) {
      page.appendChild(
        section(
          "第一步:连接 AI",
          paragraph("这个程序调用你提供的 AI 服务。密钥只保存在本机。"),
          el(
            "div",
            { class: "u-row" },
            primary("现在配置", () => this.ctx.navigate({ pane: "settings", sub: "ai" })),
            button("先看看样例,稍后再配置", () => this.ctx.navigate({ pane: "onboarding", sub: "sample" })),
          ),
        ),
      );
    }

    if (!Number(firstRun.instances ?? 0)) {
      page.appendChild(
        section(
          "推荐下一步",
          paragraph("用随程序附带的样例世界走一遍。选一位角色,创建后就能开始联络。"),
          el(
            "div",
            { class: "u-row" },
            primary("从样例世界开始", () => this.ctx.navigate({ pane: "onboarding", sub: "sample" })),
            button("创建自己的世界", () => this.ctx.navigate({ pane: "create" })),
            button("导入已有内容", () => this.ctx.navigate({ pane: "worlds", sub: "import" })),
          ),
        ),
      );
    } else {
      const last = recent[0];
      const target = last && last.pane === "contact" ? last : null;
      page.appendChild(
        section(
          "继续上次",
          paragraph(
            target ? `${target.label}` : "上次打开的是一个世界：从这里回到它的角色列表或写作/跑团入口。",
            "u-p u-strong",
          ),
          el("div", { class: "u-row" }, primary("继续", () => this.resume(last))),
        ),
      );
    }

    page.appendChild(this.taskCards());

    if (recent.length) {
      page.appendChild(
        section(
          "最近使用",
          bulletList(
            recent.map((item) =>
              button(`${item.label}（${stamp(item.at)}）`, () => this.ctx.navigate({ pane: item.pane as never }), {
                class: "u-btn u-ghost",
              }),
            ),
            "u-list u-list-plain",
          ),
        ),
      );
    }

    if (drafts.length) {
      page.appendChild(
        section(
          "未完成内容",
          bulletList(
            drafts.map((item) =>
              button(
                `${draftLabel(item)}（${stamp(Number(item.updated_at ?? 0))}）`,
                () => this.ctx.navigate({ pane: draftPane(String(item.module ?? "")) }),
                { class: "u-btn u-ghost" },
              ),
            ),
            "u-list u-list-plain",
          ),
        ),
      );
    }

    page.appendChild(
      section(
        "本机状态",
        facts([
          ["世界", `${Number(firstRun.instances ?? 0)} 个实例 / ${Number(firstRun.packages ?? 0)} 份设定`],
          ["AI 服务", ai.configured ? `${String(ai.model)}` : "还没有配置"],
        ]),
        el(
          "div",
          { class: "u-row" },
          button("打开数据位置", () => void openDir("data", this.ctx.api)),
          button("帮助与诊断", () => this.ctx.navigate({ pane: "help" })),
        ),
      ),
    );

    fill(host, page);
    void instances;
  }

  private taskCards(): HTMLElement {
    const grid = el("div", { class: "u-cards" });
    for (const task of TASKS) {
      const card = el("article", { class: "u-card" });
      card.appendChild(el("h3", { text: task.title }));
      card.appendChild(paragraph(task.body));
      const ready = this.taskReady(task.pane);
      card.appendChild(el("p", { class: "u-hint", text: ready.note }));
      card.appendChild(primary(ready.open ? `打开${task.title}` : "准备材料", () => this.ctx.navigate({ pane: task.pane })));
      grid.appendChild(card);
    }
    return section("你可以做的事", grid);
  }

  private taskReady(pane: "contact" | "writing" | "trpg"): { open: boolean; note: string } {
    const readiness = this.ctx.readiness ?? {};
    const firstRun = ((readiness.first_run as Json) ?? {}) as Json;
    const ai = ((readiness.ai as Json) ?? {}) as Json;
    const instances = Number(firstRun.instances ?? 0);
    if (pane === "contact") {
      if (!instances) return { open: false, note: "还缺：一个已创建的世界（可以从样例开始）" };
      if (!ai.configured) return { open: true, note: "还缺：可用 AI 配置；阅读本机已有记录不受影响" };
      return { open: true, note: "已就绪" };
    }
    if (pane === "writing") {
      if (!instances) return { open: false, note: "还缺：世界与观察角色" };
      return { open: true, note: ai.configured ? "已就绪" : "需要 AI 建议时先配置 AI" };
    }
    if (!instances) return { open: false, note: "还缺：世界、角色与规则绑定" };
    return { open: true, note: "先覆盖已验证的样例组合" };
  }

  private async draftList(): Promise<Json[]> {
    try {
      const result = await this.ctx.api.draftList();
      return ((result.drafts as Json[]) ?? []).filter((item) => String(item.text ?? "").length > 0);
    } catch {
      return [];
    }
  }

  private resume(last?: Recent): void {
    if (!last) {
      this.ctx.navigate({ pane: "worlds" });
      return;
    }
    this.ctx.navigate({ pane: last.pane as never });
  }
}

function draftLabel(item: Json): string {
  const target = String(item.target ?? "");
  const name = target.split(":")[0] || "草稿";
  const module = String(item.module ?? "");
  const kind = module === "contact" ? "未发送的联络" : module === "world" ? "世界设定编辑" : module === "writing" ? "写作草稿" : "草稿";
  return `${kind} · ${name.slice(0, 12)}`;
}

function draftPane(module: string): "contact" | "writing" | "worlds" {
  if (module === "writing") return "writing";
  if (module === "world") return "worlds";
  return "contact";
}
