import type { QueryClient } from "@tanstack/react-query";

/**
 * Invalidate every query that renders entity knowledge content.
 *
 * The Knowledge page reads `documents-browse` / `folder-tree` /
 * `documents-trash`, the chat pickers read `documents`, and wiki link
 * resolution reads `fs-wiki-index`. Callers that finished an operation which
 * may have created, changed, or removed documents should call this instead of
 * invalidating `["documents"]` alone — that prefix does NOT match
 * `documents-browse`/`folder-tree`, which is how editor saves and chat-created
 * files used to leave the Knowledge page stale until the user navigated away
 * and back (refetchOnWindowFocus is off globally).
 */
export function invalidateKnowledgeQueries(queryClient: QueryClient): Promise<void> {
  return Promise.all([
    queryClient.invalidateQueries({ queryKey: ["documents"] }),
    queryClient.invalidateQueries({ queryKey: ["documents-browse"] }),
    queryClient.invalidateQueries({ queryKey: ["folder-tree"] }),
    queryClient.invalidateQueries({ queryKey: ["documents-trash"] }),
    queryClient.invalidateQueries({ queryKey: ["fs-wiki-index"] }),
  ]).then(() => undefined);
}
