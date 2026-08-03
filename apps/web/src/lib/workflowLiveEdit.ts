export const WORKFLOW_LIVE_EDIT_FORMAT = "manor-workflow-v1";

export type WorkflowLiveEditSource = {
  name?: unknown;
  description?: unknown;
  trigger?: unknown;
  trigger_type?: unknown;
  trigger_config?: unknown;
  variables?: unknown;
  category?: unknown;
  tags?: unknown;
  steps?: unknown;
};

export type WorkflowLiveEditUpdate = {
  name: string;
  description: string;
  trigger_type: string;
  trigger_config: Record<string, unknown>;
  variables: Record<string, unknown>;
  category: string | null;
  tags: string[];
  steps: Record<string, unknown>[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function recordOrEmpty(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

export function serializeWorkflowLiveEdit(source: WorkflowLiveEditSource): string {
  const document = {
    format: WORKFLOW_LIVE_EDIT_FORMAT,
    name: typeof source.name === "string" && source.name.trim() ? source.name : "Untitled workflow",
    description: typeof source.description === "string" ? source.description : "",
    trigger_type:
      typeof source.trigger_type === "string" && source.trigger_type.trim()
        ? source.trigger_type
        : typeof source.trigger === "string" && source.trigger.trim()
          ? source.trigger
          : "manual",
    trigger_config: recordOrEmpty(source.trigger_config),
    variables: recordOrEmpty(source.variables),
    category: typeof source.category === "string" ? source.category : null,
    tags: Array.isArray(source.tags)
      ? source.tags.filter((tag): tag is string => typeof tag === "string")
      : [],
    steps: Array.isArray(source.steps) ? source.steps : [],
  };
  return `${JSON.stringify(document, null, 2)}\n`;
}

export function parseWorkflowLiveEdit(content: string): WorkflowLiveEditUpdate {
  let parsed: unknown;
  try {
    parsed = JSON.parse(content);
  } catch (error) {
    throw new Error(`AI edit returned invalid workflow JSON: ${(error as Error).message}`);
  }
  if (!isRecord(parsed) || parsed.format !== WORKFLOW_LIVE_EDIT_FORMAT) {
    throw new Error(`AI edit must preserve format \"${WORKFLOW_LIVE_EDIT_FORMAT}\".`);
  }

  const name = typeof parsed.name === "string" ? parsed.name.trim() : "";
  if (!name) throw new Error("AI edit returned a workflow without a name.");
  if (!Array.isArray(parsed.steps)) {
    throw new Error("AI edit returned a workflow without a steps array.");
  }

  const ids = new Set<string>();
  const steps = parsed.steps.map((step, index) => {
    if (!isRecord(step)) throw new Error(`Workflow node ${index + 1} is not an object.`);
    const id = typeof step.id === "string" ? step.id.trim() : "";
    const type = typeof step.type === "string" ? step.type.trim() : "";
    const stepName = typeof step.name === "string" ? step.name.trim() : "";
    if (!id || !type || !stepName) {
      throw new Error(`Workflow node ${index + 1} requires id, type, and name.`);
    }
    if (ids.has(id)) throw new Error(`Workflow node id \"${id}\" is duplicated.`);
    ids.add(id);
    return step;
  });

  const triggerType = typeof parsed.trigger_type === "string" ? parsed.trigger_type.trim() : "";
  if (!triggerType) throw new Error("AI edit returned a workflow without trigger_type.");
  if (!isRecord(parsed.trigger_config)) {
    throw new Error("AI edit returned invalid trigger_config data.");
  }
  if (!isRecord(parsed.variables)) {
    throw new Error("AI edit returned invalid workflow variables.");
  }
  if (parsed.category !== null && typeof parsed.category !== "string") {
    throw new Error("AI edit returned an invalid workflow category.");
  }
  if (!Array.isArray(parsed.tags) || parsed.tags.some((tag) => typeof tag !== "string")) {
    throw new Error("AI edit returned invalid workflow tags.");
  }

  return {
    name,
    description: typeof parsed.description === "string" ? parsed.description : "",
    trigger_type: triggerType,
    trigger_config: parsed.trigger_config,
    variables: parsed.variables,
    category: parsed.category,
    tags: parsed.tags,
    steps,
  };
}
