import type { QueryClient } from "@tanstack/react-query";

/**
 * Invalidate every query that projects Agent Initiative state.
 *
 * Initiative lists are keyed either by subscription or by conversation. Chat
 * tool calls do not receive the mutated Initiative id, so invalidating the
 * shared roots keeps both projections coherent and refreshes any open run
 * history without guessing an id from model output.
 */
export function invalidateAgentInitiativeQueries(
  queryClient: QueryClient,
  initiativeId?: string,
): Promise<void> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["agent-initiatives"] }),
    queryClient.invalidateQueries({
      queryKey: initiativeId
        ? ["agent-initiative-runs", initiativeId]
        : ["agent-initiative-runs"],
    }),
  ]).then(() => undefined);
}
