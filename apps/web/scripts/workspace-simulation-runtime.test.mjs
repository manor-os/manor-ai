import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const read = (path) => readFile(new URL(path, import.meta.url), "utf8");
const runtimeSource = await read("../src/components/WorkspaceSimulationRuntime.tsx");
const workspaceChatSource = await read("../src/components/WorkspaceChat.tsx");
const runtimeStyles = await read("../src/components/WorkspaceSimulationRuntime.css");
const artifactGallerySource = await read("../src/components/SimulationArtifactGallery.tsx");
const artifactGalleryStyles = await read("../src/components/SimulationArtifactGallery.css");
const actionCardSource = await read("../src/components/ui/ChatActionCard.tsx");
const apiSource = await read("../src/lib/api.ts");
const installModalSource = await read("../src/components/blueprints/InstallBlueprintModal.tsx");
const simulationContractSource = await read("../../../packages/core/blueprints/simulation.py");
const simulationBackendSource = await read("../../../packages/core/blueprints/simulation_runtime.py");
const appStyles = await read("../src/index.css");

test("sandbox Workspace Chat uses server-persisted messages instead of a client timeline", () => {
  assert.match(workspaceChatSource, /showSimulationRuntime = Boolean/);
  assert.match(workspaceChatSource, /useWorkspaceSimulationRuntime\(\{/);
  assert.match(workspaceChatSource, /\[\.\.\.\(wsMessages as WsMessage\[\]\)\]/);
  assert.doesNotMatch(workspaceChatSource, /simulationRuntime\.messages/);
  assert.doesNotMatch(workspaceChatSource, /handleTimelineResolve/);
  assert.match(runtimeSource, /api\.workspaces\.chat\.simulationRun/);
  assert.match(runtimeSource, /api\.workspaces\.chat\.startSimulationRun/);
  assert.doesNotMatch(runtimeSource, /localStorage|window\.setTimeout|advanceSnapshot/);
});

test("the backend persists each Blueprint stage and stops only at an action card", () => {
  assert.match(simulationBackendSource, /chat_service\.post_message/);
  assert.match(simulationBackendSource, /pending_action\.update/);
  assert.match(simulationBackendSource, /state\["status"\] = "waiting"/);
  assert.match(simulationBackendSource, /advance_simulation_run/);
  assert.match(simulationBackendSource, /resolve_simulation_action/);
  assert.match(simulationBackendSource, /workspace\.settings = settings/);
  assert.doesNotMatch(simulationBackendSource, /requests\.|httpx\.|playwright|selenium/);
});

test("every generated Blueprint scenario covers multiple proposals through Goal completion", () => {
  for (const stage of [
    "goal-request",
    "plan-proposal",
    "workspace-context",
    "operator-input",
    "production-proposal",
    "workflow-run",
    "artifact-packet",
    "delivery-proposal",
    "exact-output-approval",
    "safe-failure",
    "branch-retry",
    "simulation-receipt",
    "goal-completed",
  ]) {
    assert.match(simulationContractSource, new RegExp(`"${stage}"`));
  }
  assert.equal((simulationContractSource.match(/"kind": "proposal"/g) || []).length, 3);
  assert.match(simulationContractSource, /services = _records/);
  assert.match(simulationContractSource, /workflows = _records/);
  assert.match(simulationContractSource, /goals = _records/);
  assert.match(simulationContractSource, /artifact_ids/);
});

test("the frontend has no OPC or LinkedIn-specific simulation script", () => {
  assert.doesNotMatch(runtimeSource, /OPC|LinkedIn|Chrome fallback|content studio/i);
  assert.doesNotMatch(workspaceChatSource, /opc-generate-topic-from-knowledge-v1/);
  assert.match(runtimeSource, /WorkspaceSimulationRun/);
  assert.match(apiSource, /\/simulation-run\/start/);
  assert.match(apiSource, /\/simulation-run\/restart/);
});

test("Blueprint simulation artifacts render as honest file and media previews", () => {
  assert.match(workspaceChatSource, /<SimulationArtifactGallery/);
  assert.match(simulationBackendSource, /meta\["simulation_artifacts"\]/);
  assert.match(artifactGallerySource, /kind === "video"[\s\S]*?<video/);
  assert.match(artifactGallerySource, /kind === "image"[\s\S]*?<img/);
  assert.match(artifactGallerySource, /This is a Blueprint experience preview, not a live artifact/);
  assert.match(artifactGallerySource, /never downloaded, published, or written externally/);
  assert.match(artifactGalleryStyles, /grid-template-columns: repeat\(auto-fit/);
  assert.match(artifactGalleryStyles, /@media \(max-width: 560px\)/);
});

test("runtime controller is compact, accessible, responsive, and action-oriented", () => {
  assert.match(runtimeSource, /role="progressbar"/);
  assert.match(runtimeSource, /aria-live="polite"/);
  assert.match(runtimeSource, /focusWaitingAction/);
  assert.match(runtimeSource, /View decision/);
  assert.match(runtimeStyles, /grid-template-columns/);
  assert.match(runtimeStyles, /@media \(max-width: 860px\)/);
  assert.match(runtimeStyles, /@media \(max-width: 580px\)/);
  assert.match(runtimeStyles, /@media \(prefers-reduced-motion: reduce\)/);
});

test("a simulation install opens the actual Workspace Chat immediately", () => {
  assert.match(installModalSource, /\/chat\?workspace=/);
  assert.match(installModalSource, /simulation=1/);
  assert.match(installModalSource, /component\.install_blueprint_modal\.simulation_started/);
  assert.match(installModalSource, /from "\.\.\/ui\/RadioCard"/);
  assert.match(installModalSource, /from "\.\.\/ui\/Checkbox"/);
});


test("simulation questions use the shared Select, Input, and Textarea controls", () => {
  const needsInputSource = actionCardSource.slice(
    actionCardSource.indexOf("export function NeedsInputCard"),
    actionCardSource.indexOf("export function NeedsLoginCard"),
  );
  assert.match(needsInputSource, /<Select/);
  assert.match(needsInputSource, /<Input/);
  assert.match(needsInputSource, /<Textarea/);
  assert.doesNotMatch(needsInputSource, /<select/);
  assert.match(appStyles, /\.chat-hitl-question-control \.manor-select-trigger/);
  assert.match(appStyles, /box-shadow: 0 0 0 3px var\(--accent-ring\)/);
});

test("resolved actions keep context without a redundant by-you receipt", () => {
  assert.match(actionCardSource, /function ResolvedActionSummary/);
  assert.match(actionCardSource, /revision_requested/);
  assert.match(workspaceChatSource, /msg\.resolved_by_user_id && msg\.resolved_by_user_id !== currentUserId/);
  assert.doesNotMatch(workspaceChatSource, /resolved_by_user_id === currentUserId[\s\S]{0,120}component\.workspace_chat\.you/);
});
