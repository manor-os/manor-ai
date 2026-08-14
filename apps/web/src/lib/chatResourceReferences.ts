import type { ToolCall } from "./chatStream";

export type ChatCreatedResourceKind = "workflow" | "automation";

export interface ChatCreatedResourceReference {
  kind: ChatCreatedResourceKind;
  id: string;
  name: string;
  href: string;
  subtitle?: string;
}

type UnknownRecord = Record<string, unknown>;

function asRecord(value: unknown): UnknownRecord | null {
  return value != null && typeof value === "object" && !Array.isArray(value)
    ? (value as UnknownRecord)
    : null;
}

function parseRecord(value: unknown): UnknownRecord | null {
  const direct = asRecord(value);
  if (direct) return direct;
  if (typeof value !== "string" || !value.trim().startsWith("{")) return null;
  try {
    return asRecord(JSON.parse(value));
  } catch {
    return null;
  }
}

function toolArguments(tool: ToolCall): UnknownRecord {
  const direct = asRecord(tool.args);
  if (direct) return direct;
  return parseRecord(tool.arguments) || {};
}

function toolAction(tool: ToolCall, args: UnknownRecord): string {
  const rawName = String(tool.name || "").trim().toLowerCase();
  const nestedAction = String(args.action || args.name || "").trim().toLowerCase();
  if (nestedAction && ["manor", "tool", "action"].includes(rawName)) {
    return nestedAction;
  }
  return rawName.split(/[.:/]/).filter(Boolean).pop() || rawName;
}

function actionParams(args: UnknownRecord): UnknownRecord {
  return asRecord(args.params) || asRecord(args.arguments) || args;
}

function firstText(...values: unknown[]): string {
  for (const value of values) {
    const text = typeof value === "string" || typeof value === "number"
      ? String(value).trim()
      : "";
    if (text) return text;
  }
  return "";
}

function isSuccessfulResult(tool: ToolCall, result: UnknownRecord | null): boolean {
  if (tool.status === "error") return false;
  if (result?.ok === false) return false;
  const status = String(result?.status || "").toLowerCase();
  return !["error", "failed", "failure"].includes(status);
}

function workflowReference(
  result: UnknownRecord,
  params: UnknownRecord,
): ChatCreatedResourceReference | null {
  const workflow = asRecord(result.workflow) || asRecord(result.data) || result;
  const id = firstText(workflow.id, workflow.workflow_id, result.workflow_id);
  if (!id) return null;
  const name = firstText(workflow.name, workflow.workflow_name, params.name, id);
  const description = firstText(workflow.description, result.summary);
  return {
    kind: "workflow",
    id,
    name,
    href: `/flows?workflow=${encodeURIComponent(id)}`,
    subtitle: description || undefined,
  };
}

function scheduleSubtitle(source: UnknownRecord, params: UnknownRecord): string | undefined {
  const kind = firstText(source.schedule_kind, params.schedule_kind, source.job_type);
  const value = firstText(
    source.cron_expr,
    params.cron_expr,
    source.every_seconds,
    params.every_seconds,
    source.run_at,
    params.run_at,
  );
  if (kind && value) return `${kind} · ${value}`;
  return kind || value || undefined;
}

function automationReference(
  rawResult: unknown,
  result: UnknownRecord | null,
  params: UnknownRecord,
): ChatCreatedResourceReference | null {
  const automation = asRecord(result?.automation) || asRecord(result?.job) || result;
  let id = firstText(
    automation?.job_id,
    automation?.id,
    result?.job_id,
    result?.scheduled_job_id,
  );
  let name = firstText(automation?.name, result?.name, params.name);
  let subtitle = automation ? scheduleSubtitle(automation, params) : scheduleSubtitle({}, params);

  if ((!id || !name) && typeof rawResult === "string") {
    const legacy = rawResult.match(
      /Created scheduled job ['"](.+?)['"]\s*\(id=([^,\s)]+),\s*([^:)]+):\s*([^)]+)\)/i,
    );
    if (legacy) {
      name ||= legacy[1].trim();
      id ||= legacy[2].trim();
      subtitle ||= `${legacy[3].trim()} · ${legacy[4].trim()}`;
    }
  }

  if (!id) return null;
  return {
    kind: "automation",
    id,
    name: name || id,
    href: `/jobs?job=${encodeURIComponent(id)}`,
    subtitle,
  };
}

/**
 * Project successful authoring tool calls into durable, navigable chat cards.
 * Supports both the structured tool envelope and legacy scheduled-job text so
 * existing conversation history gains the same navigation affordance.
 */
export function createdChatResourceReferences(
  tools: ToolCall[] | undefined,
): ChatCreatedResourceReference[] {
  const references: ChatCreatedResourceReference[] = [];
  const seen = new Set<string>();

  for (const tool of tools || []) {
    if (!tool.result) continue;
    const args = toolArguments(tool);
    const params = actionParams(args);
    const action = toolAction(tool, args);
    const result = parseRecord(tool.result);
    if (!isSuccessfulResult(tool, result)) continue;

    let reference: ChatCreatedResourceReference | null = null;
    if (action === "create_workflow") {
      reference = result ? workflowReference(result, params) : null;
    } else if (action === "create_scheduled_job") {
      reference = automationReference(tool.result, result, params);
    } else if (action === "deploy_workflow" && result?.kind === "automation") {
      reference = automationReference(tool.result, result, params);
    }

    if (!reference) continue;
    const key = `${reference.kind}:${reference.id}`;
    if (seen.has(key)) continue;
    seen.add(key);
    references.push(reference);
  }

  return references;
}
