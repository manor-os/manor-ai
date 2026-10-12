import { queryOptions, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api";

/** Shared by Integrations and Chat; connect/disconnect invalidates this same cache. */
export const INTEGRATION_CATALOG_QUERY_KEY = ["mcp-servers"] as const;

export function integrationCatalogQueryOptions(enabled: boolean) {
  return queryOptions({
    queryKey: INTEGRATION_CATALOG_QUERY_KEY,
    queryFn: () => api.integrations.mcpServers(),
    enabled,
    retry: 1,
    staleTime: 60_000,
  });
}

export function integrationCatalogSnapshotQueryOptions(enabled: boolean) {
  return queryOptions({
    queryKey: [...INTEGRATION_CATALOG_QUERY_KEY, "snapshot"],
    queryFn: () => api.integrations.mcpServers({ snapshot: true }),
    enabled,
    retry: false,
    staleTime: 60_000,
  });
}

/** Full data remains authoritative and shared with Chat, including after errors. */
export function useIntegrationCatalog(enabled: boolean) {
  const client = useQueryClient();
  const hasCatalog = client.getQueryData(INTEGRATION_CATALOG_QUERY_KEY) !== undefined;
  const snapshot = useQuery(integrationCatalogSnapshotQueryOptions(enabled && !hasCatalog));
  // Let the cheap read finish first: first-use Skill recovery in the full
  // endpoint must not compete with the snapshot's first paint.
  const catalog = useQuery(integrationCatalogQueryOptions(
    enabled && (hasCatalog || !snapshot.isPending),
  ));
  const data = catalog.data ?? snapshot.data;
  return {
    ...catalog,
    data,
    isLoading: data === undefined && (snapshot.isLoading || catalog.isLoading),
    isFetching: snapshot.isFetching || catalog.isFetching,
  };
}
