import { useCallback, useRef, useState, type SetStateAction } from "react";

export function chatComposerDraftKey(entityId: string | undefined, userId: string | undefined, conversationId: string) {
  return `manor-chat-composer-draft:${JSON.stringify([entityId || "", userId || "", conversationId])}`;
}

export function readChatComposerDraft(key: string): string {
  try {
    return typeof window === "undefined" ? "" : window.sessionStorage.getItem(key) || "";
  } catch {
    return "";
  }
}

export function saveChatComposerDraft(key: string, value: string): void {
  try {
    if (value) window.sessionStorage.setItem(key, value);
    else window.sessionStorage.removeItem(key);
  } catch {
    // Storage restrictions must not prevent typing or sending.
  }
}

/** Text only: attachments and execution controls stay in their existing owners. */
export function useChatComposerDraft(key: string) {
  const [draft, setDraft] = useState(() => ({ key, value: readChatComposerDraft(key) }));
  // Resolve a changed conversation synchronously so one render cannot show or
  // save the preceding conversation's input in the new scope.
  const value = draft.key === key ? draft.value : readChatComposerDraft(key);
  const currentDraft = useRef({ key, value });
  currentDraft.current = { key, value };
  const setValue = useCallback((update: SetStateAction<string>) => {
    const current = currentDraft.current;
    const previousValue = current.key === key ? current.value : readChatComposerDraft(key);
    const nextValue = typeof update === "function" ? update(previousValue) : update;
    const next = { key, value: nextValue };
    saveChatComposerDraft(next.key, next.value);
    // A send started in another conversation may finish after navigation.
    // Its setter still owns that conversation, never the current composer.
    if (current.key === key) {
      currentDraft.current = next;
      setDraft(next);
    }
  }, [key]);
  return [value, setValue] as const;
}
