import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import ChatActionCard from "../ui/ChatActionCard";
import { workflowApprovalReviewFromRunDetail } from "./WorkspaceWorkflowRunHost";
import type { WorkflowRunAction } from "./workflowRunDisplay";

/** A Workflow review in Workspace Chat: the deliverables, then Accept or a
 * change request whose note reaches the run. The review is read from Workflow
 * history when the message only points there. */
export default function WorkflowReviewChatCard({ workspaceId, action, disabled, currentUserName, promptShown, onResolve }: {
  workspaceId: string;
  promptShown?: boolean;
  action: WorkflowRunAction;
  disabled?: boolean;
  currentUserName?: string;
  onResolve: (choice: string, note?: string, payload?: Record<string, any>) => void;
}) {
  const runId = String(action.workflow_run_id || "");
  const fromHistory = action.review == null && action.review_location === "workflow_history";
  // The run's own record links each reviewed file to its Document; the
  // message keeps the copy made when it was posted.
  const detail = useQuery({
    queryKey: ["workflow-run-review", runId, action.step_id, "chat"],
    queryFn: ({ signal }) => api.workflows.getRun(runId, true, signal),
    enabled: Boolean(runId),
    staleTime: 60_000,
  });
  const hydrated = fromHistory ? workflowApprovalReviewFromRunDetail(detail.data, action) : null;
  const recorded = (detail.data?.step_results?.[String(action.step_id || "")] as { review?: unknown } | undefined)?.review;
  const review = hydrated?.review ?? recorded ?? action.review ?? {};
  return (
    <ChatActionCard
      workspaceId={workspaceId}
      action={{
        ...action,
        review,
        review_title: action.review_title ?? hydrated?.reviewTitle,
      }}
      resolved={false}
      resolution={null}
      currentUserName={currentUserName}
      promptShown={promptShown}
      disabled={disabled}
      onResolve={(choice, note, payload) => onResolve(choice, note, payload)}
    />
  );
}
