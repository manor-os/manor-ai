import type { AssistantBlock } from "./chatStream";

function normalizeProcessStepStatus(status?: string) {
  if (status === "running" || status === "pending") return "running";
  if (status === "error") return "error";
  return "completed";
}

export function shouldExpandAssistantProcessBlock(
  block: Extract<AssistantBlock, { type: "process" }>,
  minimal = false,
) {
  const steps = block.steps || [];
  const hasRunning = steps.some((step) => normalizeProcessStepStatus(step.status) === "running");
  const hasError = steps.some((step) => normalizeProcessStepStatus(step.status) === "error");
  if (hasRunning) return true;
  if (block.default_collapsed === true) return false;
  if (minimal) return false;
  return block.default_collapsed === false || hasError;
}
