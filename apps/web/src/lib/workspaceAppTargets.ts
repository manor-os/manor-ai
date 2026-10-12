import type { WorkspaceAppAction, WorkspaceAppView, WorkspaceAppViewConfig } from "./api";

export function workspaceAppTargetValue(action: WorkspaceAppAction, config: WorkspaceAppViewConfig): string {
  const target = config.targets[action.key];
  if (target && /^(agent|workflow|automation|attach):/.test(target)) return target;
  if (action.key.startsWith("service:") || action.key.startsWith("action:")) return target ? `agent:${target}` : "agent:default";
  return target ? `${action.key.split(":")[0]}:${target}` : action.key;
}

export function resolveWorkspaceAppTarget(
  action: WorkspaceAppAction, view: WorkspaceAppView, config: WorkspaceAppViewConfig,
): WorkspaceAppAction {
  const target = workspaceAppTargetValue(action, config);
  const resolved = { ...action };
  for (const field of ["agent_id", "agent_name", "binding_id", "workflow_id", "job_id", "prompt", "placeholder",
    "enabled", "last_status", "job_type", "schedule_kind", "every_seconds", "run_at", "cron_expr", "timezone",
    "instructions", "target_title", "target_workflow_id"] as const) delete resolved[field];
  if (target.startsWith("agent:")) {
    const id = target.slice(6);
    const agent = view.agent_options.find((item) => item.id === (id === "default" ? action.default_agent_id : id));
    return { ...resolved, kind: "chat", description: action.source_description ?? action.description,
      prompt: config.prompts?.[action.key] || action.source_title, inputs: config.forms[action.key] || [], available: id === "default" || Boolean(agent),
      agent_id: agent?.id, agent_name: agent?.name };
  }
  if (target.startsWith("attach:")) {
    const flow = view.attachable_workflow_options?.find((item) => item.workflow_id === target.slice(7));
    return { ...resolved, kind: "workflow", workflow_id: flow?.workflow_id, available: Boolean(flow),
      description: flow?.description || "", inputs: flow?.inputs || [] };
  }
  if (target.startsWith("workflow:")) {
    const flow = view.workflow_options.find((item) => item.binding_id === target.slice(9));
    return { ...resolved, kind: "workflow", binding_id: flow?.binding_id, workflow_id: flow?.workflow_id,
      available: Boolean(flow), description: flow?.description || "", placeholder: flow?.placeholder, inputs: flow?.inputs || [] };
  }
  const job = view.automation_options.find((item) => item.job_id === target.slice(11));
  return { ...resolved, ...job, kind: "automation", title: action.title, source_title: action.source_title,
    available: Boolean(job), description: job?.description || "", inputs: [] };
}
