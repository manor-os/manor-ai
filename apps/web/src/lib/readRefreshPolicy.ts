/** A failed background read must never run faster than its healthy cadence. */
export function displayReadInterval(
  state: { status?: string; error?: unknown },
  normalInterval: number | false,
): number | false {
  if (state.status !== "error") return normalInterval;
  const error = state.error as { status?: number; retryAfterMs?: number } | null;
  const status = error?.status;
  if (typeof status === "number" && status >= 400 && status < 500 && status !== 408 && status !== 429) {
    return false;
  }
  const retryAfter = error?.retryAfterMs;
  return Math.max(
    normalInterval || 0,
    30_000,
    typeof retryAfter === "number" && Number.isFinite(retryAfter) ? retryAfter : 0,
  );
}

/** Keep full pagination consistent while bounding average background request volume. */
export function pagedReadInterval(pageCount: number | undefined, baseMs = 60_000): number {
  return Math.min(300_000, Math.max(1, pageCount || 1) * baseMs);
}

export function setupReadInterval(data?: { ready?: boolean; automatic_setup_state?: string | null }): number | false {
  if (data?.ready) return false;
  return ["pending", "running", "retrying"].includes(data?.automatic_setup_state || "")
    ? 5_000 : 60_000;
}
