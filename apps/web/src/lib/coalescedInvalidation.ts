import type { QueryClient, QueryKey } from "@tanstack/react-query";

type Pending = { timer: ReturnType<typeof setTimeout>; firstAt: number };

const pendingByClient = new WeakMap<QueryClient, Map<string, Pending>>();

/**
 * Invalidate `queryKey` once for a burst of triggers instead of once per
 * trigger. invalidateQueries refetches every active observer immediately, so a
 * stream of realtime events (agent greetings after an install, a chat turn's
 * messages) otherwise becomes one GET per event. Trailing debounce of
 * `waitMs`, but never deferred more than `maxWaitMs` after the first trigger.
 */
export function scheduleQueryInvalidation(
  queryClient: QueryClient,
  queryKey: QueryKey,
  { waitMs = 500, maxWaitMs = 2_000 }: { waitMs?: number; maxWaitMs?: number } = {},
): void {
  let pending = pendingByClient.get(queryClient);
  if (!pending) {
    pending = new Map();
    pendingByClient.set(queryClient, pending);
  }
  const id = JSON.stringify(queryKey);
  const now = Date.now();
  const current = pending.get(id);
  const firstAt = current?.firstAt ?? now;
  if (current) clearTimeout(current.timer);
  const delay = Math.max(0, Math.min(waitMs, firstAt + maxWaitMs - now));
  const timer = setTimeout(() => {
    pending?.delete(id);
    void queryClient.invalidateQueries({ queryKey }, { cancelRefetch: false });
  }, delay);
  pending.set(id, { timer, firstAt });
}
