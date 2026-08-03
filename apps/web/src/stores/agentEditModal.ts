import { create } from "zustand";

/**
 * Global agent create/edit modal (Codex-plugin style, same pattern as
 * `stores/detail.ts`). A single `<AgentEditModal />` is mounted once at the
 * app root; any page calls `openAgentEditModal(agentId?)` to open it —
 * with an id to edit that agent, or with no argument to create a new one.
 */
interface AgentEditModalState {
  /** undefined = closed. null = open in create mode. string = open editing that agent id. */
  agentId: string | null | undefined;
  open: (agentId?: string | null) => void;
  close: () => void;
}

export const useAgentEditModalStore = create<AgentEditModalState>((set) => ({
  agentId: undefined,
  open: (agentId) => set({ agentId: agentId ?? null }),
  close: () => set({ agentId: undefined }),
}));

/** Convenience helpers for non-hook call sites. */
export const openAgentEditModal = (agentId?: string | null) =>
  useAgentEditModalStore.getState().open(agentId);
export const closeAgentEditModal = () => useAgentEditModalStore.getState().close();
