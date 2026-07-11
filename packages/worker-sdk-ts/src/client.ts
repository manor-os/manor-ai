// HTTP client for the Manor worker protocol.
// Wraps undici fetch with auth headers + transient-error backoff.

import {
  HeartbeatRequest,
  HeartbeatResponse,
  LeaseResult,
  WorkerClientError,
} from "./types.js";

const PROTOCOL_VERSION = "1";
const PROTOCOL_HEADER = "Manor-Protocol-Version";
const WORKER_ID_HEADER = "Manor-Worker-Id";
const USER_AGENT = `manor-worker-sdk-ts/0.1 (proto/${PROTOCOL_VERSION})`;

export interface ManorClientOptions {
  endpoint: string;
  workerId?: string;
  secret?: string;
  /** Per-request timeout in milliseconds. */
  timeoutMs?: number;
  /** Max attempts for transient (transport / 5xx) errors. */
  maxAttempts?: number;
  /** Override `fetch` for tests. */
  fetchImpl?: typeof fetch;
}

export class ManorClient {
  private readonly endpoint: string;
  private workerId: string | undefined;
  private secret: string | undefined;
  private readonly timeoutMs: number;
  private readonly maxAttempts: number;
  private readonly fetchImpl: typeof fetch;

  constructor(opts: ManorClientOptions) {
    this.endpoint = opts.endpoint.replace(/\/+$/, "");
    this.workerId = opts.workerId;
    this.secret = opts.secret;
    this.timeoutMs = opts.timeoutMs ?? 30_000;
    this.maxAttempts = opts.maxAttempts ?? 3;
    this.fetchImpl = opts.fetchImpl ?? globalThis.fetch;
    if (!this.fetchImpl) {
      throw new Error("global fetch unavailable — Node 18+ required");
    }
  }

  configureCredentials(workerId: string, secret: string): void {
    this.workerId = workerId;
    this.secret = secret;
  }

  // ── Endpoints ──────────────────────────────────────────────────────

  async heartbeat(req: HeartbeatRequest): Promise<HeartbeatResponse> {
    const body = await this.post<HeartbeatResponse>(
      "/api/v1/workers/heartbeat",
      stripUndefined(req),
    );
    return body;
  }

  async completeLease(leaseId: string, result: LeaseResult): Promise<void> {
    await this.post(
      `/api/v1/workers/leases/${encodeURIComponent(leaseId)}/complete`,
      stripUndefined(result),
      { expect204: true },
    );
  }

  async failLease(
    leaseId: string,
    error: Record<string, unknown>,
    opts?: { willRetry?: boolean },
  ): Promise<void> {
    const payload: Record<string, unknown> = { error };
    if (opts?.willRetry !== undefined) payload.will_retry = opts.willRetry;
    await this.post(
      `/api/v1/workers/leases/${encodeURIComponent(leaseId)}/fail`,
      payload,
      { expect204: true },
    );
  }

  async needHuman(leaseId: string, prompt: string): Promise<void> {
    await this.post(
      `/api/v1/workers/leases/${encodeURIComponent(leaseId)}/need-human`,
      { prompt },
      { expect204: true },
    );
  }

  async extendLease(
    leaseId: string,
    opts?: { extraSeconds?: number; progress?: number },
  ): Promise<Record<string, unknown>> {
    const payload: Record<string, unknown> = {
      extra_seconds: opts?.extraSeconds ?? 300,
    };
    if (opts?.progress !== undefined) payload.progress = opts.progress;
    return this.post(
      `/api/v1/workers/leases/${encodeURIComponent(leaseId)}/extend`,
      payload,
    );
  }

  async deregister(): Promise<void> {
    await this.post("/api/v1/workers/me/deregister", {}, { expect204: true });
  }

  async rotateSecret(): Promise<string> {
    const body = await this.post<{ worker_secret: string }>(
      "/api/v1/workers/me/rotate-secret",
      {},
    );
    if (this.workerId) this.configureCredentials(this.workerId, body.worker_secret);
    return body.worker_secret;
  }

  /**
   * Worker registration — unauthenticated. Returns the issued
   * worker_id + worker_secret. Caller wires them back in via
   * `configureCredentials` before any further calls.
   */
  async register(payload: Record<string, unknown>): Promise<{
    worker_id: string;
    worker_secret: string;
    [k: string]: unknown;
  }> {
    return this.postUnauthenticated("/api/v1/workers/register", payload);
  }

  // ── Internals ──────────────────────────────────────────────────────

  private authHeaders(): Record<string, string> {
    if (!this.workerId || !this.secret) {
      throw new WorkerClientError("worker_id + secret not configured");
    }
    return {
      Authorization: `Bearer ${this.secret}`,
      [WORKER_ID_HEADER]: this.workerId,
      [PROTOCOL_HEADER]: PROTOCOL_VERSION,
    };
  }

  private async post<T = unknown>(
    path: string,
    json: unknown,
    opts?: { expect204?: boolean },
  ): Promise<T> {
    return this.request<T>(path, json, { headers: this.authHeaders(), ...opts });
  }

  private async postUnauthenticated<T = unknown>(
    path: string,
    json: unknown,
  ): Promise<T> {
    return this.request<T>(path, json, { headers: {} });
  }

  private async request<T>(
    path: string,
    json: unknown,
    opts: { headers: Record<string, string>; expect204?: boolean },
  ): Promise<T> {
    const url = `${this.endpoint}${path}`;
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "User-Agent": USER_AGENT,
      ...opts.headers,
    };

    let lastErr: unknown;
    for (let attempt = 0; attempt < this.maxAttempts; attempt++) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), this.timeoutMs);
      let resp: Response;
      try {
        resp = await this.fetchImpl(url, {
          method: "POST",
          headers,
          body: JSON.stringify(json),
          signal: ctrl.signal,
        });
      } catch (exc) {
        clearTimeout(timer);
        lastErr = exc;
        await sleep(backoffMs(attempt));
        continue;
      }
      clearTimeout(timer);

      if (opts.expect204) {
        if (resp.status === 204) return undefined as T;
        throw new WorkerClientError(
          `POST ${path}: expected 204, got ${resp.status}`,
          { statusCode: resp.status, body: await safeBody(resp) },
        );
      }

      if (resp.status >= 400) {
        // 401 / 403 → auth won't fix itself, fail fast.
        if (resp.status === 401 || resp.status === 403) {
          throw new WorkerClientError(`POST ${path}: ${resp.status}`, {
            statusCode: resp.status,
            body: await safeBody(resp),
          });
        }
        lastErr = new WorkerClientError(`POST ${path}: ${resp.status}`, {
          statusCode: resp.status,
          body: await safeBody(resp),
        });
        if (attempt + 1 < this.maxAttempts) {
          await sleep(backoffMs(attempt));
          continue;
        }
        throw lastErr;
      }

      try {
        return (await resp.json()) as T;
      } catch (exc) {
        throw new WorkerClientError(`POST ${path}: invalid JSON response`, {
          body: await resp.text().catch(() => ""),
        });
      }
    }

    throw new WorkerClientError(
      `POST ${path}: failed after ${this.maxAttempts} attempts`,
      { body: errToString(lastErr) },
    );
  }
}

// ── helpers ────────────────────────────────────────────────────────────

function backoffMs(attempt: number): number {
  return Math.min(30_000, 500 * 2 ** attempt);
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

async function safeBody(resp: Response): Promise<unknown> {
  try {
    return await resp.json();
  } catch {
    return (await resp.text().catch(() => "")).slice(0, 500);
  }
}

function errToString(e: unknown): string {
  return e instanceof Error ? `${e.name}: ${e.message}` : String(e);
}

function stripUndefined<T>(obj: T): T {
  // FastAPI tolerates extra keys but barfs on `null` where it expects
  // a typed field; drop undefined entries and recurse.
  if (Array.isArray(obj)) return obj.map(stripUndefined) as T;
  if (obj && typeof obj === "object") {
    const out: Record<string, unknown> = {};
    for (const [k, v] of Object.entries(obj as Record<string, unknown>)) {
      if (v === undefined) continue;
      out[k] = stripUndefined(v as never);
    }
    return out as T;
  }
  return obj;
}
