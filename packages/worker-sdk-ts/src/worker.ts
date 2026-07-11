// ManorWorker — high-level user surface (TS port of packages/worker_sdk/worker.py).

import { ManorClient, ManorClientOptions } from "./client.js";
import {
  HeartbeatActiveLease,
  HeartbeatCompletedLease,
  HeartbeatRequest,
  Lease,
  LeaseKind,
  LeaseResult,
  NeedHumanInput,
  NoHandlerError,
  WorkerClientError,
  WorkerState,
} from "./types.js";

export interface LeaseContext {
  readonly lease: Lease;
  /**
   * Push a progress update + lease extension. Use sparingly — frequent
   * extensions show up as `extended_count` in audit.
   */
  progress(fraction: number): Promise<void>;
}

export type HandlerReturn =
  | LeaseResult
  | Record<string, unknown>
  | undefined
  | void;

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
    while (!this.stop.signal.aborted) {
      try {
        nextHeartbeatIn = await this.tick();
      } catch (exc) {
        if (
          exc instanceof WorkerClientError &&
          (exc.statusCode === 401 || exc.statusCode === 403)
        ) {
          this.log.error(`auth failed (${exc.statusCode}) — exiting`);
          this.stop.abort();
          break;
        }
        if (exc instanceof WorkerClientError) {
          this.log.warn(`heartbeat failed (${exc.message}) — backing off`);
          nextHeartbeatIn = Math.max(nextHeartbeatIn, 10);
        } else {
          this.log.error("unexpected loop failure — backing off", exc);
          nextHeartbeatIn = Math.max(nextHeartbeatIn, 30);
        }
      }
      await sleepOrAbort(nextHeartbeatIn * 1000, this.stop.signal);
    }
  }

  private async tick(): Promise<number> {
    const active: HeartbeatActiveLease[] = [...this.active.keys()].map((lease_id) => ({
      lease_id,
    }));
    const completedBatch = this.completions;
    this.completions = [];

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
      capabilities: this.capabilities,
    };
    const resp = await this.client.heartbeat(req);

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

    return Math.max(1, Math.floor(resp.next_heartbeat_in_seconds || 5));
  }

  // ── Per-lease execution ──────────────────────────────────────────────

  private spawnHandler(lease: Lease): void {
    if (this.active.has(lease.lease_id)) {
      this.log.warn(`lease ${lease.lease_id} already active — server replayed`);
      return;
    }
    const p = this.runHandler(lease).finally(() => {
      this.active.delete(lease.lease_id);
    });
    this.active.set(lease.lease_id, p);
  }

  private async runHandler(lease: Lease): Promise<void> {
    const ctx: LeaseContext = {
      lease,
      progress: async (fraction: number) => {
        try {
          await this.client.extendLease(lease.lease_id, {
            extraSeconds: 300,
            progress: fraction,
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
      const result = coerceResult(raw);
      await this.client.completeLease(lease.lease_id, result);
      this.completions.push({
        lease_id: lease.lease_id,
        status: "done",
        result: result.result ?? null,
        cost: result.cost ?? null,
        evidence_refs: result.evidence_refs ?? null,
      });
    } catch (exc) {
      if (exc instanceof NeedHumanInput) {
        try {
          await this.client.needHuman(lease.lease_id, exc.prompt);
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
        await this.client.failLease(lease.lease_id, err, { willRetry: false });
        this.completions.push({
          lease_id: lease.lease_id,
          status: "failed",
          error: err,
        });
        return;
      }
      this.log.error(`handler for lease ${lease.lease_id} raised`, exc);
      const err = { type: errType(exc), message: errMsg(exc) };
      try {
        await this.client.failLease(lease.lease_id, err);
      } catch (rep) {
        this.log.warn(`fail_lease report failed: ${errMsg(rep)}`);
      }
      this.completions.push({
        lease_id: lease.lease_id,
        status: "failed",
        error: err,
      });
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
      try {
        const req: HeartbeatRequest = {
          state: "shutting_down",
          timestamp: new Date().toISOString(),
          active_leases: [],
          completed_since_last: this.completions,
          capacity: { can_accept_leases: 0 },
          capabilities: this.capabilities,
        };
        await this.client.heartbeat(req);
        this.completions = [];
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

function coerceResult(raw: HandlerReturn): LeaseResult {
  if (raw === undefined || raw === null) return {};
  if (
    typeof raw === "object" &&
    !Array.isArray(raw) &&
    ("result" in raw || "cost" in raw || "evidence_refs" in raw)
  ) {
    const r = raw as Record<string, unknown>;
    return {
      result: (r.result as Record<string, unknown>) ?? null,
      cost: (r.cost as LeaseResult["cost"]) ?? null,
      evidence_refs: (r.evidence_refs as string[]) ?? null,
    };
  }
  if (typeof raw === "object" && !Array.isArray(raw)) {
    return { result: raw as Record<string, unknown> };
  }
  return { result: { value: raw as unknown } };
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
