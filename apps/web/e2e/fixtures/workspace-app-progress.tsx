import React, { useState } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { api } from "../../src/lib/api";
import WorkspaceAppActionFlow from "../../src/components/workspaces/WorkspaceAppActionFlow";
import WorkspaceWorkflowRunHost, { buildWorkspaceWorkflowRunGroups } from "../../src/components/workflows/WorkspaceWorkflowRunHost";
import "../../src/index.css";
import "../../src/components/workspaces/WorkspaceAppView.css";

// All data stays in this fixture; no provider, paid generation, or real run APIs.
const workspaceId = "progress-fixture";
const failed = {
  id: "failed-run", workspace_id: workspaceId, workflow_id: "fixture-flow", binding_id: "fixture-binding", status: "failed",
  created_at: "2026-10-01T07:32:00Z", current_step_id: "produce", current_step_name: "Produce deliverables", workflow_name: "Content production",
  error: "The worker stopped during production.", processed_count: 1, total_count: 2,
  capabilities: { can_control: true }, retry_from_step_id: "produce",
  workflow_steps: [{ id: "read", name: "Read product materials", status: "completed" }, { id: "produce", name: "Produce deliverables", status: "failed" }],
  intervention: { kind: "workflow_retry", step_id: "produce", retry_from_step_id: "produce", options: ["retry"],
    editable_input_schema: { type: "object", properties: { brief: { type: "string", title: "Brief" } }, required: ["brief"] },
    values: { brief: "Create a short product video." } },
};
let current: any = failed;
let retryCalls = 0;
let newCalls = 0;
api.workspaces.setupStatus = async () => ({ ready: true, incomplete_checks: [] } as any);
api.workflows.listRuns = async () => [current];
api.workflows.getRun = async (id) => id === failed.id ? failed : current;
api.workflows.getRunFamily = async () => current.id === failed.id ? [failed] : [failed, current];
api.workflows.retryRun = async (_id, payload) => {
  if (payload.from_step_id !== "produce") throw new Error("Unexpected retry step");
  retryCalls += 1;
  current = { ...failed, id: "retried-run", retry_of_run_id: failed.id, attempt_number: 2, created_at: "2026-10-01T07:33:00Z",
    status: "running", error: null, intervention: null, workflow_steps: [failed.workflow_steps[0], { ...failed.workflow_steps[1], status: "running" }] };
  return current;
};
api.workflows.runBinding = async () => { newCalls += 1; return { ...current, id: "new-run" }; };
api.workflows.cancelRun = api.workflows.pauseRun = api.workflows.resumeRun = async () => { throw new Error("Disabled in isolated fixture"); };

function Fixture() {
  const [appOpen, setAppOpen] = useState(true);
  const queryClient = useQueryClient();
  const chat = useQuery({ queryKey: ["workspace-chat", workspaceId], queryFn: async () => ({
    status: current.status,
    messages: [{ id: `message-${current.id}`, created_at: current.created_at, message_kind: "workflow_activity",
      refs: [{ type: "workflow", id: "fixture-flow", title: "Content production" }, { type: "workflow_run", id: current.id }],
      meta: { workflow_run_id: current.id, workflow_retry_of_run_id: current.retry_of_run_id, workflow_title: "Content production",
        workflow_status: current.status, workflow_current_step_id: "produce", workflow_steps: current.workflow_steps } }],
  }) });
  const finish = () => {
    current = { ...current, status: "completed", processed_count: 2,
      workflow_steps: current.workflow_steps.map((step: any) => ({ ...step, status: "completed" })) };
    for (const key of ["workflow-run", "workspace-chat-workflow-run-family", "workspace-chat"]) void queryClient.invalidateQueries({ queryKey: [key] });
  };
  return <main style={{ maxWidth: 1000, margin: "0 auto", padding: 24 }}>
    <p>Isolated test · no video generation</p>
    <button onClick={() => setAppOpen((value) => !value)}>{appOpen ? "Close App" : "Open App"}</button>
    <button onClick={finish} disabled={current.status !== "running"}>Complete simulated run</button>
    {appOpen && <section aria-label="App">
      <WorkspaceAppActionFlow
        action={{ key: "fixture", kind: "workflow", title: "Content production", binding_id: "fixture-binding", workflow_id: "fixture-flow",
          inputs: [{ key: "brief", label: "Brief", type: "string", required: true, options: [] }] }}
        workspaceId={workspaceId} canStart chatAvailable={false} onUseChat={() => {}} onBack={() => {}} onFollowProgress={() => setAppOpen(false)}
      />
    </section>}
    <section aria-label="Chat" style={{ maxWidth: 580, margin: "24px auto" }}>
      <h2>Workspace chat</h2>
      <WorkspaceWorkflowRunHost workspaceId={workspaceId} groups={buildWorkspaceWorkflowRunGroups(chat.data?.messages || [])} onResolveMessage={() => {}} />
      {chat.data?.status === "completed" && <p role="status">Completed. Results are available in run details.</p>}
      <p>Retry requests: {retryCalls} · New task requests: {newCalls}</p>
    </section>
  </main>;
}
createRoot(document.getElementById("root")!).render(<MemoryRouter><QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}><Fixture /></QueryClientProvider></MemoryRouter>);
