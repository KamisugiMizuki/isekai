/*
 * 正式界面的数据访问层：只调核心契约（管理面 op + UMP），不自己持有世界状态。
 *
 * 两条约定（读一次就够，后面所有面板都靠它）：
 * - 业务拒绝不是异常：`status: "rejected"/"waiting"/"conflict"` 原样回给界面，由面板翻译；
 *   只有结构缺失与真失败才抛 `UiError`。
 * - 错误信息按 ONBOARDING §5.1 的最小返回信息构造：所属模块、目标身份、发生阶段、
 *   稳定原因码、可重试、已完成、尚未确认、可定位字段。
 */

import { MgmtClient, MgmtError, UmpClient, newId, type Envelope } from "../ump";

export type Json = Record<string, unknown>;

export interface UiError {
  module: string;
  action: string;
  target: string;
  stage: string;
  code: string;
  message: string;
  retryable: boolean;
  done: string;
  unknown: string;
  field: string;
  requestId: string;
}

export interface ErrorContext {
  module: string;
  action: string;
  target?: string;
  field?: string;
  done?: string;
  unknown?: string;
}

const CODE_TEXT: Record<string, string> = {
  invalid_input: "这一步的输入没有通过检查",
  not_found: "找不到这个对象（可能已被删除或改动）",
  state_blocked: "当前状态不允许做这件事",
  conflict: "与当前进度冲突",
  unsupported_type: "当前版本不支持这个操作",
  generation_failed: "这次生成没有成功",
  llm_not_configured: "还没有配置 AI 服务",
  llm_rejected: "AI 服务拒绝了这次调用",
  llm_unreachable: "连不上 AI 服务地址",
  empty_completion: "模型没有返回可用内容",
  truncated_completion: "模型输出被截断",
  internal: "程序内部处理失败，已记录诊断信息",
  overloaded: "核心正忙，稍后重试",
  rate_limited: "操作太快，稍后再试",
  auth_failed: "核心拒绝了这次连接凭据",
  auth_required: "需要先完成认证",
};

export function uiError(error: unknown, ctx: ErrorContext): UiError {
  const base: UiError = {
    module: ctx.module,
    action: ctx.action,
    target: ctx.target ?? "",
    stage: "",
    code: "",
    message: "",
    retryable: false,
    done: ctx.done ?? "没有任何改动",
    unknown: ctx.unknown ?? "这次操作是否已生效",
    field: ctx.field ?? "",
    requestId: "",
  };
  if (error instanceof MgmtError) {
    base.code = error.code;
    base.retryable = error.retryable;
    const known = CODE_TEXT[error.code] ?? "这一步没有完成";
    // 底层自由文本照实带上（不臆猜），只在前面加一句能看懂的定性
    base.message = error.message && !error.message.startsWith(error.code)
      ? `${known}：${error.message}`
      : known;
    return base;
  }
  if (error instanceof Error) {
    base.code = "ui_failure";
    base.message = error.message || "这一步没有完成";
    return base;
  }
  base.code = "ui_failure";
  base.message = String(error);
  return base;
}

/** 业务拒绝信封与真失败分开：拒绝原样给面板处理，不当作错误 */
export function rejected(result: Json): boolean {
  const status = String(result.status ?? "");
  return status === "rejected" || status === "waiting" || status === "conflict" || status === "duplicate";
}

export interface InstanceEntry {
  id: string;
  name: string;
  package?: string;
  original_name?: string;
  created_at?: number;
  timelines?: Array<{ id: string; name: string; state: string }>;
  [key: string]: unknown;
}

export interface TimelineEntry {
  id: string;
  name: string;
  state: string;
  [key: string]: unknown;
}

export interface ClockView {
  state: string;
  rate?: number;
  processed_world?: number;
  target_world?: number;
  [key: string]: unknown;
}

export class AppApi {
  constructor(readonly mgmt: MgmtClient) {}

  call(op: string, args: Json = {}, timeoutMs = 30000): Promise<Json> {
    return this.mgmt.call(op, args, timeoutMs) as Promise<Json>;
  }

  /* ---------------- 本机与设置 ---------------- */

  readiness(): Promise<Json> {
    return this.call("app.readiness");
  }

  status(): Promise<Json> {
    return this.call("status");
  }

  settings(): Promise<Json> {
    return this.call("settings.get");
  }

  saveSettings(payload: Json): Promise<Json> {
    return this.call("settings.set", payload, 30000);
  }

  testAi(llm: Json): Promise<Json> {
    // 测试要真发请求：给足上限（两层小请求 + 内部重试）
    return this.call("settings.test", { llm }, 180000);
  }

  /* ---------------- 样例与世界 ---------------- */

  samples(): Promise<Json> {
    return this.call("world.sample.list");
  }

  installSample(sample: string, requestId: string): Promise<Json> {
    return this.call("world.sample.install", { sample, request_id: requestId }, 60000);
  }

  packages(): Promise<Json> {
    return this.call("world.package.list");
  }

  cards(): Promise<Json> {
    return this.call("world.card.list");
  }

  packageTemplate(name: string): Promise<Json> {
    return this.call("world.package.template", { name }, 60000);
  }

  packageLoad(path: string): Promise<Json> {
    return this.call("world.package.load", { path }, 60000);
  }

  packageSave(path: string, pkg: Json): Promise<Json> {
    return this.call("world.package.save", { path, package: pkg }, 120000);
  }

  packageValidate(payload: Json): Promise<Json> {
    return this.call("world.package.validate", payload, 120000);
  }

  /** 生成一整份设定：3–6 次调用（含重试），超时给足 */
  packageGenerate(payload: Json): Promise<Json> {
    return this.call("world.package.generate", payload, 900000);
  }

  packageRevise(payload: Json): Promise<Json> {
    return this.call("world.package.revise", payload, 900000);
  }

  packageFill(payload: Json): Promise<Json> {
    return this.call("world.package.fill", payload, 900000);
  }

  // 角色卡这类 op 的路径参数名是 card_path（不是 path）：两处都用错会拿「缺少路径」
  cardLoad(path: string): Promise<Json> {
    return this.call("world.card.load", { card_path: path }, 60000);
  }

  cardSave(path: string, card: Json): Promise<Json> {
    return this.call("world.card.save", { card_path: path, card }, 120000);
  }

  cardValidate(args: Json): Promise<Json> {
    return this.call("world.card.validate", args, 120000);
  }

  /** 角色卡起草：base 给定时是「整卡重跑」，sections 给定时是字段级重跑 */
  cardGenerate(payload: Json): Promise<Json> {
    return this.call("world.card.generate", payload, 900000);
  }

  cardConfirm(args: Json): Promise<Json> {
    return this.call("world.card.confirm", args, 120000);
  }

  instances(): Promise<Json> {
    return this.call("instance.list");
  }

  instanceInfo(id: string): Promise<Json> {
    return this.call("instance.info", { id });
  }

  createInstance(args: {
    package_path?: string;
    card_paths?: string[];
    display_name?: string;
    request_id?: string;
  }): Promise<Json> {
    return this.call("instance.create", args, 60000);
  }

  renameInstance(id: string, name: string): Promise<Json> {
    return this.call("instance.rename", { id, name });
  }

  deleteInstance(id: string, confirm: boolean): Promise<Json> {
    return this.call("instance.delete", { id, confirm }, 60000);
  }

  exportInstance(id: string, path: string): Promise<Json> {
    return this.call("instance.export", { id, path }, 60000);
  }

  importInstance(path: string): Promise<Json> {
    return this.call("instance.import", { path }, 60000);
  }

  /* ---------------- 运行与版本 ---------------- */

  clock(instanceId: string, timelineId: string): Promise<Json> {
    return this.call("runtime.clock", { instance_id: instanceId, timeline_id: timelineId });
  }

  activate(instanceId: string, timelineId: string, rate?: number): Promise<Json> {
    return this.call("runtime.activate", { instance_id: instanceId, timeline_id: timelineId, rate }, 60000);
  }

  freeze(instanceId: string, timelineId: string): Promise<Json> {
    return this.call("runtime.freeze", { instance_id: instanceId, timeline_id: timelineId }, 60000);
  }

  setRate(instanceId: string, timelineId: string, rate: number): Promise<Json> {
    return this.call("runtime.rate", { instance_id: instanceId, timeline_id: timelineId, rate });
  }

  advances(instanceId: string, timelineId: string): Promise<Json> {
    return this.call("runtime.advance", { instance_id: instanceId, timeline_id: timelineId }, 60000);
  }

  commits(instanceId: string, timelineId: string): Promise<Json> {
    return this.call("runtime.commits", { instance_id: instanceId, timeline_id: timelineId });
  }

  saveVersion(instanceId: string, timelineId: string, note: string): Promise<Json> {
    return this.call("runtime.commit", { instance_id: instanceId, timeline_id: timelineId, note }, 60000);
  }

  forkTimeline(instanceId: string, timelineId: string, commitId: string, name: string): Promise<Json> {
    return this.call(
      "runtime.fork",
      { instance_id: instanceId, timeline_id: timelineId, commit_id: commitId, name },
      60000,
    );
  }

  rollback(instanceId: string, timelineId: string, commitId: string): Promise<Json> {
    return this.call(
      "runtime.rollback",
      { instance_id: instanceId, timeline_id: timelineId, commit_id: commitId, confirm: true },
      60000,
    );
  }

  renameTimeline(instanceId: string, timelineId: string, name: string): Promise<Json> {
    return this.call("runtime.timeline.rename", { instance_id: instanceId, timeline_id: timelineId, name });
  }

  archiveTimeline(instanceId: string, timelineId: string): Promise<Json> {
    return this.call("runtime.timeline.archive", { instance_id: instanceId, timeline_id: timelineId }, 60000);
  }

  deleteTimeline(instanceId: string, timelineId: string, confirm: boolean): Promise<Json> {
    return this.call(
      "runtime.timeline.delete",
      { instance_id: instanceId, timeline_id: timelineId, confirm },
      60000,
    );
  }

  budget(instanceId: string): Promise<Json> {
    return this.call("runtime.budget", { instance_id: instanceId });
  }

  /* ---------------- 会话与历史 ---------------- */

  sessionEnsure(instanceId: string, timelineId: string, characterId: string): Promise<Json> {
    return this.call("session.ensure", {
      instance_id: instanceId,
      timeline_id: timelineId,
      character_id: characterId,
    });
  }

  sessions(): Promise<Json> {
    return this.call("session.list");
  }

  ensureChannel(name: string, displayName: string, rotate = false): Promise<Json> {
    return this.call("channel.ensure", {
      name,
      display_name: displayName,
      version: "0.1.0",
      rotate,
    });
  }

  bindThread(channel: string, threadId: string, sessionId: string): Promise<Json> {
    return this.call("thread.bind", { channel, thread_id: threadId, session_id: sessionId });
  }

  history(sessionId: string, beforeSeq?: number, limit = 50): Promise<Json> {
    return this.call("history.page", {
      session_id: sessionId,
      limit,
      ...(beforeSeq ? { before_seq: beforeSeq } : {}),
    });
  }

  /* ---------------- OC 故事层 ---------------- */

  storyEnter(args: { instance_id?: string; timeline_id?: string; character_id?: string }): Promise<Json> {
    return this.call("story.enter", args);
  }

  storyHome(instanceId: string, timelineId: string, characterId: string): Promise<Json> {
    return this.call("story.home", {
      instance_id: instanceId,
      timeline_id: timelineId,
      character_id: characterId,
    });
  }

  storyScene(instanceId: string, timelineId: string, characterId: string): Promise<Json> {
    return this.call("story.scene", {
      instance_id: instanceId,
      timeline_id: timelineId,
      character_id: characterId,
    });
  }

  storyTurn(instanceId: string, timelineId: string, characterId: string, seq?: number): Promise<Json> {
    return this.call("story.turn", {
      instance_id: instanceId,
      timeline_id: timelineId,
      character_id: characterId,
      ...(seq ? { seq } : {}),
    });
  }

  storyBranch(instanceId: string, timelineId: string, commitId: string, name: string): Promise<Json> {
    return this.call(
      "story.branch",
      { instance_id: instanceId, timeline_id: timelineId, commit_id: commitId, name },
      60000,
    );
  }

  storyRestore(instanceId: string, timelineId: string, commitId: string, confirm: boolean, saved: boolean): Promise<Json> {
    return this.call(
      "story.restore",
      { instance_id: instanceId, timeline_id: timelineId, commit_id: commitId, confirm, saved },
      60000,
    );
  }

  /* ---------------- 线索与转述 ---------------- */

  narrativeMap(instanceId: string, timelineId: string, characterId: string): Promise<Json> {
    return this.call("narrative.map", {
      instance_id: instanceId,
      timeline_id: timelineId,
      card_id: characterId,
    });
  }

  /** 尝试世界变化的表单可选对象（已登记对象 + 可读名称；界面不要求填标识） */
  eventTargets(instanceId: string, timelineId: string): Promise<Json> {
    return this.call("world.event.targets", { instance_id: instanceId, timeline_id: timelineId });
  }

  /** 草案：只翻译与校验，确认前不产生任何世界变化 */
  eventDraft(instanceId: string, timelineId: string, payload: Json): Promise<Json> {
    return this.call("event.draft", { instance_id: instanceId, timeline_id: timelineId, payload }, 180000);
  }

  /** 确认：原子建一条新线并注入（新线先暂停） */
  eventConfirm(instanceId: string, draftId: string, name: string): Promise<Json> {
    return this.call("event.confirm", { instance_id: instanceId, draft_id: draftId, name }, 60000);
  }

  /** 跨角色披露的候选：只把**她讲过**的片段挑出来摆着（默认隔离不变） */
  discloseSuggest(
    instanceId: string,
    timelineId: string,
    fromCharacter: string,
    toCharacter: string,
    limit = 5,
  ): Promise<Json> {
    return this.call("disclose.suggest", {
      instance_id: instanceId,
      timeline_id: timelineId,
      from_character: fromCharacter,
      to_character: toCharacter,
      limit,
    });
  }

  /** 授权必须明确到具体消息（refs = 已经显示过的整条消息），不做截短授权 */
  discloseConfirm(
    instanceId: string,
    timelineId: string,
    fromCharacter: string,
    toCharacter: string,
    refs: string[],
    note = "",
  ): Promise<Json> {
    return this.call("disclose.confirm", {
      instance_id: instanceId,
      timeline_id: timelineId,
      from_character: fromCharacter,
      to_character: toCharacter,
      refs,
      note,
    });
  }

  discloseList(instanceId: string, timelineId: string, toCharacter = ""): Promise<Json> {
    return this.call("disclose.list", {
      instance_id: instanceId,
      timeline_id: timelineId,
      ...(toCharacter ? { to_character: toCharacter } : {}),
    });
  }

  /* ---------------- 提醒与备份 ---------------- */

  notices(instanceId?: string): Promise<Json> {
    return this.call("notice.list", instanceId ? { instance_id: instanceId } : {});
  }

  resolveNotice(id: string): Promise<Json> {
    return this.call("notice.resolve", { id });
  }

  backups(): Promise<Json> {
    return this.call("backup.list");
  }

  /** 单文件全量备份（§9.1）：列出、打包、校验、暂存、切换 */
  packList(): Promise<Json> {
    return this.call("backup.pack.list", {});
  }

  packCreate(note = ""): Promise<Json> {
    return this.call("backup.pack.create", { kind: "manual", note }, 600000);
  }

  packVerify(path: string): Promise<Json> {
    return this.call("backup.pack.verify", { path }, 600000);
  }

  packStage(path: string): Promise<Json> {
    return this.call("backup.pack.stage", { path }, 600000);
  }

  /** 从旧的开发目录迁移（§3.2）：检查是只读的 */
  migrateInspect(path: string): Promise<Json> {
    return this.call("migrate.inspect", { path }, 120000);
  }

  migrateRun(path: string, note = "从旧开发目录迁移"): Promise<Json> {
    return this.call("migrate.run", { path, note }, 900000);
  }

  packApply(staged: string, note = ""): Promise<Json> {
    return this.call("backup.pack.apply", { staged, note }, 600000);
  }

  backupNow(note = ""): Promise<Json> {
    return this.call("backup.create", { note }, 120000);
  }

  backupRestore(path: string): Promise<Json> {
    return this.call("backup.restore", { path, confirm: true }, 180000);
  }

  /* ---------------- 辅助写作（USER_INTERFACE_DESIGN §7） ---------------- */

  waOutlines(): Promise<Json> {
    return this.call("wa.outline.list", {});
  }

  waOutlineGet(id: string): Promise<Json> {
    return this.call("wa.outline.get", { id });
  }

  waOutlineSave(outline: Json): Promise<Json> {
    return this.call("wa.outline.save", { outline });
  }

  waBind(args: Json): Promise<Json> {
    return this.call("wa.bind", args);
  }

  waState(instanceId: string, timelineId: string, outlineId: string): Promise<Json> {
    return this.call("wa.state", { instance_id: instanceId, timeline_id: timelineId, outline_id: outlineId });
  }

  waEvaluate(instanceId: string, timelineId: string, outlineId: string): Promise<Json> {
    return this.call("wa.evaluate", { instance_id: instanceId, timeline_id: timelineId, outline_id: outlineId });
  }

  waItemDecide(args: Json): Promise<Json> {
    return this.call("wa.item.decide", args);
  }

  waObserve(args: Json): Promise<Json> {
    return this.call("wa.observe", args, 60000);
  }

  waSuggest(args: Json): Promise<Json> {
    return this.call("wa.suggest", args, 180000);
  }

  waCandidatePropose(args: Json, instanceId: string, timelineId: string, outlineId: string): Promise<Json> {
    return this.call("wa.candidate.propose", {
      instance_id: instanceId, timeline_id: timelineId, outline_id: outlineId, ...args,
    });
  }

  waCandidateDecide(args: Json, instanceId: string, timelineId: string): Promise<Json> {
    return this.call("wa.candidate.decide", { instance_id: instanceId, timeline_id: timelineId, ...args });
  }

  waCandidateCommit(candidateId: string, instanceId: string, timelineId: string): Promise<Json> {
    return this.call(
      "wa.candidate.commit",
      { instance_id: instanceId, timeline_id: timelineId, candidate_id: candidateId },
      120000,
    );
  }

  waTextLock(locked: boolean, instanceId: string, timelineId: string, candidateId: string): Promise<Json> {
    return this.call(locked ? "wa.text.lock" : "wa.text.unlock", {
      instance_id: instanceId, timeline_id: timelineId, candidate_id: candidateId,
    });
  }

  waBranch(args: Json): Promise<Json> {
    return this.call("wa.branch", args, 120000);
  }

  /* ---------------- 跑团（USER_INTERFACE_DESIGN §8） ---------------- */

  trpgClient(op: string, args: Json): Promise<Json> {
    return this.call(`trpg.client.${op}`, args, 180000);
  }

  trpgCampaigns(instanceId: string, timelineId = ""): Promise<Json> {
    return this.call("trpg.campaign.list", {
      instance_id: instanceId, ...(timelineId ? { timeline_id: timelineId } : {}),
    });
  }

  trpgCampaignInfo(instanceId: string, timelineId: string, campaignId: string): Promise<Json> {
    return this.call("trpg.campaign.info", {
      instance_id: instanceId, timeline_id: timelineId, campaign_id: campaignId,
    });
  }

  trpgCampaignCreate(args: Json): Promise<Json> {
    return this.call("trpg.campaign.create", args, 60000);
  }

  trpgCampaignStatus(instanceId: string, timelineId: string, campaignId: string, status: string): Promise<Json> {
    return this.call("trpg.campaign.status", {
      instance_id: instanceId, timeline_id: timelineId, campaign_id: campaignId, status,
    });
  }

  trpgSceneOpen(args: Json): Promise<Json> {
    return this.call("trpg.scene.open", args);
  }

  /* ---------------- 规则插件登记（§8.5，与通道插件 plugin.* 分开） ---------------- */

  rulesList(): Promise<Json> {
    return this.call("rules.list", {});
  }

  rulesScan(dir: string): Promise<Json> {
    return this.call("rules.scan", { dir });
  }

  rulesRegister(manifestPath: string): Promise<Json> {
    return this.call("rules.register", { manifest_path: manifestPath });
  }

  rulesEnable(enabled: boolean, rulesetId: string, rulesetVersion: string): Promise<Json> {
    return this.call(enabled ? "rules.enable" : "rules.disable", {
      ruleset_id: rulesetId, ruleset_version: rulesetVersion,
    });
  }

  rulesRemove(rulesetId: string, rulesetVersion: string): Promise<Json> {
    return this.call("rules.remove", { ruleset_id: rulesetId, ruleset_version: rulesetVersion });
  }

  /* ---------------- 界面草稿（§3.5） ---------------- */

  draftSave(key: string, module: string, target: string, textValue: string, payload?: unknown): Promise<Json> {
    return this.call("ui.draft.save", { key, module, target, text: textValue, payload, state: "saved" });
  }

  draftLoad(key: string): Promise<Json> {
    return this.call("ui.draft.load", { key });
  }

  draftList(module?: string): Promise<Json> {
    return this.call("ui.draft.list", module ? { module } : {});
  }

  draftDiscard(key: string): Promise<Json> {
    return this.call("ui.draft.discard", { key });
  }
}

/* ------------------------------------------------------------------ UMP 通道 */

export type ChannelEvent =
  | {
      kind: "reply";
      messageId: string;
      parts: string[];
      batchIndex: number;
      batchCount: number;
      replyTo: string;
      at: number;
    }
  | { kind: "notice"; text: string; messageId: string; at: number }
  | { kind: "accepted"; ref: string; state: string; messageId: string }
  | { kind: "status"; state: string }
  | { kind: "error"; code: string; message: string; ref: string; retryable: boolean; stage: string }
  | { kind: "binding"; state: string };

export interface ThreadLink {
  channel: string;
  threadId: string;
  token: string;
  sessionId: string;
}

/**
 * 一个角色的联络连接：通道登记 → 会话 → 线程绑定 → UMP 握手。
 *
 * 只服务「角色联络」工作区：发送、收回复、投递回执。切换角色 / 时间线时重建，
 * 不让上一条连接的回执落进新会话（§3.3「不把旧请求返回结果画到新对象下」）。
 */
export class ChannelLink {
  private client: UmpClient | null = null;
  private listeners = new Set<(event: ChannelEvent) => void>();
  link: ThreadLink | null = null;

  constructor(private readonly endpoint: string, private readonly channelName = "builtin") {}

  onEvent(fn: (event: ChannelEvent) => void): void {
    this.listeners.add(fn);
  }

  private emit(event: ChannelEvent): void {
    for (const listener of [...this.listeners]) listener(event);
  }

  async open(api: AppApi, instanceId: string, timelineId: string, characterId: string): Promise<ThreadLink> {
    this.close();
    const issued = await api.ensureChannel(this.channelName, "isekai 桌面");
    // 缓存里的那份凭据可能属于另一个数据根（换根 / 恢复备份之后）：它只会一直失败，所以失败就轮换重试一次
    let credential = (issued.credential as string | null) ?? localStorage.getItem(`isekai.credential.${this.channelName}`);
    if (!credential) credential = await this.rotateCredential(api);
    const session = (await api.sessionEnsure(instanceId, timelineId, characterId)).session as Json;
    const sessionId = String(session.id);
    const threadId = this.threadId(instanceId, timelineId, characterId);
    let client = this.buildClient();
    let ack: Record<string, unknown>;
    try {
      ack = await client.connect({ credential, bootstrap: null });
    } catch (error) {
      localStorage.removeItem(`isekai.credential.${this.channelName}`);
      const rotated = await this.rotateCredential(api);
      if (!rotated) throw error;
      client.close();
      client = this.buildClient();
      ack = await client.connect({ credential: rotated, bootstrap: null });
    }
    // 握手回带本通道已有的 thread 令牌（§2.2）：重连直接沿用，不重绑——
    // 重绑会换代表令，已投递消息的回执会全部对不上（表现是「回执的绑定令牌与固化时不一致」）
    const threads = (ack.threads as Array<{ id: string; binding_token: string }>) ?? [];
    let token = threads.find((item) => item.id === threadId)?.binding_token;
    if (!token) {
      const bound = (await api.bindThread(this.channelName, threadId, sessionId)).thread as Json;
      token = String(bound.binding_token ?? "");
    }
    this.client = client;
    this.link = {
      channel: this.channelName,
      threadId,
      token,
      sessionId,
    };
    return this.link;
  }

  private buildClient(): UmpClient {
    const client = new UmpClient(this.endpoint, this.channelName, "isekai 桌面");
    client.onMessage((env) => this.onEnvelope(env));
    return client;
  }

  /** 显式轮换通道凭据并记住（界面是这条通道的唯一使用者，轮换不会踢掉别人） */
  private async rotateCredential(api: AppApi): Promise<string> {
    const issued = await api.ensureChannel(this.channelName, "isekai 桌面", true);
    const credential = String(issued.credential ?? "");
    if (credential) localStorage.setItem(`isekai.credential.${this.channelName}`, credential);
    return credential;
  }

  private threadId(instanceId: string, timelineId: string, characterId: string): string {
    // 线程标识按「角色」固定：一个角色一条会话（不跟时间线走，换线要重新绑定）
    return `${instanceId}:${timelineId}:${characterId}`.slice(0, 120) || "main";
  }

  private onEnvelope(env: Envelope): void {
    const payload = (env.payload ?? {}) as Json;
    if (env.type === "reply") {
      const parts = ((payload.parts as Array<{ text?: string }>) ?? []).map((part) => String(part.text ?? ""));
      this.emit({
        kind: "reply",
        messageId: String(payload.message_id ?? ""),
        parts,
        batchIndex: Number(payload.batch_index ?? 0),
        batchCount: Number(payload.batch_count ?? 1),
        replyTo: String(payload.reply_to ?? ""),
        at: Number(env.ts ?? Date.now() / 1000),
      });
      return;
    }
    if (env.type === "system_notice") {
      this.emit({
        kind: "notice",
        text: String(payload.text ?? ""),
        messageId: String(payload.message_id ?? ""),
        at: Number(env.ts ?? Date.now() / 1000),
      });
      return;
    }
    if (env.type === "accepted") {
      this.emit({
        kind: "accepted",
        ref: String(payload.ref ?? ""),
        state: String(payload.state ?? ""),
        messageId: String(payload.message_id ?? ""),
      });
      return;
    }
    if (env.type === "status") {
      this.emit({ kind: "status", state: String(payload.state ?? "") });
      return;
    }
    if (env.type === "binding") {
      if (this.link && String(payload.thread_id ?? "") === this.link.threadId && String(payload.state) === "active") {
        this.link = { ...this.link, token: String(payload.binding_token ?? this.link.token) };
      }
      this.emit({ kind: "binding", state: String(payload.state ?? "") });
      return;
    }
    if (env.type === "error") {
      this.emit({
        kind: "error",
        code: String(payload.code ?? ""),
        message: String(payload.message ?? ""),
        ref: String(payload.ref ?? env.id ?? ""),
        retryable: Boolean(payload.retryable ?? false),
        stage: String(payload.stage ?? ""),
      });
    }
  }

  /** 发送一条联络：返回这次请求的身份（界面按它查询结果，不重复发） */
  send(textValue: string): string {
    if (!this.client || !this.link) throw new Error("还没有连上这个角色");
    return this.client.userMessage(this.link.threadId, this.link.token, textValue);
  }

  confirmDelivery(messageId: string, batchIndex: number, state: string): void {
    if (!this.client || !this.link) return;
    this.client.reportDelivery(this.link.threadId, this.link.token, messageId, batchIndex, state);
  }

  retry(ref: string, kindName?: string): void {
    if (!this.client || !this.link) return;
    this.client.retry(this.link.threadId, this.link.token, ref, kindName);
  }

  close(): void {
    this.client?.close();
    this.client = null;
    this.link = null;
  }
}

export function newRequestId(prefix: string): string {
  return newId(prefix);
}

/** 消息节（面板共用的重试/查询身份） */
export interface PendingRequest {
  id: string;
  text: string;
  startedAt: number;
  state: "submitting" | "accepted" | "unknown" | "failed";
  error?: UiError;
}
