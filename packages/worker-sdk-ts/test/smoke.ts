// Smoke test — no network. Stubs `fetch` and runs ManorWorker through:
//   1. heartbeat → server returns one lease
//   2. handler executes and returns a result
//   3. completeLease + need-human + fail-lease paths
//   4. server sends `shutdown` instruction → loop exits cleanly
//
// Run via `tsx test/smoke.ts`. Exit non-zero on any assertion failure.

import { ManorClient } from "../src/client.js";
import { ManorWorker } from "../src/worker.js";
import { Lease, NeedHumanInput } from "../src/types.js";

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
  worker.handle({ kind: "action", provider: "demo" }, async (lease) => {
    received = lease;
    return { result: { echo: lease.params.msg }, cost: { api_calls: 1, usd: 0.01 } };
  });
  await worker.runForever();

  assert(received !== undefined, "handler received the lease");
  assert(received!.lease_id === "lease_01", "lease id propagated");
  const completeCall = calls.find((c) => c.path.endsWith("/complete"));
  assert(completeCall !== undefined, "completeLease was called");
  const completeBody = completeCall!.body as { result?: { echo?: string }; cost?: { usd?: number } };
  assert(completeBody.result?.echo === "hi", "result body sent to server");
  assert(completeBody.cost?.usd === 0.01, "cost forwarded to server");
  const heartbeatCall = calls.find((c) => c.path.endsWith("/heartbeat"));
  const heartbeatBody = heartbeatCall!.body as { capabilities?: { supported_capabilities?: string[] } };
  assert(
    heartbeatBody.capabilities?.supported_capabilities?.[0] === "external.social",
    "runtime capabilities included in heartbeat",
  );
  assert(
    calls.every((c) => c.headers["Authorization"] === "Bearer wks_01"),
    "auth header set on every call",
  );
  assert(
    calls.every((c) => c.headers["Manor-Worker-Id"] === "wkr_01"),
    "worker-id header set on every call",
  );
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

function silentLogger() {
  return { info: () => {}, warn: () => {}, error: () => {} };
}

// ── runner ────────────────────────────────────────────────────────────

(async () => {
  await caseSuccessfulLease();
  await caseNeedHuman();
  await caseHandlerThrows();
  await caseNoHandler();
  if (process.exitCode && process.exitCode !== 0) {
    console.error("\nSMOKE FAILED");
    process.exit(process.exitCode);
  }
  console.log("\nSMOKE OK");
})();
