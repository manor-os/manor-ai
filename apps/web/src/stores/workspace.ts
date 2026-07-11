import { create } from "zustand";

interface WorkspaceFilterState {
  /** "all" or a workspace ID. Pages use this to filter their queries. */
  activeWorkspaceId: string;
  setActiveWorkspaceId: (id: string) => void;
}

export const useWorkspaceFilter = create<WorkspaceFilterState>((set) => ({
  activeWorkspaceId: "all",
  setActiveWorkspaceId: (id) => set({ activeWorkspaceId: id }),
}));
