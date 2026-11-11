import type { Workspace } from "./types";

export const CONVERSATION_SIDEBAR_LIMIT = 8;
export type WorkspaceSidebarPreferences = { mode: "recent" | "manual"; order: string[] };

export function readWorkspaceSidebarPreferences(key: string | null): WorkspaceSidebarPreferences {
  try {
    const saved = key ? JSON.parse(window.localStorage.getItem(key) || "null") : null;
    return {
      mode: saved?.mode === "manual" ? "manual" : "recent",
      order: Array.isArray(saved?.order) ? [...new Set(saved.order.filter((id: unknown): id is string => typeof id === "string"))] as string[] : [],
    };
  } catch {
    return { mode: "recent", order: [] };
  }
}

export function workspaceRecency(workspace: Workspace): number {
  return Date.parse(workspace.last_chat_at || workspace.created_at || "") || 0;
}

export type ConversationRanking<T> = {
  recency: (item: T) => number;
  attention: (item: T) => boolean;
};

// Conversations that need the user lead. After them come the ones the user
// placed by hand, in that order, and everything else follows recency.
export function orderSidebarConversations<T extends { id: string }>(items: T[], placed: string[], ranking: ConversationRanking<T>): T[] {
  const rank = new Map(placed.map((id, index) => [id, index]));
  const sorted = [...items].sort((a, b) =>
    (rank.get(a.id) ?? Infinity) - (rank.get(b.id) ?? Infinity)
    || ranking.recency(b) - ranking.recency(a)
    || a.id.localeCompare(b.id));
  return [...sorted.filter(ranking.attention), ...sorted.filter((item) => !ranking.attention(item))];
}

// Returns the new placed list. A dropped row stays exactly where it landed, so
// the rows above it hold still too. Dropping a placed row below the placed ones
// hands it back to recency, which is the only way to undo a placement.
export function placeSidebarConversation(order: string[], placed: string[], attention: ReadonlySet<string>, id: string, target: string, after = false): string[] {
  const next = moveSidebarWorkspace(order, id, target, after);
  if (next === order) return placed;
  if (placed.includes(id) && !placed.includes(target) && !attention.has(target)) return placed.filter((row) => row !== id);
  const landed = next.indexOf(id);
  return next.filter((row, index) => row === id || placed.includes(row) || (index < landed && (attention.has(id) || !attention.has(row))));
}

export function moveSidebarWorkspace(order: string[], id: string, target: string, after = false): string[] {
  if (id === target || !order.includes(id) || !order.includes(target)) return order;
  const next = order.filter((item) => item !== id);
  next.splice(next.indexOf(target) + Number(after), 0, id);
  return next;
}

export function compactSidebarConversations<T extends { id: string }>(conversations: T[], activeId?: string | null): T[] {
  const visible = conversations.slice(0, CONVERSATION_SIDEBAR_LIMIT);
  const active = activeId && conversations.find((conversation) => conversation.id === activeId);
  if (active && !visible.some((conversation) => conversation.id === active.id)) visible.push(active);
  return visible;
}
