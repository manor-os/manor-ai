import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import ts from "typescript";

const attentionHelperSource = await readFile(
  new URL("../src/lib/workspaceTaskAttention.ts", import.meta.url),
  "utf8",
);
const transpiledAttentionHelper = ts.transpileModule(attentionHelperSource, {
  compilerOptions: {
    module: ts.ModuleKind.ESNext,
    target: ts.ScriptTarget.ES2020,
  },
  fileName: "workspaceTaskAttention.ts",
  reportDiagnostics: true,
});
assert.deepEqual(transpiledAttentionHelper.diagnostics || [], []);

const attentionHelperUrl = `data:text/javascript;base64,${Buffer.from(
  transpiledAttentionHelper.outputText,
).toString("base64")}`;
const {
  nextSeenTaskAttentionKeys,
  parseSeenTaskAttention,
  serializeSeenTaskAttention,
  unseenTaskAttentionKeys,
  unseenTaskAttentionSeverity,
} = await import(attentionHelperUrl);

test("partial task pages retain seen attention occurrences that move off-page", () => {
  const seen = [
    "task-a:blocked:2026-08-23T10:00:00Z",
    "task-b:failed:2026-08-23T10:01:00Z",
  ];
  const current = ["task-b:failed:2026-08-23T10:01:00Z"];

  const nextSeen = nextSeenTaskAttentionKeys(current, seen, false);

  assert.deepEqual(nextSeen, seen);
  assert.deepEqual(unseenTaskAttentionKeys(current, nextSeen), []);
});

test("new task attention remains unread while the Tasks panel is hidden", () => {
  const seen = ["task-a:blocked:2026-08-23T10:00:00Z"];
  const current = [
    "task-a:blocked:2026-08-23T10:00:00Z",
    "task-b:failed:2026-08-23T10:01:00Z",
  ];

  const nextSeen = nextSeenTaskAttentionKeys(current, seen, false);

  assert.deepEqual(nextSeen, seen);
  assert.deepEqual(
    unseenTaskAttentionKeys(current, nextSeen),
    ["task-b:failed:2026-08-23T10:01:00Z"],
  );
});

test("visible Tasks panel marks current attention and preserves off-page history", () => {
  const seen = ["task-off-page:failed:2026-08-23T09:00:00Z"];
  const current = [
    "task-a:blocked:2026-08-23T10:00:00Z",
    "task-b:failed:2026-08-23T10:01:00Z",
  ];

  const nextSeen = nextSeenTaskAttentionKeys(current, seen, true);

  assert.deepEqual(nextSeen, [...seen, ...current]);
  assert.deepEqual(unseenTaskAttentionKeys(current, nextSeen), []);
});

test("a later occurrence of the same task status is unread", () => {
  const seen = ["task-a:failed:2026-08-23T10:00:00Z"];
  const current = ["task-a:failed:2026-08-23T10:05:00Z"];

  assert.deepEqual(unseenTaskAttentionKeys(current, seen), current);
});

test("task attention severity is based only on unseen occurrences", () => {
  const occurrences = [
    { key: "seen-failure", status: "failed" },
    { key: "new-pause", status: "on_hold" },
  ];

  assert.equal(
    unseenTaskAttentionSeverity(occurrences, ["seen-failure"]),
    "warning",
  );
  assert.equal(
    unseenTaskAttentionSeverity(occurrences, []),
    "danger",
  );
  assert.equal(
    unseenTaskAttentionSeverity(occurrences, occurrences.map(({ key }) => key)),
    undefined,
  );
});

test("seen attention history stays bounded without dropping current items", () => {
  const seen = Array.from(
    { length: 600 },
    (_, index) => `task-${index}:failed:2026-08-23T10:00:00Z`,
  );
  const current = ["task-current:blocked:2026-08-23T11:00:00Z"];

  const nextSeen = nextSeenTaskAttentionKeys(current, seen, true);

  assert.equal(nextSeen.length, 500);
  assert.equal(nextSeen.at(-1), current[0]);
});

test("all current attention stays seen even above the historical cap", () => {
  const current = Array.from(
    { length: 550 },
    (_, index) => `task-current-${index}:failed:2026-08-23T11:00:00Z`,
  );

  const nextSeen = nextSeenTaskAttentionKeys(current, [], true);

  assert.equal(nextSeen.length, current.length);
  assert.deepEqual(unseenTaskAttentionKeys(current, nextSeen), []);
});

test("seen task attention storage accepts legacy snapshots and writes JSON", () => {
  assert.deepEqual(
    parseSeenTaskAttention("task-b:failed|task-a:blocked"),
    ["task-b:failed", "task-a:blocked"],
  );
  assert.deepEqual(
    parseSeenTaskAttention(serializeSeenTaskAttention(["task-b:failed", "task-a:blocked"])),
    ["task-b:failed", "task-a:blocked"],
  );
});
