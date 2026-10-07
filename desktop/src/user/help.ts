/*
 * 帮助与诊断（ONBOARDING_AND_RECOVERY §8）。核心未就绪时也能打开：
 * 首屏按当前问题给出「应用是否启动、数据是否可写、AI 是否验证过、世界线状态」，
 * 没测过就写「未检查」，不凭字段非空报正常。
 *
 * 2026-10-07 普通用户可用性评审（P1-2 / §七）：这一页过去的「常见问题」是内部状态短语
 * （「结果待确认 —— 用原操作身份查询结果，不要重复提交」），用户既问不出那句话，也无从照做。
 * 现在每条都是用户的原话，并给一句能照做的动作（能点的动作直接给按钮）。
 *
 * 2026-10-08 视觉体系审查（§三-6 / D）：顺序倒过来——常见问答（磁贴）在最上，
 * 「现在的状态」在中间，本机检查明细 / 位置 / 版本收进默认关闭的折叠块。
 * 过去第一屏是绝对路径与「记录格式 0.1」这类排错读数，新手要的问答在第 4 屏。
 */

import type { AppContext, Pane } from "./app";
import { openDir } from "./app";
import { PRESET_SERVICES, openExternal } from "./ai-setup";
import type { Json } from "./api";
import { uiError } from "./api";
import { button, el, facts, fill, pageHead, panel, paragraph, primary, section, setNote, stamp } from "./dom";
import { checkList } from "./graphics";

/**
 * 核心给的本机检查标签里还留着内部叫法（评审第六节替换表）：上屏前换成用户词表里的说法。
 * 没列到的照原样显示，不猜。
 */
const CHECK_LABELS: Record<string, string> = {
  受管目录可写: "数据文件夹可写",
  单一写入者: "只由这个程序写入",
  数据格式可读: "已有的内容能读",
};

function checkLabel(label: unknown): string {
  const text = String(label ?? "");
  return CHECK_LABELS[text] ?? text;
}

/** 后台服务状态 → 人话：raw 值（persistence_blocked 这类）不上屏（评审 §四-4，同 app.ts 的 coreStatusText） */
function coreStateText(state: unknown): string {
  switch (String(state ?? "")) {
    case "ready":
      return "运行中";
    case "starting":
      return "启动中";
    case "compatibility_blocked":
      return "版本不兼容，已停住";
    case "persistence_blocked":
      return "存储不可写，已停住";
    default:
      return "没有读到状态（可以点「重新检查本机」）";
  }
}

/** 世界线状态 → 人话；世界内时间用核心给的历法说法，不印无单位的大数字 */
function timelineStateText(state: unknown): string {
  switch (String(state ?? "")) {
    case "active":
      return "运行中";
    case "frozen":
      return "已暂停";
    case "inconsistent":
      return "状态不一致（建议到世界与素材里查看）";
    default:
      return "状态尚未读到";
  }
}

export class HelpPane implements Pane {
  readonly id = "help" as const;

  constructor(private readonly ctx: AppContext) {}

  async mount(host: HTMLElement): Promise<void> {
    await this.ctx.refresh();
    const readiness = this.ctx.readiness ?? {};
    const checks = (readiness.checks as Json[]) ?? [];
    const ai = ((readiness.ai as Json) ?? {}) as Json;
    const paths = ((readiness.paths as Json) ?? {}) as Json;
    const page = el("div", { class: "u-page" });
    // ① 标题带：标题位置与「这一页干什么用」在全站统一（视觉体系审查 A）
    page.appendChild(pageHead("帮助与诊断", "先看下面的问答；本机详情在最下面"));

    // ② 问答在最上：过去第一屏是「现在的状态」与本机读数，用户真正要的问答在第 4 屏
    //    （审查 §三-6）——点开「帮助」的人要的是「怎么办」，不是诊断读数。
    page.appendChild(this.faqSection());

    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    page.appendChild(
      section(
        "现在的状态",
        facts([
          // 版本号只在真读到的时候才跟在后面：读不到就别写成「没有读到状态（版本 —）」
          [
            "后台服务",
            readiness.app ? `${coreStateText(readiness.state)}（版本 ${String(readiness.app)}）` : coreStateText(readiness.state),
          ],
          ["数据文件夹可写", checks.find((item) => item.key === "data")?.ok ? "通过" : "需要处理"],
          ["AI 配置", ai.configured ? "已填写（是否验证过见下）" : "还没有可用配置"],
          ["最近一次连接测试", String(this.ctx.prefs["ai.tested_at"] ? stamp(Number(this.ctx.prefs["ai.tested_at"])) : "未检查")],
          ["世界线状态", await this.timelineStatus()],
        ]),
        el(
          "div",
          { class: "u-row" },
          // 主按钮留给普通用户真的会点的动作；「进入高级调试」是排错入口，见下面折叠块（评审 P1-3）
          primary("复制诊断信息", () => void copyDiagnostics(this.ctx, note)),
          button("去设置里测试 AI 连接", () => this.ctx.navigate({ pane: "settings", sub: "ai" })),
          button("重新检查本机", () => {
            void (async () => {
              await this.ctx.refresh();
              note.textContent = "已重新检查本机状态（不会调用外部 AI）";
            })();
          }),
        ),
        note,
      ),
    );

    // ③ 技术详情（默认折叠）：检查明细 / 位置与日志 / 版本三块收进同一个 details。
    //    审查 §三-6 的问题不是这些读数不该存在，而是它们不该占第一屏——绝对路径、「记录格式 0.1」、
    //    「唯一写入者」对新手只是噪音，对排错的人才是证据。
    page.appendChild(
      section(
        "技术详情",
        el(
          "details",
          { class: "u-advanced" },
          el("summary", { text: "本机详情（排错用）" }),
          el("h4", { class: "u-sub", text: "本机检查明细" }),
          checkList(
            checks.map((item) => ({
              label: checkLabel(item.label),
              ok: Boolean(item.ok),
              detail: String(item.detail ?? ""),
            })),
          ),
          paragraph("路径、版本与「只由这个程序写入」这类读数都写在这里与「复制诊断信息」里，日常不用看。", "u-hint"),
          el("h4", { class: "u-sub", text: "位置与日志" }),
          facts([
            ["数据文件夹", String(paths.data ?? "—")],
            ["配置文件", String(paths.config ?? "—")],
            ["创作文件夹", String(paths.packages ?? "—")],
            ["备份文件夹", String(paths.backups ?? "—")],
          ]),
          el(
            "div",
            { class: "u-row" },
            button("打开日志文件夹", () => void openDir("logs", this.ctx.api)),
            button("打开数据文件夹", () => void openDir("data", this.ctx.api)),
            button("打开创作文件夹", () => void openDir("packages", this.ctx.api)),
          ),
          paragraph("原始日志在本机打开；「复制诊断信息」按允许范围构造，不含密钥、聊天正文与世界内部数据。", "u-hint"),
          // 版本号与记录格式属于排错读数：收进折叠区，不让普通用户以为要记住它们（评审第六节替换表）
          el("h4", { class: "u-sub", text: "版本" }),
          facts([
            ["程序版本", String(readiness.app ?? "—")],
            ["数据格式", String(readiness.data_format ?? "—")],
            ["规则版本", String(readiness.rules ?? "—")],
          ]),
        ),
      ),
    );

    // ④ 高级调试保持折叠并放在最后：它面向排错，还会暂停当前工作区的连接，
    //    不该是这一页最显眼的按钮（评审 P1-3）
    page.appendChild(
      section(
        "高级调试",
        el(
          "details",
          { class: "u-advanced" },
          el("summary", { text: "高级调试（排错用，不建议日常使用）" }),
          paragraph(
            "连接、运行与管理验证工具。它面向排错，不作为日常入口，也不提供普通工作区禁止的权限。",
          ),
          el("div", { class: "u-row" }, button("进入高级调试", () => this.ctx.openDebug())),
          paragraph("进入后正式工作区暂停使用这条连接；在调试界面点「回到正式界面」即可返回。", "u-hint"),
        ),
      ),
    );

    fill(host, page);
  }

  /**
   * 常见问题：每一条都是用户的原话，并给一句能照做的动作（评审 §四 P1-2 / §九-8）。
   * 密钥申请、模型名、等待时长、花钱、退出、备份、求助这七条是「开箱必须知道」的底线（评审 §七）。
   *
   * 2026-10-08 视觉体系审查 D：从「一大段条目」改成磁贴网格，并提到第一屏。一条一张磁贴——
   * 问题当标题、动作说明一句、按钮就在这一块里，眼睛有落点，也不必先读完一整页。
   */
  private faqSection(): HTMLElement {
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const notify = (text: string, kind: "ok" | "bad" | "muted"): void => setNote(note, text, kind);
    const preset = PRESET_SERVICES.find((item) => item.key_url) ?? PRESET_SERVICES[0];
    // 磁贴里的动作一律用描边按钮（u-ghost）：一屏摆 8 个实心黑按钮，等于又回到
    // 「全站只有一个视觉重量」那个老问题（审查根因 1）——实心黑只留页面里真正的主操作。
    const act = (label: string, run: () => void): HTMLButtonElement => button(label, run, { class: "u-btn u-ghost" });
    const openKeyPage = act("打开密钥申请页", () => void openExternal(preset.key_url, notify));
    const copyKeyUrl = act("只复制申请页地址", () => {
      void navigator.clipboard
        .writeText(preset.key_url)
        .then(() => notify("申请页地址已复制：粘贴到浏览器地址栏就能打开", "ok"))
        .catch(() => notify(`没能复制，请手动访问：${preset.key_url}`, "bad"));
    });
    const goAi = act("去设置里填写 / 测试", () => this.ctx.navigate({ pane: "settings", sub: "ai" }));
    // 带上 `sub:"data"`：设置拆成真子页之后，只给 pane 会落在偏好记住的那一格，不一定是备份
    const goBackup = act("去设置 → 数据与备份", () => this.ctx.navigate({ pane: "settings", sub: "data" }));
    const goWorlds = act("去世界与素材", () => this.ctx.navigate({ pane: "worlds" }));
    const copyDiag = act("复制诊断信息", () => void copyDiagnostics(this.ctx, note));
    // 与申请页同一个站点：从那里可以进控制台看余额与账单（不另造一个可能失效的网址）
    const openProvider = act("打开服务商网站（可看余额）", () => void openExternal(preset.key_url, notify));
    const tiles: Array<{ q: string; a: string; actions?: HTMLElement[] }> = [
      {
        q: "密钥去哪申请？",
        a: `先去服务商注册账号，再到它的密钥管理页创建：${preset.steps}。网页聊天能登录不等于有密钥——密钥要在控制台里单独创建。`,
        actions: [openKeyPage, copyKeyUrl],
      },
      {
        q: "我该填哪个模型名？",
        a: `预置服务已经把模型名填好了，照抄即可（当前填的是 ${preset.model}）；换别的服务就照它给的说明抄。写错的模型名不会被自动纠正。`,
        actions: [goAi],
      },
      {
        q: "第一次回复为什么要等很久？",
        a: "第一句通常要等 30–120 秒：它先把世界从你上次离开的地方补算到当下，再写完这句回复。这是设计如此，不是卡死——等待时不要重复发送。",
      },
      {
        q: "这要花钱吗？",
        a: "要：每次生成都会占用你在服务商那里的调用量（按用量计费或扣免费额度）。这个界面不做费用面板，余额与账单要到服务商网站看。",
        actions: [openProvider],
      },
      {
        q: "点了窗口的 × 之后程序还在跑吗？怎么彻底退出？",
        a: "× 只是把窗口收进托盘，程序与世界线都还在跑。要彻底退出：右键任务栏托盘里的程序图标 →「退出」；卸载或搬动数据之前先这样退出。",
      },
      {
        q: "怎么备份？怎么把数据搬到别的机器？",
        a: "在设置 →「数据与备份」点「立即备份全部数据」，会生成一份文件；把这份文件拷到新机器，再用同一页的「恢复…」放回去。",
        actions: [goBackup],
      },
      {
        q: "出问题了，把什么发给谁？",
        a: "点「复制诊断信息」，把复制到的内容连同你遇到的情况（在哪一步、看到什么提示）一起发给提供这个程序的人——里面有排错需要的版本与检查结果，但没有你的密钥和聊天内容。",
        actions: [copyDiag],
      },
      {
        q: "提示「密钥无效或没有权限」怎么办？",
        a: "换一个新密钥，或去服务商那里确认这个模型已经开通、额度还够。",
        actions: [openProvider],
      },
    ];
    const grid = el("div", { class: "u-cards" });
    for (const item of tiles) {
      // 一条一张磁贴：panel 无描边、只有一层底色（三级重量里最轻的一级），
      // 这样一屏八块也不会回到「一圈黑边盒子摞盒子」的老样子。
      grid.appendChild(
        panel(
          item.q,
          paragraph(item.a),
          item.actions?.length ? el("div", { class: "u-row" }, ...item.actions) : null,
        ),
      );
    }
    return section(
      "常见问题（点按钮就能做）",
      paragraph("下面是新手最常问的几条：能直接做的，按钮就在那一块里，不必先读完这一页。", "u-hint"),
      grid,
      this.otherFaq(goWorlds, goBackup),
      paragraph("上面没写到的问题：把「复制诊断信息」的结果连同你看到的情况，发给提供这个程序的人。", "u-hint"),
      note,
    );
  }

  /**
   * 剩下几条不占第一屏，但内容照旧在：收进折叠块（审查 D 要的是缩短页面，不是删掉覆盖面）。
   * 每条仍然带自己的按钮——折叠只是「不挡路」，不是「降级成纯文字」。
   */
  private otherFaq(goWorlds: HTMLElement, goBackup: HTMLElement): HTMLElement {
    const items: Array<{ q: string; a: string; action?: HTMLElement }> = [
      {
        q: "世界暂停了，还能继续吗？",
        a: "历史和草稿照常看；要发消息先去「世界与素材」里把这条世界线启动起来。",
        action: goWorlds,
      },
      {
        q: "备份恢复中途失败了怎么办？",
        a: "按提示补做没完成的部分；恢复前的那份副本一直留在备份文件夹里，不会被删掉。",
        action: goBackup,
      },
      {
        q: "界面上那些 tl-1a2b3c、tide.state/1 要记下来吗？",
        a: "不用记也不用抄：它们是程序内部的编号，只在排错时有用。",
      },
    ];
    const box = el("div", { class: "u-faq" });
    for (const item of items) {
      const block = el("div", { class: "u-faq-item" });
      block.appendChild(el("p", { class: "u-faq-q", text: item.q }));
      block.appendChild(paragraph(item.a));
      if (item.action) block.appendChild(el("div", { class: "u-row" }, item.action));
      box.appendChild(block);
    }
    return el(
      "details",
      { class: "u-advanced" },
      el("summary", { text: `其他常见问题（${items.length} 条）` }),
      box,
    );
  }

  private async timelineStatus(): Promise<string> {
    const instances = this.ctx.instances();
    if (!instances.length) return "还没有世界";
    const selection = (this.ctx.prefs["sel.contact"] as Json | undefined) ?? undefined;
    const instance = instances.find((item) => item.id === String(selection?.instance_id ?? "")) ?? instances[0];
    try {
      const info = await this.ctx.api.instanceInfo(instance.id);
      const timelines = (info.timelines as Json[]) ?? [];
      const first = timelines[0];
      if (!first) return `${instance.name}：还没有世界线`;
      const clock = await this.ctx.api.clock(instance.id, String(first.id));
      const view = (clock.clock as Json) ?? {};
      // 世界内时间直接用核心按历法算出的说法（例如「灰潮纪 雾月 3 日 晨」）：
      // 过去这里印「已完成到 129600004」，没有单位，用户只能猜（评审 §四-4）
      const label = String(view.label ?? "").trim();
      const when = label && label !== "已冻结" ? `，世界内时间 ${label}` : "";
      return `${instance.name} / ${String(first.name ?? "")}：${timelineStateText(view.state)}${when}`;
    } catch (error) {
      return `读取失败：${uiError(error, { module: "帮助", action: "读取世界线状态" }).message}`;
    }
  }
}

export async function copyDiagnostics(ctx: AppContext, note: HTMLElement): Promise<void> {
  const readiness = ctx.readiness ?? {};
  const ai = ((readiness.ai as Json) ?? {}) as Json;
  const checks = (readiness.checks as Json[]) ?? [];
  const lines = [
    `isekai 诊断 ${stamp(Date.now() / 1000)}`,
    `程序版本 ${String(readiness.app ?? "-")}｜数据格式 ${String(readiness.data_format ?? "-")}｜规则 ${String(readiness.rules ?? "-")}`,
    // raw 状态值（persistence_blocked 这类）不进这段文本：发给别人看的也该是中文说法（评审 §四-4）
    `后台服务 ${coreStateText(readiness.state)}｜存储可写 ${readiness.storage_ok ? "是" : "否"}`,
    `本机检查：${checks.map((item) => `${checkLabel(item.label)} ${item.ok ? "通过" : "未通过"}`).join("；")}`,
    `AI 服务 ${String(ai.base_url || "未填写")}｜模型 ${String(ai.model || "未填写")}｜密钥 ${ai.api_key_set ? "已设置" : "未设置"}`,
    "遇到的情况：（在这里补一句你在哪一步、看到什么提示）",
    "（未包含密钥、访问令牌、聊天正文、记忆、世界内部数据与提示词）",
  ];
  try {
    await navigator.clipboard.writeText(lines.join("\n"));
    note.textContent = "诊断信息已复制：把它连同你的问题描述，发给提供这个程序的人（可先粘贴检查一遍）";
    note.className = "u-note u-note-ok";
  } catch (error) {
    note.textContent = `复制失败：${String(error)}（可以改用「打开日志文件夹」自行查看）`;
    note.className = "u-note u-note-bad";
  }
}
