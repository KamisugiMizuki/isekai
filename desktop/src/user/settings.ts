/*
 * 设置（USER_INTERFACE_DESIGN §10.1）。分组各自保存、各自有结果槽；
 * 不放跨整页的大保存按钮；路径用原生浏览，闭集用下拉。
 *
 * 2026-10-08 视觉体系审查 §三-5：这一页过去把 7 段平铺成一根 3676px 的长条（26 个按钮、5.5 屏），
 * 上一轮加的「跳到」索引又和分区标签页长得一样（同一次审查的根因 2）。现在改成真子页：
 * 标题带 + tools 分区条 + 只画当前分区的内容，分区内部再用 panel 分组。
 */

import { invoke } from "@tauri-apps/api/core";
import type { AppContext, Pane } from "./app";
import { openDir } from "./app";
import { aiSetup } from "./ai-setup";
import { migrateCard } from "./migrate";
import type { Json } from "./api";
import { uiError } from "./api";
import {
  button,
  chip,
  el,
  errorCard,
  facts,
  field,
  fill,
  pageHead,
  panel,
  paragraph,
  primary,
  sizeText,
  section,
  setNote,
  stamp,
  tools,
  type Child,
} from "./dom";
import { meter, checkList } from "./graphics";

/** 预算账本里的任务名 → 人话（核心给的是内部标识，认不出来就照原样显示） */
const TASK_TEXT: Record<string, string> = {
  dialog: "对话生成",
  dialog_commit: "对话生成",
  event_render: "事件表述",
  claim_expand: "说法展开",
  intent_propose: "意图提案",
  life_refine: "生活线细化",
  memory_extract: "记忆提取",
  memory_compact: "记忆汇总",
  narrative_audit: "回复后验审计",
  story_classify: "输入分类",
  wa_suggest: "写作建议",
  proactive_text: "主动消息",
  embedding: "向量化",
};

/**
 * 设置分区（顺序 = 用户依次会碰到它们的顺序）。每个分区都是一张真子页：
 * 切分区只重画内容带，不整页导航——导航会重新 refresh 一次，切一格要等两次往返。
 * id 是路由 / 偏好里用的稳定标识，label 是分区条上的字。
 */
type SettingsTab = "ai" | "memory" | "version" | "data" | "usage" | "appearance" | "extensions";

const SETTINGS_TABS: Array<{ id: SettingsTab; label: string }> = [
  { id: "ai", label: "AI 服务" },
  { id: "memory", label: "记忆检索" },
  { id: "version", label: "自动保存版本" },
  { id: "data", label: "数据与备份" },
  { id: "usage", label: "用量" },
  { id: "appearance", label: "通知与外观" },
  { id: "extensions", label: "扩展" },
];

export class SettingsPane implements Pane {
  readonly id = "settings" as const;

  /** 当前分区：进页面时由 route.sub / 偏好定，之后由分区条上的点击改写 */
  private tab: SettingsTab = "ai";
  private host: HTMLElement | null = null;
  /** 连续切分区时，晚到的旧渲染不许盖住新分区（几个分区里有异步读取） */
  private renderToken = 0;

  constructor(private readonly ctx: AppContext) {}

  async mount(host: HTMLElement): Promise<void> {
    this.host = host;
    await this.ctx.refresh();
    this.tab = this.initialTab();
    // 入口带的下标也算「最近一次用过的分区」：记住它，下次不带 sub 从侧栏打开设置还停在这一格
    //（`sub` 与 `tab` 同名时说明这句话是入口带指定的，不是回落的旧偏好）
    if (this.ctx.route.sub === this.tab) void this.ctx.setPrefs({ "settings.tab": this.tab });
    await this.render();
    // 入口带的下标要认：
    //   `sub:"ai"`（首页「现在配置」等）把光标放进密钥框，省得再找一次输入框；
    //   `sub:"extensions"`（跑团页的「打开设置里的扩展页」）把扩展分区带到视口顶部——
    //   探针 _probe_ui_nav.py 的 N6b 查的就是 #u-set-extensions 的位置。
    if (this.ctx.route.sub === "ai") {
      (host.querySelector("#u-set-key") as HTMLInputElement | null)?.focus();
    } else if (this.ctx.route.sub === "extensions") {
      host.querySelector("#u-set-extensions")?.scrollIntoView({ block: "start" });
    }
  }

  unmount(): void {
    // 卸下之后在途的渲染不许再写（写进已移除的节点看不见，但会白跑一次接口）
    this.host = null;
    this.renderToken += 1;
  }

  /**
   * 先认入口带的下标，再认上次停在哪一格，最后回落到 AI 服务（第一次打开设置最可能要配 AI）。
   * 下标按分区名对表：`sub` 只认已知的分区，写错的名字不该把页面带到别的格（也不该报错）。
   */
  private initialTab(): SettingsTab {
    const sub = String(this.ctx.route.sub ?? "");
    if (SETTINGS_TABS.some((item) => item.id === sub)) return sub as SettingsTab;
    const stored = String(this.ctx.prefs["settings.tab"] ?? "");
    return SETTINGS_TABS.some((item) => item.id === stored) ? (stored as SettingsTab) : "ai";
  }

  private selectTab(id: SettingsTab): void {
    if (this.tab === id) return;
    this.tab = id;
    // 下次打开设置还停在这一格。写失败只影响「下次」，这次切换照常（setPrefs 自己会提示）
    void this.ctx.setPrefs({ "settings.tab": id });
    void this.render();
  }

  /** 只画当前分区（审查 §三-5：7 段全铺在一页 = 找一项要滚 5 屏、26 个按钮同样重） */
  private async render(): Promise<void> {
    const host = this.host;
    if (!host) return;
    const tab = this.tab;
    const token = ++this.renderToken;
    const page = el("div", { class: "u-page" });
    // 标题带：标题位置与定位语在全站统一（审查 A）
    page.appendChild(pageHead("设置", "改了立刻生效；凭据只在本机", []));
    // 分区切换用下划线选中态（tools）：与动作按钮、页内锚点胶囊形状不同，行为一眼可辨
    page.appendChild(
      tools(
        SETTINGS_TABS.map((item) => ({
          label: item.label,
          current: item.id === tab,
          onSelect: () => this.selectTab(item.id),
        })),
        "设置分区",
      ),
    );
    // 骨架先上屏、内容后填：有几个分区要先读接口（备份列表 / 用量 / 规则登记），
    // 等接口回来再换页会让「点了分区没反应」——选中态和「正在读取」必须立刻可见
    const bodyHost = el("div", {});
    bodyHost.appendChild(paragraph("正在读取这一格…", "u-hint"));
    page.appendChild(bodyHost);
    fill(host, page);
    // 换分区回到页面顶部：上一格滚到的位置在新内容里没有意义，可能直接停在空白处
    host.parentElement?.scrollTo({ top: 0 });
    const body = await this.tabBody(tab);
    if (token !== this.renderToken || !this.host) return; // 期间又切了一格：这次的结果丢掉
    fill(bodyHost, body);
  }

  /** 分区 → 内容。每格自己负责两级分组（panel 套在 section 里）与自己的结果槽 */
  private async tabBody(tab: SettingsTab): Promise<HTMLElement> {
    switch (tab) {
      case "memory":
        return await this.memorySection();
      case "version":
        return this.versionSection();
      case "data":
        return await this.backupSection();
      case "usage":
        return await this.usageSection();
      case "appearance":
        return this.appearanceSection();
      case "extensions":
        return this.extensionSection();
      default:
        return this.aiSection();
    }
  }

  /* ---------------------------------------------------------------- AI 服务 */

  private aiSection(): HTMLElement {
    const llm = ((this.ctx.settings.llm as Json) ?? {}) as Json;
    const ai = ((this.ctx.readiness.ai as Json) ?? {}) as Json;
    const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
    const results = el("div", {});
    // 与首次设置向导共用同一份控件与校验（评审 P0-1 / P1-1）：这里原先自己写了一套
    // 「服务地址 / 模型 / 密钥」，没有预置服务、没有密钥申请页、没有地址格式校验，
    // 于是能保存一个必然不通的地址并回「已保存未验证的配置」。
    const setup = aiSetup({
      saved: llm,
      apiKeySet: Boolean(llm.api_key_set ?? ai.api_key_set),
      apiKeyMasked: String(llm.api_key_masked ?? ai.api_key_masked ?? ""),
      servicePref: String(this.ctx.prefs["onboard.service"] ?? ""),
      onServicePref: (id) => void this.ctx.setPrefs({ "onboard.service": id }),
      notify: (text, kind) => setNote(note, text, kind),
    });
    // 入口带的 `sub:"ai"`（首页「现在配置」等）按这个 id 聚焦密钥框，见 mount()
    setup.key.id = "u-set-key";

    const save = async (verified: boolean): Promise<void> => {
      if (setup.validate(note)) return; // 不合格的地址 / 模型拦在保存之前，理由写在本段状态行
      try {
        await this.ctx.api.saveSettings({ llm: setup.collect() });
        setup.key.value = "";
        await this.ctx.refresh();
        // 与向导同一套记录：帮助页的「最近一次连接测试」读这两个偏好，原先只有向导写
        await this.ctx.setPrefs({
          "ai.tested_at": verified ? Date.now() / 1000 : this.ctx.prefs["ai.tested_at"] ?? 0,
          "ai.verified": verified,
        });
        setNote(
          note,
          verified ? "已保存，基础能力已验证" : "已保存未验证的配置（新生成仍会按运行时错误提示）",
          verified ? "ok" : "pending",
        );
      } catch (error) {
        setNote(note, uiError(error, { module: "设置", action: "保存 AI 服务" }).message, "bad");
      }
    };

    const test = async (): Promise<void> => {
      if (setup.validate(note)) return;
      setNote(note, "正在测试，已等待 0 秒…", "pending");
      fill(results);
      const progress = el("div", { class: "u-progress" });
      results.appendChild(progress);
      // 只显示**真实**阶段与已等待时间（ONBOARDING §4.2）：细阶段内核测完才回，
      // 等待期间只报已等待秒数，不摆三行假装在跑的固定阶段。
      const live = el("p", { class: "u-hint", text: "正在测试，已等待 0 秒…" });
      progress.appendChild(live);
      const startedAt = Date.now();
      const timer = window.setInterval(() => {
        const waited = Math.floor((Date.now() - startedAt) / 1000);
        live.textContent = `正在测试，已等待 ${waited} 秒…`;
        setNote(note, `正在测试，已等待 ${waited} 秒…`, "pending");
      }, 1000);
      try {
        const result = await this.ctx.api.testAi(setup.collect());
        const stages = (result.stages as Json[]) ?? [];
        const checks = (result.checks as Json[]) ?? [];
        fill(progress);
        // 内核返回的真实阶段（每个阶段带 label/ok/detail）先行，随后是最终能力检查
        const stageItems = stages.map((item) => ({
          label: String(item.label),
          ok: Boolean(item.ok),
          detail: String(item.detail ?? ""),
        }));
        if (stageItems.length) results.appendChild(checkList(stageItems));
        results.appendChild(
          checkList(
            checks.map((item) => ({
              label: String(item.label),
              ok: Boolean(item.ok),
              detail: String(item.detail ?? ""),
            })),
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
      } finally {
        window.clearInterval(timer);
      }
    };

    const clearKey = async (): Promise<void> => {
      try {
        // 清密钥不该被地址校验拦住：只把密钥字段置空，其余照当前填写的提交
        await this.ctx.api.saveSettings({ llm: { ...setup.collect(), api_key: "" } });
        await this.ctx.refresh();
        setNote(note, "已清除访问密钥：新的生成会停用，历史保留", "ok");
      } catch (error) {
        setNote(note, uiError(error, { module: "设置", action: "清除密钥" }).message, "bad");
      }
    };

    return section(
      "AI 服务",
      paragraph("凭据只保存在本机，读取时打码。测试只发送简短测试文字，可能产生少量用量。"),
      // 两级分组：填写与测试是两件事，按钮只跟着它服务的那一组走
      //（审查 §三-5：以前 26 个按钮排在一根长条上，看不出哪个按钮管哪一段）
      panel("填写服务与访问密钥", ...setup.nodes),
      panel(
        "测试并保存",
        el(
          "div",
          { class: "u-row" },
          primary("测试并保存", () => void test()),
          button("只保存（未验证）", () => void save(false)),
          button("清除访问密钥", () => void clearKey()),
        ),
        note,
        results,
      ),
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
      panel("检索方式", field("模式", mode), advanced),
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
      panel(
        "什么时候留版本点",
        el("label", { class: "u-check" }, enabled, el("span", { text: "开启自动保存版本" })),
        field("现实间隔（分钟）", minutes),
        field("事件阈值（条）", events),
      ),
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
                  report.complete ? "这份备份完整：内容与记录一一对上" : `这份备份不完整：${problems.slice(0, 3).join("；")}`,
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
    // 列表读取是只读的：失败时给出实话（没改数据 / 读取是否成功），重试就地重读
    const loadPacks = async (): Promise<void> => {
      fill(list, el("p", { class: "u-hint", text: "正在读取备份列表…" }));
      try {
        const result = await this.ctx.api.packList();
        packs = (result.packs as Json[]) ?? [];
        const record = (result.record as Json) ?? {};
        if (String(record.state ?? "") === "rolled_back" || String(record.state ?? "") === "needs_attention") {
          note.textContent = `上次恢复没有走完：${String(record.state) === "rolled_back" ? "已回退到恢复前的数据" : "需要人工确认数据状态"}`;
          note.className = `u-note u-note-${String(record.state) === "rolled_back" ? "pending" : "bad"}`;
        }
        renderList();
      } catch (error) {
        fill(
          list,
          errorCard(
            uiError(error, { module: "备份", action: "读取列表", done: "没有改动任何数据", unknown: "这次读取是否成功" }),
            [{ label: "重试读取", run: () => void loadPacks() }],
          ),
        );
      }
    };
    await loadPacks();

    return section(
      "数据与备份",
      paragraph(
        "备份就是一份文件：你的全部内容（世界、会话、版本、素材、草稿）都装在里面，可以拷到别的磁盘或别的机器。密钥与外部扩展的凭据不进备份。",
      ),
      // 两级分组：手动备份/恢复是一组，自动备份的节奏是另一组——
      // 「立即备份」与「保存备份设置」以前并排在一行，看起来像同一个动作的两个按钮
      panel(
        "备份与恢复",
        el(
          "div",
          { class: "u-row" },
          button("打开数据文件夹", () => void openDir("data", this.ctx.api)),
          button("打开备份文件夹", () => void openDir("backups", this.ctx.api)),
        ),
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
        ),
        note,
        list,
        restoreHost,
      ),
      panel(
        "自动备份",
        field("备份文件夹（在数据文件夹里的相对位置）", dir),
        field("自动备份间隔（小时，0 = 只在退出前补做）", interval),
        field("自动备份保留份数", keep),
        el(
          "div",
          { class: "u-row" },
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
      ),
      // 迁移卡片不属于上面两组：它是另一件事（换机器时把内容带过来），放在分区最后
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
          ["这份备份里有", `${String(counts.instances ?? "?")} 个世界、${String(counts.timelines ?? "?")} 条世界线`],
          ["当前有", `${current} 个世界`],
          ["展开后大小", sizeText(Number(staged.expanded_bytes ?? 0))],
          ["恢复后会", "全部世界线暂停；需要时再逐条启动"],
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
                setNote(confirmNote, `恢复完成：全部世界线暂停，请到世界与素材里逐条启动（恢复前的数据留在备份文件夹里）`, "ok");
                await this.ctx.refresh();
                void done;
              } catch (error) {
                setNote(
                  confirmNote,
                  uiError(error, {
                    module: "备份",
                    action: "恢复全部数据",
                    done: "已经按记录回退或停在那里，请看备份文件夹里的恢复记录",
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
    const factsHost = el("div", {});
    const host = el("div", {});
    const load = async (): Promise<void> => {
      fill(host, paragraph("正在读取用量…", "u-hint"));
      if (!select.value) {
        fill(host, paragraph("还没有世界。", "u-hint"));
        fill(factsHost);
        return;
      }
      try {
        const result = await this.ctx.api.budget(select.value);
        const limits = ((result.limits as Json) ?? {}) as Json;
        const usage = ((result.usage as Json) ?? {}) as Json;
        const ledger = (result.rows as Json[]) ?? [];
        const paused = ((result.paused_tasks as string[]) ?? []).map((item) => String(item));
        const names = new Map<string, string>();
        // 只读的「世界 / 会话」facts 并进这一格：用量这一页看到的全是读数，没有一个可以改
        const worldFacts: Array<[string, string]> = [];
        try {
          const info = await this.ctx.api.instanceInfo(select.value);
          const lines = (info.timelines as Json[]) ?? [];
          for (const timeline of lines) {
            names.set(String(timeline.id), String(timeline.name ?? timeline.id));
          }
          const live = lines.filter((item) => String(item.state) === "active").length;
          worldFacts.push(
            ["世界", instances.find((item) => item.id === select.value)?.name ?? select.value],
            ["世界线", lines.length ? `${lines.length} 条（${live ? `运行中 ${live} 条` : "全部暂停"}）` : "还没有世界线"],
            ["角色", `${((info.characters as Json[]) ?? []).length} 位`],
          );
        } catch {
          /* 名字与条数读不到就少两行读数：读数是真值，名字只是好看，不猜一个 0 出来 */
        }
        try {
          const listed = await this.ctx.api.sessions();
          const count = ((listed.sessions as Json[]) ?? []).filter(
            (item) => String(item.instance_id) === select.value,
          ).length;
          worldFacts.push(["会话", `${count} 条`]);
        } catch {
          /* 会话清单读不到就不出这一行——不把「读不到」画成「0 条」 */
        }
        fill(factsHost, worldFacts.length ? facts(worldFacts) : paragraph("这个世界的基本读数暂时读不到。", "u-hint"));
        const instanceLimit = Number(limits.instance_tokens_per_day ?? 0);
        const timelineLimit = Number(limits.timeline_tokens_per_day ?? 0);
        const taskLimit = Number(limits.task_tokens_per_day ?? 0);
        const perTask = new Map<string, { tokens: number; calls: number }>();
        for (const row of ledger) {
          const task = String(row.task ?? "");
          const seen = perTask.get(task) ?? { tokens: 0, calls: 0 };
          seen.tokens += Number(row.tokens ?? 0);
          seen.calls += Number(row.calls ?? 0);
          perTask.set(task, seen);
        }
        const timelines = Object.entries(((usage.timelines as Json) ?? {}) as Json);
        const body: Child[] = [
          paragraph(
            "按现实日统计：每一个外部调用发起前先占额度，成功、失败、超时都按真实消耗结算。这里只读。",
            "u-hint",
          ),
          el("div", { class: "u-meters" }, meter(Number(usage.instance ?? 0), instanceLimit, "这个世界今天用掉（全部世界线）")),
        ];
        if (timelines.length) {
          body.push(el("h4", { class: "u-sub", text: "各条世界线" }));
          body.push(
            el(
              "div",
              { class: "u-meters" },
              ...timelines.map(([id, tokens]) =>
                meter(Number(tokens ?? 0), timelineLimit, names.get(id) ?? id),
              ),
            ),
          );
        }
        if (perTask.size) {
          body.push(el("h4", { class: "u-sub", text: "按任务" }));
          body.push(
            el(
              "div",
              { class: "u-meters" },
              ...[...perTask.entries()]
                .sort((a, b) => b[1].tokens - a[1].tokens)
                .map(([task, seen]) => meter(seen.tokens, taskLimit, `${TASK_TEXT[task] ?? task}（${seen.calls} 次）`)),
            ),
          );
        } else {
          body.push(paragraph("这一现实日还没有调用记录。", "u-hint"));
        }
        if (paused.length) {
          body.push(
            el(
              "div",
              { class: "u-row" },
              ...paused.map((task) => chip(`已暂停：${TASK_TEXT[task] ?? task}`, "bad")),
            ),
          );
        }
        fill(host, ...body);
      } catch (error) {
        fill(host, paragraph(uiError(error, { module: "用量", action: "读取用量" }).message, "u-note u-note-bad"));
      }
    };
    select.addEventListener("change", () => void load());
    await load();
    return section(
      "用量",
      paragraph("这里显示核心记录的调用次数与量级，不做费用面板；用量按现实日重新累计，当天的额度用完后新的调用会停下来。"),
      panel("这个世界", field("世界", select), factsHost),
      panel("今天的用量", host),
      note,
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
      // 这一格的四个控件由同一个「保存这一组」一起提交，所以只分一层面板：
      // 面板标题写清它们是一组，按钮就放在这一组里（`_probe_ui_anchor.py` 也按
      // 「离选择器最近的那个 section 里的保存按钮」来找它，别把按钮挪出这一组）
      panel(
        "提醒与显示（一起保存）",
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
        paragraph("通知不是第二份历史：它只作已定稿消息的入口，历史始终在角色联络里。", "u-hint"),
      ),
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
            : paragraph("本机没有安装外部扩展。", "u-hint"),
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
          fill(rulesList, paragraph("本机还没有登记跑团规则包。", "u-hint"));
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
          if (item.changed_since_registered) row.appendChild(chip("登记后文件被改过", "pending"));
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
    const node = section(
      "扩展",
      paragraph("规则包与外部聊天扩展是两类外部扩展，分开管：下面两块各自独立，互不代管。"),
      // 两级分组：两块扩展各有自己的读取按钮、列表与结果槽——
      // 以前两个「读取」按钮并排在一行，看起来像同一件事的两个入口
      panel(
        "外部聊天扩展",
        el("div", { class: "u-row" }, button("读取本机扩展", () => void scan())),
        list,
      ),
      panel(
        "跑团规则",
        paragraph("规则包来自随发行样例或你自己选的文件夹：登记前先看检查摘要，选择文件本身不执行它；规则包只在你本机运行，程序会做基本限制，但不能当成完整的安全沙箱。"),
        el("div", { class: "u-row" }, button("刷新规则登记", () => void loadRules())),
        rulesList,
        el(
          "div",
          { class: "u-row" },
          button("从本机选择规则文件夹…", () => {
            void (async () => {
              try {
                const { invoke } = await import("@tauri-apps/api/core");
                const picked = await invoke<string | null>("pick_dir", { title: "选择规则包所在的文件夹" });
                if (!picked) {
                  setNote(rulesNote, "已取消选择", "muted");
                  return;
                }
                const scanned = await this.ctx.api.rulesScan(picked);
                const candidates = (scanned.candidates as Json[]) ?? [];
                if (!candidates.length) {
                  setNote(rulesNote, `这里没有找到规则包：${String(scanned.reason ?? "")}`, "bad");
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
      ),
      note,
      chip("当前版本不自动启用任何外部扩展", "muted"),
    );
    // 这个 id 是外部约定，不能跟着结构走：跑团页的「打开设置里的扩展页」与探针 _probe_ui_nav.py
    // 的 N6b 都按它认「扩展分区」；去掉它会连带让那条导航用例失败（切到扩展分区时它要在视口顶部附近）。
    node.id = "u-set-extensions";
    return node;
  }
}
