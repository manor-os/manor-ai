import { Navigate, useLocation, useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ApiError, api } from "../lib/api";
import { t } from "../lib/i18n";
import { displayReadInterval } from "../lib/readRefreshPolicy";
import { usePageVisible } from "../lib/usePageVisible";
import { selectedWorkflowRunRefetchInterval } from "../lib/workspaceLiveRefresh";
import { workflowPath, workflowRunPath } from "../lib/workflowRoutes";
import Flows from "./Flows";
import type { WorkflowHistoryRun } from "../components/workflows/workflowRunDisplay";
import Button from "../components/ui/Button";
import LoadingSpinner from "../components/ui/LoadingSpinner";

export default function WorkflowRun() {
  const { workspaceId, workflowId, runId = "" } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const visible = usePageVisible();
  const detail = useQuery({
    queryKey: ["workflow-run-history-detail", runId],
    queryFn: ({ signal }) => api.workflows.getRun(runId, true, signal),
    enabled: Boolean(runId) && visible,
    retry: false,
    refetchInterval: (query) => {
      const status = String(query.state.data?.status || "");
      return displayReadInterval(query.state, selectedWorkflowRunRefetchInterval(status));
    },
    refetchOnWindowFocus: "always",
  });
  const run = detail.data as WorkflowHistoryRun | undefined;
  const readDenied = detail.error instanceof ApiError
    && detail.error.status >= 400 && detail.error.status < 500
    && ![408, 429].includes(detail.error.status);
  const mismatch = Boolean(run && (
    (workspaceId && run.workspace_id !== workspaceId)
    || (workflowId && run.workflow_id !== workflowId)
  ));
  const state = location.state as { returnTo?: string; chatReturnTo?: string; returnState?: unknown } | null;
  const requestedReturnTo = state?.returnTo || state?.chatReturnTo;
  const returnTo = requestedReturnTo?.startsWith("/") && !requestedReturnTo.startsWith("//")
    ? requestedReturnTo
    : (workspaceId || run?.workspace_id)
      ? `/workspaces/${encodeURIComponent(workspaceId || run!.workspace_id!)}?tab=workflows&workflow_view=history`
      : workflowId ? workflowPath(workflowId) : "/flows";
  const goBack = () => navigate(returnTo, { state: state?.returnState });
  if (detail.isPending) return <div className="workspace-workflows-loading"><LoadingSpinner size={24} /></div>;
  // A transient background failure must not unmount an already loaded run.
  // Explicit access/missing-resource responses still hide the cached detail.
  if (!run || mismatch || readDenied) return <section role="alert" className="workflow-run-history-list-state is-error">
    <p>{mismatch ? t("component.workspace_app_view.run_mismatch") : t("component.workflow_run_history.detail_load_error")}</p>
    <Button variant="ghost" onClick={goBack}>{t("component.workflow_run_history.back")}</Button>
    {!mismatch && <Button onClick={() => void detail.refetch()}>{t("component.workflow_run_history.try_again")}</Button>}
  </section>;
  if (workspaceId) return <Navigate
    to={`${workflowRunPath(run.workflow_id || "", run.id)}${location.search}${location.hash}`}
    state={{ ...state, returnTo, returnState: state?.returnState }} replace
  />;
  return <Flows key={runId} run={run} onBack={goBack} />;
}
