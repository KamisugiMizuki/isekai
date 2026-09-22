/*
 * 辅助写作 / 跑团两个工作区的当前落地页。
 *
 * 这两条路径的界面按实施顺序在 U3（写作）与 U4（跑团）交付：内核语义与命令行已经可用，
 * 但界面还不消费它们。按 §1「正式入口没有完成时不摆可点击的空壳占位」的要求，
 * 这里只说清现在能做什么、缺什么，不摆假按钮、不假装已实现。
 */

import type { AppContext, Pane } from "./app";
import type { Json } from "./api";
import { bulletList, button, el, facts, fill, paragraph, primary, section } from "./dom";

export class WritingPane implements Pane {
  readonly id: "writing" | "trpg";

  constructor(private readonly ctx: AppContext, id: "writing" | "trpg" = "writing") {
    this.id = id;
  }

  async mount(host: HTMLElement): Promise<void> {
    const writing = this.id === "writing";
    const readiness = this.ctx.readiness ?? {};
    const firstRun = ((readiness.first_run as Json) ?? {}) as Json;
    const ai = ((readiness.ai as Json) ?? {}) as Json;
    const instances = this.ctx.instances();
    const page = el("div", { class: "u-page" });
    page.appendChild(el("h2", { class: "u-h2", text: writing ? "辅助写作" : "跑团" }));
    page.appendChild(
      section(
        "这条路径还在准备中",
        paragraph(
          writing
            ? "一起组织大纲、观察人物、比较推进方案，文字由你决定——这套工作区正在实现中。"
            : "选规则与角色，声明行动，确认后得到裁定与后果——这套工作区正在实现中。",
        ),
        facts([
          ["世界与角色", instances.length ? `${instances.length} 个世界可用` : "还没有世界（可以先从样例开始）"],
          ["AI 服务", ai.configured ? "已配置" : "还没有可用配置"],
          ["素材", `${Number(firstRun.packages ?? 0)} 份世界设定 / ${Number(firstRun.instances ?? 0)} 个世界`],
        ]),
        bulletList(
          writing
            ? [
                "现在可以做的：整理世界与角色素材、和角色联络、管理时间线与版本。",
                "还没做的：大纲编辑工作区、观察素材、推进建议、文字草稿与导出。",
                "已落地的内核能力（命令行可用）：大纲保存、观察、候选提出与决定、世界变化预览与提交、分支试演。",
              ]
            : [
                "现在可以做的：整理世界与角色素材、和角色联络、管理时间线与版本。",
                "还没做的：战役列表与创建、场景与行动确认卡、玩家 / 主持视图、规则登记。",
                "已落地的内核能力（命令行可用）：战役创建、场景、行动声明与裁定、待选择、联合提交、规则版本迁移。",
              ],
          "u-list",
        ),
        el(
          "div",
          { class: "u-row" },
          primary("去角色联络", () => this.ctx.navigate({ pane: "contact" })),
          button("世界与素材", () => this.ctx.navigate({ pane: "worlds" })),
          writing ? null : button("看帮助与诊断", () => this.ctx.navigate({ pane: "help" })),
        ),
      ),
    );
    fill(host, page);
  }
}
