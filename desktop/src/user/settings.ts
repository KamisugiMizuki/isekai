/*
 * 设置（USER_INTERFACE_DESIGN §10.1）。分组各自保存、各自有结果槽；
 * 不放跨整页的大保存按钮；路径用原生浏览，闭集用下拉。
 */

import { invoke } from "@tauri-apps/api/core";
import type { AppContext, Pane } from "./app";
import { openDir } from "./app";
import { migrateCard } from "./migrate";
import type { Json } from "./api";
import { uiError } from "./api";
import {
  bulletList,
  button,
  chip,
  el,
  errorCard,
  facts,
  field,
  fill,
  paragraph,
  primary,
  sizeText,
  section,
  setNote,
  stamp,
} from "./dom";

export class SettingsPane implements Pane {
  readonly id = "settings" as const;

  constructor(private readonly ctx: AppContext) {}

  async mount(host: HTMLElement): Promise<void> {
    await this.ctx.refresh();
    const page = el("div", { class: "u-page" });
    page.appendChild(el("h2", { class: "u-h2", text: "设置" }));
    page.appendChild(this.aiSection());
    page.appendChild(await this.memorySection());
    page.appendChild(this.versionSection());
    page.appendChild(await this.backupSection());
    page.appendChild(await this.usageSection());
    page.appendChild(this.appearanceSection());
    page.appendChild(this.extensionSection());
    fill(host, page);
    if (this.ctx.route.sub === "ai") {
      const node = host.querySelector("#u-set-key") as HTMLInputElement | null;
      node?.focus();
    }
  }

  /* ---------------------------------------------------------------- AI 服务 */

  private aiSection(): HTMLElement {
    const llm = ((this.ctx.settings.llm as Json) ?? {}) as Json;
    const baseUrl = el("input", { class: "u-input", value: String(llm.base_url ?? "") }) as HTMLInputElement;
    const model = el("input", { class: "u-input", value: String(llm.model ?? "") }) as HTMLInputElement;
    const key = el("input", {
      class: "u-input",
      id: "u-set-key",
      type: "password",
      autocomplete: "off",
      placeholder: llm.api_key_set ? `已设置（${String(llm.api_key_masked ?? "")}）；留空表示不改` : "粘贴访问密钥",
    }) as HTMLInputElement;
    const timeout = el("input", { class: "u-input", type: "number", min: "1", value: String(llm.timeout_s ?? 60) }) as HTMLInputElement;
    const maxTokens = el("input", { class: "u-input", type: "number", min: "1", value: String(llm.max_tokens ?? 1024) }) as HTMLInputElement;
    const temperature = el("input", { class: "u-input", type: "number", min: "0", max: "2", step: "0.1", value: String(llm.temperature ?? 0.8) }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const results = el("div", {});

    const payload = (): Json => {
      const values: Json = {
        base_url: baseUrl.value.trim(),
        model: model.value.trim(),
        timeout_s: Number(timeout.value || 60),
        max_tokens: Number(maxTokens.value || 1024),
        temperature: Number(temperature.value || 0),
      };
      if (key.value.trim()) values.api_key = key.value.trim();
      return values;
    };

    const save = async (verified: boolean): Promise<void> => {
      try {
        await this.ctx.api.saveSettings({ llm: payload() });
        key.value = "";
        await this.ctx.refresh();
        setNote(note, verified ? "已保存，基础能力已验证" : "已保存未验证的配置", verified ? "ok" : "pending");
      } catch (error) {
        setNote(note, uiError(error, { module: "设置", action: "保存 AI 服务" }).message, "bad");
      }
    };

    const test = async (): Promise<void> => {
      setNote(note, "正在测试...", "pending");
      fill(results);
      const progress = el("div", { class: "u-progress" });
      results.appendChild(progress);
      
      const addStep = (label: string, ok: boolean | null) => {
        const icon = ok === null ? "⏳" : ok ? "✓" : "✗";
        const line = el("p", { text: `${icon} ${label}` });
        progress.appendChild(line);
      };
      
      addStep("检查地址", null);
      try {
        const result = await this.ctx.api.testAi(payload());
        const checks = (result.checks as Json[]) ?? [];
        fill(progress);
        results.appendChild(
          facts(
            checks.map((item) => [String(item.label), `${item.ok ? "✓ 通过" : "✗ 未通过"}：${String(item.detail ?? "")}`]),
          ),
        );
        if (result.ok) {
          await save(true);
          return;
        }
        setNote(note, String(result.reason || "测试未通过"), "bad");
        results.appendChild(
          paragraph("你可以："),
        );
        results.appendChild(
          el(
            "div",
            { class: "u-row" },
            primary("重新测试连接", () => void test()),
            button("保存未验证配置", () => void save(false)),
          ),
        );
      } catch (error) {
        setNote(note, uiError(error, { module: "设置", action: "测试 AI 连接" }).message, "bad");
      }
    };

    const clearKey = async (): Promise<void> => {
      try {
        await this.ctx.api.saveSettings({ llm: { base_url: baseUrl.value.trim(), model: model.value.trim(), api_key: "" } });
        await this.ctx.refresh();
        setNote(note, "已清除访问密钥：新的生成会停用，历史保留", "ok");
      } catch (error) {
        setNote(note, uiError(error, { module: "设置", action: "清除密钥" }).message, "bad");
      }
    };

    return section(
      "AI 服务",
      paragraph("凭据只保存在本机,读取时打码。测试只发送简短测试文字。"),
      field("服务地址", baseUrl, "接口地址，不是聊天网页网址"),
      field("模型", model),
      field("访问密钥", key),
      el(
        "details",
        { class: "u-advanced" },
        el("summary", { text: "高级参数（通常不需要调整）" }),
        field("等待时间（秒）", timeout),
        field("单次输出长度（token）", maxTokens),
        field("生成随机程度（0–2）", temperature),
      ),
      el(
        "div",
        { class: "u-row" },
        primary("测试并保存", () => void test()),
        button("只保存（未验证）", () => void save(false)),
        button("清除访问密钥", () => void clearKey()),
      ),
      note,
      results,
    );
  }

  /* ---------------------------------------------------------------- 记忆检索 */

  private async memorySection(): Promise<HTMLElement> {
    const memory = ((this.ctx.settings.memory as Json) ?? {}) as Json;
    const mode = el("select", { class: "u-input" }) as HTMLSelectElement;
    mode.appendChild(el("option", { value: "chat", text: "基础文字检索（默认，不需要额外服务）" }));
    mode.appendChild(el("option", { value: "separate", text: "增强语义检索（独立服务）" }));
    mode.value = String(memory.mode ?? "chat");
    const baseUrl = el("input", { class: "u-input", value: String(memory.base_url ?? "") }) as HTMLInputElement;
    const model = el("input", { class: "u-input", value: String(memory.model ?? "") }) as HTMLInputElement;
    const key = el("input", { class: "u-input", type: "password", autocomplete: "off", placeholder: memory.api_key_set ? "已设置；留空表示不改" : "访问密钥" }) as HTMLInputElement;
    const note = el("p", { class: "u-note" });
    const advanced = el(
      "details",
      { class: "u-advanced", hidden: mode.value !== "separate" },
      el("summary", { text: "增强服务配置" }),
      field("服务地址", baseUrl),
      field("模型", model),
      field("访问密钥", key),
    );
    mode.addEventListener("change", () => {
      advanced.hidden = mode.value !== "separate";
    });
    return section(
      "记忆检索",
      paragraph("默认基础文字检索；增强语义检索是独立服务，失败不会禁用聊天。"),
      field("模式", mode),
      advanced,
      el(
        "div",
        { class: "u-row" },
        primary("保存检索设置", () => {
          void (async () => {
            try {
              await this.ctx.api.saveSettings({
                memory: {
                  mode: mode.value,
                  ...(mode.value === "separate"
                    ? {
                        base_url: baseUrl.value.trim(),
                        model: model.value.trim(),
                        ...(key.value.trim() ? { api_key: key.value.trim() } : {}),
                      }
                    : {}),
                },
              });
              key.value = "";
              await this.ctx.refresh();
              setNote(note, "检索设置已保存", "ok");
            } catch (error) {
              setNote(note, uiError(error, { module: "设置", action: "保存检索设置" }).message, "bad");
            }
          })();
        }),
      ),
      note,
    );
  }

  /* ---------------------------------------------------------------- 自动保存版本 */

  private versionSection(): HTMLElement {
    const commit = ((this.ctx.settings.commit as Json) ?? {}) as Json;
    const enabled = el("input", { type: "checkbox" }) as HTMLInputElement;
    enabled.checked = Boolean(commit.auto_enabled);
    const minutes = el("input", { class: "u-input", type: "number", min: "1", value: String(commit.minutes ?? 60) }) as HTMLInputElement;
    const events = el("input", { class: "u-input", type: "number", min: "1", value: String(commit.events ?? 50) }) as HTMLInputElement;
    const note = el("p", { class: "u-note" });
    return section(
      "自动保存版本",
      paragraph("运行中按现实间隔或新增事件条数留下版本点；关闭不影响日常数据持久化。"),
      el("label", { class: "u-check" }, enabled, el("span", { text: "开启自动保存版本" })),
      field("现实间隔（分钟）", minutes),
      field("事件阈值（条）", events),
      el(
        "div",
        { class: "u-row" },
        primary("保存这一组", () => {
          void (async () => {
            try {
              await this.ctx.api.saveSettings({
                commit: {
                  auto_enabled: enabled.checked,
                  minutes: Number(minutes.value || 60),
                  events: Number(events.value || 50),
                },
              });
              await this.ctx.refresh();
              setNote(note, "已保存", "ok");
            } catch (error) {
              setNote(note, uiError(error, { module: "设置", action: "保存自动版本设置" }).message, "bad");
            }
          })();
        }),
      ),
      note,
    );
  }

  /* ---------------------------------------------------------------- 数据与备份 */

  private async backupSection(): Promise<HTMLElement> {
    const backup = ((this.ctx.settings.backup as Json) ?? {}) as Json;
    const dir = el("input", { class: "u-input", value: String(backup.dir ?? "backups") }) as HTMLInputElement;
    const interval = el("input", { class: "u-input", type: "number", min: "0", value: String(backup.interval_hours ?? 24) }) as HTMLInputElement;
    const keep = el("input", { class: "u-input", type: "number", min: "1", value: String(backup.keep ?? 7) }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const list = el("div", { class: "u-rows" });
    const restoreHost = el("div", { class: "u-restore" });

    let packs: Json[] = [];
    try {
      const result = await this.ctx.api.packList();
      packs = (result.packs as Json[]) ?? [];
      const record = (result.record as Json) ?? {};
      if (String(record.state ?? "") === "rolled_back" || String(record.state ?? "") === "needs_attention") {
        note.textContent= `上次恢复没有走完：${String(record.state) === "rolled_back" ? "已回退到恢复前的数据" : "需要人工确认数据状态"}`;
        note.className = `u-note u-note-${String(record.state) === "rolled_back" ? "pending" : "bad"}`;
      }
    } catch (error) {
      list.appendChild(errorCard(uiError(error, { module: "备份", action: "读取列表" })));
    }

    const renderList = (): void => {
      fill(list);
      if (!packs.length) {
        list.appendChild(el("p", { class: "u-hint", text: "还没有备份文件。点「立即备份全部数据」会生成一份可以整个搬走的文件。" }));
        return;
      }
      for (const item of packs) {
        const status = String(item.status ?? "");
        const row = el("div", { class: "u-row-line" });
        row.appendChild(
          el("span", {
            class: "u-grow",
            text: `${String(item.name)}｜${stamp(Number(item.created_at ?? 0))}｜${sizeText(Number(item.bytes ?? 0))}｜${String(
              (item.counts as Json)?.instances ?? "?",
            )} 个世界`,
          }),
        );
        row.appendChild(
          chip(
            status === "ok" ? "完整" : status === "incompatible" ? "不兼容" : "不完整",
            status === "ok" ? "ok" : "bad",
          ),
        );
        row.appendChild(
          button("校验", () => {
            void (async () => {
              setNote(note, "正在校验…", "pending");
              try {
                const report = await this.ctx.api.packVerify(String(item.name));
                const problems = (report.problems as string[]) ?? [];
                setNote(
                  note,
                  report.complete ? "这份备份完整：内容与清单一一对上" : `这份备份不完整：${problems.slice(0, 3).join("；")}`,
                  report.complete ? "ok" : "bad",
                );
              } catch (error) {
                setNote(note, uiError(error, { module: "备份", action: "校验" }).message, "bad");
              }
            })();
          }),
        );
        row.appendChild(button("恢复…", () => void this.restoreFlow(String(item.name), note, restoreHost)));
        list.appendChild(row);
        const problems = (item.problems as string[]) ?? [];
        if (problems.length && status !== "ok") {
          list.appendChild(el("p", { class: "u-hint", text: `问题：${problems.slice(0, 2).join("；")}` }));
        }
      }
    };
    renderList();

    return section(
      "数据与备份",
      paragraph(
        "备份是**一份文件**：整个数据目录（世界、会话、版本、素材、草稿）都装在里面，可以拷到别的磁盘或别的机器。密钥与通道凭据不进备份。",
      ),
      el(
        "div",
        { class: "u-row" },
        button("打开数据目录", () => void openDir("data", this.ctx.api)),
        button("打开备份目录", () => void openDir("backups", this.ctx.api)),
      ),
      field("备份目录（相对数据根）", dir),
      field("自动备份间隔（小时，0 = 只在退出前补做）", interval),
      field("自动备份保留份数", keep),
      el(
        "div",
        { class: "u-row" },
        primary("立即备份全部数据", () => {
          void (async () => {
            setNote(note, "正在打包（世界先停一下，装完继续）…", "pending");
            try {
              const result = await this.ctx.api.packCreate("界面手动备份");
              const record = (result.backup as Json) ?? result;
              packs = ((await this.ctx.api.packList()).packs as Json[]) ?? packs;
              renderList();
              setNote(note, `备份完成：${String(record.name ?? "新文件")}（${sizeText(Number(record.bytes ?? 0))}）`, "ok");
            } catch (error) {
              setNote(note, uiError(error, { module: "备份", action: "备份全部数据" }).message, "bad");
            }
          })();
        }),
        button("保存备份设置", () => {
          void (async () => {
            try {
              await this.ctx.api.saveSettings({
                backup: { dir: dir.value.trim(), interval_hours: Number(interval.value || 0), keep: Number(keep.value || 7) },
              });
              await this.ctx.refresh();
              setNote(note, "已保存（关闭自动备份不影响日常保存）", "ok");
            } catch (error) {
              setNote(note, uiError(error, { module: "设置", action: "保存备份设置" }).message, "bad");
            }
          })();
        }),
      ),
      note,
      list,
      restoreHost,
      migrateCard(this.ctx),
    );
  }

  /** 恢复全部数据（§9.2）：预检 → 说清替换范围 → 确认 → 切换（先留恢复前副本，失败回退） */
  private async restoreFlow(name: string, note: HTMLElement, host: HTMLElement): Promise<void> {
    fill(host);
    const box = el("div", { class: "u-card" });
    host.appendChild(box);
    setNote(note, "正在预检这份备份…", "pending");
    try {
      const staged = await this.ctx.api.packStage(name);
      if (staged.ok !== true) {
        const problems = (staged.problems as string[]) ?? [];
        box.appendChild(paragraph(`这份备份不能用来恢复：${problems.slice(0, 3).join("；")}`, "u-note-bad"));
        box.appendChild(paragraph("当前数据没有被改动。可以换一份备份再试。", "u-hint"));
        setNote(note, "预检没通过，当前数据没被改动", "bad");
        return;
      }
      const counts = (staged.counts as Json) ?? {};
      const current = this.ctx.instances().length;
      const confirmation = el("input", { class: "u-input", placeholder: "键入 RESTORE 确认" }) as HTMLInputElement;
      box.appendChild(el("h3", { text: `用「${name}」替换当前数据？` }));
      box.appendChild(
        facts([
          ["这份备份里有", `${String(counts.instances ?? "?")} 个世界、${String(counts.timelines ?? "?")} 条时间线`],
          ["当前有", `${current} 个世界`],
          ["展开后大小", sizeText(Number(staged.expanded_bytes ?? 0))],
          ["恢复后会", "全部时间线暂停；需要时再逐条启动"],
        ]),
      );
      box.appendChild(paragraph("会先留一份恢复前副本；切换失败会自动回退并说明。这一步之后没有「取消」，请确认要替换。"));
      box.appendChild(field("确认", confirmation));
      const confirmNote = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
      box.appendChild(
        el(
          "div",
          { class: "u-row" },
          primary("保留当前数据并恢复", () => {
            void (async () => {
              if (confirmation.value.trim().toUpperCase() !== "RESTORE") {
                setNote(confirmNote, "没有确认，已取消（当前数据没被改动）", "muted");
                return;
              }
              setNote(confirmNote, "正在切换（世界先停一下）…", "pending");
              try {
                const done = await this.ctx.api.packApply(String(staged.staged), `恢复 ${name}`);
                setNote(confirmNote, `恢复完成：全部时间线暂停，请到世界与素材里逐条启动（恢复前的数据留在备份目录）`, "ok");
                await this.ctx.refresh();
                void done;
              } catch (error) {
                setNote(
                  confirmNote,
                  uiError(error, {
                    module: "备份",
                    action: "恢复全部数据",
                    done: "已经按记录回退或停在那里，请看备份目录里的恢复记录",
                    unknown: "切换是否完成",
                  }).message,
                  "bad",
                );
              }
            })();
          }),
          button("取消（不改动数据）", () => {
            fill(host);
            setNote(note, "已取消，当前数据没被改动", "muted");
          }),
        ),
      );
      box.appendChild(confirmNote);
      setNote(note, "预检通过：确认前不会改动任何数据", "ok");
    } catch (error) {
      const info = uiError(error, { module: "备份", action: "预检", done: "当前数据没被改动" });
      box.appendChild(errorCard(info, [{ label: "重新预检", run: () => void this.restoreFlow(name, note, host) }]));
      setNote(note, info.message, "bad");
    }
  }

  /* ---------------------------------------------------------------- 用量 */

  private async usageSection(): Promise<HTMLElement> {
    const instances = this.ctx.instances();
    const select = el("select", { class: "u-input" }) as HTMLSelectElement;
    for (const item of instances) select.appendChild(el("option", { value: item.id, text: item.name }));
    const note = el("p", { class: "u-note" });
    const host = el("div", {});
    const load = async (): Promise<void> => {
      fill(host, paragraph("正在读取用量…", "u-hint"));
      if (!select.value) {
        fill(host, paragraph("还没有世界实例。", "u-hint"));
        return;
      }
      try {
        const result = await this.ctx.api.budget(select.value);
        const totals = (result.totals as Json) ?? (result as Json);
        fill(
          host,
          facts(
            Object.entries(totals)
              .filter(([, value]) => typeof value === "number" || typeof value === "string")
              .slice(0, 8)
              .map(([key, value]) => [key, String(value)]),
          ),
        );
      } catch (error) {
        fill(host, paragraph(uiError(error, { module: "用量", action: "读取用量" }).message, "u-note u-note-bad"));
      }
    };
    select.addEventListener("change", () => void load());
    await load();
    return section(
      "用量",
      paragraph("这里显示核心记录的调用次数与量级，不做费用面板；每个世界的额度在世界详情里调整。"),
      field("世界", select),
      host,
      note,
      bulletList([`设置 -> 世界详情可以调整上限（runtime.budget.set）`], "u-list u-hint"),
    );
  }

  /* ---------------------------------------------------------------- 通知与外观 */

  private appearanceSection(): HTMLElement {
    const notify = el("input", { type: "checkbox" }) as HTMLInputElement;
    notify.checked = this.ctx.prefs["notify_enabled"] !== false;
    const theme = el("select", { class: "u-input" }) as HTMLSelectElement;
    for (const item of [
      ["system", "跟随系统"],
      ["light", "浅色"],
      ["dark", "深色"],
    ]) {
      theme.appendChild(el("option", { value: item[0], text: item[1] }));
    }
    theme.value = String(this.ctx.prefs["appearance.theme"] ?? "system");
    const size = el("select", { class: "u-input" }) as HTMLSelectElement;
    for (const item of [
      ["100", "100%"],
      ["125", "125%"],
      ["150", "150%"],
    ]) {
      size.appendChild(el("option", { value: item[0], text: item[1] }));
    }
    size.value = String(this.ctx.prefs["appearance.text_size"] ?? "100");
    const note = el("p", { class: "u-note" });
    return section(
      "通知与外观",
      el("label", { class: "u-check" }, notify, el("span", { text: "桌面提醒：主动消息到达时发系统通知" })),
      field("明暗", theme),
      field("文字大小", size),
      el(
        "div",
        { class: "u-row" },
        primary("保存这一组", () => {
          void (async () => {
            await this.ctx.setPrefs({
              notify_enabled: notify.checked,
              "appearance.theme": theme.value,
              "appearance.text_size": size.value,
            });
            try {
              await invoke("shell_setting_set", { key: "notify_enabled", value: notify.checked });
              setNote(note, "已保存", "ok");
            } catch (error) {
              setNote(note, `通知开关保存失败：${String(error)}`, "bad");
            }
          })();
        }),
      ),
      note,
      paragraph("通知不是第二份历史：它只作已固化消息的入口，历史始终在角色联络里。", "u-hint"),
    );
  }

  /* ---------------------------------------------------------------- 扩展 */

  private extensionSection(): HTMLElement {
    const note = el("p", { class: "u-note" });
    const list = el("div", {});
    const scan = async (): Promise<void> => {
      fill(list, paragraph("正在读取本机扩展…", "u-hint"));
      try {
        const result = await this.ctx.api.call("plugin.list");
        const plugins = (result.plugins as Json[]) ?? [];
        fill(
          list,
          plugins.length
            ? facts(
                plugins.map((item) => [
                  String(item.id ?? item.name ?? ""),
                  `${String(item.state ?? "")}${item.enabled ? "（已启用）" : ""}`,
                ]),
              )
            : paragraph("本机没有安装外部通道插件。", "u-hint"),
        );
      } catch (error) {
        fill(list, paragraph(uiError(error, { module: "扩展", action: "读取扩展" }).message, "u-note u-note-bad"));
      }
    };
    const rulesNote = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const rulesList = el("div", {});
    const loadRules = async (): Promise<void> => {
      fill(rulesList, paragraph("正在读取本机规则…", "u-hint"));
      try {
        const result = await this.ctx.api.rulesList();
        const plugins = (result.plugins as Json[]) ?? [];
        if (!plugins.length) {
          fill(rulesList, paragraph("本机还没有登记跑团规则插件。", "u-hint"));
          return;
        }
        const rows = el("div", { class: "u-rows" });
        for (const item of plugins) {
          const id = String(item.ruleset_id ?? "");
          const version = String(item.ruleset_version ?? "");
          const row = el("div", { class: "u-row-line" });
          row.appendChild(el("span", { class: "u-grow", text: `${String(item.name ?? id)} ${version}` }));
          row.appendChild(
            chip(
              String(item.status_text ?? item.status),
              String(item.status) === "available" ? "ok" : String(item.status) === "disabled" ? "muted" : "bad",
            ),
          );
          if (item.referenced_count) row.appendChild(chip(`被 ${Number(item.referenced_count)} 局使用`, "muted"));
          if (item.changed_since_registered) row.appendChild(chip("登记后清单被改过", "pending"));
          row.appendChild(
            button(item.enabled ? "停用" : "启用", () => {
              void (async () => {
                try {
                  await this.ctx.api.rulesEnable(!item.enabled, id, version);
                  setNote(rulesNote, item.enabled ? "已停用：只阻止之后的调用，进行中的结果照旧" : "已启用", "ok");
                  await loadRules();
                } catch (error) {
                  setNote(rulesNote, uiError(error, { module: "规则", action: "切换启用状态" }).message, "bad");
                }
              })();
            }),
          );
          row.appendChild(
            button("移除", () => {
              void (async () => {
                try {
                  await this.ctx.api.rulesRemove(id, version);
                  setNote(rulesNote, "已移除这条规则登记（战役引用过的版本不会走到这里）", "ok");
                  await loadRules();
                } catch (error) {
                  setNote(rulesNote, uiError(error, { module: "规则", action: "移除" }).message, "bad");
                }
              })();
            }),
          );
          rows.appendChild(row);
          if (item.reason) rows.appendChild(paragraph(String(item.reason), "u-hint"));
        }
        fill(rulesList, rows);
      } catch (error) {
        fill(rulesList, paragraph(uiError(error, { module: "规则", action: "读取规则" }).message, "u-note u-note-bad"));
      }
    };
    void loadRules();
    return section(
      "扩展",
      paragraph("规则与外部聊天通道是两类扩展，分开管：这一页上下两块各自独立，互不代管。"),
      el("div", { class: "u-row" }, button("读取本机通道", () => void scan()), button("刷新规则登记", () => void loadRules())),
      list,
      el("h4", { class: "u-sub", text: "跑团规则" }),
      paragraph("规则来自随发行样例或你自己选的目录：登记前先看检查摘要，选择文件本身不执行它；进程隔离不是完整安全沙箱。"),
      rulesList,
      el(
        "div",
        { class: "u-row" },
        button("从本机选择规则目录…", () => {
          void (async () => {
            try {
              const { invoke } = await import("@tauri-apps/api/core");
              const picked = await invoke<string | null>("pick_dir", { title: "选择规则插件所在目录" });
              if (!picked) {
                setNote(rulesNote, "已取消选择", "muted");
                return;
              }
              const scanned = await this.ctx.api.rulesScan(picked);
              const candidates = (scanned.candidates as Json[]) ?? [];
              if (!candidates.length) {
                setNote(rulesNote, `这里没有找到规则插件清单：${String(scanned.reason ?? "")}`, "bad");
                return;
              }
              for (const item of candidates) {
                await this.ctx.api.rulesRegister(String(item.manifest_path ?? picked));
              }
              setNote(rulesNote, `已登记并启用 ${candidates.length} 份规则`, "ok");
              await loadRules();
            } catch (error) {
              setNote(rulesNote, uiError(error, { module: "规则", action: "添加并启用" }).message, "bad");
            }
          })();
        }),
      ),
      rulesNote,
      note,
      chip("当前版本不自动启用任何外部扩展", "muted"),
    );
  }
}
