// Smoke test — no network. Stubs `fetch` and runs ManorWorker through:
//   1. heartbeat → server returns one lease
//   2. handler executes and returns a result
//   3. completeLease + need-human + fail-lease paths
//   4. server sends `shutdown` instruction → loop exits cleanly
//
// Run via `tsx test/smoke.ts`. Exit non-zero on any assertion failure.

import { ManorClient } from "../src/client.js";
import { ManorWorker } from "../src/worker.js";
import {
  HeartbeatCompletedLease,
  HeartbeatRequest,
  HeartbeatResponse,
  Lease,
  LeaseResultFactory,
  NeedHumanInput,
  WorkerClientError,
} from "../src/types.js";

interface WorkerInternals {
  runHandler(lease: Lease): Promise<void>;
  spawnHandler(lease: Lease): void;
  tick(): Promise<number>;
  completions: HeartbeatCompletedLease[];
}

interface FakeCall {
  path: string;
  body: unknown;
  headers: Record<string, string>;
}

function buildFakeFetch(): { fetch: typeof fetch; calls: FakeCall[]; serverState: { issued: boolean; shutdown: boolean } } {
  const calls: FakeCall[] = [];
  const serverState = { issued: false, shutdown: false };

  const fakeLease: Lease = {
    lease_id: "lease_01",
    attempt_token: "attempt_01",
    step_id: "step_01",
    plan_id: "plan_01",
    workspace_id: "ws_01",
    kind: "action",
    provider: "demo",
    action_key: "echo",
    integration_id: null,
    params: { msg: "hi" },
    risk_level: "low",
    lease_until: new Date(Date.now() + 60_000).toISOString(),
    execution_mode: "live",
    credentials: [],
  };

  const fetchImpl: typeof fetch = async (input, init) => {
    const url = typeof input === "string" ? input : (input as URL | Request).toString();
    const path = new URL(url).pathname;
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const body = init?.body ? JSON.parse(init.body as string) : null;
    calls.push({ path, body, headers });

    if (path.endsWith("/heartbeat")) {
      const newLeases = serverState.issued ? [] : [fakeLease];
      serverState.issued = true;
      const instructions = serverState.shutdown
        ? [{ type: "shutdown" as const }]
        : [];
      return jsonResp(200, {
        server_time: new Date().toISOString(),
        next_heartbeat_in_seconds: 1,
        new_leases: newLeases,
        instructions,
      });
    }
    if (path.endsWith("/complete") || path.endsWith("/fail") || path.endsWith("/need-human")) {
      // After the lease is reported done/failed, ask the worker to shut down
      // on the next heartbeat — keeps the test deterministic.
      serverState.shutdown = true;
      return new Response(null, { status: 204 });
    }
    return jsonResp(404, { error: "not found" });
  };

  return { fetch: fetchImpl, calls, serverState };
}

function jsonResp(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function assert(cond: unknown, msg: string): asserts cond {
  if (!cond) {
    console.error(`✗ ${msg}`);
    process.exitCode = 1;
    throw new Error(msg);
  }
  console.log(`  ✓ ${msg}`);
}

// ── Cases ─────────────────────────────────────────────────────────────

async function caseSuccessfulLease(): Promise<void> {
  console.log("\n[case] handler completes lease successfully");
  const { fetch, calls } = buildFakeFetch();
  const client = new ManorClient({
    endpoint: "http://test.local",
    workerId: "wkr_01",
    secret: "wks_01",
    fetchImpl: fetch,
    timeoutMs: 1_000,
  });
  const worker = new ManorWorker({
    endpoint: "http://test.local",
    workerId: "wkr_01",
    secret: "wks_01",
    client,
    capabilities: { supported_capabilities: ["external.social"] },
    logger: silentLogger(),
  });
  let received: Lease | undefined;
  const evidence = ["s3://legacy/receipt", {
    kind: "tool_effect", effect: "action", fields: { provider: "demo", action_key: "echo" },
  }];
  worker.handle({ kind: "action", provider: "demo" }, async (lease) => {
    received = lease;
    return LeaseResultFactory.envelope({
      result: { echo: lease.params.msg },
      cost: { api_calls: 1, usd: 0.01 },
      evidence_refs: evidence,
    });
  });
  await worker.runForever();

  assert(received !== undefined, "handler received the lease");
  assert(received!.lease_id === "lease_01", "lease id propagated");
  const completeCall = calls.find((c) => c.path.endsWith("/complete"));
  assert(completeCall !== undefined, "completeLease was called");
  const completeBody = completeCall!.body as { result?: { echo?: string }; cost?: { usd?: number } };
  assert(completeBody.result?.echo === "hi", "result body sent to server");
  assert(completeBody.cost?.usd === 0.01, "cost forwarded to server");
  assert(
    JSON.stringify((completeCall!.body as { evidence_refs?: unknown }).evidence_refs) === JSON.stringify(evidence),
    "structured and legacy evidence forwarded unchanged",
  );
  assert(
    (completeCall!.body as { task_output_value_kind?: string }).task_output_value_kind === "task_payload",
    "completion discriminator sent to server",
  );
  assert(
    (completeCall!.body as { attempt_token?: string }).attempt_token === "attempt_01",
    "completion echoes the exact lease attempt token",
  );
  const heartbeatCall = calls.find((c) => c.path.endsWith("/heartbeat"));
  const heartbeatBody = heartbeatCall!.body as { capabilities?: { supported_capabilities?: string[] } };
  assert(
    heartbeatBody.capabilities?.supported_capabilities?.[0] === "external.social",
    "runtime capabilities included in heartbeat",
  );
  assert(
    (heartbeatBody.capabilities as { lease_attempt_token_version?: number })
      ?.lease_attempt_token_version === 1,
    "worker advertises attempt-token fencing",
  );
  const completionHeartbeat = calls.find((call) => {
    const body = call.body as { completed_since_last?: Array<{ lease_id?: string }> };
    return body.completed_since_last?.some((item) => item.lease_id === "lease_01");
  });
  assert(completionHeartbeat !== undefined, "completion is replayed on heartbeat");
  const completionHeartbeatBody = completionHeartbeat.body as {
    completed_since_last: Array<{
      attempt_token?: string;
      task_output_value_kind?: string;
      evidence_refs?: unknown;
    }>;
  };
  assert(
    completionHeartbeatBody.completed_since_last[0]?.attempt_token === "attempt_01",
    "heartbeat completion keeps the exact attempt token",
  );
  assert(
    completionHeartbeatBody.completed_since_last[0]?.task_output_value_kind === "task_payload",
    "heartbeat completion discriminator sent to server",
  );
  assert(
    JSON.stringify(completionHeartbeatBody.completed_since_last[0]?.evidence_refs) === JSON.stringify(evidence),
    "heartbeat preserves the same structured evidence contract",
  );
  assert(
    calls.every((c) => c.headers["Authorization"] === "Bearer wks_01"),
    "auth header set on every call",
  );
  assert(
    calls.every((c) => c.headers["Manor-Worker-Id"] === "wkr_01"),
    "worker-id header set on every call",
  );
  assert(
    calls.every((c) => c.headers["Manor-Protocol-Version"] === "2"),
    "v2 protocol header set on every call",
  );
}

async function caseReservedBusinessKeysRemainPayload(): Promise<void> {
  console.log("\n[case] reserved business keys remain a task payload");
  const { fetch, calls } = buildFakeFetch();
  const worker = new ManorWorker({
    endpoint: "http://test.local",
    workerId: "wkr_01",
    secret: "wks_01",
    client: new ManorClient({
      endpoint: "http://test.local",
      workerId: "wkr_01",
      secret: "wks_01",
      fetchImpl: fetch,
    }),
    logger: silentLogger(),
  });
  worker.handle({ kind: "action", provider: "demo" }, async () => ({
    result: 42,
    status: "ok",
  }));
  await worker.runForever();

  const completeCall = calls.find((call) => call.path.endsWith("/complete"));
  const body = completeCall?.body as {
    result?: { result?: number; status?: string };
    cost?: unknown;
  };
  assert(body.result?.result === 42, "business result key remains inside payload");
  assert(body.result?.status === "ok", "business payload fields remain intact");
  assert(body.cost === undefined, "business payload does not become transport cost");
}

async function caseNeedHuman(): Promise<void> {
  console.log("\n[case] handler raises NeedHumanInput");
  const { fetch, calls } = buildFakeFetch();
  const client = new ManorClient({
    endpoint: "http://test.local",
    workerId: "wkr_01",
    secret: "wks_01",
    fetchImpl: fetch,
  });
  const worker = new ManorWorker({
    endpoint: "http://test.local",
    workerId: "wkr_01",
    secret: "wks_01",
    client,
    logger: silentLogger(),
  });
  worker.handle({ kind: "action", provider: "demo" }, async () => {
    throw new NeedHumanInput("which order?", { kind: "ambiguous_input" });
  });
  await worker.runForever();
  const nh = calls.find((c) => c.path.endsWith("/need-human"));
  assert(nh !== undefined, "need-human endpoint called");
  assert((nh!.body as { prompt?: string }).prompt === "which order?", "prompt forwarded");
  assert(
    (nh!.body as { attempt_token?: string }).attempt_token === "attempt_01",
    "need-human echoes the exact lease attempt token",
  );
}

async function caseHandlerThrows(): Promise<void> {
  console.log("\n[case] handler throws → fail-lease called");
  const { fetch, calls } = buildFakeFetch();
  const worker = new ManorWorker({
    endpoint: "http://test.local",
    workerId: "wkr_01",
    secret: "wks_01",
    client: new ManorClient({
      endpoint: "http://test.local",
      workerId: "wkr_01",
      secret: "wks_01",
      fetchImpl: fetch,
    }),
    logger: silentLogger(),
  });
  worker.handle({ kind: "action", provider: "demo" }, async () => {
    throw new Error("boom");
  });
  await worker.runForever();
  const failCall = calls.find((c) => c.path.endsWith("/fail"));
  assert(failCall !== undefined, "fail-lease called");
  const body = failCall!.body as { error?: { message?: string; type?: string } };
  assert(body.error?.message === "boom", "error message forwarded");
  assert(body.error?.type === "Error", "error type forwarded");
  assert(
    (failCall!.body as { attempt_token?: string }).attempt_token === "attempt_01",
    "failure echoes the exact lease attempt token",
  );
}

async function caseNoHandler(): Promise<void> {
  console.log("\n[case] no matching handler → fail with NoHandler + will_retry=false");
  const { fetch, calls } = buildFakeFetch();
  const worker = new ManorWorker({
    endpoint: "http://test.local",
    workerId: "wkr_01",
    secret: "wks_01",
    client: new ManorClient({
      endpoint: "http://test.local",
      workerId: "wkr_01",
      secret: "wks_01",
      fetchImpl: fetch,
    }),
    logger: silentLogger(),
  });
  // No handlers registered.
  await worker.runForever();
  const failCall = calls.find((c) => c.path.endsWith("/fail"));
  assert(failCall !== undefined, "fail-lease called");
  const body = failCall!.body as { error?: { type?: string }; will_retry?: boolean };
  assert(body.error?.type === "NoHandler", "fail tagged as NoHandler");
  assert(body.will_retry === false, "no-handler is non-retryable");
}

async function caseRegistrationDeclaresProtocolV2(): Promise<void> {
  console.log("\n[case] direct SDK registration declares protocol v2");
  let requestBody: unknown;
  const client = new ManorClient({
    endpoint: "http://test.local",
    fetchImpl: async (_input, init) => {
      requestBody = init?.body ? JSON.parse(init.body as string) : null;
      return jsonResp(200, { worker_id: "wkr_01", worker_secret: "wks_01" });
    },
  });
  await client.register({
    kind: "custom_http",
    display_name: "test worker",
    capabilities: { supported_kinds: ["action"], protocol_version: 1 },
  });
  const body = requestBody as { capabilities?: { protocol_version?: number } };
  assert(
    body.capabilities?.protocol_version === 2,
    "SDK registration cannot advertise the removed v1 protocol",
  );
}

async function caseProtocolUpgradeStopsWithoutRetrying(): Promise<void> {
  console.log("\n[case] protocol upgrade response stops without retrying");
  let calls = 0;
  const client = new ManorClient({
    endpoint: "http://test.local",
    workerId: "wkr_legacy",
    secret: "wks_legacy",
    maxAttempts: 3,
    fetchImpl: async () => {
      calls += 1;
      return jsonResp(426, { detail: "re-register worker" });
    },
  });
  let error: unknown;
  try {
    await client.heartbeat({
      state: "idle",
      active_leases: [],
      completed_since_last: [],
      capacity: { can_accept_leases: 0 },
    });
  } catch (exc) {
    error = exc;
  }
  assert(error instanceof WorkerClientError, "426 is surfaced as WorkerClientError");
  assert(error.requiresOperatorAction, "426 is classified as requiring operator action");
  assert(calls === 1, "426 is not retried as a transient response");

  const logMessages: string[] = [];
  const worker = new ManorWorker({
    endpoint: "http://test.local",
    workerId: "wkr_legacy",
    secret: "wks_legacy",
    client,
    logger: {
      info: () => {},
      warn: () => {},
      error: (message) => logMessages.push(message),
    },
  });
  await worker.runForever();
  assert(calls === 2, "worker loop stops after its first 426 heartbeat");
  assert(
    logMessages.some((message) => message.includes("re-register")),
    "worker loop explains the required recovery action",
  );
}

async function caseTerminalReportOutbox(): Promise<void> {
  console.log("\n[case] terminal report timeout preserves one immutable retry");
  let handlerCalls = 0;
  let completeCalls = 0;
  let failCalls = 0;
  let heartbeatFailures = 1;
  const heartbeatRequests: HeartbeatRequest[] = [];
  const client = {
    completeLease: async () => {
      completeCalls += 1;
      throw new WorkerClientError("completion response timed out");
    },
    failLease: async () => {
      failCalls += 1;
    },
    heartbeat: async (request: HeartbeatRequest): Promise<HeartbeatResponse> => {
      heartbeatRequests.push(request);
      if (heartbeatFailures > 0) {
        heartbeatFailures -= 1;
        throw new WorkerClientError("heartbeat response timed out");
      }
      return {
        server_time: new Date().toISOString(),
        next_heartbeat_in_seconds: 1,
        new_leases: [],
        instructions: [],
      };
    },
  } as unknown as ManorClient;
  const worker = new ManorWorker({
    endpoint: "http://test.local",
    workerId: "wkr_01",
    secret: "wks_01",
    client,
    logger: silentLogger(),
  });
  const payload = { provider_receipt: "receipt-1" };
  worker.handle({ kind: "action", provider: "demo" }, async () => {
    handlerCalls += 1;
    return payload;
  });
  const lease: Lease = {
    lease_id: "lease_outbox",
    attempt_token: "attempt_outbox",
    step_id: "step_outbox",
    plan_id: "plan_outbox",
    workspace_id: "ws_outbox",
    kind: "action",
    provider: "demo",
    action_key: "write",
    integration_id: null,
    params: {},
    risk_level: "low",
    lease_until: new Date(Date.now() + 60_000).toISOString(),
    execution_mode: "live",
    credentials: [],
  };
  const internals = worker as unknown as WorkerInternals;

  await internals.runHandler(lease);
  assert(handlerCalls === 1, "application handler ran once");
  assert(completeCalls === 1, "direct completion report was attempted once");
  assert(failCalls === 0, "completion timeout did not emit a contradictory failure");
  assert(internals.completions.length === 1, "successful outcome remains queued");
  assert(
    internals.completions[0]?.status === "done",
    "queued terminal outcome remains successful",
  );

  payload.provider_receipt = "mutated";
  assert(
    (internals.completions[0]?.result as { provider_receipt?: string })
      ?.provider_receipt === "receipt-1",
    "queued terminal outcome is an immutable snapshot",
  );

  internals.spawnHandler(lease);
  await Promise.resolve();
  assert(handlerCalls === 1, "lease replay cannot rerun a handler with a pending outcome");

  let heartbeatError: unknown;
  try {
    await internals.tick();
  } catch (exc) {
    heartbeatError = exc;
  }
  assert(heartbeatError instanceof WorkerClientError, "failed heartbeat is surfaced");
  assert(internals.completions.length === 1, "failed heartbeat keeps its batch queued");

  await internals.tick();
  assert(internals.completions.length === 0, "successful heartbeat acknowledges its batch");
  assert(heartbeatRequests.length === 2, "terminal outcome was retried once");
  assert(
    JSON.stringify(heartbeatRequests[0]?.completed_since_last) ===
      JSON.stringify(heartbeatRequests[1]?.completed_since_last),
    "heartbeat retry preserves the exact terminal envelope",
  );
  assert(failCalls === 0, "no failure report is emitted during terminal retry");
}

function silentLogger() {
  return { info: () => {}, warn: () => {}, error: () => {} };
}

async function caseServerBackpressure(): Promise<void> {
  console.log("\n[case] server backpressure delays reads and preserves terminal reports");
  const originalRandom = Math.random;
  Math.random = () => 0;
  try {
    const calls: { path: string; at: number; body: unknown }[] = [];
    const client = new ManorClient({
      endpoint: "http://test.local", workerId: "worker-backpressure", secret: "secret",
      fetchImpl: async (input, init) => {
        const path = new URL(String(input)).pathname;
        calls.push({ path, at: performance.now(), body: JSON.parse(String(init?.body)) });
        if (calls.length === 1) {
          return new Response(JSON.stringify({ detail: { code: "database_busy" } }), {
            status: 503, headers: { "Retry-After": "0.05" },
          });
        }
        return jsonResp(200, { server_time: new Date().toISOString(), next_heartbeat_in_seconds: 10, new_leases: [], instructions: [] });
      },
    });
    let rejected: unknown;
    try {
      await client.completeLease("lease-backpressure", { result: { receipt: "done" } }, { attemptToken: "attempt" });
    } catch (exc) { rejected = exc; }
    assert(rejected instanceof WorkerClientError && rejected.retryAfterMs === 50, "terminal 503 exposes Retry-After");
    assert(calls.length === 1, "ambiguous terminal write is not replayed automatically");
    await client.heartbeat({ state: "idle" });
    assert(calls.length === 2 && calls[1].at - calls[0].at >= 45, "next request honors the shared server cooldown");

    let attempts = 0;
    const retryCredentials: string[] = [];
    const heartbeatClient = new ManorClient({
      endpoint: "http://test.local", workerId: "worker-backpressure", secret: "secret",
      fetchImpl: async (_input, init) => {
        attempts++;
        retryCredentials.push((init?.headers as Record<string, string>).Authorization);
        if (attempts === 1) {
          heartbeatClient.configureCredentials("worker-backpressure", "rotated-secret");
          return new Response("busy", { status: 503, headers: { "Retry-After": "0.01" } });
        }
        return jsonResp(200, { server_time: new Date().toISOString(), next_heartbeat_in_seconds: 10, new_leases: [], instructions: [] });
      },
    });
    await heartbeatClient.heartbeat({ state: "idle" });
    assert(attempts === 2, "temporary auth/DB overload recovers without discarding credentials");
    assert(retryCredentials[1] === "Bearer rotated-secret", "retry refreshes credentials after same-worker rotation");
  } finally { Math.random = originalRandom; }
}

// ── runner ────────────────────────────────────────────────────────────

(async () => {
  await caseSuccessfulLease();
  await caseReservedBusinessKeysRemainPayload();
  await caseNeedHuman();
  await caseHandlerThrows();
  await caseNoHandler();
  await caseRegistrationDeclaresProtocolV2();
  await caseProtocolUpgradeStopsWithoutRetrying();
  await caseTerminalReportOutbox();
  await caseServerBackpressure();
  if (process.exitCode && process.exitCode !== 0) {
    console.error("\nSMOKE FAILED");
    process.exit(process.exitCode);
  }
  console.log("\nSMOKE OK");
})();
