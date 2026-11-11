import type { QueryClient } from "@tanstack/react-query";

export const TASK_UPDATE_INVALIDATION_WINDOW_MS = 250;
/** The Workspace list carries aggregated stats, far too costly to reload with every Task event. */
export const WORKSPACE_LIST_REFRESH_INTERVAL_MS = 15_000;

export interface TaskUpdateInvalidationBatcher {
  enqueue(update: { taskId?: string | null }): void;
  flush(): Promise<void>;
  dispose(): void;
}

type QueryInvalidator = Pick<QueryClient, "invalidateQueries" | "getQueryState">;
type TaskUpdateInvalidationOptions = {
  isCurrentScope?: () => boolean;
};

/** Coalesce high-frequency task_update events without delaying a busy stream indefinitely. */
export function createTaskUpdateInvalidationBatcher(
  queryClient: QueryInvalidator,
  { isCurrentScope = () => true }: TaskUpdateInvalidationOptions = {},
): TaskUpdateInvalidationBatcher {
  const taskIds = new Set<string>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let pending = false;
  let disposed = false;
  let workspaceListTimer: ReturnType<typeof setTimeout> | undefined;

  const refreshWorkspaceList = () => {
    if (workspaceListTimer !== undefined) return undefined;
    const age = Date.now() - (queryClient.getQueryState(["workspaces"])?.dataUpdatedAt ?? 0);
    if (age >= WORKSPACE_LIST_REFRESH_INTERVAL_MS) {
      return queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    }
    workspaceListTimer = setTimeout(() => {
      workspaceListTimer = undefined;
      if (!disposed && isCurrentScope()) void queryClient.invalidateQueries({ queryKey: ["workspaces"] });
    }, WORKSPACE_LIST_REFRESH_INTERVAL_MS - age);
    return undefined;
  };

  const flush = async () => {
    if (timer !== undefined) {
      clearTimeout(timer);
      timer = undefined;
    }
    if (disposed || !isCurrentScope()) {
      taskIds.clear();
      pending = false;
      return;
    }
    if (!pending) return;

    const queuedTaskIds = [...taskIds];
    taskIds.clear();
    pending = false;

    await Promise.all([
      ...queuedTaskIds.flatMap((taskId) => [
        queryClient.invalidateQueries({ queryKey: ["task", taskId] }),
        queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] }),
      ]),
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] }),
      queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      refreshWorkspaceList(),
    ]);
  };

  return {
    enqueue({ taskId }) {
      if (disposed || !isCurrentScope()) return;
      pending = true;
      if (taskId) taskIds.add(taskId);
      if (timer === undefined) timer = setTimeout(() => { void flush(); }, TASK_UPDATE_INVALIDATION_WINDOW_MS);
    },
    flush,
    dispose() {
      disposed = true;
      if (timer !== undefined) clearTimeout(timer);
      if (workspaceListTimer !== undefined) clearTimeout(workspaceListTimer);
      timer = undefined;
      workspaceListTimer = undefined;
      taskIds.clear();
      pending = false;
    },
  };
}
