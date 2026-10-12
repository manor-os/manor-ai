export type WorkflowRunInput = {
  key: string;
  label: string;
  type: "string" | "number" | "boolean" | "json";
  required: boolean;
  placeholder?: string;
  defaultValue: string;
  integer?: boolean;
  minimum?: number;
  maximum?: number;
  documents: boolean;
  options?: { value: string; label: string }[];
};

function runInputDefault(value: unknown, variables?: Record<string, unknown>): unknown {
  if (typeof value !== "string") return value;
  const match = /^\s*\{\{([^{}]+)\}\}\s*$/.exec(value);
  if (!match) return value;
  const parts = match[1].trim().split(".");
  let current: unknown = variables?.[parts[0]];
  for (const part of parts.slice(1)) {
    if (!current || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

function formatRunInputValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    try { return JSON.stringify(value, null, 2); } catch { return String(value); }
  }
  return String(value);
}

/** Inputs declared on the entry node are runtime values, not saved workflow
 * settings. Imported n8n Form Triggers use this same schema. */
export function workflowRunInputs(flow: { steps?: { type?: string; config?: Record<string, any> }[]; variables?: Record<string, unknown> }): WorkflowRunInput[] {
  const entries = (flow.steps || []).filter((step: any) => ["trigger", "webhook"].includes(step.type));
  const seen = new Set<string>();
  const result: WorkflowRunInput[] = [];
  for (const step of entries) {
    const rows = Array.isArray(step.config?.run_inputs)
      ? step.config.run_inputs
      : Array.isArray(step.config?.inputs) ? step.config.inputs : [];
    for (const row of rows) {
      const key = String(row?.key || row?.name || "").trim();
      if (!key || row?.hidden || seen.has(key)) continue;
      seen.add(key);
      const rawType = String(row?.type || "string").toLowerCase();
      const type: WorkflowRunInput["type"] = rawType === "integer"
        ? "number"
        : ["number", "boolean", "json"].includes(rawType)
          ? rawType as WorkflowRunInput["type"]
          : "string";
      const schema = row?.schema && typeof row.schema === "object" ? row.schema : {};
      const rawDefault = row?.defaultValue ?? row?.default ?? row?.value;
      const formattedDefault = formatRunInputValue(runInputDefault(rawDefault, flow.variables));
      result.push({
        key,
        label: String(row?.label || key),
        type,
        required: row?.required ?? row?.requiredField ?? true,
        placeholder: row?.placeholder ? String(row.placeholder) : undefined,
        defaultValue: type === "boolean" && formattedDefault === "" ? "false" : formattedDefault,
        integer: rawType === "integer" || schema.type === "integer",
        minimum: typeof schema.minimum === "number" ? schema.minimum : undefined,
        maximum: typeof schema.maximum === "number" ? schema.maximum : undefined,
        documents: type === "json" && schema.type === "array" && schema["x-ui"]?.control === "documents",
        options: Array.isArray(schema.enum)
          ? schema.enum.map((value: unknown, i: number) => {
            const label = Array.isArray(schema["x-enum-labels"]) ? schema["x-enum-labels"][i] : undefined;
            return {
              value: String(value),
              label: typeof label === "string" && label.trim() ? label : String(value),
            };
          })
          : undefined,
      });
    }
  }
  return result;
}

export type WorkflowDocumentRef = {
  document_id: string;
  id?: string;
  name?: string;
  type?: string;
  viewer_url?: string;
  fs_path?: string | null;
};

export type WorkflowUpload = {
  file: File;
  uploadKey: string;
  document?: WorkflowDocumentRef;
};

/** Keep each successful receipt on the pending item before attempting the next
 * upload, including when a later upload or the eventual launch fails. */
export async function prepareWorkflowDocuments(
  selected: WorkflowDocumentRef[],
  pending: WorkflowUpload[],
  upload: (item: WorkflowUpload) => Promise<WorkflowDocumentRef>,
): Promise<WorkflowDocumentRef[]> {
  const documents = new Map(selected.map((document) => [document.document_id, document]));
  for (const item of pending) {
    item.document ??= await upload(item);
    documents.set(item.document.document_id, item.document);
  }
  return [...documents.values()];
}

type WorkflowDocumentGroup = {
  id: string;
  is_default_collection?: boolean;
  documents?: { id: string }[];
};

/** Use current membership and verify the server result: skipped IDs may mean
 * either an existing membership or denied access, never unconditional success. */
export async function linkWorkflowDocuments(
  documents: WorkflowDocumentRef[],
  listGroups: () => Promise<WorkflowDocumentGroup[]>,
  addDocuments: (groupId: string, documentIds: string[]) => Promise<unknown>,
): Promise<void> {
  if (!documents.length) return;
  const groups = await listGroups();
  const existing = new Set(groups.flatMap((group) => (group.documents || []).map((document) => document.id)));
  const missing = [...new Set(documents.map((document) => document.document_id))].filter((id) => !existing.has(id));
  if (!missing.length) return;
  const target = groups.find((group) => group.is_default_collection);
  if (!target) throw new Error("The Workspace materials collection is unavailable. Refresh and try again.");
  await addDocuments(target.id, missing);
  const verified = new Set((await listGroups()).flatMap((group) => (group.documents || []).map((document) => document.id)));
  if (missing.some((id) => !verified.has(id))) {
    throw new Error("Some files could not be added to this Workspace. Check their access permissions and try again.");
  }
}
