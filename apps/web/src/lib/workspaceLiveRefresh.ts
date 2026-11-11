export const ACTIVE_WORKFLOW_RUN_POLL_MS = 5_000;
export const ACTIVE_WORKFLOW_FAMILY_POLL_MS = 10_000;
export const WORKFLOW_RUN_EVENT_COALESCE_MS = 300;
export const ACTIVE_SETUP_POLL_MS = 5_000;
export const UNHEALTHY_CONNECTION_POLL_MS = 30_000;
export const HEALTHY_CONNECTION_POLL_MS = 300_000;

export function workflowRunListRefetchInterval(
  runs: ReadonlyArray<{ status?: string | null }>,
): number {
  return runs.some((run) => ["pending", "running"].includes(String(run.status).toLowerCase()))
    ? ACTIVE_WORKFLOW_FAMILY_POLL_MS
    : 60_000;
}

export function selectedWorkflowRunRefetchInterval(status?: string | null): number | false {
  const normalized = String(status || "").toLowerCase();
  if (["pending", "running"].includes(normalized)) return ACTIVE_WORKFLOW_RUN_POLL_MS;
  return normalized === "paused" ? 30_000 : false;
}

type RunSnapshot = { status?: string | null; updated_at?: string | null; completed_at?: string | null };

/** List and detail requests can finish out of order; server revisions outrank arrival time. */
export function isOlderWorkflowRunSnapshot(current: RunSnapshot, incoming: RunSnapshot): boolean {
  const currentAt = Date.parse(current.updated_at || current.completed_at || "");
  const incomingAt = Date.parse(incoming.updated_at || incoming.completed_at || "");
  if (Number.isFinite(currentAt) && Number.isFinite(incomingAt) && currentAt !== incomingAt) {
    return incomingAt < currentAt;
  }
  // Without a newer server revision, an old active read cannot undo a final receipt.
  // A nested retry may legitimately project a newer active status onto the same id.
  return ["completed", "failed", "cancelled"].includes(String(current.status))
    && ["pending", "running", "paused"].includes(String(incoming.status));
}

export type WorkspaceRealtimeRefreshInput = {
  messageKind?: string | null;
  isKnownMessage: boolean;
  workflowRunId?: string | null;
  /** A known run row moved by its event's progress; false when it could not be. */
  progressApplied?: boolean;
};

export type WorkspaceRealtimeRefreshPlan = {
  invalidateChat: boolean;
  workflowRunId: string | null;
};

export function workspaceRealtimeRefreshPlan(
  input: WorkspaceRealtimeRefreshInput,
): WorkspaceRealtimeRefreshPlan {
  const workflowActivity = input.messageKind === "workflow_activity";
  return {
    invalidateChat: !workflowActivity || !input.isKnownMessage || input.progressApplied === false,
    workflowRunId: input.workflowRunId || null,
  };
}

export function workspaceSetupRefetchInterval(input: {
  enabled: boolean;
  ready?: boolean;
  hasSetupJob?: boolean;
  automaticSetupState?: string | null;
}): number | false {
  return input.enabled
    && input.ready === false
    && input.hasSetupJob
    && input.automaticSetupState !== "failed"
    ? ACTIVE_SETUP_POLL_MS
    : false;
}

export function workspaceConnectionRefetchInterval(input: {
  hasData: boolean;
  isError: boolean;
  hasRequiredIssue: boolean;
}): number {
  return !input.hasData || input.isError || input.hasRequiredIssue
    ? UNHEALTHY_CONNECTION_POLL_MS
    : HEALTHY_CONNECTION_POLL_MS;
}

export function createKeyedTrailingScheduler(
  delayMs: number,
  callback: (key: string) => void | Promise<void>,
): {
  schedule: (key: string) => void;
  cancelAll: () => void;
} {
  const timers = new Map<string, ReturnType<typeof setTimeout>>();
  return {
    schedule(key: string) {
      const current = timers.get(key);
      if (current !== undefined) clearTimeout(current);
      timers.set(key, setTimeout(() => {
        timers.delete(key);
        void callback(key);
      }, delayMs));
    },
    cancelAll() {
      timers.forEach((timer) => clearTimeout(timer));
      timers.clear();
    },
  };
}
