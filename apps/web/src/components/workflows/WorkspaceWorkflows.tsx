import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { formatDateLong, relativeTime } from "../../lib/format";
import { t } from "../../lib/i18n";
import { workflowPath, workflowRunPath } from "../../lib/workflowRoutes";
import { formatUserFacingLabel, formatUserFacingText } from "../../lib/taskDisplay";
import { useToastStore } from "../../stores/toast";
import { workflowRunInputs } from "../../lib/workflowRunInputs";
import { displayReadInterval } from "../../lib/readRefreshPolicy";
import { usePageVisible } from "../../lib/usePageVisible";
import { workflowRunListRefetchInterval } from "../../lib/workspaceLiveRefresh";
import { IconClock, IconFlow, IconPlay, IconStop, IconTrash } from "../icons";
import Button from "../ui/Button";
import ConfirmDialog from "../ui/ConfirmDialog";
import EmptyState from "../ui/EmptyState";
import { ConfigAddRow, ConfigBlock, ConfigGroup, ConfigRow } from "../ui/GroupedList";
import IconTile from "../ui/IconTile";
import LoadingSpinner from "../ui/LoadingSpinner";
import Modal from "../ui/Modal";
import Select from "../ui/Select";
import WorkflowRunInputModal from "./WorkflowRunInputModal";
import {
  canCancelWorkflowRun,
  formatWorkflowDuration,
  formatWorkflowError,
  groupWorkflowRunFamilies,
  sortWorkflowRunsNewestFirst,
  workflowRunHasImmutableListMetadata,
  workflowRunDurationMs,
  workflowRunProgressPercent,
  workflowRunStatusPresentation,
  type WorkflowHistoryNode,
  type WorkflowHistoryRun,
} from "./workflowRunDisplay";

interface WorkflowSummary {
  id: string;
  name: string;
  description?: string;
  status: string;
  is_active?: boolean;
  trigger_type?: string;
  version?: number;
  steps?: WorkflowHistoryNode[];
  variables?: Record<string, unknown>;
}

function isOperatorBinding(binding: WorkflowBinding): boolean {
  const chatEntryPoint = binding.config?.chat_entrypoint as Record<string, unknown> | undefined;
  return binding.trigger_type === "manual" || chatEntryPoint?.enabled === true;
}

interface WorkflowBinding {
  id: string;
  workflow_id: string;
  workspace_id?: string;
  name?: string;
  trigger_type: string;
  config?: Record<string, unknown>;
  enabled: boolean;
  status: string;
}

interface WorkspaceWorkflowsProps {
  workspaceId: string;
  canManage?: boolean;
}

function runDuration(run?: WorkflowHistoryRun): string | null {
  if (!run) return null;
  const duration = workflowRunDurationMs(run);
  return duration == null ? null : formatWorkflowDuration(duration);
}

export default function WorkspaceWorkflows({ workspaceId, canManage = false }: WorkspaceWorkflowsProps) {
  const navigate = useNavigate();
  const location = useLocation();
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const visible = usePageVisible();
  const [attachOpen, setAttachOpen] = useState(false);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState("");
  const [detachTarget, setDetachTarget] = useState<WorkflowBinding | null>(null);
  const [cancelTarget, setCancelTarget] = useState<WorkflowHistoryRun | null>(null);
  const [runInputBinding, setRunInputBinding] = useState<WorkflowBinding | null>(null);
  const [runInputFlow, setRunInputFlow] = useState<any>(null);
  const requestedWorkflowView = searchParams.get("workflow_view");
  const requestedRunId = searchParams.get("workflow_run") || "";
  const [section, setSection] = useState<"attached" | "history">(
    requestedWorkflowView === "history" || requestedRunId ? "history" : "attached",
  );
  const historyParams = new URLSearchParams(searchParams);
  historyParams.set("workflow_view", "history");
  historyParams.delete("workflow_run");
  const historyReturnTo = `${location.pathname}?${historyParams}${location.hash}`;

  const bindingsKey = useMemo(() => ["workflow-bindings", workspaceId], [workspaceId]);
  const runsKey = useMemo(() => ["workspace-workflow-runs", workspaceId], [workspaceId]);
  const { data: workflows = [], isLoading: workflowsLoading } = useQuery({
    queryKey: ["workflows", "summary"],
    queryFn: () => api.workflows.listSummaries(),
    enabled: visible,
  });
  const { data: bindings = [], isLoading: bindingsLoading } = useQuery({
    queryKey: bindingsKey,
    queryFn: () => api.workflows.listBindings({ workspace_id: workspaceId }),
    enabled: visible,
  });
  const {
    data: runs = [],
    isLoading: runsLoading,
    isError: runsUnavailable,
    error: runsError,
  } = useQuery({
    queryKey: runsKey,
    queryFn: ({ signal }) => api.workflows.listRuns({ workspace_id: workspaceId, limit: 100 }, signal),
    enabled: visible,
    retry: false,
    refetchInterval: (query) => {
      const items = (query.state.data as WorkflowHistoryRun[] | undefined) || [];
      return displayReadInterval(
        query.state,
        workflowRunListRefetchInterval(items),
      );
    },
    refetchOnWindowFocus: "always",
  });

  const attachedBindings = useMemo(
    () => (bindings as WorkflowBinding[]).filter(isOperatorBinding),
    [bindings],
  );
  const attachedWorkflowIds = useMemo(
    () => new Set(attachedBindings.map((binding) => binding.workflow_id)),
    [attachedBindings],
  );
  const availableWorkflows = useMemo(
    () => (workflows as WorkflowSummary[]).filter(
      (workflow) => workflow.trigger_type !== "internal"
        && workflow.status === "active"
        && workflow.is_active !== false
        && !attachedWorkflowIds.has(workflow.id),
    ),
    [attachedWorkflowIds, workflows],
  );
  // Older runs never recorded their step's name, so only those need the graphs.
  const { data: fullWorkflows = [] } = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api.workflows.list(),
    enabled: (runs as WorkflowHistoryRun[]).some((run) => run.current_step_id && !run.current_step_name),
  });
  const workflowById = useMemo(
    () => new Map((workflows as WorkflowSummary[]).map((workflow) => [workflow.id, workflow])),
    [workflows],
  );
  const historyRuns = useMemo(
    () => sortWorkflowRunsNewestFirst(runs as WorkflowHistoryRun[]),
    [runs],
  );
  const historyFamilies = useMemo(
    () => groupWorkflowRunFamilies(runs as WorkflowHistoryRun[]),
    [runs],
  );
  const latestRunByBinding = useMemo(() => {
    const latest = new Map<string, WorkflowHistoryRun>();
    for (const run of historyRuns) {
      if (run.binding_id && !latest.has(run.binding_id)) latest.set(run.binding_id, run);
    }
    return latest;
  }, [historyRuns]);

  const attachMutation = useMutation({
    mutationFn: (workflowId: string) => {
      const workflow = workflowById.get(workflowId);
      return api.workflows.createBinding({
        workflow_id: workflowId,
        workspace_id: workspaceId,
        name: workflow?.name || "Workspace workflow",
        trigger_type: "manual",
        config: {
          workspace_attached: true,
          chat_entrypoint: { enabled: true },
        },
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: bindingsKey });
      setAttachOpen(false);
      setSelectedWorkflowId("");
      toast.success("Workflow attached to workspace");
    },
    onError: (error) => toast.error(translateApiError(error, "Failed to attach workflow")),
  });
  const detachMutation = useMutation({
    mutationFn: (bindingId: string) => api.workflows.deleteBinding(bindingId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: bindingsKey });
      setDetachTarget(null);
      toast.success("Workflow detached from workspace");
    },
    onError: (error) => toast.error(translateApiError(error, "Failed to detach workflow")),
  });
  const runMutation = useMutation({
    mutationFn: ({ bindingId, triggerData = {} }: { bindingId: string; triggerData?: Record<string, unknown> }) => api.workflows.runBinding(bindingId, {
      execute: false,
      trigger_data: { ...triggerData, workspace_manual: true },
    }),
    onSuccess: (run: WorkflowHistoryRun) => {
      setRunInputBinding(null);
      queryClient.setQueryData<WorkflowHistoryRun[]>(runsKey, (current = []) => [
        run,
        ...current.filter((item) => item.id !== run.id),
      ]);
      queryClient.invalidateQueries({ queryKey: runsKey });
      toast.success("Workflow run queued");
    },
    onError: (error) => toast.error(
      "Couldn't run workflow",
      translateApiError(error, "The workflow service is unavailable. Refresh and try again."),
    ),
  });
  const requestWorkflowRun = async (binding: WorkflowBinding) => {
    if (!workflowById.has(binding.workflow_id)) return;
    // The list has no graph, and what a run asks for lives in it.
    let workflow;
    try {
      workflow = await queryClient.fetchQuery({
        queryKey: ["workflow-definition", binding.workflow_id],
        queryFn: () => api.workflows.get(binding.workflow_id),
        staleTime: 0,
      });
    } catch (error) {
      toast.error(translateApiError(error, "The workflow service is unavailable. Refresh and try again."));
      return;
    }
    if (workflowRunInputs(workflow).length) {
      setRunInputFlow(workflow);
      setRunInputBinding(binding);
      return;
    }
    runMutation.mutate({ bindingId: binding.id });
  };
  const cancelMutation = useMutation({
    mutationFn: (runId: string) => api.workflows.cancelRun(runId),
    onSuccess: async (_result, runId) => {
      setCancelTarget(null);
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: runsKey }),
        queryClient.invalidateQueries({ queryKey: ["workflow-run-history-detail", runId] }),
        queryClient.invalidateQueries({ queryKey: ["workflow-run-history-family"] }),
        queryClient.invalidateQueries({ queryKey: ["workflow-run", runId] }),
        queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] }),
      ]);
    },
  });

  const openAttach = () => {
    setSelectedWorkflowId(availableWorkflows[0]?.id || "");
    setAttachOpen(true);
  };
  useEffect(() => {
    const hasHistoryLocation = requestedWorkflowView === "history" || Boolean(requestedRunId);
    setSection(hasHistoryLocation ? "history" : "attached");
  }, [requestedRunId, requestedWorkflowView]);
  useEffect(() => {
    if (requestedRunId) {
      const remaining = new URLSearchParams(searchParams);
      remaining.delete("tab");
      remaining.delete("workflow_view");
      remaining.delete("workflow_run");
      navigate(workflowRunPath("", requestedRunId, workspaceId) + `${remaining.size ? `?${remaining}` : ""}${location.hash}`, {
        replace: true,
        state: { ...location.state,
          returnTo: location.state?.returnTo || location.state?.chatReturnTo || historyReturnTo,
          returnState: location.state?.returnState ?? location.state },
      });
    }
  }, [requestedRunId, workspaceId, navigate, historyReturnTo, searchParams, location.hash, location.state]);

  const updateHistoryLocation = (nextSection: "attached" | "history") => {
    const next = new URLSearchParams(searchParams);
    if (nextSection === "history") next.set("workflow_view", "history");
    else next.delete("workflow_view");
    next.delete("workflow_run");
    setSearchParams(next, { replace: true, state: location.state });
  };
  const openHistoryRun = (run: WorkflowHistoryRun) => {
    navigate(workflowRunPath(run.workflow_id || "", run.id, workspaceId), {
      state: { returnTo: historyReturnTo, returnState: location.state },
    });
  };
  const isLoading = workflowsLoading || bindingsLoading;
  const requestRunCancellation = (run: WorkflowHistoryRun) => {
    cancelMutation.reset();
    setCancelTarget(run);
  };

  return (
    <div className="workspace-workflows">
      <ConfigGroup title={t("nav.flows")}>
        {isLoading ? (
          <ConfigBlock><LoadingSpinner size={20} /></ConfigBlock>
        ) : attachedBindings.length === 0 ? (
          <ConfigBlock><p className="config-empty">{t("page.workspace_detail.no_workflows_attached")}</p></ConfigBlock>
        ) : attachedBindings.map((binding) => {
          const workflow = workflowById.get(binding.workflow_id);
          const lastRun = latestRunByBinding.get(binding.id);
          const running = lastRun?.status === "running" || lastRun?.status === "pending";
          const duration = runDuration(lastRun);
          const runLabel = lastRun
            ? `${lastRun.status}${duration ? ` · ${duration}` : ""}${lastRun.started_at ? ` · ${relativeTime(lastRun.started_at)}` : ""}`
            : workflow?.description || "";
          return (
            <ConfigRow
              key={binding.id}
              label={(
                <Link className="config-row-link" to={workflowPath(binding.workflow_id)} state={{ returnTo: `${location.pathname}${location.search}` }}>
                  {formatUserFacingText(binding.name || workflow?.name || "Workspace workflow")}
                </Link>
              )}
              note={String(lastRun?.error || runLabel) || undefined}
              control={(
                <span className="config-row-actions">
                  <Button
                    variant="ghost"
                    size="sm"
                    loading={running || (runMutation.isPending && runMutation.variables?.bindingId === binding.id)}
                    disabled={!canManage || !workflow || !["active", "paused"].includes(binding.status)}
                    onClick={() => requestWorkflowRun(binding)}
                    ariaLabel={`Run ${binding.name || workflow?.name || "workflow"}`}
                    title={t("ui.run_in_this_workspace")}
                    style={{ width: 30, padding: 0 }}
                  ><IconPlay size={14} /></Button>
                  {canManage && (
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => setDetachTarget(binding)}
                      ariaLabel={`Detach ${binding.name || workflow?.name || "workflow"}`}
                      title={t("ui.detach_from_workspace")}
                      style={{ width: 30, padding: 0 }}
                    ><IconTrash size={13} /></Button>
                  )}
                </span>
              )}
            />
          );
        })}
        {canManage && <ConfigAddRow label={t("page.workspace_detail.attach_workflow")} onClick={openAttach} />}
        <ConfigRow
          label={`${t("component.workflow_run_history.history")} · ${historyFamilies.length}`}
          expanded={section === "history"}
          onClick={() => {
            const nextSection = section === "history" ? "attached" : "history";
            setSection(nextSection);
            updateHistoryLocation(nextSection);
          }}
        />
      </ConfigGroup>

      {section !== "history" ? null : runsLoading || workflowsLoading ? (
        <div className="workspace-workflows-loading" aria-live="polite">
          <LoadingSpinner size={24} />
          <span className="sr-only">{t("component.workflow_run_history.loading")}</span>
        </div>
      ) : runsUnavailable ? (
        <div className="workflow-run-history-list-state is-error" role="alert">
          <IconFlow size={24} />
          <strong>{t("component.workflow_run_history.load_error")}</strong>
          <p>{translateApiError(runsError, t("component.workflow_run_history.try_again"))}</p>
        </div>
      ) : historyFamilies.length === 0 ? (
        <EmptyState
          icon={<IconClock size={30} />}
          title={t("component.workflow_run_history.empty")}
          description={t("component.workflow_run_history.empty_description")}
        />
      ) : (
        <div className="workflow-run-history-list" aria-label={t("component.workflow_run_history.history")}>
          {historyFamilies.map((family) => {
            const run = family.latestRun;
            const statusPresentation = workflowRunStatusPresentation({
              status: run.status as "pending" | "running" | "completed" | "paused" | "failed" | "skipped" | "cancelled",
              businessOutcome: family.businessOutcome,
            });
            const workflow = workflowById.get(family.latestRun.workflow_id || "");
            const currentNode = (fullWorkflows as { id: string; steps?: { id: string; name?: string }[] }[])
              .find((item) => item.id === family.latestRun.workflow_id)
              ?.steps?.find((step) => step.id === run.current_step_id);
            const immutableListMetadata = workflowRunHasImmutableListMetadata(run);
            const progressPercent = workflowRunProgressPercent(family.processedCount, family.totalCount, family.status);
            const workflowName = run.workflow_name || workflow?.name || t("component.workflow_run_history.unknown_workflow");
            const currentNodeName = run.current_step_name || currentNode?.name || run.current_step_id;
            const blocker = formatWorkflowError(
              family.blocker,
              t("component.workflow_run.error_truncated"),
            ).split("\n").find(Boolean);
            const contextLabel = blocker
              || (run.current_step_id
                ? `${run.status === "failed" ? t("component.workflow_run_history.failed_node") : t("component.workflow_run_history.current_node")}: ${formatUserFacingText(currentNodeName)}`
                : t("component.workflow_run_history.no_current_node"));
            const canCancel = canCancelWorkflowRun(run);
            return (
              <div
                className="workflow-run-history-row"
                key={family.id}
                data-run-id={run.id}
              >
                <button
                  type="button"
                  className="workflow-run-history-row-main"
                  onClick={() => openHistoryRun(run)}
                >
                  <span className="workflow-run-history-row-identity">
                    <IconTile size={34}><IconFlow size={17} /></IconTile>
                    <span>
                      <span className="workflow-run-history-row-title">
                        <strong>{formatUserFacingText(workflowName)}</strong>
                        {!immutableListMetadata && (
                          <span className="workflow-run-history-legacy-label">
                            {t("component.workflow_run_history.legacy_list_metadata")}
                          </span>
                        )}
                        {run.lineage_status === "legacy_untrusted_incomplete" && (
                          <span className="workflow-run-history-legacy-label">
                            {t("component.workflow_run_history.legacy_lineage_label")}
                          </span>
                        )}
                      </span>
                      <span className="workflow-run-history-row-progress">
                        {progressPercent !== null
                          ? `${Math.floor(progressPercent)}%`
                          : t("component.workflow_run_history.progress_in_details")}
                      </span>
                    </span>
                  </span>
                  <span className="workflow-run-history-status" data-status={statusPresentation.iconStatus}>
                    <span aria-hidden="true" />
                    {t(statusPresentation.stateKey)}
                  </span>
                  <span className="workflow-run-history-row-meta">
                    <span>{t("component.workflow_run_history.attempts", { count: family.attemptCount })}</span>
                    <span>{formatUserFacingLabel(run.trigger_source || "manual")}</span>
                    <span className="mono">{formatDateLong(family.startedAt)}</span>
                    <span className="mono">{formatWorkflowDuration(family.durationMs)}</span>
                    {family.artifactCount !== null && (
                      <span>{t("component.workflow_run_history.artifact_count", { count: family.artifactCount })}</span>
                    )}
                  </span>
                  <span className="workflow-run-history-row-context">{contextLabel}</span>
                  <IconPlay className="workflow-run-history-row-open" size={14} />
                </button>
                {canCancel && (
                  <button
                    type="button"
                    className="workflow-run-history-row-cancel"
                    title={t("component.workflow_run.action.cancel")}
                    aria-label={t("component.workflow_run.action.cancel")}
                    disabled={cancelMutation.isPending && cancelTarget?.id === run.id}
                    onClick={() => requestRunCancellation(run)}
                  >
                    {cancelMutation.isPending && cancelTarget?.id === run.id
                      ? <LoadingSpinner size={13} />
                      : <IconStop size={13} aria-hidden="true" />}
                    <span>{t("component.workflow_run.action.cancel")}</span>
                  </button>
                )}
              </div>
            );
          })}
        </div>
      )}

      <WorkflowRunInputModal
        flow={runInputBinding ? runInputFlow : null}
        workspaceId={workspaceId}
        busy={runMutation.isPending}
        onClose={() => { if (!runMutation.isPending) setRunInputBinding(null); }}
        onSubmit={async (triggerData) => {
          if (runInputBinding && !runMutation.isPending) {
            await runMutation.mutateAsync({ bindingId: runInputBinding.id, triggerData });
          }
        }}
      />

      <Modal
        open={attachOpen}
        onClose={() => setAttachOpen(false)}
        title={t("ui.attach_workflow")}
        footer={(
          <>
            <Button variant="outline" onClick={() => setAttachOpen(false)}>{t("ui.cancel")}</Button>
            <Button
              onClick={() => attachMutation.mutate(selectedWorkflowId)}
              disabled={!selectedWorkflowId}
              loading={attachMutation.isPending}
            >{t("ui.attach_workflow")}</Button>
          </>
        )}
      >
        {availableWorkflows.length > 0 ? (
          <div className="workspace-workflows-attach-form">
            <label className="manor-label">{t("ui.workflow")}</label>
            <Select
              value={selectedWorkflowId}
              onChange={setSelectedWorkflowId}
              filterable
              placeholder={t("ui.select_a_workflow")}
              options={availableWorkflows.map((workflow) => ({ value: workflow.id, label: workflow.name }))}
            />
            <p>Attaching gives this workflow the Workspace context, connectors, knowledge, approvals, and budget when it runs.</p>
          </div>
        ) : (
          <EmptyState
            icon={<IconFlow size={28} />}
            title={t("ui.all_available_workflows_are_attached")}
            description={t("ui.create_another_workflow_or_detach_an_existing_on")}
            action={<Link to="/flows">{t("ui.open_workflows")}</Link>}
          />
        )}
      </Modal>

      <ConfirmDialog
        open={!!cancelTarget}
        onClose={() => setCancelTarget(null)}
        onConfirm={() => {
          if (cancelTarget) cancelMutation.mutate(cancelTarget.id);
        }}
        title={t("component.workflow_run.cancel_confirm_title")}
        message={t("component.workflow_run.cancel_confirm_message")}
        confirmLabel={t("component.workflow_run.cancel_confirm_action")}
        cancelLabel={t("component.workflow_run.cancel_confirm_keep_running")}
        danger
        loading={cancelMutation.isPending}
        closeOnConfirm={false}
        error={cancelMutation.isError
          ? translateApiError(cancelMutation.error, t("component.workflow_run_history.cancel_error"))
          : undefined}
      />

      <ConfirmDialog
        open={!!detachTarget}
        onClose={() => setDetachTarget(null)}
        onConfirm={() => { if (detachTarget) detachMutation.mutate(detachTarget.id); }}
        title={t("ui.detach_workflow")}
        message="This removes the workflow from this workspace. Remove any automations that reference it before detaching. The workflow definition is kept."
        confirmLabel="Detach"
        danger
      />
    </div>
  );
}
