import type { SelectedTextTaskDraft } from "./selectionActions";

export const TASK_DRAFT_HANDOFF_KEY = "manor:task-draft-handoff:v1";
const MAX_DRAFTS = 100;
const MAX_BYTES = 200_000;
const MAX_AGE = 30 * 60 * 1000;

// Companion apps may prefill the existing task form. This never creates a
// task, assigns an agent, or grants access to a workspace.
export function taskDraftOriginAllowed(origin: string, currentOrigin: string): boolean {
  if ([currentOrigin, "https://minutes.manorai.xyz", "https://portfolio.manorai.xyz"].includes(origin)) return true;
  const local = (value: string) => {
    try {
      const url = new URL(value);
      return url.protocol === "http:" && ["localhost", "127.0.0.1"].includes(url.hostname);
    } catch { return false; }
  };
  return local(currentOrigin) && local(origin);
}

export function parseTaskDrafts(value: unknown): SelectedTextTaskDraft[] {
  if (!Array.isArray(value) || !value.length || value.length > MAX_DRAFTS
    || JSON.stringify(value).length > MAX_BYTES) throw new Error("Invalid task drafts.");
  return value.map(raw => {
    if (!raw || typeof raw !== "object" || typeof raw.title !== "string"
      || !raw.title.trim() || raw.title.length > 300
      || typeof raw.description !== "string" || raw.description.length > 20000) {
      throw new Error("Invalid task draft.");
    }
    let sourcePath: string | undefined;
    if (raw.sourcePath) {
      const url = new URL(raw.sourcePath);
      if (url.protocol !== "https:" || url.username || url.password || url.href.length > 2000) {
        throw new Error("Invalid task source.");
      }
      sourcePath = url.href;
    }
    return { title: raw.title.trim(), description: raw.description, sourcePath };
  });
}

export function saveTaskDraftHandoff(storage: Storage, drafts: unknown, now = Date.now()) {
  storage.setItem(TASK_DRAFT_HANDOFF_KEY, JSON.stringify({ createdAt: now, drafts: parseTaskDrafts(drafts) }));
}

export function takeTaskDraftHandoff(storage: Storage, now = Date.now()): SelectedTextTaskDraft[] {
  const raw = storage.getItem(TASK_DRAFT_HANDOFF_KEY);
  storage.removeItem(TASK_DRAFT_HANDOFF_KEY);
  if (!raw) return [];
  try {
    const data = JSON.parse(raw);
    if (!Number.isFinite(data.createdAt) || now < data.createdAt || now - data.createdAt > MAX_AGE) return [];
    return parseTaskDrafts(data.drafts);
  } catch { return []; }
}

export function receiveTaskDraftHandoff(
  event: MessageEvent, opener: Window | null, nonce: string, origin: string, storage: Storage,
): boolean {
  if (!opener || event.source !== opener || !taskDraftOriginAllowed(event.origin, origin)
    || !nonce || event.data?.nonce !== nonce || event.data?.type !== "manor:task-drafts:v1") return false;
  saveTaskDraftHandoff(storage, event.data.drafts);
  return true;
}

/** The companion frame accepts drafts from its exact parent, never from a
 * sibling frame. The return channel carries task IDs, not account credentials. */
export function readEmbeddedTaskDrafts(event: MessageEvent, parent: Window, nonce: string, source: string, origin: string) {
  if (!nonce || event.source !== parent || event.origin !== source
    || !taskDraftOriginAllowed(source, origin) || event.data?.nonce !== nonce
    || event.data?.type !== "manor:task-drafts:v1") return null;
  return parseTaskDrafts(event.data.drafts);
}
