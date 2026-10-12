export const PREVIEW_FETCH_CONCURRENCY = 2;

export interface PreviewQueueOptions {
  /**
   * Keep loading the other items when one fails. Failures are reported in the
   * result instead of aborting siblings; only the outer signal cancels.
   */
  isolateFailures?: boolean;
  /**
   * Choose the next index among those not started yet (called each time a
   * worker frees up, so priorities may change while the queue runs). Returning
   * an index that is not pending falls back to the lowest pending index.
   */
  pickNext?: (pending: ReadonlySet<number>) => number | undefined;
  concurrency?: number;
}

export interface PreviewQueueResult {
  /** Index → error for items that failed (only populated with isolateFailures). */
  failed: Map<number, unknown>;
}

function lowestPending(pending: ReadonlySet<number>): number {
  let lowest = Number.POSITIVE_INFINITY;
  pending.forEach((index) => { if (index < lowest) lowest = index; });
  return lowest;
}

/** Keep slide fetches bounded and settle in-flight work before fallback/cleanup. */
export async function runPreviewQueue(
  count: number,
  load: (index: number, signal: AbortSignal) => Promise<void>,
  signal: AbortSignal,
  options: PreviewQueueOptions = {},
): Promise<PreviewQueueResult> {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener("abort", abort, { once: true });
  if (signal.aborted) controller.abort();
  const pending = new Set<number>(Array.from({ length: Math.max(0, count) }, (_, index) => index));
  const failed = new Map<number, unknown>();
  const takeNext = (): number => {
    const picked = options.pickNext?.(pending);
    const index = picked !== undefined && pending.has(picked) ? picked : lowestPending(pending);
    pending.delete(index);
    return index;
  };
  const concurrency = Math.max(1, options.concurrency ?? PREVIEW_FETCH_CONCURRENCY);
  try {
    const results = await Promise.allSettled(Array.from(
      { length: Math.min(concurrency, count) },
      async () => {
        while (!controller.signal.aborted && pending.size > 0) {
          const index = takeNext();
          try {
            await load(index, controller.signal);
          } catch (error) {
            if (options.isolateFailures && !controller.signal.aborted) {
              failed.set(index, error);
              continue;
            }
            controller.abort();
            throw error;
          }
        }
      },
    ));
    const rejected = results.find((result) => result.status === "rejected");
    if (rejected?.status === "rejected") throw rejected.reason;
    if (controller.signal.aborted) throw new DOMException("Preview cancelled", "AbortError");
    return { failed };
  } finally {
    signal.removeEventListener("abort", abort);
  }
}
