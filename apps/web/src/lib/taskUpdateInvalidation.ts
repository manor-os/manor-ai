import type { QueryClient } from "@tanstack/react-query";

export const TASK_UPDATE_INVALIDATION_WINDOW_MS = 250;

export interface TaskUpdateInvalidationBatcher {
  enqueue(update: { taskId?: string | null; planId?: string | null }): void;
  flush(): Promise<void>;
  dispose(): void;
}

type QueryInvalidator = Pick<QueryClient, "invalidateQueries">;
type TaskUpdateInvalidationOptions = {
  isCurrentScope?: () => boolean;
};

/** Coalesce high-frequency task_update events without delaying a busy stream indefinitely. */
export function createTaskUpdateInvalidationBatcher(
  queryClient: QueryInvalidator,
  { isCurrentScope = () => true }: TaskUpdateInvalidationOptions = {},
): TaskUpdateInvalidationBatcher {
  const taskIds = new Set<string>();
  const planIds = new Set<string>();
  let timer: ReturnType<typeof setTimeout> | undefined;
  let pending = false;
  let disposed = false;

  const flush = async () => {
    if (timer !== undefined) {
      clearTimeout(timer);
      timer = undefined;
    }
    if (disposed || !isCurrentScope()) {
      taskIds.clear();
      planIds.clear();
      pending = false;
      return;
    }
    if (!pending) return;

    const queuedTaskIds = [...taskIds];
    const queuedPlanIds = [...planIds];
    taskIds.clear();
    planIds.clear();
    pending = false;

    await Promise.all([
      ...queuedTaskIds.flatMap((taskId) => [
        queryClient.invalidateQueries({ queryKey: ["task", taskId] }),
        queryClient.invalidateQueries({ queryKey: ["task-logs", taskId] }),
        queryClient.invalidateQueries({ queryKey: ["task-plans", taskId] }),
      ]),
      ...queuedPlanIds.map((planId) => queryClient.invalidateQueries({ queryKey: ["plan-steps", planId] })),
      queryClient.invalidateQueries({ queryKey: ["taskBoard"] }),
      queryClient.invalidateQueries({ queryKey: ["tasks"] }),
      queryClient.invalidateQueries({ queryKey: ["workspaces"] }),
    ]);
  };

  return {
    enqueue({ taskId, planId }) {
      if (disposed || !isCurrentScope()) return;
      pending = true;
      if (taskId) taskIds.add(taskId);
      if (planId) planIds.add(planId);
      if (timer === undefined) timer = setTimeout(() => { void flush(); }, TASK_UPDATE_INVALIDATION_WINDOW_MS);
    },
    flush,
    dispose() {
      disposed = true;
      if (timer !== undefined) clearTimeout(timer);
      timer = undefined;
      taskIds.clear();
      planIds.clear();
      pending = false;
    },
  };
}
