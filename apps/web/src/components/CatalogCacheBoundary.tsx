import { useEffect } from "react";
import { useQueryClient, type Query, type QueryClient } from "@tanstack/react-query";
import {
  CATALOG_INVALIDATION_STORAGE_KEY,
  catalogScope,
  clearCatalogCache,
  invalidateCatalogCache,
  type CatalogKind,
} from "../lib/catalogQueries";
import { catalogListKind } from "../lib/catalogInvalidation";
import { useAuthStore } from "../stores/auth";

export const CATALOG_REVALIDATE_EVENT = "manor:catalog-revalidate";
const CATALOG_KINDS: CatalogKind[] = ["agents", "skills"];

export function installCatalogCacheLifecycle(queryClient: QueryClient): () => void {
  let scope = catalogScope(useAuthStore.getState().token);
  let timer: ReturnType<typeof setTimeout> | undefined;
  let disposed = false;
  const pending = new Set<CatalogKind>();
  const forced = new Set<CatalogKind>();
  if (!scope) clearCatalogCache();

  const schedule = () => {
    if (!scope || timer !== undefined || document.visibilityState === "hidden") return;
    timer = setTimeout(() => {
      timer = undefined;
      if (!scope || document.visibilityState === "hidden") return;
      const currentScope = scope;
      const kinds = [...pending];
      pending.clear();
      for (const kind of kinds) {
        const filters = {
          type: "active" as const,
          predicate: (query: Query) => catalogListKind(query, currentScope) === kind,
        };
        if (forced.delete(kind)) {
          // A write during the first load must also replace a pending request
          // without data; cancelRefetch alone reuses that original promise.
          void queryClient.cancelQueries(filters).then(() => {
            if (!disposed && scope === currentScope) {
              return queryClient.refetchQueries(filters, { cancelRefetch: false });
            }
          });
        } else if (!queryClient.getQueryCache().findAll(filters)
          .some((query) => query.state.fetchStatus === "fetching")) {
          // A reconnect/foreground burst should reuse validation already in
          // flight, rather than invalidating its generation and discarding it.
          invalidateCatalogCache(kind, currentScope);
          void queryClient.refetchQueries(filters, { cancelRefetch: false });
        }
      }
    }, 100);
  };

  const onResume = () => {
    for (const kind of CATALOG_KINDS) pending.add(kind);
    schedule();
  };
  const onStorage = (event: StorageEvent) => {
    if (event.key !== CATALOG_INVALIDATION_STORAGE_KEY || !event.newValue || !scope) return;
    let signal: { kind?: unknown; scope?: unknown };
    try { signal = JSON.parse(event.newValue); } catch { return; }
    if (!signal || signal.scope !== scope || (signal.kind !== "agents" && signal.kind !== "skills")) return;
    const kind = signal.kind;
    invalidateCatalogCache(kind, scope);
    void queryClient.invalidateQueries({
      predicate: (query) => catalogListKind(query, scope) === kind,
      refetchType: "none",
    });
    forced.add(kind);
    pending.add(kind);
    schedule();
  };

  const unsubscribeAuth = useAuthStore.subscribe((state) => {
    const nextScope = catalogScope(state.token);
    if (nextScope === scope) return;
    scope = nextScope;
    clearTimeout(timer);
    timer = undefined;
    pending.clear();
    forced.clear();
    clearCatalogCache();
  });
  document.addEventListener("visibilitychange", onResume);
  window.addEventListener("online", onResume);
  window.addEventListener(CATALOG_REVALIDATE_EVENT, onResume);
  window.addEventListener("storage", onStorage);

  return () => {
    disposed = true;
    clearTimeout(timer);
    unsubscribeAuth();
    document.removeEventListener("visibilitychange", onResume);
    window.removeEventListener("online", onResume);
    window.removeEventListener(CATALOG_REVALIDATE_EVENT, onResume);
    window.removeEventListener("storage", onStorage);
  };
}

export default function CatalogCacheBoundary() {
  const queryClient = useQueryClient();
  useEffect(() => installCatalogCacheLifecycle(queryClient), [queryClient]);
  return null;
}
