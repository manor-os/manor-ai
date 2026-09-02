#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import vm from "node:vm";
import ts from "typescript";

const policySource = await readFile(
  new URL("../src/utils/recoverableErrors.ts", import.meta.url),
  "utf8",
);
const errorBoundarySource = await readFile(
  new URL("../src/components/ErrorBoundary.tsx", import.meta.url),
  "utf8",
);
const routeErrorBoundarySource = await readFile(
  new URL("../src/components/RouteErrorBoundary.tsx", import.meta.url),
  "utf8",
);
const versionRefreshManagerSource = await readFile(
  new URL("../src/VersionRefreshManager.tsx", import.meta.url),
  "utf8",
);

const compiledPolicy = ts.transpileModule(policySource, {
  compilerOptions: {
    module: ts.ModuleKind.CommonJS,
    target: ts.ScriptTarget.ES2022,
  },
}).outputText;

function loadPolicy({
  storedValue = null,
  version = "build-a",
  now = 1_000,
  failStorageWrites = false,
  pathname = "/tasks",
} = {}) {
  const values = new Map();
  const scheduledReloads = [];
  if (storedValue !== null) values.set("manor:eb-auto-reload", storedValue);
  let currentTime = now;
  const commonJsExports = {};
  const context = {
    __APP_VERSION__: version,
    DOMException,
    exports: commonJsExports,
    module: { exports: commonJsExports },
    sessionStorage: {
      getItem: (key) => values.get(key) ?? null,
      removeItem: (key) => values.delete(key),
      setItem: (key, value) => {
        if (failStorageWrites) throw new DOMException("Storage denied", "SecurityError");
        values.set(key, value);
      },
    },
    Date: class extends Date {
      static now() {
        return currentTime;
      }
    },
    window: {
      location: { pathname, reload: () => {} },
      setTimeout: (callback, delay) => {
        scheduledReloads.push({ callback, delay });
        return scheduledReloads.length;
      },
    },
  };

  vm.runInNewContext(compiledPolicy, context, { filename: "recoverableErrors.js" });
  return {
    policy: context.module.exports,
    readStoredState: () => JSON.parse(values.get("manor:eb-auto-reload")),
    scheduledReloads,
    setNow: (nextTime) => {
      currentTime = nextTime;
    },
  };
}

test("recoverable errors retry at most three times with bounded backoff", () => {
  const { policy, readStoredState } = loadPolicy();

  assert.equal(policy.shouldAutoReloadForRecoverableError(), true);
  assert.equal(policy.getRecoverableErrorAutoReloadDelayMs(), 0);

  policy.markRecoverableErrorAutoReloadAttempt();
  assert.equal(readStoredState().attempts, 1);
  assert.equal(policy.getRecoverableErrorAutoReloadDelayMs(), 1_500);

  policy.markRecoverableErrorAutoReloadAttempt();
  assert.equal(readStoredState().attempts, 2);
  assert.equal(policy.getRecoverableErrorAutoReloadDelayMs(), 4_000);

  policy.markRecoverableErrorAutoReloadAttempt();
  assert.equal(readStoredState().attempts, 3);
  assert.equal(policy.shouldAutoReloadForRecoverableError(), false);
});

test("Safari's module import failure is classified as a stale chunk", () => {
  const { policy } = loadPolicy();
  assert.equal(
    policy.isStaleChunkError("TypeError Importing a module script failed."),
    true,
  );
});

test("duplicate observers schedule only one reload for the same page instance", () => {
  const { policy, readStoredState, scheduledReloads } = loadPolicy();

  assert.equal(policy.scheduleRecoverableErrorAutoReload(), 0);
  assert.equal(policy.scheduleRecoverableErrorAutoReload(), null);
  assert.equal(readStoredState().attempts, 1);
  assert.deepEqual(scheduledReloads.map(({ delay }) => delay), [0]);
});

test("restricted storage fails safe instead of entering a reload loop", () => {
  const { policy, scheduledReloads } = loadPolicy({ failStorageWrites: true });

  assert.equal(policy.scheduleRecoverableErrorAutoReload(), null);
  assert.equal(scheduledReloads.length, 0);
});

test("stateful editors require a manual reload so unsaved work is preserved", () => {
  for (const pathname of ["/editor/doc-1", "/video-editor/doc-1"]) {
    const { policy, scheduledReloads } = loadPolicy({ pathname });

    assert.equal(policy.isStatefulEditingRoute(), true);
    assert.equal(policy.shouldAutoReloadForRecoverableError(), false);
    assert.equal(policy.scheduleRecoverableErrorAutoReload(), null);
    assert.equal(scheduledReloads.length, 0);
  }

  const { policy, scheduledReloads } = loadPolicy({ pathname: "/tasks/task-1" });
  assert.equal(policy.isStatefulEditingRoute(), false);
  assert.equal(policy.scheduleRecoverableErrorAutoReload(), 0);
  assert.equal(scheduledReloads.length, 1);
});

test("stateful editor matching does not block similarly named routes", () => {
  const { policy } = loadPolicy({ pathname: "/editorial" });
  assert.equal(policy.isStatefulEditingRoute(), false);
});

test("the retry budget resets after the rollout window", () => {
  const { policy, setNow } = loadPolicy();
  policy.markRecoverableErrorAutoReloadAttempt();
  policy.markRecoverableErrorAutoReloadAttempt();
  policy.markRecoverableErrorAutoReloadAttempt();
  assert.equal(policy.shouldAutoReloadForRecoverableError(), false);

  setNow(61_000);
  assert.equal(policy.shouldAutoReloadForRecoverableError(), true);
  assert.equal(policy.getRecoverableErrorAutoReloadDelayMs(), 0);
});

test("one-attempt records from older builds keep the remaining retry budget", () => {
  const { policy, readStoredState } = loadPolicy({ storedValue: "build-a" });

  assert.equal(policy.shouldAutoReloadForRecoverableError(), true);
  assert.equal(policy.getRecoverableErrorAutoReloadDelayMs(), 1_500);
  policy.markRecoverableErrorAutoReloadAttempt();
  assert.equal(readStoredState().attempts, 2);
});

test("both React boundaries schedule delayed recovery and stale routes stay actionable", () => {
  assert.match(errorBoundarySource, /scheduleRecoverableErrorAutoReload\(\)/);
  assert.match(routeErrorBoundarySource, /scheduleRecoverableErrorAutoReload\(\)/);
  assert.match(errorBoundarySource, /isRecoverableErrorAutoReloadScheduled\(\) \|\| shouldAutoReloadForRecoverableError\(\)/);
  assert.match(routeErrorBoundarySource, /isRecoverableErrorAutoReloadScheduled\(\) \|\| shouldAutoReloadForRecoverableError\(\)/);
  assert.match(routeErrorBoundarySource, /stale[\s\S]*?app_was_updated/);
  assert.match(routeErrorBoundarySource, /\{!stale && \([\s\S]*?go_home/);
});

test("the global version manager shares the same retry budget", () => {
  assert.match(versionRefreshManagerSource, /scheduleRecoverableErrorAutoReload\(\)/);
  assert.doesNotMatch(versionRefreshManagerSource, /manor-chunk-auto-reload-attempted/);
  assert.doesNotMatch(versionRefreshManagerSource, /CHUNK_AUTO_RELOAD_TTL_MS/);
});
