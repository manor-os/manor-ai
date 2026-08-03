import { useEffect, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, translateApiError } from "../../lib/api";
import { formatDateLong } from "../../lib/format";
import { t } from "../../lib/i18n";
import { formatUserFacingLabel, formatUserFacingText } from "../../lib/taskDisplay";
import InlineFileReferenceCard from "../InlineFileReferenceCard";
import {
  IconArrowLeft,
  IconChevronDown,
  IconChevronRight,
  IconClock,
  IconEye,
  IconFlow,
  IconRefresh,
} from "../icons";
import Button from "../ui/Button";
import LoadingSpinner from "../ui/LoadingSpinner";
import MediaPreview from "./MediaPreview";
import WorkflowRunIntervention from "./WorkflowRunIntervention";
import {
  buildWorkflowSnapshotNodes,
  buildWorkflowRunTimeline,
  canRetryWithoutCorrection,
  formatWorkflowDuration,
  formatWorkflowError,
  formatWorkflowValue,
  groupWorkflowRunFamilies,
  isWorkflowRunActive,
  normalizeWorkflowArtifactRefs,
  workflowRetrySchemaIsCompatible,
  workflowRunDurationMs,
  workflowRunIsLegacy,
  workflowRunStatusPresentation,
  type WorkflowArtifactRef,
  type WorkflowHistoryNode,
  type WorkflowHistoryRun,
  type WorkflowRunAction,
  type WorkflowRunStatus,
  type WorkflowSnapshotNode,
  type WorkflowRunTimelineEntry,
  type WorkflowRunView,
} from "./workflowRunDisplay";
import type { MediaRef, MediaType } from "../../lib/workflowMedia";

interface WorkflowDefinition {
  id: string;
  name?: string;
  version?: number;
  steps?: WorkflowHistoryNode[];
}

interface WorkflowRunDetailProps {
  runId: string;
  workspaceId: string;
  workflow?: WorkflowDefinition;
  onBack: () => void;
  onSelectRun: (runId: string) => void;
}

const RUN_STATUSES = new Set<WorkflowRunStatus>([
  "pending",
  "running",
  "completed",
  "paused",
  "failed",
  "skipped",
  "cancelled",
]);

function runStatus(value: unknown): WorkflowRunStatus {
  const normalized = typeof value === "string" ? value.toLowerCase() : "pending";
  return RUN_STATUSES.has(normalized as WorkflowRunStatus)
    ? normalized as WorkflowRunStatus
    : "pending";
}

function runIsActive(run?: WorkflowHistoryRun): boolean {
  return Boolean(run && isWorkflowRunActive({ status: runStatus(run.status) }));
}

function statusLabel(status: string): string {
  const normalized = runStatus(status);
  return t(`component.workflow_run.status.${normalized}`);
}

function displayNodeType(type?: string): string {
  return type ? formatUserFacingLabel(type) : t("component.workflow_run_history.unknown_type");
}

function hasDisplayValue(value: unknown): boolean {
  return value !== undefined && value !== null && value !== "";
}

function artifactReference(ref: WorkflowArtifactRef): string {
  if (ref.document_id) return `/viewer/${encodeURIComponent(ref.document_id)}`;
  return ref.fs_path || "";
}

function artifactLabel(ref: WorkflowArtifactRef, index: number): string {
  const reference = artifactReference(ref);
  return ref.name
    || reference.split(/[\\/]/).filter(Boolean).pop()
    || t("component.workflow_run_history.artifact_number", { count: index + 1 });
}

function mediaTypeFor(ref: WorkflowArtifactRef, reference: string): MediaType {
  const mime = (ref.mime_type || "").toLowerCase();
  if (mime.startsWith("image/")) return "image";
  if (mime.startsWith("video/")) return "video";
  if (mime.startsWith("audio/")) return "audio";
  const extension = reference.split(/[?#]/)[0].split(".").pop()?.toLowerCase() || "";
  if (["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif"].includes(extension)) return "image";
  if (["mp4", "webm", "mov", "m4v", "ogv"].includes(extension)) return "video";
  if (["mp3", "wav", "ogg", "m4a", "flac", "aac"].includes(extension)) return "audio";
  return "file";
}

function artifactMediaRef(ref: WorkflowArtifactRef, index: number): MediaRef | null {
  const reference = ref.fs_path || "";
  const canPreview = reference.startsWith("/api/v1/fs/")
    || /^https?:\/\//i.test(reference)
    || reference.startsWith("data:")
    || reference.startsWith("blob:");
  if (!canPreview) return null;
  const type = mediaTypeFor(ref, reference);
  return type === "file" ? null : { url: reference, type, name: artifactLabel(ref, index) };
}

function ArtifactReferences({ refs }: { refs?: WorkflowArtifactRef[] }) {
  const safeRefs = normalizeWorkflowArtifactRefs(refs);
  if (!safeRefs.length) return null;
  return (
    <div className="workflow-run-history-artifacts" aria-label={t("component.workflow_run_history.artifacts")}>
      {safeRefs.map((ref, index) => {
        const reference = artifactReference(ref);
        const media = artifactMediaRef(ref, index);
        return (
          <div className="workflow-run-history-artifact" key={`${reference || ref.name || "artifact"}-${index}`}>
            {reference ? (
              <InlineFileReferenceCard
                reference={reference}
                label={artifactLabel(ref, index)}
                compact
              />
            ) : (
              <span className="workflow-run-history-artifact-label">
                {artifactLabel(ref, index)}
              </span>
            )}
            {(ref.mime_type || ref.status) && (
              <span className="workflow-run-history-artifact-meta">
                {[ref.mime_type, ref.status].filter(Boolean).join(" · ")}
              </span>
            )}
            {media && (
              <details className="workflow-run-history-artifact-preview">
                <summary>
                  <IconEye size={12} />
                  {t("component.workflow_run_history.preview")}
                </summary>
                <div className="workflow-run-history-artifact-media">
                  <MediaPreview refItem={media} maxHeight={220} />
                </div>
              </details>
            )}
          </div>
        );
      })}
    </div>
  );
}

function WorkflowSnapshot({ nodes }: { nodes: WorkflowSnapshotNode[] }) {
  if (!nodes.length) return null;
  return (
    <section className="workflow-run-history-snapshot" aria-labelledby="workflow-run-history-snapshot-title">
      <div className="workflow-run-history-section-heading">
        <div>
          <span>{t("component.workflow_run_history.immutable_definition")}</span>
          <h3 id="workflow-run-history-snapshot-title">
            {t("component.workflow_run_history.definition_snapshot")}
          </h3>
        </div>
        <span className="mono">
          {t("component.workflow_run_history.node_count", { count: nodes.length })}
        </span>
      </div>
      <ol className="workflow-run-history-snapshot-list">
        {nodes.map((node) => (
          <li key={node.nodeId}>
            <span className="workflow-run-history-snapshot-order mono">{node.order + 1}</span>
            <div className="workflow-run-history-snapshot-node">
              <header>
                <div>
                  <strong>{formatUserFacingText(node.nodeName)}</strong>
                  <span className="mono">{node.nodeId}</span>
                </div>
                <span className="workflow-run-history-status" data-status={runStatus(node.status)}>
                  <span aria-hidden="true" />
                  {statusLabel(node.status)}
                </span>
              </header>
              <dl>
                <div>
                  <dt>{t("component.workflow_run_history.node_type")}</dt>
                  <dd>{displayNodeType(node.nodeType)}</dd>
                </div>
                <div>
                  <dt>{t("component.workflow_run_history.frozen_targets")}</dt>
                  <dd className="workflow-run-history-snapshot-targets">
                    {node.targets.length ? node.targets.map((target) => (
                      <span className="mono" key={target}>
                        <IconChevronRight size={11} />
                        {target}
                      </span>
                    )) : <span>{t("component.workflow_run_history.no_targets")}</span>}
                  </dd>
                </div>
              </dl>
            </div>
          </li>
        ))}
      </ol>
    </section>
  );
}

function timelineRunView(
  run: WorkflowHistoryRun,
  title: string,
  timeline: WorkflowRunTimelineEntry[],
): WorkflowRunView {
  const nodeById = new Map<string, WorkflowRunTimelineEntry>();
  for (const entry of timeline) nodeById.set(entry.nodeId, entry);
  if (run.current_step_id && !nodeById.has(run.current_step_id)) {
    nodeById.set(run.current_step_id, {
      node_id: run.current_step_id,
      nodeId: run.current_step_id,
      nodeName: run.current_step_id,
      status: run.status,
      legacy: workflowRunIsLegacy(run),
    });
  }
  return {
    id: run.id,
    title,
    status: runStatus(run.status),
    workflowId: run.workflow_id,
    currentNodeId: run.current_step_id,
    attemptNumber: run.attempt_number,
    startedAt: run.started_at,
    completedAt: run.completed_at,
    error: run.error,
    nodes: Array.from(nodeById.values()).map((entry, index) => ({
      id: entry.nodeId,
      name: entry.nodeName,
      type: entry.nodeType,
      order: index,
      status: runStatus(entry.status),
      error: entry.error,
    })),
  };
}

function ValueBlock({ label, value, error = false }: { label: string; value: unknown; error?: boolean }) {
  if (!hasDisplayValue(value)) return null;
  const formatted = error
    ? formatWorkflowError(value, t("component.workflow_run.error_truncated"))
    : formatWorkflowValue(value, t("component.workflow_run.error_truncated"));
  if (!formatted) return null;
  return (
    <details className="workflow-run-history-value-block" open={error}>
      <summary>{label}</summary>
      <pre className="workflow-run-history-value">{formatted}</pre>
    </details>
  );
}

function WorkflowChildRunDetail({ childRunId }: { childRunId: string }) {
  const childQuery = useQuery({
    queryKey: ["workflow-run-history-child", childRunId],
    queryFn: () => api.workflows.getRun(childRunId),
    refetchInterval: (query) => runIsActive(query.state.data as WorkflowHistoryRun | undefined) ? 1_000 : false,
    refetchIntervalInBackground: true,
  });

  if (childQuery.isLoading) {
    return (
      <div className="workflow-run-history-child-state" aria-live="polite">
        <LoadingSpinner size={16} />
        {t("component.workflow_run_history.loading_child")}
      </div>
    );
  }
  if (childQuery.isError || !childQuery.data) {
    return (
      <div className="workflow-run-history-child-state is-error" role="alert">
        {t("component.workflow_run_history.child_load_error")}
      </div>
    );
  }
  const childRun = childQuery.data as WorkflowHistoryRun;
  const childTimeline = buildWorkflowRunTimeline(childRun);
  return (
    <section className="workflow-run-history-child-detail" aria-label={t("component.workflow_run_history.child_run")}>
      <header>
        <div>
          <span>{t("component.workflow_run_history.child_run")}</span>
          <strong className="mono">{childRun.id}</strong>
        </div>
        <span className="workflow-run-history-status" data-status={runStatus(childRun.status)}>
          <span aria-hidden="true" />
          {statusLabel(childRun.status)}
        </span>
      </header>
      <WorkflowTimeline
        run={childRun}
        timeline={childTimeline}
        allowChildExpansion={false}
        expandedChildRunId=""
        onToggleChild={() => {}}
      />
    </section>
  );
}

function WorkflowTimeline({
  run,
  timeline,
  allowChildExpansion,
  expandedChildRunId,
  onToggleChild,
}: {
  run: WorkflowHistoryRun;
  timeline: WorkflowRunTimelineEntry[];
  allowChildExpansion: boolean;
  expandedChildRunId: string;
  onToggleChild: (runId: string) => void;
}) {
  if (!timeline.length) {
    return <p className="workflow-run-history-no-trace">{t("component.workflow_run_history.no_trace")}</p>;
  }
  return (
    <ol className="workflow-run-history-timeline">
      {timeline.map((entry, index) => (
        <li key={`${entry.sequence || index}-${entry.nodeId}-${entry.status}`}>
          <span className="workflow-run-history-sequence mono">{entry.sequence || index + 1}</span>
          <div className="workflow-run-history-transition">
            <header>
              <div className="workflow-run-history-node-heading">
                <strong>{formatUserFacingText(entry.nodeName)}</strong>
                <span>{displayNodeType(entry.nodeType)}</span>
              </div>
              <span className="workflow-run-history-status" data-status={runStatus(entry.status)}>
                <span aria-hidden="true" />
                {statusLabel(entry.status)}
              </span>
            </header>
            <dl className="workflow-run-history-transition-meta">
              {entry.started_at && (
                <div><dt>{t("component.workflow_run_history.started")}</dt><dd className="mono">{formatDateLong(entry.started_at)}</dd></div>
              )}
              {entry.completed_at && (
                <div><dt>{t("component.workflow_run_history.completed")}</dt><dd className="mono">{formatDateLong(entry.completed_at)}</dd></div>
              )}
              {entry.duration_ms != null && (
                <div><dt>{t("component.workflow_run_history.duration")}</dt><dd className="mono">{formatWorkflowDuration(entry.duration_ms)}</dd></div>
              )}
            </dl>
            <div className="workflow-run-history-values">
              <ValueBlock label={t("component.workflow_run_history.input")} value={entry.input_summary} />
              <ValueBlock label={t("component.workflow_run_history.output")} value={entry.output_summary} />
              <ValueBlock label={t("component.workflow_run_history.error")} value={entry.error} error />
            </div>
            <ArtifactReferences refs={entry.artifact_refs} />
            {allowChildExpansion && entry.child_run_ids?.length ? (
              <div className="workflow-run-history-children">
                <span>{t("component.workflow_run_history.child_runs")}</span>
                {entry.child_run_ids.map((childRunId) => {
                  const expanded = childRunId === expandedChildRunId;
                  return (
                    <div key={childRunId}>
                      <button
                        type="button"
                        className="workflow-run-history-child-toggle"
                        aria-expanded={expanded}
                        onClick={() => onToggleChild(childRunId)}
                      >
                        {expanded ? <IconChevronDown size={13} /> : <IconChevronRight size={13} />}
                        <span className="mono">{childRunId}</span>
                      </button>
                      {expanded && (
                        <WorkflowChildRunDetail childRunId={childRunId} />
                      )}
                    </div>
                  );
                })}
              </div>
            ) : null}
          </div>
        </li>
      ))}
    </ol>
  );
}

export default function WorkflowRunDetail({
  runId,
  workspaceId,
  workflow,
  onBack,
  onSelectRun,
}: WorkflowRunDetailProps) {
  const queryClient = useQueryClient();
  const headingRef = useRef<HTMLHeadingElement>(null);
  const [expandedChildRunId, setExpandedChildRunId] = useState("");
  const detailQuery = useQuery({
    queryKey: ["workflow-run-history-detail", runId],
    queryFn: () => api.workflows.getRun(runId),
    refetchInterval: (query) => (
      runIsActive(query.state.data as WorkflowHistoryRun | undefined) ? 1_000 : false
    ),
    refetchIntervalInBackground: true,
  });
  const run = detailQuery.data as WorkflowHistoryRun | undefined;
  const familyQuery = useQuery({
    queryKey: ["workflow-run-history-family", runId],
    queryFn: () => api.workflows.getRunFamily(runId),
    refetchInterval: (query) => {
      const familyRuns = (query.state.data || []) as WorkflowHistoryRun[];
      return familyRuns.some(runIsActive) ? 1_000 : false;
    },
    refetchIntervalInBackground: true,
  });
  const relatedRuns = (familyQuery.data || []) as WorkflowHistoryRun[];
  const provisionalFamily = run
    ? groupWorkflowRunFamilies([...relatedRuns, run]).find((candidate) => (
        candidate.runs.some((candidateRun) => candidateRun.id === runId)
      ))
    : undefined;
  const controlRunId = provisionalFamily?.latestRun.id || run?.id || "";
  const latestDetailQuery = useQuery({
    queryKey: ["workflow-run-history-detail", controlRunId],
    queryFn: () => api.workflows.getRun(controlRunId),
    enabled: Boolean(
      run
      && familyQuery.isSuccess
      && controlRunId
      && controlRunId !== run.id
    ),
    refetchInterval: (query) => (
      runIsActive(query.state.data as WorkflowHistoryRun | undefined) ? 1_000 : false
    ),
    refetchIntervalInBackground: true,
  });
  const latestDetailedRun = controlRunId === run?.id
    ? run
    : latestDetailQuery.data as WorkflowHistoryRun | undefined;
  const family = run
    ? groupWorkflowRunFamilies([
        ...relatedRuns,
        ...(latestDetailedRun ? [latestDetailedRun] : []),
        run,
      ]).find((candidate) => (
        candidate.runs.some((candidateRun) => candidateRun.id === runId)
      ))
    : undefined;
  const controlRun = latestDetailedRun || family?.latestRun;
  const canControl = controlRun?.capabilities?.can_control === true;
  const canHaveRetry = controlRun?.status === "failed" || controlRun?.status === "completed";
  const controlQuery = useQuery({
    queryKey: ["workflow-run-history-control", controlRunId],
    queryFn: () => api.workflows.getRun(controlRunId, false),
    enabled: Boolean(familyQuery.isSuccess && controlRunId && canControl && canHaveRetry),
  });
  const compactRun = controlQuery.data as WorkflowHistoryRun | undefined;
  const intervention = compactRun?.intervention?.kind === "workflow_retry"
    ? compactRun.intervention
    : null;

  useEffect(() => {
    setExpandedChildRunId("");
  }, [runId]);

  useEffect(() => {
    headingRef.current?.focus();
  }, [run?.id]);

  const retryMutation = useMutation({
    mutationFn: ({
      sourceRun,
      action,
      variables,
    }: {
      sourceRun: WorkflowHistoryRun;
      action?: WorkflowRunAction | null;
      variables?: Record<string, unknown>;
    }) => api.workflows.retryRun(sourceRun.id, {
      from_step_id: action?.retry_from_step_id
        || sourceRun.retry_from_step_id
        || sourceRun.current_step_id,
      variables,
      execute: true,
    }),
    onSuccess: (retriedRun: WorkflowHistoryRun) => {
      queryClient.invalidateQueries({ queryKey: ["workspace-workflow-runs", workspaceId] });
      queryClient.invalidateQueries({ queryKey: ["workflow-run-history-family"] });
      onSelectRun(retriedRun.id);
    },
  });

  if (detailQuery.isLoading) {
    return (
      <section className="workflow-run-history-detail is-loading" aria-live="polite">
        <LoadingSpinner size={24} />
        <span>{t("component.workflow_run_history.loading_detail")}</span>
      </section>
    );
  }
  if (detailQuery.isError || !run) {
    return (
      <section className="workflow-run-history-detail is-error" role="alert">
        <IconFlow size={24} />
        <strong>{t("component.workflow_run_history.detail_load_error")}</strong>
        <p>{translateApiError(detailQuery.error, t("component.workflow_run_history.try_again"))}</p>
        <div className="workflow-run-history-error-actions">
          <Button variant="outline" size="sm" onClick={onBack}>
            <IconArrowLeft size={14} />
            {t("component.workflow_run_history.back")}
          </Button>
          <Button size="sm" onClick={() => detailQuery.refetch()}>
            <IconRefresh size={14} />
            {t("component.workflow_run_history.try_again")}
          </Button>
        </div>
      </section>
    );
  }

  if (!family || !controlRun) return null;
  const currentDefinitionNodes = workflow?.steps || [];
  const timeline = buildWorkflowRunTimeline(run, currentDefinitionNodes);
  const snapshotNodes = buildWorkflowSnapshotNodes(run, currentDefinitionNodes);
  const snapshot = run.definition_snapshot || {};
  const legacy = workflowRunIsLegacy(run);
  const legacyLineage = run.lineage_status === "legacy_untrusted_incomplete";
  const workflowName = snapshot.name || run.workflow_name || workflow?.name || run.workflow_id || t("component.workflow_run_history.unknown_workflow");
  const failedEntry = [...timeline].reverse().find((entry) => entry.status === "failed");
  const currentEntry = run.current_step_id
    ? [...timeline].reverse().find((entry) => entry.nodeId === run.current_step_id)
    : undefined;
  const contextEntry = failedEntry || currentEntry;
  const controlTimeline = controlRun.id === run.id
    ? timeline
    : buildWorkflowRunTimeline(controlRun, currentDefinitionNodes);
  const controlRunView = timelineRunView(controlRun, workflowName, controlTimeline);
  const summaryBlocker = formatWorkflowError(
    family.blocker,
    t("component.workflow_run.error_truncated"),
  );
  const familyStatusPresentation = workflowRunStatusPresentation({
    status: runStatus(family.status),
    businessOutcome: family.businessOutcome,
  });
  const interventionSchemaCompatible = Boolean(
    intervention
    && workflowRetrySchemaIsCompatible(intervention.editable_input_schema),
  );
  const directRetryAllowed = Boolean(
    controlQuery.isSuccess
    && !intervention
    && canRetryWithoutCorrection({
      ...controlRun,
      capabilities: compactRun?.capabilities || controlRun.capabilities,
    }),
  );
  const visibleIntervention = intervention && interventionSchemaCompatible
    ? { ...intervention, options: (intervention.options || []).filter((option) => option.toLowerCase().startsWith("retry")) }
    : null;
  const showControlSurface = canControl && canHaveRetry && (
    controlQuery.isLoading
    || controlQuery.isError
    || Boolean(visibleIntervention)
    || directRetryAllowed
  );

  return (
    <section className="workflow-run-history-detail" aria-labelledby="workflow-run-history-title">
      <header className="workflow-run-history-detail-header">
        <Button variant="ghost" size="sm" onClick={onBack}>
          <IconArrowLeft size={14} />
          {t("component.workflow_run_history.back")}
        </Button>
        <div>
          <span>{t("component.workflow_run_history.run_detail")}</span>
          <h2 id="workflow-run-history-title" ref={headingRef} tabIndex={-1}>
            {formatUserFacingText(workflowName)}
          </h2>
        </div>
        <span className="workflow-run-history-status" data-status={familyStatusPresentation.iconStatus}>
          <span aria-hidden="true" />
          {t(`component.workflow_run.status.${familyStatusPresentation.labelKey}`)}
        </span>
      </header>

      {legacy && (
        <div className="workflow-run-history-legacy" role="status">
          {t("component.workflow_run_history.legacy")}
        </div>
      )}

      {legacyLineage && (
        <div className="workflow-run-history-legacy" role="status">
          {t("component.workflow_run_history.legacy_lineage_untrusted")}
        </div>
      )}

      <section className="workflow-run-history-summary" data-status={familyStatusPresentation.iconStatus}>
        <div className="workflow-run-history-section-heading">
          <div>
            <span>{t("component.workflow_run_history.run_detail")}</span>
            <h3>{t("component.workflow_run_history.execution_summary")}</h3>
          </div>
        </div>
        <dl className="workflow-run-history-summary-metrics">
          <div>
            <dt>{t("component.workflow_run_history.outcome")}</dt>
            <dd>{formatUserFacingLabel(family.businessOutcome || family.status)}</dd>
          </div>
          <div>
            <dt>{t("component.workflow_run_history.attempt")}</dt>
            <dd>{t("component.workflow_run_history.attempts", { count: family.attemptCount })}</dd>
          </div>
          <div>
            <dt>{t("component.workflow_run_history.started")}</dt>
            <dd className="mono">{formatDateLong(family.startedAt)}</dd>
          </div>
          <div>
            <dt>{t("component.workflow_run_history.duration")}</dt>
            <dd className="mono">{formatWorkflowDuration(family.durationMs)}</dd>
          </div>
          <div>
            <dt>{t("component.workflow_run_history.progress")}</dt>
            <dd>{t("component.workflow_run_history.steps_progress", {
              count: family.processedCount,
              total: family.totalCount,
            })}</dd>
          </div>
          <div>
            <dt>{t("component.workflow_run_history.artifacts")}</dt>
            <dd>{t("component.workflow_run_history.artifact_count", { count: family.artifactCount || 0 })}</dd>
          </div>
        </dl>
        {summaryBlocker && (
          <div className="workflow-run-history-summary-blocker">
            <span>{t("component.workflow_run_history.blocker")}</span>
            <p>{summaryBlocker}</p>
          </div>
        )}
        <ArtifactReferences refs={family.artifactRefs} />
      </section>

      {(Boolean(contextEntry) || hasDisplayValue(run.error)) && (
        <section className="workflow-run-history-context" data-status={runStatus(run.status)}>
          <div className="workflow-run-history-context-heading">
            <IconClock size={16} />
            <div>
              <span>{failedEntry ? t("component.workflow_run_history.failure_context") : t("component.workflow_run_history.current_context")}</span>
              <strong>{formatUserFacingText(contextEntry?.nodeName || run.current_step_id || statusLabel(run.status))}</strong>
            </div>
          </div>
          <ValueBlock label={t("component.workflow_run_history.error")} value={contextEntry?.error ?? run.error} error />
        </section>
      )}

      {showControlSurface && (
        <section className="workflow-run-history-controls" aria-label={t("component.workflow_run_history.controls")}>
          {controlQuery.isLoading ? (
            <div className="workflow-run-history-control-state" aria-live="polite">
              <LoadingSpinner size={15} />
              {t("component.workflow_run_history.loading_controls")}
            </div>
          ) : controlQuery.isError ? (
            <p className="workflow-run-history-control-error" role="alert">
              {t("component.workflow_run_history.control_load_error")}
            </p>
          ) : visibleIntervention ? (
            <WorkflowRunIntervention
              run={controlRunView}
              action={visibleIntervention}
              loading={retryMutation.isPending}
              error={retryMutation.error}
              onResolve={async (_choice, _note, payload) => {
                await retryMutation.mutateAsync({
                  sourceRun: controlRun,
                  action: visibleIntervention,
                  variables: payload?.variables as Record<string, unknown> | undefined,
                });
              }}
            />
          ) : directRetryAllowed ? (
            <div className="workflow-run-history-direct-retry">
              {retryMutation.isError && (
                <p role="alert">{translateApiError(retryMutation.error, t("component.workflow_run_history.retry_error"))}</p>
              )}
              <Button
                size="sm"
                loading={retryMutation.isPending}
                onClick={() => retryMutation.mutate({ sourceRun: controlRun })}
              >
                <IconRefresh size={14} />
                {t("component.workflow_run_history.retry")}
              </Button>
            </div>
          ) : null}
        </section>
      )}

      <details className="workflow-run-history-technical">
        <summary>
          <span>
            <IconFlow size={15} />
            {t("component.workflow_run_history.technical_details")}
          </span>
          <span>{t("component.workflow_run_history.transition_count", { count: timeline.length })}</span>
        </summary>
        <div className="workflow-run-history-technical-body">
          <dl className="workflow-run-history-metadata">
            <div><dt>{t("component.workflow_run_history.run_id")}</dt><dd className="mono">{run.id}</dd></div>
            <div><dt>{t("component.workflow_run_history.attempt")}</dt><dd className="mono">{run.attempt_number || 1}</dd></div>
            <div><dt>{t("component.workflow_run_history.trigger")}</dt><dd>{formatUserFacingLabel(run.trigger_source || "manual")}</dd></div>
            <div><dt>{t("component.workflow_run_history.started")}</dt><dd className="mono">{formatDateLong(run.started_at || run.created_at)}</dd></div>
            <div><dt>{t("component.workflow_run_history.duration")}</dt><dd className="mono">{formatWorkflowDuration(workflowRunDurationMs(run))}</dd></div>
            <div><dt>{t("component.workflow_run_history.definition_version")}</dt><dd className="mono">{snapshot.version ?? workflow?.version ?? "--"}</dd></div>
            <div className="workflow-run-history-fingerprint">
              <dt>{t("component.workflow_run_history.fingerprint")}</dt>
              <dd className="mono">{snapshot.fingerprint || run.workflow_definition_fingerprint || "--"}</dd>
            </div>
          </dl>

          {family.runs.length > 1 && (
            <section className="workflow-run-history-attempts" aria-labelledby="workflow-run-history-attempts-title">
              <h3 id="workflow-run-history-attempts-title">{t("component.workflow_run_history.retry_attempts")}</h3>
              <div>
                {family.runs.map((attempt) => (
                  <button
                    type="button"
                    key={attempt.id}
                    disabled={attempt.id === run.id}
                    onClick={() => onSelectRun(attempt.id)}
                  >
                    {t("component.workflow_run_history.attempt_number", { count: attempt.attempt_number || 1 })}
                    <span className="mono">{attempt.id}</span>
                    {attempt.id !== run.id && <IconChevronRight size={12} />}
                  </button>
                ))}
              </div>
            </section>
          )}

          <WorkflowSnapshot nodes={snapshotNodes} />

          <section className="workflow-run-history-trace" aria-labelledby="workflow-run-history-trace-title">
            <div className="workflow-run-history-section-heading">
              <div>
                <span>{t("component.workflow_run_history.timeline")}</span>
                <h3 id="workflow-run-history-trace-title">{t("component.workflow_run_history.execution_trace")}</h3>
              </div>
              <span className="mono">{t("component.workflow_run_history.transition_count", { count: timeline.length })}</span>
            </div>
            <WorkflowTimeline
              run={run}
              timeline={timeline}
              allowChildExpansion
              expandedChildRunId={expandedChildRunId}
              onToggleChild={(childRunId) => setExpandedChildRunId((current) => current === childRunId ? "" : childRunId)}
            />
          </section>
        </div>
      </details>
    </section>
  );
}
