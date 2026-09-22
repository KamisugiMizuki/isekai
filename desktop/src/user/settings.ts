/*
 * 设置（USER_INTERFACE_DESIGN §10.1）。分组各自保存、各自有结果槽；
 * 不放跨整页的大保存按钮；路径用原生浏览，闭集用下拉。
 */

import { invoke } from "@tauri-apps/api/core";
import type { AppContext, Pane } from "./app";
import { openDir } from "./app";
import type { Json } from "./api";
import { uiError } from "./api";
import {
  bulletList,
  button,
  chip,
  el,
  facts,
  field,
  fill,
  paragraph,
  primary,
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
      setNote(note, "正在测试：检查地址 / 验证访问 / 检查回复格式…", "pending");
      fill(results);
      try {
        const result = await this.ctx.api.testAi(payload());
        const checks = (result.checks as Json[]) ?? [];
        results.appendChild(
          facts(
            checks.map((item) => [String(item.label), `${item.ok ? "通过" : "未通过"}：${String(item.detail ?? "")}`]),
          ),
        );
        if (result.ok) {
          await save(true);
          return;
        }
        setNote(note, String(result.reason || "这次测试没有成功"), "bad");
        results.appendChild(
          el(
            "div",
            { class: "u-row" },
            button("重新测试连接", () => void test()),
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
      paragraph("凭据只保存在本机配置文件，读取时打码；修改不会重写已有内容。"),
      field("服务地址", baseUrl, "接口地址，不是聊天网页网址"),
      field("模型", model),
      field("访问密钥", key),
      el(
        "details",
        { class: "u-advanced" },
        el("summary", { text: "高级输出参数" }),
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
    const interval = el("input", { class: "u-input", type: "number", min: "1", value: String(backup.interval_hours ?? 24) }) as HTMLInputElement;
    const keep = el("input", { class: "u-input", type: "number", min: "1", value: String(backup.keep ?? 7) }) as HTMLInputElement;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const list = el("ul", { class: "u-list" });
    try {
      const result = await this.ctx.api.backups();
      const items = (result.backups as Json[]) ?? [];
      for (const item of items.slice(0, 10)) {
        list.appendChild(
          el("li", {
            text: `${String(item.file ?? "")}｜${stamp(Number(item.created_at ?? 0))}｜${Math.round(Number(item.bytes ?? 0) / 1024)} KB`,
          }),
        );
      }
      if (!items.length) list.appendChild(el("li", { class: "u-hint", text: "还没有备份文件。" }));
    } catch (error) {
      list.appendChild(el("li", { class: "u-note u-note-bad", text: uiError(error, { module: "备份", action: "读取列表" }).message }));
    }
    return section(
      "数据与备份",
      paragraph("数据默认放在当前用户的应用数据目录；备份目录可以改到外部磁盘（同盘备份不能防磁盘损坏）。"),
      el(
        "div",
        { class: "u-row" },
        button("打开数据目录", () => void openDir("data", this.ctx.api)),
        button("打开备份目录", () => void openDir("backups", this.ctx.api)),
      ),
      field("备份目录（相对数据根）", dir),
      field("检查间隔（小时）", interval),
      field("保留份数", keep),
      el(
        "div",
        { class: "u-row" },
        primary("保存备份设置", () => {
          void (async () => {
            try {
              await this.ctx.api.saveSettings({
                backup: { dir: dir.value.trim(), interval_hours: Number(interval.value || 24), keep: Number(keep.value || 7) },
              });
              await this.ctx.refresh();
              setNote(note, "已保存", "ok");
            } catch (error) {
              setNote(note, uiError(error, { module: "设置", action: "保存备份设置" }).message, "bad");
            }
          })();
        }),
        button("立即备份", () => {
          void (async () => {
            setNote(note, "正在备份…", "pending");
            try {
              const result = await this.ctx.api.backupNow("界面手动备份");
              const saved = (result.backup as Json) ?? result;
              setNote(note, `备份完成：${String(saved.file ?? "新的备份文件")}`, "ok");
            } catch (error) {
              setNote(note, uiError(error, { module: "备份", action: "立即备份" }).message, "bad");
            }
          })();
        }),
        button("恢复备份…", () => void this.restoreBackup(note)),
      ),
      note,
      list,
    );
  }

  private async restoreBackup(note: HTMLElement): Promise<void> {
    try {
      const picked = await invoke<string | null>("pick_file", {
        dir: null,
        title: "选择要恢复的备份",
        filter: "备份文件 (*.db)|*.db|所有文件 (*.*)|*.*",
      });
      if (!picked) {
        setNote(note, "已取消恢复", "muted");
        return;
      }
      setNote(note, "正在校验并恢复…", "pending");
      await this.ctx.api.call("backup.check", { path: picked });
      await this.ctx.api.backupRestore(picked);
      setNote(note, "已恢复：全部时间线处于暂停，请检查世界列表（恢复前的副本留在备份目录）", "ok");
      await this.ctx.refresh();
    } catch (error) {
      setNote(note, uiError(error, { module: "备份", action: "恢复备份" }).message, "bad");
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
    return section(
      "扩展",
      paragraph("规则与外部聊天通道是两类扩展：这里管通道；规则插件的登记与版本管理按实施顺序在 U4 落地。"),
      el("div", { class: "u-row" }, button("读取本机扩展", () => void scan())),
      list,
      note,
      chip("当前版本不自动启用任何外部扩展", "muted"),
    );
  }
}
