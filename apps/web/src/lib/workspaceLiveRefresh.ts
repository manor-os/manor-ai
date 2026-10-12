export const ACTIVE_WORKFLOW_RUN_POLL_MS = 5_000;
export const ACTIVE_WORKFLOW_FAMILY_POLL_MS = 10_000;
export const WORKFLOW_RUN_EVENT_COALESCE_MS = 300;
export const ACTIVE_SETUP_POLL_MS = 5_000;
export const UNHEALTHY_CONNECTION_POLL_MS = 30_000;
export const HEALTHY_CONNECTION_POLL_MS = 300_000;

export type WorkspaceRealtimeRefreshInput = {
  messageKind?: string | null;
  isKnownMessage: boolean;
  workflowRunId?: string | null;
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
    invalidateChat: !workflowActivity || !input.isKnownMessage,
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
