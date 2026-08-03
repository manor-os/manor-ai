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

export interface WorkflowRunStatusPresentation {
  labelKey: WorkflowRunStatusLabelKey;
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
  values?: Record<string, unknown>;
  step_id?: string;
  retry_from_step_id?: string;
  observed_problem?: unknown;
  required_change?: unknown;
  preserved_receipts?: unknown[];
  retry_segment_ids?: string[];
  [key: string]: unknown;
}

export interface WorkflowRunView {
  id: string;
  title: string;
  status: WorkflowRunStatus;
  nodes: WorkflowRunNode[];
  workflowId?: string;
  currentNodeId?: string | null;
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
  processed_count?: number;
  total_count?: number;
  artifact_count?: number | null;
  history_blocker?: unknown;
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
]);
const PROCESSED_STATUSES = new Set<WorkflowRunStatus>([
  "completed",
  "failed",
  "cancelled",
]);
const PROGRESS_EXCLUDED_NODE_TYPES = new Set(["end"]);
const EDITABLE_SCHEMA_TYPES = new Set(["string", "number", "integer", "boolean", "array", "object"]);
const EDITABLE_SCHEMA_COMMON_KEYS = [
  "$schema",
  "type",
  "title",
  "description",
  "enum",
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
    && !["textarea", "line_list"].includes(String(ui.control))
  ) return false;
  if (ui.control === "line_list" && schemaType !== "array") return false;
  if (ui.control === "textarea" && !["string", "array"].includes(schemaType)) return false;
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

export function formatWorkflowError(value: unknown, truncationText: string): string {
  if (value == null || value === "") return "";
  if (value instanceof Error) return boundedUtf8(redactSensitiveText(value.message || value.name), truncationText);
  if (typeof value === "string") return boundedUtf8(redactSensitiveText(value.trim()), truncationText);

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
      message,
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

export function workflowRunStatusPresentation(
  run: Pick<WorkflowRunView, "status" | "businessOutcome">,
): WorkflowRunStatusPresentation {
  const businessOutcome = String(run.businessOutcome || "").trim().toLowerCase();
  if (
    run.status === "completed"
    && ACTIONABLE_COMPLETED_OUTCOMES.has(businessOutcome as WorkflowRunStatusLabelKey)
  ) {
    return {
      labelKey: businessOutcome as WorkflowRunStatusLabelKey,
      iconStatus: "paused",
      motion: "waiting",
    };
  }
  return {
    labelKey: run.status,
    iconStatus: run.status,
    motion: ACTIVE_STATUSES.has(run.status)
      ? "running"
      : run.status === "paused" ? "waiting" : "static",
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

export function processedNodeCount(nodes: WorkflowRunNode[]): number {
  return nodes.reduce(
    (count, node) => count + (PROCESSED_STATUSES.has(node.status) ? 1 : 0),
    0,
  );
}

export function workflowProgressNodes(nodes: WorkflowRunNode[]): WorkflowRunNode[] {
  return nodes.filter((node) => (
    !PROGRESS_EXCLUDED_NODE_TYPES.has(String(node.type || "").trim().toLowerCase())
  ));
}

export function progressNodeCount(nodes: WorkflowRunNode[]): number {
  return nodes.reduce(
    (count, node) => count + (node.status === "skipped" ? 0 : 1),
    0,
  );
}

export function notReachedNodeCount(nodes: WorkflowRunNode[]): number {
  return nodes.reduce(
    (count, node) => count + (node.status === "pending" ? 1 : 0),
    0,
  );
}

export function workflowCurrentStepLabelKey(
  run: Pick<WorkflowRunView, "action" | "status">,
): "current_step" | "retry_from" | "continue_from" {
  if (run.action?.kind !== "workflow_retry") return "current_step";
  return "retry_from";
}

export function workflowRunActionLabelKey(
  run: Pick<WorkflowRunView, "status">,
  option: string,
): string | null {
  const normalized = option.toLowerCase().replace(/[-\s]+/g, "_");
  if (["retry", "retry_now"].includes(normalized)) {
    return "component.workflow_run.action.retry";
  }
  const keyByOption: Record<string, string> = {
    run: "component.workflow_run.action.run",
    resume: "component.workflow_run.action.resume",
    accept: "component.workflow_run.action.accept",
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
  const snapshotNodes = run.definition_snapshot?.nodes || [];
  const visibleNodes = snapshotNodes.filter((node) => (
    String(node.type || "").toLowerCase() !== "end"
    && (node.chat_projection || node.config?.chat_projection) !== "hidden"
  ));
  const results = run.step_results || {};
  if (
    visibleNodes.length === 0
    && Number.isFinite(run.processed_count)
    && Number.isFinite(run.total_count)
  ) {
    return {
      processedCount: Math.max(0, Number(run.processed_count)),
      totalCount: Math.max(0, Number(run.total_count)),
    };
  }
  if (visibleNodes.length === 0 && run.execution_trace?.length) {
    const latestTraceByNode = new Map<string, WorkflowTraceEntry>();
    for (const entry of [...run.execution_trace].sort((left, right) => (
      Number(left.sequence || 0) - Number(right.sequence || 0)
    ))) {
      if (String(entry.node_type || "").toLowerCase() !== "end") {
        latestTraceByNode.set(entry.node_id, entry);
      }
    }
    const traceEntries = [...latestTraceByNode.values()];
    return {
      processedCount: traceEntries.filter((entry) => (
        ["completed", "failed", "cancelled"].includes(String(entry.status || "").toLowerCase())
      )).length,
      totalCount: traceEntries.length,
    };
  }
  const nodeIds = visibleNodes.length > 0
    ? visibleNodes.map((node) => node.id)
    : Object.keys(results);
  const processedCount = nodeIds.reduce((count, nodeId) => {
    const result = results[nodeId];
    if (!result || typeof result !== "object" || Array.isArray(result)) return count;
    const record = result as Record<string, unknown>;
    const status = String(record.status || "").toLowerCase();
    return !record.skipped && ["completed", "failed", "cancelled"].includes(status)
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

function hasCorrectionRequirement(value: unknown, depth = 0): boolean {
  if (depth > 4 || value == null) return false;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return false;
    try {
      return hasCorrectionRequirement(JSON.parse(trimmed), depth + 1);
    } catch {
      return false;
    }
  }
  if (Array.isArray(value)) return value.some((item) => hasCorrectionRequirement(item, depth + 1));
  const record = asRecord(value);
  if (!record) return false;
  if (record.required_change != null && record.required_change !== "") return true;
  const schema = asRecord(record.editable_input_schema);
  const properties = asRecord(schema?.properties);
  if (properties && Object.keys(properties).length > 0) return true;
  return Object.entries(record).some(([key, item]) => (
    !SENSITIVE_JSON_KEY.test(key) && hasCorrectionRequirement(item, depth + 1)
  ));
}

export function canRetryWithoutCorrection(run: WorkflowHistoryRun): boolean {
  return run.capabilities?.can_control === true
    && run.status === "failed"
    && Boolean(run.retry_from_step_id || run.current_step_id)
    && !hasCorrectionRequirement(run.error);
}
