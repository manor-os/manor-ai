// HTTP client for the Manor worker protocol.
// Wraps undici fetch with auth headers + transient-error backoff.

import {
  HeartbeatRequest,
  HeartbeatResponse,
  LeaseResult,
  WorkerClientError,
} from "./types.js";

const PROTOCOL_VERSION = "2";
const PROTOCOL_HEADER = "Manor-Protocol-Version";
const WORKER_ID_HEADER = "Manor-Worker-Id";
const USER_AGENT = `manor-worker-sdk-ts/0.1 (proto/${PROTOCOL_VERSION})`;

export interface ManorClientOptions {
  endpoint: string;
  workerId?: string;
  secret?: string;
  /** Total call deadline in milliseconds, including cooldown and retries. */
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
  private retryNotBeforeMs = 0;
  private rotation?: { workerId: string | undefined; promise: Promise<string> };

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

  async heartbeat(req: HeartbeatRequest, signal?: AbortSignal): Promise<HeartbeatResponse> {
    const body = await this.post<HeartbeatResponse>(
      "/api/v1/workers/heartbeat",
      stripUndefined(req),
      { signal },
    );
    return body;
  }

  async completeLease(
    leaseId: string,
    result: LeaseResult,
    opts?: { attemptToken?: string | null },
  ): Promise<void> {
    const payload = {
      ...stripUndefined(result),
      task_output_value_kind: result.task_output_value_kind ?? "task_payload",
      ...(opts?.attemptToken ? { attempt_token: opts.attemptToken } : {}),
    };
    await this.post(
      `/api/v1/workers/leases/${encodeURIComponent(leaseId)}/complete`,
      payload,
      { expect204: true },
    );
  }

  async failLease(
    leaseId: string,
    error: Record<string, unknown>,
    opts?: { willRetry?: boolean; attemptToken?: string | null },
  ): Promise<void> {
    const payload: Record<string, unknown> = { error };
    if (opts?.willRetry !== undefined) payload.will_retry = opts.willRetry;
    if (opts?.attemptToken) payload.attempt_token = opts.attemptToken;
    await this.post(
      `/api/v1/workers/leases/${encodeURIComponent(leaseId)}/fail`,
      payload,
      { expect204: true },
    );
  }

  async needHuman(
    leaseId: string,
    prompt: string,
    opts?: { attemptToken?: string | null },
  ): Promise<void> {
    await this.post(
      `/api/v1/workers/leases/${encodeURIComponent(leaseId)}/need-human`,
      {
        prompt,
        ...(opts?.attemptToken ? { attempt_token: opts.attemptToken } : {}),
      },
      { expect204: true },
    );
  }

  async extendLease(
    leaseId: string,
    opts?: { extraSeconds?: number; progress?: number; attemptToken?: string | null },
  ): Promise<Record<string, unknown>> {
    const payload: Record<string, unknown> = {
      extra_seconds: opts?.extraSeconds ?? 300,
    };
    if (opts?.progress !== undefined) payload.progress = opts.progress;
    if (opts?.attemptToken) payload.attempt_token = opts.attemptToken;
    return this.post(
      `/api/v1/workers/leases/${encodeURIComponent(leaseId)}/extend`,
      payload,
    );
  }

  async deregister(): Promise<void> {
    await this.post("/api/v1/workers/me/deregister", {}, { expect204: true });
  }

  async rotateSecret(): Promise<string> {
    const workerId = this.workerId;
    if (this.rotation && this.rotation.workerId === workerId) return this.rotation.promise;
    const rotation = { workerId, promise: this.rotateSecretForWorker(workerId) };
    this.rotation = rotation;
    try {
      return await rotation.promise;
    } finally {
      if (this.rotation === rotation) this.rotation = undefined;
    }
  }

  private async rotateSecretForWorker(workerId: string | undefined): Promise<string> {
    const body = await this.post<{ worker_secret: string }>(
      "/api/v1/workers/me/rotate-secret",
      {},
    );
    if (this.workerId !== workerId) {
      throw new WorkerClientError("Worker identity changed during rotation", { statusCode: 401 });
    }
    if (workerId) this.configureCredentials(workerId, body.worker_secret);
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
    const declaredCapabilities = isRecord(payload.capabilities)
      ? payload.capabilities
      : {};
    return this.postUnauthenticated("/api/v1/workers/register", {
      ...payload,
      capabilities: {
        ...declaredCapabilities,
        protocol_version: 2,
      },
    });
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
    opts?: { expect204?: boolean; signal?: AbortSignal },
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
    opts: { headers: Record<string, string>; expect204?: boolean; signal?: AbortSignal },
  ): Promise<T> {
    const ctrl = new AbortController();
    const onAbort = () => ctrl.abort(opts.signal?.reason);
    if (opts.signal?.aborted) onAbort();
    else opts.signal?.addEventListener("abort", onAbort, { once: true });
    const timer = setTimeout(() => ctrl.abort(new Error("Request deadline exceeded")), this.timeoutMs);
    let rejectOnAbort: (() => void) | undefined;
    try {
      // Enforce the deadline even when an injected transport/body reader does
      // not itself reject on abort. The inner retry loop also checks the signal.
      return await new Promise<T>((resolve, reject) => {
        rejectOnAbort = () => reject(ctrl.signal.reason);
        ctrl.signal.addEventListener("abort", rejectOnAbort, { once: true });
        this.requestWithRetry<T>(path, json, opts, ctrl.signal).then(resolve, reject);
        if (ctrl.signal.aborted) rejectOnAbort();
      });
    } catch (exc) {
      if (ctrl.signal.aborted) {
        throw new WorkerClientError(`POST ${path}: ${opts.signal?.aborted ? "request aborted" : "request deadline exceeded"}`, {
          retryAfterMs: Math.max(0, this.retryNotBeforeMs - performance.now()),
        });
      }
      throw exc;
    } finally {
      clearTimeout(timer);
      if (rejectOnAbort) ctrl.signal.removeEventListener("abort", rejectOnAbort);
      opts.signal?.removeEventListener("abort", onAbort);
    }
  }

  private async requestWithRetry<T>(
    path: string,
    json: unknown,
    opts: { headers: Record<string, string>; expect204?: boolean },
    signal: AbortSignal,
  ): Promise<T> {
    const url = `${this.endpoint}${path}`;
    const expectedWorkerId = opts.headers[WORKER_ID_HEADER];
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "User-Agent": USER_AGENT,
      ...opts.headers,
    };
    const requireSameWorker = () => {
      if (expectedWorkerId && expectedWorkerId !== this.workerId) {
        throw new WorkerClientError("Worker identity changed during request", { statusCode: 401 });
      }
    };

    let lastErr: unknown;
    let allowedAttempts = this.maxAttempts;
    let credentialRetryUsed = false;
    for (let attempt = 0; attempt < this.maxAttempts + 1; attempt++) {
      if (attempt >= allowedAttempts) break;
      while (this.retryNotBeforeMs > performance.now()) {
        await sleep(this.retryNotBeforeMs - performance.now() + Math.random() * 500, signal);
      }
      signal.throwIfAborted();
      requireSameWorker();
      const attemptHeaders = expectedWorkerId ? { ...headers, ...this.authHeaders() } : headers;
      const attemptSecret = this.secret;
      let resp: Response;
      try {
        resp = await this.fetchImpl(url, {
          method: "POST",
          headers: attemptHeaders,
          body: JSON.stringify(json),
          signal,
        });
      } catch (exc) {
        signal.throwIfAborted();
        lastErr = exc;
        if (attempt + 1 < allowedAttempts) await sleep(backoffMs(attempt), signal);
        continue;
      }
      signal.throwIfAborted();
      requireSameWorker();

      const retryAfterMs = responseRetryAfterMs(resp);
      if (resp.status === 429 || resp.status === 503) {
        this.retryNotBeforeMs = Math.max(this.retryNotBeforeMs, performance.now() + retryAfterMs);
      }
      const errorBody = resp.status >= 400 || (opts.expect204 && resp.status !== 204)
        ? await safeBody(resp) : undefined;
      signal.throwIfAborted();
      requireSameWorker();
      if (resp.status === 401 && path === "/api/v1/workers/heartbeat"
        && expectedWorkerId && !credentialRetryUsed) {
        // A committed rotation's response may arrive after this old 401.
        // The outer request deadline and abort still bound this wait.
        const rotation = this.rotation;
        if (rotation?.workerId === expectedWorkerId) await rotation.promise.catch(() => {});
        signal.throwIfAborted();
        requireSameWorker();
        if (this.secret && this.secret !== attemptSecret) {
          credentialRetryUsed = true;
          allowedAttempts++;
          continue;
        }
      }
      if (opts.expect204) {
        if (resp.status === 204) return undefined as T;
        throw new WorkerClientError(
          `POST ${path}: expected 204, got ${resp.status}`,
          { statusCode: resp.status, body: errorBody, retryAfterMs },
        );
      }

      if (resp.status >= 400) {
        const responseError = new WorkerClientError(`POST ${path}: ${resp.status}`, {
          statusCode: resp.status,
          body: errorBody,
          retryAfterMs,
        });
        // Credentials and a rejected protocol registration cannot repair
        // themselves. Let the worker loop stop on the first response.
        if (responseError.requiresOperatorAction) throw responseError;
        lastErr = responseError;
        if (attempt + 1 < allowedAttempts) {
          await sleep(backoffMs(attempt), signal);
          continue;
        }
        throw lastErr;
      }

      let body: T;
      try {
        body = (await resp.json()) as T;
      } catch (exc) {
        throw new WorkerClientError(`POST ${path}: invalid JSON response`, {
          body: await resp.text().catch(() => ""),
        });
      }
      signal.throwIfAborted();
      requireSameWorker();
      return body;
    }

    throw new WorkerClientError(
      `POST ${path}: failed after ${allowedAttempts} attempts`,
      { body: errToString(lastErr) },
    );
  }
}

// ── helpers ────────────────────────────────────────────────────────────

function backoffMs(attempt: number): number {
  return Math.random() * Math.min(30_000, 500 * 2 ** attempt);
}

function responseRetryAfterMs(resp: Response): number {
  const header = resp.headers.get("Retry-After")?.trim();
  const numeric = header && /^\d+(\.\d+)?$/.test(header) ? Number(header) * 1000 : NaN;
  const delay = Number.isFinite(numeric) ? numeric : header ? Date.parse(header) - Date.now() : NaN;
  return Number.isFinite(delay) ? Math.max(0, delay) : resp.status === 503 ? 2_000 : 0;
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) return reject(signal.reason);
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal.reason);
    };
    const timer = setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve();
    }, Math.min(ms, 2_147_483_647));
    signal.addEventListener("abort", onAbort, { once: true });
  });
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
