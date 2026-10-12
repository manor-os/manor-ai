export function workflowPath(workflowId: string): string {
  return `/flows/${encodeURIComponent(workflowId)}`;
}

export function workflowRunPath(workflowId: string, runId: string, workspaceId?: string | null): string {
  if (workflowId) return `${workflowPath(workflowId)}/runs/${encodeURIComponent(runId)}`;
  // Run-only historical references need an authorized lookup before routing.
  return workspaceId
    ? `/workspaces/${encodeURIComponent(workspaceId)}/workflow-runs/${encodeURIComponent(runId)}`
    : "/flows";
}

type WorkflowReference = {
  workflow_id?: string;
  run_id?: string;
  workspace_id?: string | null;
  url?: string;
};

/** Historical messages can carry old URLs without a workspace field. */
export function workflowReferencePath(reference: WorkflowReference): string {
  let workflowId = reference.workflow_id || "";
  let runId = reference.run_id || "";
  let workspaceId = reference.workspace_id || "";
  const declared = String(reference.url || "").trim();
  if (declared.startsWith("/") && !declared.startsWith("//")) {
    try {
      const url = new URL(declared, "https://manor.invalid");
      const workspace = url.pathname.match(/^\/workspaces\/([^/]+)(?:\/workflow-runs\/([^/]+))?$/);
      const flow = url.pathname.match(/^\/flows(?:\/([^/]+)(?:\/runs\/([^/]+))?)?$/);
      const declaredRun = workspace?.[2] ? decodeURIComponent(workspace[2])
        : flow?.[2] ? decodeURIComponent(flow[2])
        : url.searchParams.get("run") || url.searchParams.get("workflow_run") || "";
      if (!runId || !declaredRun || declaredRun === runId) {
        if (workspace && declaredRun) workspaceId ||= decodeURIComponent(workspace[1]);
        if (flow) workflowId ||= flow[1] ? decodeURIComponent(flow[1]) : url.searchParams.get("workflow") || "";
        if (workspace || flow) runId ||= declaredRun;
      }
    } catch {
      // Malformed historical URLs fall back to the structured reference.
    }
  }
  if (runId && (workflowId || workspaceId)) return workflowRunPath(workflowId, runId, workspaceId);
  return workflowId ? workflowPath(workflowId) : "/flows";
}
