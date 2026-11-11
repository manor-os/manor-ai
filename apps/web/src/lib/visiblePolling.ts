import { displayReadInterval } from "./readRefreshPolicy";

/** Serial display reads. Mutations and identity/lease renewal do not belong here. */
export function startVisiblePolling(
  read: (signal: AbortSignal) => Promise<void>,
  interval: number | (() => number),
  {
    immediate = true,
    timeoutMs = 15_000,
    // Live conversations recover from a transient failure faster than display reads.
    errorBackoffMs = 30_000,
    maxErrorBackoffMs = 300_000,
  } = {},
) {
  let stopped = false;
  let inFlight = false;
  let refreshQueued = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let controller: AbortController | undefined;
  let failures = 0;
  let retryAt = 0;
  let permanent = false;
  const cadence = () => typeof interval === "function" ? interval() : interval;
  const schedule = (delay: number) => {
    clearTimeout(timer);
    if (!stopped && !permanent && document.visibilityState !== "hidden") {
      timer = setTimeout(() => void refresh(), Math.min(delay, 2_147_483_647));
    }
  };
  const refresh = async () => {
    if (stopped || permanent || document.visibilityState === "hidden") return;
    if (inFlight) {
      refreshQueued = true;
      return;
    }
    if (Date.now() < retryAt) { schedule(retryAt - Date.now()); return; }
    clearTimeout(timer);
    inFlight = true;
    controller = new AbortController();
    const signal = AbortSignal.any([controller.signal, AbortSignal.timeout(timeoutMs)]);
    let next = cadence();
    try {
      await read(signal);
      failures = 0;
      next = cadence();
    } catch (error) {
      if (!controller.signal.aborted) {
        const delay = displayReadInterval({ status: "error", error }, cadence());
        permanent = delay === false;
        const retryAfter = (error as { retryAfterMs?: number } | null)?.retryAfterMs;
        const backoff = Math.min(maxErrorBackoffMs, errorBackoffMs * 2 ** Math.min(failures++, 4));
        next = Math.max(
          cadence(),
          backoff,
          typeof retryAfter === "number" && Number.isFinite(retryAfter) ? retryAfter : 0,
        );
        retryAt = Date.now() + next;
      }
    } finally {
      inFlight = false;
      const delay = refreshQueued ? Math.max(0, retryAt - Date.now()) : next;
      refreshQueued = false;
      schedule(delay);
    }
  };
  const onVisibility = () => {
    clearTimeout(timer);
    // Let an already submitted check settle; its caller fences any stale reply.
    if (document.visibilityState !== "hidden") void refresh();
  };
  document.addEventListener("visibilitychange", onVisibility);
  if (immediate) void refresh();
  else schedule(cadence());
  return {
    refresh,
    stop() {
      stopped = true;
      clearTimeout(timer);
      controller?.abort();
      document.removeEventListener("visibilitychange", onVisibility);
    },
  };
}
