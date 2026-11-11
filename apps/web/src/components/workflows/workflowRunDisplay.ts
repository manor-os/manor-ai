import type { WorkflowInputSchema } from "./WorkflowSchemaFields";

export type WorkflowRunStatus =
  | "pending"
  | "running"
  | "completed"
  | "paused"
  | "failed"
  | "skipped"
  | "cancelled";

export type WorkflowRunStatusLabelKey = WorkflowRunStatus
  | "needs_input"
  | "revision_required"
  | "ready_for_acceptance";

export type WorkflowRunStatusMotion = "running" | "waiting" | "static";

/**
 * The one status a person reads for a whole run, on every surface. Only
 * "needs_you" asks anything of them; what exactly is the run's own detail.
 */
export type WorkflowRunState = "queued" | "running" | "needs_you" | "completed" | "failed" | "cancelled";

export interface WorkflowRunStatusPresentation {
  /** The specific reason (needs_input, revision_required…), for a detail line. */
  labelKey: WorkflowRunStatusLabelKey;
  state: WorkflowRunState;
  /** i18n key of `state`. */
  stateKey: string;
  iconStatus: WorkflowRunStatus;
  motion: WorkflowRunStatusMotion;
}

export interface WorkflowRunNode {
  id: string;
  name: string;
  status: WorkflowRunStatus;
  type?: string;
  order?: number;
  subscriptionId?: string | null;
  error?: unknown;
  /** A check's decision: it finishes either way, so its answer is what to show. */
  answer?: "yes" | "no";
  /** A step that finished running but reported it could not do its job, or that it needs the person. */
  outcome?: WorkflowStepOutcome;
  /** What the step's long work (a sandbox command, a render) reported while running. */
  activity?: WorkflowStepActivity;
}

export interface WorkflowActivityEntry {
  kind: "progress" | "warning" | "error" | "waiting";
  code?: string;
  message?: string;
  percent?: number;
  done?: number;
  total?: number;
  count?: number;
  position?: number;
}

export interface WorkflowStepActivity {
  current: WorkflowActivityEntry;
  /** Warnings and errors among the recent entries, oldest first. */
  notices: WorkflowActivityEntry[];
}

const ACTIVITY_KINDS = new Set(["progress", "warning", "error", "waiting"]);
const ACTIVITY_CODES = new Set([
  "queued", "running", "preparing_machine", "uploading_files", "checking", "preparing",
  "rendering_frames", "encoding", "retrying", "verifying_output", "saving_outputs", "finished",
]);

function activityEntry(value: unknown): WorkflowActivityEntry | null {
  if (!value || typeof value !== "object") return null;
  const raw = value as Record<string, unknown>;
  const kind = typeof raw.kind === "string" && ACTIVITY_KINDS.has(raw.kind) ? raw.kind as WorkflowActivityEntry["kind"] : "progress";
  const entry: WorkflowActivityEntry = { kind };
  if (typeof raw.code === "string" && ACTIVITY_CODES.has(raw.code)) entry.code = raw.code;
  if (typeof raw.message === "string" && raw.message.trim()) entry.message = raw.message.trim();
  for (const key of ["percent", "done", "total", "count", "position"] as const) {
    const number = raw[key];
    if (typeof number === "number" && Number.isFinite(number) && number >= 0) entry[key] = Math.floor(number);
  }
  return entry.code || entry.message ? entry : null;
}

/** Read a step row's activity from a projected workflow step. */
export function workflowStepActivity(step: Record<string, unknown> | null | undefined): Pick<WorkflowRunNode, "activity"> {
  const raw = step?.activity;
  if (!raw || typeof raw !== "object") return {};
  const record = raw as Record<string, unknown>;
  const current = activityEntry(record.current);
  // Work that ended has nothing more to show; the step's own status takes over.
  if (!current || current.code === "finished") return {};
  const events = Array.isArray(record.events) ? record.events.map(activityEntry) : [];
  // A wait that ended (a place in line, a request answered) is no longer news.
  const notices = events.filter((entry): entry is WorkflowActivityEntry => entry?.kind === "warning" || entry?.kind === "error").slice(-3);
  return { activity: { current, notices } };
}

/**
 * An activity entry as words: progress reads as its shared phase in the viewer's
 * language; a warning or error says what went wrong, as the work put it.
 */
export function workflowActivityText(
  entry: WorkflowActivityEntry,
  translate: (key: string, params?: Record<string, string | number>) => string,
): string {
  if ((entry.kind === "warning" || entry.kind === "error") && entry.message) return entry.message;
  if (entry.code) {
    return translate(`component.workflow_run.activity.${entry.code}`, {
      position: entry.position ?? "", count: entry.count ?? "",
    });
  }
  return entry.message || "";
}

/** The counts beside an activity's words, e.g. "120/300 · 45%". */
export function workflowActivityCounts(entry: WorkflowActivityEntry): string {
  const parts: string[] = [];
  if (entry.total) parts.push(`${Math.min(entry.done ?? 0, entry.total)}/${entry.total}`);
  if (typeof entry.percent === "number") parts.push(`${Math.min(100, entry.percent)}%`);
  return parts.join(" · ");
}

/** What a step that finished running said of its work, when not plain success. */
export type WorkflowStepOutcome = "blocked" | "needs_input" | "revision_required";

const STEP_OUTCOME_KEYS: Record<WorkflowStepOutcome, string> = {
  blocked: "component.workflow_run.step_blocked",
  // The same words the run uses for the same outcome.
  needs_input: "component.workflow_run.status.needs_input",
  revision_required: "component.workflow_run.status.revision_required",
};

/** A step outcome that waits on the person rather than reporting a failure. */
export function workflowStepNeedsPerson(node: Pick<WorkflowRunNode, "status" | "outcome">): boolean {
  return node.status === "completed" && (node.outcome === "needs_input" || node.outcome === "revision_required");
}

/** How a finished step reads: a check's Yes/No, Blocked, Input needed, or its run status. */
export function workflowNodeOutcomeKey(node: Pick<WorkflowRunNode, "status" | "answer" | "outcome">): string | null {
  if (node.status !== "completed") return null;
  if (node.answer) return node.answer === "no" ? "component.workspace_app_view.check_no" : "component.workspace_app_view.check_yes";
  return node.outcome ? STEP_OUTCOME_KEYS[node.outcome] : null;
}

/**
 * What a step's row says: its outcome when it reported one; once the run has
 * ended, a step it never got to is "Not reached", not still waiting.
 */
export function workflowStepStatusKey(
  node: Pick<WorkflowRunNode, "status" | "answer" | "outcome">,
  runActive: boolean,
): string {
  const outcome = workflowNodeOutcomeKey(node);
  if (outcome) return outcome;
  if (node.status === "pending" && !runActive) return "component.workflow_run.step_not_reached";
  return `component.workflow_run.status.${node.status}`;
}

/** Read a step row's answer / outcome from a projected workflow step. */
export function workflowStepOutcome(step: Record<string, unknown> | null | undefined): Pick<WorkflowRunNode, "answer" | "outcome"> {
  const answer = step?.answer === "yes" || step?.answer === "no" ? step.answer : undefined;
  const outcome = typeof step?.outcome === "string" && Object.hasOwn(STEP_OUTCOME_KEYS, step.outcome)
    ? step.outcome as WorkflowStepOutcome
    : undefined;
  return { ...(answer ? { answer } : {}), ...(outcome ? { outcome } : {}) };
}

export interface WorkflowRunActionInput {
  key: string;
  label?: string;
  description?: string;
  placeholder?: string;
  type?: "string" | "number" | "boolean" | "json";
  required?: boolean;
  hidden?: boolean;
  default?: unknown;
  schema?: WorkflowInputSchema;
  suggestions?: string[];
  /** The form page this input is on; inputs sharing a step are asked together. */
  step?: string;
  step_label?: string;
}

export interface WorkflowRunAction {
  kind: string;
  id?: string;
  action_id?: string;
  workflow_run_id?: string;
  options?: string[];
  title?: string;
  description?: string;
  inputs?: WorkflowRunActionInput[];
  input_schema?: WorkflowInputSchema;
  editable_input_schema?: WorkflowInputSchema;
  result_schema?: WorkflowInputSchema;
  project_revisions?: Record<string, number>;
  values?: Record<string, unknown>;
  step_id?: string;
  retry_from_step_id?: string;
  retry_from_step_name?: string;
  /** The integration the failed step was waiting for; connect it, then continue. */
  connection_provider?: string;
  connection_label?: string;
  observed_problem?: unknown;
  required_change?: unknown;
  preserved_receipts?: unknown[];
  retry_segment_ids?: string[];
  review_editable_fields?: string[];
  review_revision_field?: string;
  [key: string]: unknown;
}

export interface WorkflowRunView {
  id: string;
  title: string;
  status: WorkflowRunStatus;
  nodes: WorkflowRunNode[];
  // The server's stage counts against the whole Flow, when it sent them.
  processedCount?: number | null;
  totalCount?: number | null;
  workflowId?: string;
  currentNodeId?: string | null;
  retryFromStepId?: string | null;
  attemptNumber?: number;
  startedAt?: string | null;
  completedAt?: string | null;
  elapsedMs?: number | null;
  businessOutcome?: string | null;
  error?: unknown;
  action?: WorkflowRunAction | null;
}

export interface WorkflowHistoryNode {
  id: string;
  name?: string;
  type?: string;
  order?: number;
  targets?: string[];
  chat_projection?: "progress" | "hidden" | "output";
  config?: {
    chat_projection?: "progress" | "hidden" | "output";
    [key: string]: unknown;
  };
}

export interface WorkflowDefinitionSnapshot {
  workflow_id?: string;
  name?: string;
  version?: number;
  fingerprint?: string;
  nodes?: WorkflowHistoryNode[];
}

export interface WorkflowArtifactRef {
  document_id?: string;
  fs_path?: string;
  name?: string;
  mime_type?: string;
  status?: string;
}

export interface WorkflowPublicationReceipt {
  schema_version: "publication-receipt/v1";
  platform: string;
  verification_status: "verified" | "unverified" | "failed";
  external_id?: string | null;
  published_url?: string | null;
  published_at: string;
  verified_at: string;
  payload_hash: string;
  fallback_used?: string;
  evidence?: Record<string, unknown>[];
  attempts?: Record<string, unknown>[];
  account?: string;
  content_type?: string;
  title?: string;
}

export interface WorkflowTraceEntry {
  sequence?: number;
  attempt_number?: number;
  node_id: string;
  node_name?: string;
  node_type?: string;
  status: string;
  started_at?: string;
  completed_at?: string;
  duration_ms?: number;
  input_summary?: unknown;
  output_summary?: unknown;
  error?: unknown;
  artifact_refs?: WorkflowArtifactRef[];
  child_run_ids?: string[];
}

export interface WorkflowHistoryRun {
  id: string;
  /** A newer run has made this run's project since; it is not continued. */
  superseded?: boolean;
  workflow_id?: string;
  binding_id?: string;
  workspace_id?: string;
  status: string;
  trigger_source?: string;
  current_step_id?: string;
  error?: unknown;
  started_at?: string;
  completed_at?: string;
  created_at?: string;
  updated_at?: string;
  retry_of_run_id?: string;
  lineage_root_run_id?: string;
  retry_from_step_id?: string;
  attempt_number?: number;
  lineage_status?: "canonical" | "legacy_untrusted_incomplete";
  workflow_name?: string;
  current_step_name?: string;
  workflow_definition_fingerprint?: string;
  definition_snapshot?: WorkflowDefinitionSnapshot;
  execution_trace?: WorkflowTraceEntry[];
  step_results?: Record<string, unknown>;
  variables?: Record<string, unknown>;
  capabilities?: { can_control?: boolean };
  business_outcome?: string;
  intervention?: WorkflowRunAction | null;
  resume_action?: WorkflowRunAction | null;
  processed_count?: number;
  total_count?: number;
  artifact_count?: number | null;
  history_blocker?: unknown;
  publication_receipts?: WorkflowPublicationReceipt[];
}

export interface WorkflowHistoryFamily {
  id: string;
  runs: WorkflowHistoryRun[];
  latestRun: WorkflowHistoryRun;
  attemptCount: number;
  status: string;
  businessOutcome: string;
  startedAt?: string;
  completedAt?: string;
  durationMs: number | null;
  processedCount: number;
  totalCount: number;
  artifactRefs: WorkflowArtifactRef[];
  artifactCount: number | null;
  blocker?: unknown;
}

export interface WorkflowRunTimelineEntry extends WorkflowTraceEntry {
  nodeId: string;
  nodeName: string;
  nodeType?: string;
  legacy: boolean;
}

export interface WorkflowSnapshotNode {
  nodeId: string;
  nodeName: string;
  nodeType?: string;
  order: number;
  status: string;
  targets: string[];
  legacy: boolean;
}

const ACTIVE_STATUSES = new Set<WorkflowRunStatus>(["pending", "running"]);
const ACTIONABLE_COMPLETED_OUTCOMES = new Set<WorkflowRunStatusLabelKey>([
  "needs_input",
  "revision_required",
]);
const PROCESSED_STATUSES = new Set<WorkflowRunStatus>([
  "completed",
  "failed",
  "cancelled",
  "skipped",
]);
// Steps that route or shape data rather than do work. They are not stages, so
// they never move a run's progress. Mirrors _CONTROL_STEP_TYPES in
// packages/core/services/workflow_run_trace.py, which sets the server's totals.
const CONTROL_STEP_TYPES = new Set([
  "trigger", "webhook", "end", "condition", "switch", "merge", "stop", "split", "parallel", "loop",
  "filter", "limit", "sort", "dedupe", "aggregate", "transform", "note", "datetime",
  "workflow_action_grant", "unsupported",
]);
const EDITABLE_SCHEMA_TYPES = new Set(["string", "number", "integer", "boolean", "array", "object"]);
const EDITABLE_SCHEMA_COMMON_KEYS = [
  "$schema",
  "type",
  "title",
  "description",
  "enum",
  "x-enum-labels",
  "const",
  "default",
  "x-workflow-type",
  "x-ui",
] as const;
const EDITABLE_SCHEMA_KEYS_BY_TYPE: Record<string, readonly string[]> = {
  string: ["format", "pattern", "minLength", "maxLength"],
  number: ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"],
  integer: ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"],
  boolean: [],
  array: ["items", "minItems", "maxItems", "uniqueItems"],
  object: ["properties", "required", "additionalProperties"],
};
const EDITABLE_SCHEMA_UI_KEYS = new Set([
  "hidden",
  "control",
  "rows",
  "collapsible",
  "collapsed",
  "order",
  "project_type",
  "allowed_stages",
  "record_source",
]);
const EDITABLE_SCHEMA_MAX_DEPTH = 12;
const ERROR_TEXT_BYTES = 8 * 1024;
const ARTIFACT_REF_LIMIT = 16;
const ARTIFACT_VALUE_LIMIT = 256;
const JSON_PREVIEW_DEPTH = 5;
const JSON_PREVIEW_ITEMS = 24;
const JSON_PREVIEW_KEY_LENGTH = 128;
const JSON_PREVIEW_STRING_LENGTH = 512;
const SENSITIVE_JSON_KEY = /(?:password|passwd|secret|token|api[_-]?key|authorization|credential|cookie)/i;
const SENSITIVE_PATH_TEXT = /(?:password|passwd|secret|token|api[_-]?key|authorization|credential|cookie)(?:\s*[:=]|%3d)|\bbearer(?:%20|\s)+/i;
const SENSITIVE_ARTIFACT_IDENTIFIER = /(?:^|[^A-Za-z0-9])(?:access[^A-Za-z0-9]*token|refresh[^A-Za-z0-9]*token|client[^A-Za-z0-9]*(?:secret|key)|private[^A-Za-z0-9]*key|api[^A-Za-z0-9]*key|auth(?:entication|orization)?[^A-Za-z0-9]*token|session[^A-Za-z0-9]*token|credentials?|secrets?)(?=$|[^A-Za-z0-9])/i;
const TOKEN_LIKE_SECRET_VALUE = /(?:^|[^A-Za-z0-9])(?:sk-|pk-|gsk_|ark[-_])[A-Za-z0-9_-]{8,}/i;
const TOKEN_LIKE_SECRET_VALUE_GLOBAL = /(^|[^A-Za-z0-9])(?:sk-|pk-|gsk_|ark[-_])[A-Za-z0-9_-]{8,}/gi;
const URL_LIKE_ARTIFACT_REFERENCE = /^(?:[A-Za-z][A-Za-z0-9+.-]*:|\/\/)/;
const CONTROL_CHARACTER = /[\u0000-\u001f\u007f]/;
const SAFE_ARTIFACT_EXTENSIONS = new Set([
  "png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif", "ico",
  "mp4", "webm", "mov", "m4v", "ogv", "avi", "mkv",
  "mp3", "wav", "ogg", "m4a", "flac", "aac", "wma",
  "srt", "vtt", "ass", "ssa",
  "pdf", "doc", "docx", "wps", "xls", "xlsx", "et", "ppt", "pptx", "dps",
  "csv", "tsv", "md", "markdown", "txt", "rtf", "html", "htm", "json",
  "css", "js", "jsx", "ts", "tsx", "py", "sql", "mmd", "mermaid", "drawio", "diagram",
]);
const ARTIFACT_REF_FIELDS = [
  "id",
  "artifact_id",
  "file_id",
  "document_id",
  "name",
  "title",
  "type",
  "kind",
  "path",
  "url",
  "uri",
  "mime_type",
  "media_type",
  "size",
  "size_bytes",
] as const;

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function editableSchemaType(schema: Record<string, unknown>): string | null {
  const declared = schema.type;
  if (typeof declared === "string") {
    return EDITABLE_SCHEMA_TYPES.has(declared) ? declared : null;
  }
  if (Array.isArray(declared)) {
    if (!declared.every((item) => typeof item === "string")) return null;
    const editableTypes = declared.filter((item) => item !== "null");
    return editableTypes.length === 1 && EDITABLE_SCHEMA_TYPES.has(editableTypes[0])
      ? editableTypes[0]
      : null;
  }
  if (declared !== undefined) return null;
  return Object.prototype.hasOwnProperty.call(schema, "properties") ? "object" : "string";
}

function schemaStringAnnotationIsCompatible(schema: Record<string, unknown>, key: string): boolean {
  return schema[key] === undefined || typeof schema[key] === "string";
}

function schemaNonNegativeIntegerIsCompatible(schema: Record<string, unknown>, key: string): boolean {
  const value = schema[key];
  return value === undefined || (Number.isInteger(value) && Number(value) >= 0);
}

function schemaFiniteNumberIsCompatible(schema: Record<string, unknown>, key: string): boolean {
  const value = schema[key];
  return value === undefined || (typeof value === "number" && Number.isFinite(value));
}

function editableSchemaUiIsCompatible(
  schema: Record<string, unknown>,
  schemaType: string,
  properties: Record<string, unknown> | null,
): boolean {
  if (schema["x-ui"] === undefined) return true;
  const ui = asRecord(schema["x-ui"]);
  if (!ui || Object.keys(ui).some((key) => !EDITABLE_SCHEMA_UI_KEYS.has(key))) return false;
  for (const key of ["hidden", "collapsible", "collapsed"]) {
    if (ui[key] !== undefined && typeof ui[key] !== "boolean") return false;
  }
  if (
    (ui.collapsible !== undefined || ui.collapsed !== undefined)
    && schemaType !== "object"
  ) return false;
  if (
    ui.control !== undefined
    && !["textarea", "line_list", "workflow_project_select", "documents", "record_select"].includes(String(ui.control))
  ) return false;
  if (ui.control === "line_list" && schemaType !== "array") return false;
  if (ui.control === "documents" && schemaType !== "array") return false;
  if (ui.control === "textarea" && !["string", "array"].includes(schemaType)) return false;
  if (ui.control === "workflow_project_select") {
    // One record (a string) or several (an array of strings).
    const items = asRecord(schema.items);
    const multiple = schemaType === "array" && Boolean(items) && editableSchemaType(items!) === "string";
    if (!multiple && schemaType !== "string") return false;
    if (typeof ui.project_type !== "string" || !ui.project_type.trim()) return false;
    // Without allowed_stages the picker offers every stage.
    if (
      ui.allowed_stages !== undefined && (
        !Array.isArray(ui.allowed_stages)
        || ui.allowed_stages.length === 0
        || ui.allowed_stages.length > 20
        || ui.allowed_stages.some((stage) => typeof stage !== "string" || !stage.trim())
        || new Set(ui.allowed_stages).size !== ui.allowed_stages.length
      )
    ) return false;
  } else if (ui.project_type !== undefined || ui.allowed_stages !== undefined) {
    return false;
  }
  if (ui.control === "record_select") {
    const source = asRecord(ui.record_source);
    const items = asRecord(schema.items);
    const multiple = schemaType === "array" && Boolean(items) && editableSchemaType(items!) === "string";
    if (!multiple && schemaType !== "string") return false;
    if (schema.enum !== undefined || items?.enum !== undefined) return false;
    if (!source || typeof source.ledger !== "string" || !source.ledger.trim()) return false;
  } else if (ui.record_source !== undefined) {
    return false;
  }
  if (
    ui.rows !== undefined
    && (!Number.isInteger(ui.rows) || Number(ui.rows) < 1)
  ) return false;
  if (ui.order !== undefined) {
    if (schemaType !== "object" || !properties || !Array.isArray(ui.order)) return false;
    if (
      ui.order.some((key) => (
        typeof key !== "string" || !Object.prototype.hasOwnProperty.call(properties, key)
      ))
      || new Set(ui.order).size !== ui.order.length
    ) return false;
  }
  return true;
}

function editableSchemaFieldIsCompatible(value: unknown, depth: number): boolean {
  if (depth > EDITABLE_SCHEMA_MAX_DEPTH) return false;
  const schema = asRecord(value);
  if (!schema) return false;
  const schemaType = editableSchemaType(schema);
  if (!schemaType) return false;
  const allowedKeys = new Set([
    ...EDITABLE_SCHEMA_COMMON_KEYS,
    ...(EDITABLE_SCHEMA_KEYS_BY_TYPE[schemaType] || []),
  ]);
  if (Object.keys(schema).some((key) => !allowedKeys.has(key))) return false;
  if (
    !schemaStringAnnotationIsCompatible(schema, "$schema")
    || !schemaStringAnnotationIsCompatible(schema, "title")
    || !schemaStringAnnotationIsCompatible(schema, "description")
  ) return false;
  if (
    schema.enum !== undefined
    && (
      !Array.isArray(schema.enum)
      || schema.enum.some((item) => !["string", "number", "boolean"].includes(typeof item) && item !== null)
    )
  ) return false;
  if (
    schema["x-enum-labels"] !== undefined
    && (
      !Array.isArray(schema.enum)
      || !Array.isArray(schema["x-enum-labels"])
      || schema["x-enum-labels"].length !== schema.enum.length
      || schema["x-enum-labels"].some((item) => typeof item !== "string")
    )
  ) return false;
  if (
    schema["x-workflow-type"] !== undefined
    && (schema["x-workflow-type"] !== "json" || schemaType !== "string")
  ) return false;

  const properties = schemaType === "object" ? asRecord(schema.properties) : null;
  if (!editableSchemaUiIsCompatible(schema, schemaType, properties)) return false;
  if (asRecord(schema["x-ui"])?.hidden === true) return true;
  if (schemaType === "string") {
    if (schema.format !== undefined && schema.format !== "uri") return false;
    if (!schemaStringAnnotationIsCompatible(schema, "pattern")) return false;
    if (
      !schemaNonNegativeIntegerIsCompatible(schema, "minLength")
      || !schemaNonNegativeIntegerIsCompatible(schema, "maxLength")
      || (
        schema.minLength !== undefined
        && schema.maxLength !== undefined
        && Number(schema.minLength) > Number(schema.maxLength)
      )
    ) return false;
    if (typeof schema.pattern === "string") {
      try {
        new RegExp(schema.pattern);
      } catch {
        return false;
      }
    }
    return true;
  }
  if (schemaType === "number" || schemaType === "integer") {
    const numericKeys = ["minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"];
    return numericKeys.every((key) => schemaFiniteNumberIsCompatible(schema, key));
  }
  if (schemaType === "boolean") return true;
  if (schemaType === "array") {
    if (
      !schemaNonNegativeIntegerIsCompatible(schema, "minItems")
      || !schemaNonNegativeIntegerIsCompatible(schema, "maxItems")
      || (
        schema.minItems !== undefined
        && schema.maxItems !== undefined
        && Number(schema.minItems) > Number(schema.maxItems)
      )
      || (schema.uniqueItems !== undefined && typeof schema.uniqueItems !== "boolean")
    ) return false;
    if (schema.items !== undefined && !editableSchemaFieldIsCompatible(schema.items, depth + 1)) {
      return false;
    }
    const ui = asRecord(schema["x-ui"]);
    if (ui?.control === "line_list") {
      const itemSchema = asRecord(schema.items);
      if (!itemSchema || editableSchemaType(itemSchema) !== "string") return false;
    }
    return true;
  }
  if (!properties) return false;
  const required = schema.required;
  if (
    required !== undefined
    && (!Array.isArray(required) || required.some((key) => (
      typeof key !== "string" || !Object.prototype.hasOwnProperty.call(properties, key)
    )) || new Set(required).size !== required.length)
  ) return false;
  if (schema.additionalProperties !== undefined && schema.additionalProperties !== false) return false;
  return Object.entries(properties).every(([key, field]) => (
    Boolean(key.trim()) && editableSchemaFieldIsCompatible(field, depth + 1)
  ));
}

/** The values a retry form starts from. Segment ids go back only when the
 *  retry schema declares them: the server rejects a variable the Workflow does
 *  not declare. The run page and the chat bubble both start from this. */
export function workflowRetrySuppliedValues(
  action: Pick<WorkflowRunAction, "kind" | "values" | "retry_segment_ids">,
  schema: WorkflowInputSchema | null | undefined,
): Record<string, unknown> {
  const values: Record<string, unknown> = { ...(asRecord(action.values) || {}) };
  if (
    action.kind === "workflow_retry"
    && asRecord(schema?.properties)?.retry_segment_ids
    && action.retry_segment_ids
  ) {
    values.retry_segment_ids = action.retry_segment_ids;
  }
  return values;
}

export function workflowRetrySchemaIsCompatible(value: unknown): value is WorkflowInputSchema {
  const schema = asRecord(value);
  return Boolean(
    schema
    && editableSchemaType(schema) === "object"
    && editableSchemaFieldIsCompatible(schema, 0),
  );
}

function boundedJsonKey(key: string, index: number): string {
  const characters = Array.from(key);
  if (characters.length <= JSON_PREVIEW_KEY_LENGTH) return key;
  const suffix = `...#${index + 1}`;
  return `${characters.slice(0, JSON_PREVIEW_KEY_LENGTH - suffix.length).join("")}${suffix}`;
}

function redactSensitiveText(value: string): string {
  return value
    .replace(TOKEN_LIKE_SECRET_VALUE_GLOBAL, "$1[REDACTED]")
    .replace(/\bBearer\s+[^\s,;]+/gi, "Bearer [REDACTED]")
    .replace(
      /((?:password|passwd|secret|token|api[_-]?key|authorization|credential|cookie)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)/gi,
      "$1[REDACTED]",
    );
}

export function boundedJsonPreview(
  value: unknown,
  depth = 0,
  seen = new WeakSet<object>(),
): unknown {
  if (value == null || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "bigint") return `${value}`;
  if (typeof value === "string") {
    const redacted = redactSensitiveText(value);
    return redacted.length > JSON_PREVIEW_STRING_LENGTH
      ? `${redacted.slice(0, JSON_PREVIEW_STRING_LENGTH)}...`
      : redacted;
  }
  if (typeof value === "function" || typeof value === "symbol") return undefined;
  if (typeof value !== "object") return undefined;
  if (seen.has(value)) return "[Circular]";
  seen.add(value);
  if (depth >= JSON_PREVIEW_DEPTH) {
    return { value_type: Array.isArray(value) ? "array" : "object" };
  }
  if (value instanceof Error) {
    return { name: value.name, message: boundedJsonPreview(value.message, depth + 1, seen) };
  }
  if (value instanceof Date) return Number.isFinite(value.getTime()) ? value.toISOString() : null;
  if (Array.isArray(value)) {
    const preview = value.slice(0, JSON_PREVIEW_ITEMS)
      .map((item) => boundedJsonPreview(item, depth + 1, seen));
    if (value.length > JSON_PREVIEW_ITEMS) {
      preview.push({ remaining_count: value.length - JSON_PREVIEW_ITEMS });
    }
    return preview;
  }
  const preview: Record<string, unknown> = Object.create(null);
  let scannedKeyCount = 0;
  let previewKeyCount = 0;
  let hasAdditionalProperties = false;
  for (const key in value) {
    scannedKeyCount += 1;
    if (scannedKeyCount > JSON_PREVIEW_ITEMS) {
      hasAdditionalProperties = true;
      break;
    }
    if (!Object.prototype.hasOwnProperty.call(value, key) || key === "toJSON") continue;
    try {
      const child = SENSITIVE_JSON_KEY.test(key)
        ? "[REDACTED]"
        : boundedJsonPreview((value as Record<string, unknown>)[key], depth + 1, seen);
      if (child !== undefined) {
        preview[boundedJsonKey(key, previewKeyCount)] = child;
        previewKeyCount += 1;
      }
    } catch {
      preview[boundedJsonKey(key, previewKeyCount)] = { value_type: "unavailable" };
      previewKeyCount += 1;
    }
  }
  if (hasAdditionalProperties) {
    let markerKey = "additional_properties_omitted";
    while (Object.prototype.hasOwnProperty.call(preview, markerKey)) markerKey = `_${markerKey}`;
    preview[markerKey] = true;
  }
  return preview;
}

function readableJson(value: unknown): string {
  try {
    const encoded = JSON.stringify(boundedJsonPreview(value), null, 2);
    if (encoded !== undefined) return encoded;
  } catch {
    // The stable type fallback below still avoids opaque object coercion.
  }
  return JSON.stringify({ value_type: typeof value }, null, 2);
}

function readablePart(value: unknown): string {
  if (typeof value === "string") return redactSensitiveText(value.trim());
  if (typeof value === "number" || typeof value === "boolean" || typeof value === "bigint") {
    return `${value}`;
  }
  if (value == null) return "";
  return readableJson(value);
}

function readablePath(value: unknown): string {
  if (!Array.isArray(value)) return readablePart(value);
  return value.map((part) => readablePart(part)).filter(Boolean).join(".");
}

function compactArtifactValue(value: unknown): string | number | boolean | null {
  if (value == null) return null;
  if (typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > ARTIFACT_VALUE_LIMIT
    ? `${trimmed.slice(0, ARTIFACT_VALUE_LIMIT)}...`
    : trimmed;
}

function compactArtifactRef(value: unknown): unknown {
  if (typeof value === "string") return compactArtifactValue(value);
  const record = asRecord(value);
  if (!record) return null;
  const compact = Object.fromEntries(ARTIFACT_REF_FIELDS.flatMap((field) => {
    const fieldValue = compactArtifactValue(record[field]);
    return fieldValue === null || fieldValue === "" ? [] : [[field, fieldValue]];
  }));
  return Object.keys(compact).length ? compact : { value_type: "artifact" };
}

function compactArtifactRefs(value: unknown): unknown {
  const refs = Array.isArray(value) ? value : [value];
  const compact = refs.slice(0, ARTIFACT_REF_LIMIT).map(compactArtifactRef);
  if (refs.length > ARTIFACT_REF_LIMIT) {
    compact.push({ remaining_count: refs.length - ARTIFACT_REF_LIMIT });
  }
  return compact;
}

function safeArtifactString(value: unknown): string {
  if (typeof value !== "string") return "";
  const redacted = redactSensitiveText(value.trim());
  return redacted.length > ARTIFACT_VALUE_LIMIT
    ? `${redacted.slice(0, ARTIFACT_VALUE_LIMIT - 3)}...`
    : redacted;
}

function decodedArtifactReference(value: string): string | null {
  try {
    return decodeURIComponent(value.replaceAll("\\", "/"));
  } catch {
    return null;
  }
}

function artifactExtension(value: string): string {
  const basename = value.split("/").filter(Boolean).pop() || "";
  const match = basename.match(/\.([A-Za-z0-9]+)$/);
  return match?.[1].toLowerCase() || "";
}

function artifactReferenceContainsSensitiveSegment(value: string): boolean {
  return (
    TOKEN_LIKE_SECRET_VALUE.test(value)
    || value.split("/").some((segment) => SENSITIVE_ARTIFACT_IDENTIFIER.test(segment))
  );
}

function safeArtifactLabel(input: unknown): string {
  if (typeof input !== "string") return "";
  const label = input.trim();
  const decoded = decodedArtifactReference(label);
  if (
    !decoded
    || URL_LIKE_ARTIFACT_REFERENCE.test(decoded)
    || decoded.includes("/")
    || decoded.includes("?")
    || decoded.includes("#")
    || CONTROL_CHARACTER.test(decoded)
    || artifactReferenceContainsSensitiveSegment(decoded)
    || (
      !/\s/.test(decoded)
      && decoded.includes(".")
      && !SAFE_ARTIFACT_EXTENSIONS.has(artifactExtension(decoded))
    )
  ) return "";
  return safeArtifactString(label);
}

function safeArtifactDocumentId(input: unknown): string {
  const documentId = safeArtifactString(input);
  return documentId
    && !SENSITIVE_PATH_TEXT.test(documentId)
    && /^[A-Za-z0-9._:-]+$/.test(documentId)
    ? documentId
    : "";
}

function safeArtifactPath(value: unknown): string {
  if (typeof value !== "string") return "";
  const trimmed = value.trim();
  if (!trimmed) return "";
  const path = trimmed.replaceAll("\\", "/");
  if (path.length > ARTIFACT_VALUE_LIMIT || CONTROL_CHARACTER.test(path)) return "";
  const decoded = decodedArtifactReference(path);
  if (
    !decoded
    || URL_LIKE_ARTIFACT_REFERENCE.test(decoded)
    || decoded.includes("?")
    || decoded.includes("#")
    || CONTROL_CHARACTER.test(decoded)
    || SENSITIVE_PATH_TEXT.test(decoded)
    || artifactReferenceContainsSensitiveSegment(decoded)
    || decoded.split("/").some((segment) => segment === ".." || segment === "~")
    || (decoded.startsWith("/") && !decoded.startsWith("/api/v1/fs/"))
    || !SAFE_ARTIFACT_EXTENSIONS.has(artifactExtension(decoded))
  ) return "";
  return path;
}

export function normalizeWorkflowArtifactRefs(value: unknown): WorkflowArtifactRef[] {
  const refs = Array.isArray(value) ? value : [value];
  return refs.slice(0, ARTIFACT_REF_LIMIT).flatMap((item) => {
    const record = asRecord(item);
    if (!record) return [];
    const documentId = safeArtifactDocumentId(record.document_id);
    const fsPath = safeArtifactPath(record.fs_path ?? record.path);
    const name = safeArtifactLabel(record.name);
    const mimeType = safeArtifactString(record.mime_type);
    const status = safeArtifactString(record.status);
    const normalized: WorkflowArtifactRef = {};
    if (documentId) normalized.document_id = documentId;
    if (fsPath) normalized.fs_path = fsPath;
    if (name) normalized.name = name;
    if (mimeType) normalized.mime_type = mimeType;
    if (status) normalized.status = status;
    return Object.keys(normalized).length ? [normalized] : [];
  });
}

export function workflowArtifactLabel(
  ref: WorkflowArtifactRef,
  fallback: string,
): string {
  const name = safeArtifactLabel(ref.name);
  if (name) return name;
  const path = safeArtifactPath(ref.fs_path);
  return path.split(/[\\/]/).filter(Boolean).pop() || fallback;
}

function utf8Prefix(text: string, byteLimit: number, encoder: TextEncoder): string {
  const characters = Array.from(text);
  let low = 0;
  let high = characters.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (encoder.encode(characters.slice(0, middle).join("")).byteLength <= byteLimit) low = middle;
    else high = middle - 1;
  }
  return characters.slice(0, low).join("");
}

function boundedUtf8(text: string, truncationText: string): string {
  const encoder = new TextEncoder();
  if (encoder.encode(text).byteLength <= ERROR_TEXT_BYTES) return text;
  const truncationMarker = utf8Prefix(
    `\n... ${truncationText.trim()}`,
    ERROR_TEXT_BYTES,
    encoder,
  );
  const characters = Array.from(text);
  let low = 0;
  let high = characters.length;
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    const candidate = `${characters.slice(0, middle).join("")}${truncationMarker}`;
    if (encoder.encode(candidate).byteLength <= ERROR_TEXT_BYTES) low = middle;
    else high = middle - 1;
  }
  return `${characters.slice(0, low).join("")}${truncationMarker}`;
}

/** A run error's lead-in lines, then its "- " items (one per line from a list-valued stop message). */
export function splitWorkflowErrorItems(text: string): { lead: string[]; items: string[] } {
  const lead: string[] = [];
  const items: string[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith("- ")) items.push(line.slice(2).trim());
    else lead.push(line);
  }
  return { lead, items };
}

export function formatWorkflowError(value: unknown, truncationText: string): string {
  if (value == null || value === "") return "";
  const explain = (text: string) => text === "workflow_effect_deadline_expired"
    ? "The allowed time window has ended, so this action was not executed. Arrange a new window before retrying. (workflow_effect_deadline_expired)"
    : text;
  if (value instanceof Error) return boundedUtf8(redactSensitiveText(explain(value.message || value.name)), truncationText);
  if (typeof value === "string") return boundedUtf8(redactSensitiveText(explain(value.trim())), truncationText);

  const record = asRecord(value);
  if (record) {
    const message = readablePart(record.message ?? record.detail ?? record.error);
    const code = readablePart(record.code);
    const path = readablePath(record.path);
    const requiredChange = readablePart(record.required_change);
    const retryFromStepId = readablePart(record.retry_from_step_id);
    const hasArtifactRefs = Object.prototype.hasOwnProperty.call(record, "artifact_refs");
    const artifactRefs = hasArtifactRefs
      ? readableJson(compactArtifactRefs(record.artifact_refs))
      : "";
    const extracted = [
      explain(message),
      code ? `[${code}]` : "",
      path,
      requiredChange,
      retryFromStepId ? `retry_from_step_id: ${retryFromStepId}` : "",
      artifactRefs ? `artifact_refs: ${artifactRefs}` : "",
    ].filter(Boolean);
    if (extracted.length) return boundedUtf8(extracted.join("\n"), truncationText);
  }

  return boundedUtf8(readableJson(value), truncationText);
}

export function formatWorkflowValue(value: unknown, truncationText: string): string {
  return boundedUtf8(readableJson(value), truncationText);
}

export function isWorkflowRunActive(run: Pick<WorkflowRunView, "status">): boolean {
  return ACTIVE_STATUSES.has(run.status);
}

export function canCancelWorkflowRun(run: {
  status?: string;
  business_outcome?: string | null;
  businessOutcome?: string | null;
  capabilities?: { can_control?: boolean };
}): boolean {
  if (run.capabilities?.can_control !== true) return false;
  const status = String(run.status || "").trim().toLowerCase();
  if (["pending", "running", "paused", "failed"].includes(status)) return true;
  const businessOutcome = String(
    run.business_outcome ?? run.businessOutcome ?? "",
  ).trim().toLowerCase();
  return status === "completed"
    && ACTIONABLE_COMPLETED_OUTCOMES.has(businessOutcome as WorkflowRunStatusLabelKey);
}

const RUN_STATES: Record<WorkflowRunStatusLabelKey, WorkflowRunState> = {
  pending: "queued",
  running: "running",
  paused: "needs_you",
  needs_input: "needs_you",
  revision_required: "needs_you",
  ready_for_acceptance: "needs_you",
  completed: "completed",
  skipped: "completed",
  failed: "failed",
  cancelled: "cancelled",
};

function withState(
  presentation: Omit<WorkflowRunStatusPresentation, "state" | "stateKey">,
  state: WorkflowRunState = RUN_STATES[presentation.labelKey] || "queued",
): WorkflowRunStatusPresentation {
  return { ...presentation, state, stateKey: `component.workflow_run.state.${state}` };
}

/** i18n key of the shown status for a bare run status (no outcome known). */
export function workflowRunStateKey(status: string): string {
  return `component.workflow_run.state.${RUN_STATES[status as WorkflowRunStatus] || "queued"}`;
}

export function workflowRunStatusPresentation(
  run: Pick<WorkflowRunView, "status" | "businessOutcome"> & Partial<Pick<WorkflowRunView, "nodes" | "currentNodeId">>,
): WorkflowRunStatusPresentation {
  // A run paused only for background work (an automatic retry, a render, a
  // timer) needs nobody: it is queued and continues on its own.
  const current = run.status === "paused" && run.currentNodeId
    ? run.nodes?.find((node) => node.id === run.currentNodeId) : undefined;
  if (current?.status === "running") {
    return withState({ labelKey: "running", iconStatus: "running", motion: "running" }, "queued");
  }
  const businessOutcome = String(run.businessOutcome || "").trim().toLowerCase();
  if (
    run.status === "completed"
   && ACTIONABLE_COMPLETED_OUTCOMES.has(businessOutcome as WorkflowRunStatusLabelKey)
  ) {
    return withState({
      labelKey: businessOutcome as WorkflowRunStatusLabelKey,
      iconStatus: "paused",
      motion: "waiting",
    });
  }
  return withState({
    labelKey: run.status,
    iconStatus: run.status,
    motion: ACTIVE_STATUSES.has(run.status)
      ? "running"
      : run.status === "paused" ? "waiting" : "static",
  });
}

export interface WorkflowRunFailure {
  /** Node the run stopped on; retrying from it keeps every earlier result. */
  stepId: string;
  stepName: string;
  /** Where a retry should restart; the failed node unless it names another. */
  retryFromStepId: string;
  title: string;
  detail: string;
  /** Short "what to change" hint before retrying, when the cause is known. */
  hint: string;
  /** Raw technical details, shown collapsed; never the headline. */
  technicalDetails: string;
}

const INSUFFICIENT_CREDITS = /insufficient credits:\s*required\s*([\d.,]+)\s*,\s*available\s*([\d.,-]+)/i;
// A client delivery trial runs on its provider's credits; the server's
// message names no amounts (TRIAL_CREDIT_MESSAGE).
const TRIAL_CREDITS_USED_UP = /provider['’]s credits for this trial are used up/i;

function formatCredits(raw: string): string {
  const value = Number(raw.replace(/,/g, ""));
  return Number.isFinite(value) ? Math.round(value).toLocaleString("en-US") : raw;
}

/**
 * Explain a failed run in terms of the node it stopped on. Falls back to the
 * failed node's own error when the run-level error is empty, so a failure is
 * never shown without a reason.
 */
/**
 * Canvas status for one node result. A node paused on background work (a
 * rendering video, a timer, a child flow) is still working, so it shows as
 * running; only waits for a person stay paused.
 */
export function workflowNodeDisplayStatus(result: {
  status?: unknown;
  wait_type?: unknown;
  subrun_id?: unknown;
}): string {
  const status = String(result.status || "");
  if (status !== "paused") return status;
  const waitType = String(result.wait_type || "");
  if (
    waitType === "media_job"
    || waitType === "timer"
    || waitType === "transient_retry"
    || (!waitType && result.subrun_id)
  ) return "running";
  return status;
}

/**
 * What a paused run is waiting on when nobody needs to act: rendering media,
 * a timer, or a child flow. Null when the pause waits for a person.
 */
export function workflowRunBackgroundWait(
  run: {
    status?: string;
    current_step_id?: string | null;
    step_results?: Record<string, unknown> | null;
    workflow_steps?: Array<{ id: string; status: string }> | null;
  } | null | undefined,
): string | null {
  if (!run || run.status !== "paused") return null;
  const result = asRecord((run.step_results || {})[String(run.current_step_id || "")]);
  if (!result) {
    // A run summary carries no step results, but its step list already says
    // a paused step that waits on background work (a timer, a render, an
    // automatic retry) is still running; only a wait for a person is paused.
    const step = (run.workflow_steps || []).find((item) => item.id === run.current_step_id);
    return step?.status === "running" ? "Waiting on background work · continues on its own" : null;
  }
  if (workflowNodeDisplayStatus(result) !== "running") return null;
  const waitType = String(result.wait_type || "");
  if (waitType === "media_job") return "Rendering media · continues on its own";
  if (waitType === "timer") return "Waiting on a timer · continues on its own";
  if (waitType === "transient_retry") {
    // e.g. "Retrying (1/3): the model provider stopped responding."
    const message = typeof result.output === "string" && result.output.trim()
      ? result.output.trim()
      : "Retrying after a temporary problem.";
    return `${message} · continues on its own`;
  }
  return "Waiting on a sub-flow · continues on its own";
}

type FailedRun = {
  status?: string;
  error?: unknown;
  current_step_id?: string | null;
  step_results?: Record<string, unknown> | null;
} | null | undefined;

function failedStepIdOf(run: NonNullable<FailedRun>): string {
  const results = run.step_results || {};
  return String(run.current_step_id || "").trim()
    || Object.entries(results).reverse().find(([, value]) => asRecord(value)?.status === "failed")?.[0]
    || "";
}

/**
 * Technical details behind a failed run's headline: the raw tool error of a
 * platform failure, or the raw text the server lifted out of the headline.
 * Shown collapsed, never as the headline itself. Empty when there are none.
 */
export function workflowRunTechnicalDetails(run: FailedRun, truncatedLabel = "…"): string {
  if (!run || run.status !== "failed") return "";
  const result = asRecord((run.step_results || {})[failedStepIdOf(run)]);
  const platformFailure = asRecord(result?.platform_failure);
  const details = platformFailure
    ? { code: platformFailure.code, ...(asRecord(platformFailure.details) || {}) }
    : result?.error_details;
  if (details === undefined || details === null || details === "") return "";
  return formatWorkflowValue(details, truncatedLabel);
}

export function workflowRunFailure(
  run: {
    status?: string;
    error?: unknown;
    current_step_id?: string | null;
    step_results?: Record<string, unknown> | null;
  } | null | undefined,
  steps: ReadonlyArray<{ id: string; name?: string }> = [],
): WorkflowRunFailure | null {
  if (!run || run.status !== "failed") return null;
  const results = run.step_results || {};
  const failedStepId = failedStepIdOf(run);
  const stepResult = asRecord(results[failedStepId]);
  const stepName = steps.find((step) => step.id === failedStepId)?.name?.trim() || failedStepId;
  const rawError = formatWorkflowError(run.error, "…")
    || formatWorkflowError(stepResult?.error, "…");
  const retryFromStepId = String(stepResult?.retry_from_step_id || "").trim() || failedStepId;
  const technicalDetails = workflowRunTechnicalDetails(run);
  const credits = INSUFFICIENT_CREDITS.exec(rawError);
  if (credits) {
    return {
      stepId: failedStepId,
      stepName,
      retryFromStepId,
      technicalDetails,
      title: "Not enough credits",
      detail: `${stepName ? `“${stepName}” needs` : "This step needs"} ${formatCredits(credits[1])} credits, `
        + `but your balance is ${formatCredits(credits[2])}.`,
      hint: "Add credits and retry from this step (earlier results are kept), "
        + "or edit the inputs, for example a shorter video, and run again.",
    };
  }
  if (TRIAL_CREDITS_USED_UP.test(rawError)) {
    return {
      stepId: failedStepId,
      stepName,
      retryFromStepId,
      technicalDetails,
      title: "Trial credits used up",
      detail: `${stepName ? `“${stepName}” stopped: the` : "The"} provider pays for AI usage during this trial, `
        + "and its credits are used up.",
      hint: "Ask the provider to add credits, or accept the delivery to continue on your own plan, "
        + "then retry from this step (earlier results are kept).",
    };
  }
  return {
    stepId: failedStepId,
    stepName,
    retryFromStepId,
    technicalDetails,
    title: stepName ? `“${stepName}” failed` : "Run failed",
    detail: rawError || "The run stopped without reporting an error.",
    hint: failedStepId ? "Review this failed run before retrying." : "",
  };
}

function identityValue(value: unknown): string {
  return typeof value === "string" || typeof value === "number" ? `${value}`.trim() : "";
}

export function actionIdentity(action: WorkflowRunAction): string {
  return JSON.stringify([
    identityValue(action.kind),
    identityValue(action.workflow_run_id),
    identityValue(action.retry_from_step_id),
    identityValue(action.step_id),
    identityValue(action.action_id ?? action.id ?? action.action ?? action.response_variable),
  ]);
}

export interface WorkflowEditorStep {
  id: string;
  name?: string;
  type?: string;
  next?: string[];
  true_next?: string[];
  false_next?: string[];
  config?: {
    cases?: { next?: string[] }[];
    default_next?: string[];
    [key: string]: unknown;
  };
}

export interface WorkflowFinalResult {
  stepId: string | null;
  stepName: string;
  output: unknown;
}

export interface WorkflowFinalOutputField {
  key: string;
  label: string;
  value: string;
  structured: boolean;
  /** Openable file reference when the raw value was a storage URL. */
  reference?: string;
  /** Redacted, bounded structured value for the readable view. */
  raw?: unknown;
}

const STORAGE_FILE_URL = /^\/api\/v1\/fs\/[^/?#]+\/[^?#]+$/;
const WORKSPACE_STORAGE_PATH = /^Workspaces\/_by_id\/[^/]+(?:\/|$)/;

function lastPathSegment(value: string): string {
  const segment = value.split(/[?#]/)[0].split("/").filter(Boolean).pop() || "";
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

/** Storage URLs and `_by_id` paths are routing details, not display copy. */
function storageFieldDisplay(value: string): { value: string; reference?: string } | null {
  const text = value.trim();
  if (STORAGE_FILE_URL.test(text)) {
    const name = lastPathSegment(text);
    return name ? { value: name, reference: text } : null;
  }
  if (WORKSPACE_STORAGE_PATH.test(text)) {
    const name = lastPathSegment(text);
    return name && !/^_by_id$/.test(name) ? { value: name } : null;
  }
  return null;
}

const FINAL_OUTPUT_WRAPPERS = new Set(["data", "output", "result", "state"]);
const FINAL_OUTPUT_FIELD_LIMIT = 16;

function workflowFinalOutputLabel(key: string): string {
  const words = key
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .replace(/[._-]+/g, " ")
    .trim()
    .toLowerCase();
  const label = words ? `${words.charAt(0).toUpperCase()}${words.slice(1)}` : "Value";
  return label
    .replace(/\bid\b/gi, "ID")
    .replace(/\burl\b/gi, "URL")
    .replace(/\buri\b/gi, "URI")
    .replace(/\bmp4\b/gi, "MP4")
    .replace(/\byoutube\b/gi, "YouTube");
}

function unwrapWorkflowFinalOutput(value: unknown): Record<string, unknown> | null {
  let record = asRecord(value);
  for (let depth = 0; record && depth < 2; depth += 1) {
    const entries = Object.entries(record).filter(([, fieldValue]) => fieldValue !== undefined);
    if (entries.length !== 1 || !FINAL_OUTPUT_WRAPPERS.has(entries[0][0])) break;
    const nested = asRecord(entries[0][1]);
    if (!nested) break;
    record = nested;
  }
  return record;
}

const HEADLINE_TITLE_KEYS = ["title", "name", "headline"];
const HEADLINE_SUMMARY_KEYS = ["summary", "description", "message"];

/** The title/summary a final result leads with, taken from any flow's output. */
export function workflowFinalHeadline(
  output: unknown,
): { title?: string; summary?: string; keys: string[] } | null {
  const record = unwrapWorkflowFinalOutput(output);
  if (!record) return null;
  const pick = (candidates: string[]) => candidates.find((key) => (
    typeof record[key] === "string" && String(record[key]).trim()
  ));
  const titleKey = pick(HEADLINE_TITLE_KEYS);
  const summaryKey = pick(HEADLINE_SUMMARY_KEYS);
  if (!titleKey && !summaryKey) return null;
  return {
    ...(titleKey ? { title: redactSensitiveText(String(record[titleKey]).trim()) } : {}),
    ...(summaryKey ? { summary: redactSensitiveText(String(record[summaryKey]).trim()) } : {}),
    keys: [titleKey, summaryKey].filter((key): key is string => Boolean(key)),
  };
}

function locatesAFile(value: unknown): boolean {
  const record = asRecord(value);
  return !!record && ["document_id", "fs_path", "path", "url", "reference"].some((key) => (
    typeof record[key] === "string" && String(record[key]).trim()
  ));
}

/** Output keys whose value is only a list of files (shown as file cards). */
export function workflowFinalFileListKeys(output: unknown): string[] {
  const record = unwrapWorkflowFinalOutput(output);
  if (!record) return [];
  return Object.entries(record)
    .filter(([, value]) => Array.isArray(value) && value.length > 0 && value.every(locatesAFile))
    .map(([key]) => key);
}

/** README files lead a result's file list; everything else keeps output order. */
export function orderWorkflowFinalArtifacts(refs: WorkflowArtifactRef[]): WorkflowArtifactRef[] {
  const isReadme = (ref: WorkflowArtifactRef) => /^readme(\.|$)/i.test(
    workflowArtifactLabel(ref, ""),
  );
  return [...refs.filter(isReadme), ...refs.filter((ref) => !isReadme(ref))];
}

export function workflowFinalOutputFields(
  output: unknown,
  { omitKeys = [] }: { omitKeys?: string[] } = {},
): WorkflowFinalOutputField[] | null {
  const record = unwrapWorkflowFinalOutput(output);
  if (!record) return null;
  const omitted = new Set(omitKeys);
  const entries = Object.entries(record).filter(([key, value]) => value !== undefined && !omitted.has(key));
  if (omitted.size && !entries.length) return null;
  const orderedEntries = [
    ...entries.filter(([, value]) => value === null || typeof value !== "object"),
    ...entries.filter(([, value]) => value !== null && typeof value === "object"),
  ];
  const fields = orderedEntries.slice(0, FINAL_OUTPUT_FIELD_LIMIT).map(([key, value]) => {
    const structured = value !== null && typeof value === "object";
    let displayValue: string;
    if (structured) displayValue = readableJson(value);
    else if (typeof value === "boolean") displayValue = value ? "Yes" : "No";
    else if (value === null) displayValue = "None";
    else if (typeof value === "string") {
      const storage = storageFieldDisplay(value);
      if (storage) {
        return {
          key,
          label: workflowFinalOutputLabel(key),
          value: storage.value,
          structured,
          ...(storage.reference ? { reference: storage.reference } : {}),
        };
      }
      displayValue = redactSensitiveText(value);
    }
    else if (typeof value === "number" || typeof value === "bigint") displayValue = `${value}`;
    else displayValue = readableJson(value);
    return {
      key,
      label: workflowFinalOutputLabel(key),
      value: displayValue,
      structured,
      ...(structured ? { raw: boundedJsonPreview(value) } : {}),
    };
  });
  if (entries.length > FINAL_OUTPUT_FIELD_LIMIT) {
    fields.push({
      key: "__additional_fields",
      label: "Additional fields",
      value: `${entries.length - FINAL_OUTPUT_FIELD_LIMIT} more`,
      structured: false,
    });
  }
  return fields;
}

export function workflowFinalArtifactRefs(output: unknown, limit = 6): WorkflowArtifactRef[] {
  const refs: WorkflowArtifactRef[] = [];
  const identities = new Set<string>();
  const visited = new WeakSet<object>();
  const boundedLimit = Math.max(0, Math.min(limit, 50));
  const visit = (value: unknown, depth: number) => {
    if (value == null || refs.length >= boundedLimit || depth > 6) return;
    if (Array.isArray(value)) {
      for (const item of value) visit(item, depth + 1);
      return;
    }
    const record = asRecord(value);
    if (!record || visited.has(record)) return;
    visited.add(record);
    // Only records that locate a file are artifacts. A result object's own
    // `status`/`name` must not become a nameless, unopenable "File 1" that
    // also dedupes every real file away under the empty identity.
    const viewerDocumentId = [record.reference, record.viewer_url, record.url]
      .map((candidate) => (typeof candidate === "string"
        ? /^\/viewer\/([A-Za-z0-9._:-]+)\/?$/.exec(candidate.trim())?.[1]
        : undefined))
      .find(Boolean);
    const [artifact] = normalizeWorkflowArtifactRefs({
      ...record,
      document_id: record.document_id ?? viewerDocumentId,
      name: record.name ?? record.filename,
    }).filter((ref) => ref.document_id || ref.fs_path);
    if (artifact) {
      const identity = JSON.stringify([artifact.document_id || "", artifact.fs_path || ""]);
      if (!identities.has(identity)) {
        identities.add(identity);
        refs.push(artifact);
      }
    }
    for (const [key, nested] of Object.entries(record)) {
      if (!SENSITIVE_JSON_KEY.test(key)) visit(nested, depth + 1);
    }
  };
  visit(output, 0);
  return refs;
}

function routeTargets(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.map((item) => String(item || "").trim()).filter(Boolean);
}

/**
 * Every node a step can route to, matching the runner's declared routes:
 * `next`, `true_next`, `false_next`, switch `cases` / `default_next` /
 * `output_next`, and `config.error_next` for handled failures.
 */
export function workflowStepTargets(step: {
  next?: unknown;
  true_next?: unknown;
  false_next?: unknown;
  config?: Record<string, unknown> | null;
}): string[] {
  const cases = Array.isArray(step.config?.cases) ? step.config.cases : [];
  const outputNext = Array.isArray(step.config?.output_next) ? step.config.output_next : [];
  return [...new Set([
    ...routeTargets(step.next),
    ...routeTargets(step.true_next),
    ...routeTargets(step.false_next),
    ...cases.flatMap((item) => routeTargets((item as { next?: unknown } | null)?.next)),
    ...routeTargets(step.config?.default_next),
    ...outputNext.flatMap((value) => routeTargets(Array.isArray(value) ? value : [value])),
    ...routeTargets(step.config?.error_next),
  ])];
}

function workflowEditorTargets(step: WorkflowEditorStep): string[] {
  return workflowStepTargets(step);
}

function hasWorkflowOutput(value: unknown): boolean {
  return value !== undefined && value !== null && value !== "";
}

// Gates only route the run; their true/false is never the run's result.
const WORKFLOW_ROUTING_STEP_TYPES = new Set(["condition", "switch"]);

export function resolveWorkflowFinalResult(
  steps: WorkflowEditorStep[],
  run: {
    status?: string;
    current_step_id?: string | null;
    variables?: Record<string, unknown>;
    step_results?: Record<string, unknown>;
  },
): WorkflowFinalResult | null {
  const durableResult = run.variables?.__result;
  if (hasWorkflowOutput(durableResult)) {
    return { stepId: null, stepName: "Workflow", output: durableResult };
  }

  const results = run.step_results || {};
  const terminals = steps.filter(
    (step) => step.type === "end" || (
      workflowEditorTargets(step).length === 0 && !WORKFLOW_ROUTING_STEP_TYPES.has(step.type || "")
    ),
  );
  for (const terminal of terminals) {
    const terminalResult = asRecord(results[terminal.id]);
    const terminalReached = terminalResult !== null
      || (run.status === "completed" && run.current_step_id === terminal.id);
    if (!terminalReached) continue;
    const output = terminalResult?.output;
    if (hasWorkflowOutput(output) && output !== terminal.name) {
      return { stepId: terminal.id, stepName: terminal.name || "End", output };
    }
    const predecessor = [...steps].reverse().find((step) => {
      const result = asRecord(results[step.id]);
      return workflowEditorTargets(step).includes(terminal.id)
        && !WORKFLOW_ROUTING_STEP_TYPES.has(step.type || "")
        && result?.status === "completed"
        && result.skipped !== true
        && hasWorkflowOutput(result.output);
    });
    if (predecessor) {
      const result = asRecord(results[predecessor.id])!;
      return {
        stepId: predecessor.id,
        stepName: predecessor.name || predecessor.id,
        output: result.output,
      };
    }
  }

  for (const [stepId, rawResult] of Object.entries(results).reverse()) {
    const step = steps.find((candidate) => candidate.id === stepId);
    const result = asRecord(rawResult);
    if (
      !step
      || !result
      || ["trigger", "webhook", "end", "note"].includes(step.type || "")
      || WORKFLOW_ROUTING_STEP_TYPES.has(step.type || "")
    ) {
      continue;
    }
    if (result.status === "completed" && result.skipped !== true && hasWorkflowOutput(result.output)) {
      return { stepId, stepName: step.name || stepId, output: result.output };
    }
  }
  return null;
}

function isStageType(type: string | undefined): boolean {
  return !CONTROL_STEP_TYPES.has(String(type || "").toLowerCase());
}

/**
 * The percentage a Workflow run shows for its stages. It moves as stages finish
 * and reads 100 only once the run has completed. A run that failed, was cancelled,
 * or is still going shows the share it reached, below 100, so it never looks done.
 */
export function workflowRunProgressPercent(
  processed: number | null | undefined,
  total: number | null | undefined,
  status: string | null | undefined,
): number | null {
  if (
    typeof processed !== "number" || typeof total !== "number"
    || !Number.isFinite(processed) || !Number.isFinite(total) || total <= 0
  ) return null;
  if (String(status || "").toLowerCase() === "completed") return 100;
  return Math.min(99, Math.max(0, (processed / total) * 100));
}

export function workflowEditorRunProgress(
  steps: Pick<WorkflowEditorStep, "id" | "type">[],
  statusById: Record<string, string>,
  run?: { status?: string; processed_count?: number | null } | null,
): { processed: number; total: number } {
  const executableSteps = steps.filter((step) => isStageType(step.type));
  const observed = executableSteps.reduce(
    (count, step) => count + (PROCESSED_STATUSES.has(statusById[step.id] as WorkflowRunStatus) ? 1 : 0),
    0,
  );
  const persisted = Number.isInteger(run?.processed_count)
    ? Math.max(0, Number(run?.processed_count))
    : 0;
  const processed = Math.max(observed, persisted);
  // A completed run executed its whole path (branches skip the rest), so it
  // reads N/N. A failed or cancelled run stopped early; keep the graph size
  // as the denominator so "3/3" doesn't read as "all done".
  const completed = String(run?.status || "") === "completed";
  return {
    processed,
    total: completed ? processed : Math.max(executableSteps.length, processed),
  };
}

/**
 * A run's stage counts for its progress bar. The server counts the whole Flow,
 * so the total holds while the run moves; the nodes drawn are only the path
 * reached so far, and count only when the server sent no figure.
 */
export function workflowRunStageCounts(
  run: Pick<WorkflowRunView, "processedCount" | "totalCount">,
  nodes: WorkflowRunNode[],
): { processed: number; total: number } {
  const stages = workflowProgressNodes(nodes);
  return {
    processed: Number.isInteger(run.processedCount)
      ? Number(run.processedCount)
      : processedNodeCount(stages),
    total: Number.isInteger(run.totalCount)
      ? Number(run.totalCount)
      : progressNodeCount(stages),
  };
}

/** "Step N of M" among a run's stages, counted as the run page and App count them. */
export function workflowStagePosition(
  steps: Array<{ type?: string | null; status?: string | null }>,
): { current: number; total: number } {
  const stages = steps.filter((step) => isStageType(step.type || undefined));
  const processed = stages.filter((step) => PROCESSED_STATUSES.has(String(step.status || "") as WorkflowRunStatus)).length;
  return { current: Math.min(stages.length, processed + 1), total: stages.length };
}

export function processedNodeCount(nodes: WorkflowRunNode[]): number {
  return nodes.reduce(
    (count, node) => count + (PROCESSED_STATUSES.has(node.status) ? 1 : 0),
    0,
  );
}

export function workflowProgressNodes(nodes: WorkflowRunNode[]): WorkflowRunNode[] {
  return nodes.filter((node) => isStageType(node.type));
}

export function progressNodeCount(nodes: WorkflowRunNode[]): number {
  return nodes.length;
}

export function notReachedNodeCount(nodes: WorkflowRunNode[]): number {
  return nodes.reduce(
    (count, node) => count + (node.status === "pending" ? 1 : 0),
    0,
  );
}

export function workflowCurrentStepLabelKey(
  run: Pick<WorkflowRunView, "action" | "status" | "businessOutcome">,
): "current_step" | "retry_from" | "continue_from" {
  if (run.action?.kind !== "workflow_retry") return "current_step";
  if (workflowRunNeedsInput(run)) return "continue_from";
  return "retry_from";
}

export function workflowRunNeedsInput(
  run: Pick<WorkflowRunView, "status" | "businessOutcome" | "action">,
): boolean {
  return run.status === "completed"
    && (run.businessOutcome || run.action?.business_outcome) === "needs_input";
}

/**
 * Readable input guidance: what the run found missing, in its own words, then
 * the Flow's general instruction. The general line alone ("add what is missing
 * to the brief") left people resubmitting the same brief.
 */
export function workflowInputGuidance(action: WorkflowRunAction): string[] {
  const lines: string[] = [];
  for (const value of [action.observed_problem, action.required_change]) {
    for (const item of Array.isArray(value) ? value : [value]) {
      if (typeof item !== "string" || !item.trim()) continue;
      const line = action.editable_input_schema?.properties?.[item]?.title || item.trim();
      if (!lines.includes(line)) lines.push(line);
    }
  }
  if (lines.length) return lines;
  const fallback = action.prompt || action.description;
  return typeof fallback === "string" && fallback.trim() ? [fallback.trim()] : [];
}

export function workflowRunActionLabelKey(
  run: Pick<WorkflowRunView, "status" | "businessOutcome" | "action">,
  option: string,
): string | null {
  const normalized = option.toLowerCase().replace(/[-\s]+/g, "_");
  if (["retry", "retry_now"].includes(normalized)) {
    if (workflowRunNeedsInput(run)) return "component.workflow_run.action.continue";
    return "component.workflow_run.action.retry";
  }
  const keyByOption: Record<string, string> = {
    run: "component.workflow_run.action.run",
    pause: "component.workflow_run.action.pause",
    resume: "component.workflow_run.action.resume",
    accept: "component.workflow_run.action.accept",
    confirm: "component.workflow_run.action.confirm",
    done: "component.workflow_run.action.done",
    revise: "component.workflow_run.action.revise",
    cancel: "component.workflow_run.action.cancel",
  };
  return keyByOption[normalized] || null;
}

export function workflowRunInputFieldsLabelKey(
  action: Pick<WorkflowRunAction, "kind">,
): "component.workflow_run.retry_fields" | "component.workflow_run.continue_fields" {
  return action.kind === "workflow_retry"
    ? "component.workflow_run.retry_fields"
    : "component.workflow_run.continue_fields";
}

export function currentNodeIndex(
  run: Pick<WorkflowRunView, "nodes" | "currentNodeId" | "status"> & {
    action?: WorkflowRunAction | null;
  },
): number {
  const actionableNodeIds = [
    run.action?.retry_from_step_id,
    run.action?.step_id,
  ];
  for (const nodeId of actionableNodeIds) {
    if (!nodeId) continue;
    const index = run.nodes.findIndex((node) => node.id === nodeId);
    if (index >= 0) return index;
  }

  const explicit = run.currentNodeId
    ? run.nodes.findIndex((node) => node.id === run.currentNodeId)
    : -1;
  if (explicit >= 0) return explicit;

  const active = run.nodes.findIndex((node) => (
    node.status === "running" || node.status === "paused" || node.status === "failed"
  ));
  if (active >= 0) return active;

  if (isWorkflowRunActive(run)) {
    const pending = run.nodes.findIndex((node) => node.status === "pending");
    if (pending >= 0) return pending;
  }

  for (let index = run.nodes.length - 1; index >= 0; index -= 1) {
    if (PROCESSED_STATUSES.has(run.nodes[index].status)) return index;
  }
  return run.nodes.length ? 0 : -1;
}

export function interventionNodeIndex(
  run: Pick<WorkflowRunView, "nodes" | "currentNodeId" | "status">,
  action: Pick<WorkflowRunAction, "retry_from_step_id" | "step_id">,
): number {
  const declaredNodeIds = [
    action.retry_from_step_id,
    action.step_id,
    run.currentNodeId,
  ];
  for (const nodeId of declaredNodeIds) {
    if (!nodeId) continue;
    const index = run.nodes.findIndex((node) => node.id === nodeId);
    if (index >= 0) return index;
  }
  const failedIndex = run.nodes.findIndex((node) => node.status === "failed");
  return failedIndex >= 0 ? failedIndex : currentNodeIndex(run);
}

export function visibleLabelIndexes(
  nodes: WorkflowRunNode[],
  currentIndex: number,
  labelsFit: boolean,
): number[] {
  if (labelsFit) return nodes.map((_node, index) => index);
  if (!nodes.length) return [];
  if (currentIndex < 0) return [0];
  return [currentIndex - 1, currentIndex, currentIndex + 1]
    .filter((index) => index >= 0 && index < nodes.length);
}

function workflowRunTime(run: WorkflowHistoryRun): number {
  for (const value of [run.started_at, run.created_at, run.updated_at]) {
    const timestamp = Date.parse(value || "");
    if (Number.isFinite(timestamp)) return timestamp;
  }
  return 0;
}

export function sortWorkflowRunsNewestFirst<T extends WorkflowHistoryRun>(runs: T[]): T[] {
  return runs
    .map((run, index) => ({ run, index }))
    .sort((left, right) => workflowRunTime(right.run) - workflowRunTime(left.run) || left.index - right.index)
    .map(({ run }) => run);
}

function workflowFamilyRootId(
  run: WorkflowHistoryRun,
  runById: Map<string, WorkflowHistoryRun>,
): string {
  const declaredRoot = String(run.lineage_root_run_id || "").trim();
  if (declaredRoot) return declaredRoot;
  const visited = new Set<string>();
  let current = run;
  while (current.retry_of_run_id) {
    const parentId = String(current.retry_of_run_id).trim();
    if (!parentId || visited.has(parentId)) {
      return [...visited, current.id].sort()[0] || run.id;
    }
    visited.add(current.id);
    const parent = runById.get(parentId);
    if (!parent) return parentId;
    current = parent;
  }
  return current.id;
}

function workflowFamilyAttemptOrder(
  left: WorkflowHistoryRun,
  right: WorkflowHistoryRun,
): number {
  const attemptDifference = Number(left.attempt_number || 1) - Number(right.attempt_number || 1);
  return attemptDifference || workflowRunTime(left) - workflowRunTime(right);
}

function workflowFamilyProgress(run: WorkflowHistoryRun): {
  processedCount: number;
  totalCount: number;
} {
  if (
    Number.isFinite(run.processed_count)
    && Number.isFinite(run.total_count)
  ) {
    return {
      processedCount: Math.max(0, Number(run.processed_count)),
      totalCount: Math.max(0, Number(run.total_count)),
    };
  }
  const snapshotNodes = run.definition_snapshot?.nodes || [];
  const visibleNodeIds = new Set(snapshotNodes.filter((node) => (
    (node.chat_projection || node.config?.chat_projection) !== "hidden"
  )).map((node) => node.id));
  const hasVisibilityContract = snapshotNodes.length > 0;
  if (run.execution_trace?.length) {
    const latestTraceByNode = new Map<string, WorkflowTraceEntry>();
    for (const entry of [...run.execution_trace].sort((left, right) => (
      Number(left.sequence || 0) - Number(right.sequence || 0)
    ))) {
      if (!hasVisibilityContract || visibleNodeIds.has(entry.node_id)) {
        latestTraceByNode.set(entry.node_id, entry);
      }
    }
    const reachedStatuses = new Map<string, string>(
      [...latestTraceByNode].map(([nodeId, entry]) => [nodeId, String(entry.status || "")]),
    );
    const results = run.step_results || {};
    for (const [nodeId, rawResult] of Object.entries(results)) {
      if (hasVisibilityContract && !visibleNodeIds.has(nodeId)) continue;
      const result = asRecord(rawResult);
      if (!result) continue;
      reachedStatuses.set(
        nodeId,
        result.skipped === true
          ? "skipped"
          : String(result.status || (result.error ? "failed" : "completed")),
      );
    }
    const currentNodeId = String(run.current_step_id || "").trim();
    if (
      currentNodeId
      && (!hasVisibilityContract || visibleNodeIds.has(currentNodeId))
      && !reachedStatuses.has(currentNodeId)
    ) {
      reachedStatuses.set(currentNodeId, run.status);
    }
    return {
      processedCount: [...reachedStatuses.values()].filter((status) => (
        ["completed", "failed", "cancelled", "skipped"].includes(
          status.toLowerCase(),
        )
      )).length,
      totalCount: reachedStatuses.size,
    };
  }
  const results = run.step_results || {};
  const nodeIds = Object.keys(results).filter((nodeId) => (
    !hasVisibilityContract || visibleNodeIds.has(nodeId)
  ));
  const processedCount = nodeIds.reduce((count, nodeId) => {
    const result = results[nodeId];
    if (!result || typeof result !== "object" || Array.isArray(result)) return count;
    const record = result as Record<string, unknown>;
    const status = String(record.status || "").toLowerCase();
    return (record.skipped === true || ["completed", "failed", "cancelled", "skipped"].includes(status))
      ? count + 1
      : count;
  }, 0);
  return { processedCount, totalCount: nodeIds.length };
}

function workflowFamilyArtifacts(runs: WorkflowHistoryRun[]): WorkflowArtifactRef[] {
  const refs = normalizeWorkflowArtifactRefs(runs.flatMap((run) => (
    [
      ...(run.execution_trace || []).flatMap((entry) => entry.artifact_refs || []),
      ...Object.values(run.step_results || {}).flatMap((result) => {
        const record = asRecord(result);
        if (record?.artifact_refs == null) return [];
        return Array.isArray(record.artifact_refs)
          ? record.artifact_refs
          : [record.artifact_refs];
      }),
    ]
  )));
  const seen = new Set<string>();
  return refs.filter((ref) => {
    const identity = JSON.stringify([
      ref.document_id || "",
      ref.fs_path || "",
      ref.name || "",
      ref.mime_type || "",
    ]);
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}

function workflowFamilyBlocker(run: WorkflowHistoryRun): unknown {
  return run.intervention?.observed_problem
    ?? run.intervention?.required_change
    ?? run.history_blocker
    ?? run.error;
}

function workflowFamilyBusinessOutcome(run: WorkflowHistoryRun): string {
  if (run.business_outcome) return String(run.business_outcome);
  const project = asRecord(run.variables?.project);
  const state = asRecord(project?.state);
  return typeof state?.business_outcome === "string" ? state.business_outcome : "";
}

export function groupWorkflowRunFamilies(
  runs: WorkflowHistoryRun[],
): WorkflowHistoryFamily[] {
  const uniqueRuns = [...new Map(runs.map((run) => [run.id, run])).values()];
  const runById = new Map(uniqueRuns.map((run) => [run.id, run]));
  const grouped = new Map<string, WorkflowHistoryRun[]>();
  for (const run of uniqueRuns) {
    const rootId = workflowFamilyRootId(run, runById);
    grouped.set(rootId, [...(grouped.get(rootId) || []), run]);
  }

  const families = [...grouped.entries()].map(([id, familyRuns]) => {
    const orderedRuns = [...familyRuns].sort(workflowFamilyAttemptOrder);
    const latestRun = orderedRuns[orderedRuns.length - 1];
    const startedRun = [...orderedRuns].sort((left, right) => (
      workflowRunTime(left) - workflowRunTime(right)
    ))[0];
    const completedRuns = orderedRuns.filter((run) => Boolean(run.completed_at));
    const completedRun = completedRuns.sort((left, right) => (
      Date.parse(right.completed_at || "") - Date.parse(left.completed_at || "")
    ))[0];
    const startedAt = startedRun?.started_at || startedRun?.created_at;
    const completedAt = completedRun?.completed_at;
    const startedTime = Date.parse(startedAt || "");
    const completedTime = Date.parse(completedAt || "");
    const updatedTime = Date.parse(latestRun.updated_at || "");
    const endedTime = Number.isFinite(completedTime) ? completedTime : updatedTime;
    const progress = workflowFamilyProgress(latestRun);
    const artifactRefs = workflowFamilyArtifacts(orderedRuns);
    const compactArtifactCount = typeof latestRun.artifact_count === "number"
      ? latestRun.artifact_count
      : null;
    return {
      id,
      runs: orderedRuns,
      latestRun,
      attemptCount: orderedRuns.length,
      status: latestRun.status,
      businessOutcome: workflowFamilyBusinessOutcome(latestRun),
      startedAt,
      completedAt,
      durationMs: Number.isFinite(startedTime) && Number.isFinite(endedTime)
        ? Math.max(0, endedTime - startedTime)
        : null,
      ...progress,
      artifactRefs,
      artifactCount: artifactRefs.length > 0
        ? artifactRefs.length
        : compactArtifactCount !== null
          ? Math.max(0, compactArtifactCount)
          : null,
      blocker: workflowFamilyBlocker(latestRun),
    } satisfies WorkflowHistoryFamily;
  });
  return families.sort((left, right) => (
    workflowRunTime(right.latestRun) - workflowRunTime(left.latestRun)
  ));
}

export function workflowRunDurationMs(
  run: WorkflowHistoryRun,
  nowMs = Date.now(),
): number | null {
  const startedAt = Date.parse(run.started_at || "");
  if (!Number.isFinite(startedAt)) return null;
  const completedAt = Date.parse(run.completed_at || "");
  const updatedAt = Date.parse(run.updated_at || "");
  const active = run.status === "pending" || run.status === "running";
  const endedAt = Number.isFinite(completedAt)
    ? completedAt
    : active
      ? nowMs
      : Number.isFinite(updatedAt)
        ? updatedAt
        : NaN;
  if (!Number.isFinite(endedAt) || endedAt < startedAt) return null;
  return endedAt - startedAt;
}

export function formatWorkflowDuration(durationMs: number | null): string {
  if (durationMs == null || !Number.isFinite(durationMs) || durationMs < 0) return "--";
  if (durationMs < 1_000) return `${Math.round(durationMs)} ms`;
  if (durationMs < 60_000) return `${(durationMs / 1_000).toFixed(durationMs < 10_000 ? 1 : 0)} s`;
  if (durationMs < 3_600_000) return `${Math.floor(durationMs / 60_000)}m ${Math.floor((durationMs % 60_000) / 1_000)}s`;
  return `${Math.floor(durationMs / 3_600_000)}h ${Math.floor((durationMs % 3_600_000) / 60_000)}m`;
}

function orderedHistoryNodes(nodes: WorkflowHistoryNode[]): WorkflowHistoryNode[] {
  return nodes
    .map((node, index) => ({ node, index }))
    .filter(({ node }) => Boolean(node?.id))
    .sort((left, right) => (
      (Number.isFinite(left.node.order) ? Number(left.node.order) : left.index)
      - (Number.isFinite(right.node.order) ? Number(right.node.order) : right.index)
    ))
    .map(({ node }) => node);
}

function historyChildRunIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
    .slice(0, 64);
}

export function workflowRunIsLegacy(run: WorkflowHistoryRun): boolean {
  const snapshotNodes = Array.isArray(run.definition_snapshot?.nodes)
    ? run.definition_snapshot.nodes
    : [];
  return snapshotNodes.length === 0;
}

export function workflowRunHasImmutableListMetadata(run: WorkflowHistoryRun): boolean {
  return typeof run.workflow_name === "string" && Boolean(run.workflow_name.trim());
}

export function buildWorkflowRunTimeline(
  run: WorkflowHistoryRun,
  currentDefinitionNodes: WorkflowHistoryNode[] = [],
): WorkflowRunTimelineEntry[] {
  const snapshotNodes = orderedHistoryNodes(
    Array.isArray(run.definition_snapshot?.nodes) ? run.definition_snapshot.nodes : [],
  );
  const currentNodes = orderedHistoryNodes(currentDefinitionNodes);
  const identityNodes = snapshotNodes.length ? snapshotNodes : currentNodes;
  const nodeById = new Map(identityNodes.map((node) => [node.id, node]));
  const snapshotNodeById = new Map(snapshotNodes.map((node) => [node.id, node]));
  const currentNodeById = new Map(currentNodes.map((node) => [node.id, node]));
  const legacy = snapshotNodes.length === 0;
  const trace = Array.isArray(run.execution_trace)
    ? run.execution_trace.flatMap((item, index) => {
        const record = asRecord(item);
        const nodeId = typeof record?.node_id === "string" ? record.node_id : "";
        if (!record || !nodeId) return [];
        const frozenNode = snapshotNodeById.get(nodeId);
        const currentNode = currentNodeById.get(nodeId);
        const traceNodeName = typeof record.node_name === "string" ? record.node_name : "";
        const traceNodeType = typeof record.node_type === "string" ? record.node_type : "";
        const rawSequence = Number(record.sequence);
        return [{
          sequence: Number.isFinite(rawSequence) ? rawSequence : index + 1,
          attempt_number: Number.isFinite(Number(record.attempt_number))
            ? Number(record.attempt_number)
            : run.attempt_number,
          node_id: nodeId,
          nodeId,
          nodeName: frozenNode?.name || traceNodeName || currentNode?.name || nodeId,
          nodeType: frozenNode?.type || traceNodeType || currentNode?.type,
          status: typeof record.status === "string" ? record.status : "unknown",
          started_at: typeof record.started_at === "string" ? record.started_at : undefined,
          completed_at: typeof record.completed_at === "string" ? record.completed_at : undefined,
          duration_ms: Number.isFinite(Number(record.duration_ms)) ? Number(record.duration_ms) : undefined,
          input_summary: record.input_summary,
          output_summary: record.output_summary,
          error: record.error,
          artifact_refs: normalizeWorkflowArtifactRefs(record.artifact_refs),
          child_run_ids: historyChildRunIds(record.child_run_ids),
          legacy,
          sourceIndex: index,
        }];
      })
    : [];
  if (trace.length) {
    return trace
      .sort((left, right) => Number(left.sequence) - Number(right.sequence) || left.sourceIndex - right.sourceIndex)
      .map(({ sourceIndex: _sourceIndex, ...entry }) => entry);
  }

  const stepResults = asRecord(run.step_results) || {};
  const orderedIds = [
    ...identityNodes.map((node) => node.id).filter((id) => Object.prototype.hasOwnProperty.call(stepResults, id)),
    ...Object.keys(stepResults).filter((id) => !nodeById.has(id)),
  ];
  return orderedIds.map((nodeId, index) => {
    const rawResult = stepResults[nodeId];
    const result = asRecord(rawResult);
    const node = nodeById.get(nodeId);
    const status = typeof result?.status === "string"
      ? result.status
      : result?.skipped
        ? "skipped"
        : result?.error
          ? "failed"
          : "completed";
    return {
      sequence: index + 1,
      attempt_number: run.attempt_number || 1,
      node_id: nodeId,
      nodeId,
      nodeName: node?.name || nodeId,
      nodeType: node?.type,
      status,
      started_at: typeof result?.started_at === "string" ? result.started_at : undefined,
      completed_at: typeof result?.completed_at === "string" ? result.completed_at : undefined,
      duration_ms: Number.isFinite(Number(result?.duration_ms)) ? Number(result?.duration_ms) : undefined,
      input_summary: result?.inputs ?? result?.input,
      output_summary: result?.output ?? result?.result ?? (result?.error ? undefined : rawResult),
      error: result?.error,
      artifact_refs: normalizeWorkflowArtifactRefs(result?.artifact_refs),
      child_run_ids: historyChildRunIds(result?.child_run_ids),
      legacy: true,
    };
  });
}

export function buildWorkflowSnapshotNodes(
  run: WorkflowHistoryRun,
  currentDefinitionNodes: WorkflowHistoryNode[] = [],
): WorkflowSnapshotNode[] {
  const frozenNodes = orderedHistoryNodes(
    Array.isArray(run.definition_snapshot?.nodes) ? run.definition_snapshot.nodes : [],
  );
  const legacy = frozenNodes.length === 0;
  const nodes = frozenNodes.length ? frozenNodes : orderedHistoryNodes(currentDefinitionNodes);
  const latestByNodeId = new Map<string, WorkflowRunTimelineEntry>();
  for (const entry of buildWorkflowRunTimeline(run, currentDefinitionNodes)) {
    latestByNodeId.set(entry.nodeId, entry);
  }

  return nodes.map((node, index) => {
    const latest = latestByNodeId.get(node.id);
    const currentStatus = run.current_step_id === node.id
      && ["pending", "running", "paused", "failed", "cancelled"].includes(run.status)
      ? run.status
      : "";
    const status = latest?.status
      || currentStatus
      || (run.status === "completed" ? "skipped" : "pending");
    return {
      nodeId: node.id,
      nodeName: node.name || node.id,
      nodeType: node.type,
      order: Number.isFinite(node.order) ? Number(node.order) : index,
      status,
      targets: Array.isArray(node.targets)
        ? node.targets.filter((target): target is string => typeof target === "string" && Boolean(target.trim()))
        : [],
      legacy,
    };
  });
}

export function canRetryWithoutCorrection(run: WorkflowHistoryRun): boolean {
  return run.capabilities?.can_control === true
    && !run.superseded
    && run.status === "failed"
    && typeof run.retry_from_step_id === "string"
    && Boolean(run.retry_from_step_id.trim());
}

export function canShowDirectWorkflowRetry(
  run: WorkflowHistoryRun,
  hasVisibleIntervention: boolean,
): boolean {
  return !hasVisibleIntervention && canRetryWithoutCorrection(run);
}

/* File patch operations in an approval, as a person reads them. */

export interface WorkflowPatchSummary {
  /** An i18n key for a known verb, or the operation itself in lowercase words. */
  verbKey?: string;
  verb?: string;
  target: string;
  value?: unknown;
}

const JSON_PATCH_VERBS: Record<string, string> = {
  "json.add": "component.workflow_review.patch_add",
  "json.replace": "component.workflow_review.patch_change",
  "json.remove": "component.workflow_review.patch_remove",
};
// Where an operation acts; everything else it carries is what it writes.
const PATCH_TARGET_FIELDS = ["pointer", "path_in_file", "sheet", "cell", "range", "row", "index", "slide", "slide_index", "paragraph_index", "table_index", "name"];
const PATCH_IGNORED_FIELDS = new Set(["op", "operation", "old_text", "new_text"]);

/** "/bounded_recovery/retry_plan" → "bounded_recovery › retry_plan". */
function jsonPointerLabel(pointer: string): string {
  return pointer.split("/").filter(Boolean)
    .map((part) => part.replace(/~1/g, "/").replace(/~0/g, "~"))
    .join(" › ") || "/";
}

/** A file patch operation as a person reads it; text edits are shown as a diff instead. */
export function workflowPatchSummary(operation: Record<string, any>): WorkflowPatchSummary | null {
  const op = String(operation.op || operation.operation || "").trim();
  if (!op || typeof operation.old_text === "string" || typeof operation.new_text === "string") return null;
  const targets = PATCH_TARGET_FIELDS.flatMap((field) => {
    const where = operation[field];
    // A target is a name or a position; anything structured belongs in the value shown below.
    if (typeof where !== "string" && typeof where !== "number") return [];
    const text = `${where}`.trim();
    if (!text) return [];
    return [field === "pointer" ? jsonPointerLabel(text) : text];
  });
  const rest = Object.fromEntries(Object.entries(operation).filter(([key]) => (
    !PATCH_IGNORED_FIELDS.has(key) && !PATCH_TARGET_FIELDS.includes(key)
  )));
  const value = "value" in rest && Object.keys(rest).length === 1 ? rest.value : Object.keys(rest).length ? rest : undefined;
  const verbKey = JSON_PATCH_VERBS[op];
  return {
    ...(verbKey ? { verbKey } : { verb: op.replace(/[._]/g, " ") }),
    target: targets.join(" · "),
    ...(value !== undefined ? { value } : {}),
  };
}
