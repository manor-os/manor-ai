import type { ClientTrial } from "../../lib/api";

const status = (error: unknown) => (error as { status?: unknown } | null)?.status;

/** A refused mint: the trial, or this person's right to open it, ended. */
export function mintEnded(error: unknown): boolean {
  return [401, 403, 404, 409, 410].includes(status(error) as number);
}

/** A failed trials list: refused (not a manager here) or a load failure worth retrying. */
export function trialListError(error: unknown): "no_access" | "load_failed" {
  return [401, 403, 404].includes(status(error) as number) ? "no_access" : "load_failed";
}

export type FrameState = "connecting" | "ready" | "ended" | "unavailable";

/** What replaces the frame: the ended (or could-not-open) state, with Close. */
export function frameNotice(state: FrameState): "ended" | "unavailable" | null {
  return state === "ended" || state === "unavailable" ? state : null;
}

/**
 * The trial the frame shows. Once opened it stays mounted from its snapshot:
 * when access ends the list refetch drops the delivery (or is refused), and
 * the frame must keep showing its ended state until the person closes it.
 */
export function openTrialFor(trialId: string | null, listed: ClientTrial[] | undefined, opened: ClientTrial | null): ClientTrial | null {
  if (!trialId) return null;
  if (opened?.delivery_id === trialId) return opened;
  return listed?.find((item) => item.delivery_id === trialId) ?? null;
}
