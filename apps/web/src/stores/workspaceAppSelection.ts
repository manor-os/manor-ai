import { create } from "zustand";

/**
 * The rows a person ticked in each App list, keyed by ``workspace:component:block``. The list keeps it while
 * it is on screen; whatever acts on several records at once (bulk actions) reads it from here.
 */
interface WorkspaceAppSelectionState {
  selected: Record<string, string[]>;
  setSelected: (list: string, ids: string[]) => void;
  clear: (list: string) => void;
}

export const useWorkspaceAppSelection = create<WorkspaceAppSelectionState>((set) => ({
  selected: {},
  setSelected: (list, ids) => set((state) => ({ selected: { ...state.selected, [list]: ids } })),
  clear: (list) => set((state) => {
    const { [list]: _cleared, ...rest } = state.selected;
    return { selected: rest };
  }),
}));

const EMPTY: string[] = [];

/** The ids ticked in one list, in the order they were ticked. */
export function useSelectedIds(list: string): string[] {
  return useWorkspaceAppSelection((state) => state.selected[list] ?? EMPTY);
}
