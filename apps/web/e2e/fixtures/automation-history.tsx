import { useSyncExternalStore } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { api } from "../../src/lib/api";
import { useAuthStore } from "../../src/stores/auth";
import { useConfigStore } from "../../src/stores/config";
import ScheduledJobs from "../../src/pages/ScheduledJobs";
import JobLogs from "../../src/pages/JobLogs";
import Button from "../../src/components/ui/Button";
import "../../src/index.css";

// All product API methods used here are local mocks. This fixture never starts
// an Automation, Workflow, provider call, or mutation against a real backend.
const mode = new URLSearchParams(window.location.search).get("mode") || "workflow";
const isAgent = mode === "agent";
const isGeneric = mode === "generic";
const isSnapshotNodes = mode === "snapshot-nodes";
const runId = isAgent ? "agent-output-run" : "workflow-result-run";
const startedAt = "2026-10-06T18:00:00Z";
const completedAt = "2026-10-06T18:00:05Z";
const report = {
  summary: "The automation report is ready.",
  files: [{ document_id: "automation-report", name: "automation-report.md", file_type: "md", mime_type: "text/markdown" }],
};
const targetJob = {
  id: "automation-db-id",
  job_id: "business-job-id",
  entity_id: "automation-fixture-entity",
  name: "Daily automation report",
  job_type: "cron",
  schedule_kind: "cron",
  cron_expr: "0 9 * * *",
  timezone: "UTC",
  enabled: true,
  execution_type: isAgent ? "agent" : isGeneric ? "script" : "workflow",
  execution_target: isAgent || isGeneric ? {} : { workflow_id: "automation-report-flow" },
  agent_id: isAgent ? "report-agent" : undefined,
  payload_message: "Prepare the daily automation report from saved evidence.",
  last_run_at: startedAt,
  last_status: mode === "running" ? "running" : "completed",
  consecutive_errors: 0,
  created_at: startedAt,
  can_manage: false,
};
// The requested Automation is deliberately absent from the first server page.
const firstPageJobs = Array.from({ length: 10 }, (_, index) => ({
  ...targetJob,
  id: `other-automation-${index + 1}`,
  job_id: `other-business-job-${index + 1}`,
  name: `Scheduled automation ${index + 1}`,
  execution_type: "agent",
  execution_target: {},
  agent_id: undefined,
  last_run_at: undefined,
  last_status: undefined,
}));

type FixtureState = {
  status: "running" | "completed";
  jobGets: string[];
  detailReads: number;
  workflowReads: number;
  dispatchRequests: number;
  listedRunIds: string[];
};
let state: FixtureState = {
  status: mode === "running" ? "running" : "completed",
  jobGets: [],
  detailReads: 0,
  workflowReads: 0,
  dispatchRequests: 0,
  listedRunIds: [],
};
const listeners = new Set<() => void>();
const subscribe = (listener: () => void) => {
  listeners.add(listener);
  return () => { listeners.delete(listener); };
};
const getSnapshot = () => state;
const update = (patch: Partial<FixtureState>) => {
  state = { ...state, ...patch };
  listeners.forEach((listener) => listener());
};

function scheduledRun() {
  return {
    id: runId,
    job_id: targetJob.job_id,
    status: isAgent ? "success" : state.status,
    trigger_type: "manual",
    started_at: startedAt,
    completed_at: state.status === "completed" ? completedAt : null,
    duration_ms: state.status === "completed" ? 5300 : null,
    result: isAgent ? null : isGeneric
      ? { status: "success", summary: "The generic automation result is ready." }
      : { workflow_run_id: "canonical-workflow-run", status: state.status },
    created_at: startedAt,
  };
}

function workflowRun() {
  return {
    id: "canonical-workflow-run",
    workflow_id: "automation-report-flow",
    entity_id: targetJob.entity_id,
    status: state.status,
    trigger_source: "manual",
    started_at: startedAt,
    completed_at: state.status === "completed" ? completedAt : null,
    current_step_id: isSnapshotNodes ? "final-report" : "prepare-report",
    current_step_name: "Prepare report",
    processed_count: state.status === "completed" ? (isSnapshotNodes ? 2 : 1) : 0,
    total_count: isSnapshotNodes ? 2 : 1,
    artifact_count: state.status === "completed" ? 1 : 0,
    capabilities: { can_control: false },
    definition_snapshot: {
      id: "automation-report-flow",
      name: "Automation report workflow",
      version: 1,
      nodes: isSnapshotNodes ? [
        { id: "research", name: "Research", type: "agent", order: 0, targets: ["final-report"] },
        { id: "final-report", name: "Final report", type: "agent", order: 1, targets: [] },
      ] : [{ id: "prepare-report", name: "Prepare report", type: "agent", order: 0, targets: [] }],
    },
    variables: state.status === "completed" && !isSnapshotNodes ? { __result: report } : {},
    step_results: state.status !== "completed" ? {} : isSnapshotNodes ? {
      research: { status: "completed", output: {
        summary: "Intermediate research notes.",
        files: [{ document_id: "intermediate-report", name: "intermediate-report.md" }],
      } },
      "final-report": { status: "completed", output: report },
    } : { "prepare-report": { status: "completed", output: report } },
    execution_trace: [],
    trigger_data: { scheduled_job_id: targetJob.job_id, scheduled_run_id: runId },
  };
}

const fixtureAgent = { id: "report-agent", name: "Report Agent", status: "active" };
const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
// Keep auth/config/catalog entirely in memory, without replacing browser login.
useAuthStore.setState({ token: null, user: null, isLoading: false });
useConfigStore.setState({ loaded: true, loading: false, flows_available: true, flows_released: true, fs_enabled: true, load: async () => {} });
queryClient.setQueryData(["agents", "list", null], [fixtureAgent]);
api.agents.catalog = async () => ({ version: "automation-fixture-v1", items: [fixtureAgent] } as any);
api.workspaces.list = async () => [];
api.workflows.list = async () => [];
api.workflows.listBindings = async () => [];
api.platform.flags = async () => ({ flags: {} } as any);
api.jobs.list = async (params) => ({
  items: (params?.offset || 0) >= 10 ? [{ ...targetJob, last_status: state.status }] : firstPageJobs,
  total: 11,
  summary_total: 11,
  enabled_total: 11,
  attention_total: 0,
});
// The detail lookup must accept the business job identity and return its DB id.
(api.jobs as typeof api.jobs & { get: (id: string) => Promise<any> }).get = async (id) => {
  update({ jobGets: [...state.jobGets, id] });
  if (id !== targetJob.id && id !== targetJob.job_id) throw new Error("Automation not found in fixture");
  return { ...targetJob, last_status: state.status };
};
api.jobs.runs = async (id) => {
  if (id !== targetJob.id && id !== targetJob.job_id) return [];
  const runs = mode === "old-run"
    ? Array.from({ length: 50 }, (_, index) => ({ ...scheduledRun(), id: `recent-run-${index + 1}` }))
    : [scheduledRun()];
  update({ listedRunIds: runs.map((run) => run.id) });
  return runs;
};
api.jobs.runDetail = async (id, selectedRunId) => {
  if ((id !== targetJob.id && id !== targetJob.job_id) || selectedRunId !== runId) throw new Error("Run not found in fixture");
  update({ detailReads: state.detailReads + 1 });
  return {
    run: scheduledRun(),
    task: isAgent ? {
      id: "automation-agent-task",
      title: "Prepare daily report",
      status: "completed",
      description: targetJob.payload_message,
      response: "The Agent report was saved and is ready to review.",
      timeline: [],
    } : null,
    agent_execution: isAgent ? {
      id: "automation-agent-execution",
      agent_id: fixtureAgent.id,
      status: "completed",
      output_message: "The Agent report was saved and is ready to review.",
      tools_used: ["generate_file"],
      token_usage: { input_tokens: 120, output_tokens: 80 },
    } : null,
  };
};
api.workflows.getRun = async (id) => {
  if (id !== "canonical-workflow-run") throw new Error("Workflow run not found in fixture");
  const previousReads = state.workflowReads;
  update({ workflowReads: state.workflowReads + 1 });
  if (mode === "failure" && previousReads === 0) throw new Error("Canonical Workflow result is temporarily unavailable");
  return workflowRun();
};
api.workflows.getRunFamily = async () => [workflowRun()];
api.workflows.get = async () => ({ id: "automation-report-flow", name: "Automation report workflow", status: "active" });
const rejectDispatch = async (): Promise<never> => {
  update({ dispatchRequests: state.dispatchRequests + 1 });
  throw new Error("This read-only fixture does not dispatch work");
};
api.jobs.runNow = rejectDispatch;
api.jobs.create = rejectDispatch;
api.jobs.update = rejectDispatch;
api.jobs.toggle = rejectDispatch;
api.jobs.delete = rejectDispatch;
api.workflows.runBinding = rejectDispatch;
api.workflows.createBinding = rejectDispatch;
api.workflows.updateBinding = rejectDispatch;
api.workflows.deleteBinding = rejectDispatch;

function Fixture() {
  const location = useLocation();
  const snapshot = useSyncExternalStore(subscribe, getSnapshot);
  return (
    <main style={{ padding: 16, maxWidth: 1180, margin: "0 auto", minWidth: 0 }}>
      <Routes>
        <Route path="/jobs/:jobId/logs" element={<JobLogs />} />
        <Route path="/jobs" element={<ScheduledJobs workflowsEnabled={mode !== "unavailable"} />} />
        <Route path="/viewer/:documentId" element={<p>Automation report document</p>} />
      </Routes>
      <section aria-label="Fixture controls" style={{ marginTop: 24, display: "flex", flexDirection: "column", gap: 8, overflowWrap: "anywhere" }}>
        <Button variant="outline" onClick={() => update({ status: "completed" })} disabled={snapshot.status !== "running"}>
          Complete simulated run
        </Button>
        <output aria-label="Current route">{location.pathname}{location.search}</output>
        <output aria-label="Automation lookups">{JSON.stringify(snapshot.jobGets)}</output>
        <output aria-label="Run detail reads">{snapshot.detailReads}</output>
        <output aria-label="Workflow reads">{snapshot.workflowReads}</output>
        <output aria-label="Dispatch requests">{snapshot.dispatchRequests}</output>
        <output aria-label="Recent run count">{snapshot.listedRunIds.length}</output>
        <output aria-label="Requested run in recent history">{String(snapshot.listedRunIds.includes(runId))}</output>
      </section>
    </main>
  );
}

const initialRoute = mode === "legacy"
  ? `/jobs/business-job-id/logs?run=${runId}`
  : `/jobs?job=business-job-id&run=${runId}`;
createRoot(document.getElementById("root")!).render(
  <MemoryRouter initialEntries={[initialRoute]}>
    <QueryClientProvider client={queryClient}><Fixture /></QueryClientProvider>
  </MemoryRouter>,
);
