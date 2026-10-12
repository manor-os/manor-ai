// ManorWorker — high-level user surface (TS port of packages/worker_sdk/worker.py).

import { ManorClient, ManorClientOptions } from "./client.js";
import {
  HeartbeatActiveLease,
  HeartbeatCompletedLease,
  HeartbeatRequest,
  Lease,
  LeaseKind,
  LeaseResult,
  LeaseResultFactory,
  NeedHumanInput,
  NoHandlerError,
  WorkerClientError,
  WorkerState,
} from "./types.js";

const LEASE_ATTEMPT_TOKEN_CAPABILITY = "lease_attempt_token_version";
const LEASE_ATTEMPT_TOKEN_VERSION = 1;

export interface LeaseContext {
  readonly lease: Lease;
  /**
   * Push a progress update + lease extension. Use sparingly — frequent
   * extensions show up as `extended_count` in audit.
   */
  progress(fraction: number): Promise<void>;
}

// A declared output contract may be any JSON value, not only an object.
export type HandlerReturn = unknown;

export type LeaseHandler = (
  lease: Lease,
  ctx: LeaseContext,
) => Promise<HandlerReturn>;

export interface ManorWorkerOptions {
  endpoint: string;
  workerId: string;
  secret: string;
  maxConcurrentLeases?: number;
  capabilities?: Record<string, unknown> | null;
  client?: ManorClient;
  /** Callback for SDK-internal log lines. Defaults to console. */
  logger?: { info: (m: string, ...a: unknown[]) => void; warn: (m: string, ...a: unknown[]) => void; error: (m: string, ...a: unknown[]) => void };
  /** Override client construction args (timeout, fetch). Ignored when `client` provided. */
  clientOptions?: Partial<ManorClientOptions>;
}

interface HandlerKey {
  kind: LeaseKind;
  provider?: string | null;
}

export class ManorWorker {
  private readonly client: ManorClient;
  private readonly maxConcurrent: number;
  private capabilities: Record<string, unknown> | null;
  private readonly handlers = new Map<string, LeaseHandler>();
  private readonly active = new Map<string, Promise<void>>();
  private readonly attemptTokens = new Map<string, string>();
  // Immutable terminal reports. A batch remains here until the heartbeat
  // carrying that exact snapshot receives an acknowledgement.
  private completions: HeartbeatCompletedLease[] = [];
  private readonly stop = new AbortController();
  private readonly log: NonNullable<ManorWorkerOptions["logger"]>;

  constructor(opts: ManorWorkerOptions) {
    this.client =
      opts.client ??
      new ManorClient({
        endpoint: opts.endpoint,
        workerId: opts.workerId,
        secret: opts.secret,
        ...opts.clientOptions,
    });
    this.maxConcurrent = opts.maxConcurrentLeases ?? 1;
    this.capabilities = opts.capabilities ? { ...opts.capabilities } : null;
    this.log = opts.logger ?? {
      info: (m, ...a) => console.log(`[manor] ${m}`, ...a),
      warn: (m, ...a) => console.warn(`[manor] ${m}`, ...a),
      error: (m, ...a) => console.error(`[manor] ${m}`, ...a),
    };
  }

  // ── Handler registration ─────────────────────────────────────────────

  /**
   * Register a handler for `(kind, provider)`. Most-specific wins:
   * `(kind, provider)` beats `(kind, undefined)` — register a wildcard
   * handler by passing `provider: undefined`.
   */
  handle(key: HandlerKey, fn: LeaseHandler): this {
    this.handlers.set(handlerKey(key.kind, key.provider ?? null), fn);
    return this;
  }

  findHandler(lease: Lease): LeaseHandler | undefined {
    return (
      this.handlers.get(handlerKey(lease.kind, lease.provider)) ??
      this.handlers.get(handlerKey(lease.kind, null))
    );
  }

  updateCapabilities(capabilities?: Record<string, unknown> | null): void {
    this.capabilities = capabilities ? { ...capabilities } : null;
  }

  private reportedCapabilities(): Record<string, unknown> {
    return {
      ...(this.capabilities ?? {}),
      [LEASE_ATTEMPT_TOKEN_CAPABILITY]: LEASE_ATTEMPT_TOKEN_VERSION,
    };
  }

  private queueCompletion(completion: HeartbeatCompletedLease): boolean {
    const existing = this.completions.find(
      (pending) => pending.lease_id === completion.lease_id,
    );
    if (existing) {
      this.log.error(
        `conflicting or duplicate terminal report ignored for lease ${completion.lease_id}`,
      );
      return false;
    }
    this.completions.push(structuredClone(completion));
    return true;
  }

  private hasQueuedCompletion(leaseId: string): boolean {
    return this.completions.some((completion) => completion.lease_id === leaseId);
  }

  private acknowledgeCompletions(completedBatch: HeartbeatCompletedLease[]): void {
    const acknowledged = new Set(completedBatch);
    this.completions = this.completions.filter(
      (completion) => !acknowledged.has(completion),
    );
  }

  private successCompletion(
    lease: Lease,
    result: LeaseResult,
  ): HeartbeatCompletedLease {
    const completion: HeartbeatCompletedLease = {
      lease_id: lease.lease_id,
      attempt_token: lease.attempt_token,
      status: "done",
      task_output_value_kind: result.task_output_value_kind ?? "task_payload",
    };
    if (Object.prototype.hasOwnProperty.call(result, "result")) {
      completion.result = result.result;
    }
    if (Object.prototype.hasOwnProperty.call(result, "cost")) {
      completion.cost = result.cost;
    }
    if (Object.prototype.hasOwnProperty.call(result, "evidence_refs")) {
      completion.evidence_refs = result.evidence_refs;
    }
    return completion;
  }

  private async reportSuccess(lease: Lease, result: LeaseResult): Promise<void> {
    try {
      await this.client.completeLease(lease.lease_id, result, {
        attemptToken: lease.attempt_token,
      });
    } catch (exc) {
      // The handler already succeeded. Never turn an ambiguous completion
      // transport into a contradictory failure; heartbeat retries the exact
      // immutable terminal report.
      this.log.warn(
        `completeLease report failed for lease ${lease.lease_id}; queued for heartbeat retry: ${errMsg(exc)}`,
      );
    }
  }

  private async reportFailure(
    lease: Lease,
    error: Record<string, unknown>,
    opts?: { willRetry?: boolean },
  ): Promise<void> {
    try {
      await this.client.failLease(lease.lease_id, error, {
        willRetry: opts?.willRetry,
        attemptToken: lease.attempt_token,
      });
    } catch (exc) {
      this.log.warn(
        `failLease report failed for lease ${lease.lease_id}; queued for heartbeat retry: ${errMsg(exc)}`,
      );
    }
  }

  // ── Run loop ─────────────────────────────────────────────────────────

  async runForever(): Promise<void> {
    this.installSignalHandlers();
    try {
      await this.loop();
    } finally {
      await this.drainThenClose();
    }
  }

  /** Programmatic shutdown — drain in-flight leases then exit. */
  async shutdown(): Promise<void> {
    this.stop.abort();
  }

  private async loop(): Promise<void> {
    let nextHeartbeatIn = 2;
    let lastHeartbeatIn = nextHeartbeatIn;
    while (!this.stop.signal.aborted) {
      try {
        nextHeartbeatIn = await this.tick();
        lastHeartbeatIn = nextHeartbeatIn;
      } catch (exc) {
        if (this.stop.signal.aborted) break;
        if (
          exc instanceof WorkerClientError &&
          exc.requiresOperatorAction
        ) {
          this.log.error(
            exc.statusCode === 426
              ? "worker protocol registration rejected (426); re-register this worker before restarting"
              : `auth failed (${exc.statusCode}) — exiting`,
          );
          this.stop.abort();
          break;
        }
        if (exc instanceof WorkerClientError) {
          this.log.warn(`heartbeat failed (${exc.message}) — backing off`);
          nextHeartbeatIn = Math.max(lastHeartbeatIn, 10, exc.retryAfterMs / 1000) + Math.random() * 2;
        } else {
          this.log.error("unexpected loop failure — backing off", exc);
          nextHeartbeatIn = Math.max(lastHeartbeatIn, 30) + Math.random() * 5;
        }
      }
      await sleepOrAbort(nextHeartbeatIn * 1000, this.stop.signal);
    }
  }

  private async tick(): Promise<number> {
    // Snapshot without removing. Handlers may append while heartbeat I/O is
    // in flight; only this exact batch is removed after the response.
    const completedBatch = [...this.completions];
    const pendingCompletionIds = new Set(
      this.completions.map((completion) => completion.lease_id),
    );
    const active: HeartbeatActiveLease[] = [...this.active.keys()]
      .filter((lease_id) => !pendingCompletionIds.has(lease_id))
      .map((lease_id) => ({
        lease_id,
        attempt_token: this.attemptTokens.get(lease_id),
      }));

    const capacityN = Math.max(0, this.maxConcurrent - active.length);
    const state: WorkerState = this.stop.signal.aborted
      ? "shutting_down"
      : active.length >= this.maxConcurrent
        ? "busy"
        : "idle";

    const req: HeartbeatRequest = {
      state,
      timestamp: new Date().toISOString(),
      active_leases: active,
      completed_since_last: completedBatch,
      capacity: { can_accept_leases: capacityN },
      capabilities: this.reportedCapabilities(),
    };
    const resp = await this.client.heartbeat(req, this.stop.signal);

    for (const lease of resp.new_leases ?? []) {
      this.spawnHandler(lease);
    }

    for (const ins of resp.instructions ?? []) {
      if (ins.type === "shutdown") {
        this.log.info("server-initiated shutdown");
        this.stop.abort();
      } else if (ins.type === "pause") {
        const reason = (ins.payload as { reason?: string } | null)?.reason ?? "no reason";
        this.log.info(`paused by server (${reason})`);
      }
    }

    this.acknowledgeCompletions(completedBatch);

    return Math.max(1, Math.floor(resp.next_heartbeat_in_seconds || 5));
  }

  // ── Per-lease execution ──────────────────────────────────────────────

  private spawnHandler(lease: Lease): void {
    if (this.active.has(lease.lease_id)) {
      this.log.warn(`lease ${lease.lease_id} already active — server replayed`);
      return;
    }
    if (this.hasQueuedCompletion(lease.lease_id)) {
      this.log.warn(`lease ${lease.lease_id} replayed while terminal report is pending`);
      return;
    }
    const p = this.runHandler(lease).finally(() => {
      this.active.delete(lease.lease_id);
      this.attemptTokens.delete(lease.lease_id);
    });
    this.active.set(lease.lease_id, p);
    if (lease.attempt_token) this.attemptTokens.set(lease.lease_id, lease.attempt_token);
  }

  private async runHandler(lease: Lease): Promise<void> {
    const ctx: LeaseContext = {
      lease,
      progress: async (fraction: number) => {
        try {
          await this.client.extendLease(lease.lease_id, {
            extraSeconds: 300,
            progress: fraction,
            attemptToken: lease.attempt_token,
          });
        } catch (exc) {
          this.log.warn(`progress update failed: ${errMsg(exc)}`);
        }
      },
    };

    const handler = this.findHandler(lease);
    try {
      if (!handler) {
        throw new NoHandlerError(
          `no handler for kind=${JSON.stringify(lease.kind)} provider=${JSON.stringify(lease.provider)}`,
        );
      }
      const raw = await handler(lease, ctx);
      const result = structuredClone(LeaseResultFactory.fromHandlerOutput(raw));
      const completion = this.successCompletion(lease, result);
      if (this.queueCompletion(completion)) {
        await this.reportSuccess(lease, result);
      }
    } catch (exc) {
      if (exc instanceof NeedHumanInput) {
        try {
          await this.client.needHuman(lease.lease_id, exc.prompt, {
            attemptToken: lease.attempt_token,
          });
        } catch (rep) {
          this.log.warn(`need_human report failed: ${errMsg(rep)}`);
        }
        return;
      }
      if (exc instanceof NoHandlerError) {
        const err = {
          type: "NoHandler",
          message: exc.message,
          kind: lease.kind,
          provider: lease.provider,
        };
        const completion: HeartbeatCompletedLease = {
          lease_id: lease.lease_id,
          attempt_token: lease.attempt_token,
          status: "failed",
          error: err,
          task_output_value_kind: "task_payload",
        };
        if (this.queueCompletion(completion)) {
          await this.reportFailure(lease, err, { willRetry: false });
        }
        return;
      }
      this.log.error(`handler for lease ${lease.lease_id} raised`, exc);
      const err = { type: errType(exc), message: errMsg(exc) };
      const completion: HeartbeatCompletedLease = {
        lease_id: lease.lease_id,
        attempt_token: lease.attempt_token,
        status: "failed",
        error: err,
        task_output_value_kind: "task_payload",
      };
      if (this.queueCompletion(completion)) {
        await this.reportFailure(lease, err);
      }
    }
  }

  // ── Lifecycle ────────────────────────────────────────────────────────

  private installSignalHandlers(): void {
    const onSig = () => {
      if (!this.stop.signal.aborted) this.stop.abort();
    };
    try {
      process.on("SIGINT", onSig);
      process.on("SIGTERM", onSig);
    } catch {
      // Non-Node host (e.g. some test runners) — caller can shutdown() manually.
    }
  }

  private async drainThenClose(): Promise<void> {
    if (this.active.size > 0) {
      this.log.info(`draining ${this.active.size} in-flight lease(s)`);
      await Promise.allSettled(this.active.values());
    }
    if (this.completions.length > 0) {
      const completedBatch = [...this.completions];
      try {
        const req: HeartbeatRequest = {
          state: "shutting_down",
          timestamp: new Date().toISOString(),
          active_leases: [],
          completed_since_last: completedBatch,
          capacity: { can_accept_leases: 0 },
          capabilities: this.reportedCapabilities(),
        };
        await this.client.heartbeat(req);
        this.acknowledgeCompletions(completedBatch);
      } catch (exc) {
        this.log.warn(`final flush failed: ${errMsg(exc)}`);
      }
    }
  }
}

// ── helpers ────────────────────────────────────────────────────────────

function handlerKey(kind: LeaseKind, provider: string | null | undefined): string {
  return `${kind}::${provider ?? ""}`;
}

function sleepOrAbort(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) return resolve();
    const t = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    const onAbort = () => {
      clearTimeout(t);
      signal.removeEventListener("abort", onAbort);
      resolve();
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}
function errType(e: unknown): string {
  return e instanceof Error ? e.name : "Error";
}
