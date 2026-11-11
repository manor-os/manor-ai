/**
 * Unsent form answers kept on this device, so a reload or a closed tab does
 * not lose them. Keyed by what the form is for (a run form of one Workflow
 * binding, one question card); cleared when the form is submitted.
 */
import { useCallback, useEffect, useRef, useState } from "react";

const PREFIX = "manor.form-draft.v1:";
/** A draft older than this is dropped instead of restored. */
const MAX_AGE_MS = 14 * 24 * 60 * 60 * 1000;

/** The storage key of a form; null when the form has no stable identity. */
export function formDraftKey(...parts: Array<string | null | undefined>): string | null {
  const named = parts.map((part) => String(part ?? "").trim());
  return named.every(Boolean) ? `${PREFIX}${named.join(":")}` : null;
}

interface StoredDraft {
  savedAt: number;
  values: unknown;
}

type DraftStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

function storage(): DraftStorage | null {
  try {
    return typeof localStorage === "undefined" ? null : localStorage;
  } catch {
    return null;
  }
}

/** File picks still uploading and blank answers are not worth keeping. */
function hasContent(value: unknown): boolean {
  if (value === undefined || value === null || value === false) return false;
  if (typeof value === "string") return value.trim() !== "";
  if (Array.isArray(value)) return value.some(hasContent);
  if (typeof value === "object") return Object.values(value as Record<string, unknown>).some(hasContent);
  return true;
}

export function readFormDraft(key: string | null, now = Date.now(), store = storage()): unknown {
  if (!key || !store) return undefined;
  try {
    const raw = store.getItem(key);
    if (!raw) return undefined;
    const parsed = JSON.parse(raw) as StoredDraft;
    if (!parsed || typeof parsed.savedAt !== "number" || now - parsed.savedAt > MAX_AGE_MS) {
      store.removeItem(key);
      return undefined;
    }
    return parsed.values;
  } catch {
    return undefined;
  }
}

export function writeFormDraft(key: string | null, values: unknown, now = Date.now(), store = storage()): void {
  if (!key || !store) return;
  try {
    if (!hasContent(values)) store.removeItem(key);
    else store.setItem(key, JSON.stringify({ savedAt: now, values } satisfies StoredDraft));
  } catch {
    // Full or blocked storage only loses the convenience.
  }
}

export function clearFormDraft(key: string | null, store = storage()): void {
  if (!key || !store) return;
  try { store.removeItem(key); } catch { /* nothing kept */ }
}

/**
 * Restore a draft over the form's opening values: a saved answer replaces the
 * field's default, fields the form no longer has are dropped.
 */
export function mergeFormDraft<T>(initial: T, saved: unknown): T {
  if (saved === undefined) return initial;
  if (!initial || typeof initial !== "object" || Array.isArray(initial)) return saved as T;
  if (!saved || typeof saved !== "object" || Array.isArray(saved)) return initial;
  const kept = saved as Record<string, unknown>;
  // A form whose fields are not known up front keeps what was saved.
  if (!Object.keys(initial as Record<string, unknown>).length) return saved as T;
  return Object.fromEntries(Object.entries(initial as Record<string, unknown>).map(([field, value]) => [
    field,
    field in kept ? kept[field] : value,
  ])) as T;
}

/**
 * Form state that survives a reload: ``useState`` with the saved draft
 * restored at mount and every change saved (debounced). ``clear`` forgets it,
 * for a submitted form.
 */
export function useFormDraft<T>(key: string | null, initial: () => T, delayMs = 400) {
  // The form's own opening values: a draft equal to them holds nothing to keep.
  const [pristine] = useState(() => {
    const opening = initial();
    return { opening, json: JSON.stringify(opening) };
  });
  const [values, setValues] = useState<T>(() => mergeFormDraft(pristine.opening, readFormDraft(key)));
  const [restored, setRestored] = useState(() => readFormDraft(key) !== undefined);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    if (!key) return;
    if (timer.current) clearTimeout(timer.current);
    timer.current = setTimeout(() => {
      if (JSON.stringify(values) === pristine.json) clearFormDraft(key);
      else writeFormDraft(key, values);
    }, delayMs);
    return () => { if (timer.current) clearTimeout(timer.current); };
  }, [key, values, delayMs, pristine]);
  const clear = useCallback(() => {
    if (timer.current) clearTimeout(timer.current);
    clearFormDraft(key);
    setRestored(false);
  }, [key]);
  /** Back to the form's opening values, forgetting the draft. */
  const reset = useCallback(() => {
    clear();
    setValues(pristine.opening);
  }, [clear, pristine]);
  return { values, setValues, clear, reset, restored };
}
