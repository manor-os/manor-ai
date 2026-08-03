import type { SubAgentEvent } from "./chatStream";

type DelegationItem = {
  name?: string;
  args?: unknown;
  arguments?: unknown;
  arguments_preview?: unknown;
  result?: unknown;
  result_preview?: unknown;
};

function parseRecord(value: unknown): Record<string, any> | null {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    return value as Record<string, any>;
  }
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? parsed
      : null;
  } catch {
    return null;
  }
}

function delegationDescriptor(item: DelegationItem) {
  if (item.name !== "workspace_agent") return null;
  const args = parseRecord(
    item.arguments_preview ?? item.arguments ?? item.args,
  );
  if (args?.action !== "delegate_service") return null;
  const params =
    args.params && typeof args.params === "object" && !Array.isArray(args.params)
      ? args.params
      : {};
  const result = parseRecord(item.result_preview ?? item.result);
  return {
    runId: String(result?.run_id || "").trim(),
    serviceKey: String(params.service_key || "").trim(),
    agentSubscriptionId: String(params.agent_subscription_id || "").trim(),
    agentId: String(params.agent_id || "").trim(),
  };
}

export function matchSubAgentRuns<T extends DelegationItem>(
  items: T[],
  runs: SubAgentEvent[],
): Map<number, SubAgentEvent> {
  const matches = new Map<number, SubAgentEvent>();
  const usedRunIndexes = new Set<number>();

  items.forEach((item, itemIndex) => {
    const descriptor = delegationDescriptor(item);
    if (!descriptor) return;

    let runIndex = runs.findIndex(
      (run, index) =>
        !usedRunIndexes.has(index) &&
        Boolean(descriptor.runId) &&
        run.run_id === descriptor.runId,
    );
    if (runIndex < 0) {
      runIndex = runs.findIndex(
        (run, index) =>
          !usedRunIndexes.has(index) &&
          ((descriptor.agentSubscriptionId &&
            run.agent_subscription_id === descriptor.agentSubscriptionId) ||
            (descriptor.agentId && run.agent_id === descriptor.agentId) ||
            (descriptor.serviceKey && run.service_key === descriptor.serviceKey)),
      );
    }
    if (runIndex < 0) {
      runIndex = runs.findIndex((_run, index) => !usedRunIndexes.has(index));
    }
    if (runIndex < 0) return;

    usedRunIndexes.add(runIndex);
    matches.set(itemIndex, runs[runIndex]);
  });

  return matches;
}
