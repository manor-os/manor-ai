import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { workflowPath, workflowRunPath } from "../../lib/workflowRoutes";
import { t } from "../../lib/i18n";
import Button from "../ui/Button";
import LoadingSpinner from "../ui/LoadingSpinner";

export default function WorkflowLegacyRedirect({ children }: { children: ReactNode }) {
  const location = useLocation();
  const query = new URLSearchParams(location.search);
  const workflowId = query.get("workflow");
  const runId = query.get("run") || query.get("workflow_run");
  const run = useQuery({
    queryKey: ["workflow-legacy-route", runId],
    queryFn: () => api.workflows.getRun(runId!, false),
    enabled: Boolean(runId),
    retry: false,
  });
  if (!workflowId && !runId) return <>{children}</>;
  if (runId && run.isPending) return <LoadingSpinner size={24} />;
  if (runId && run.isError) return <section role="alert" className="workflow-run-history-list-state is-error">
    <p>{t("component.workflow_run_history.detail_load_error")}</p>
    <Button onClick={() => void run.refetch()}>{t("component.workflow_run_history.try_again")}</Button>
  </section>;
  const path = runId
    ? workflowRunPath(run.data.workflow_id, runId, run.data.workspace_id)
    : workflowPath(workflowId!);
  query.delete("workflow");
  query.delete("run");
  query.delete("workflow_run");
  return <Navigate to={`${path}${query.size ? `?${query}` : ""}${location.hash}`} state={location.state} replace />;
}
