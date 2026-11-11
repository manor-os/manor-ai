/**
 * A small queue for the per-run media reads used by production history.
 * It deliberately does not govern unrelated API reads.
 */
const MAX_CONCURRENT_MEDIA_RUN_READS = 2;

type PendingRead<T> = {
  priority: number;
  signal: AbortSignal;
  read: () => Promise<T>;
  resolve: (value: T) => void;
  reject: (error: unknown) => void;
  cleanup: () => void;
};

const pending: PendingRead<unknown>[] = [];
let active = 0;

function abortError(): DOMException {
  return new DOMException("Media run read cancelled", "AbortError");
}

function drain(): void {
  while (active < MAX_CONCURRENT_MEDIA_RUN_READS && pending.length) {
    const next = pending.shift()!;
    if (next.signal.aborted) {
      next.cleanup();
      next.reject(next.signal.reason || abortError());
      continue;
    }
    active += 1;
    void next.read().then(next.resolve, next.reject).finally(() => {
      next.cleanup();
      active -= 1;
      drain();
    });
  }
}

/** Queue one family-member read, giving the selected/active member precedence. */
export function scheduleMediaRunRead<T>(
  signal: AbortSignal,
  read: () => Promise<T>,
  priority = 1,
): Promise<T> {
  if (signal.aborted) return Promise.reject(signal.reason || abortError());
  return new Promise<T>((resolve, reject) => {
    const entry: PendingRead<T> = { priority, signal, read, resolve, reject, cleanup: () => {} };
    const abort = () => {
      const index = pending.indexOf(entry as PendingRead<unknown>);
      if (index >= 0) pending.splice(index, 1);
      reject(signal.reason || abortError());
    };
    entry.cleanup = () => signal.removeEventListener("abort", abort);
    signal.addEventListener("abort", abort, { once: true });
    pending.push(entry as PendingRead<unknown>);
    pending.sort((left, right) => left.priority - right.priority);
    drain();
  });
}

export const mediaRunReadConcurrency = MAX_CONCURRENT_MEDIA_RUN_READS;
