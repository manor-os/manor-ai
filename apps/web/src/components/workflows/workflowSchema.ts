export interface WorkflowInputSchema {
  type?: string | string[];
  title?: string;
  description?: string;
  format?: string;
  pattern?: string;
  minLength?: number;
  maxLength?: number;
  enum?: unknown[];
  const?: unknown;
  default?: unknown;
  properties?: Record<string, WorkflowInputSchema>;
  required?: string[];
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
    control?: "textarea" | "line_list";
    rows?: number;
    collapsible?: boolean;
    collapsed?: boolean;
    order?: string[];
  };
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
}

export interface WorkflowSchemaFieldIdSet {
  fieldId: string;
  helpId: string;
  errorId: string;
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

export function workflowSchemaDraft(schema: WorkflowInputSchema, supplied: unknown): unknown {
  const source = supplied ?? schema.default ?? schema.const;
  if (schema["x-workflow-type"] === "json") {
    if (typeof source === "string") return source;
    try { return JSON.stringify(source ?? {}, null, 2); } catch { return "{}"; }
  }
  const schemaType = workflowSchemaType(schema);
  if (schemaType === "object") {
    const sourceRecord = asRecord(source) || {};
    return Object.fromEntries(Object.entries(schema.properties || {}).map(([key, child]) => [
      key,
      workflowSchemaDraft(child, sourceRecord[key]),
    ]));
  }
  if (schemaType === "array") {
    if (workflowSchemaType(schema.items) === "string") {
      return Array.isArray(source)
        ? source.map((item) => String(item || "").trim()).filter(Boolean).join("\n")
        : String(source || "");
    }
    if (typeof source === "string") return source;
    try { return JSON.stringify(source ?? [], null, 2); } catch { return "[]"; }
  }
  if (schemaType === "boolean") return Boolean(source);
  return source === undefined || source === null ? "" : String(source);
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

export function parseWorkflowSchemaDraft(
  schema: WorkflowInputSchema,
  draft: unknown,
  path: string,
  required: boolean,
  errors: Record<string, string>,
  messages: WorkflowSchemaValidationMessages,
): unknown {
  if (schema.const !== undefined) return schema.const;
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
    const draftRecord = asRecord(draft) || {};
    const requiredKeys = new Set(schema.required || []);
    const parsed: Record<string, unknown> = {};
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
    }
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
  const raw = String(draft ?? "").trim();
  if (!raw) {
    if (required) errors[path] = messages.required;
    return required ? undefined : "";
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
    try {
      new URL(raw);
    } catch {
      errors[path] = messages.invalidUrl;
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
