import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  useMutation,
  useQuery,
  useQueries,
  useQueryClient,
  type QueryKey,
} from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useLocation } from "react-router-dom";
import { t } from "../../lib/i18n";
import { workflowPath, workflowRunPath } from "../../lib/workflowRoutes";
import {
  ACTIVE_WORKFLOW_FAMILY_POLL_MS,
  ACTIVE_WORKFLOW_RUN_POLL_MS,
} from "../../lib/workspaceLiveRefresh";
import { IconPause, IconStop } from "../icons";
import ConfirmDialog from "../ui/ConfirmDialog";
import LoadingSpinner from "../ui/LoadingSpinner";
import Select from "../ui/Select";
import WorkflowRunIntervention from "./WorkflowRunIntervention";
import { usePreviewFeatureAccess } from "../../lib/previewFeatureAccess";
import WorkflowRunProgress from "./WorkflowRunProgress";
import {
  canCancelWorkflowRun,
  formatWorkflowError,
  isWorkflowRunActive,
  workflowRunStatusPresentation,
  workflowStepActivity,
  workflowStepOutcome,
  type WorkflowHistoryRun,
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
  id?: string;
  created_at?: string;
  updated_at?: string | null;
  message_kind?: string | null;
  refs?: WorkspaceWorkflowRunMessageRef[] | null;
  meta?: Record<string, unknown> | null;
  pending_action?: WorkflowRunAction | null;
  resolved_at?: string | null;
  resolution?: Record<string, unknown> | null;
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
  "workflow_effect_review",
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
  "revision_required",
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

function normalizeWorkflowActionChoice(value: unknown): string {
  return nonEmptyString(value).toLowerCase().replace(/[-\s]+/g, "_");
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
      ...workflowStepOutcome(step),
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
    retryFromStepId: nonEmptyString(action?.retry_from_step_id) || null,
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
    if (message.meta?.workflow_run_accessible === false) return;
    const messageRunId = workflowRunIdForMessage(message);
    const runId = nonEmptyString(
      message.pending_action?.workflow_chat_host_run_id,
    ) || messageRunId;
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
    if (message.id) group.ownedMessageIds.push(message.id);
    const projectionUpdatedAt = messageRunId === runId
      ? nonEmptyString(message.updated_at)
      : "";
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

export function isWorkspaceWorkflowRunInProgress(
  run: { status?: unknown; businessOutcome?: unknown; business_outcome?: unknown },
): boolean {
  const status = nonEmptyString(run.status).toLowerCase();
  if (["queued", "pending", "running", "paused"].includes(status)) return true;
  return status === "completed"
    && nonEmptyString(run.businessOutcome || run.business_outcome).toLowerCase() === "needs_input";
}

export function currentWorkspaceWorkflowRunGroups(
  groups: WorkspaceWorkflowRunGroup[],
  detailsByRunId: Record<string, unknown> = {},
  dismissedRunIds: ReadonlySet<string> = new Set(),
): WorkspaceWorkflowRunGroup[] {
  const latestAttempts = excludeSupersededWorkflowRunGroups(groups, detailsByRunId).map((group) => ({
    ...group,
    projection: mergeWorkflowRunView(detailsByRunId[group.id], group.projection, group.projectionUpdatedAt),
  }));
  // Keep one recent result beside live runs. A newer success or cancellation
  // retires old failures instead of revealing the next old failure in the list.
  const latestResult = latestAttempts.filter((group) => (
    !isWorkspaceWorkflowRunInProgress(group.projection)
  )).sort((left, right) => {
    const leftTime = Date.parse(left.projection.startedAt || left.latestCreatedAt || "");
    const rightTime = Date.parse(right.projection.startedAt || right.latestCreatedAt || "");
    return (Number.isFinite(rightTime) && Number.isFinite(leftTime) ? rightTime - leftTime : 0)
      || right.latestIndex - left.latestIndex;
  })[0];
  return latestAttempts.filter((group) => (
    !dismissedRunIds.has(group.id)
    && isWorkspaceWorkflowRunActionable(group.projection)
    && (isWorkspaceWorkflowRunInProgress(group.projection) || group.id === latestResult?.id)
  ));
}

export function suppressUnverifiedWorkflowRunGroups(
  groups: WorkspaceWorkflowRunGroup[],
  unverifiedRunIds: ReadonlySet<string>,
): WorkspaceWorkflowRunGroup[] {
  return excludeSupersededWorkflowRunGroups(groups).filter(
    (group) => !unverifiedRunIds.has(group.id),
  );
}

function workflowFamilyAttemptTimestamp(run: WorkflowHistoryRun): number {
  for (const value of [run.started_at, run.created_at, run.updated_at]) {
    const timestamp = Date.parse(value || "");
    if (Number.isFinite(timestamp)) return timestamp;
  }
  return 0;
}

function latestWorkflowFamilyAttempt(
  runs: WorkflowHistoryRun[],
): WorkflowHistoryRun | null {
  return [...runs].sort((left, right) => (
    Number(right.attempt_number || 1) - Number(left.attempt_number || 1)
    || workflowFamilyAttemptTimestamp(right) - workflowFamilyAttemptTimestamp(left)
    || right.id.localeCompare(left.id)
  ))[0] || null;
}

function resetProjectionNodesForRetry(
  nodes: WorkflowRunNode[],
  run: WorkflowHistoryRun,
): WorkflowRunNode[] {
  const currentNodeId = nonEmptyString(run.current_step_id);
  const currentStatus = normalizeWorkflowRunStatus(run.status);
  return nodes.map((node) => ({
    ...node,
    status: currentNodeId && node.id === currentNodeId ? currentStatus : "pending",
    error: undefined,
  }));
}

export function reconcileWorkspaceWorkflowRunGroups(
  groups: WorkspaceWorkflowRunGroup[],
  familyRunsByGroupId: Record<string, WorkflowHistoryRun[]>,
): WorkspaceWorkflowRunGroup[] {
  const reconciledByRunId = new Map<string, WorkspaceWorkflowRunGroup>();

  // Family queries are keyed by retry leaves. Keep superseded ancestors out of
  // reconciliation so an unqueried failed root cannot reappear after its
  // family reconciles to a newer terminal attempt.
  for (const group of excludeSupersededWorkflowRunGroups(groups)) {
    const latestRun = latestWorkflowFamilyAttempt(familyRunsByGroupId[group.id] || []);
    const runIdChanged = Boolean(latestRun && latestRun.id !== group.id);
    const projection = latestRun
      ? {
          ...group.projection,
          id: latestRun.id,
          title: nonEmptyString(latestRun.workflow_name) || group.projection.title,
          status: normalizeWorkflowRunStatus(latestRun.status),
          nodes: runIdChanged
            ? resetProjectionNodesForRetry(group.projection.nodes, latestRun)
            : group.projection.nodes,
          workflowId: nonEmptyString(latestRun.workflow_id) || group.projection.workflowId,
          currentNodeId: Object.hasOwn(latestRun, "current_step_id")
            ? nonEmptyString(latestRun.current_step_id) || null
            : group.projection.currentNodeId,
          retryFromStepId: nonEmptyString(latestRun.retry_from_step_id)
            || group.projection.retryFromStepId
            || null,
          attemptNumber: Math.max(
            1,
            Number(latestRun.attempt_number || group.projection.attemptNumber || 1),
          ),
          startedAt: nonEmptyString(latestRun.started_at) || group.projection.startedAt,
          completedAt: nonEmptyString(latestRun.completed_at) || null,
          businessOutcome: nonEmptyString(latestRun.business_outcome).toLowerCase() || null,
          error: Object.hasOwn(latestRun, "error")
            ? latestRun.error
            : latestRun.history_blocker ?? group.projection.error,
          action: runIdChanged ? null : group.projection.action,
        } satisfies WorkflowRunView
      : group.projection;
    const reconciled: WorkspaceWorkflowRunGroup = latestRun
      ? {
          ...group,
          id: latestRun.id,
          latestCreatedAt: nonEmptyString(
            latestRun.started_at || latestRun.created_at || latestRun.updated_at,
          ) || group.latestCreatedAt,
          actionMessage: runIdChanged ? null : group.actionMessage,
          projectionUpdatedAt: nonEmptyString(latestRun.updated_at) || group.projectionUpdatedAt,
          retryOfRunId: nonEmptyString(latestRun.retry_of_run_id) || null,
          projection,
        }
      : group;
    const existing = reconciledByRunId.get(reconciled.id);
    if (!existing) {
      reconciledByRunId.set(reconciled.id, reconciled);
      continue;
    }
    const newer = reconciled.latestIndex >= existing.latestIndex ? reconciled : existing;
    reconciledByRunId.set(reconciled.id, {
      ...newer,
      latestIndex: Math.max(existing.latestIndex, reconciled.latestIndex),
      ownedMessageIds: [...new Set([
        ...existing.ownedMessageIds,
        ...reconciled.ownedMessageIds,
      ])],
      actionMessages: [...existing.actionMessages, ...reconciled.actionMessages],
    });
  }

  return [...reconciledByRunId.values()];
}

export function isWorkflowRunAlreadyTerminalError(error: unknown): boolean {
  const message = error instanceof Error
    ? error.message
    : nonEmptyString(recordValue(error)?.message);
  return /^run already (?:completed|cancelled)$/i.test(message);
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
          ...workflowStepOutcome(node),
          ...workflowStepActivity(node),
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
  // Activity exists only on the polled run, so it joins whichever nodes are shown.
  const liveActivity = new Map((serverNodes as WorkflowRunNode[]).flatMap((node) => node.activity ? [[node.id, node.activity] as const] : []));
  const nodes = detailIsAuthoritative
    ? serverNodes
    : projection.nodes.map((node) => liveActivity.has(node.id) ? { ...node, activity: liveActivity.get(node.id) } : node);
  const detailBusinessOutcome = businessOutcomeFromRunDetail(detail);
  const businessOutcome = detailIsAuthoritative
    ? detailBusinessOutcome || null
    : projection.businessOutcome || null;
  const hasAuthoritativeCurrentNode = Object.hasOwn(detail, "current_step_id");
  const hasAuthoritativeError = Object.hasOwn(detail, "error");
  const hasAuthoritativeIntervention = Object.hasOwn(detail, "intervention");
  const hasAuthoritativeRetryFrom = Object.hasOwn(detail, "retry_from_step_id");
  const projectionActionRunId = nonEmptyString(projection.action?.workflow_run_id);
  const projectionHasNestedAction = Boolean(
    projectionActionRunId && projectionActionRunId !== projection.id,
  );
  const detailIntervention = mergeWorkflowIntervention(
    detail.intervention,
    projection.action,
  );
  const mergedAction = projectionIsNewer
    ? projection.action
    : projectionHasNestedAction
      ? projection.action
      : hasAuthoritativeIntervention ? detailIntervention : projection.action;
  const currentStepId = nonEmptyString(detail.current_step_id);
  const currentStepResult = recordValue(
    recordValue(detail.step_results)?.[currentStepId],
  );
  const compactCurrentStep = recordValue(compactSteps?.find(
    (rawNode) => nonEmptyString(recordValue(rawNode)?.id) === currentStepId,
  ));
  const compactCurrentStepType = nonEmptyString(compactCurrentStep?.type).toLowerCase();
  const detailedStepWaitsOnSubworkflow = Boolean(
    nonEmptyString(currentStepResult?.status).toLowerCase() === "paused"
    && nonEmptyString(currentStepResult?.subrun_id)
    && nonEmptyString(currentStepResult?.output).toLowerCase()
      === "waiting for subworkflow to resume"
  );
  const compactStepWaitsOnSubworkflow = Boolean(
    (compactCurrentStepType === "subworkflow" || compactCurrentStepType === "foreach_subworkflow")
    && nonEmptyString(compactCurrentStep?.status).toLowerCase() === "paused"
    && Array.isArray(compactCurrentStep?.child_run_ids)
    && compactCurrentStep.child_run_ids.some((childRunId) => nonEmptyString(childRunId))
  );
  const waitsOnSubworkflow = Boolean(
    !projectionIsNewer
    && normalizeWorkflowRunStatus(detail.status) === "paused"
    && (detailedStepWaitsOnSubworkflow || compactStepWaitsOnSubworkflow)
    && !mergedAction
  );
  // The runtime suspends the parent while its asynchronous child owns execution.
  const visibleNodes = waitsOnSubworkflow
    ? nodes.map((node) => (
        node.id === currentStepId
          ? { ...node, status: "running" as const }
          : node
      ))
    : nodes;
  return {
    ...projection,
    id: nonEmptyString(detail.id) || projection.id,
    title: nonEmptyString(snapshot?.name) || projection.title,
    status: projectionIsNewer
      ? projection.status
      : waitsOnSubworkflow
        ? "running"
        : normalizeWorkflowRunStatus(detail.status || projection.status),
    nodes: visibleNodes,
    workflowId: nonEmptyString(detail.workflow_id) || projection.workflowId,
    currentNodeId: projectionIsNewer
      ? projection.currentNodeId || null
      : hasAuthoritativeCurrentNode
        ? nonEmptyString(detail.current_step_id) || null
        : projection.currentNodeId || null,
    retryFromStepId: projectionIsNewer
      ? projection.retryFromStepId || null
      : hasAuthoritativeRetryFrom
        ? nonEmptyString(detail.retry_from_step_id) || null
        : projection.retryFromStepId || null,
    attemptNumber: Math.max(1, Number(detail.attempt_number || projection.attemptNumber || 1)),
    startedAt: nonEmptyString(detail.started_at) || projection.startedAt || null,
    completedAt: nonEmptyString(detail.completed_at) || projection.completedAt || null,
    businessOutcome,
    error: projectionIsNewer
      ? projection.error
      : hasAuthoritativeError ? detail.error : projection.error,
    action: mergedAction,
    // The server's counts are the Flow's own stages; the drawn path is not.
    processedCount: Number.isInteger(detail.processed_count)
      ? Number(detail.processed_count)
      : projection.processedCount ?? null,
    totalCount: Number.isInteger(detail.total_count)
      ? Number(detail.total_count)
      : projection.totalCount ?? null,
  };
}

export function selectWorkspaceWorkflowRunDetail(
  queryDetail: unknown,
  cachedDetails: Record<string, unknown>,
  runId: string,
): unknown {
  return queryDetail === undefined ? cachedDetails[runId] : queryDetail;
}

export interface WorkflowApprovalReviewHydration {
  review: unknown;
  reviewTitle?: string;
}

export function workflowApprovalReviewFromRunDetail(
  detailValue: unknown,
  action: WorkflowRunAction | null | undefined,
): WorkflowApprovalReviewHydration | null {
  if (
    action?.kind !== "workflow_approval"
    || action.review_location !== "workflow_history"
  ) {
    return null;
  }
  const stepId = nonEmptyString(action.step_id);
  const detail = recordValue(detailValue);
  const stepResults = recordValue(detail?.step_results);
  const stepResult = stepId ? recordValue(stepResults?.[stepId]) : null;
  if (!stepResult || stepResult.review == null) return null;

  const reviewTitle = nonEmptyString(stepResult.review_title || action.review_title);
  return {
    review: stepResult.review,
    ...(reviewTitle ? { reviewTitle } : {}),
  };
}

function directInterventionAction(run: WorkflowRunView): WorkflowRunAction | null {
  const retryFromStepId = nonEmptyString(run.retryFromStepId);
  if (run.status === "failed" && run.currentNodeId && retryFromStepId) {
    return {
      kind: "workflow_retry",
      workflow_run_id: run.id,
      step_id: run.currentNodeId,
      retry_from_step_id: retryFromStepId,
      editable_input_schema: { type: "object", properties: {} },
      options: ["retry"],
    };
  }
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

export function workflowResumeTargetRunId(
  action: WorkflowRunAction | null | undefined,
  fallbackRunId: string,
): string {
  return nonEmptyString(action?.workflow_run_id) || fallbackRunId;
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

export interface WorkflowRunInvalidationContext {
  runId: string;
  workspaceId?: string;
}

interface WorkspaceWorkflowRunHostProps {
  workspaceId?: string;
  conversationId?: string;
  groups: WorkspaceWorkflowRunGroup[];
  attentionKeys?: Record<string, Record<string, string>>;
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
  invalidationQueryKeys?: readonly QueryKey[];
  onInvalidate?: (context: WorkflowRunInvalidationContext) => void | Promise<unknown>;
  onRunChange?: () => void;
}

type CancelRunMutationVariables = {
  runId: string;
  source: "direct" | "intervention";
};

type RetryRunMutationVariables = {
  runId: string;
  action: WorkflowRunAction;
  variables?: Record<string, unknown>;
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
  const status = t(workflowRunStatusPresentation(run).stateKey);
  const attempt = t("component.workflow_run.attempt").replace(
    "{count}",
    String(run.attemptNumber || 1),
  );
  return `${run.title || t("component.workspace_chat.workflow")} - ${status} - ${attempt}`;
}

export default function WorkspaceWorkflowRunHost({
  workspaceId,
  conversationId,
  groups,
  attentionKeys,
  onResolveMessage,
  resolveLoading = false,
  resolveError,
  resolveMessageId,
  invalidationQueryKeys = [],
  onInvalidate,
  onRunChange,
}: WorkspaceWorkflowRunHostProps) {
  const queryClient = useQueryClient();
  const location = useLocation();
  const attentionMessageId = new URLSearchParams(location.search).has("attention") && location.hash.startsWith("#workspace-chat-message-")
    ? location.hash.slice("#workspace-chat-message-".length) : "";
  const familyQueryGroups = useMemo(
    () => excludeSupersededWorkflowRunGroups(groups).filter((group) => (
      isWorkspaceWorkflowRunActionable(group.projection)
    )),
    [groups],
  );
  const familyQueries = useQueries({
    queries: familyQueryGroups.map((group) => ({
      queryKey: ["workspace-chat-workflow-run-family", group.id],
      queryFn: ({ signal }) => api.workflows.getRunFamily(group.id, signal),
      refetchInterval: (query: { state: { data?: WorkflowHistoryRun[] } }) => {
        const latestRun = latestWorkflowFamilyAttempt(query.state.data || []);
        return latestRun && ["pending", "running"].includes(
          nonEmptyString(latestRun.status).toLowerCase(),
        ) ? ACTIVE_WORKFLOW_FAMILY_POLL_MS : false;
      },
      refetchIntervalInBackground: false,
    })),
  });
  const familyRunsByGroupId = useMemo(
    () => Object.fromEntries(familyQueryGroups.map((group, index) => [
      group.id,
      (familyQueries[index]?.data || []) as WorkflowHistoryRun[],
    ])),
    [familyQueries, familyQueryGroups],
  );
  const unverifiedFamilyRunIds = useMemo(
    () => new Set(familyQueryGroups.flatMap((group, index) => (
      familyQueries[index]?.isPending || familyQueries[index]?.isError
        ? [group.id]
        : []
    ))),
    [familyQueries, familyQueryGroups],
  );
  const verifiedGroups = useMemo(
    () => suppressUnverifiedWorkflowRunGroups(groups, unverifiedFamilyRunIds),
    [groups, unverifiedFamilyRunIds],
  );
  const reconciledGroups = useMemo(
    () => reconcileWorkspaceWorkflowRunGroups(verifiedGroups, familyRunsByGroupId),
    [familyRunsByGroupId, verifiedGroups],
  );
  const initialGroups = currentWorkspaceWorkflowRunGroups(reconciledGroups);
  const initialForegroundRunId = selectForegroundWorkflowRunId(initialGroups, "");
  const [selectedRunId, setSelectedRunId] = useState(initialForegroundRunId);
  const [dismissedRunIds, setDismissedRunIds] = useState<Set<string>>(() => new Set());
  const [runDetailsById, setRunDetailsById] = useState<Record<string, unknown>>({});
  const submittingRunIdsRef = useRef(new Set<string>());
  const [submittingRunIds, setSubmittingRunIds] = useState<Set<string>>(() => new Set());
  const [cancelConfirmationOpen, setCancelConfirmationOpen] = useState(false);
  const [cancelConfirmationRunId, setCancelConfirmationRunId] = useState<string | null>(null);
  const cancelConfirmationIdentityRef = useRef<CancelConfirmationIdentity | null>(null);
  const cancelConfirmationGenerationRef = useRef(0);
  const previousGroupSignatureRef = useRef(
    reconciledGroups.map((group) => group.id).join("|"),
  );
  const previousForegroundRunIdRef = useRef(initialForegroundRunId);
  const groupSignature = reconciledGroups.map((group) => group.id).join("|");
  const scopeId = conversationId || workspaceId || "global";

  useEffect(() => {
    setDismissedRunIds(new Set());
    setRunDetailsById({});
    setSelectedRunId(selectForegroundWorkflowRunId(initialGroups, ""));
  }, [scopeId]);

  useEffect(() => {
    if (previousGroupSignatureRef.current === groupSignature) return;
    previousGroupSignatureRef.current = groupSignature;
    setSelectedRunId(selectForegroundWorkflowRunId(reconciledGroups, ""));
  }, [groupSignature, reconciledGroups]);

  const currentGroups = useMemo(
    () => currentWorkspaceWorkflowRunGroups(
      reconciledGroups,
      runDetailsById,
      dismissedRunIds,
    ),
    [dismissedRunIds, reconciledGroups, runDetailsById],
  );
  const attentionGroup = actionableWorkflowRunGroups(reconciledGroups, runDetailsById, dismissedRunIds)
    .find((group) => group.ownedMessageIds.includes(attentionMessageId));
  // An explicitly opened reminder can still be inspected, but is not an active run.
  const selectableGroups = attentionGroup && !currentGroups.some((group) => group.id === attentionGroup.id)
    ? [...currentGroups, attentionGroup]
    : currentGroups;
  const activeRunCount = currentGroups.filter((group) => isWorkspaceWorkflowRunInProgress(group.projection)).length;
  useEffect(() => {
    if (attentionGroup) setSelectedRunId(attentionGroup.id);
  }, [attentionMessageId, attentionGroup?.id]);
  const foregroundRunId = selectForegroundWorkflowRunId(selectableGroups, selectedRunId);
  const foregroundGroup = selectableGroups.find((group) => group.id === foregroundRunId) || null;
  const foregroundProjection = foregroundGroup?.projection;
  const foregroundProjectionUpdatedAt = foregroundGroup?.projectionUpdatedAt;
  const runQuery = useQuery({
    queryKey: ["workflow-run", foregroundRunId],
    queryFn: ({ signal }) => api.workflows.getRun(foregroundRunId, false, signal),
    enabled: Boolean(foregroundRunId),
    refetchInterval: (query) => {
      if (!foregroundProjection) return false;
      const currentRun = mergeWorkflowRunView(
        query.state.data,
        foregroundProjection,
        foregroundProjectionUpdatedAt,
      );
      return isWorkflowRunActive(currentRun)
        ? ACTIVE_WORKFLOW_RUN_POLL_MS
        : false;
    },
    refetchIntervalInBackground: false,
  });
  const selectedRunDetail = selectWorkspaceWorkflowRunDetail(
    runQuery.data,
    runDetailsById,
    foregroundRunId,
  );
  const foregroundRun = foregroundProjection
    ? mergeWorkflowRunView(
        selectedRunDetail,
        foregroundProjection,
        foregroundProjectionUpdatedAt,
      )
    : null;
  const runDetail = recordValue(selectedRunDetail);
  const authoritativeWorkspaceId = nonEmptyString(runDetail?.workspace_id);
  const resolvedWorkspaceId = authoritativeWorkspaceId || workspaceId || "";
  const runCapabilities = recordValue(runDetail?.capabilities);
  const canControl = Boolean(runCapabilities?.can_control);
  const serverConfirmedTerminal = Boolean(
    runQuery.data
    && foregroundRun
    && !isWorkspaceWorkflowRunActionable(foregroundRun),
  );
  const interventionAction = foregroundRun
    ? selectWorkspaceWorkflowInterventionAction(foregroundRun)
    : null;
  const approvalReviewStepId = nonEmptyString(interventionAction?.step_id);
  const approvalReviewActionId = nonEmptyString(
    interventionAction?.message_id
      || interventionAction?.action_id
      || interventionAction?.id,
  );
  const approvalReviewRunId = nonEmptyString(
    interventionAction?.workflow_run_id,
  ) || foregroundRunId;
  const hydratesApprovalReview = interventionAction?.kind === "workflow_approval"
    && interventionAction.review_location === "workflow_history"
    && Boolean(approvalReviewStepId);
  const approvalReviewQuery = useQuery({
    queryKey: ["workflow-run-review", approvalReviewRunId, approvalReviewStepId, approvalReviewActionId],
    queryFn: ({ signal }) => api.workflows.getRun(approvalReviewRunId, true, signal),
    enabled: Boolean(approvalReviewRunId && hydratesApprovalReview),
    staleTime: Infinity,
  });
  const approvalReview = workflowApprovalReviewFromRunDetail(
    approvalReviewQuery.data,
    interventionAction,
  );
  const canCancelRun = canControl && Boolean(
    foregroundRun
    && canCancelWorkflowRun({
      ...foregroundRun,
      capabilities: { can_control: canControl },
    }),
  );
  const canPauseRunningRun = canControl && Boolean(
    foregroundRun
    && !interventionAction
    && (foregroundRun.status === "pending" || foregroundRun.status === "running"),
  );
  const headerInterventionAction = interventionAction && canCancelRun
    ? {
        ...interventionAction,
        options: (interventionAction.options || []).filter(
          (option) => normalizeWorkflowActionChoice(option) !== "cancel",
        ),
      }
    : interventionAction;

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
    const invalidations = [
      queryClient.invalidateQueries({ queryKey: ["workflow-run", runId] }),
      queryClient.invalidateQueries({ queryKey: ["workflow-runs"] }),
      queryClient.invalidateQueries({ queryKey: ["workflow-run-history-control", runId] }),
      queryClient.invalidateQueries({ queryKey: ["workspace-app-data", resolvedWorkspaceId] }),
      queryClient.invalidateQueries({ queryKey: ["workspace-app-data-contexts", resolvedWorkspaceId] }),
      queryClient.invalidateQueries({ queryKey: ["workspace-app-section", resolvedWorkspaceId] }),
      queryClient.invalidateQueries({
        queryKey: ["workspace-chat-workflow-run-family", runId],
        exact: true,
      }),
      ...invalidationQueryKeys.map((queryKey: QueryKey) => (
        queryClient.invalidateQueries({ queryKey })
      )),
    ];
    if (workspaceId) {
      invalidations.push(
        queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] }),
        queryClient.invalidateQueries({ queryKey: ["workspace-workflow-runs", workspaceId] }),
      );
    }
    if (resolvedWorkspaceId && resolvedWorkspaceId !== workspaceId) {
      invalidations.push(
        queryClient.invalidateQueries({ queryKey: ["workspace-chat", resolvedWorkspaceId] }),
        queryClient.invalidateQueries({
          queryKey: ["workspace-workflow-runs", resolvedWorkspaceId],
        }),
      );
    }
    await Promise.all(invalidations);
    await onInvalidate?.({
      runId,
      workspaceId: resolvedWorkspaceId || undefined,
    });
  }, [
    invalidationQueryKeys,
    onInvalidate,
    queryClient,
    resolvedWorkspaceId,
    workspaceId,
  ]);

  const cancelMutation = useMutation({
    mutationFn: ({ runId }: CancelRunMutationVariables) => api.workflows.cancelRun(runId),
    onSuccess: async (_result, { runId }) => {
      setDismissedRunIds((current) => new Set(current).add(runId));
      await invalidateRunSurfaces(runId);
    },
  });
  const pauseMutation = useMutation({
    mutationFn: () => api.workflows.pauseRun(foregroundRunId),
    onSuccess: async () => {
      await invalidateRunSurfaces(foregroundRunId);
    },
  });
  const resumeMutation = useMutation({
    mutationFn: ({ runId }: { runId: string }) => api.workflows.resumeRun(runId),
    onSuccess: async (_result, { runId }) => {
      await invalidateRunSurfaces(runId);
      if (runId !== foregroundRunId) {
        await invalidateRunSurfaces(foregroundRunId);
      }
    },
  });
  const retryMutation = useMutation({
    mutationFn: ({ runId, action, variables }: RetryRunMutationVariables) => (
      api.workflows.retryRun(runId, {
        from_step_id: action.retry_from_step_id || action.step_id,
        variables,
        execute: true,
      })
    ),
    onSuccess: async (_result, { runId }) => {
      await invalidateRunSurfaces(runId);
      if (runId !== foregroundRunId) {
        await invalidateRunSurfaces(foregroundRunId);
      }
    },
  });

  useEffect(() => {
    if (previousForegroundRunIdRef.current === foregroundRunId) return;
    previousForegroundRunIdRef.current = foregroundRunId;
    cancelConfirmationIdentityRef.current = null;
    setCancelConfirmationOpen(false);
    setCancelConfirmationRunId(null);
    cancelMutation.reset();
    pauseMutation.reset();
    resumeMutation.reset();
    retryMutation.reset();
    onRunChange?.();
  }, [foregroundRunId, onRunChange]);

  useEffect(() => {
    if (!cancelConfirmationOpen) return;
    if (!canCancelRun || cancelConfirmationRunId !== foregroundRunId) {
      cancelConfirmationIdentityRef.current = null;
      setCancelConfirmationOpen(false);
      setCancelConfirmationRunId(null);
    }
  }, [
    canCancelRun,
    cancelConfirmationOpen,
    cancelConfirmationRunId,
    foregroundRunId,
  ]);
  // Flow and run pages are part of Flows; without it the bar links nowhere.
  const flowsAccess = usePreviewFeatureAccess("flows");

  if (!foregroundGroup || !foregroundRun || serverConfirmedTerminal) return null;

  const actionMessageId = nonEmptyString(interventionAction?.message_id);
  const interventionCancelError = cancelMutation.variables?.source === "intervention"
    ? cancelMutation.error
    : null;
  const directActionError = interventionCancelError
    || pauseMutation.error
    || resumeMutation.error
    || retryMutation.error;
  const resolvingMessage = Boolean(
    actionMessageId && actionMessageId === resolveMessageId && resolveLoading,
  );
  const resolving = submittingRunIds.has(foregroundRunId)
    || resolvingMessage
    || cancelMutation.isPending
    || pauseMutation.isPending
    || resumeMutation.isPending
    || retryMutation.isPending;
  const scopedResolveError = actionMessageId === resolveMessageId ? resolveError : null;
  const workflowHref = flowsAccess.enabled && foregroundRun.workflowId
    ? workflowPath(foregroundRun.workflowId)
    : undefined;
  const historyHref = flowsAccess.enabled && foregroundRun.workflowId
    ? workflowRunPath(foregroundRun.workflowId, foregroundRun.id, resolvedWorkspaceId)
    : undefined;
  const approvalHistoryHref = flowsAccess.enabled && foregroundRun.workflowId && approvalReviewRunId
    ? workflowRunPath(foregroundRun.workflowId, approvalReviewRunId, resolvedWorkspaceId)
    : undefined;
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
    if (!canControl || resolving || submittingRunIdsRef.current.has(foregroundRunId)) return;
    submittingRunIdsRef.current.add(foregroundRunId);
    setSubmittingRunIds(new Set(submittingRunIdsRef.current));
    try {
      const runtimeApprovalId = nonEmptyString(interventionAction?.runtime_approval_id);
      if (interventionAction?.kind === "workflow_approval" && runtimeApprovalId) {
        if ((choice !== "approve" && choice !== "reject") || !interventionAction.options?.includes(choice)) return;
        const runId = workflowResumeTargetRunId(interventionAction, foregroundRunId);
        await api.workflows.resolveRuntimeApproval(runId, runtimeApprovalId, choice, payload?.review);
        await invalidateRunSurfaces(runId);
        if (runId !== foregroundRunId) await invalidateRunSurfaces(foregroundRunId);
        return;
      }
      const messageHandled = await resolveWorkspaceWorkflowMessageAction(
        interventionAction,
        choice,
        note,
        payload,
        files,
        onResolveMessage,
      );
      if (messageHandled) {
        await invalidateRunSurfaces(foregroundRunId);
        return;
      }
      const normalizedChoice = normalizeWorkflowActionChoice(choice);
      if (normalizedChoice === "resume") {
        await resumeMutation.mutateAsync({
          runId: workflowResumeTargetRunId(interventionAction, foregroundRunId),
        });
      } else if (normalizedChoice === "retry" && interventionAction?.kind === "workflow_retry") {
        await retryMutation.mutateAsync({
          runId: foregroundRunId,
          action: interventionAction,
          variables: payload?.variables as Record<string, unknown> | undefined,
        });
      } else if (normalizedChoice === "cancel") {
        await cancelMutation.mutateAsync({
          runId: foregroundRunId,
          source: "intervention",
        });
      }
    } finally {
      submittingRunIdsRef.current.delete(foregroundRunId);
      setSubmittingRunIds(new Set(submittingRunIdsRef.current));
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
    if (!canCancelRun) return;
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
      !canCancelRun
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
    } catch (error) {
      if (isWorkflowRunAlreadyTerminalError(error)) {
        setDismissedRunIds((current) => new Set(current).add(expectedConfirmation.runId));
        closeCancellationConfirmation(expectedConfirmation);
        cancelMutation.reset();
        await invalidateRunSurfaces(expectedConfirmation.runId);
      }
      // Other mutation errors remain visible in the dialog so the user can retry.
    }
  };

  return (
    <section
      className="workspace-workflow-run-host"
      aria-label={t("component.workflow_run.progress")}
      aria-busy={runQuery.isLoading || resolving}
    >
      {selectableGroups.length > 1 && (
        <div className="workspace-workflow-run-switcher">
          {activeRunCount > 0 && (
            <span className="workspace-workflow-run-count mono">
              {t("component.workflow_run.active_runs").replace("{count}", String(activeRunCount))}
            </span>
          )}
          <Select
            value={foregroundRunId}
            onChange={setSelectedRunId}
            options={selectableGroups.map((group) => ({
              value: group.id,
              label: runSwitcherLabel(mergeWorkflowRunView(
                runDetailsById[group.id],
                group.projection,
                group.projectionUpdatedAt,
              )) + (currentGroups.some((current) => current.id === group.id)
                ? "" : ` · ${t("component.workflow_run_history.history")}`),
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
          submitting={resolving}
          workflowHref={workflowHref}
          historyHref={historyHref}
          headerAction={canCancelRun ? (
            <div className="workflow-run-control-actions">
              {canPauseRunningRun && (
                <button
                  type="button"
                  className="workflow-run-pause-action"
                  title={t("component.workflow_run.action.pause")}
                  aria-label={t("component.workflow_run.action.pause")}
                  disabled={resolving}
                  onClick={() => pauseMutation.mutate()}
                >
                  {pauseMutation.isPending
                    ? <LoadingSpinner size={13} />
                    : <IconPause size={13} aria-hidden="true" />}
                </button>
              )}
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
            </div>
          ) : undefined}
        />
        {!cancelConfirmationOpen && !interventionAction && cancelError && (
          <p className="workflow-run-intervention-error workflow-run-cancel-error" role="alert">
            {cancelError}
          </p>
        )}
        {headerInterventionAction && (
          <div data-workspace-action-message-id={actionMessageId || undefined} data-workspace-attention-key={actionMessageId
            ? attentionKeys?.[actionMessageId]?.[String(headerInterventionAction.approval_request_id || headerInterventionAction.approval_token || actionMessageId)]
            : undefined}>
            <WorkflowRunIntervention
              key={`${foregroundRun.id}:${actionMessageId || headerInterventionAction.kind}`}
              run={foregroundRun}
              action={headerInterventionAction}
              workspaceId={resolvedWorkspaceId || undefined}
              onResolve={resolveIntervention}
              disabled={resolving || !canControl}
              loading={resolving}
              error={scopedResolveError || directActionError}
              historyHref={approvalHistoryHref}
              historyReview={approvalReview?.review}
              historyReviewTitle={approvalReview?.reviewTitle}
              historyReviewLoading={approvalReviewQuery.isLoading}
              historyReviewError={approvalReviewQuery.error}
            />
          </div>
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
