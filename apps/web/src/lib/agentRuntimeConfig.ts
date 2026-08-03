export type AgentRuntimeProfile = "hosted" | "https";

export const RUNTIME_PROFILE_OPTIONS: Array<{
  key: AgentRuntimeProfile;
  title: string;
  body: string;
  badge: string;
}> = [
  {
    key: "hosted",
    title: "Manor Hosted",
    body: "Runs on Manor's hosted agent service. No extra connection required.",
    badge: "Default",
  },
  {
    key: "https",
    title: "HTTPS endpoint",
    body: "Runs through a workspace-bound HTTPS agent endpoint.",
    badge: "Remote",
  },
];

export function objectConfig(value: unknown): Record<string, any> {
  if (!value || typeof value !== "object" || Array.isArray(value)) return {};
  return value as Record<string, any>;
}

export function runtimeLearningEnabled(config: unknown): boolean {
  const runtimeLearning = objectConfig(objectConfig(config).runtime_learning);
  return runtimeLearning.enabled !== false;
}

export function mergeRuntimeLearningConfig(config: unknown, enabled: boolean): Record<string, any> {
  const next = { ...objectConfig(config) };
  const runtimeLearning = { ...objectConfig(next.runtime_learning), enabled };
  next.runtime_learning = runtimeLearning;
  return next;
}

export function runtimeProfileFromConfig(config: unknown): AgentRuntimeProfile {
  const connection = objectConfig(objectConfig(config).runtime_connection);
  const source = String(connection.source || "").toLowerCase();
  // Legacy sources ("cli", with optional tool claude_code/codex_cli) collapse
  // to hosted: execution-wise they always were hosted — local capabilities come
  // from the local CLI integration, not from the agent's run method.
  if (source === "https") return "https";
  return "hosted";
}

export function runtimeConnectionForProfile(
  profile: AgentRuntimeProfile,
  config: unknown,
): Record<string, unknown> {
  const existing = objectConfig(objectConfig(config).runtime_connection);
  if (profile === "https") {
    return { ...existing, source: "https" };
  }
  return { source: "manor_hosted" };
}

export function mergeAgentConfig(
  config: unknown,
  runtimeLearningEnabled: boolean,
  runtimeProfile: AgentRuntimeProfile,
): Record<string, any> {
  const next = mergeRuntimeLearningConfig(config, runtimeLearningEnabled);
  next.runtime_connection = runtimeConnectionForProfile(runtimeProfile, config);
  return next;
}

// ── Model override (config.model_mode / config.model) ──────────────────────
// Shared by AgentDetail's Config card and the Edit Agent modal so the two
// surfaces can't drift on what "inherit" vs "fixed" means.

export enum AgentModelMode {
  Inherit = "inherit",
  Fixed = "fixed",
}

// Sentinel option value for "use the platform default model".
export const AGENT_MODEL_INHERIT_VALUE = "__agent_model_inherit__";

export function agentModelMode(config: unknown): AgentModelMode {
  const values = objectConfig(config);
  if (values.model_mode === AgentModelMode.Fixed) return AgentModelMode.Fixed;
  if (values.model_mode === AgentModelMode.Inherit) return AgentModelMode.Inherit;
  return String(values.model || "").trim()
    ? AgentModelMode.Fixed
    : AgentModelMode.Inherit;
}

export function fixedAgentModel(config: unknown): string {
  if (agentModelMode(config) !== AgentModelMode.Fixed) return "";
  return String(objectConfig(config).model || "").trim();
}

export function agentModelSelectOptions(
  catalog: Record<string, any[]> | undefined,
  currentFixedModel: string,
  inheritLabel: string,
): Array<{ value: string; label: string }> {
  const seen = new Set<string>();
  const options: Array<{ value: string; label: string }> = [];
  for (const role of ["primary", "worker"]) {
    for (const model of (catalog || {})[role] || []) {
      const value = String(model?.id || "").trim();
      if (!value || seen.has(value)) continue;
      seen.add(value);
      options.push({ value, label: model?.name || value });
    }
  }
  if (currentFixedModel && !seen.has(currentFixedModel)) {
    options.push({ value: currentFixedModel, label: `${currentFixedModel} (custom)` });
  }
  return [{ value: AGENT_MODEL_INHERIT_VALUE, label: inheritLabel }, ...options];
}
