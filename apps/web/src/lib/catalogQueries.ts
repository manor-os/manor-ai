import { api } from "./api";
import { decodeAuthTokenClaims, getAuthToken } from "./authToken";
import { useAuthStore } from "../stores/auth";
import {
  CATALOG_CACHE_MAX_AGE_MS, catalogSnapshotIsCurrent, clearCatalogStorage,
  loadCatalogSnapshot, saveCatalogSnapshot, type CatalogSnapshot,
} from "./catalogStorage";

export { CATALOG_CACHE_MAX_AGE_MS, CATALOG_CACHE_STORAGE_PREFIX } from "./catalogStorage";
export const CATALOG_INVALIDATION_STORAGE_KEY = "manor_catalog_invalidation_v1";
// Query reads validate a version. The much larger payload is retained for a week.
export const CATALOG_STALE_TIME_MS = 0;
export const DEPLOYMENT_STALE_TIME_MS = 60_000;
export type CatalogKind = "agents" | "skills";

const snapshots = new Map<string, CatalogSnapshot<unknown>>();
const validated = new Map<string, { generation: string; checkedAt: number }>();
const inFlight = new Map<string, { generation: string; promise: Promise<unknown[]> }>();
const generations = new Map<string, number>();
let cacheEpoch = 0;

export function invalidateCatalogCache(kind: CatalogKind, scope: string): void {
  const family = JSON.stringify([kind, scope]);
  generations.set(family, (generations.get(family) || 0) + 1);
}

export function clearCatalogCache(): void {
  cacheEpoch += 1;
  generations.clear();
  snapshots.clear();
  validated.clear();
  inFlight.clear();
  clearCatalogStorage();
}

// Match the effective identity, including permission changes, without putting
// credentials in query keys or discarding cached lists on ordinary renewal.
export function catalogScope(token: string | null | undefined): string | null {
  const claims = decodeAuthTokenClaims(token);
  if (!claims?.sub || !claims.entity_id) return null;
  return JSON.stringify([
    claims.entity_id, claims.sub, claims.role, claims.token_version,
    claims.typ, claims.amr || [],
  ]);
}

export function useCatalogScope(): string | null {
  return useAuthStore((state) => catalogScope(state.token));
}

function assertCurrentScope(scope: string | null): asserts scope is string {
  if (!scope || catalogScope(getAuthToken()) !== scope) {
    throw new DOMException("Session changed while reading the catalog", "AbortError");
  }
}

async function readCatalog<T>(scope: string | null, read: () => Promise<T>): Promise<T> {
  assertCurrentScope(scope);
  const result = await read();
  assertCurrentScope(scope);
  return result;
}

async function readCachedCatalog<T>(
  kind: CatalogKind,
  scope: string | null,
  filters: object,
  read: (knownVersion?: string) => Promise<{ version: string; items: T[] | null }>,
): Promise<T[]> {
  assertCurrentScope(scope);
  const family = JSON.stringify([kind, scope]);
  const key = JSON.stringify([kind, scope, filters]);
  const currentGeneration = () => `${cacheEpoch}:${generations.get(family) || 0}`;
  const generation = currentGeneration();
  let snapshot = snapshots.get(key) as CatalogSnapshot<T> | undefined;
  if (snapshot && !catalogSnapshotIsCurrent(snapshot)) {
    snapshots.delete(key);
    snapshot = undefined;
  }
  snapshot ??= loadCatalogSnapshot<T>(key) || undefined;
  if (snapshot) snapshots.set(key, snapshot);
  const checked = validated.get(key);
  // Several observers can mount together. Coalesce that burst without treating
  // a previous browser session's persisted payload as already authorized.
  if (snapshot && checked?.generation === generation && Date.now() - checked.checkedAt < 1_000) {
    return snapshot.items;
  }
  const pending = inFlight.get(key);
  if (pending?.generation === generation) return pending.promise as Promise<T[]>;

  const promise = (async () => {
    const response = await read(snapshot?.version);
    assertCurrentScope(scope);
    if (currentGeneration() !== generation) {
      throw new DOMException("Catalog changed while checking its version", "AbortError");
    }
    if (typeof response.version !== "string" || !response.version
      || (!Array.isArray(response.items) && response.items !== null)) {
      throw new Error("Invalid catalog response");
    }
    if (response.items === null) {
      if (!snapshot || snapshot.version !== response.version) {
        throw new Error("Catalog version has no matching cached data");
      }
    } else {
      snapshot = { version: response.version, items: response.items, savedAt: Date.now() };
      snapshots.set(key, snapshot);
      saveCatalogSnapshot(key, snapshot);
    }
    validated.set(key, { generation, checkedAt: Date.now() });
    return snapshot!.items;
  })();
  inFlight.set(key, { generation, promise });
  try {
    return await promise;
  } finally {
    if (inFlight.get(key)?.promise === promise) inFlight.delete(key);
  }
}

export function agentListQueryOptions(scope: string | null) {
  return {
    queryKey: ["agents", "list", scope] as const,
    queryFn: () => readCachedCatalog("agents", scope, {}, (version) => api.agents.catalog(version)),
    staleTime: CATALOG_STALE_TIME_MS,
    gcTime: CATALOG_CACHE_MAX_AGE_MS,
    refetchOnMount: "always" as const,
    refetchOnReconnect: false,
    refetchOnWindowFocus: false,
    enabled: !!scope,
  };
}

export function skillListQueryOptions(
  scope: string | null,
  params?: { include_platform?: boolean; category?: string },
) {
  const filters = {
    include_platform: Boolean(params?.include_platform),
    category: params?.category || "",
  };
  return {
    queryKey: ["skills", "list", scope, filters] as const,
    queryFn: () => readCachedCatalog("skills", scope, filters, (version) => api.skills.catalog({ ...filters, known_version: version })),
    staleTime: CATALOG_STALE_TIME_MS,
    gcTime: CATALOG_CACHE_MAX_AGE_MS,
    refetchOnMount: "always" as const,
    refetchOnReconnect: false,
    refetchOnWindowFocus: false,
    enabled: !!scope,
  };
}

export function deploymentQueryKey(scope: string | null, agentId: string | undefined) {
  return ["agent-deployments", agentId, scope] as const;
}

export function deploymentListQueryKey(scope: string | null, agentIds: readonly string[]) {
  return ["agent-deployments", "my", scope, [...agentIds].sort().join(",")] as const;
}

export function deploymentQueryOptions(scope: string | null, agentId: string | undefined) {
  return {
    queryKey: deploymentQueryKey(scope, agentId),
    queryFn: () => readCatalog(scope, () => {
      if (!agentId) throw new Error("Agent ID is required");
      return api.agents.deployments(agentId);
    }),
    staleTime: DEPLOYMENT_STALE_TIME_MS,
    enabled: !!scope && !!agentId,
  };
}

export function deploymentListQueryOptions(scope: string | null, agentIds: readonly string[]) {
  return {
    queryKey: deploymentListQueryKey(scope, agentIds),
    queryFn: () => readCatalog(scope, () => api.agents.allDeployments()),
    staleTime: DEPLOYMENT_STALE_TIME_MS,
    enabled: !!scope && agentIds.length > 0,
  };
}
