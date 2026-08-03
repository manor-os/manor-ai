import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { formatDateLong, relativeTime } from "../../lib/format";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel, formatUserFacingText } from "../../lib/taskDisplay";
import { useToastStore } from "../../stores/toast";
import { IconClock, IconFlow, IconPlay, IconTrash } from "../icons";
import Button from "../ui/Button";
import CompactCard from "../ui/CompactCard";
import ConfirmDialog from "../ui/ConfirmDialog";
import EmptyState from "../ui/EmptyState";
import IconTile from "../ui/IconTile";
import LoadingSpinner from "../ui/LoadingSpinner";
import Modal from "../ui/Modal";
import PageHeader, { PageHeaderAddButton } from "../ui/PageHeader";
import Select from "../ui/Select";
import TabSwitcher from "../ui/TabSwitcher";
import WorkflowRunDetail from "./WorkflowRunDetail";
import {
  formatWorkflowDuration,
  formatWorkflowError,
  groupWorkflowRunFamilies,
  sortWorkflowRunsNewestFirst,
  workflowRunHasImmutableListMetadata,
  workflowRunDurationMs,
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
  const [searchParams, setSearchParams] = useSearchParams();
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [attachOpen, setAttachOpen] = useState(false);
  const [selectedWorkflowId, setSelectedWorkflowId] = useState("");
  const [detachTarget, setDetachTarget] = useState<WorkflowBinding | null>(null);
  const requestedWorkflowView = searchParams.get("workflow_view");
  const requestedRunId = searchParams.get("workflow_run") || "";
  const [section, setSection] = useState<"attached" | "history">(
    requestedWorkflowView === "history" || requestedRunId ? "history" : "attached",
  );
  const [selectedRunId, setSelectedRunId] = useState(requestedRunId);
  const historyRowRefs = useRef(new Map<string, HTMLButtonElement>());

  const bindingsKey = useMemo(() => ["workflow-bindings", workspaceId], [workspaceId]);
  const runsKey = useMemo(() => ["workspace-workflow-runs", workspaceId], [workspaceId]);
  const { data: workflows = [], isLoading: workflowsLoading } = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api.workflows.list(),
  });
  const { data: bindings = [], isLoading: bindingsLoading } = useQuery({
    queryKey: bindingsKey,
    queryFn: () => api.workflows.listBindings({ workspace_id: workspaceId }),
  });
  const {
    data: runs = [],
    isLoading: runsLoading,
    isError: runsUnavailable,
    error: runsError,
  } = useQuery({
    queryKey: runsKey,
    queryFn: () => api.workflows.listRuns({ workspace_id: workspaceId, limit: 100 }),
    refetchInterval: (query) => {
      const items = (query.state.data as WorkflowHistoryRun[] | undefined) || [];
      return items.some((run) => run.status === "running" || run.status === "pending") ? 1_000 : false;
    },
    refetchIntervalInBackground: true,
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
        config: { workspace_attached: true },
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
    mutationFn: (bindingId: string) => api.workflows.runBinding(bindingId, {
      execute: false,
      trigger_data: { workspace_manual: true },
    }),
    onSuccess: (run: WorkflowHistoryRun) => {
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

  const openAttach = () => {
    setSelectedWorkflowId(availableWorkflows[0]?.id || "");
    setAttachOpen(true);
  };
  useEffect(() => {
    const hasHistoryLocation = requestedWorkflowView === "history" || Boolean(requestedRunId);
    setSection(hasHistoryLocation ? "history" : "attached");
    setSelectedRunId(hasHistoryLocation ? requestedRunId : "");
  }, [requestedRunId, requestedWorkflowView]);

  const updateHistoryLocation = (nextSection: "attached" | "history", runId = "") => {
    const next = new URLSearchParams(searchParams);
    if (nextSection === "history") next.set("workflow_view", "history");
    else next.delete("workflow_view");
    if (nextSection === "history" && runId) next.set("workflow_run", runId);
    else next.delete("workflow_run");
    setSearchParams(next, { replace: true });
  };
  const openHistoryRun = (runId: string) => {
    setSection("history");
    setSelectedRunId(runId);
    updateHistoryLocation("history", runId);
  };
  const isLoading = workflowsLoading || bindingsLoading;
  const selectedRun = historyRuns.find((run) => run.id === selectedRunId);
  const selectedRunWorkflow = selectedRun?.workflow_id
    ? workflowById.get(selectedRun.workflow_id)
    : undefined;

  const showHistoryList = () => {
    const previousRunId = selectedRunId;
    setSelectedRunId("");
    updateHistoryLocation("history");
    window.requestAnimationFrame(() => historyRowRefs.current.get(previousRunId)?.focus());
  };

  return (
    <div className="workspace-workflows">
      <PageHeader
        inline
        title="Workflows"
        subtitle="Attach reusable workflows to this workspace, run them directly, and use them from tasks, agents, APIs, or automations."
        actions={canManage && section === "attached" ? <PageHeaderAddButton label="Attach workflow" onClick={openAttach} /> : undefined}
      />

      <div className="workspace-workflow-section-tabs" aria-label={t("component.workflow_run_history.views")}>
        <TabSwitcher
          size="sm"
          value={section}
          onChange={(nextSection) => {
            setSection(nextSection as "attached" | "history");
            setSelectedRunId("");
            updateHistoryLocation(nextSection as "attached" | "history");
          }}
          tabs={[
            { key: "attached", label: t("component.workflow_run_history.attached"), count: attachedBindings.length },
            { key: "history", label: t("component.workflow_run_history.history"), count: historyFamilies.length },
          ]}
        />
      </div>

      {section === "attached" ? (
        isLoading ? (
          <div className="workspace-workflows-loading"><LoadingSpinner size={24} /></div>
        ) : attachedBindings.length === 0 ? (
          <EmptyState
            icon={<IconFlow size={30} />}
            title="No workflows attached"
            description="Attach a workflow to make it available in this workspace without requiring an automation."
            action={canManage ? <Button size="sm" onClick={openAttach}>Attach workflow</Button> : undefined}
          />
        ) : (
          <div className="workspace-workflows-grid">
            {attachedBindings.map((binding) => {
              const workflow = workflowById.get(binding.workflow_id);
              const lastRun = latestRunByBinding.get(binding.id);
              const running = lastRun?.status === "running" || lastRun?.status === "pending";
              const duration = runDuration(lastRun);
              const runLabel = lastRun
                ? `${lastRun.status}${duration ? ` · ${duration}` : ""}${lastRun.started_at ? ` · ${relativeTime(lastRun.started_at)}` : ""}`
                : "Not run in this workspace yet";
              return (
                <CompactCard
                  key={binding.id}
                  className="workspace-workflow-card"
                  onClick={() => navigate(`/flows?workflow=${encodeURIComponent(binding.workflow_id)}`)}
                  icon={<IconTile size={36}><IconFlow size={18} /></IconTile>}
                  title={formatUserFacingText(binding.name || workflow?.name || "Workspace workflow")}
                  subtitle={String(lastRun?.error || workflow?.description || (runsUnavailable ? "Run history unavailable" : runLabel))}
                  meta={(
                    <span className={`workspace-workflow-run-state is-${lastRun?.status || "idle"}`}>
                      {runLabel}
                    </span>
                  )}
                  action={(
                    <span className="workspace-workflow-card-actions">
                        <Button
                          variant="ghost"
                          size="sm"
                          loading={running || (runMutation.isPending && runMutation.variables === binding.id)}
                          disabled={!binding.enabled || binding.status !== "active"}
                          onClick={() => runMutation.mutate(binding.id)}
                          ariaLabel={`Run ${binding.name || workflow?.name || "workflow"}`}
                          title="Run in this workspace"
                          style={{ width: 30, padding: 0 }}
                        ><IconPlay size={14} /></Button>
                        {canManage && (
                          <Button
                            variant="ghost"
                            size="sm"
                            onClick={() => setDetachTarget(binding)}
                            ariaLabel={`Detach ${binding.name || workflow?.name || "workflow"}`}
                            title="Detach from workspace"
                          style={{ width: 30, padding: 0 }}
                        ><IconTrash size={13} /></Button>
                        )}
                    </span>
                  )}
                />
              );
            })}
          </div>
        )
      ) : selectedRunId ? (
        <WorkflowRunDetail
          runId={selectedRunId}
          workspaceId={workspaceId}
              workflow={selectedRunWorkflow}
              onBack={showHistoryList}
              onSelectRun={openHistoryRun}
        />
      ) : runsLoading || workflowsLoading ? (
        <div className="workspace-workflows-loading" aria-live="polite">
          <LoadingSpinner size={24} />
          <span>{t("component.workflow_run_history.loading")}</span>
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
            const currentNode = workflow?.steps?.find((step) => step.id === run.current_step_id);
            const immutableListMetadata = workflowRunHasImmutableListMetadata(run);
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
            return (
              <button
                type="button"
                className="workflow-run-history-row"
                key={family.id}
                data-run-id={run.id}
                ref={(node) => {
                  if (node) historyRowRefs.current.set(run.id, node);
                  else historyRowRefs.current.delete(run.id);
                }}
                onClick={() => openHistoryRun(run.id)}
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
                      {family.totalCount > 0
                        ? t("component.workflow_run_history.steps_progress", {
                            count: family.processedCount,
                            total: family.totalCount,
                          })
                        : t("component.workflow_run_history.progress_in_details")}
                    </span>
                  </span>
                </span>
                <span className="workflow-run-history-status" data-status={statusPresentation.iconStatus}>
                  <span aria-hidden="true" />
                  {t(`component.workflow_run.status.${statusPresentation.labelKey}`)}
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
            );
          })}
        </div>
      )}

      <Modal
        open={attachOpen}
        onClose={() => setAttachOpen(false)}
        title="Attach workflow"
        footer={(
          <>
            <Button variant="outline" onClick={() => setAttachOpen(false)}>Cancel</Button>
            <Button
              onClick={() => attachMutation.mutate(selectedWorkflowId)}
              disabled={!selectedWorkflowId}
              loading={attachMutation.isPending}
            >Attach workflow</Button>
          </>
        )}
      >
        {availableWorkflows.length > 0 ? (
          <div className="workspace-workflows-attach-form">
            <label className="manor-label">Workflow</label>
            <Select
              value={selectedWorkflowId}
              onChange={setSelectedWorkflowId}
              filterable
              placeholder="Select a workflow"
              options={availableWorkflows.map((workflow) => ({ value: workflow.id, label: workflow.name }))}
            />
            <p>Attaching gives this workflow the Workspace context, connectors, knowledge, approvals, and budget when it runs.</p>
          </div>
        ) : (
          <EmptyState
            icon={<IconFlow size={28} />}
            title="All available workflows are attached"
            description="Create another workflow or detach an existing one first."
            action={<Link to="/flows">Open Workflows</Link>}
          />
        )}
      </Modal>

      <ConfirmDialog
        open={!!detachTarget}
        onClose={() => setDetachTarget(null)}
        onConfirm={() => { if (detachTarget) detachMutation.mutate(detachTarget.id); }}
        title="Detach workflow"
        message="This removes the workflow from this workspace. Remove any automations that reference it before detaching. The workflow definition is kept."
        confirmLabel="Detach"
        danger
      />
    </div>
  );
}
