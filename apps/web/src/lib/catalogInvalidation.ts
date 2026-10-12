import type { InvalidateQueryFilters, Query, QueryClient } from "@tanstack/react-query";
import { getAuthToken } from "./authToken";
import {
  CATALOG_INVALIDATION_STORAGE_KEY,
  catalogScope,
  invalidateCatalogCache,
  type CatalogKind,
} from "./catalogQueries";

export function catalogListKind(query: Query, scope: string | null): CatalogKind | null {
  const [kind, resource, queryScope] = query.queryKey;
  return scope && queryScope === scope && resource === "list"
    && (kind === "agents" || kind === "skills") ? kind : null;
}

let signalSequence = 0;

export async function invalidateCatalogQueries(
  queryClient: QueryClient,
  kind: CatalogKind,
  filters: InvalidateQueryFilters = { queryKey: [kind] },
): Promise<void> {
  const scope = catalogScope(getAuthToken());
  if (!scope) return;
  // Each successful write is a new generation, including consecutive writes
  // while TanStack already considers the query invalidated or has evicted it.
  invalidateCatalogCache(kind, scope);
  try {
    window.localStorage.setItem(CATALOG_INVALIDATION_STORAGE_KEY, JSON.stringify({
      kind, scope, nonce: `${Date.now()}:${++signalSequence}:${Math.random()}`,
    }));
  } catch {
    // Storage can be disabled; local invalidation must still succeed.
  }
  await queryClient.cancelQueries({
    type: "active",
    predicate: (query) => catalogListKind(query, scope) === kind,
  });
  if (catalogScope(getAuthToken()) !== scope) return;
  // Explicit cancellation also replaces a cold request without cached data,
  // which invalidateQueries alone would otherwise reuse.
  await queryClient.invalidateQueries(filters);
}
