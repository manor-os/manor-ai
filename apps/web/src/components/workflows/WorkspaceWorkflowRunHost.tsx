import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { t } from "../../lib/i18n";
import { IconStop } from "../icons";
import ConfirmDialog from "../ui/ConfirmDialog";
import LoadingSpinner from "../ui/LoadingSpinner";
import Select from "../ui/Select";
import WorkflowRunIntervention from "./WorkflowRunIntervention";
import WorkflowRunProgress from "./WorkflowRunProgress";
import {
  formatWorkflowError,
  isWorkflowRunActive,
  workflowRunStatusPresentation,
  type WorkflowRunAction,
  type WorkflowRunNode,
  type WorkflowRunStatus,
  type WorkflowRunView,
} from "./workflowRunDisplay";

/* Workspace workflow run host helpers */

export interface WorkspaceWorkflowRunMessageRef {
  type: string;
  id: string;
  title?: string;
  name?: string;
}

export interface WorkspaceWorkflowRunMessage {
  id: string;
  created_at?: string;
  updated_at?: string | null;
  message_kind?: string;
  refs?: WorkspaceWorkflowRunMessageRef[] | null;
  meta?: Record<string, unknown> | null;
  pending_action?: WorkflowRunAction | null;
  resolved_at?: string | null;
}

export interface WorkspaceWorkflowRunGroup {
  id: string;
  latestIndex: number;
  latestCreatedAt: string;
  activityMessage: WorkspaceWorkflowRunMessage | null;
  actionMessage: WorkspaceWorkflowRunMessage | null;
  actionMessages: WorkspaceWorkflowRunMessage[];
  ownedMessageIds: string[];
  projectionUpdatedAt: string | null;
  retryOfRunId: string | null;
  projection: WorkflowRunView;
}

export const WORKFLOW_HOST_ACTION_KINDS = new Set([
  "workflow_starter_input",
  "workflow_retry",
  "workflow_approval",
  "workflow_input",
]);

const ACTIONABLE_RUN_STATUSES = new Set([
  "queued",
  "pending",
  "running",
  "paused",
  "failed",
]);
const ACTIONABLE_COMPLETED_OUTCOMES = new Set([
  "needs_input",
]);
const NODE_STATUSES = new Set([
  "pending",
  "running",
  "completed",
  "paused",
  "failed",
  "skipped",
  "cancelled",
]);

function recordValue(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : null;
}

function nonEmptyString(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function normalizeWorkflowRunStatus(value: unknown): WorkflowRunStatus {
  const status = nonEmptyString(value).toLowerCase();
  if (status === "queued") return "pending";
  return NODE_STATUSES.has(status) ? status as WorkflowRunStatus : "pending";
}

function normalizeWorkflowNodeStatus(value: unknown): WorkflowRunStatus {
  return normalizeWorkflowRunStatus(value);
}

export function workflowRunIdForMessage(message: WorkspaceWorkflowRunMessage): string {
  const actionRunId = nonEmptyString(message.pending_action?.workflow_run_id);
  if (actionRunId) return actionRunId;
  const metadataRunId = nonEmptyString(message.meta?.workflow_run_id);
  if (metadataRunId) return metadataRunId;
  return nonEmptyString(
    (message.refs || []).find((ref) => ref.type === "workflow_run")?.id,
  );
}

function workflowTitleForMessage(message: WorkspaceWorkflowRunMessage | null): string {
  if (!message) return "";
  const metadataTitle = nonEmptyString(message.meta?.workflow_title);
  if (metadataTitle) return metadataTitle;
  const actionTitle = nonEmptyString(message.pending_action?.title);
  if (actionTitle) return actionTitle;
  const workflowRef = (message.refs || []).find((ref) => ref.type === "workflow");
  return nonEmptyString(workflowRef?.title || workflowRef?.name);
}

function workflowIdForMessage(message: WorkspaceWorkflowRunMessage | null): string | undefined {
  if (!message) return undefined;
  const workflowRef = (message.refs || []).find((ref) => ref.type === "workflow");
  return nonEmptyString(workflowRef?.id) || undefined;
}

function workflowProjectionForGroup(
  id: string,
  activityMessage: WorkspaceWorkflowRunMessage | null,
  actionMessage: WorkspaceWorkflowRunMessage | null,
): WorkflowRunView {
  const meta = activityMessage?.meta || {};
  const action: WorkflowRunAction | null = actionMessage?.pending_action
    ? {
        ...actionMessage.pending_action,
        message_id: actionMessage.id,
        source: "workspace_chat",
      }
    : null;
  const rawSteps = Array.isArray(meta.workflow_steps) ? meta.workflow_steps : [];
  const nodes: WorkflowRunNode[] = rawSteps.flatMap((rawStep, index) => {
    const step = recordValue(rawStep);
    const stepId = nonEmptyString(step?.id);
    if (!stepId) return [];
    return [{
      id: stepId,
      name: nonEmptyString(step?.name) || stepId,
      status: normalizeWorkflowNodeStatus(step?.status),
      type: nonEmptyString(step?.type) || undefined,
      order: index,
      subscriptionId: nonEmptyString(step?.subscription_id) || null,
      error: step?.error,
    } satisfies WorkflowRunNode];
  });
  const actionKind = nonEmptyString(action?.kind);
  const actionOutcome = nonEmptyString(action?.business_outcome).toLowerCase();
  const fallbackStatus = actionKind === "workflow_retry" && actionOutcome
    ? "completed"
    : actionKind
      ? "paused"
      : "pending";
  const actionStepId = nonEmptyString(action?.step_id || action?.retry_from_step_id);
  if (nodes.length === 0 && actionStepId) {
    nodes.push({
      id: actionStepId,
      name: nonEmptyString(action?.title) || actionStepId,
      status: actionKind === "workflow_retry" ? "failed" : "paused",
    });
  }
  return {
    id,
    title: workflowTitleForMessage(activityMessage) || workflowTitleForMessage(actionMessage),
    status: normalizeWorkflowRunStatus(meta.workflow_status || fallbackStatus),
    nodes,
    workflowId: workflowIdForMessage(activityMessage) || workflowIdForMessage(actionMessage),
    currentNodeId: nonEmptyString(meta.workflow_current_step_id || actionStepId) || null,
    attemptNumber: Math.max(1, Number(meta.workflow_attempt_number || 1)),
    startedAt: activityMessage?.created_at || null,
    businessOutcome: nonEmptyString(
      meta.workflow_business_outcome || action?.business_outcome,
    ).toLowerCase() || null,
    error: meta.workflow_error,
    action,
  };
}

export function buildWorkspaceWorkflowRunGroups(
  messages: WorkspaceWorkflowRunMessage[],
): WorkspaceWorkflowRunGroup[] {
  const pendingGroups = new Map<string, {
    id: string;
    latestIndex: number;
    latestCreatedAt: string;
    activityMessage: WorkspaceWorkflowRunMessage | null;
    actionMessage: WorkspaceWorkflowRunMessage | null;
    actionMessages: WorkspaceWorkflowRunMessage[];
    ownedMessageIds: string[];
    projectionUpdatedAt: string | null;
    retryOfRunId: string | null;
  }>();

  messages.forEach((message, index) => {
    const runId = workflowRunIdForMessage(message);
    if (!runId) return;
    const ownsActivity = message.message_kind === "workflow_activity";
    const ownsAction = WORKFLOW_HOST_ACTION_KINDS.has(
      nonEmptyString(message.pending_action?.kind),
    );
    if (!ownsActivity && !ownsAction) return;

    const group = pendingGroups.get(runId) || {
      id: runId,
      latestIndex: index,
      latestCreatedAt: message.created_at || "",
      activityMessage: null,
      actionMessage: null,
      actionMessages: [],
      ownedMessageIds: [],
      projectionUpdatedAt: null,
      retryOfRunId: null,
    };
    group.latestIndex = index;
    group.latestCreatedAt = message.created_at || group.latestCreatedAt;
    group.ownedMessageIds.push(message.id);
    const projectionUpdatedAt = nonEmptyString(message.updated_at);
    if (
      projectionUpdatedAt
      && (!group.projectionUpdatedAt || projectionUpdatedAt > group.projectionUpdatedAt)
    ) {
      group.projectionUpdatedAt = projectionUpdatedAt;
    }
    const retryOfRunId = nonEmptyString(
      message.meta?.workflow_retry_of_run_id || message.pending_action?.retry_of_run_id,
    );
    if (retryOfRunId) group.retryOfRunId = retryOfRunId;
    if (ownsActivity) group.activityMessage = message;
    if (ownsAction) {
      group.actionMessages.push(message);
      if (!message.resolved_at) group.actionMessage = message;
    }
    pendingGroups.set(runId, group);
  });

  return [...pendingGroups.values()].map((group) => ({
    ...group,
    projection: workflowProjectionForGroup(
      group.id,
      group.activityMessage,
      group.actionMessage,
    ),
  }));
}

export function workflowHostOwnedMessageIds(
  groups: WorkspaceWorkflowRunGroup[],
): Set<string> {
  return new Set(groups.flatMap((group) => group.ownedMessageIds));
}

export function excludeSupersededWorkflowRunGroups(
  groups: WorkspaceWorkflowRunGroup[],
  detailsByRunId: Record<string, unknown> = {},
): WorkspaceWorkflowRunGroup[] {
  const latestIndexByRunId = new Map(groups.map((group) => [group.id, group.latestIndex]));
  const supersededRunIds = new Set<string>();
  for (const group of groups) {
    const detail = recordValue(detailsByRunId[group.id]);
    const retryOfRunId = nonEmptyString(detail?.retry_of_run_id) || group.retryOfRunId || "";
    const parentIndex = latestIndexByRunId.get(retryOfRunId);
    if (retryOfRunId && parentIndex !== undefined && group.latestIndex > parentIndex) {
      supersededRunIds.add(retryOfRunId);
    }
  }
  return groups.filter((group) => !supersededRunIds.has(group.id));
}

export function isWorkspaceWorkflowRunActionable(
  run: { status?: unknown; businessOutcome?: unknown; business_outcome?: unknown },
): boolean {
  const status = nonEmptyString(run.status).toLowerCase();
  if (ACTIONABLE_RUN_STATUSES.has(status)) return true;
  if (status !== "completed") return false;
  const outcome = nonEmptyString(run.businessOutcome || run.business_outcome).toLowerCase();
  return ACTIONABLE_COMPLETED_OUTCOMES.has(outcome);
}

export function actionableWorkflowRunGroups(
  groups: WorkspaceWorkflowRunGroup[],
  detailsByRunId: Record<string, unknown> = {},
  dismissedRunIds: ReadonlySet<string> = new Set(),
): WorkspaceWorkflowRunGroup[] {
  return excludeSupersededWorkflowRunGroups(groups, detailsByRunId).filter((group) => (
    !dismissedRunIds.has(group.id)
    && isWorkspaceWorkflowRunActionable(group.projection)
  ));
}

export function selectForegroundWorkflowRunId(
  groups: Array<Pick<WorkspaceWorkflowRunGroup, "id" | "latestIndex" | "projection" | "actionMessage">>,
  preferredRunId: string,
): string {
  const actionable = groups.filter((group) => (
    isWorkspaceWorkflowRunActionable(group.projection)
  ));
  if (actionable.some((group) => group.id === preferredRunId)) return preferredRunId;
  return [...actionable].sort((left, right) => right.latestIndex - left.latestIndex)[0]?.id || "";
}

function businessOutcomeFromRunDetail(detail: Record<string, unknown>): string {
  const variables = recordValue(detail.variables);
  const project = recordValue(variables?.project);
  const state = recordValue(project?.state);
  return nonEmptyString(
    state?.business_outcome || detail.business_outcome,
  ).toLowerCase();
}

function serverNodeStatus(
  nodeId: string,
  detail: Record<string, unknown>,
  projectionStatus: WorkflowRunStatus | undefined,
): WorkflowRunStatus {
  const results = recordValue(detail.step_results);
  const result = recordValue(results?.[nodeId]);
  const resultStatus = nonEmptyString(result?.status).toLowerCase();
  if (NODE_STATUSES.has(resultStatus)) return resultStatus as WorkflowRunStatus;
  if (result?.skipped) return "skipped";
  const currentNodeId = nonEmptyString(detail.current_step_id);
  const runStatus = normalizeWorkflowRunStatus(detail.status);
  if (currentNodeId === nodeId && ["running", "paused", "failed"].includes(runStatus)) {
    return runStatus;
  }
  if (nonEmptyString(detail.status).toLowerCase() === "completed") return "skipped";
  return projectionStatus || "pending";
}

function isTruncatedWorkflowValue(value: unknown): boolean {
  return recordValue(value)?.truncated === true;
}

function mergeWorkflowIntervention(
  detailValue: unknown,
  projectionAction: WorkflowRunAction | null | undefined,
): WorkflowRunAction | null {
  const detailAction = recordValue(detailValue) as WorkflowRunAction | null;
  if (!detailAction) return null;
  const detailMessageId = nonEmptyString(detailAction.message_id);
  const projectionMessageId = nonEmptyString(projectionAction?.message_id);
  if (!projectionAction || !detailMessageId || detailMessageId !== projectionMessageId) {
    return detailAction;
  }

  if (isTruncatedWorkflowValue(detailAction)) {
    return {
      ...projectionAction,
      message_id: detailMessageId,
      source: detailAction.source,
    };
  }

  const merged = { ...projectionAction, ...detailAction };
  for (const [key, value] of Object.entries(detailAction)) {
    if (isTruncatedWorkflowValue(value) && projectionAction[key] !== undefined) {
      merged[key] = projectionAction[key];
    }
  }
  return merged;
}

export function mergeWorkflowRunView(
  detailValue: unknown,
  projection: WorkflowRunView,
  projectionUpdatedAt?: string | null,
): WorkflowRunView {
  const detail = recordValue(detailValue);
  if (!detail) return projection;
  const detailUpdatedAt = Date.parse(nonEmptyString(detail.updated_at));
  const projectionTimestamp = Date.parse(nonEmptyString(projectionUpdatedAt));
  const projectionIsNewer = Number.isFinite(projectionTimestamp) && (
    !Number.isFinite(detailUpdatedAt) || projectionTimestamp > detailUpdatedAt
  );
  const detailIsAuthoritative = !projectionIsNewer;
  const snapshot = recordValue(detail.definition_snapshot);
  const snapshotNodes = Array.isArray(snapshot?.nodes) ? snapshot.nodes : [];
  const compactSteps = Array.isArray(detail.workflow_steps) ? detail.workflow_steps : null;
  const projectionById = new Map(projection.nodes.map((node) => [node.id, node]));
  const serverNodes = compactSteps !== null
    ? compactSteps.flatMap((rawNode, index) => {
        const node = recordValue(rawNode);
        const id = nonEmptyString(node?.id);
        if (!id) return [];
        return [{
          id,
          name: nonEmptyString(node?.name) || id,
          status: normalizeWorkflowNodeStatus(node?.status),
          type: nonEmptyString(node?.type) || undefined,
          order: index,
          subscriptionId: nonEmptyString(node?.subscription_id) || null,
          error: node?.error,
        } satisfies WorkflowRunNode];
      })
    : snapshotNodes.length > 0
      ? snapshotNodes.flatMap((rawNode, index) => {
        const node = recordValue(rawNode);
        const id = nonEmptyString(node?.id);
        if (!id) return [];
        const projected = projectionById.get(id);
        return [{
          id,
          name: nonEmptyString(node?.name) || projected?.name || id,
          status: serverNodeStatus(id, detail, projected?.status),
          type: nonEmptyString(node?.type) || projected?.type,
          order: index,
          subscriptionId: projected?.subscriptionId || null,
          error: recordValue(recordValue(detail.step_results)?.[id])?.error ?? projected?.error,
        } satisfies WorkflowRunNode];
      })
      : projection.nodes;
  const nodes = detailIsAuthoritative ? serverNodes : projection.nodes;
  const detailBusinessOutcome = businessOutcomeFromRunDetail(detail);
  const businessOutcome = detailIsAuthoritative
    ? detailBusinessOutcome || null
    : projection.businessOutcome || null;
  const hasAuthoritativeCurrentNode = Object.hasOwn(detail, "current_step_id");
  const hasAuthoritativeError = Object.hasOwn(detail, "error");
  const hasAuthoritativeIntervention = Object.hasOwn(detail, "intervention");
  const detailIntervention = mergeWorkflowIntervention(
    detail.intervention,
    projection.action,
  );
  return {
    ...projection,
    id: nonEmptyString(detail.id) || projection.id,
    title: nonEmptyString(snapshot?.name) || projection.title,
    status: projectionIsNewer
      ? projection.status
      : normalizeWorkflowRunStatus(detail.status || projection.status),
    nodes,
    workflowId: nonEmptyString(detail.workflow_id) || projection.workflowId,
    currentNodeId: projectionIsNewer
      ? projection.currentNodeId || null
      : hasAuthoritativeCurrentNode
        ? nonEmptyString(detail.current_step_id) || null
        : projection.currentNodeId || null,
    attemptNumber: Math.max(1, Number(detail.attempt_number || projection.attemptNumber || 1)),
    startedAt: nonEmptyString(detail.started_at) || projection.startedAt || null,
    completedAt: nonEmptyString(detail.completed_at) || projection.completedAt || null,
    businessOutcome,
    error: projectionIsNewer
      ? projection.error
      : hasAuthoritativeError ? detail.error : projection.error,
    action: projectionIsNewer
      ? projection.action
      : hasAuthoritativeIntervention ? detailIntervention : projection.action,
  };
}

function directInterventionAction(run: WorkflowRunView): WorkflowRunAction | null {
  if (run.status === "failed") {
    return {
      kind: "workflow_cancel",
      workflow_run_id: run.id,
      step_id: run.currentNodeId || undefined,
      options: ["cancel"],
    };
  }
  return null;
}

export function selectWorkspaceWorkflowInterventionAction(
  run: WorkflowRunView,
): WorkflowRunAction | null {
  return run.action || directInterventionAction(run);
}

export async function resolveWorkspaceWorkflowMessageAction(
  action: WorkflowRunAction | null,
  choice: string,
  note: string | undefined,
  payload: Record<string, unknown> | undefined,
  files: File[] | undefined,
  onResolveMessage: (
    messageId: string,
    choice: string,
    note?: string,
    payload?: Record<string, unknown>,
    files?: File[],
  ) => void | Promise<unknown>,
): Promise<boolean> {
  const messageId = nonEmptyString(action?.message_id);
  if (!messageId) return false;
  await onResolveMessage(messageId, choice, note, payload, files);
  return true;
}

/* End workspace workflow run host helpers */

interface WorkspaceWorkflowRunHostProps {
  workspaceId: string;
  groups: WorkspaceWorkflowRunGroup[];
  onResolveMessage: (
    messageId: string,
    choice: string,
    note?: string,
    payload?: Record<string, unknown>,
    files?: File[],
  ) => void | Promise<unknown>;
  resolveLoading?: boolean;
  resolveError?: unknown;
  resolveMessageId?: string | null;
  onRunChange?: () => void;
}

type CancelRunMutationVariables = {
  runId: string;
  source: "direct" | "intervention";
};

type CancelConfirmationIdentity = {
  runId: string;
  generation: number;
};

const WORKFLOW_FOCUS_TARGET_SELECTOR = [
  ".workspace-workflow-run-host button:not([disabled])",
  ".workspace-workflow-run-host a[href]",
  '.workspace-workflow-run-host input:not([disabled]):not([type="hidden"])',
  ".workspace-workflow-run-host select:not([disabled])",
  '.workspace-workflow-run-host [contenteditable="true"]',
  '.workspace-workflow-run-host [tabindex]:not([tabindex="-1"])',
].join(", ");

const CHAT_COMPOSER_FOCUS_TARGET_SELECTOR =
  '.chat-composer-rich-editor[contenteditable="true"]';

function isVisibleFocusTarget(element: HTMLElement): boolean {
  if (
    element.hidden
    || element.matches(":disabled")
    || element.getAttribute("aria-disabled") === "true"
    || element.closest('[aria-hidden="true"], [hidden]')
  ) {
    return false;
  }
  const style = window.getComputedStyle(element);
  return style.display !== "none"
    && style.visibility !== "hidden"
    && element.getClientRects().length > 0;
}

function focusNextWorkflowControlOrComposer(): void {
  const workflowTarget = Array.from(
    document.querySelectorAll<HTMLElement>(WORKFLOW_FOCUS_TARGET_SELECTOR),
  ).find(isVisibleFocusTarget);
  const composer = Array.from(
    document.querySelectorAll<HTMLElement>(CHAT_COMPOSER_FOCUS_TARGET_SELECTOR),
  ).find(isVisibleFocusTarget);
  const target = workflowTarget || composer;
  target?.focus();
}

function runSwitcherLabel(run: WorkflowRunView): string {
  const status = t(
    `component.workflow_run.status.${workflowRunStatusPresentation(run).labelKey}`,
  );
  const attempt = t("component.workflow_run.attempt").replace(
    "{count}",
    String(run.attemptNumber || 1),
  );
  return `${run.title || t("component.workspace_chat.workflow")} - ${status} - ${attempt}`;
}

export default function WorkspaceWorkflowRunHost({
  workspaceId,
  groups,
  onResolveMessage,
  resolveLoading = false,
  resolveError,
  resolveMessageId,
  onRunChange,
}: WorkspaceWorkflowRunHostProps) {
  const queryClient = useQueryClient();
  const initialGroups = excludeSupersededWorkflowRunGroups(groups);
  const initialForegroundRunId = selectForegroundWorkflowRunId(initialGroups, "");
  const [selectedRunId, setSelectedRunId] = useState(initialForegroundRunId);
  const [dismissedRunIds, setDismissedRunIds] = useState<Set<string>>(() => new Set());
  const [runDetailsById, setRunDetailsById] = useState<Record<string, unknown>>({});
  const [cancelConfirmationOpen, setCancelConfirmationOpen] = useState(false);
  const [cancelConfirmationRunId, setCancelConfirmationRunId] = useState<string | null>(null);
  const cancelConfirmationIdentityRef = useRef<CancelConfirmationIdentity | null>(null);
  const cancelConfirmationGenerationRef = useRef(0);
  const previousGroupSignatureRef = useRef(groups.map((group) => group.id).join("|"));
  const previousForegroundRunIdRef = useRef(initialForegroundRunId);
  const groupSignature = groups.map((group) => group.id).join("|");

  useEffect(() => {
    setDismissedRunIds(new Set());
    setRunDetailsById({});
    setSelectedRunId(selectForegroundWorkflowRunId(initialGroups, ""));
  }, [workspaceId]);

  useEffect(() => {
    if (previousGroupSignatureRef.current === groupSignature) return;
    previousGroupSignatureRef.current = groupSignature;
    setSelectedRunId(selectForegroundWorkflowRunId(groups, ""));
  }, [groupSignature, groups]);

  const actionableGroups = useMemo(
    () => actionableWorkflowRunGroups(
      groups,
      runDetailsById,
      dismissedRunIds,
    ),
    [dismissedRunIds, groups, runDetailsById],
  );
  const foregroundRunId = selectForegroundWorkflowRunId(actionableGroups, selectedRunId);
  const foregroundGroup = actionableGroups.find((group) => group.id === foregroundRunId) || null;
  const foregroundProjection = foregroundGroup?.projection;
  const foregroundProjectionUpdatedAt = foregroundGroup?.projectionUpdatedAt;
  const runQuery = useQuery({
    queryKey: ["workflow-run", foregroundRunId],
    queryFn: () => api.workflows.getRun(foregroundRunId, false),
    enabled: Boolean(foregroundRunId),
    refetchInterval: (query) => {
      if (!foregroundProjection) return false;
      const currentRun = mergeWorkflowRunView(
        query.state.data,
        foregroundProjection,
        foregroundProjectionUpdatedAt,
      );
      return isWorkflowRunActive(currentRun) ? 1_000 : false;
    },
  });
  const foregroundRun = foregroundProjection
    ? mergeWorkflowRunView(
        runQuery.data,
        foregroundProjection,
        foregroundProjectionUpdatedAt,
      )
    : null;
  const runCapabilities = recordValue(recordValue(runQuery.data)?.capabilities);
  const canControl = Boolean(runCapabilities?.can_control);
  const serverConfirmedTerminal = Boolean(
    runQuery.data
    && foregroundRun
    && !isWorkspaceWorkflowRunActionable(foregroundRun),
  );
  const interventionAction = foregroundRun
    ? selectWorkspaceWorkflowInterventionAction(foregroundRun)
    : null;
  const canCancelRunningRun = canControl && Boolean(
    foregroundRun
    && !interventionAction
    && (foregroundRun.status === "pending" || foregroundRun.status === "running"),
  );

  useEffect(() => {
    if (!foregroundRunId || !runQuery.data) return;
    setRunDetailsById((current) => (
      current[foregroundRunId] === runQuery.data
        ? current
        : { ...current, [foregroundRunId]: runQuery.data }
    ));
  }, [foregroundRunId, runQuery.data]);

  useEffect(() => {
    if (!serverConfirmedTerminal || !foregroundRunId) return;
    setDismissedRunIds((current) => new Set(current).add(foregroundRunId));
    setSelectedRunId("");
  }, [foregroundRunId, serverConfirmedTerminal]);

  const invalidateRunSurfaces = useCallback(async (runId: string) => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["workflow-run", runId] }),
      queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] }),
      queryClient.invalidateQueries({ queryKey: ["workspace-workflow-runs", workspaceId] }),
      queryClient.invalidateQueries({ queryKey: ["workflow-runs"] }),
    ]);
  }, [queryClient, workspaceId]);

  const cancelMutation = useMutation({
    mutationFn: ({ runId }: CancelRunMutationVariables) => api.workflows.cancelRun(runId),
    onSuccess: async (_result, { runId }) => {
      setDismissedRunIds((current) => new Set(current).add(runId));
      await invalidateRunSurfaces(runId);
    },
  });
  const resumeMutation = useMutation({
    mutationFn: () => api.workflows.resumeRun(foregroundRunId),
    onSuccess: async () => {
      await invalidateRunSurfaces(foregroundRunId);
    },
  });

  useEffect(() => {
    if (previousForegroundRunIdRef.current === foregroundRunId) return;
    previousForegroundRunIdRef.current = foregroundRunId;
    cancelConfirmationIdentityRef.current = null;
    setCancelConfirmationOpen(false);
    setCancelConfirmationRunId(null);
    cancelMutation.reset();
    resumeMutation.reset();
    onRunChange?.();
  }, [foregroundRunId, onRunChange]);

  useEffect(() => {
    if (!cancelConfirmationOpen) return;
    if (!canCancelRunningRun || cancelConfirmationRunId !== foregroundRunId) {
      cancelConfirmationIdentityRef.current = null;
      setCancelConfirmationOpen(false);
      setCancelConfirmationRunId(null);
    }
  }, [
    canCancelRunningRun,
    cancelConfirmationOpen,
    cancelConfirmationRunId,
    foregroundRunId,
  ]);

  if (!foregroundGroup || !foregroundRun || serverConfirmedTerminal) return null;

  const actionMessageId = nonEmptyString(interventionAction?.message_id);
  const interventionCancelError = cancelMutation.variables?.source === "intervention"
    ? cancelMutation.error
    : null;
  const directActionError = interventionCancelError || resumeMutation.error;
  const resolvingMessage = Boolean(
    actionMessageId && actionMessageId === resolveMessageId && resolveLoading,
  );
  const resolving = resolvingMessage || cancelMutation.isPending || resumeMutation.isPending;
  const scopedResolveError = actionMessageId === resolveMessageId ? resolveError : null;
  const workflowHref = foregroundRun.workflowId
    ? `/flows?workflow=${encodeURIComponent(foregroundRun.workflowId)}`
    : undefined;
  const historyHref = `/workspaces/${encodeURIComponent(workspaceId)}?tab=workflows&workflow_view=history&workflow_run=${encodeURIComponent(foregroundRun.id)}`;
  const cancelActionLabel = t("component.workflow_run.action.cancel");
  const cancelError = cancelMutation.variables?.source === "direct" && cancelMutation.error
    ? formatWorkflowError(
        cancelMutation.error,
        t("component.workflow_run.error_truncated"),
      )
    : undefined;

  const resolveIntervention = async (
    choice: string,
    note?: string,
    payload?: Record<string, unknown>,
    files?: File[],
  ) => {
    const messageHandled = await resolveWorkspaceWorkflowMessageAction(
      interventionAction,
      choice,
      note,
      payload,
      files,
      onResolveMessage,
    );
    if (messageHandled) return;
    const normalizedChoice = choice.toLowerCase().replace(/[-\s]+/g, "_");
    if (normalizedChoice === "resume") {
      await resumeMutation.mutateAsync();
    } else if (normalizedChoice === "cancel") {
      await cancelMutation.mutateAsync({
        runId: foregroundRunId,
        source: "intervention",
      });
    }
  };

  const closeCancellationConfirmation = (
    expectedConfirmation?: CancelConfirmationIdentity,
  ) => {
    if (expectedConfirmation) {
      const currentConfirmation = cancelConfirmationIdentityRef.current;
      if (
        !currentConfirmation
        || currentConfirmation.runId !== expectedConfirmation.runId
        || currentConfirmation.generation !== expectedConfirmation.generation
      ) {
        return;
      }
    }
    cancelConfirmationIdentityRef.current = null;
    setCancelConfirmationOpen(false);
    setCancelConfirmationRunId(null);
  };

  const openCancellationConfirmation = () => {
    if (!canCancelRunningRun) return;
    cancelMutation.reset();
    cancelConfirmationGenerationRef.current += 1;
    cancelConfirmationIdentityRef.current = {
      runId: foregroundRunId,
      generation: cancelConfirmationGenerationRef.current,
    };
    setCancelConfirmationRunId(foregroundRunId);
    setCancelConfirmationOpen(true);
  };

  const confirmCancellation = async () => {
    const expectedConfirmation = cancelConfirmationIdentityRef.current;
    if (
      !canCancelRunningRun
      || !expectedConfirmation
      || expectedConfirmation.runId !== foregroundRunId
      || cancelConfirmationRunId !== foregroundRunId
    ) {
      closeCancellationConfirmation(expectedConfirmation || undefined);
      return;
    }
    try {
      await cancelMutation.mutateAsync({
        runId: expectedConfirmation.runId,
        source: "direct",
      });
      closeCancellationConfirmation(expectedConfirmation);
    } catch {
      // The mutation error remains visible in the run surface and dialog stays open.
    }
  };

  return (
    <section
      className="workspace-workflow-run-host"
      aria-label={t("component.workflow_run.progress")}
      aria-busy={runQuery.isLoading || resolving}
    >
      {actionableGroups.length > 1 && (
        <div className="workspace-workflow-run-switcher">
          <span className="workspace-workflow-run-count mono">
            {t("component.workflow_run.active_runs").replace(
              "{count}",
              String(actionableGroups.length),
            )}
          </span>
          <Select
            value={foregroundRunId}
            onChange={setSelectedRunId}
            options={actionableGroups.map((group) => ({
              value: group.id,
              label: runSwitcherLabel(group.projection),
            }))}
            ariaLabel={t("component.workflow_run.switcher_label")}
            showSelectedIcon
          />
        </div>
      )}

      <div className="workspace-workflow-run-panel">
        {runQuery.isLoading && !runQuery.data && (
          <div className="workspace-workflow-run-query-state" role="status">
            <LoadingSpinner size={13} />
            <span>{t("component.workflow_run.loading")}</span>
          </div>
        )}
        {runQuery.isError && (
          <p className="workspace-workflow-run-query-error" role="status">
            {t("component.workflow_run.refresh_error")}
          </p>
        )}
        <WorkflowRunProgress
          run={foregroundRun}
          workflowHref={workflowHref}
          historyHref={historyHref}
          headerAction={canCancelRunningRun ? (
            <button
              type="button"
              className="workflow-run-cancel-action"
              title={cancelActionLabel}
              aria-label={cancelActionLabel}
              disabled={resolving}
              onClick={openCancellationConfirmation}
            >
              {cancelMutation.isPending
                ? <LoadingSpinner size={13} />
                : <IconStop size={13} aria-hidden="true" />}
            </button>
          ) : undefined}
        />
        {!cancelConfirmationOpen && !interventionAction && cancelError && (
          <p className="workflow-run-intervention-error workflow-run-cancel-error" role="alert">
            {cancelError}
          </p>
        )}
        {interventionAction && (
          <WorkflowRunIntervention
            key={`${foregroundRun.id}:${actionMessageId || interventionAction.kind}`}
            run={foregroundRun}
            action={interventionAction}
            onResolve={resolveIntervention}
            disabled={resolving || !canControl}
            loading={resolving}
            error={scopedResolveError || directActionError}
          />
        )}
      </div>
      <ConfirmDialog
        open={cancelConfirmationOpen}
        onClose={closeCancellationConfirmation}
        onConfirm={confirmCancellation}
        title={t("component.workflow_run.cancel_confirm_title")}
        message={t("component.workflow_run.cancel_confirm_message")}
        confirmLabel={t("component.workflow_run.cancel_confirm_action")}
        cancelLabel={t("component.workflow_run.cancel_confirm_keep_running")}
        danger
        loading={cancelMutation.isPending}
        closeOnConfirm={false}
        error={cancelError}
        restoreFocusFallback={focusNextWorkflowControlOrComposer}
      />
    </section>
  );
}
