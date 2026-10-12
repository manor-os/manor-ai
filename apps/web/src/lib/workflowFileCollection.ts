import { viewerPathForDocumentId } from "./fileReferences";

export const WORKFLOW_FILE_COLLECTION_SCHEMA = "workflow-file-collection/v1";

export interface WorkflowCollectionFile {
  documentId: string;
  name: string;
  group: string;
  role: string;
  mimeType: string;
  viewerUrl: string;
}

export interface WorkflowFileCollection {
  title: string;
  summary: string;
  files: WorkflowCollectionFile[];
  pendingItems: string[];
}

type UnknownRecord = Record<string, unknown>;

const GROUPS = [
  { label: "Audio & artwork", aliases: ["audio & artwork", "audio and artwork", "audio/artwork", "audio", "artwork"] },
  { label: "Start Here", aliases: ["start here"] },
  { label: "Release Materials", aliases: ["release materials"] },
  { label: "Press & Marketing", aliases: ["press & marketing", "press and marketing", "press/marketing"] },
  { label: "Rights & Submission", aliases: ["rights & submission", "rights and submission", "rights/submission"] },
] as const;

function asRecord(value: unknown): UnknownRecord | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as UnknownRecord
    : null;
}

function readableText(value: unknown, maximum = 240): string {
  return String(value ?? "")
    .replace(/[\u0000-\u001f\u007f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, maximum);
}

function groupLabel(value: unknown): string {
  const text = readableText(value, 80);
  const normalized = text.toLowerCase();
  return GROUPS.find((group) => group.aliases.some((alias) => alias === normalized))?.label || text || "Files";
}

function pendingItem(value: unknown): string {
  if (typeof value === "string") return readableText(value);
  const record = asRecord(value);
  if (!record) return "";
  return readableText(record.label ?? record.title ?? record.name ?? record.summary ?? record.description);
}

function collectionRecord(output: unknown): UnknownRecord | null {
  const queue: unknown[] = [output];
  const visited = new WeakSet<object>();
  for (let depth = 0; queue.length && depth < 24; depth += 1) {
    const record = asRecord(queue.shift());
    if (!record || visited.has(record)) continue;
    visited.add(record);
    if (record.schema_version === WORKFLOW_FILE_COLLECTION_SCHEMA) return record;
    for (const key of ["output", "result", "data", "final_output"]) {
      if (record[key] != null) queue.push(record[key]);
    }
  }
  return null;
}

/**
 * Recognize the explicit workflow deliverable manifest. The collection only
 * opens canonical Document routes; any supplied URL is display metadata, never
 * navigation authority.
 */
export function parseWorkflowFileCollection(output: unknown): WorkflowFileCollection | null {
  const record = collectionRecord(output);
  if (!record) return null;
  if (record.delivery_verified !== true || record.preparation_completed !== true) return null;

  const seenDocumentIds = new Set<string>();
  const files: WorkflowCollectionFile[] = [];
  for (const value of Array.isArray(record.files) ? record.files : []) {
    const file = asRecord(value);
    if (!file) continue;
    const documentId = readableText(file.document_id, 160);
    const viewerUrl = viewerPathForDocumentId(documentId);
    if (!viewerUrl || seenDocumentIds.has(documentId)) continue;
    seenDocumentIds.add(documentId);
    files.push({
      documentId,
      name: readableText(file.name, 240) || "File",
      group: groupLabel(file.group),
      role: readableText(file.role, 120),
      mimeType: readableText(file.mime_type, 120),
      viewerUrl,
    });
  }

  return {
    title: readableText(record.title, 160) || "Your files",
    summary: readableText(record.summary, 500),
    files,
    pendingItems: (Array.isArray(record.pending_items) ? record.pending_items : [])
      .map(pendingItem)
      .filter(Boolean),
  };
}

export function groupedWorkflowCollectionFiles(collection: WorkflowFileCollection): Array<{ label: string; files: WorkflowCollectionFile[] }> {
  const byGroup = new Map<string, WorkflowCollectionFile[]>();
  for (const file of collection.files) {
    const group = byGroup.get(file.group) || [];
    group.push(file);
    byGroup.set(file.group, group);
  }
  const orderedLabels = [
    ...GROUPS.map((group) => group.label),
    ...Array.from(byGroup.keys()).filter((label) => !GROUPS.some((group) => group.label === label)),
  ];
  return orderedLabels.flatMap((label) => {
    const files = byGroup.get(label);
    return files ? [{ label, files }] : [];
  });
}
