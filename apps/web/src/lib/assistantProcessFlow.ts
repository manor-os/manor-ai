import type { AssistantBlock, AssistantProcessStep } from "./chatStream";
import { isLiveAssistantProcess } from "./assistantProcessPresentation";
import { isWebsiteAccessDenied } from "./websiteAccessDenied";

export function isVerifiedProcessStep(steps: AssistantProcessStep[], index: number) {
  const step = steps[index];
  return step?.status === "unverified" && Boolean(step.verified_by_step_id) && steps.slice(index + 1).some(
    (later) => later.id === step.verified_by_step_id && ["success", "completed"].includes(later.status || ""),
  );
}

export function hasUnverifiedProcessStep(steps: AssistantProcessStep[]) {
  return steps.some((step, index) => step.status === "unverified" && !isVerifiedProcessStep(steps, index));
}

export function isUnrecoveredProcessStep(steps: AssistantProcessStep[], index: number) {
  const step = steps[index];
  if (!step || step.status !== "error" || isWebsiteAccessDenied(step)) return false;
  return !steps.slice(index + 1).some(
    (later) => later.id === step.recovered_by_step_id &&
      ["success", "completed", "not_run"].includes(later.status || ""),
  );
}

export function hasUnrecoveredProcessError(steps: AssistantProcessStep[]) {
  return steps.some((_, index) => isUnrecoveredProcessStep(steps, index));
}

export function shouldExpandAssistantProcessBlock(
  block: Extract<AssistantBlock, { type: "process" }>,
  { streaming = false, waitingForHuman = false, hasFinalOutput = false } = {},
) {
  return !hasFinalOutput && block.default_collapsed !== true
    && isLiveAssistantProcess(block, streaming, waitingForHuman);
}

export type ConsecutiveGroup<T> = {
  key: string | null;
  items: T[];
  startIndex: number;
};

export function groupConsecutiveByKey<T>(
  items: T[],
  keyOf: (item: T, index: number) => string | null,
): ConsecutiveGroup<T>[] {
  const groups: ConsecutiveGroup<T>[] = [];
  items.forEach((item, index) => {
    const key = keyOf(item, index);
    const last = groups[groups.length - 1];
    if (key != null && last && last.key === key) {
      last.items.push(item);
      return;
    }
    groups.push({ key, items: [item], startIndex: index });
  });
  return groups;
}
