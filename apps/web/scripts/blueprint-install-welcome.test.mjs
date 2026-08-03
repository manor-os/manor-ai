#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const installModal = readFileSync(
  new URL("../src/components/blueprints/InstallBlueprintModal.tsx", import.meta.url),
  "utf8",
);
const workspaceDetail = readFileSync(
  new URL("../src/pages/WorkspaceDetail.tsx", import.meta.url),
  "utf8",
);

test("blueprint install opens the standard workspace-created welcome dialog", () => {
  assert.match(
    installModal,
    /navigate\(`\/workspaces\/\$\{result\.workspace_id\}\?created=1`\)/,
  );
  assert.doesNotMatch(
    installModal,
    /navigate\(`\/workspaces\/\$\{result\.workspace_id\}\/simulation-report`\)/,
  );
  assert.match(
    workspaceDetail,
    /searchParams\.get\("created"\) === "1"/,
  );
  assert.match(
    workspaceDetail,
    /title=\{t\("page\.workspace_detail\.welcome_title"\)\}/,
  );
});
