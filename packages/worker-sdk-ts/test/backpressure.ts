import assert from "node:assert/strict";
import { test } from "node:test";
import { ManorClient } from "../src/client.js";
import { ManorWorker } from "../src/worker.js";
import { HeartbeatCompletedLease, HeartbeatRequest, WorkerClientError } from "../src/types.js";

const response = () => Response.json({
  server_time: new Date().toISOString(), next_heartbeat_in_seconds: 10,
  new_leases: [], instructions: [],
});
const logger = { info() {}, warn() {}, error() {} };
const heartbeatRequest = (overrides: Partial<HeartbeatRequest> = {}): HeartbeatRequest => ({
  state: "idle", active_leases: [], completed_since_last: [],
  capacity: { can_accept_leases: 0 }, ...overrides,
});

interface WorkerInternals {
  loop(): Promise<void>;
  tick(): Promise<number>;
  installSignalHandlers(): void;
  completions: HeartbeatCompletedLease[];
  stop: AbortController;
}

test("an in-flight 401 refreshes only a changed same-worker heartbeat secret once", async () => {
  const calls: { authorization: string; body: string }[] = [];
  const client = new ManorClient({
    endpoint: "http://test", workerId: "worker", secret: "old-secret", maxAttempts: 1,
    fetchImpl: async (_input, init) => {
      calls.push({ authorization: (init?.headers as Record<string, string>).Authorization, body: String(init?.body) });
      if (calls.length === 1) {
        client.configureCredentials("worker", "new-secret");
        return Response.json({ detail: "Invalid worker secret" }, { status: 401 });
      }
      return response();
    },
  });
  await client.heartbeat(heartbeatRequest({ active_leases: [{ lease_id: "lease", attempt_token: "attempt" }] }));
  assert.deepEqual(calls.map(call => call.authorization), ["Bearer old-secret", "Bearer new-secret"]);
  assert.equal(calls[0].body, calls[1].body);
});

for (const [status, rotate] of [[401, false], [403, true], [426, true]] as const) {
  test(`rotation cannot retry policy rejection ${status}, rotate=${rotate}`, async () => {
    let calls = 0;
    const client = new ManorClient({
      endpoint: "http://test", workerId: "worker", secret: "old-secret",
      fetchImpl: async () => {
        calls++;
        if (rotate) client.configureCredentials("worker", "new-secret");
        return Response.json({ detail: "rejected" }, { status });
      },
    });
    await assert.rejects(client.heartbeat(heartbeatRequest()), (error: unknown) => error instanceof WorkerClientError && error.statusCode === status && error.requiresOperatorAction);
    assert.equal(calls, 1);
  });
}

test("credential refresh cannot loop forever when the new secret is also rejected", async () => {
  let calls = 0;
  const client = new ManorClient({
    endpoint: "http://test", workerId: "worker", secret: "old-secret",
    fetchImpl: async () => {
      client.configureCredentials("worker", `secret-${++calls}`);
      return Response.json({ detail: "revoked" }, { status: 401 });
    },
  });
  await assert.rejects(client.heartbeat(heartbeatRequest()), (error: unknown) => error instanceof WorkerClientError && error.statusCode === 401);
  assert.equal(calls, 2);
});

test("401 rotation recovery never replays a lifecycle write", async () => {
  let calls = 0;
  const client = new ManorClient({
    endpoint: "http://test", workerId: "worker", secret: "old-secret",
    fetchImpl: async () => {
      calls++;
      client.configureCredentials("worker", "new-secret");
      return Response.json({ detail: "rejected" }, { status: 401 });
    },
  });
  await assert.rejects(client.completeLease("lease", { result: { receipt: "done" } }, { attemptToken: "attempt" }), WorkerClientError);
  assert.equal(calls, 1);
});

for (const status of [200, 401]) {
  test(`an in-flight worker identity change rejects an old ${status} response`, async () => {
    let calls = 0;
    const client = new ManorClient({
      endpoint: "http://test", workerId: "worker", secret: "secret",
      fetchImpl: async () => {
        calls++;
        client.configureCredentials("other-worker", "other-secret");
        return Response.json({}, { status });
      },
    });
    await assert.rejects(client.heartbeat(heartbeatRequest()), /identity changed/);
    assert.equal(calls, 1);
  });
}

for (const status of [500, 503]) {
  test(`total deadline includes ${status === 503 ? "cooldown" : "backoff"} without early retry`, async context => {
    context.mock.method(Math, "random", () => status === 500 ? 1 : 0);
    let calls = 0;
    const client = new ManorClient({
      endpoint: "http://test", workerId: "worker", secret: "secret", timeoutMs: 30,
      fetchImpl: async () => {
        calls++;
        return Response.json({ detail: "busy" }, { status, headers: status === 503 ? { "Retry-After": "60" } : {} });
      },
    });
    await assert.rejects(client.heartbeat(heartbeatRequest()), (error: unknown) => error instanceof WorkerClientError && error.message.includes("deadline exceeded") && (status !== 503 || error.retryAfterMs > 59_000));
    assert.equal(calls, 1);
  });
}

test("deadline still settles when an injected transport ignores abort", async () => {
  let finish!: (response: Response) => void;
  let calls = 0;
  const client = new ManorClient({
    endpoint: "http://test", workerId: "worker", secret: "secret", timeoutMs: 20,
    fetchImpl: () => {
      calls++;
      return new Promise(resolve => { finish = resolve; });
    },
  });
  await assert.rejects(client.heartbeat(heartbeatRequest()), /deadline exceeded/);
  finish(Response.json({ detail: "busy" }, { status: 503 }));
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 1, "late transport completion cannot start another attempt");
});

for (const pendingResult of [false, true]) {
  test(`shutdown interrupts cooldown; pending result=${pendingResult} retains its acknowledgement boundary`, async context => {
    context.mock.method(Math, "random", () => 0);
    let entered!: () => void;
    const cooling = new Promise<void>(resolve => { entered = resolve; });
    const originalTimer = globalThis.setTimeout;
    context.mock.method(globalThis, "setTimeout", (callback: (...args: unknown[]) => void, delay?: number, ...args: unknown[]) => {
      if (Number(delay) > 50_000) entered();
      return originalTimer(callback, delay, ...args);
    });
    let calls = 0;
    const client = new ManorClient({
      endpoint: "http://test", workerId: "worker", secret: "secret", timeoutMs: 50,
      fetchImpl: async () => {
        calls++;
        return Response.json({ detail: "busy" }, { status: 503, headers: { "Retry-After": "60" } });
      },
    });
    const worker = new ManorWorker({ endpoint: "http://test", workerId: "worker", secret: "secret", client, logger });
    const internal = worker as unknown as WorkerInternals;
    internal.installSignalHandlers = () => {};
    const completion: HeartbeatCompletedLease = { lease_id: "lease", status: "done", result: { receipt: "done" }, attempt_token: "attempt", task_output_value_kind: "task_payload" };
    if (pendingResult) internal.completions.push(completion);
    const running = worker.runForever();
    await cooling;
    await worker.shutdown();
    await running;
    assert.equal(calls, 1, "shutdown/final flush cannot bypass Retry-After");
    assert.deepEqual(internal.completions, pendingResult ? [completion] : []);
  });
}

test("pre-aborted heartbeat never sends a request", async () => {
  let calls = 0;
  const client = new ManorClient({ endpoint: "http://test", workerId: "worker", secret: "secret", fetchImpl: async () => { calls++; return response(); } });
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(client.heartbeat(heartbeatRequest(), controller.signal), /request aborted/);
  assert.equal(calls, 0);
});

test("failed heartbeats retain the successful cadence without accumulating prior errors", async context => {
  context.mock.method(Math, "random", () => 0);
  const worker = new ManorWorker({ endpoint: "http://test", workerId: "worker", secret: "secret", logger });
  const internal = worker as unknown as WorkerInternals;
  const responses = [60, new WorkerClientError("busy", { statusCode: 503, retryAfterMs: 180_000 }), new WorkerClientError("busy", { statusCode: 503, retryAfterMs: 2000 }), new Error("unexpected")];
  const delays: number[] = [];
  internal.tick = async () => {
    const value = responses.shift();
    if (value instanceof Error) throw value;
    return value!;
  };
  const originalTimer = globalThis.setTimeout;
  context.mock.method(globalThis, "setTimeout", (callback: (...args: unknown[]) => void, delay?: number, ...args: unknown[]) => {
    delays.push(Number(delay));
    return originalTimer(() => {
      if (delays.length === 4) void worker.shutdown();
      callback(...args);
    }, 0);
  });
  await internal.loop();
  assert.deepEqual(delays, [60_000, 180_000, 60_000, 60_000]);
});

test("rotation completing during the old 401 body read also recovers and drains the response", async context => {
  const credentials: string[] = [];
  let oldResponse: Response | undefined;
  const client = new ManorClient({
    endpoint: "http://test", workerId: "worker", secret: "old-secret",
    fetchImpl: async (_input, init) => {
      credentials.push((init?.headers as Record<string, string>).Authorization);
      if (credentials.length > 1) return response();
      oldResponse = Response.json({ detail: "Invalid worker secret" }, { status: 401 });
      const readBody = oldResponse.json.bind(oldResponse);
      context.mock.method(oldResponse, "json", async () => {
        client.configureCredentials("worker", "new-secret");
        return readBody();
      });
      return oldResponse;
    },
  });
  await client.heartbeat(heartbeatRequest());
  assert.deepEqual(credentials, ["Bearer old-secret", "Bearer new-secret"]);
  assert(oldResponse!.bodyUsed, "the rejected response must be consumed before retrying");
});

test("identity changing during a successful response body read cannot deliver old leases", async context => {
  let calls = 0;
  const client = new ManorClient({
    endpoint: "http://test", workerId: "worker", secret: "secret",
    fetchImpl: async () => {
      calls++;
      const result = response();
      const readBody = result.json.bind(result);
      context.mock.method(result, "json", async () => {
        client.configureCredentials("other-worker", "other-secret");
        return readBody();
      });
      return result;
    },
  });
  await assert.rejects(client.heartbeat(heartbeatRequest()), (error: unknown) => error instanceof WorkerClientError && error.statusCode === 401);
  assert.equal(calls, 1);
});

test("total deadline covers a stalled response body and discards its late success", async context => {
  let finish!: (body: unknown) => void;
  let calls = 0;
  const client = new ManorClient({
    endpoint: "http://test", workerId: "worker", secret: "secret", timeoutMs: 20,
    fetchImpl: async () => {
      calls++;
      const result = response();
      context.mock.method(result, "json", () => new Promise(resolve => { finish = resolve; }));
      return result;
    },
  });
  await assert.rejects(client.heartbeat(heartbeatRequest()), /deadline exceeded/);
  finish({ server_time: new Date().toISOString(), next_heartbeat_in_seconds: 10, new_leases: [], instructions: [] });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 1);
});

function deferred<T = void>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => { resolve = done; });
  return { promise, resolve };
}

for (const outcome of ["success", "failed", "identity_changed", "heartbeat_stopped"] as const) {
  test(`a pending same-worker rotation preserves auth boundaries: ${outcome}`, async () => {
    const entered = deferred(), release = deferred(), rejected = deferred();
    const calls: { authorization: string; body: string }[] = [];
    const controller = new AbortController();
    const client = new ManorClient({
      endpoint: "http://test", workerId: "worker", secret: "old-secret",
      fetchImpl: async (input, init) => {
        if (String(input).endsWith("rotate-secret")) {
          entered.resolve();
          await release.promise;
          return Response.json({ worker_secret: "new-secret" }, { status: outcome === "failed" ? 403 : 200 });
        }
        calls.push({ authorization: (init?.headers as Record<string, string>).Authorization, body: String(init?.body) });
        if (calls.length === 1) {
          rejected.resolve();
          return Response.json({ detail: "Invalid worker secret" }, { status: 401 });
        }
        return response();
      },
    });
    let rotationSettled = false, heartbeatSettled = false;
    const rotating = client.rotateSecret();
    void rotating.then(() => { rotationSettled = true; }, () => { rotationSettled = true; });
    await entered.promise;
    const heartbeat = client.heartbeat(heartbeatRequest(), controller.signal);
    void heartbeat.then(() => { heartbeatSettled = true; }, () => { heartbeatSettled = true; });
    try {
      await rejected.promise;
      await new Promise(resolve => setImmediate(resolve));
      assert(!heartbeatSettled, "the old 401 must wait for the pending rotation's response");
      if (outcome === "identity_changed") client.configureCredentials("other-worker", "other-secret");
      if (outcome === "heartbeat_stopped") {
        controller.abort();
        await assert.rejects(heartbeat, /request aborted/);
        assert(!rotationSettled, "heartbeat abort cannot abort another caller's rotation");
      }
      release.resolve();
      const rotationResult = (await Promise.allSettled([rotating]))[0];
      if (outcome === "success" || outcome === "heartbeat_stopped") {
        assert.deepEqual(rotationResult, { status: "fulfilled", value: "new-secret" });
      } else {
        assert.equal(rotationResult.status, "rejected");
      }
      if (outcome === "success") {
        await heartbeat;
        assert.deepEqual(calls.map(call => call.authorization), ["Bearer old-secret", "Bearer new-secret"]);
        assert.equal(calls[0].body, calls[1].body);
      } else if (outcome !== "heartbeat_stopped") {
        await assert.rejects(heartbeat, (error: unknown) => error instanceof WorkerClientError && error.statusCode === 401);
        assert.equal(calls.length, 1);
      }
    } finally {
      release.resolve();
      await Promise.allSettled([rotating, heartbeat]);
    }
  });
}

test("the worker survives an old 401 arriving before a successful rotation response", async () => {
  const entered = deferred(), committed = deferred(), rejected = deferred(), release = deferred();
  const credentials: string[] = [];
  const client = new ManorClient({
    endpoint: "http://test", workerId: "worker", secret: "old-secret",
    fetchImpl: async (input, init) => {
      if (String(input).endsWith("rotate-secret")) {
        committed.resolve();
        await release.promise;
        return Response.json({ worker_secret: "new-secret" });
      }
      credentials.push((init?.headers as Record<string, string>).Authorization);
      if (credentials.length === 1) {
        entered.resolve();
        await committed.promise;
        rejected.resolve();
        return Response.json({ detail: "Invalid worker secret" }, { status: 401 });
      }
      return Response.json({ server_time: new Date().toISOString(), next_heartbeat_in_seconds: 10, new_leases: [], instructions: [{ type: "shutdown" }] });
    },
  });
  const worker = new ManorWorker({ endpoint: "http://test", workerId: "worker", secret: "old-secret", client, logger });
  const internal = worker as unknown as WorkerInternals;
  const running = internal.loop();
  await entered.promise;
  const rotating = client.rotateSecret();
  try {
    await rejected.promise;
    await new Promise(resolve => setImmediate(resolve));
    assert(!internal.stop.signal.aborted, "a pending rotation must not stop the worker");
    release.resolve();
    assert.equal(await rotating, "new-secret");
    await running;
    assert.deepEqual(credentials, ["Bearer old-secret", "Bearer new-secret"]);
  } finally {
    release.resolve();
    await worker.shutdown();
    await Promise.allSettled([rotating, running]);
  }
});

test("concurrent same-worker rotations share one request", async () => {
  const entered = deferred(), release = deferred();
  let calls = 0;
  const client = new ManorClient({
    endpoint: "http://test", workerId: "worker", secret: "old-secret",
    fetchImpl: async () => {
      calls++;
      entered.resolve();
      await release.promise;
      return Response.json({ worker_secret: "new-secret" });
    },
  });
  const first = client.rotateSecret();
  await entered.promise;
  const second = client.rotateSecret();
  release.resolve();
  assert.deepEqual(await Promise.all([first, second]), ["new-secret", "new-secret"]);
  assert.equal(calls, 1);
});

test("a pending rotation cannot extend the heartbeat deadline or replay its late success", async () => {
  const entered = deferred(), committed = deferred(), release = deferred();
  let calls = 0;
  const client = new ManorClient({
    endpoint: "http://test", workerId: "worker", secret: "old-secret", timeoutMs: 30,
    fetchImpl: async input => {
      if (String(input).endsWith("rotate-secret")) {
        committed.resolve();
        await release.promise;
        return Response.json({ worker_secret: "new-secret" });
      }
      calls++;
      entered.resolve();
      await committed.promise;
      return Response.json({ detail: "Invalid worker secret" }, { status: 401 });
    },
  });
  const heartbeat = client.heartbeat(heartbeatRequest());
  await entered.promise;
  const rotating = client.rotateSecret();
  void rotating.catch(() => {});
  try {
    await assert.rejects(heartbeat, /deadline exceeded/);
  } finally {
    release.resolve();
    await Promise.allSettled([rotating]);
  }
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(calls, 1);
});
