/*
 * 帮助与诊断（ONBOARDING_AND_RECOVERY §8）。核心未就绪时也能打开：
 * 首屏按当前问题给出「应用是否启动、数据是否可写、AI 是否验证过、时间线状态」，
 * 没测过就写「未检查」，不凭字段非空报正常。
 */

import type { AppContext, Pane } from "./app";
import { openDir } from "./app";
import type { Json } from "./api";
import { uiError } from "./api";
import { bulletList, button, el, facts, fill, paragraph, primary, section, stamp } from "./dom";
import { checkList } from "./graphics";

const FAQ: Array<[string, string]> = [
  ["后台服务未启动", "在启动页点「重启后台服务」；仍失败就打开日志目录并复制诊断信息。"],
  ["无法保存到这个位置", "检查数据目录的权限与剩余空间；界面不会自动删除历史来腾空间。"],
  ["还未连接 AI", "去设置 → AI 服务填写服务、模型与密钥，点「测试并保存」。"],
  ["服务拒绝了这次访问（401 / 403）", "密钥无效或权限不足：换一个密钥或检查该模型的开通情况。"],
  ["未在等待时间内得到结果", "测试可以重试；写入类操作先用「查询原结果」确认，再决定重试。"],
  ["模型没有返回可用结果", "不是角色在故意沉默：重试这一轮，或调整输出长度设置。"],
  ["世界已暂停", "查看历史与编辑草稿照常；要发消息先点「启动」。"],
  ["正在补齐离开期间的进展", "读已经完成的记录即可；时钟显示的是已完成的那一刻。"],
  ["结果待确认", "用原操作身份查询结果，不要重复提交。"],
  ["备份部分成功 / 恢复失败", "按提示补做失败部分；恢复前副本始终保留在备份目录。"],
];

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
    page.appendChild(el("h2", { class: "u-h2", text: "帮助与诊断" }));

    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    page.appendChild(
      section(
        "现在的状态",
        facts([
          ["后台服务", `运行中（版本 ${String(readiness.app ?? "—")}）`],
          ["数据目录可写", checks.find((item) => item.key === "data")?.ok ? "通过" : "需要处理"],
          ["AI 配置", ai.configured ? "已填写（是否验证过见下）" : "还没有可用配置"],
          ["最近一次连接测试", String(this.ctx.prefs["ai.tested_at"] ? stamp(Number(this.ctx.prefs["ai.tested_at"])) : "未检查")],
          ["时间线状态", await this.timelineStatus()],
        ]),
        el(
          "div",
          { class: "u-row" },
          primary("重新检查本机", () => {
            void (async () => {
              await this.ctx.refresh();
              note.textContent = "已重新检查本机状态（不会调用外部 AI）";
            })();
          }),
          button("测试 AI", () => this.ctx.navigate({ pane: "settings", sub: "ai" })),
          button("复制诊断信息", () => void copyDiagnostics(this.ctx, note)),
        ),
        note,
      ),
    );

    page.appendChild(
      section(
        "本机检查明细",
        checkList(
          checks.map((item) => ({
            label: String(item.label),
            ok: Boolean(item.ok),
            detail: String(item.detail ?? ""),
          })),
        ),
      ),
    );

    page.appendChild(
      section(
        "位置与日志",
        facts([
          ["数据位置", String(paths.data ?? "—")],
          ["配置位置", String(paths.config ?? "—")],
          ["创作目录", String(paths.packages ?? "—")],
          ["备份目录", String(paths.backups ?? "—")],
        ]),
        el(
          "div",
          { class: "u-row" },
          button("打开日志目录", () => void openDir("logs", this.ctx.api)),
          button("打开数据位置", () => void openDir("data", this.ctx.api)),
          button("打开创作目录", () => void openDir("packages", this.ctx.api)),
        ),
        paragraph("原始日志在本机打开；「复制诊断信息」按允许清单构造，不含密钥、聊天正文与世界内部数据。", "u-hint"),
      ),
    );

    page.appendChild(
      section(
        "常见问题",
        bulletList(
          FAQ.map(([title, action]) => `${title} —— ${action}`),
          "u-list",
        ),
      ),
    );

    page.appendChild(
      section(
        "高级调试",
        paragraph(
          "连接、运行与管理验证工具（Core Debugging）。它面向排错，不作为日常入口，也不提供普通工作区禁止的权限。",
        ),
        el("div", { class: "u-row" }, primary("进入高级调试", () => this.ctx.openDebug())),
        paragraph("进入后正式工作区暂停使用这条连接；在调试界面点「回到正式界面」即可返回。", "u-hint"),
      ),
    );

    page.appendChild(
      section(
        "版本",
        facts([
          ["程序版本", String(readiness.app ?? "—")],
          ["数据格式", String(readiness.data_format ?? "—")],
          ["规则版本", String(readiness.rules ?? "—")],
        ]),
      ),
    );

    fill(host, page);
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
      if (!first) return `${instance.name}：没有时间线`;
      const clock = await this.ctx.api.clock(instance.id, String(first.id));
      const view = (clock.clock as Json) ?? {};
      const state = String(view.state ?? "");
      return `${instance.name} / ${String(first.name ?? "")}：${state === "active" ? "运行中" : state === "frozen" ? "已暂停" : state}（已完成到 ${Number(view.processed_world ?? 0)}）`;
    } catch (error) {
      return `读取失败：${uiError(error, { module: "帮助", action: "读取时间线状态" }).message}`;
    }
  }
}

export async function copyDiagnostics(ctx: AppContext, note: HTMLElement): Promise<void> {
  const readiness = ctx.readiness ?? {};
  const ai = ((readiness.ai as Json) ?? {}) as Json;
  const lines = [
    `isekai 诊断 ${stamp(Date.now() / 1000)}`,
    `程序版本 ${String(readiness.app ?? "-")}｜数据格式 ${String(readiness.data_format ?? "-")}｜规则 ${String(readiness.rules ?? "-")}`,
    `核心状态 ${String(readiness.state ?? "-")}｜存储可写 ${readiness.storage_ok ? "是" : "否"}`,
    `本机检查：${((readiness.checks as Json[]) ?? []).map((item) => `${String(item.key)}=${item.ok ? "ok" : "bad"}`).join(" ")}`,
    `AI 服务 ${String(ai.base_url ?? "-")}｜模型 ${String(ai.model ?? "-")}｜密钥 ${ai.api_key_set ? "已设置" : "未设置"}`,
    "（未包含密钥、访问令牌、聊天正文、记忆、世界内部数据与提示词）",
  ];
  try {
    await navigator.clipboard.writeText(lines.join("\n"));
    note.textContent = "诊断信息已复制到剪贴板（可先粘贴检查一遍）";
    note.className = "u-note u-note-ok";
  } catch (error) {
    note.textContent = `复制失败：${String(error)}（可以改用「打开日志目录」自行查看）`;
    note.className = "u-note u-note-bad";
  }
}
