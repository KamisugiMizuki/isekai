/*
 * 从旧的开发目录迁移（ONBOARDING_AND_RECOVERY §3.2）。
 *
 * 首次设置与「设置 → 数据与备份」共用这一块：选旧根（原生目录选择器）→ 检查 →
 * 确认目标还没有用户资产 → 迁移（复制到暂存区 → 完整校验 → 启用）。
 * 源目录始终保留；密钥 / 通道凭据 / 进程锁不迁移；迁移后所有时间线暂停。
 */

import { invoke } from "@tauri-apps/api/core";
import type { AppContext } from "./app";
import type { Json } from "./api";
import { uiError } from "./api";
import { button, dialog, el, errorCard, facts, fill, paragraph, primary, setNote, sizeText } from "./dom";
import { flowRail } from "./graphics";

export function migrateCard(ctx: AppContext): HTMLElement {
  const note = el("p", { class: "u-note", role: "status", "aria-live": "polite" });
  const result = el("div", {});
  let picked = "";

  const check = async (path: string): Promise<void> => {
    fill(result);
    setNote(note, "正在检查这个目录…", "pending");
    try {
      const report = await ctx.api.migrateInspect(path);
      const inspect = (report.inspect as Json) ?? {};
      const target = (report.target as Json) ?? {};
      const problems = (inspect.problems as string[]) ?? [];
      const counts = (inspect.counts as Json) ?? {};
      result.appendChild(
        facts([
          ["这个目录", String(inspect.path ?? path)],
          ["里面有什么", `${String(counts.instances ?? "?")} 个世界、${String(counts.timelines ?? "?")} 条时间线、${String(inspect.assets ?? 0)} 个素材文件`],
          ["要搬的大小", sizeText(Number(inspect.size_bytes ?? 0))],
          ["当前数据根", `${String(target.instances ?? 0)} 个世界、${String(target.assets ?? 0)} 个素材文件`],
        ]),
      );
      if (problems.length) {
        result.appendChild(paragraph(`现在还不能迁移：${problems.join("；")}`, "u-note-bad"));
        setNote(note, "检查没通过：按上面那条修完再来", "bad");
        return;
      }
      result.appendChild(
        paragraph("迁移会把这些带过来：世界、会话、版本、素材与草稿、非敏感偏好。不带过来的：API 密钥与通道凭据、进程锁与运行句柄、日志与缓存。源目录始终保留，不会移动或删除。"),
      );
      // 三步是顺序发生的，且失败会停在其中一步：画出来比「复制 → 校验 → 启用」一句话清楚
      const steps = flowRail(
        [
          { label: "检查旧目录", hint: "只读，不改任何一边的数据" },
          { label: "复制到暂存", hint: "先落到暂存区，不覆盖当前数据" },
          { label: "校验并启用", hint: "整批校验通过才切换；失败会回退并留副本" },
        ],
        0,
      );
      if (steps) result.appendChild(steps);
      result.appendChild(paragraph("迁移完成后，所有时间线处于暂停状态；要接着跑就在世界里逐条启动。", "u-hint"));
      result.appendChild(
        el(
          "div",
          { class: "u-row" },
          primary("开始迁移", () => void confirmRun(path)),
          button("换个目录", () => void choose()),
        ),
      );
      setNote(note, "检查通过：确认前不会改动任何数据", "ok");
    } catch (error) {
      const info = uiError(error, { module: "迁移", action: "检查旧目录", done: "当前数据没被改动" });
      result.appendChild(errorCard(info, [{ label: "重新检查", run: () => void check(path) }]));
      setNote(note, info.message, "bad");
    }
  };

  const confirmRun = (path: string): void => {
    const modal = dialog(
      "把这个旧目录搬进来？",
      [
        paragraph("会用旧目录里的数据替换当前数据根（现在还没有用户资产，所以不会丢东西）。"),
        paragraph("迁移前会自动留一份当前数据的完整副本；中途失败会回退并说明。开始之后没有「取消」。"),
      ],
      [
        { label: "开始迁移", run: () => void run(path) },
        { label: "取消", run: () => undefined },
      ],
    );
    document.body.appendChild(modal.node);
  };

  const run = async (path: string): Promise<void> => {
    setNote(note, "正在迁移（复制 → 校验 → 启用）…", "pending");
    try {
      const response = await ctx.api.migrateRun(path);
      const migration = (response.migration as Json) ?? {};
      const counts = (migration.counts as Json) ?? {};
      fill(result);
      fill(
        result,
        el("h3", { text: "迁移完成" }),
        facts([
          ["搬过来", `${String(counts.instances ?? "?")} 个世界、${String(counts.timelines ?? "?")} 条时间线`],
          ["时间线状态", "全部暂停"],
          ["源目录", `${String(migration.kept_source ?? path)}（原样保留）`],
          ["恢复前副本", String(migration.before ?? "")],
          ["偏好", ((migration.prefs_merged as string[]) ?? []).length ? `合并了 ${(migration.prefs_merged as string[]).length} 项（目标已有的值优先）` : "没有需要合并的"],
        ]),
        paragraph("没有搬过来的：API 密钥与通道凭据、进程锁与运行句柄、日志与缓存。", "u-hint"),
      );
      setNote(note, "迁移完成：请到世界与素材里检查世界，然后逐条启动时间线", "ok");
      await ctx.refresh();
    } catch (error) {
      const info = uiError(error, {
        module: "迁移",
        action: "迁移旧目录",
        done: "已经按记录回退或停在检查阶段，当前数据仍可读",
        unknown: "迁移是否完成",
      });
      result.appendChild(errorCard(info, [{ label: "重新检查这个目录", run: () => void check(path) }]));
      setNote(note, info.message, "bad");
    }
  };

  const choose = async (): Promise<void> => {
    try {
      const chosen = await invoke<string | null>("pick_dir", { dir: null, title: "选择旧的数据目录（里面有 data/isekai.db）" });
      if (!chosen) {
        setNote(note, "已取消", "muted");
        return;
      }
      picked = chosen;
      await check(picked);
    } catch (error) {
      setNote(note, uiError(error, { module: "迁移", action: "选择目录" }).message, "bad");
    }
  };

  const box = el("div", { class: "u-card" });
  box.appendChild(el("h3", { text: "从旧的开发目录迁移" }));
  box.appendChild(
    paragraph(
      "以前在这台或别的机器上用过 isekai 的开发版？选那个数据目录（里面有 data/isekai.db），把它整个搬过来。"
        + "源目录始终保留，不会移动或删除；API 密钥与通道凭据不随数据搬走。",
    ),
  );
  // 路径也可以直接粘：选目录按钮之外留一条手输的路（也能被验收探针驱动）
  const typed = el("input", {
    class: "u-input",
    id: "u-migrate-path",
    placeholder: "旧数据目录的完整路径（例如 D:\\isekai_data）",
  }) as HTMLInputElement;
  box.appendChild(
    el(
      "div",
      { class: "u-row" },
      button("选择旧数据目录…", () => void choose()),
      typed,
      button("检查这个目录", () => {
        const value = typed.value.trim();
        if (!value) {
          setNote(note, "先选目录或填一个路径", "bad");
          return;
        }
        picked = value;
        void check(picked);
      }),
    ),
  );
  box.appendChild(note);
  box.appendChild(result);
  return box;
}
