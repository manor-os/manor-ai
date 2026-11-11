import { useEffect, useMemo } from "react";
import { useCatalogScope } from "./catalogQueries";
import { CATALOG_CACHE_STORAGE_PREFIX, loadCatalogSnapshot, saveCatalogSnapshot } from "./catalogStorage";

/**
 * What this person last saw of a Workspace App (its views, a run they opened), kept in the
 * browser so it opens with that at once and refreshes behind it. Kept per signed-in identity
 * beside the catalog snapshots, so signing out clears it; the oldest go first past a budget.
 * Only response data is kept here, never a file's bytes.
 */
const INDEX_KEY = "app-data-index";
// A finished music video's run reads about 500k characters; the browser allows about 5M in all.
const BUDGET_CHARS = 3_000_000;
const MAX_ENTRY_CHARS = 800_000;

type IndexEntry = { key: string; size: number };

function readIndex(): IndexEntry[] {
  return (loadCatalogSnapshot<IndexEntry>(INDEX_KEY)?.items || []).filter((entry) => entry && typeof entry.key === "string");
}

function forget(key: string) {
  try {
    window.localStorage.removeItem(CATALOG_CACHE_STORAGE_PREFIX + key);
  } catch {
    // Storage may be disabled by the browser.
  }
}

function keep(key: string, value: unknown, savedAt: number) {
  const size = JSON.stringify(value).length;
  const others = readIndex().filter((entry) => entry.key !== key);
  if (size > MAX_ENTRY_CHARS) {
    forget(key);
    saveCatalogSnapshot(INDEX_KEY, { version: "1", items: others, savedAt: Date.now() });
    return;
  }
  const kept: IndexEntry[] = [{ key, size }];
  let total = size;
  for (const entry of others) {
    if (total + entry.size > BUDGET_CHARS) forget(entry.key);
    else { kept.push(entry); total += entry.size; }
  }
  saveCatalogSnapshot(key, { version: "1", items: [value], savedAt });
  saveCatalogSnapshot(INDEX_KEY, { version: "1", items: kept, savedAt: Date.now() });
}

/** The last stored data for a query, and a hook that stores each fresh response of it. */
export function useStoredQueryData<T>(queryKey: readonly unknown[], enabled: boolean) {
  const scope = useCatalogScope();
  const key = scope && enabled ? `app-data:${JSON.stringify([scope, ...queryKey])}` : null;
  const stored = useMemo(() => key ? loadCatalogSnapshot<T>(key) : null, [key]);
  return {
    /** Spread into a query's options: shows the stored data at once and refetches it as stale. */
    initial: stored ? { initialData: stored.items[0], initialDataUpdatedAt: stored.savedAt } : {},
    value: stored?.items[0],
    savedAt: stored?.savedAt,
    useSave(value: T | undefined, updatedAt: number) {
      useEffect(() => {
        if (key && value !== undefined && updatedAt && updatedAt !== stored?.savedAt) keep(key, value, updatedAt);
      }, [key, value, updatedAt]);
    },
  };
}
