/**
 * The run attempt a person actually answers.
 *
 * A retried result is one run family. Its compact view, keyed by the family's
 * first run, shows the latest attempt's state and names that attempt in
 * `intervention.workflow_run_id`; the full view of the first run is still its
 * own (failed) attempt. A response is loaded and sent for the attempt that
 * waits, so an App row or page holding the family's first id can still answer
 * a retry's review.
 */
type RunLoader<Run> = (runId: string, detail?: boolean) => Promise<Run>;

type RunLike = {
  id?: string;
  status?: string;
  intervention?: { workflow_run_id?: unknown } | null;
};

export async function loadWaitingRun<Run extends RunLike>(runId: string, getRun: RunLoader<Run>): Promise<Run> {
  const current = await getRun(runId);
  if (current.status === "paused") return current;
  const family = await getRun(runId, false);
  const waiting = family.intervention?.workflow_run_id;
  if (typeof waiting === "string" && waiting.trim() && waiting.trim() !== runId) {
    return getRun(waiting.trim());
  }
  return current;
}
