import { createContext, useContext } from "react";

/**
 * The open design draft whose App page is shown in place of the saved one:
 * the candidate page, read with the Workspace's real data before anyone
 * approves it. Every App read below it passes this id along.
 */
export const WorkspaceAppDraftContext = createContext<string | null>(null);

export function useWorkspaceAppDraftId(): string | null {
  return useContext(WorkspaceAppDraftContext);
}
