import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { api, type WorkspaceAppAction } from "../../src/lib/api";
import WorkspaceAppActionFlow from "../../src/components/workspaces/WorkspaceAppActionFlow";
import "../../src/index.css";
import "../../src/components/workspaces/WorkspaceAppView.css";

// Isolated UI fixture: no real Workflow, provider, or billable API calls.
const params = new URLSearchParams(location.search);
const workspaceId = "inputs-fixture";
const actions: WorkspaceAppAction[] = [{
  key: "analyze", kind: "workflow", title: "Analyze Reel or Post", binding_id: "analyze-binding", workflow_id: "analyze-flow",
  inputs: [
    { key: "source", label: "Source caption", type: "string", required: true, options: [], schema: { type: "string", "x-ui": { control: "textarea" } } },
    { key: "mode", label: "Analysis mode", type: "string", required: true, options: [], default: "text_only", schema: { type: "string", enum: ["text_only", "full_media"] } },
  ],
}, {
  key: "discover", kind: "workflow", title: "Discover content", binding_id: "discover-binding", workflow_id: "discover-flow",
  inputs: [{ key: "days", label: "Lookback days", type: "number", required: true, options: [], default: 14, schema: { type: "integer", minimum: 1, maximum: 28 } }],
}, { key: "review", kind: "workflow", title: "Review saved content", binding_id: "review-binding", workflow_id: "review-flow", inputs: [] }];
let current: any;
let submitted: unknown;
api.workspaces.setupStatus = async () => ({ ready: !params.has("setup"), incomplete_checks: params.has("setup") ? [{ kind: "missing_skill", reason: "Connect the required Skill." }] : [] } as any);
api.workflows.listRuns = async () => [];
api.workflows.getRun = async () => current;
api.workflows.runBinding = async (bindingId, payload) => {
  submitted = { bindingId, ...payload };
  const action = actions.find((item) => item.binding_id === bindingId)!;
  current = { id: "fixture-run", workspace_id: workspaceId, workflow_id: action.workflow_id, binding_id: bindingId,
    status: "completed", variables: { __result: { summary: "The fixture run completed in the App." } } };
  return current;
};
function Fixture() {
  const [selected, setSelected] = useState(actions[0]);
  const [chatRequests, setChatRequests] = useState(0);
  return <main style={{ maxWidth: 720, padding: 24, margin: "auto" }}>
    <p>Isolated Workflow input test</p>
    <nav aria-label="Test workflows">{actions.map((action) => <button key={action.key} onClick={() => setSelected(action)}>{action.title}</button>)}</nav>
    <WorkspaceAppActionFlow key={selected.key} action={selected} workspaceId={workspaceId}
      canStart={!params.has("readonly")} chatAvailable={!params.has("nochat")}
      onUseChat={() => setChatRequests((count) => count + 1)} onBack={() => setSelected(actions[0])} />
    <p>Chat requests: {chatRequests}</p>
    <button onClick={() => document.getElementById("submitted")!.textContent = JSON.stringify(submitted)}>Show submitted request</button>
    <pre id="submitted" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }} />
  </main>;
}
createRoot(document.getElementById("root")!).render(<MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Fixture /></QueryClientProvider></MemoryRouter>);
