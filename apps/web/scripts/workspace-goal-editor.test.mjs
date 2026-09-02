import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const workspaceDetail = await readFile(
  new URL("../src/pages/WorkspaceDetail.tsx", import.meta.url),
  "utf8",
);
const workspaceChat = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);

test("the Goal editor excludes measurement-derived achieved status", () => {
  const start = workspaceDetail.indexOf('value={goalForm.status || "active"}');
  const end = workspaceDetail.indexOf(
    'ariaLabel={t("page.agent_dashboard.status")}',
    start,
  );
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  const statusSelect = workspaceDetail.slice(start, end);

  assert.match(statusSelect, /value: "active"/);
  assert.match(statusSelect, /value: "paused"/);
  assert.match(statusSelect, /value: "abandoned"/);
  assert.doesNotMatch(statusSelect, /value: "achieved"/);
});

test("a paused Workspace disables existing action cards", () => {
  assert.match(workspaceChat, /workspacePaused: boolean/);
  assert.match(
    workspaceChat,
    /workspacePaused=\{workspace\?\.status === "paused"\}/,
  );
  assert.match(
    workspaceChat,
    /disabled=\{workspacePaused \|\| Boolean\(streaming\) \|\| Boolean\(hitl\.resolved\)\}/,
  );
  assert.match(
    workspaceChat,
    /disabled=\{workspacePaused\}/,
  );
});
