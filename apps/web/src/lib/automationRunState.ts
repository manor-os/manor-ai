/**
 * Effective run state of one automation row (a ScheduledJob or a Workflow
 * binding) on the Automations surface.
 *
 * A paused Workspace stops only its Strategist/autonomy schedules; Workflow
 * automations and standalone Stat collectors keep running on their own
 * switch. The server decides which rows the Workspace lifecycle blocks; this
 * helper only turns that decision into the label, switch and filter state so
 * the switch always sends the opposite of what it shows.
 */
export type AutomationRunState = "enabled" | "paused" | "workspace_blocked";

export interface AutomationRunView {
  state: AutomationRunState;
  /** i18n key of the state chip; null while the row is running. */
  labelKey: string | null;
  /** What the switch shows: the row's own enabled flag. */
  checked: boolean;
  /** What clicking the switch sends. */
  nextEnabled: boolean;
  /** Enabling is refused while the Workspace lifecycle blocks the row. */
  canToggle: boolean;
}

export function automationRunView({
  enabled,
  workspaceBlocked = false,
  workspaceStatus,
}: {
  enabled: boolean;
  workspaceBlocked?: boolean;
  workspaceStatus?: string | null;
}): AutomationRunView {
  const state: AutomationRunState = workspaceBlocked
    ? "workspace_blocked"
    : enabled ? "enabled" : "paused";
  let labelKey: string | null = null;
  if (state === "paused") labelKey = "page.scheduled_jobs.paused";
  else if (state === "workspace_blocked") {
    labelKey = workspaceStatus === "paused"
      ? "page.scheduled_jobs.workspace_paused"
      : "page.scheduled_jobs.workspace_inactive";
  }
  return {
    state,
    labelKey,
    checked: enabled,
    nextEnabled: !enabled,
    canToggle: enabled || !workspaceBlocked,
  };
}

/** Workflow bindings run in an active or paused Workspace, nowhere else. */
export function workspaceBlocksConfiguredWork(workspaceStatus?: string | null): boolean {
  return Boolean(workspaceStatus) && workspaceStatus !== "active" && workspaceStatus !== "paused";
}

export function matchesAutomationStatusFilter(
  view: AutomationRunView,
  filter: "all" | "enabled" | "paused" | "attention",
): boolean {
  if (filter === "enabled") return view.state === "enabled";
  if (filter === "paused") return view.state !== "enabled";
  return true;
}
