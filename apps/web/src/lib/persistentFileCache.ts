import { AUTH_TOKEN_CHANGED_EVENT, decodeAuthTokenClaims, getAuthToken, isSameAuthIdentity } from "./authToken";
import { getReadyAuthToken } from "./persistentSession";
import { fetchFileBlob } from "./fileTransfer";
import { EMBED } from "../embed/embedContext";

type LocalFile = { blob: Blob; headers: Headers };
type Transfer = { url: string; controller: AbortController; promise: Promise<LocalFile>; consumers: number };
// Used only when the browser cannot persist a downloaded file (for example,
// when its storage quota is exhausted). Successfully persisted blobs stay on disk.
const memoryFallback = new Map<string, LocalFile>();
const inflight = new Map<string, Transfer>();
let cacheToken: string | null = null;
let observingIdentity = false;
// Order invalidations after outstanding writes, so a save cannot leave stale
// bytes behind. New reads wait for this queue before consulting disk.
let diskWrites: Promise<unknown> = Promise.resolve();
function writeDisk(operation: () => Promise<unknown>): Promise<unknown> {
  const pending = diskWrites.then(operation);
  diskWrites = pending.catch(() => {});
  return pending;
}

/** Invalidate this account's mutable document, including metadata/derivatives. */
export function invalidateCachedFiles(prefix: string): Promise<unknown> {
  syncIdentity();
  const token = getAuthToken();
  const path = new URL(prefix, "http://cache.local").pathname;
  const matches = (url: string) => {
    const candidate = new URL(url, "http://cache.local").pathname;
    return candidate === path || candidate.startsWith(`${path}/`);
  };
  for (const [key, transfer] of inflight) {
    if (matches(transfer.url)) {
      transfer.controller.abort(new DOMException("File changed", "AbortError"));
      inflight.delete(key);
    }
  }
  for (const key of memoryFallback.keys()) if (matches(key)) memoryFallback.delete(key);
  return writeDisk(async () => {
    const cache = await openFileCache(token);
    if (cache) for (const key of await cache.keys()) if (matches(key.url)) await cache.delete(key);
  }).catch(() => {});
}

function syncIdentity() {
  const token = getAuthToken();
  if (!isSameAuthIdentity(cacheToken, token)) {
    memoryFallback.clear();
    for (const transfer of inflight.values()) transfer.controller.abort();
    inflight.clear();
  }
  cacheToken = token;
}

async function openFileCache(token: string | null): Promise<Cache | undefined> {
  // CacheStorage is shared with the parent page: the frame keeps no files on disk.
  if (EMBED) return undefined;
  const claims = decodeAuthTokenClaims(token);
  if (!claims?.sub || !claims.entity_id) return undefined;
  try {
    // Persist ownership identifiers, never the access token. Token rotation and
    // signing back into the same account preserve that account's local files.
    const scope = JSON.stringify([claims.sub, claims.entity_id, claims.typ || "access"]);
    return await caches.open(`manor-room-files:v1:${encodeURIComponent(scope)}`);
  } catch {
    return undefined;
  }
}

/** Local copies have no TTL and open before any auth refresh or network read.
 * Mutable documents are invalidated by successful saves; force downloads again.
 * The legacy cache name preserves files already cached by the group chat client. */
export async function fetchCachedFile(
  url: string,
  options: { signal?: AbortSignal; force?: boolean },
  fetchFile: (url: string, init: RequestInit) => Promise<Response>,
): Promise<Response> {
  options.signal?.throwIfAborted();
  if (!observingIdentity && typeof window !== "undefined") {
    window.addEventListener(AUTH_TOKEN_CHANGED_EVENT, syncIdentity);
    window.addEventListener("storage", syncIdentity);
    observingIdentity = true;
  }
  syncIdentity();
  const before = getAuthToken();
  const transferKey = JSON.stringify([url, Boolean(options.force)]);
  let transfer = inflight.get(transferKey);
  if (!transfer || transfer.controller.signal.aborted) {
    const controller = new AbortController();
    const checkSession = () => {
      controller.signal.throwIfAborted();
      if (!isSameAuthIdentity(before, getAuthToken())) throw new DOMException("Session changed", "AbortError");
    };
    const current: Transfer = { url, controller, consumers: 0, promise: Promise.resolve().then(async () => {
      try {
        checkSession();
        if (!options.force && memoryFallback.has(url)) return memoryFallback.get(url)!;
        await diskWrites;
        const diskCache = await openFileCache(before);
        checkSession();
        if (!options.force && diskCache) {
          let file: LocalFile | undefined;
          try {
            const response = await diskCache.match(url);
            if (response) file = { blob: await response.blob(), headers: response.headers };
          } catch {
            // An unreadable local copy can still be downloaded normally.
          }
          checkSession();
          if (file) return file;
        }
        const token = await getReadyAuthToken();
        checkSession();
        if (!isSameAuthIdentity(before, token)) throw new DOMException("Session changed", "AbortError");
        const headers: Record<string, string> = token ? { Authorization: `Bearer ${token}` } : {};
        const file = await fetchFileBlob(url, { headers, cache: "no-store", signal: controller.signal }, fetchFile);
        checkSession();
        let persisted = false;
        if (diskCache) {
          try {
            // Store a local downloaded copy, independent of HTTP cache headers.
            await writeDisk(async () => {
              checkSession();
              await diskCache.put(url, new Response(file.blob, { headers: file.headers }));
            });
            persisted = true;
          } catch {
            // Storage being unavailable/full must not prevent opening the file.
          }
        }
        checkSession();
        if (persisted) memoryFallback.delete(url);
        else if (before) memoryFallback.set(url, file);
        return file;
      } finally {
        if (inflight.get(transferKey) === current) inflight.delete(transferKey);
      }
    }) };
    transfer = current;
    inflight.set(transferKey, transfer);
  }
  const shared = transfer;
  shared.consumers += 1;
  return new Promise<Response>((resolve, reject) => {
    let finished = false;
    const finish = () => {
      if (finished) return false;
      finished = true;
      options.signal?.removeEventListener("abort", abort);
      shared.consumers -= 1;
      if (!shared.consumers) shared.controller.abort();
      return true;
    };
    const abort = () => {
      if (finish()) reject(options.signal?.reason ?? new DOMException("Aborted", "AbortError"));
    };
    options.signal?.addEventListener("abort", abort, { once: true });
    shared.promise.then(
      (file) => {
        if (!finish()) return;
        if (!isSameAuthIdentity(before, getAuthToken())) reject(new DOMException("Session changed", "AbortError"));
        else resolve(new Response(file.blob, { headers: file.headers }));
      },
      (error) => { if (finish()) reject(error); },
    );
  });
}
