import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { api } from "../../src/lib/api";
import { setLocale } from "../../src/lib/i18n";
import WorkspaceAppActionFlow from "../../src/components/workspaces/WorkspaceAppActionFlow";
import "../../src/index.css";
import "../../src/components/workspaces/WorkspaceAppView.css";

// Isolated fixture: no requests or media production reach the real service.
const failed = {
  id: "failed-run", workspace_id: "retry-fixture", workflow_id: "fixture-flow", binding_id: "fixture-binding", status: "failed",
  current_step_id: "produce", current_step_name: "Produce deliverables", workflow_name: "Content production",
  error: "The worker stopped during production.", processed_count: 1, total_count: 2,
  capabilities: { can_control: true }, retry_from_step_id: "produce",
  workflow_steps: [{ id: "read", name: "Read product materials", status: "completed" }, { id: "produce", name: "Produce deliverables", status: "failed" }],
  step_results: { read: { status: "completed", output: "Accepted product evidence" } },
  intervention: { kind: "workflow_retry", step_id: "produce", retry_from_step_id: "produce", options: ["retry", "cancel"],
    observed_problem: "The worker stopped during production.", preserved_receipts: [],
    editable_input_schema: { type: "object", properties: { brief: { type: "string", title: "Brief" } }, required: ["brief"] },
    values: { brief: "Create a short product video." } },
};
let current = failed;
let retryCalls = 0;
let newCalls = 0;
let failNext = false;
api.workspaces.setupStatus = async () => ({ ready: true, incomplete_checks: [] } as any);
api.workflows.listRuns = async () => [current];
api.workflows.getRun = async () => current;
api.workflows.retryRun = async (_id, payload) => {
  retryCalls += 1;
  if (failNext) { failNext = false; throw new Error("Retry request unavailable. Try again."); }
  if (payload.from_step_id !== "produce") throw new Error("Unexpected retry step");
  current = { ...failed, id: "retried-run", status: "running", error: null, intervention: null,
    workflow_steps: [failed.workflow_steps[0], { ...failed.workflow_steps[1], status: "running" }] } as any;
  return current;
};
api.workflows.runBinding = async () => { newCalls += 1; return { ...current, id: "new-run" }; };

function Fixture() {
  const [, refresh] = useState(0);
  return <main style={{ maxWidth: 900, margin: "0 auto", padding: 24 }}>
    <p>Isolated test · no video generation</p>
    <button onClick={() => { failNext = true; }}>Simulate retry failure</button>
    <WorkspaceAppActionFlow
      action={{ key: "fixture", kind: "workflow", title: "Content production", binding_id: "fixture-binding", workflow_id: "fixture-flow", inputs: [{ key: "brief", label: "Brief", type: "string", required: true, options: [] }] }}
      workspaceId="retry-fixture" canStart chatAvailable={false} onUseChat={() => {}} onBack={() => {}}
    />
    <button onClick={() => refresh((value) => value + 1)}>Check requests</button>
    <p>Retry requests: {retryCalls} · New task requests: {newCalls}</p>
  </main>;
}

void setLocale(new URLSearchParams(location.search).get("locale") === "zh" ? "zh" : "en").then(() => {
  createRoot(document.getElementById("root")!).render(<MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Fixture /></QueryClientProvider></MemoryRouter>);
});
