/**
 * The form an agent asks in chat (`request_input` fields) as one object schema,
 * so it renders through the same controls as a Workflow run form
 * (WorkflowSchemaFields) and is checked by the same parser.
 *
 * The server sends each field's `schema` (packages/core/ai/runtime/human_input.py
 * `input_field_schema`); questions asked before that carry only a `type`, which
 * maps here to the schema it would have had.
 */
import type { WorkflowInputSchema } from "../components/workflows/workflowSchema";

export interface RequestInputField {
  name: string;
  label?: string;
  type?: string;
  required?: boolean;
  options?: string[];
  description?: string;
  schema?: WorkflowInputSchema;
}

const DOCUMENT_ITEMS: WorkflowInputSchema = {
  type: "object",
  required: ["document_id"],
  properties: { document_id: { type: "string", minLength: 1 } },
};

/** A question's field read from a card or run payload; null for anything unusable. */
export function requestInputFieldFrom(raw: unknown): RequestInputField | null {
  if (!raw || typeof raw !== "object") return null;
  const item = raw as Record<string, unknown>;
  const name = String(item.name || "").trim();
  if (!name) return null;
  const options = Array.isArray(item.options) ? item.options.map(String).filter(Boolean) : undefined;
  const schema = item.schema && typeof item.schema === "object" && !Array.isArray(item.schema)
    ? item.schema as WorkflowInputSchema : undefined;
  return {
    name,
    label: String(item.label || item.name || ""),
    type: String(item.type || "text"),
    required: item.required !== false,
    ...(options?.length ? { options } : {}),
    ...(typeof item.description === "string" && item.description.trim() ? { description: item.description.trim() } : {}),
    ...(schema ? { schema } : {}),
  };
}

/** The schema of a field the server sent without one (questions from before field schemas). */
function legacyFieldSchema(field: RequestInputField): WorkflowInputSchema {
  switch (field.type) {
    case "boolean": return { type: "boolean" };
    case "date": return { type: "string", format: "date" };
    case "number": return { type: "number" };
    case "textarea": return { type: "string", "x-ui": { control: "textarea" } };
    case "file": return { type: "array", items: DOCUMENT_ITEMS, "x-ui": { control: "documents" } };
    case "select": return field.options?.length ? { type: "string", enum: field.options } : { type: "string" };
    default: return { type: "string" };
  }
}

export function requestInputFieldSchema(field: RequestInputField): WorkflowInputSchema {
  const schema = field.schema || legacyFieldSchema(field);
  const required = Boolean(field.required) && schema.type === "array" && schema.minItems === undefined;
  return {
    ...schema,
    title: field.label || schema.title || field.name,
    ...(field.description && !schema.description ? { description: field.description } : {}),
    // A required list is answered by at least one item, as on the server.
    ...(required ? { minItems: 1 } : {}),
  };
}

/** All of a question's fields as one form, in the order the agent asked them. */
export function requestInputFormSchema(fields: RequestInputField[]): WorkflowInputSchema {
  return {
    type: "object",
    properties: Object.fromEntries(fields.map((field) => [field.name, requestInputFieldSchema(field)])),
    required: fields.filter((field) => field.required).map((field) => field.name),
    additionalProperties: false,
    "x-ui": { order: fields.map((field) => field.name) },
  };
}

/** One answer value as a person reads it. */
export function requestInputValueText(value: unknown, yes = "Yes", no = "No"): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "boolean") return value ? yes : no;
  if (Array.isArray(value)) return value.map((item) => requestInputValueText(item, yes, no)).filter(Boolean).join("; ");
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    if ("document_id" in record) return String(record.name || record.document_id || "");
    if (Object.keys(record).length === 2 && "start" in record && "end" in record) return `${record.start} – ${record.end}`;
    return Object.entries(record)
      .map(([key, item]) => [key, requestInputValueText(item, yes, no)])
      .filter(([, text]) => text)
      .map(([key, text]) => `${key}: ${text}`)
      .join(", ");
  }
  return String(value).trim();
}

/** "Label: value" per answered field, the line the transcript keeps for a form answer. */
export function requestInputAnswerText(
  fields: RequestInputField[],
  values: Record<string, unknown>,
  yes?: string,
  no?: string,
): string {
  return fields
    .map((field) => [field.label || field.name, requestInputValueText(values[field.name], yes, no)])
    .filter(([, text]) => text)
    .map(([label, text]) => `${label}: ${text}`)
    .join("\n");
}
