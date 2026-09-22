/*
 * UMP v1 客户端（TypeScript）：与 Python 侧 isekai_core/ump.py 同一语义。
 * 握手、消息、回执、重试走 UMP；会话选择 / 绑定 / 历史 / 设置走受信管理面。
 */

export interface Envelope {
  ump: string;
  type: string;
  id: string;
  ts: number;
  thread?: { id: string; binding_token?: string };
  payload: Record<string, unknown>;
}

export function newId(prefix: string): string {
  return `${prefix}-${Math.random().toString(16).slice(2, 12)}`;
}

export function makeEnvelope(
  type: string,
  payload: Record<string, unknown>,
  thread?: { id: string; token?: string },
): Envelope {
  const env: Envelope = { ump: "1.0", type, id: newId("e"), ts: Date.now() / 1000, payload };
  if (thread) {
    env.thread = { id: thread.id };
    if (thread.token) env.thread.binding_token = thread.token;
  }
  return env;
}

type Waiter = { predicate: (env: Envelope) => boolean; resolve: (env: Envelope) => void };

/** 管理面错误：把「稳定原因码 + 安全短说明 + 是否可重试」带到界面层（ONBOARDING §5.1）。 */
export class MgmtError extends Error {
  readonly code: string;
  readonly retryable: boolean;

  constructor(code: string, message: string, retryable = false) {
    super(message);
    this.name = "MgmtError";
    this.code = code;
    this.retryable = retryable;
  }
}

export class UmpClient {
  private ws: WebSocket | null = null;
  private waiters: Waiter[] = [];
  private listeners = new Set<(env: Envelope) => void>();
  private closeHandlers = new Set<() => void>();
  helloAck: Record<string, unknown> | null = null;
  negotiated: Record<string, number | boolean> = {};

  constructor(
    private readonly endpoint: string,
    private readonly channelId = "builtin",
    private readonly name = "内建聊天窗口",
  ) {}

  onMessage(fn: (env: Envelope) => void): void {
    this.listeners.add(fn);
  }

  /** 连接断开（非本地主动关闭）：客户端据此做有界退避重连（§六）。 */
  onClose(fn: () => void): void {
    this.closeHandlers.add(fn);
  }

  async connect(opts: { credential?: string | null; bootstrap?: string | null }): Promise<Record<string, unknown>> {
    const ws = new WebSocket(this.endpoint);
    await new Promise<void>((resolve, reject) => {
      ws.onopen = () => resolve();
      ws.onerror = () => reject(new Error("无法连接核心端点"));
    });
    this.ws = ws;
    ws.onmessage = (event) => {
      let env: Envelope;
      try {
        env = JSON.parse(String(event.data)) as Envelope;
      } catch {
        return;
      }
      for (const waiter of [...this.waiters]) {
        if (waiter.predicate(env)) {
          this.waiters.splice(this.waiters.indexOf(waiter), 1);
          waiter.resolve(env);
        }
      }
      for (const listener of this.listeners) listener(env);
    };
    ws.onclose = () => {
      for (const handler of this.closeHandlers) handler();
    };

    const auth: Record<string, string> = {};
    if (opts.credential) auth.credential = opts.credential;
    else if (opts.bootstrap) auth.bootstrap = opts.bootstrap;
    this.send(
      makeEnvelope("hello", {
        channel: { id: this.channelId, name: this.name, version: "0.1.0" },
        capabilities: { segments: true, status: true, max_text_len: 4000, max_parts: 10 },
        auth,
      }),
    );
    const ack = await this.expect((env) => env.type === "hello_ack" || env.type === "error", 20000);
    if (ack.type === "error") throw new Error(String(ack.payload.message ?? "握手失败"));
    this.helloAck = ack.payload;
    this.negotiated = (ack.payload.negotiated ?? {}) as Record<string, number | boolean>;
    return ack.payload;
  }

  expect(predicate: (env: Envelope) => boolean, timeoutMs = 60000): Promise<Envelope> {
    return new Promise((resolve, reject) => {
      const waiter: Waiter = { predicate, resolve };
      this.waiters.push(waiter);
      setTimeout(() => {
        const index = this.waiters.indexOf(waiter);
        if (index >= 0) {
          this.waiters.splice(index, 1);
          reject(new Error("等待核心响应超时"));
        }
      }, timeoutMs);
    });
  }

  send(env: Envelope): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) throw new Error("未连接核心");
    this.ws.send(JSON.stringify(env));
  }

  userMessage(threadId: string, token: string, text: string): string {
    const env = makeEnvelope("user_message", { text }, { id: threadId, token });
    this.send(env);
    return env.id;
  }

  retry(threadId: string, token: string, ref: string, kind?: string): void {
    const payload: Record<string, unknown> = { ref };
    if (kind) payload.kind = kind;
    this.send(makeEnvelope("retry", payload, { id: threadId, token }));
  }

  reportDelivery(threadId: string, token: string, messageId: string, batchIndex: number, state: string): void {
    this.send(
      makeEnvelope("delivery", { message_id: messageId, batch_index: batchIndex, state }, { id: threadId, token }),
    );
  }

  close(): void {
    this.closeHandlers.clear(); // 主动关闭不触发重连
    this.ws?.close();
    this.ws = null;
  }
}

export class MgmtClient {
  private ws: WebSocket | null = null;
  private counter = 0;
  private pending = new Map<string, (frame: Record<string, unknown>) => void>();
  info: Record<string, unknown> = {};

  constructor(
    private readonly endpoint: string,
    private readonly token: string,
  ) {}

  async connect(): Promise<Record<string, unknown>> {
    const ws = new WebSocket(this.endpoint);
    await new Promise<void>((resolve, reject) => {
      ws.onopen = () => resolve();
      ws.onerror = () => reject(new Error("无法连接核心端点"));
    });
    this.ws = ws;
    ws.onmessage = (event) => {
      let frame: Record<string, unknown>;
      try {
        frame = JSON.parse(String(event.data)) as Record<string, unknown>;
      } catch {
        return;
      }
      const resolve = this.pending.get(String(frame.id));
      if (resolve) {
        this.pending.delete(String(frame.id));
        resolve(frame);
      }
    };
    const reply = await this.send({ mgmt: "1", op: "auth", id: "r-0", args: { token: this.token } });
    if (!reply.ok) throw new Error(String((reply.error as { message?: string })?.message ?? "管理认证失败"));
    this.info = (reply.result ?? {}) as Record<string, unknown>;
    return this.info;
  }

  private send(frame: Record<string, unknown>, timeoutMs = 30000): Promise<Record<string, unknown>> {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error("未连接核心"));
        return;
      }
      const id = String(frame.id ?? "");
      this.pending.set(id, resolve);
      this.ws.send(JSON.stringify(frame));
      setTimeout(() => {
        if (this.pending.delete(id)) reject(new Error("管理面响应超时"));
      }, timeoutMs);
    });
  }

  async call(op: string, args: Record<string, unknown> = {}, timeoutMs = 30000): Promise<Record<string, unknown>> {
    this.counter += 1;
    const reply = await this.send({ mgmt: "1", op, id: `r-${this.counter}`, args }, timeoutMs);
    if (!reply.ok) {
      const error = reply.error as { code?: string; message?: string; retryable?: boolean } | undefined;
      throw new MgmtError(
        error?.code ?? "mgmt_error",
        error?.message ?? "管理操作失败",
        Boolean(error?.retryable),
      );
    }
    return (reply.result ?? {}) as Record<string, unknown>;
  }

  close(): void {
    this.ws?.close();
    this.ws = null;
  }
}
