export interface WorkflowInputSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  format?: string;
  pattern?: string;
  minLength?: number;
  maxLength?: number;
  enum?: unknown[];
  "x-enum-labels"?: string[];
  /** Suggested values for a free-text field: offered to pick, never required. */
  examples?: unknown[];
  /** Short names for ``examples``, in the same order. */
  "x-example-labels"?: string[];
  /** A picture of each of ``examples``, in the same order (a site path or https URL). */
  "x-example-images"?: string[];
  /** The group each of ``examples`` is browsed under (a song style's genre), in the same order. */
  "x-example-groups"?: string[];
  /** A sample to hear of each of ``examples`` (an audio URL), in the same order. */
  "x-example-samples"?: string[];
  /** One line on each ``enum`` option, in the same order. */
  "x-enum-descriptions"?: string[];
  /** A picture of each ``enum`` option, in the same order: the options are then picked as pictures. */
  "x-enum-images"?: string[];
  /** A rule's own words for what is missing, shown on the inputs it names. */
  "x-message"?: string;
  /** Per ``enum`` option, the other inputs (same level) it sets when picked: a template. */
  "x-enum-presets"?: Record<string, Record<string, unknown>>;
  /** Per ``examples`` entry, in the same order, the other inputs it sets when picked. */
  "x-example-presets"?: Array<Record<string, unknown> | null>;
  /**
   * Per value of a sibling field (e.g. the kind of deck): the options that suit
   * it, shown first, and pictures of options made for it.
   */
  "x-enum-cases"?: {
    field: string;
    cases: Record<string, { recommended?: string[]; images?: Record<string, string> }>;
  };
  const?: unknown;
  default?: unknown;
  properties?: Record<string, WorkflowInputSchema>;
  additionalProperties?: boolean | WorkflowInputSchema;
  required?: string[];
  /** Constraints over the same object, as the server's jsonschema reads them. */
  allOf?: WorkflowInputSchema[];
  anyOf?: WorkflowInputSchema[];
  if?: WorkflowInputSchema;
  then?: WorkflowInputSchema;
  else?: WorkflowInputSchema;
  items?: WorkflowInputSchema;
  minItems?: number;
  maxItems?: number;
  uniqueItems?: boolean;
  minimum?: number;
  maximum?: number;
  exclusiveMinimum?: number;
  exclusiveMaximum?: number;
  "x-workflow-type"?: "json";
  "x-ui"?: {
    hidden?: boolean;
    /**
     * "range": a slider over a number's minimum..maximum; "segmented": an
     * ``enum`` as a row of buttons; "aspect": an ``enum`` of ratios ("16:9")
     * as buttons that draw each frame; "audio_clip": an object of
     * ``start_seconds``/``end_seconds`` picked as a window on the waveform of
     * the audio chosen in ``audio_field``.
     */
    control?: "textarea" | "line_list" | "workflow_project_select" | "documents" | "knowledge_file" | "timezone" | "record_select"
      | "range" | "segmented" | "aspect" | "audio_clip";
    /** "audio_clip": the sibling input (``documents`` or ``knowledge_file``) that holds the audio. */
    audio_field?: string;
    /** "audio_clip": the window's length when nothing is picked yet, in seconds. */
    clip_seconds?: number;
    /** "audio_clip": the shortest and longest part that may be picked, in seconds (5 and 60 when unset). */
    min_seconds?: number;
    max_seconds?: number;
    /** "audio_clip": also offer letting the AI pick the part; the value then holds ``auto: true``. */
    allow_auto?: boolean;
    /** A yes/no choice that stands for a service ("youtube"): its logo is shown beside the label. */
    provider?: string;
    /** "range": the slider's step. */
    step?: number;
    /** "range": the unit read after the value ("s"). */
    unit?: string;
    /**
     * Inputs that share a page are asked together on one page of an App
     * entry, a settings page, instead of one input per page.
     */
    page?: string;
    file_kind?: "audio" | "image";
    /** "documents": the only media kinds the field takes, so pickers list just those. */
    accept?: Array<"audio" | "video" | "image" | "document">;
    rows?: number;
    collapsible?: boolean;
    collapsed?: boolean;
    order?: string[];
    /** "list": the fields are asked one by one, numbered, in a single column. */
    layout?: "list";
    /** An example answer shown inside the empty box. */
    placeholder?: string;
    /** Where a pre-filled value was found, shown under the label. */
    source?: string;
    project_type?: string;
    allowed_stages?: string[];
    timezone_field?: string;
    record_source?: WorkflowRecordSource;
    /** Recommended answers the user can accept with one click (text fields). */
    suggestions?: string[];
  };
}

/** Workspace Ledger records offered by a `record_select` field (see workflow_record_source.py). */
export interface WorkflowRecordSource {
  ledger?: string;
  filter?: Record<string, unknown>;
  value_field?: string;
  label_field?: string;
  secondary_field?: string;
}

export interface WorkflowSchemaValidationMessages {
  required: string;
  invalidJson: string;
  invalidUrl: string;
  invalidFormat: string;
  invalidNumber: string;
  minLength: (minimum: number) => string;
  maxLength: (maximum: number) => string;
  minItems: (minimum: number) => string;
  maxItems: (maximum: number) => string;
  uniqueItems: string;
  minimum: (minimum: number) => string;
  maximum: (maximum: number) => string;
  exclusiveMinimum: (minimum: number) => string;
  exclusiveMaximum: (maximum: number) => string;
  /** Names the fields one of which must be filled (an ``anyOf`` of required fields). */
  anyOfRequired: (fields: string[]) => string;
}

export interface WorkflowSchemaFieldIdSet {
  fieldId: string;
  helpId: string;
  errorId: string;
}

/** The blanks a preset leaves for the person to fill, e.g. "[audience]". */
export function presetPlaceholderTokens(template: unknown): string[] {
  if (typeof template !== "string") return [];
  return [...new Set(template.match(/\[[^[\]\n]{1,60}\]/g) || [])];
}

/** Every still-unfilled preset blank in the text, in reading order. */
function placeholderRanges(value: string, tokens: string[]): Array<[number, number]> {
  const ranges: Array<[number, number]> = [];
  for (const token of tokens) {
    for (let at = value.indexOf(token); at >= 0; at = value.indexOf(token, at + token.length)) {
      ranges.push([at, at + token.length]);
    }
  }
  return ranges.sort((a, b) => a[0] - b[0]);
}

/** The preset blank the caret sits in, so clicking it selects the whole blank. */
export function presetPlaceholderAt(value: string, caret: number, tokens: string[]): [number, number] | null {
  return placeholderRanges(value, tokens).find(([start, end]) => caret > start && caret < end) || null;
}

/**
 * The next (or previous) preset blank from a position, for Tab. None past the
 * last one, so Tab can still leave the field.
 */
export function nextPresetPlaceholder(
  value: string, from: number, tokens: string[], backwards = false,
): [number, number] | null {
  const ranges = placeholderRanges(value, tokens);
  if (backwards) return [...ranges].reverse().find(([, end]) => end <= from) || null;
  return ranges.find(([start]) => start >= from) || null;
}

/** People type www.example.com; a URL field means https://www.example.com (same rule as the API). */
export function withUrlScheme(value: string): string {
  const text = value.trim();
  const host = text.replace(/^\/\//, "");
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(text)
    || !/^(?:[a-z0-9-]+\.)+[a-z]{2,}(?::\d+)?(?:[/?#]\S*)?$/i.test(host)) return text;
  return `https://${host}`;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function safeIdPart(value: string): string {
  return value.replace(/[^a-zA-Z0-9_-]+/g, "-").replace(/^-+|-+$/g, "") || "field";
}

export function workflowSchemaFieldIds(
  idNamespace: string,
  rootKey: string,
  path: string[],
): WorkflowSchemaFieldIdSet {
  const base = [idNamespace, rootKey, ...path].map(safeIdPart).join("-");
  return {
    fieldId: `${base}-input`,
    helpId: `${base}-help`,
    errorId: `${base}-error`,
  };
}

export function workflowSchemaDescribedBy(
  ids: WorkflowSchemaFieldIdSet,
  hasHelp: boolean,
  hasError: boolean,
): string | undefined {
  const describedBy = [hasHelp ? ids.helpId : "", hasError ? ids.errorId : ""]
    .filter(Boolean)
    .join(" ");
  return describedBy || undefined;
}

export function workflowSchemaType(schema?: WorkflowInputSchema): string {
  const declared = schema?.type;
  if (Array.isArray(declared)) return declared.find((item) => item !== "null") || "string";
  if (declared) return declared;
  if (schema?.properties) return "object";
  return "string";
}

/** Several file fields in one form read as compact rows instead of stacked drop areas. */
export function workflowSchemaCompactDocuments(schema: WorkflowInputSchema): boolean {
  return Object.values(schema.properties || {}).filter((child) => (
    child["x-ui"]?.control === "documents" && !child["x-ui"]?.hidden
  )).length >= 2;
}

export function workflowSchemaEntries(
  schema: WorkflowInputSchema,
): [string, WorkflowInputSchema][] {
  const entries = Object.entries(schema.properties || {});
  const declaredOrder = schema["x-ui"]?.order || [];
  if (declaredOrder.length === 0) return entries;
  const order = new Map(declaredOrder.map((key, index) => [key, index]));
  const fallback = new Map(entries.map(([key], index) => [key, declaredOrder.length + index]));
  return [...entries].sort(
    ([left], [right]) => (order.get(left) ?? fallback.get(left) ?? 0)
      - (order.get(right) ?? fallback.get(right) ?? 0),
  );
}

export function workflowSchemaDraft(
  schema: WorkflowInputSchema,
  supplied: unknown,
  timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
): unknown {
  const source = supplied ?? schema.default ?? schema.const;
  if (schema["x-workflow-type"] === "json") {
    if (typeof source === "string") return source;
    try { return JSON.stringify(source ?? {}, null, 2); } catch { return "{}"; }
  }
  const schemaType = workflowSchemaType(schema);
  if (schemaType === "object") {
    const sourceRecord = asRecord(source) || {};
    const declared = Object.fromEntries(Object.entries(schema.properties || {}).map(([key, child]) => [
      key,
      workflowSchemaDraft(child, sourceRecord[key], timeZone),
    ]));
    return schema.additionalProperties === false
      ? declared
      : { ...sourceRecord, ...declared };
  }
  if (schemaType === "array") {
    // The documents control owns structured document references rather than a
    // JSON text draft. Keep prefilled references as an array so the picker can
    // render and submit them without a lossy stringify/parse round trip.
    if (schema["x-ui"]?.control === "documents" && Array.isArray(source)) {
      // A prefill may name one file several times (e.g. search excerpts).
      const seen = new Set<string>();
      return source.filter((item) => {
        const id = String(asRecord(item)?.document_id ?? "");
        if (!id) return true;
        if (seen.has(id)) return false;
        seen.add(id);
        return true;
      });
    }
    if (workflowSchemaType(schema.items) === "string") {
      return Array.isArray(source)
        ? source.map((item) => String(item || "").trim()).filter(Boolean).join("\n")
        : String(source || "");
    }
    if (typeof source === "string") return source;
    try { return JSON.stringify(source ?? [], null, 2); } catch { return "[]"; }
  }
  if (schemaType === "boolean") return Boolean(source);
  if (schema["x-ui"]?.control === "timezone" && source == null) return timeZone;
  return source === undefined || source === null ? "" : String(source);
}

export function workflowSchemaTimeZones(selected: string): string[] {
  const intl = Intl as typeof Intl & { supportedValuesOf?: (key: string) => string[] };
  return [...new Set([selected, "UTC", ...(intl.supportedValuesOf?.("timeZone") || [])])].filter(Boolean);
}

/** Display an instant in the selected zone, without changing its stored offset. */
export function workflowSchemaDateTimeValue(value: string, timeZone: string): string {
  if (!value || !/(?:Z|[+-]\d{2}:\d{2})$/i.test(value)) return value;
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return value;
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone, hourCycle: "h23", year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit",
    }).formatToParts(date);
    const fields = Object.fromEntries(parts.map((part) => [part.type, part.value]));
    return `${fields.year}-${fields.month}-${fields.day}T${fields.hour}:${fields.minute}`;
  } catch { return value; }
}

/** Resolve a calendar selection to an instant. A DST gap remains invalid input. */
export function workflowSchemaDateTimeIso(value: string, timeZone: string): string {
  if (!value) return "";
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
  if (!match) return value;
  const [, year, month, day, hour, minute] = match.map(Number);
  const wallUtc = Date.UTC(year, month - 1, day, hour, minute);
  if (new Date(wallUtc).toISOString().slice(0, 16) !== value) return value;
  // Observe both sides of a possible offset transition. For a repeated local
  // time, choose the earlier instant consistently, including half-hour folds.
  const candidates = [-36, 0, 36].map((hours) => {
    const anchor = wallUtc + hours * 3_600_000;
    const local = workflowSchemaDateTimeValue(new Date(anchor).toISOString(), timeZone);
    const offset = Date.parse(`${local}:00Z`) - anchor;
    return wallUtc - offset;
  }).filter((instant) => Number.isFinite(instant)
    && workflowSchemaDateTimeValue(new Date(instant).toISOString(), timeZone) === value);
  return candidates.length ? new Date(Math.min(...candidates)).toISOString() : value;
}

export function workflowValueAtPath(value: unknown, path: string[]): unknown {
  let current = value;
  for (const part of path) {
    const record = asRecord(current);
    if (!record) return undefined;
    current = record[part];
  }
  return current;
}

export function workflowSchemaSelectValue(
  schema: WorkflowInputSchema,
  value: unknown,
): string {
  const selected = String(value ?? "");
  if (!schema.enum) return selected;
  return schema.enum.some((option) => String(option) === selected) ? selected : "";
}

export function workflowSchemaOptionLabel(
  schema: WorkflowInputSchema,
  option: unknown,
  fallback: (value: string) => string,
): string {
  const labels = schema["x-enum-labels"];
  const index = schema.enum ? schema.enum.findIndex((candidate) => String(candidate) === String(option)) : -1;
  const label = index >= 0 && Array.isArray(labels) ? labels[index] : undefined;
  return typeof label === "string" && label.trim() ? label : fallback(String(option));
}

export interface WorkflowSchemaSuggestion {
  value: string;
  label: string;
  /** The value as shown under its name, without the name repeated in front. */
  detail: string;
  /** What the value looks like, when the field shows one. */
  image?: string;
  /** The group it is browsed under. */
  group?: string;
  /** What it sounds like, when the field offers a sample. */
  sample?: string;
}

/** Only a site path or an https URL is shown as a picture or played as a sample. */
function exampleUrl(value: unknown): string | undefined {
  if (typeof value !== "string") return undefined;
  const url = value.trim();
  return /^\/(?!\/)/.test(url) || /^https:\/\//i.test(url) ? url : undefined;
}

/** A free-text field's suggested values, each with its short name when one is declared. */
export function workflowSchemaSuggestions(schema: WorkflowInputSchema | undefined): WorkflowSchemaSuggestion[] {
  if (!schema || schema.enum || !Array.isArray(schema.examples)) return [];
  const labels = Array.isArray(schema["x-example-labels"]) ? schema["x-example-labels"] : [];
  const images = Array.isArray(schema["x-example-images"]) ? schema["x-example-images"] : [];
  const groups = Array.isArray(schema["x-example-groups"]) ? schema["x-example-groups"] : [];
  const samples = Array.isArray(schema["x-example-samples"]) ? schema["x-example-samples"] : [];
  return schema.examples.flatMap((value, index) => {
    if (typeof value !== "string" || !value.trim()) return [];
    const label = typeof labels[index] === "string" ? labels[index].trim() : "";
    const named = label && value.toLowerCase().startsWith(label.toLowerCase())
      ? value.slice(label.length).replace(/^[^:：]*[:：]\s*/, "").replace(/^[\s,，;；–—-]+/, "")
      : value;
    const image = exampleUrl(images[index]);
    const sample = exampleUrl(samples[index]);
    const group = typeof groups[index] === "string" ? groups[index].trim() : "";
    return [{ value, label, detail: named.trim() || value, ...(image ? { image } : {}),
      ...(group ? { group } : {}), ...(sample ? { sample } : {}) }];
  });
}

/**
 * A fixed list whose options each have a picture (a deck style, a layout…):
 * picked by looking, one at a time. Empty when any option lacks a picture.
 * ``caseValue`` is the sibling value ``x-enum-cases`` names: the options that
 * suit it come first, marked recommended, with the pictures made for it.
 */
export function workflowSchemaPictureChoices(
  schema: WorkflowInputSchema | undefined,
  fallback: (value: string) => string,
  caseValue?: unknown,
): Array<WorkflowSchemaSuggestion & { value: string; recommended?: boolean }> {
  if (!schema?.enum || !Array.isArray(schema["x-enum-images"])) return [];
  const images = schema["x-enum-images"];
  const descriptions = Array.isArray(schema["x-enum-descriptions"]) ? schema["x-enum-descriptions"] : [];
  const variant = typeof caseValue === "string" && caseValue ? schema["x-enum-cases"]?.cases?.[caseValue] : undefined;
  const recommended = (variant?.recommended || []).map(String);
  const choices = schema.enum.map((option, index) => {
    const value = `${option ?? ""}`;
    const image = exampleUrl(variant?.images?.[value]) || exampleUrl(images[index]);
    const detail = typeof descriptions[index] === "string" ? descriptions[index].trim() : "";
    return {
      value, label: workflowSchemaOptionLabel(schema, option, fallback), detail,
      ...(image ? { image } : {}), ...(recommended.includes(value) ? { recommended: true } : {}),
    };
  });
  if (!choices.every((choice) => choice.image)) return [];
  const rank = (value: string) => (recommended.includes(value) ? recommended.indexOf(value) : recommended.length);
  return choices.map((choice, index) => ({ choice, index }))
    .sort((left, right) => rank(left.choice.value) - rank(right.choice.value) || left.index - right.index)
    .map(({ choice }) => choice);
}

/**
 * The other inputs a picked option sets (``x-enum-presets`` / ``x-example-presets``),
 * so choosing one template brings its whole set of settings.
 */
export function workflowSchemaPreset(schema: WorkflowInputSchema | undefined, value: unknown): Record<string, unknown> {
  if (!schema) return {};
  const picked = `${value ?? ""}`;
  const byOption = schema["x-enum-presets"];
  let preset: unknown = byOption && typeof byOption === "object" ? byOption[picked] : undefined;
  if (preset === undefined && Array.isArray(schema.examples) && Array.isArray(schema["x-example-presets"])) {
    const index = schema.examples.findIndex((example) => `${example ?? ""}` === picked);
    preset = index >= 0 ? schema["x-example-presets"][index] : undefined;
  }
  return preset && typeof preset === "object" && !Array.isArray(preset) ? preset as Record<string, unknown> : {};
}

/** "9:16" as width and height, or null for an option that is not a ratio ("auto"). */
export function workflowAspectRatio(value: string): [number, number] | null {
  const match = /^(\d+(?:\.\d+)?):(\d+(?:\.\d+)?)$/.exec(value.trim());
  if (!match) return null;
  const width = Number(match[1]);
  const height = Number(match[2]);
  return width > 0 && height > 0 ? [width, height] : null;
}

/** A string array whose items are a fixed list is a multi-choice field. */
export function workflowSchemaChoices(schema: WorkflowInputSchema | undefined): string[] | null {
  if (!schema || workflowSchemaType(schema) !== "array" || !schema.items) return null;
  if (workflowSchemaType(schema.items) !== "string") return null;
  const options = schema.items.enum;
  return Array.isArray(options) && options.length ? options.map(String) : null;
}

/**
 * A `record_select` field picks Workspace Ledger records by key: a string
 * schema picks one, a string array picks several. Null for anything else.
 */
export function workflowSchemaRecordSelect(
  schema: WorkflowInputSchema | undefined,
): { multiple: boolean } | null {
  const ui = schema?.["x-ui"];
  if (ui?.control !== "record_select" || typeof ui.record_source?.ledger !== "string") return null;
  const schemaType = workflowSchemaType(schema);
  if (schemaType === "array" && workflowSchemaType(schema?.items) === "string") return { multiple: true };
  return schemaType === "string" ? { multiple: false } : null;
}

/** The chosen values of a multi-choice draft (a line list or an array). */
export function workflowSchemaChosen(draft: unknown): Set<string> {
  const values = Array.isArray(draft) ? draft : String(draft ?? "").split(/\r?\n/);
  return new Set(values.map((value) => String(value ?? "").trim()).filter(Boolean));
}

/**
 * Toggle one choice and return the line-list draft in the declared order.
 * Record fields pass the options they loaded as that order.
 */
export function toggleWorkflowSchemaChoice(
  schema: WorkflowInputSchema,
  draft: unknown,
  option: string,
  checked: boolean,
  options: string[] = workflowSchemaChoices(schema) || [],
): string {
  const chosen = workflowSchemaChosen(draft);
  if (checked) chosen.add(option);
  else chosen.delete(option);
  return options.filter((value) => chosen.has(value)).join("\n");
}

export function setWorkflowValueAtPath(
  value: unknown,
  path: string[],
  nextValue: unknown,
): unknown {
  if (path.length === 0) return nextValue;
  const [head, ...tail] = path;
  const record = { ...(asRecord(value) || {}) };
  record[head] = setWorkflowValueAtPath(record[head], tail, nextValue);
  return record;
}

export function workflowInputErrorPath(rootKey: string, path: string[]): string {
  return [rootKey, ...path].filter(Boolean).join(".");
}

/** Nothing entered anywhere: no text, no list item, no nested value. */
function draftIsBlank(value: unknown): boolean {
  if (value === undefined || value === null) return true;
  if (typeof value === "string") return !value.trim();
  if (Array.isArray(value)) return value.every(draftIsBlank);
  if (typeof value === "object") return Object.values(value as Record<string, unknown>).every(draftIsBlank);
  return false;
}

function arrayValueIsPresent(value: unknown): boolean {
  if (Array.isArray(value)) return true;
  if (typeof value === "string") return Boolean(value.trim());
  return value !== undefined && value !== null;
}

function jsonSchemaValuesEqual(left: unknown, right: unknown): boolean {
  if (Object.is(left, right)) return true;
  if (Array.isArray(left) || Array.isArray(right)) {
    return Array.isArray(left)
      && Array.isArray(right)
      && left.length === right.length
      && left.every((value, index) => jsonSchemaValuesEqual(value, right[index]));
  }
  const leftRecord = asRecord(left);
  const rightRecord = asRecord(right);
  if (!leftRecord || !rightRecord) return false;
  const leftKeys = Object.keys(leftRecord);
  const rightKeys = Object.keys(rightRecord);
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key) => (
      Object.prototype.hasOwnProperty.call(rightRecord, key)
      && jsonSchemaValuesEqual(leftRecord[key], rightRecord[key])
    ));
}

function hasDuplicateItems(values: unknown[]): boolean {
  return values.some((value, index) => (
    values.slice(0, index).some((previous) => jsonSchemaValuesEqual(previous, value))
  ));
}

function validateArrayConstraints(
  schema: WorkflowInputSchema,
  values: unknown[],
  valueIsPresent: boolean,
  path: string,
  required: boolean,
  errors: Record<string, string>,
  messages: WorkflowSchemaValidationMessages,
): void {
  if (valueIsPresent && schema.minItems !== undefined && values.length < schema.minItems) {
    errors[path] = messages.minItems(schema.minItems);
  } else if (valueIsPresent && schema.maxItems !== undefined && values.length > schema.maxItems) {
    errors[path] = messages.maxItems(schema.maxItems);
  } else if (valueIsPresent && schema.uniqueItems && hasDuplicateItems(values)) {
    errors[path] = messages.uniqueItems;
  } else if (required && (schema.minItems || 0) > values.length) {
    errors[path] = messages.required;
  }
}

/** A parsed value meets one field's own limits (the ones an ``anyOf`` branch states). */
function valueSatisfies(schema: WorkflowInputSchema, value: unknown): boolean {
  if (schema.const !== undefined && !jsonSchemaValuesEqual(schema.const, value)) return false;
  if (schema.enum && !schema.enum.some((option) => jsonSchemaValuesEqual(option, value))) return false;
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems) return false;
    if (schema.maxItems !== undefined && value.length > schema.maxItems) return false;
  }
  if (typeof value === "string" && schema.minLength !== undefined
    && Array.from(value).length < schema.minLength) return false;
  return constraintsHold(schema, value);
}

/**
 * Whether a parsed object meets the keywords that combine its fields: required
 * fields, ``allOf``, ``anyOf``, ``oneOf`` and ``if``/``then``/``else``. A blank
 * field is absent, the same way the form drops it from the answer.
 */
function constraintsHold(schema: WorkflowInputSchema, value: unknown): boolean {
  const record = asRecord(value) || {};
  if ((schema.required || []).some((key) => draftIsBlank(record[key]))) return false;
  for (const [key, child] of Object.entries(schema.properties || {})) {
    if (!draftIsBlank(record[key]) && !valueSatisfies(child, record[key])) return false;
  }
  if (!(schema.allOf || []).every((branch) => constraintsHold(branch, value))) return false;
  if (schema.anyOf && !schema.anyOf.some((branch) => constraintsHold(branch, value))) return false;
  if (schema.if) {
    const branch = constraintsHold(schema.if, value) ? schema.then : schema.else;
    if (branch && !constraintsHold(branch, value)) return false;
  }
  return true;
}

/**
 * Explains why a parsed object fails its constraints, on the fields the person
 * can fill. Each error lands on the field's own path so the form shows it there.
 */
function reportConstraintFailures(
  schema: WorkflowInputSchema,
  value: unknown,
  path: string,
  errors: Record<string, string>,
  messages: WorkflowSchemaValidationMessages,
): void {
  const record = asRecord(value) || {};
  for (const key of schema.required || []) {
    if (draftIsBlank(record[key])) errors[`${path}.${key}`] ??= messages.required;
  }
  for (const branch of schema.allOf || []) {
    if (!constraintsHold(branch, value)) reportConstraintFailures(branch, value, path, errors, messages);
  }
  if (schema.anyOf && !schema.anyOf.some((branch) => constraintsHold(branch, value))) {
    const fields = [...new Set(schema.anyOf.flatMap((branch) => branch.required || []))];
    const labels = fields.map((key) => schema.properties?.[key]?.title || key);
    // A rule may say in its own words what is missing ("Choose at least one platform").
    const message = typeof schema["x-message"] === "string" && schema["x-message"].trim()
      ? schema["x-message"].trim() : messages.anyOfRequired(labels);
    for (const key of fields) errors[`${path}.${key}`] ??= message;
  }
  if (schema.if) {
    const branch = constraintsHold(schema.if, value) ? schema.then : schema.else;
    if (branch && !constraintsHold(branch, value)) reportConstraintFailures(branch, value, path, errors, messages);
  }
}

export function parseWorkflowSchemaDraft(
  schema: WorkflowInputSchema,
  draft: unknown,
  path: string,
  required: boolean,
  errors: Record<string, string>,
  messages: WorkflowSchemaValidationMessages,
): unknown {
  if (schema.const !== undefined) return schema.const;
  if (draft === undefined && !required) return undefined;
  if (schema["x-workflow-type"] === "json") {
    const rawJson = typeof draft === "string" ? draft.trim() : draft;
    if (!rawJson) {
      if (required) errors[path] = messages.required;
      return required ? undefined : {};
    }
    try {
      return typeof rawJson === "string" ? JSON.parse(rawJson) : rawJson;
    } catch {
      errors[path] = messages.invalidJson;
      return undefined;
    }
  }
  const schemaType = workflowSchemaType(schema);
  if (schemaType === "object") {
    // An optional group inside a form, left untouched, is absent: its own
    // required fields only apply once the person starts filling it in. The
    // form itself (the root path) is never dropped.
    if (!required && path.includes(".") && draftIsBlank(draft)) return undefined;
    const draftRecord = asRecord(draft) || {};
    const requiredKeys = new Set(schema.required || []);
    const parsed: Record<string, unknown> = schema.additionalProperties === false
      ? {}
      : { ...draftRecord };
    for (const [key, child] of Object.entries(schema.properties || {})) {
      const childValue = parseWorkflowSchemaDraft(
        child,
        draftRecord[key],
        `${path}.${key}`,
        requiredKeys.has(key),
        errors,
        messages,
      );
      if (childValue !== undefined) parsed[key] = childValue;
      else delete parsed[key];
    }
    if (!constraintsHold(schema, parsed)) reportConstraintFailures(schema, parsed, path, errors, messages);
    return parsed;
  }
  if (schemaType === "array") {
    const valueIsPresent = arrayValueIsPresent(draft);
    if (workflowSchemaType(schema.items) === "string") {
      const values = (Array.isArray(draft) ? draft : String(draft || "").split(/\r?\n/))
        .map((item) => String(item || "").trim())
        .filter(Boolean);
      validateArrayConstraints(schema, values, valueIsPresent, path, required, errors, messages);
      return values;
    }
    if (!valueIsPresent) return [];
    try {
      const parsed = typeof draft === "string" ? JSON.parse(draft) : draft;
      if (!Array.isArray(parsed)) throw new Error("array required");
      validateArrayConstraints(schema, parsed, valueIsPresent, path, required, errors, messages);
      return parsed;
    } catch {
      errors[path] = messages.invalidJson;
      return undefined;
    }
  }
  if (schemaType === "boolean") {
    const enumIndex = schema.enum?.findIndex((option) => String(option) === String(draft)) ?? -1;
    if (schema.enum && enumIndex < 0) {
      errors[path] = messages.required;
      return undefined;
    }
    return Boolean(enumIndex >= 0 ? schema.enum?.[enumIndex] : draft);
  }
  let raw = String(draft ?? "").trim();
  if (
    !raw
    && !required
    && schemaType === "string"
    && (schema["x-ui"]?.control === "workflow_project_select" || schema.format)
  ) {
    // Unselected optional projects and blank formatted values are absent;
    // an empty string is not a valid date or URI. Keep ordinary fields so forms can still
    // intentionally clear them and retry fields still enforce minLength.
    return undefined;
  }
  if (!raw) {
    if (required) errors[path] = messages.required;
    else if (schemaType === "string" && (schema.minLength || 0) > 0) {
      errors[path] = messages.minLength(schema.minLength!);
    }
    return errors[path] ? undefined : "";
  }
  if (schemaType === "string") {
    const length = Array.from(raw).length;
    if (schema.minLength !== undefined && length < schema.minLength) {
      errors[path] = messages.minLength(schema.minLength);
      return undefined;
    }
    if (schema.maxLength !== undefined && length > schema.maxLength) {
      errors[path] = messages.maxLength(schema.maxLength);
      return undefined;
    }
  }
  if (schemaType === "string" && schema.format === "uri") {
    raw = withUrlScheme(raw);
    try {
      new URL(raw);
    } catch {
      errors[path] = messages.invalidUrl;
      return undefined;
    }
  }
  if (schemaType === "string" && schema.format === "date-time"
    && (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/i.test(raw)
      || !Number.isFinite(Date.parse(raw))
      || new Date(`${raw.slice(0, 19)}Z`).toISOString().slice(0, 19) !== raw.slice(0, 19))) {
    errors[path] = messages.invalidFormat;
    return undefined;
  }
  if (schemaType === "string" && schema["x-ui"]?.control === "timezone") {
    try { new Intl.DateTimeFormat("en", { timeZone: raw }); }
    catch {
      errors[path] = messages.invalidFormat;
      return undefined;
    }
  }
  if (schemaType === "string" && schema.pattern) {
    try {
      if (!new RegExp(schema.pattern).test(raw)) {
        errors[path] = messages.invalidFormat;
        return undefined;
      }
    } catch {
      // Invalid schemas are rejected by the API; keep the supplied field usable.
    }
  }
  if (schemaType === "number" || schemaType === "integer") {
    const number = Number(raw);
    if (!Number.isFinite(number) || (schemaType === "integer" && !Number.isInteger(number))) {
      errors[path] = messages.invalidNumber;
      return undefined;
    }
    if (schema.minimum !== undefined && number < schema.minimum) {
      errors[path] = messages.minimum(schema.minimum);
      return undefined;
    }
    if (schema.maximum !== undefined && number > schema.maximum) {
      errors[path] = messages.maximum(schema.maximum);
      return undefined;
    }
    if (schema.exclusiveMinimum !== undefined && number <= schema.exclusiveMinimum) {
      errors[path] = messages.exclusiveMinimum(schema.exclusiveMinimum);
      return undefined;
    }
    if (schema.exclusiveMaximum !== undefined && number >= schema.exclusiveMaximum) {
      errors[path] = messages.exclusiveMaximum(schema.exclusiveMaximum);
      return undefined;
    }
    return number;
  }
  if (schema.enum && !schema.enum.some((option) => String(option) === raw)) {
    errors[path] = messages.required;
    return undefined;
  }
  return raw;
}

export function visibleWorkflowSchemaFieldCount(schema?: WorkflowInputSchema): number {
  if (!schema || schema["x-ui"]?.hidden) return 0;
  if (workflowSchemaType(schema) !== "object") return 1;
  return Object.values(schema.properties || {}).reduce(
    (total, child) => total + visibleWorkflowSchemaFieldCount(child),
    0,
  );
}
