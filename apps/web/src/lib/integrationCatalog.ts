import { queryOptions } from "@tanstack/react-query";
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
