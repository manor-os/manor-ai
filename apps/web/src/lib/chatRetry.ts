export interface PendingChatRetry {
  message: string;
  conversationId?: string;
  documentIds?: string[];
  agentId?: string;
  workspaceId?: string;
  manualSkillIds?: string[];
  chatMode?: string;
  chatModePayload?: Record<string, unknown>;
  createdAt: number;
}

const KEY = "manor_pending_chat_retry";
const TTL_MS = 1000 * 60 * 30;

export function savePendingChatRetry(payload: Omit<PendingChatRetry, "createdAt">): void {
  if (!payload.message?.trim()) return;
  const record: PendingChatRetry = { ...payload, message: payload.message.trim(), createdAt: Date.now() };
  localStorage.setItem(KEY, JSON.stringify(record));
}

export function clearPendingChatRetry(): void {
  localStorage.removeItem(KEY);
}

export function hasPendingChatRetry(): boolean {
  const raw = localStorage.getItem(KEY);
  if (!raw) return false;
  try {
    const parsed = JSON.parse(raw) as PendingChatRetry;
    if (!parsed.message || Date.now() - parsed.createdAt > TTL_MS) {
      localStorage.removeItem(KEY);
      return false;
    }
    return true;
  } catch {
    localStorage.removeItem(KEY);
    return false;
  }
}

export function consumePendingChatRetry(): PendingChatRetry | null {
  const raw = localStorage.getItem(KEY);
  localStorage.removeItem(KEY);
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as PendingChatRetry;
    if (!parsed.message || Date.now() - parsed.createdAt > TTL_MS) return null;
    return parsed;
  } catch {
    return null;
  }
}
