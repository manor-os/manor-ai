import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const workspaces = await readFile(
  new URL("../src/pages/Workspaces.tsx", import.meta.url),
  "utf8",
);
const workspaceChat = await readFile(
  new URL("../src/components/WorkspaceChat.tsx", import.meta.url),
  "utf8",
);

test("workspace list identifies a simulation before displaying the active lifecycle label", () => {
  assert.match(
    workspaces,
    /const isWorkspaceSimulation = Boolean\([\s\S]*?ws\.settings[\s\S]*?\.sandbox === true[\s\S]*?ws\.kind === "sandbox"[\s\S]*?\);/,
  );
  assert.match(
    workspaces,
    /const statusText = isWorkspaceSimulation[\s\S]*?component\.simulation_artifact_gallery\.workspace_simulation/,
  );
});

test("a paused workspace uses the existing resume label rather than start", () => {
  assert.match(
    workspaceChat,
    /workspace\?\.status === "paused"[\s\S]*?component\.workspace_chat\.resume_workspace_runtime/,
  );
});

test("simulation chat disables ordinary composition and only lets managers open promotion", () => {
  assert.match(
    workspaceChat,
    /const composerDisabled = showSimulationRuntime;/,
  );
  assert.match(
    workspaceChat,
    /onPromoteToLive=\{canToggleWorkspace \? promoteSimulationToLive : undefined\}/,
  );
  assert.match(
    workspaceChat,
    /canToggleWorkspace && !showSimulationRuntime/,
  );
});

test("simulation chat does not expose the ordinary Workspace lifecycle control", () => {
  assert.match(
    workspaceChat,
    /\{canToggleWorkspace && !showSimulationRuntime && \(\s*<AnchoredPopover/,
  );
});
