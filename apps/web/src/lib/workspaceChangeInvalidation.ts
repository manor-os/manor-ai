import type { Query, QueryClient } from "@tanstack/react-query";

const WAIT_MS = 500;
const pendingByClient = new WeakMap<QueryClient, Map<string, ReturnType<typeof setTimeout>>>();

// Chat has its own message events; refetching it for a configuration change
// would only reload the transcript.
const OWN_EVENT_ROOTS = /^workspace-chat/;

/** A query that reads this Workspace: any key that names its id. */
export function readsWorkspace(query: Pick<Query, "queryKey">, workspaceId: string): boolean {
  const [root, ...rest] = query.queryKey;
  if (typeof root === "string" && OWN_EVENT_ROOTS.test(root)) return false;
  return rest.includes(workspaceId);
}

/**
 * Someone changed this Workspace's configuration (a `workspace_changed`
 * event): refetch what reads it, and the Workspace list, which a newly
 * created or delivered Workspace joins. Bursts (one apply writes many rows
 * across commits) collapse into one refetch per Workspace.
 */
export function invalidateChangedWorkspace(queryClient: QueryClient, workspaceId: string): void {
  if (!workspaceId) return;
  let pending = pendingByClient.get(queryClient);
  if (!pending) {
    pending = new Map();
    pendingByClient.set(queryClient, pending);
  }
  if (pending.has(workspaceId)) return;
  pending.set(workspaceId, setTimeout(() => {
    pending?.delete(workspaceId);
    void queryClient.invalidateQueries(
      { predicate: (query) => query.queryKey[0] === "workspaces" || readsWorkspace(query, workspaceId) },
      { cancelRefetch: false },
    );
  }, WAIT_MS));
}
