export const CATALOG_CACHE_MAX_AGE_MS = 7 * 24 * 60 * 60_000;
export const CATALOG_CACHE_STORAGE_PREFIX = "manor_catalog_v1:";

export interface CatalogSnapshot<T> {
  version: string;
  items: T[];
  savedAt: number;
}

const CREDENTIAL_KEYS = new Set([
  "envvarvalues", "apikey", "token", "accesstoken", "refreshtoken",
  "clientsecret", "secret", "password", "authorization", "proxyauthorization", "credentials",
]);

function hasValue(value: unknown): boolean {
  if (typeof value === "string") return value.trim().length > 0;
  if (value && typeof value === "object") return Object.values(value).some(hasValue);
  return value !== null && value !== undefined && value !== false;
}

function containsCredentials(value: unknown): boolean {
  if (!value || typeof value !== "object") return false;
  return Object.entries(value).some(([key, child]) => {
    const normalized = key.toLowerCase().replace(/[_-]/g, "");
    if (CREDENTIAL_KEYS.has(normalized) && hasValue(child)) return true;
    if (normalized === "envvars" && Array.isArray(child)
      && child.some((entry) => entry && typeof entry === "object"
        && (hasValue(entry.value) || hasValue(entry.default)))) return true;
    return containsCredentials(child);
  });
}

export function catalogSnapshotIsCurrent<T>(snapshot: CatalogSnapshot<T>): boolean {
  const age = Date.now() - snapshot.savedAt;
  return Number.isFinite(snapshot.savedAt) && age >= 0 && age < CATALOG_CACHE_MAX_AGE_MS;
}

export function loadCatalogSnapshot<T>(key: string): CatalogSnapshot<T> | null {
  try {
    const raw = window.localStorage.getItem(CATALOG_CACHE_STORAGE_PREFIX + key);
    if (!raw) return null;
    const value = JSON.parse(raw);
    if (
      value && typeof value.version === "string" && value.version.length > 0
      && Array.isArray(value.items) && typeof value.savedAt === "number"
      && catalogSnapshotIsCurrent(value) && !containsCredentials(value.items)
    ) return value;
    window.localStorage.removeItem(CATALOG_CACHE_STORAGE_PREFIX + key);
  } catch {
    // Blocked, full, or invalid browser storage must not block normal reads.
  }
  return null;
}

export function saveCatalogSnapshot<T>(key: string, snapshot: CatalogSnapshot<T>): void {
  try {
    // Legacy skill responses can include credential values used by the editor.
    // Keep those complete responses only in memory; do not persist a partial
    // snapshot that would later be mistaken for a complete version match.
    if (containsCredentials(snapshot.items)) {
      window.localStorage.removeItem(CATALOG_CACHE_STORAGE_PREFIX + key);
      return;
    }
    window.localStorage.setItem(CATALOG_CACHE_STORAGE_PREFIX + key, JSON.stringify(snapshot));
  } catch {
    // The in-memory snapshot still works when persistence is unavailable.
  }
}

export function clearCatalogStorage(): void {
  try {
    const keys: string[] = [];
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index);
      if (key?.startsWith(CATALOG_CACHE_STORAGE_PREFIX)) keys.push(key);
    }
    keys.forEach((key) => window.localStorage.removeItem(key));
  } catch {
    // Storage may be disabled by the browser.
  }
}
