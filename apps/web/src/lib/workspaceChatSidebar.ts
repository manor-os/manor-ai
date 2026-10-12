import type { Workspace } from "./types";

export const WORKSPACE_SIDEBAR_LIMIT = 8;
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

export function orderSidebarWorkspaces(workspaces: Workspace[], preferences: WorkspaceSidebarPreferences): Workspace[] {
  const rank = new Map(preferences.order.map((id, index) => [id, index]));
  const time = (workspace: Workspace) => Date.parse(workspace.last_chat_at || workspace.created_at || "") || 0;
  return [...workspaces].sort((a, b) => {
    if (preferences.mode === "manual") {
      const difference = (rank.get(a.id) ?? Infinity) - (rank.get(b.id) ?? Infinity);
      if (difference && !Number.isNaN(difference)) return difference;
    }
    return time(b) - time(a) || a.id.localeCompare(b.id);
  });
}

export function moveSidebarWorkspace(order: string[], id: string, target: string, after = false): string[] {
  if (id === target || !order.includes(id) || !order.includes(target)) return order;
  const next = order.filter((item) => item !== id);
  next.splice(next.indexOf(target) + Number(after), 0, id);
  return next;
}

export function compactSidebarWorkspaces(workspaces: Workspace[], activeId?: string | null): Workspace[] {
  const visible = workspaces.slice(0, WORKSPACE_SIDEBAR_LIMIT);
  const active = activeId && workspaces.find((workspace) => workspace.id === activeId);
  if (active && !visible.some((workspace) => workspace.id === active.id)) visible.push(active);
  return visible;
}
