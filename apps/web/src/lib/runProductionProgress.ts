/**
 * How far a Workflow run's production has got, stage by stage (see RunProduction).
 *
 * A run's step list names every step the Workflow has, not only the ones it has
 * reached: a step it has not reached is "pending". So a step being listed says
 * nothing about progress; only a step that has started or finished does.
 */

export type StepState = { id: string; status: string };
export type StageState = "done" | "active" | "failed" | "stopped" | "pending";
export type ProductionStage = { label: string; steps: string[]; counts_units: boolean; weight: number };

/** The share an active stage with no units of its own counts once one of its steps has finished. */
const ACTIVE_SHARE = 0.4;

function stageStatuses(stage: ProductionStage, steps: StepState[]): string[] {
  const byId = new Map(steps.map((step) => [step.id, step.status]));
  return stage.steps.map((id) => byId.get(id)).filter((value): value is string => Boolean(value));
}

export function stageStates(stages: ProductionStage[], steps: StepState[], currentStepId: string | null, status: string): StageState[] {
  // Reached: the run is at one of the stage's steps, or one of them has started or finished.
  const reached = stages.map((stage) => stage.steps.includes(currentStepId || "")
    || stageStatuses(stage, steps).some((value) => value !== "pending"));
  const finished = status === "completed";
  const states: StageState[] = stages.map((stage, index) => {
    const statuses = stageStatuses(stage, steps);
    if (statuses.includes("failed")) return "failed";
    if (finished) return "done";
    // "pending" is a step not started yet: only one at work makes its stage active.
    if (stage.steps.includes(currentStepId || "") || statuses.some((value) => value === "running" || value === "paused")) return "active";
    // A stage the run went past (or around) is behind it.
    if (reached.slice(index + 1).some(Boolean)) return "done";
    return statuses.length > 0 && statuses.every((value) => value === "completed" || value === "skipped") ? "done" : "pending";
  });
  // A run that failed or was cancelled at a step no stage lists (a stop after
  // a check) ended in the stage it stopped at, or else the last one it reached.
  if ((status === "failed" && !states.includes("failed")) || status === "cancelled") {
    const at = stages.findIndex((stage) => stage.steps.includes(currentStepId || ""));
    const index = at >= 0 ? at : reached.lastIndexOf(true);
    if (index >= 0) states[index] = status === "failed" ? "failed" : "stopped";
    // Nothing after where it stopped was made.
    if (status === "cancelled") states.forEach((state, later) => { if (later > index && state !== "pending") states[later] = "pending"; });
  }
  return states;
}

/**
 * How much of one stage is made, from 0 to 1. `unitsShare` is the share of its
 * units made, for a stage that makes units once units are planned; null otherwise.
 */
export function stageShare(stage: ProductionStage, state: StageState, steps: StepState[], unitsShare: number | null): number {
  if (state === "done") return 1;
  if (state !== "active") return 0;
  if (unitsShare !== null) return unitsShare;
  // An active stage with no units counts a share once one of its steps has
  // finished: a run at its first step has made nothing yet.
  return stageStatuses(stage, steps).some((value) => value === "completed" || value === "skipped") ? ACTIVE_SHARE : 0;
}

/** The run's percent: each stage's share, weighted by the stage's weight. */
export function runPercent(stages: ProductionStage[], shares: number[]): number {
  const totalWeight = stages.reduce((sum, stage) => sum + stage.weight, 0) || 1;
  return Math.round(stages.reduce((sum, stage, index) => sum + stage.weight * shares[index], 0) / totalWeight * 100);
}
