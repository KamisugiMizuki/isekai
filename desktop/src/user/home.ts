/*
 * 首页（USER_INTERFACE_DESIGN §4）：一个明确的「下一步」，加上可以直接开始的三件事。
 *
 * 无数据时只表达三件事：当前还缺什么、推荐下一步、推荐之外还可以做什么。
 * 已有数据时优先「继续上次」，不编造动态摘要，也不建全局剧情看板。
 *
 * 2026-10-08 视觉体系审查（docs/user-interface/UI_VISUAL_SYSTEM_REVIEW_2026-10-08.md）：
 * 这一页原来是三个同等重量的盒子（第一步：连接 AI / 继续上次 / 你可以做的事），眼睛找不到主次；
 * 「继续上次」用一行字占了一整个盒子；任务卡又是「绿点 + 还缺…」两个信号打架。现在改成：
 * 一个主卡（按真实状态选出的**唯一**推荐动作，其余入口降级成链接）+ 一个紧凑的
 * 「继续上次 / 最近使用」 + 三张任务磁贴（就绪情况画成一段条）。
 */

import type { AppContext, Pane } from "./app";
import { openDir } from "./app";
import { uiError, type InstanceEntry, type Json } from "./api";
import {
  bulletList,
  button,
  el,
  errorCard,
  facts,
  fill,
  link,
  pageHead,
  panel,
  paragraph,
  primary,
  section,
  stamp,
} from "./dom";
import { dotLine, stackBar } from "./graphics";

interface Recent {
  pane: string;
  label: string;
  at: number;
  key: string;
}

/** 一条就绪检查项：首页只画它真读得到的读数 */
interface ReadyItem {
  label: string;
  done: boolean;
  /** 缺这一项时写进那句话的说法（比标号更能照做） */
  hint?: string;
  /** 缺这一项不算拦路（例如写作的 AI 建议）：决定状态色是「提醒」还是「告警」 */
  soft?: boolean;
}

/** 一张任务磁贴的就绪结论：进度项 + 一句话 + 与这句话一致的状态色 */
interface Ready {
  open: boolean;
  items: ReadyItem[];
  note: string;
  tone: "ok" | "pending" | "muted";
}

/** 未完成内容的读取结果：真没有与读取失败必须分开（失败不假装没有） */
type DraftRead = { ok: true; drafts: Json[] } | { ok: false; error: unknown };

const TASKS: Array<{ pane: "contact" | "writing" | "trpg"; title: string; body: string; open: string }> = [
  // 标题里不再嵌 emoji：图标被写进动作名会渲染成「打开💬 与角色联络」，像渲染故障。
  // 按钮文案单独给一份不带图标的名字，替换旧代码 `打开${title}` 的拼接。
  { pane: "contact", title: "与角色联络", body: "选一个角色，和她对话", open: "打开角色联络" },
  { pane: "writing", title: "辅助写作", body: "整理大纲，保存文字草稿", open: "打开辅助写作" },
  { pane: "trpg", title: "进行跑团", body: "声明行动，得到结果", open: "打开跑团" },
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
    // 页面骨架：标题带说明这一页负责回答什么（缺什么、下一步做什么）
    page.appendChild(pageHead("从这里开始", "缺什么、下一步做什么，都在这里"));

    // 顺序即主次：唯一的主卡在最上，其余都是它下面的补充材料
    page.appendChild(this.nextStep(host, ai, instances, recent));
    const recentBlock = this.recentBlock(recent);
    if (recentBlock) page.appendChild(recentBlock);
    page.appendChild(this.taskCards());
    page.appendChild(this.draftBlock(host, drafts));
    page.appendChild(this.machineBlock(host, firstRun, ai));

    fill(host, page);
  }

  /**
   * 唯一的主卡：按真实读数挑出此刻最该做的那一件事，其余入口都降级成这条下面的链接。
   *
   * 为什么只给一个：审查说三个同等重量的盒子让眼睛找不到主次。主次不是靠加粗得来的，
   * 是靠「只有一个」——其余入口做链接，用户扫一眼就知道先做哪件。
   */
  private nextStep(host: HTMLElement, ai: Json, instances: InstanceEntry[], recent: Recent[]): HTMLElement {
    const card = panel("推荐下一步");
    const world = this.hasWorld();
    // 读不到世界列表：这是「读不到」，不是「没有世界」。给重试，不劝人从样例再装一遍
    if (this.ctx.instancesError && !instances.length) {
      card.appendChild(paragraph("没能读出这台电脑上有哪些世界，所以现在不能替你决定下一步。"));
      card.appendChild(
        errorCard(uiError(new Error(this.ctx.instancesError), { module: "首页", action: "读取世界列表" }), [
          { label: "重试读取", run: () => void this.ctx.reloadReadings().then(() => this.mount(host)) },
        ]),
      );
      return card;
    }

    const aiReady = Boolean(ai.configured);
    const actions = el("div", { class: "u-row" });
    let headline = "";
    let detail = "";
    let note = "";
    let tone: Ready["tone"] = "pending";
    // 未配置 AI 时把那一步的原话说清楚（含新手最常问的「密钥放哪」）
    let explainAi = false;

    if (this.ctx.readinessError) {
      // 读数失败 ≠ 一切都没就绪：不说「还缺」，只给「重试读取」（与任务磁贴同一条纪律）
      headline = "先把这台电脑的状态读出来";
      detail = "本机状态读失败，所以现在既不能确定你缺什么，也不能替你决定下一步。";
      note = "本机状态：读取失败（不是「没有」）";
      tone = "muted";
      actions.appendChild(primary("重试读取", () => void this.ctx.reloadReadings().then(() => this.mount(host))));
      if (world) actions.appendChild(link("打开角色联络", () => this.ctx.navigate({ pane: "contact" })));
    } else if (!world) {
      headline = "从样例世界开始";
      detail = "用随程序附带的样例世界走一遍：选一位角色，创建后就能开始联络。";
      note = aiReady ? "还缺：一个世界" : "还缺：一个世界、AI 配置";
      tone = "pending";
      explainAi = !aiReady;
      actions.appendChild(primary("从样例世界开始", () => this.ctx.navigate({ pane: "onboarding", sub: "sample" })));
      actions.appendChild(link("创建自己的世界", () => this.ctx.navigate({ pane: "create" })));
      actions.appendChild(link("导入已有内容", () => this.ctx.navigate({ pane: "worlds", sub: "import" })));
    } else if (!aiReady) {
      headline = "现在配置 AI 服务";
      detail = "世界已经有了；联络与写作都要调用你提供的 AI 服务，填好地址与密钥就能开始。";
      note = "还缺：AI 配置；看已有记录不受影响";
      tone = "pending";
      explainAi = true;
      actions.appendChild(primary("现在配置", () => this.ctx.navigate({ pane: "settings", sub: "ai" })));
      actions.appendChild(link("打开角色联络", () => this.ctx.navigate({ pane: "contact" })));
      actions.appendChild(link("世界与素材", () => this.ctx.navigate({ pane: "worlds" })));
    } else {
      const last = recent[0];
      headline = last ? `继续上次：${last.label}` : "打开角色联络";
      detail = last ? "接着上次的地方继续；也可以直接从下面的任务开始。" : "世界和 AI 都就绪了：选一个角色开始联络。";
      note = "世界与 AI 都就绪";
      tone = "ok";
      actions.appendChild(
        primary(last ? "继续" : "打开角色联络", () =>
          last ? this.resume(last) : this.ctx.navigate({ pane: "contact" }),
        ),
      );
      actions.appendChild(link("世界与素材", () => this.ctx.navigate({ pane: "worlds" })));
    }

    card.appendChild(el("p", { class: "u-p u-strong", text: headline }));
    card.appendChild(paragraph(detail));
    // 状态点跟着这句话走：写「还缺…」就不给绿点（审查实锤的颜色与文字打架）
    card.appendChild(dotLine(note, tone));
    if (explainAi) {
      card.appendChild(
        el("p", { class: "u-hint", text: "第一步：连接 AI：这个程序调用你提供的 AI 服务，密钥只保存在本机。" }),
      );
    }
    card.appendChild(actions);
    return card;
  }

  /**
   * 「继续上次」与「最近使用」是同一件事的两种说法：合成一个紧凑 panel。
   *
   * 以前它们是两个分区，其中「继续上次」用一行字占了一整个盒子（审查点名的浪费）。
   * 现在只有一条列表：第一条就是主卡「继续」会打开的那一个。
   */
  private recentBlock(recent: Recent[]): HTMLElement | null {
    if (!recent.length) return null;
    const items = recent.map((item) =>
      button(`${item.label}（${stamp(item.at)}）`, () => this.ctx.navigate({ pane: item.pane as never }), {
        class: "u-btn u-ghost",
      }),
    );
    return panel(
      "继续上次",
      el("p", { class: "u-hint", text: "最近使用" }),
      bulletList(items, "u-list u-list-plain"),
    );
  }

  /**
   * 未完成内容三态：读到列表 / 确实没有 / 读取失败。
   * 失败给错误卡 + 重试，不显示「暂无」也不假装没有；真空态只用一行字（一行字不占一个盒子）。
   */
  private draftBlock(host: HTMLElement, drafts: DraftRead): HTMLElement {
    if (!drafts.ok) {
      return section(
        "未完成内容",
        errorCard(uiError(drafts.error, { module: "首页", action: "读取未完成内容" }), [
          { label: "重试读取", run: () => void this.mount(host) },
        ]),
      );
    }
    if (!drafts.drafts.length) {
      return el("p", { class: "u-hint", text: "未完成内容：没有。写到一半的内容会自动出现在这里。" });
    }
    const items = drafts.drafts.map((item) =>
      button(`${draftLabel(item)}（${stamp(Number(item.updated_at ?? 0))}）`, () => this.openDraftItem(item), {
        class: "u-btn u-ghost",
      }),
    );
    return section("未完成内容", bulletList(items, "u-list u-list-plain"));
  }

  /** 本机状态：读数与出口放在一起（读失败时错误卡就长在这块里） */
  private machineBlock(host: HTMLElement, firstRun: Json, ai: Json): HTMLElement {
    return panel(
      "本机状态",
      facts([
        ["世界", `${Number(firstRun.instances ?? 0)} 个世界 / ${Number(firstRun.packages ?? 0)} 份设定`],
        ["AI 服务", ai.configured ? `${String(ai.model)}` : "还没有配置"],
      ]),
      this.ctx.readinessError
        ? errorCard(uiError(new Error(this.ctx.readinessError), { module: "首页", action: "读取本机状态" }), [
            { label: "重试读取", run: () => void this.ctx.reloadReadings().then(() => this.mount(host)) },
          ])
        : null,
      el(
        "div",
        { class: "u-row" },
        button("打开数据位置", () => void openDir("data", this.ctx.api)),
        button("帮助与诊断", () => this.ctx.navigate({ pane: "help" })),
      ),
    );
  }

  /** 有没有世界：世界列表读数优先，读不到时退回本机状态的计数（两处都不猜「没有」） */
  private hasWorld(): boolean {
    if (this.ctx.instances().length) return true;
    const firstRun = (((this.ctx.readiness ?? {}).first_run as Json) ?? {}) as Json;
    return Number(firstRun.instances ?? 0) > 0;
  }

  private taskCards(): HTMLElement {
    const grid = el("div", { class: "u-cards" });
    for (const task of TASKS) {
      const card = el("article", { class: "u-card" });
      card.appendChild(el("h3", { text: task.title }));
      card.appendChild(paragraph(task.body, "u-hint"));
      const ready = this.taskReady(task.pane);
      // 就绪表达交给一段条：已成的项各占一段，缺的项没有宽度——条本身就短一截。
      // 三项都没成时 stackBar 返回 null，这里就不放条（graphics.ts 的纪律：不画空数据）。
      const bar = stackBar(
        ready.items.map((item) => ({ label: item.label, value: item.done ? 1 : 0, tone: "ok" as const })),
        { legend: false },
      );
      if (bar) {
        // 数字图例省掉了（1/0 不是给人读的读数），用 title 把「哪几项已成」说完整：
        // 图只是补充，含义仍由下面那句话与这个 title 承担（graphics.ts 的纪律）
        bar.title = ready.items.map((item) => `${item.label}：${item.done ? "已就绪" : "还缺"}`).join("；");
        card.appendChild(bar);
      }
      card.appendChild(dotLine(ready.note, ready.tone));
      // 磁贴里不再有主按钮：主操作只有一个（在上面那张推荐卡里），这里一律次要按钮
      card.appendChild(
        button(ready.open ? task.open : "准备材料", () => this.ctx.navigate({ pane: task.pane }), {
          class: "u-btn u-ghost",
        }),
      );
      grid.appendChild(card);
    }
    return panel("你可以做的事", grid);
  }

  /**
   * 每张磁贴的就绪情况：进度项 + 一句话 + 与这句话一致的颜色。
   *
   * 首页读得到「世界」「设定份数」「AI 配置」「规则版本」这几个读数，读不到角色卡数量，
   * 所以这里不画「角色卡」那一段——宁可少画一段，也不让图说一句我们没读到的话。
   * 那句话由**缺的那几段**拼出来（不是各写各的），这样条与文字永远同一条结论，
   * 不会再出现审查里那种「绿点 + 还缺…」两个信号打架。
   * 读数失败时不下结论（不然已有世界的用户会被劝去「从样例开始」，重复建一个世界）：
   * 不画条、不说「还缺」，只说「读不到」并指出去哪儿重试。
   */
  private taskReady(pane: "contact" | "writing" | "trpg"): Ready {
    const readiness = this.ctx.readiness ?? {};
    const firstRun = ((readiness.first_run as Json) ?? {}) as Json;
    const ai = ((readiness.ai as Json) ?? {}) as Json;
    const world = this.hasWorld();
    if (this.ctx.readinessError) {
      return { open: world, items: [], note: "本机状态没读到：先在下面「本机状态」里重试读取", tone: "muted" };
    }
    const aiReady = Boolean(ai.configured);
    let items: ReadyItem[];
    let readyNote = "已就绪";
    if (pane === "contact") {
      items = [
        { label: "世界", done: world, hint: "一个已创建的世界（可以从样例开始）" },
        { label: "AI 配置", done: aiReady, hint: "可用的 AI 配置（看已有记录不受影响）" },
      ];
    } else if (pane === "writing") {
      items = [{ label: "世界", done: world, hint: "一个世界和一个要观察的角色" }];
      // 没有世界时「设定材料」这一段没有意义：不画它，免得同一件缺事被说两遍
      if (world) {
        items.push({
          label: "设定材料",
          done: Number(firstRun.packages ?? 0) > 0,
          hint: "可用的设定材料（在「世界与素材」里准备）",
        });
      }
      // 没配 AI 不拦路（素材还能整理），所以这一项是「提醒」不是「告警」
      items.push({ label: "AI 配置", done: aiReady, hint: "AI 配置（要写建议时才需要）", soft: true });
    } else {
      items = [
        { label: "世界", done: world, hint: "一个世界（规则可以先用随程序附带的样例）" },
        { label: "跑团规则", done: Boolean(readiness.rules), hint: "一套跑团规则（可以先在跑团页里用样例规则）" },
        { label: "AI 配置", done: aiReady, hint: "可用的 AI 配置" },
      ];
      readyNote = "可以先用随程序附带的样例规则跑一局";
    }
    const missing = items.filter((item) => !item.done);
    if (!missing.length) return { open: true, items, note: readyNote, tone: "ok" };
    return {
      // 只有世界都没有时按钮才降级成「准备材料」；其余情况页面自己会说清缺什么
      open: world,
      items,
      note: `还缺：${missing.map((item) => item.hint ?? item.label).join("；")}`,
      // 缺的都是「提醒」那一类就不给告警色：色与话仍然一致（审查点名的打架）
      tone: missing.every((item) => item.soft) ? "muted" : "pending",
    };
  }

  /** 未完成内容入口：create 草稿要带着 key 回创建向导读回；其余按模块进对应页 */
  private openDraftItem(item: Json): void {
    const module = String(item.module ?? "");
    if (module === "create") {
      this.ctx.navigate({ pane: "create", sub: `draft:${String(item.key ?? "")}` });
      return;
    }
    this.ctx.navigate({ pane: draftPane(module) });
  }

  /**
   * 未完成内容列表：把「真的没有」与「读取失败」分开（审计 Q2④#5）。
   * 失败时把错误原样交给调用方渲染错误卡，不再吞成空数组。
   */
  private async draftList(): Promise<DraftRead> {
    try {
      const result = await this.ctx.api.draftList();
      return {
        ok: true,
        drafts: ((result.drafts as Json[]) ?? []).filter((item) => String(item.text ?? "").length > 0),
      };
    } catch (error) {
      return { ok: false, error };
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
  const kind = module === "contact" ? "未发送的联络" : module === "world" ? "世界设定编辑" : module === "create" ? "新世界设定" : module === "writing" ? "写作草稿" : "草稿";
  return `${kind} · ${name.slice(0, 12)}`;
}

function draftPane(module: string): "contact" | "writing" | "worlds" {
  if (module === "writing") return "writing";
  if (module === "world") return "worlds";
  return "contact";
}
