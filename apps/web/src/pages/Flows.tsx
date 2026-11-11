import { useEffect, useMemo, useRef, useState } from "react";
import { useLocation, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import { displayReadInterval } from "../lib/readRefreshPolicy";
import { usePageVisible } from "../lib/usePageVisible";
import { isOlderWorkflowRunSnapshot, selectedWorkflowRunRefetchInterval, workflowRunListRefetchInterval } from "../lib/workspaceLiveRefresh";
import { workflowPath, workflowRunPath } from "../lib/workflowRoutes";
import { useToastStore } from "../stores/toast";
import PageHeader, {
  PageHeaderAddButton,
  PageHeaderTitle,
} from "../components/ui/PageHeader";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
import AiEditPreviewControls from "../components/ui/AiEditPreviewControls";
import Input from "../components/ui/Input";
import Textarea from "../components/ui/Textarea";
import { CardGridSkeleton } from "../components/ui/Skeleton";
import CompactCard from "../components/ui/CompactCard";
import ContextMenu, { type MenuItem, useContextMenu } from "../components/ui/ContextMenu";
import Dropdown from "../components/ui/Dropdown";
import IconTile from "../components/ui/IconTile";
import StatusBadge from "../components/ui/StatusBadge";
import { closeDetail, openDetail } from "../stores/detail";
import {
  IconChevronLeft,
  IconClose,
  IconEdit,
  IconFlow,
  IconClock,
  IconInfo,
  IconMoreHorizontal,
  IconPause,
  IconPlay,
  IconPlus,
  IconRefresh,
  IconStop,
  IconTrash,
  IconUpload,
} from "../components/icons";
import SmartToolbar from "../components/ui/SmartToolbar";
import HoverMarqueeText from "../components/ui/HoverMarqueeText";
import WorkflowImportModal from "../components/workflows/WorkflowImportModal";
import WorkflowDeployModal from "../components/workflows/WorkflowDeployModal";
import WorkflowRunInputModal from "../components/workflows/WorkflowRunInputModal";
import WorkflowRunResumeControl from "../components/workflows/WorkflowRunResumeControl";
import WorkflowRunRetryControl from "../components/workflows/WorkflowRunRetryControl";
import ChatMarkdown from "../components/ChatMarkdown";
import WorkflowCanvas, { NodeIcon, type CanvasStep } from "../components/workflows/WorkflowCanvas";
import { agentListQueryOptions, useCatalogScope } from "../lib/catalogQueries";
import WorkflowNodePalette from "../components/workflows/WorkflowNodePalette";
import WorkflowNodeConfigPanel from "../components/workflows/WorkflowNodeConfigPanel";
import { workflowStepOutputs } from "../lib/workflowBindings";
import InlineFileReferenceCard from "../components/InlineFileReferenceCard";
import MediaPreview from "../components/workflows/MediaPreview";
import { WorkflowValueWithJson } from "../components/workflows/WorkflowValueView";
import WorkflowTemplates, { type WorkflowTemplate } from "../components/workflows/WorkflowTemplates";
import {
  AiEditPreviewStatus,
  AiEditTargetKind,
  closeEditorLiveChat,
  createAiEditCommitCoordinator,
  createEditorLiveAdapter,
  nextEditorLiveChangeCount,
  openEditorLiveChat,
} from "../lib/editorLiveChat";
import { parseWorkflowLiveEdit, serializeWorkflowLiveEdit } from "../lib/workflowLiveEdit";
import { extractMediaRefs, primaryMediaRef, workflowFinalMediaRefs, type MediaRef } from "../lib/workflowMedia";
import { validateWorkflow, issuesByNode } from "../lib/workflowValidate";
import { workflowRunInputs } from "../lib/workflowRunInputs";
import { parseWorkflowFileCollection } from "../lib/workflowFileCollection";
import WorkflowFileCollection from "../components/workflows/WorkflowFileCollection";
import {
  resolveWorkflowFinalResult,
  orderWorkflowFinalArtifacts,
  workflowArtifactLabel,
  workflowFinalArtifactRefs,
  workflowFinalFileListKeys,
  workflowFinalHeadline,
  workflowFinalOutputFields,
  workflowEditorRunProgress,
  workflowNodeDisplayStatus,
  workflowRunBackgroundWait,
  workflowRunFailure,
  workflowRunProgressPercent,
  workflowStepTargets,
  buildWorkflowSnapshotNodes,
  buildWorkflowRunTimeline,
  canRetryWithoutCorrection,
  canCancelWorkflowRun,
  workflowRunIsLegacy,
  type WorkflowHistoryRun,
  type WorkflowFinalResult,
  type WorkflowRunFailure,
  workflowRunStateKey,
} from "../components/workflows/workflowRunDisplay";
import { getPreferredTimeZone } from "../lib/format";
import WorkflowErrorText from "../components/workflows/WorkflowErrorText";
import "./Flows.css";

const ACTIVE_RUN_STATUSES = new Set(["pending", "running", "paused"]);

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface FlowStep {
  id: string;
  type: string;
  name: string;
  status?: "pending" | "running" | "done" | "error";
  config?: Record<string, unknown>;
}

interface Flow {
  id: string;
  name: string;
  description: string;
  workspace_id?: string | null;
  workspace_name?: string | null;
  icon?: string;
  trigger: "manual" | "event" | "schedule" | "webhook" | "mcp" | "internal";
  trigger_type?: "manual" | "event" | "schedule" | "webhook" | "mcp" | "internal";
  trigger_config?: Record<string, unknown>;
  variables?: Record<string, unknown>;
  category?: string | null;
  tags?: string[];
  status: "active" | "draft";
  steps: FlowStep[];
  /** Set only on list summaries, whose `steps` are empty. */
  step_count?: number | null;
  last_run?: string;
  created_by?: string | null;
  created_at: string;
  updated_at?: string | null;
  /** Moves on every save; sent back so a save made elsewhere is never overwritten. */
  version?: number;
}

interface WorkflowAiPreview {
  baseline: Flow;
  current: Flow;
  status: AiEditPreviewStatus;
  changeCount: number;
}

interface WorkflowMetadata {
  workflow_id: string;
  created_by?: string | null;
  creator?: { id: string; name: string } | null;
  created_at: string;
  updated_at?: string | null;
  version: number;
  status: string;
  trigger_type: string;
  binding_count: number;
  workspace_count: number;
  standalone_binding_count: number;
  workspace_usage: Array<{
    binding_id: string;
    binding_name?: string | null;
    workspace_id: string;
    workspace_name?: string | null;
    business_line?: string | null;
    trigger_type: string;
    enabled: boolean;
    status: string;
    created_at: string;
  }>;
}

function getWorkflowReturnTo(state: unknown): string | null {
  if (!state || typeof state !== "object") return null;
  const value = (state as { chatReturnTo?: unknown; returnTo?: unknown }).chatReturnTo
    ?? (state as { returnTo?: unknown }).returnTo;
  return typeof value === "string" && value.startsWith("/") && !value.startsWith("//")
    ? value
    : null;
}

/* ------------------------------------------------------------------ */
/*  Step type icons                                                    */
/* ------------------------------------------------------------------ */

const STEP_ICONS: Record<FlowStep["type"], React.ReactNode> = {
  agent: (
    <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M9.813 15.904L9 18.75l-.813-2.846a4.5 4.5 0 00-3.09-3.09L2.25 12l2.846-.813a4.5 4.5 0 003.09-3.09L9 5.25l.813 2.846a4.5 4.5 0 003.09 3.09L15.75 12l-2.846.813a4.5 4.5 0 00-3.09 3.09z" />
    </svg>
  ),
  tool: (
    <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M11.42 15.17l-5.1 5.1a2.121 2.121 0 01-3-3l5.1-5.1m0 0L15.17 4.42a2.121 2.121 0 013 3l-7.75 7.75z" />
    </svg>
  ),
  condition: (
    <IconFlow size={16} />
  ),
  wait: (
    <IconClock size={16} />
  ),
  notify: (
    <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M14.857 17.082a23.848 23.848 0 005.454-1.31A8.967 8.967 0 0118 9.75v-.7V9A6 6 0 006 9v.75a8.967 8.967 0 01-2.312 6.022c1.733.64 3.56 1.085 5.455 1.31m5.714 0a24.255 24.255 0 01-5.714 0m5.714 0a3 3 0 11-5.714 0" />
    </svg>
  ),
  transform: (
    <svg style={{ width: 16, height: 16 }} fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
      <path strokeLinecap="round" strokeLinejoin="round" d="M17.25 6.75L22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3l-4.5 16.5" />
    </svg>
  ),
};

const STEP_GRADIENTS: Record<FlowStep["type"], string> = {
  agent: "linear-gradient(135deg, #4f7d75, #436b65)",
  tool: "linear-gradient(135deg, #cf9b44, #b66a3c)",
  condition: "linear-gradient(135deg, #a07fc0, #c96a98)",
  wait: "linear-gradient(135deg, #5f84bd, #5a55a6)",
  notify: "linear-gradient(135deg, #4f9c84, #5f928a)",
  transform: "linear-gradient(135deg, #5e9098, #5f84bd)",
};

const TRIGGER_LABELS: Record<string, string> = {
  manual: "page.flows.trigger_manual",
  event: "page.flows.trigger_event",
  schedule: "page.flows.trigger_schedule",
};

const FLOW_ICON_OPTIONS = [
  { value: "flow", label: t("ui.workflow") },
  { value: "llm", label: "AI" },
  { value: "image", label: t("ui.image") },
  { value: "video", label: t("ui.video") },
  { value: "rag", label: t("ui.knowledge") },
  { value: "classifier", label: t("ui.classify") },
  { value: "http", label: t("ui.web") },
  { value: "code", label: t("ui.code") },
  { value: "notify", label: t("ui.notify") },
  { value: "wait", label: t("ui.schedule") },
] as const;

function workflowIconGlyph(icon?: string, size = 18) {
  return icon && icon !== "flow"
    ? <NodeIcon type={icon} size={size} />
    : <IconFlow size={size} />;
}

const STATUS_DOT: Record<string, string> = {
  pending: "#aeaeb2",
  running: "#cf9b44",
  done: "#4f9c84",
  error: "#d65f59",
};

const RUN_STATUS_COLOR: Record<string, string> = {
  completed: "#4f9c84",
  failed: "#d65f59",
  paused: "#cf9b44",
  running: "#0f766e",
};

function fmtRunTime(iso?: string): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: getPreferredTimeZone() });
}

function formatWorkflowTimestamp(iso?: string | null): string {
  if (!iso) return "—";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return iso;
  return value.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
    timeZone: getPreferredTimeZone(),
  });
}

function WorkflowMetadataPanel({ workflowId }: { workflowId: string }) {
  const { data, isLoading, isError } = useQuery<WorkflowMetadata>({
    queryKey: ["workflow-metadata", workflowId],
    queryFn: () => api.workflows.metadata(workflowId),
  });

  if (isLoading) {
    return (
      <div className="workflow-metadata-state" role="status">
        <LoadingSpinner size={20} />
        <span className="sr-only">{t("page.flows.details_loading")}</span>
      </div>
    );
  }
  if (isError || !data) {
    return (
      <div className="workflow-metadata-state is-error">
        {t("page.flows.details_failed")}
      </div>
    );
  }

  const workspaceGroups = Object.values(data.workspace_usage.reduce((groups, usage) => {
    const existing = groups[usage.workspace_id];
    if (existing) {
      existing.bindings.push(usage);
    } else {
      groups[usage.workspace_id] = {
        workspaceId: usage.workspace_id,
        workspaceName: usage.workspace_name || usage.workspace_id,
        bindings: [usage],
      };
    }
    return groups;
  }, {} as Record<string, {
    workspaceId: string;
    workspaceName: string;
    bindings: WorkflowMetadata["workspace_usage"];
  }>));

  return (
    <div className="workflow-metadata">
      <section className="workflow-metadata-section">
        <h3>{t("page.flows.metadata")}</h3>
        <dl className="workflow-metadata-grid">
          <div>
            <dt>{t("page.flows.created_by")}</dt>
            <dd>{data.creator?.name || t("page.flows.creator_not_recorded")}</dd>
          </div>
          <div>
            <dt>{t("page.flows.created_at")}</dt>
            <dd>{formatWorkflowTimestamp(data.created_at)}</dd>
          </div>
          <div>
            <dt>{t("page.flows.updated_at")}</dt>
            <dd>{formatWorkflowTimestamp(data.updated_at || data.created_at)}</dd>
          </div>
          <div>
            <dt>{t("page.flows.version")}</dt>
            <dd className="mono">v{data.version}</dd>
          </div>
          <div>
            <dt>{t("page.flows.trigger_type")}</dt>
            <dd>{t(TRIGGER_LABELS[data.trigger_type] || data.trigger_type)}</dd>
          </div>
          <div>
            <dt>{t("page.flows.workflow_id")}</dt>
            <dd className="mono" title={data.workflow_id}>{data.workflow_id}</dd>
          </div>
        </dl>
      </section>

      <section className="workflow-metadata-section">
        <div className="workflow-metadata-section-heading">
          <h3>{t("page.flows.workspace_usage")}</h3>
          <span className="mono">{data.workspace_count}</span>
        </div>
        {workspaceGroups.length === 0 ? (
          <p className="workflow-metadata-empty">{t("page.flows.no_workspace_usage")}</p>
        ) : (
          <div className="workflow-workspace-usage">
            {workspaceGroups.map((group) => {
              const triggerTypes = [...new Set(group.bindings.map((binding) => binding.trigger_type))];
              const active = group.bindings.some((binding) => binding.enabled && binding.status === "active");
              const summary = group.bindings.length === 1
                ? group.bindings[0].binding_name || group.bindings[0].trigger_type
                : `${group.bindings.length} ${t("page.flows.bindings")} · ${triggerTypes.join(", ")}`;
              return (
                <div className="workflow-workspace-usage-row" key={group.workspaceId}>
                  <IconTile size={32}><IconFlow size={15} /></IconTile>
                  <div>
                    <strong>{group.workspaceName}</strong>
                    <span>{summary}</span>
                  </div>
                  <StatusBadge type={active ? "active" : "gray"} dot>
                    {active ? t("page.flows.active") : t("page.flows.disabled")}
                  </StatusBadge>
                </div>
              );
            })}
          </div>
        )}
        {(data.binding_count > data.workspace_usage.length || data.standalone_binding_count > 0) && (
          <p className="workflow-metadata-footnote">
            {t("page.flows.bindings")}: <span className="mono">{data.binding_count}</span>
            {data.standalone_binding_count > 0 && (
              <> · {t("page.flows.standalone_bindings")}: <span className="mono">{data.standalone_binding_count}</span></>
            )}
          </p>
        )}
      </section>
    </div>
  );
}


function workflowNodeOutputs(step: any): { name: string; type: string }[] {
  const named = workflowStepOutputs(step)
    .map((output: any) => ({
      name: String(output?.key || output?.name || "").trim(),
      type: String(output?.type || "any").toLowerCase() === "string" ? "text" : output?.type || "any",
    }))
    .filter((output: any) => output.name);
  return named;
}

function hasWorkflowOutput(value: unknown): boolean {
  return value !== undefined && value !== null && value !== "";
}

function WorkflowFinalResultPanel({
  run,
  result,
  workflowName,
  processedCount,
  outputPending,
  failure,
  onRunAgain,
  onClose,
  onOpenNode,
  retryControl,
}: {
  run: any;
  result: WorkflowFinalResult | null;
  workflowName: string;
  processedCount: number;
  /** The run is a compact history row whose step results are still loading. */
  outputPending: boolean;
  failure: WorkflowRunFailure | null;
  onRunAgain?: () => void;
  onClose: () => void;
  onOpenNode: (stepId: string) => void;
  retryControl?: React.ReactNode;
}) {
  const failed = run?.status === "failed";
  // A failed run can still carry the output of the node it stopped at (for
  // example per-platform publish results); show it under the failure.
  const failedWithOutput = failed && !!failure && hasWorkflowOutput(result?.output);
  const output = failed && !failedWithOutput ? run?.error : result?.output;
  const fileCollection = failed && !failedWithOutput ? null : parseWorkflowFileCollection(output);
  const artifacts = failed && !failedWithOutput ? [] : orderWorkflowFinalArtifacts(workflowFinalArtifactRefs(output, 50));
  // Generic files are listed once, as file cards. Only when nothing resolves
  // to a card do bare file URLs fall back to download rows.
  const media = failed && !failedWithOutput ? [] : [
    ...workflowFinalMediaRefs(output),
    ...(artifacts.length === 0 ? extractMediaRefs(output, 3).filter((ref) => ref.type === "file") : []),
  ];
  // Previewed media already shows in full; list the remaining files.
  const isPreviewed = (artifact: (typeof artifacts)[number]) => media.some((ref) => {
    let url = ref.url;
    try { url = decodeURIComponent(ref.url); } catch { /* keep raw */ }
    return (!!artifact.fs_path && url.split(/[?#]/)[0].endsWith(`/${artifact.fs_path}`))
      || (!!artifact.document_id && url.includes(artifact.document_id));
  });
  const listedFiles = artifacts.filter((artifact) => !isPreviewed(artifact));
  const primaryFiles = listedFiles.slice(0, 3);
  const otherFiles = listedFiles.slice(3);
  const headline = failed && !failedWithOutput ? null : workflowFinalHeadline(output);
  const showHero = !!headline || artifacts.length > 0;
  const outputFields = workflowFinalOutputFields(output, {
    omitKeys: [
      ...(headline?.keys || []),
      ...(artifacts.length > 0 ? workflowFinalFileListKeys(output) : []),
    ],
  });
  const renderArtifact = (artifact: (typeof artifacts)[number], index: number) => (
    <InlineFileReferenceCard
      key={`${artifact.document_id || artifact.fs_path || "file"}-${index}`}
      reference={artifact.document_id
        ? `/viewer/${encodeURIComponent(artifact.document_id)}`
        : artifact.fs_path || ""}
      label={workflowArtifactLabel(artifact, `File ${index + 1}`)}
      compact
    />
  );
  const packageOutput = !failed && output && typeof output === "object" && !Array.isArray(output)
    && Array.isArray((output as any).files) && Array.isArray((output as any).assets)
    && typeof (output as any).bundle_path === "string"
      ? output as Record<string, any>
      : null;
  const packageAssets = packageOutput?.assets as Array<Record<string, any>> || [];
  const packageFiles = packageOutput?.files as Array<Record<string, any>> || [];
  const essentialFiles = packageFiles.filter((file) =>
    ["README.md", "release.json", "lyrics.txt"].includes(String(file.path || "").split("/").pop() || ""),
  );
  const otherPackageFiles = packageFiles.filter((file) => !essentialFiles.includes(file));
  const renderPackageFile = (file: Record<string, any>, index: number) => (
    <InlineFileReferenceCard
      key={String(file.document_id || file.fs_path || file.path || index)}
      reference={file.document_id
        ? `/viewer/${encodeURIComponent(String(file.document_id))}`
        : String(file.fs_path || file.path || "")}
      label={String(file.path || file.fs_path || "File").split("/").pop() || "File"}
      compact
    />
  );
  const runInputs = Object.entries(run?.trigger_data || {}).filter(
    ([key, value]) => (
      key
      && !key.startsWith("_")
      && key !== "runtime_context"
      && value !== undefined
    ),
  );
  return (
    <section
      className="workflow-final-result"
      role="region"
      aria-label={t("ui.workflow_final_result")}
      aria-live="polite"
    >
      <div className="workflow-final-result-header">
        <div style={{ minWidth: 0 }}>
          <div className="workflow-final-result-title">
            {failed ? "Run failed" : "Run completed"}
          </div>
          <div className="workflow-final-result-meta">
            <span
              aria-hidden="true"
              className={`workflow-final-result-dot${failed ? " is-failed" : ""}`}
            />
            <span>{workflowName || "Workflow"}</span>
            {processedCount > 0 && <span>· {processedCount} {processedCount === 1 ? "step" : "steps"}</span>}
            {run?.completed_at && <span>· {fmtRunTime(run.completed_at)}</span>}
          </div>
        </div>
        <button
          type="button"
          className="workflow-final-result-close"
          onClick={onClose}
          title={t("ui.close_final_result")}
          aria-label={t("ui.close_final_result")}
        >
          <IconClose size={15} />
        </button>
      </div>
      <div className="workflow-final-result-body">
        {failure ? (
          <section className="workflow-final-result-failure" role="alert">
            <strong>{failure.title}</strong>
            <WorkflowErrorText text={failure.detail} />
            {failure.hint && <p className="workflow-final-result-failure-hint">{failure.hint}</p>}
            {failure.technicalDetails && (
              <details className="workflow-final-result-failure-details">
                <summary>{t("component.workflow_run.technical_details")}</summary>
                <pre>{failure.technicalDetails}</pre>
              </details>
            )}
            <div className="workflow-final-result-failure-actions">
              {onRunAgain && <Button variant="outline" size="sm" onClick={onRunAgain}>
                {t("ui.edit_inputs_and_run_again")}
              </Button>}
            </div>
          </section>
        ) : null}
        {(!failure || failedWithOutput) && (
        <section className="workflow-final-result-output" aria-labelledby="workflow-final-result-output-title">
          <div id="workflow-final-result-output-title" className="workflow-final-result-section-title">
            {failed && !failedWithOutput ? "Error" : "Output"}
          </div>
          <div className={`workflow-final-result-content${failed && !failedWithOutput ? " is-failed" : ""}`}>
            {outputPending && !hasWorkflowOutput(output) ? (
              <span className="workflow-final-result-empty">{t("ui.loading_the_run_output")}</span>
            ) : !hasWorkflowOutput(output) ? (
              <span className="workflow-final-result-empty">{t("ui.this_run_did_not_produce_a_final_output")}</span>
            ) : packageOutput ? (
              <div className="workflow-release-package">
                <div className="workflow-release-package-hero">
                  <span className="workflow-release-package-count">
                    {packageAssets.length + packageFiles.length} files ready
                  </span>
                  <strong>{String(packageOutput.title || "Release package")}</strong>
                  <p>{String(packageOutput.summary || "Your release files are ready to review.")}</p>
                </div>
                <div className="workflow-release-package-group">
                  <span>{t("ui.audio_cover")}</span>
                  <div className="workflow-release-package-files">
                    {packageAssets.map(renderPackageFile)}
                  </div>
                </div>
                <div className="workflow-release-package-group">
                  <span>{t("ui.start_here")}</span>
                  <div className="workflow-release-package-files">
                    {essentialFiles.map(renderPackageFile)}
                  </div>
                </div>
                {otherPackageFiles.length > 0 && (
                  <details className="workflow-release-package-more">
                    <summary>All other files <span>{otherPackageFiles.length}</span></summary>
                    <div className="workflow-release-package-files">
                      {otherPackageFiles.map(renderPackageFile)}
                    </div>
                  </details>
                )}
              </div>
            ) : fileCollection ? (
              <WorkflowFileCollection collection={fileCollection} />
            ) : (
              <>
                {showHero && (
                  <div className="workflow-final-result-hero">
                    {artifacts.length > 0 && (
                      <span className="workflow-final-result-hero-count">
                        {artifacts.length} {artifacts.length === 1 ? "file" : "files"} ready
                      </span>
                    )}
                    {headline?.title && <strong>{headline.title}</strong>}
                    {headline?.summary && <p>{headline.summary}</p>}
                  </div>
                )}
                {media.length > 0 && (
                  <div className="workflow-final-result-media">
                    {media.map((item, index) => <MediaPreview key={`${item.type}-${index}`} refItem={item} maxHeight={360} />)}
                  </div>
                )}
                {listedFiles.length > 0 && (
                  <div className="workflow-final-result-artifacts" aria-label={t("ui.output_files")}>
                    {primaryFiles.map((artifact, index) => renderArtifact(artifact, index))}
                    {otherFiles.length > 0 && (
                      <details className="workflow-final-result-more">
                        <summary>All other files <span>{otherFiles.length}</span></summary>
                        <div className="workflow-final-result-artifacts">
                          {otherFiles.map((artifact, index) => renderArtifact(artifact, index + primaryFiles.length))}
                        </div>
                      </details>
                    )}
                  </div>
                )}
                {outputFields && outputFields.length > 0 ? (
                  <dl className="workflow-final-result-output-fields">
                    {outputFields.map((field) => (
                      <div key={field.key} className={field.structured ? "is-structured" : undefined}>
                        {field.structured ? (
                          <section className="workflow-final-result-structured">
                            <div className="workflow-final-result-structured-title">{field.label}</div>
                            <WorkflowValueWithJson value={field.raw} />
                          </section>
                        ) : (
                          <>
                            <dt>{field.label}</dt>
                            <dd>
                              {field.reference ? (
                                <InlineFileReferenceCard reference={field.reference} label={field.value} compact />
                              ) : field.value}
                            </dd>
                          </>
                        )}
                      </div>
                    ))}
                  </dl>
                ) : media.length === 0 && !showHero ? (
                  typeof output === "string" ? (
                    // Agent text reads like its chat reply: links and integration
                    // links render (an integration link opens its connect panel).
                    <div className="workflow-final-result-text"><ChatMarkdown content={output} /></div>
                  ) : (
                    <pre>{JSON.stringify(output, null, 2)}</pre>
                  )
                ) : null}
              </>
            )}
          </div>
        </section>
        )}
        {runInputs.length > 0 && (
          <details className="workflow-final-result-inputs">
            <summary>
              <span>{t("ui.inputs")}</span>
              <span>{runInputs.length}</span>
            </summary>
            <dl>
              {runInputs.map(([key, value]) => (
                <div key={key}>
                  <dt>{key}</dt>
                  <dd title={typeof value === "string" ? value : undefined}>
                    {typeof value === "string" ? value : JSON.stringify(value)}
                  </dd>
                </div>
              ))}
            </dl>
          </details>
        )}
      </div>
      {retryControl && <div className="workflow-final-result-actions">{retryControl}</div>}
      {failure?.stepId ? (
        <div className="workflow-final-result-actions">
          <span className="workflow-final-result-source">Stopped at {failure.stepName}</span>
          <Button variant="outline" size="sm" onClick={() => onOpenNode(failure.stepId)}>
            {t("ui.open_failed_node")}
          </Button>
        </div>
      ) : result?.stepId && (
        <div className="workflow-final-result-actions">
          <span className="workflow-final-result-source">Output from {result.stepName}</span>
          <Button variant="outline" size="sm" onClick={() => onOpenNode(result.stepId!)}>
            {t("ui.open_source_node")}
          </Button>
        </div>
      )}
    </section>
  );
}


/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

/** `embedded`: the workflow library inside Settings › AI keeps its header in the panel. */
const WORKFLOW_RUN_STATUS_KEYS: Record<string, string> = {
  starting: "ui.workflow_run_starting",
  pending: "ui.workflow_run_pending",
  running: "ui.workflow_run_running",
  paused: "ui.workflow_run_paused",
  completed: "ui.workflow_run_completed",
  failed: "ui.workflow_run_failed",
  cancelled: "ui.workflow_run_cancelled",
};

function workflowRunStatusLabel(status: string | undefined): string {
  const key = status ? WORKFLOW_RUN_STATUS_KEYS[status] : undefined;
  return key ? t(key) : status || "";
}

export default function Flows({ run, onBack, embedded = false }: { run?: WorkflowHistoryRun; onBack?: () => void; embedded?: boolean } = {}) {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const location = useLocation();
  const navigate = useNavigate();
  const visible = usePageVisible();
  const [searchParams, setSearchParams] = useSearchParams();
  const { workflowId: requestedWorkflowId } = useParams<{ workflowId: string }>();
  const requestedRunId = run?.id || searchParams.get("run") || searchParams.get("workflow_run");
  const readOnly = Boolean(run);
  const workflowReturnTo = getWorkflowReturnTo(location.state);

  const [search, setSearch] = useState("");
  const [selectedFlow, setSelectedFlow] = useState<Flow | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [showTemplates, setShowTemplates] = useState(false);
  const [deployFlow, setDeployFlow] = useState<Flow | null>(null);
  const [configStepId, setConfigStepId] = useState<string | null>(null);
  const [addFromId, setAddFromId] = useState<string | null>(null);
  const [showIssues, setShowIssues] = useState(false);
  const [runResult, setRunResult] = useState<any>(null);
  const [showFinalResult, setShowFinalResult] = useState(false);
  const openedFinalRunRef = useRef<string | null>(null);
  const observedActiveRunIdsRef = useRef(new Set<string>());
  const runDetailReloadRef = useRef("");
  const [liveStatus, setLiveStatus] = useState<Record<string, string>>({});
  const [streaming, setStreaming] = useState(false);
  // Full run responses own the frozen graph and receipts; compact responses
  // own current recovery controls (including recovery of nested attempts).
  const runControlQuery = useQuery({
    queryKey: ["workflow-run-history-control", runResult?.id],
    queryFn: ({ signal }) => api.workflows.getRun(runResult.id, false, signal),
    enabled: Boolean(runResult?.id) && !streaming && runResult?.capabilities?.can_control === true
      && (["failed", "cancelled"].includes(runResult?.status || "") || (runResult?.status === "completed"
        && ["needs_input", "revision_required"].includes(runResult?.business_outcome))) && visible,
    staleTime: 15_000,
    retry: false,
    refetchInterval: (query) => displayReadInterval(query.state, 30_000),
    refetchOnWindowFocus: "always",
  });
  const [runInputFlow, setRunInputFlow] = useState<Flow | null>(null);
  // Inputs of the run being repeated, so "run again" only asks for changes.
  const [runInputPrefill, setRunInputPrefill] = useState<Record<string, unknown> | null>(null);
  // results from running a single node via its hover ▶ (merged onto the canvas)
  const [singleResults, setSingleResults] = useState<Record<string, any>>({});
  const recordSingleResult = (stepId: string, result: any) =>
    setSingleResults((previous) => ({ ...previous, [stepId]: result }));
  const runSingleNode = async (stepId: string) => {
    const step = (selectedFlow?.steps || []).find((s: any) => s.id === stepId);
    if (!step) return;
    // The hover action should reveal the same result surface as the modal's
    // Test node button, rather than only changing a tiny status dot.
    setConfigStepId(stepId);
    // A node with declared inputs needs explicit test data. Opening the panel
    // first prevents a hover action from silently reusing stale workflow data.
    if (Array.isArray(step.config?.inputs)
      && step.config.inputs.some((input: any) => String(input?.key || "").trim())) return;
    recordSingleResult(stepId, { status: "running" });
    try {
      const runVariables = runResult?.workflow_id === selectedFlow?.id ? runResult?.variables : undefined;
      const res = await api.workflows.runNode({ ...step }, runVariables);
      recordSingleResult(stepId, res);
    } catch (e: any) {
      recordSingleResult(stepId, { status: "failed", error: e?.message || "Run failed" });
    }
  };
  const [showStepPanel, setShowStepPanel] = useState(false);
  const [editingStep, setEditingStep] = useState<FlowStep | null>(null);
  const [showRunHistory, setShowRunHistory] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);
  const [cancelRunTarget, setCancelRunTarget] = useState<string | null>(null);
  const [showIdentityModal, setShowIdentityModal] = useState(false);
  const [identityTarget, setIdentityTarget] = useState<Flow | null>(null);
  const [identityName, setIdentityName] = useState("");
  const [identityDescription, setIdentityDescription] = useState("");
  const [identityIcon, setIdentityIcon] = useState("flow");
  const [identityNameError, setIdentityNameError] = useState("");
  const [workflowAiPreview, setWorkflowAiPreview] = useState<WorkflowAiPreview | null>(null);
  const [workflowAiTurnLocked, setWorkflowAiTurnLocked] = useState(false);
  const workflowAiPreviewRef = useRef<WorkflowAiPreview | null>(null);
  const workflowAiCommitCoordinator = useRef(createAiEditCommitCoordinator()).current;
  const selectedFlowRef = useRef<Flow | null>(null);
  // Canvas edits apply at once and save in the background; only the newest
  // save's reply may replace the local steps (an older one must not undo a
  // later edit).
  const canvasSaveSeqRef = useRef(0);
  // Saves go out one at a time, each carrying the version the previous one
  // returned: the editor never conflicts with itself, and a save made by
  // someone else in between is refused instead of silently overwritten.
  const saveChainRef = useRef<Promise<unknown>>(Promise.resolve());
  const savedVersionRef = useRef<Record<string, number>>({});
  selectedFlowRef.current = selectedFlow;
  const flowContextMenu = useContextMenu();

  // Create form
  const [formName, setFormName] = useState("");
  const [formDesc, setFormDesc] = useState("");
  const [formTrigger, setFormTrigger] = useState<"manual" | "event" | "schedule">("manual");

  // Add step form
  const [addStepType, setAddStepType] = useState<FlowStep["type"]>("agent");
  const [addStepName, setAddStepName] = useState("");
  const [showAddStep, setShowAddStep] = useState(false);
  const [_addStepIndex, setAddStepIndex] = useState(-1);

  // Step ids such as "start" and "end" are commonly reused across flows;
  // never leak standalone test results from the previously opened workflow.
  useEffect(() => setSingleResults({}), [selectedFlow?.id]);

  const catalogScope = useCatalogScope();
  const { data: agentList = [] } = useQuery({
    ...agentListQueryOptions(catalogScope),
    enabled: !!catalogScope,
  });
  const agentsById = useMemo(
    () => Object.fromEntries((agentList as { id: string; name: string; avatar_url?: string | null }[]).map((agent) => [agent.id, agent])),
    [agentList],
  );
  const { data: flows, isLoading } = useQuery({
    queryKey: ["workflows", "summary"],
    queryFn: () => api.workflows.listSummaries(),
    enabled: visible,
  });

  const requestedDefinition = useQuery({
    queryKey: ["workflow-definition", requestedWorkflowId],
    queryFn: () => api.workflows.get(requestedWorkflowId!),
    enabled: Boolean(requestedWorkflowId) && visible,
    retry: false,
  });
  // Older links name the workflow instead of giving its id.
  const namedWorkflowId = requestedDefinition.isError
    ? (flows as Flow[] | undefined)?.find((flow) => flow.name === requestedWorkflowId)?.id
    : undefined;
  const namedDefinition = useQuery({
    queryKey: ["workflow-definition", namedWorkflowId],
    queryFn: () => api.workflows.get(namedWorkflowId!),
    enabled: Boolean(namedWorkflowId),
    retry: false,
  });

  useEffect(() => {
    if (!run) return;
    const definition = requestedDefinition.data as Flow | undefined;
    const snapshot = run.definition_snapshot;
    const currentNodes = (definition?.steps || []).map((step) => ({
      ...step, targets: workflowStepTargets(step),
    }));
    const recordedNodes = buildWorkflowRunTimeline(run).map((entry) => ({
      id: entry.nodeId, name: entry.nodeName, type: entry.nodeType,
    }));
    const nodes = buildWorkflowSnapshotNodes(run, currentNodes.length ? currentNodes
      : [...new Map(recordedNodes.map((node) => [node.id, node])).values()]);
    setSelectedFlow({
      id: run.workflow_id || requestedWorkflowId || "",
      name: snapshot?.name || run.workflow_name || definition?.name || "Workflow",
      description: "",
      status: "active",
      trigger: "manual",
      created_at: run.created_at || "",
      steps: nodes.map((node) => ({
        id: node.nodeId, name: node.nodeName, type: node.nodeType || "action", next: node.targets,
      })),
    });
    setRunResult(run);
  }, [run, requestedDefinition.data, requestedWorkflowId]);

  // Closing clears selectedFlow before the route update commits, so
  // this effect can still see the old path and reopen the editor.
  // Remember the dismissed id until the URL actually changes.
  const dismissedWorkflowIdRef = useRef<string | null>(null);

  // Workspace cards deep-link to a concrete workflow. Keep the selected
  // editor in the URL so refresh/back navigation preserves that context.
  useEffect(() => {
    if (readOnly) return;
    if (requestedWorkflowId !== dismissedWorkflowIdRef.current) {
      dismissedWorkflowIdRef.current = null;
    }
    if (!requestedWorkflowId) return;
    if (dismissedWorkflowIdRef.current === requestedWorkflowId) return;
    if (
      selectedFlow?.id === requestedWorkflowId
      || selectedFlow?.name === requestedWorkflowId
    ) return;
    const requested = (requestedDefinition.data || namedDefinition.data) as Flow | undefined;
    if (requested) {
      if (
        workflowAiPreviewRef.current
        && workflowAiPreviewRef.current.current.id !== requested.id
      ) {
        closeEditorLiveChat();
      }
      setSelectedFlow(requested);
    }
  }, [readOnly, namedDefinition.data, requestedDefinition.data, requestedWorkflowId, selectedFlow?.id]);

  const closeWorkflow = () => {
    if (readOnly && onBack) { onBack(); return; }
    closeEditorLiveChat();
    dismissedWorkflowIdRef.current = requestedWorkflowId || null;
    setSelectedFlow(null);
    if (workflowReturnTo) {
      navigate(workflowReturnTo, { state: location.state?.returnState });
      return;
    }
    navigate("/flows");
  };

  const { data: runs } = useQuery({
    queryKey: ["workflow-runs", selectedFlow?.id],
    queryFn: ({ signal }) => (selectedFlow ? api.workflows.runs(selectedFlow.id, signal) : Promise.resolve([])),
    enabled: Boolean(selectedFlow) && visible,
    // A long timer resumes in Celery, outside this browser request. Keep the
    // active run fresh until the worker reaches a terminal state so the canvas
    // does not remain stuck on the initial "Paused" response.
    refetchInterval: (query) => {
      const runRows = (query.state.data as Array<{ status?: string | null }> | undefined) || [];
      // Poll an active run closely; otherwise check slowly for runs started
      // elsewhere (Chat, API, schedules) so their reviews show up here.
      return displayReadInterval(
        query.state,
        workflowRunListRefetchInterval(runRows),
      );
    },
    retry: false,
    refetchOnWindowFocus: "always",
  });

  // Opening a flow should immediately restore its newest run onto the canvas;
  // requiring a separate History click made successful results look lost.
  useEffect(() => {
    if (readOnly || !selectedFlow || !runs?.length) return;
    if (requestedRunId) {
      const requested = runs.find((run: any) => run.id === requestedRunId);
      if (requested && runResult?.id !== requested.id) setRunResult(requested);
      return;
    }
    if (runResult?.workflow_id !== selectedFlow.id) {
      setRunResult(runs[0]);
      return;
    }
    // A run started elsewhere (Chat, API, a schedule) that is still working or
    // waiting for a decision replaces a finished one, so its review is visible.
    const newest = runs[0];
    if (
      newest?.id !== runResult?.id
      && ACTIVE_RUN_STATUSES.has(newest?.status)
      && !ACTIVE_RUN_STATUSES.has(runResult?.status)
    ) {
      setRunResult(newest);
    }
  }, [readOnly, requestedRunId, runs, runResult?.id, runResult?.status, runResult?.workflow_id, selectedFlow?.id]);

  // An explicitly opened run stays put; point at a newer run that needs you.
  const newerActiveRun = runs?.find((run: any) => ACTIVE_RUN_STATUSES.has(run.status));
  const showNewerActiveRun = !!newerActiveRun
    && !!runResult
    && newerActiveRun.id !== runResult.id
    && !ACTIVE_RUN_STATUSES.has(runResult.status);

  // Compact list rows need full detail; an explicit link can name an older run
  // outside the first history page. Both are read by the same query below.
  const runResultHasDetail = !!runResult
    && Object.prototype.hasOwnProperty.call(runResult, "step_results");

  // Reveal a result reached in this page session, or one explicitly opened
  // from a result link. Merely opening a Flow keeps its previous run unobtrusive.
  useEffect(() => {
    if (!selectedFlow || runResult?.workflow_id !== selectedFlow.id) return;
    if (["pending", "running", "paused"].includes(runResult?.status)) {
      observedActiveRunIdsRef.current.add(runResult.id);
      return;
    }
    if (!["completed", "failed"].includes(runResult?.status)) return;
    const explicitlyRequested = requestedRunId === runResult.id;
    if (!explicitlyRequested && !observedActiveRunIdsRef.current.has(runResult.id)) return;
    if (openedFinalRunRef.current === runResult.id) return;
    openedFinalRunRef.current = runResult.id;
    setShowFinalResult(true);
  }, [requestedRunId, runResult?.id, runResult?.status, runResult?.workflow_id, selectedFlow?.id]);

  useEffect(() => {
    if (readOnly || !runResult || !runs?.length) return;
    const refreshed = runs.find((run: any) => run.id === runResult.id);
    if (!refreshed) return;
    if (isOlderWorkflowRunSnapshot(runResult, refreshed)) return;
    const resultCount = Object.keys(runResult.step_results || {}).length;
    const refreshedCount = Object.keys(refreshed.step_results || {}).length;
    const refreshedHasResults = Object.prototype.hasOwnProperty.call(refreshed, "step_results");
    const progressed = refreshed.status !== runResult.status
      || refreshed.completed_at !== runResult.completed_at;
    if (progressed || (refreshedHasResults && refreshedCount !== resultCount)) {
      // Compact polling rows must never erase the full node results received
      // from the SSE completion frame or the detail endpoint.
      setRunResult((current: any) => (
        current?.id === refreshed.id && !isOlderWorkflowRunSnapshot(current, refreshed)
          ? { ...current, ...refreshed } : current
      ));
    }
    // Runs resumed or retried from this page finish in the worker, so the
    // node results held here date from the click. Compact rows carry no node
    // results; reload the detail whenever the run moves on.
    if (!progressed || refreshedHasResults) return;
    const reloadKey = `${refreshed.id}:${refreshed.status}:${refreshed.completed_at || ""}`;
    if (runDetailReloadRef.current === reloadKey) return;
    runDetailReloadRef.current = reloadKey;
    void queryClient.invalidateQueries({
      queryKey: ["workflow-run-flow-detail", refreshed.id], exact: true,
      refetchType: visible ? "active" : "none",
    }, { cancelRefetch: false });
  }, [readOnly, runs, runResult, queryClient, visible]);

  // One cancellable source owns hydration, live trace and the final receipt.
  const detailRunId = requestedRunId || runResult?.id;
  const liveRunDetail = useQuery({
    queryKey: ["workflow-run-flow-detail", detailRunId],
    queryFn: ({ signal }) => api.workflows.getRun(detailRunId!, true, signal),
    enabled: !readOnly && !streaming && Boolean(detailRunId && selectedFlow) && visible,
    retry: false,
    refetchInterval: (query) => displayReadInterval(
      query.state,
      selectedWorkflowRunRefetchInterval(query.state.data?.status),
    ),
    refetchOnWindowFocus: "always",
  });
  useEffect(() => {
    const detail = liveRunDetail.data;
    if (!detail || detail.workflow_id !== selectedFlow?.id) return;
    setRunResult((current: any) => {
      if (current?.id !== detail.id) return requestedRunId === detail.id ? detail : current;
      // A read begun before a terminal list update must not make the run look
      // active again. Its active query data keeps polling until final detail arrives.
      if (isOlderWorkflowRunSnapshot(current, detail)) return current;
      return { ...current, ...detail };
    });
  }, [liveRunDetail.data, requestedRunId, selectedFlow?.id]);

  // Install by stable catalogue id; the returned workflow id is the runtime
  // identity used by bindings, Tasks, Automations, Proposals, and subflows.
  const templateMutation = useMutation({
    mutationFn: (tpl: WorkflowTemplate) =>
      api.workflows.installTemplate(tpl.id),
    onSuccess: (result: any) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.invalidateQueries({ queryKey: ["workflow-templates"] });
      setShowTemplates(false);
      setSelectedFlow(result.workflow);
      navigate(workflowPath(result.workflow.id), { state: location.state });
      toast.success(result.already_installed ? "Opened installed Flow" : "Flow installed");
    },
    onError: (error: any) => {
      toast.error("Couldn't install Flow", error?.message || "Please try again.");
    },
  });

  const createMutation = useMutation({
    mutationFn: (data: Record<string, unknown>) => api.workflows.create(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      setShowCreateModal(false);
      setFormName("");
      setFormDesc("");
      setFormTrigger("manual");
      toast.success(t("page.flows.toast_created"));
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.workflows.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      closeEditorLiveChat();
      setShowIdentityModal(false);
      setIdentityTarget(null);
      setDeleteTarget(null);
      closeDetail();
      closeWorkflow();
      toast.success(t("page.flows.toast_deleted"));
    },
    onError: (error: any) => {
      toast.error(t("page.flows.delete_failed"), error?.message || t("page.flows.delete_try_again"));
    },
  });

  const pauseMutation = useMutation({
    mutationFn: (runId: string) => api.workflows.pauseRun(runId),
    onSuccess: (updated: any) => {
      setRunResult(updated);
      queryClient.invalidateQueries({ queryKey: ["workflow-runs"] });
      queryClient.invalidateQueries({ queryKey: ["workflow-run-history-detail"] });
      toast.success("Workflow paused", "The current node may finish; no next node will start.");
    },
    onError: (e: any) => {
      toast.error("Couldn't pause workflow", e?.message || "Please try again.");
    },
  });

  const cancelRunMutation = useMutation({
    mutationFn: (runId: string) => api.workflows.cancelRun(runId),
    onSuccess: (updated: any) => {
      setRunResult(updated);
      setCancelRunTarget(null);
      queryClient.invalidateQueries({ queryKey: ["workflow-runs"] });
      for (const key of ["workflow-run-history-detail", "workspace-workflow-runs", "workspace-app-recent-run", "workspace-app-run", "workspace-app-run-detail", "workspace-chat", "workflow-run"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
      toast.success("Workflow stopped", "No additional nodes will start.");
    },
    onError: (e: any) => {
      toast.error("Couldn't stop workflow", e?.message || "Please try again.");
    },
  });

  const retryMutation = useMutation({
    mutationFn: (run: any) => api.workflows.retryRun(run.id, {
      from_step_id: run.retry_from_step_id,
      execute: true,
    }),
    onSuccess: (retriedRun: any) => {
      observedActiveRunIdsRef.current.add(retriedRun.id);
      setShowFinalResult(false);
      for (const key of ["workflow-runs", "workspace-workflow-runs", "workspace-app-recent-run", "workspace-app-run", "workspace-app-run-detail", "workflow-run-history-detail", "workflow-run-history-family", "workspace-chat", "workflow-run"]) {
        void queryClient.invalidateQueries({ queryKey: [key] });
      }
      if (readOnly) {
        navigate(workflowRunPath(retriedRun.workflow_id || requestedWorkflowId || "", retriedRun.id), { state: location.state });
        return;
      }
      const next = new URLSearchParams(searchParams);
      next.delete("workflow");
      next.set("run", retriedRun.id);
      next.delete("workflow_run");
      setSearchParams(next, { replace: true, state: location.state });
      setRunResult(retriedRun);
    },
    onError: (e: any) => {
      toast.error("Couldn't retry workflow", e?.message || "Please try again.");
    },
  });

  const saveWorkflow = (id: string, changes: Record<string, any>) => {
    const save = saveChainRef.current.catch(() => undefined).then(async () => {
      const shown = selectedFlowRef.current?.id === id ? Number(selectedFlowRef.current?.version) || 0 : 0;
      const known = Math.max(savedVersionRef.current[id] || 0, shown);
      const saved = await api.workflows.update(id, known ? { ...changes, expected_version: known } : changes);
      savedVersionRef.current[id] = Number(saved?.version) || known;
      return saved;
    });
    saveChainRef.current = save;
    return save;
  };

  const updateMutation = useMutation({
    mutationFn: ({ id, canvasSaveSeq: _canvasSaveSeq, ...changes }: { id: string; canvasSaveSeq?: number; [key: string]: any }) =>
      saveWorkflow(id, changes),
    onSuccess: (updated: any, variables) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.invalidateQueries({ queryKey: ["workflow-metadata", updated.id] });
      const staleCanvasSave = variables.canvasSaveSeq !== undefined
        && variables.canvasSaveSeq !== canvasSaveSeqRef.current;
      if (!staleCanvasSave && selectedFlowRef.current?.id === updated.id) {
        selectedFlowRef.current = updated;
        setSelectedFlow(updated);
      }
      if (identityTarget?.id === updated.id) {
        setIdentityTarget(updated);
      }
    },
    // Saves (add node, AI edit, config, drag) previously failed silently — the
    // API layer suppresses 401/403 toasts, so an expired session made clicking
    // "do nothing". Surface every failure with an actionable message.
    onError: (e: any, variables) => {
      if (e?.code === "workflow.error.edited_elsewhere") {
        // Someone else saved first: show their version so the edit can be redone on top of it.
        toast.error(t("page.flows.edited_elsewhere_title"), t("workflow.error.edited_elsewhere"));
        void api.workflows.get(variables.id).then((latest: Flow) => {
          savedVersionRef.current[latest.id] = Number(latest.version) || 0;
          if (selectedFlowRef.current?.id === latest.id) {
            canvasSaveSeqRef.current += 1;
            selectedFlowRef.current = latest;
            setSelectedFlow(latest);
          }
          queryClient.invalidateQueries({ queryKey: ["workflows"] });
        }).catch(() => undefined);
      } else if (e?.status === 401 || e?.status === 403) {
        toast.error("Session expired", "Refresh the page and sign in again to save your changes.");
      } else {
        toast.error("Couldn't save the workflow", e?.message || "Please try again.");
      }
    },
  });

  const openWorkflowIdentityEditor = (target?: Flow) => {
    const current = target || selectedFlowRef.current;
    if (!current) return;
    setIdentityTarget(current);
    setIdentityName(current.name || "");
    setIdentityDescription(current.description || "");
    setIdentityIcon(current.icon || "flow");
    setIdentityNameError("");
    setShowIdentityModal(true);
  };

  const saveWorkflowIdentity = () => {
    if (workflowAiPreviewRef.current) return;
    const current = identityTarget || selectedFlowRef.current;
    const name = identityName.trim();
    if (!current) return;
    if (!name) {
      setIdentityNameError("Enter a workflow name.");
      return;
    }
    updateMutation.mutate(
      {
        id: current.id,
        name,
        description: identityDescription.trim(),
        icon: identityIcon,
      },
      {
        onSuccess: (updated: Flow) => {
          setShowIdentityModal(false);
          setIdentityTarget(updated);
          toast.success("Workflow details saved");
        },
      },
    );
  };

  const openFlowEditor = (flow: Flow) => {
    if (
      workflowAiPreviewRef.current
      && workflowAiPreviewRef.current.current.id !== flow.id
    ) {
      closeEditorLiveChat();
    }
    closeDetail();
    if (typeof flow.step_count === "number") {
      // A list row has no graph. Load it fresh, since the last edit may be seconds old.
      setSelectedFlow(null);
      queryClient.removeQueries({ queryKey: ["workflow-definition", flow.id] });
    } else {
      setSelectedFlow(flow);
    }
    const next = new URLSearchParams(searchParams);
    next.delete("workflow");
    next.delete("run");
    next.delete("workflow_run");
    navigate(`${workflowPath(flow.id)}${next.size ? `?${next}` : ""}`, { state: location.state });
  };

  const openWorkflowDetails = (flow: Flow) => {
    openDetail({
      key: `workflow:${flow.id}`,
      icon: <IconTile size={40}>{workflowIconGlyph(flow.icon, 20)}</IconTile>,
      title: flow.name,
      subtitle: flow.description || t("page.flows.no_description"),
      badges: (
        <>
          <StatusBadge type={flow.status === "active" ? "active" : "gray"} dot>
            {flow.status === "active" ? t("page.flows.active") : t("page.flows.draft")}
          </StatusBadge>
          <StatusBadge type="gray">
            {t(TRIGGER_LABELS[flow.trigger || flow.trigger_type || "manual"] || flow.trigger_type || "manual")}
          </StatusBadge>
        </>
      ),
      body: <WorkflowMetadataPanel workflowId={flow.id} />,
      primaryAction: {
        label: t("page.flows.open_workflow"),
        icon: <IconFlow size={15} />,
        onClick: () => openFlowEditor(flow),
      },
      secondaryActions: [{
        label: t("page.flows.edit_workflow"),
        icon: <IconEdit size={15} />,
        onClick: () => {
          closeDetail();
          openWorkflowIdentityEditor(flow);
        },
      }],
      width: 560,
    });
  };

  const workflowContextItems = (flow: Flow): MenuItem[] => [
    {
      label: t("page.flows.open_workflow"),
      icon: <IconFlow size={15} />,
      onClick: () => openFlowEditor(flow),
    },
    {
      label: t("page.flows.edit_workflow"),
      icon: <IconEdit size={15} />,
      onClick: () => openWorkflowIdentityEditor(flow),
    },
    {
      label: t("page.flows.view_details"),
      icon: <IconInfo size={15} />,
      onClick: () => openWorkflowDetails(flow),
    },
  ];

  const discardWorkflowAiPreview = () => {
    if (workflowAiCommitCoordinator.isCommitting()) return;
    const preview = workflowAiPreviewRef.current;
    if (!preview) return;
    const baseline = structuredClone(preview.baseline);
    selectedFlowRef.current = baseline;
    setSelectedFlow(baseline);
    workflowAiPreviewRef.current = null;
    setWorkflowAiPreview(null);
  };

  const acceptWorkflowAiPreview = async () => {
    if (workflowAiCommitCoordinator.isCommitting()) return;
    const preview = workflowAiPreviewRef.current;
    if (!preview || preview.status !== AiEditPreviewStatus.Ready) return;
    await workflowAiCommitCoordinator.run(async () => {
      const update = parseWorkflowLiveEdit(serializeWorkflowLiveEdit(preview.current));
      try {
        const saved = await updateMutation.mutateAsync({
          id: preview.current.id,
          ...update,
          validate_steps: true,
        }) as Flow;
        selectedFlowRef.current = saved;
        setSelectedFlow(saved);
        workflowAiPreviewRef.current = null;
        setWorkflowAiPreview(null);
        toast.success(t("page.flows.ai_edit_accepted", { count: saved.steps?.length || 0 }));
      } catch {
        // The mutation owns the error toast. Keep the draft so the user can retry.
      }
    });
  };

  const openWorkflowAiEdit = () => {
    const current = selectedFlowRef.current;
    if (!current || updateMutation.isPending) return;
    setShowAddStep(false);
    setAddFromId(null);
    setConfigStepId(null);
    setShowIdentityModal(false);
    setIdentityTarget(null);
    openEditorLiveChat({
      documentName: current.name,
      fileType: "workflow",
      mimeType: "application/vnd.manor.workflow+json",
      editorType: "Workflow",
      sessionLabel: `Workflow: ${current.name}`,
      emptyDescription: `Describe what to change. I will update ${current.name} directly on the canvas as the answer streams.`,
      placeholder: `Describe how to change ${current.name}...`,
      examples: ["Add an LLM node", "Add a branch", "Fix connections", "Add error handling"],
      adapter: createEditorLiveAdapter({
        target: { kind: AiEditTargetKind.Workflow, id: current.id },
        read: () => serializeWorkflowLiveEdit(selectedFlowRef.current || current),
        getTurnPreviewState: () => ({
          changeCount: workflowAiPreviewRef.current?.changeCount || 0,
        }),
        beginTurn: (meta) => {
          if (meta.signal?.aborted) return false;
          const preview = workflowAiPreviewRef.current;
          if (!preview || preview.current.id !== current.id) return true;
          const pending = { ...preview, status: AiEditPreviewStatus.Animating };
          workflowAiPreviewRef.current = pending;
          setWorkflowAiPreview(pending);
          return true;
        },
        preview: (content, meta) => {
          const active = selectedFlowRef.current;
          if (!active || active.id !== current.id) {
            throw new Error("The workflow is no longer open.");
          }
          const update = parseWorkflowLiveEdit(content);
          const previous = workflowAiPreviewRef.current?.current.id === current.id
            ? workflowAiPreviewRef.current
            : null;
          const triggerType = update.trigger_type as Flow["trigger"];
          const draft: Flow = {
            ...active,
            ...update,
            id: active.id,
            trigger: triggerType,
            trigger_type: triggerType,
            steps: update.steps.map((step) => ({
              ...step,
              id: step.id as string,
              type: step.type as string,
              name: step.name as string,
            })),
          };
          const preview: WorkflowAiPreview = {
            baseline: previous?.baseline || structuredClone(active),
            current: draft,
            status: AiEditPreviewStatus.Animating,
            changeCount: nextEditorLiveChangeCount(previous, meta),
          };
          workflowAiPreviewRef.current = preview;
          setWorkflowAiPreview(preview);
          selectedFlowRef.current = draft;
          setSelectedFlow(draft);
          return true;
        },
        complete: () => {
          const preview = workflowAiPreviewRef.current;
          if (!preview || preview.current.id !== current.id) return false;
          const ready = { ...preview, status: AiEditPreviewStatus.Ready };
          workflowAiPreviewRef.current = ready;
          setWorkflowAiPreview(ready);
          return true;
        },
        rollback: discardWorkflowAiPreview,
        setTurnLocked: setWorkflowAiTurnLocked,
        commitCoordinator: workflowAiCommitCoordinator,
      }),
    });
  };

  // Add a node of `type`, linking it from `addFromId` (a node's "+" output) or,
  // failing that, the end of the current chain.
  const addNode = (type: string, plainLabel?: string) => {
    if (!selectedFlow) return;
    const steps = (selectedFlow.steps || []).map((s: any) => ({ ...s, next: [...(s.next || [])] }));
    const id = `n_${Date.now().toString(36)}${steps.length}`;
    if (type === "note") {
      // A note is a free-floating annotation — not wired into the run, dropped
      // near the anchor node (or canvas) so it doesn't overlap the flow.
      const anchor = addFromId ? steps.find((s: any) => s.id === addFromId) : steps[steps.length - 1];
      const ap = anchor?.position;
      const position = ap ? { x: ap.x, y: ap.y + 130 } : { x: 80, y: 220 };
      steps.push({ id, type: "note", name: "Note", config: { text: "" }, next: [], position });
    } else {
      const label = plainLabel || type.charAt(0).toUpperCase() + type.slice(1);
      // "A person checks" is an approval wait, ready to run as added.
      const config = type === "wait" && plainLabel
        ? { wait_type: "approval", options: ["approve", "revise"], approval_values: ["approve"], response_variable: `${id}_decision` }
        : {};
      if (addFromId) {
        // added from a node's "+" output handle → wire it from that node
        const source = steps.find((s: any) => s.id === addFromId);
        if (source) source.next.push(id);
        steps.push({ id, type, name: label, config, next: [] });
      } else {
        // top "Add node" button → drop an UNCONNECTED node next to the rightmost
        // node (never auto-wire it to the end/terminal node). The user draws the
        // connection by dragging an edge to it.
        const positioned = steps.filter((s: any) => s.position);
        const anchor = positioned.length
          ? positioned.reduce((a: any, b: any) => (b.position.x > a.position.x ? b : a))
          : null;
        const position = anchor ? { x: anchor.position.x + 260, y: anchor.position.y } : { x: 120, y: 340 };
        steps.push({ id, type, name: label, config, next: [], position });
      }
    }
    setShowAddStep(false);
    setAddFromId(null);
    updateMutation.mutate(
      { id: selectedFlow.id, steps },
      { onSuccess: () => toast.success(type === "note" ? t("component.workflow_palette.note_added") : t("component.workflow_palette.step_added")) },
    );
  };

  const visibleFlows = (flows || []).filter((flow: Flow) => flow.trigger_type !== "internal");
  const filtered = visibleFlows.filter(
    (f: any) =>
      f.name?.toLowerCase().includes(search.toLowerCase()) ||
      f.description?.toLowerCase().includes(search.toLowerCase()) ||
      f.workspace_name?.toLowerCase().includes(search.toLowerCase()),
  );

  const handleCreate = () => {
    if (!formName.trim()) return;
    const triggerName = formTrigger === "schedule"
      ? "Scheduled start"
      : formTrigger === "event"
        ? "Event start"
        : "Start";
    createMutation.mutate({
      name: formName,
      description: formDesc,
      trigger_type: formTrigger,
      steps: [{
        id: `trigger_${Date.now().toString(36)}`,
        type: "trigger",
        name: triggerName,
        config: {},
        next: [],
      }],
    });
  };

  const openAddStep = (index: number) => {
    setAddFromId(null);
    setAddStepIndex(index);
    setAddStepType("agent");
    setAddStepName("");
    setShowAddStep(true);
  };

  const executeWorkflow = async (flow: Flow, triggerData: Record<string, unknown> = {}) => {
    if (streaming) return;
    const flowErrors = validateWorkflow(flow.steps || []).filter((issue) => issue.level === "error");
    if (flowErrors.length > 0) {
      setSelectedFlow(flow);
      setShowIssues(true);
      toast.error("Workflow isn't runnable", "Fix the entry point and other errors first.");
      return;
    }
    closeDetail();
    setSelectedFlow(flow);
    setStreaming(true);
    setShowFinalResult(false);
    setLiveStatus({});
    const selectStartedRun = (run: any) => {
      const next = new URLSearchParams(searchParams);
      next.delete("workflow");
      next.set("run", run.id);
      next.delete("workflow_run");
      navigate(`${workflowPath(flow.id)}?${next}`, { replace: true, state: location.state });
      setRunResult(run);
    };
    try {
      if (flow.workspace_id) {
        const bindings = (await api.workflows.listBindings({ workflow_id: flow.id, workspace_id: flow.workspace_id }))
          .filter((binding: any) => binding.trigger_type === "manual" && binding.status === "active" && binding.enabled);
        if (bindings.length !== 1) {
          throw new Error("Open this Workspace's Flows and run the intended workflow binding.");
        }
        const run = await api.workflows.runBinding(bindings[0].id, {
          execute: false, trigger_data: { ...triggerData, workspace_manual: true },
        });
        selectStartedRun(run);
        setRunInputFlow(null);
        queryClient.invalidateQueries({ queryKey: ["workflow-runs"] });
        return;
      }
      const finalRun = await api.workflows.runStream(
        flow.id,
        (nodeId, status) => setLiveStatus((previous) => ({ ...previous, [nodeId]: status })),
        (runId) => {
          setRunInputFlow(null);
          selectStartedRun({
            id: runId,
            workflow_id: flow.id,
            status: "running",
            step_results: {},
            started_at: new Date().toISOString(),
          });
          queryClient.invalidateQueries({ queryKey: ["workflow-runs", flow.id] });
        },
        { trigger_data: triggerData },
      );
      if (finalRun) setRunResult(finalRun);
      queryClient.invalidateQueries({ queryKey: ["workflow-runs"] });
    } catch (error: any) {
      toast.error("Workflow couldn't run", error?.message || "Please check the inputs and try again.");
      throw error;
    } finally {
      setStreaming(false);
      setLiveStatus({});
    }
  };

  const requestWorkflowRun = (flow: Flow, prefill: Record<string, unknown> | null = null) => {
    setRunInputPrefill(prefill);
    const inputs = workflowRunInputs(flow);
    if (!inputs.length) {
      void executeWorkflow(flow).catch(() => { /* Reported by executeWorkflow. */ });
      return;
    }
    closeDetail();
    setSelectedFlow(flow);
    setRunInputFlow(flow);
  };

  const runFromList = async (summary: Flow) => {
    let flow: Flow;
    try {
      flow = await queryClient.fetchQuery({
        queryKey: ["workflow-definition", summary.id],
        queryFn: () => api.workflows.get(summary.id) as Promise<Flow>,
        staleTime: 0,
      });
    } catch {
      toast.error(t("page.flows.details_failed"));
      return;
    }
    if (validateWorkflow(flow.steps || []).some((issue) => issue.level === "error")) {
      // Reports the errors on the canvas instead of asking for inputs first.
      void executeWorkflow(flow).catch(() => { /* Reported by executeWorkflow. */ });
      return;
    }
    requestWorkflowRun(flow);
  };

  const runInputModal = (
    <WorkflowRunInputModal
      flow={runInputFlow}
      workspaceId={runInputFlow?.workspace_id}
      initialValues={runInputPrefill}
      busy={streaming}
      onClose={() => setRunInputFlow(null)}
      onSubmit={async (triggerData) => {
        if (!runInputFlow) return;
        await executeWorkflow(runInputFlow, triggerData);
      }}
    />
  );

  const workflowIdentityModal = (
    <Modal
      open={showIdentityModal}
      onClose={() => {
        if (updateMutation.isPending) return;
        setShowIdentityModal(false);
        setIdentityTarget(null);
      }}
      title={t("page.flows.edit_workflow")}
      maxWidth="560px"
      footer={(
        <div className="workflow-identity-footer">
          <Button
            variant="danger"
            onClick={() => {
              const targetId = identityTarget?.id || selectedFlowRef.current?.id;
              setShowIdentityModal(false);
              setIdentityTarget(null);
              if (targetId) setDeleteTarget(targetId);
            }}
            disabled={updateMutation.isPending || deleteMutation.isPending}
          >
            <IconTrash size={14} />
            {t("action.delete")}
          </Button>
          <div className="workflow-identity-footer-actions">
            <Button
              variant="outline"
              onClick={() => {
                setShowIdentityModal(false);
                setIdentityTarget(null);
              }}
              disabled={updateMutation.isPending}
            >
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={saveWorkflowIdentity}
              loading={updateMutation.isPending}
            >
              {t("action.save")}
            </Button>
          </div>
        </div>
      )}
    >
      <div className="workflow-identity-form">
        <fieldset className="workflow-icon-fieldset">
          <legend>{t("page.flows.icon")}</legend>
          <div className="workflow-icon-options" role="radiogroup" aria-label={t("ui.workflow_icon")}>
            {FLOW_ICON_OPTIONS.map((option) => {
              const selected = identityIcon === option.value;
              return (
                <button
                  key={option.value}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  className={`workflow-icon-option${selected ? " is-selected" : ""}`}
                  onClick={() => setIdentityIcon(option.value)}
                  title={option.label}
                >
                  <IconTile size={36}>{workflowIconGlyph(option.value, 18)}</IconTile>
                  <span>{option.label}</span>
                </button>
              );
            })}
          </div>
        </fieldset>
        <Input
          label={t("page.flows.name")}
          value={identityName}
          onChange={(event) => {
            setIdentityName(event.target.value);
            if (event.target.value.trim()) setIdentityNameError("");
          }}
          placeholder={t("page.flows.name_placeholder")}
          error={identityNameError}
          autoFocus
        />
        <Textarea
          label={t("page.flows.description")}
          value={identityDescription}
          onChange={(event) => setIdentityDescription(event.target.value)}
          placeholder={t("page.flows.description_placeholder")}
          rows={3}
        />
      </div>
    </Modal>
  );

  /* ---------------------------------------------------------------- */
  /*  Flow editor view                                                 */
  /* ---------------------------------------------------------------- */

  if (requestedWorkflowId && selectedFlow?.id !== requestedWorkflowId && selectedFlow?.name !== requestedWorkflowId) {
    return (requestedDefinition.isError && !namedWorkflowId) || namedDefinition.isError ? <section role="alert" className="workflow-run-history-list-state is-error">
      <p>{t("component.workspace_app_view.target_unavailable")}</p>
      <Button variant="ghost" onClick={closeWorkflow}>{t("component.workflow_run_history.back")}</Button>
      <Button onClick={() => void requestedDefinition.refetch()}>{t("component.workflow_run_history.try_again")}</Button>
    </section> : <LoadingSpinner size={24} />;
  }

  if (selectedFlow) {
    const flow = selectedFlow;
    const steps: FlowStep[] = flow.steps || [];

    // Per-node run status from the last run of THIS flow (stale runs ignored).
    const lastRun = runResult && runResult.workflow_id === flow.id ? runResult : null;
    const finalResult = lastRun ? resolveWorkflowFinalResult(steps, lastRun) : null;
    // last full run's results, with any single-node runs (hover ▶) layered on top
    const stepResults: Record<string, any> = { ...(lastRun?.step_results || {}), ...singleResults };
    const runStatusById: Record<string, string> = {};
    const previewById: Record<string, MediaRef> = {};
    const outputById: Record<string, string> = {};
    for (const [id, r] of Object.entries(stepResults)) {
      if (r && typeof r === "object" && (r as any).status) {
        const rr = r as any;
        runStatusById[id] = rr.skipped ? "skipped" : workflowNodeDisplayStatus(rr);
        // a one-line preview of what this node produced, shown on the card after
        // a run — so you can see each node's result without opening it.
        let out = "";
        if (rr.skipped) out = "skipped — needs config";
        else if (rr.wait_type === "connection" && rr.connection_label) out = t("component.integration_connect.run_status", { provider: String(rr.connection_label) });
        else if (rr.error) out = String(rr.error);
        else if (rr.output == null || rr.output === "") out = "(no output)";
        else out = typeof rr.output === "string" ? rr.output : JSON.stringify(rr.output);
        outputById[id] = out.replace(/\s+/g, " ").trim().slice(0, 90);
      }
      const ref = r && typeof r === "object" ? primaryMediaRef((r as any).output) : undefined;
      if (ref) previewById[id] = ref;
    }
    // The node a working run is on shows as running before it has a result.
    const currentStepId = String(lastRun?.current_step_id || "");
    if (
      currentStepId
      && ["running", "pending"].includes(lastRun?.status)
      && !["completed", "failed", "skipped"].includes(runStatusById[currentStepId])
    ) {
      runStatusById[currentStepId] = "running";
    }
    // While a streaming run is in flight, live per-node events drive the canvas.
    const statusById = streaming ? liveStatus : runStatusById;

    // Static validation — config gaps and broken wiring, surfaced before a run.
    const issues = validateWorkflow(steps as any[]);
    const issueById = issuesByNode(issues);
    const errorCount = issues.filter((i) => i.level === "error").length;

    // Entry inputs are collected first; execution then lights up each node.
    const runStep = () => {
      if (streaming) return;
      if (errorCount > 0) {
        setShowIssues(true);
        toast.error("Workflow isn't runnable", "Fix the entry point and other errors first.");
        return;
      }
      requestWorkflowRun(flow);
    };
    const runProgress = workflowEditorRunProgress(
      steps,
      streaming ? liveStatus : runStatusById,
      streaming ? { status: "running" } : lastRun,
    );
    const runPercent = workflowRunProgressPercent(
      runProgress.processed, runProgress.total, streaming ? "running" : lastRun?.status,
    );
    const showBanner = streaming || !!lastRun;
    // A run waiting for an integration says which one, not just "paused".
    const waitingFor = lastRun?.status === "paused" ? String(lastRun?.resume_action?.connection_label || lastRun?.resume_action?.connection_provider || "") : "";
    const bannerStatus = (waitingFor ? t("component.integration_connect.run_status", { provider: waitingFor }) : workflowRunBackgroundWait(lastRun) ? "running" : lastRun?.status) || (streaming ? "starting" : undefined);
    const runBannerColor = lastRun
        ? lastRun.status === "completed" ? "#4f9c84" : lastRun.status === "failed" ? "#d65f59" : "#cf9b44"
        : streaming ? "#0f766e" : null;
    const runCanControl = !readOnly || lastRun?.capabilities?.can_control === true;
    const runCanPause = runCanControl && !!lastRun && ["pending", "running"].includes(lastRun.status);
    // A run paused on background work (a rendering video) needs nobody.
    const runBackgroundWait = workflowRunBackgroundWait(lastRun);
    const runCanResume = runCanControl && !!lastRun && lastRun.status === "paused" && !runBackgroundWait;
    const runCanStop = !!lastRun && (readOnly ? canCancelWorkflowRun(lastRun) : ["pending", "running", "paused"].includes(lastRun.status));
    const recoveryRun = runControlQuery.isSuccess && !runControlQuery.isError
      && runControlQuery.data?.id === lastRun?.id
      && runControlQuery.data?.workflow_id === lastRun?.workflow_id
      && runControlQuery.data?.workspace_id === lastRun?.workspace_id
      ? runControlQuery.data : null;
    const recoveryAction = recoveryRun?.intervention;
    const runCanRetry = Boolean(
      !streaming
      && recoveryRun && canRetryWithoutCorrection(recoveryRun)
      && !recoveryAction,
    );
    const hasServerRetryAction = Boolean(
      !streaming
      && ((recoveryAction?.kind === "workflow_retry" && recoveryAction.options?.includes("retry"))
        || (recoveryAction?.kind === "workflow_effect_review" && recoveryAction.options?.some((option: string) => option === "review" || option === "review_applied"))),
    );
    const retryControl = recoveryRun?.capabilities?.can_control && lastRun && !streaming
      && (hasServerRetryAction || (recoveryRun.status === "failed" && !runCanRetry && !recoveryRun.superseded))
      ? <WorkflowRunRetryControl
          runId={lastRun.id} workflowId={flow.id} workspaceId={lastRun.workspace_id || ""}
          bindingId={lastRun.binding_id} businessOutcome={lastRun.business_outcome} size="sm"
          reviewRequired={recoveryAction?.kind === "workflow_effect_review" && recoveryAction.options?.some((option: string) => option === "review" || option === "review_applied")}
          onRetried={(next) => {
            observedActiveRunIdsRef.current.add(next.id);
            setShowFinalResult(false);
            for (const key of ["workflow-runs", "workspace-workflow-runs", "workspace-app-recent-run", "workspace-app-run", "workspace-app-run-detail", "workflow-run-history-detail", "workflow-run-history-family", "workspace-chat", "workflow-run"]) {
              void queryClient.invalidateQueries({ queryKey: [key] });
            }
            if (readOnly) {
              navigate(workflowRunPath(flow.id, next.id), { state: location.state });
              return;
            }
            const nextSearch = new URLSearchParams(searchParams);
            nextSearch.delete("workflow");
            nextSearch.set("run", next.id);
            nextSearch.delete("workflow_run");
            setSearchParams(nextSearch, { replace: true, state: location.state });
            setRunResult(next);
          }}
        />
      : undefined;
    const openNode = (id: string) => {
      if (!readOnly) { setConfigStepId(id); return; }
      const node = steps.find((step) => step.id === id);
      openDetail({
        title: node?.name || id,
        icon: <IconTile size={40}><NodeIcon type={node?.type || "action"} size={20} /></IconTile>,
        body: <WorkflowValueWithJson value={{
          result: stepResults[id] || null,
          execution_trace: buildWorkflowRunTimeline(lastRun, steps).filter((entry) => entry.nodeId === id),
        }} />,
      });
    };
    return (
      <div className={readOnly ? "workflow-run-view" : undefined} style={{ height: "100%", display: readOnly ? "grid" : "flex", flexDirection: "column", padding: 0, overflow: readOnly ? "auto" : "hidden", position: "relative", zIndex: 10 }}>
        {/* Back + editor identity + a consistent metadata/action toolbar. */}
        <div className="workflow-editor-header">
          <button
            type="button"
            className="workflow-editor-back"
            aria-label={t("component.workflow_run_history.back")}
            title={t("component.workflow_run_history.back")}
            onClick={() => {
              closeWorkflow();
            }}
          >
            <IconChevronLeft size={16} />
            {!readOnly && <span>{t("nav.flows")}</span>}
          </button>
          <div className="workflow-editor-identity">
            <IconTile className="workflow-editor-identity-icon" size={28}>
              {workflowIconGlyph(flow.icon, 15)}
            </IconTile>
            <div className="workflow-editor-heading">
              <PageHeaderTitle variant="editor">
                {readOnly ? flow.name : <button
                  type="button"
                  className="workflow-editor-identity-edit"
                  onClick={() => openWorkflowIdentityEditor()}
                  disabled={Boolean(workflowAiPreview)}
                  aria-label={t("ui.edit_workflow_name_description_and_icon")}
                  title={t("ui.edit_workflow_details")}
                >
                  <span>{flow.name}</span>
                  <IconEdit size={15} />
                </button>}
              </PageHeaderTitle>
              {readOnly && <p>{fmtRunTime(lastRun?.started_at || lastRun?.created_at)}</p>}
            </div>
          </div>
          <div className="workflow-editor-controls">
            <div className="workflow-editor-meta" aria-label={t("ui.workflow_status")}>
              {readOnly ? <>
                <StatusBadge type={lastRun?.status === "failed" ? "error" : "gray"} dot>
                  {t(workflowRunStateKey(lastRun?.status || "pending"))}
                </StatusBadge>
                <StatusBadge type="gray">{t("component.workflow_run_history.attempts", { count: lastRun?.attempt_number || 1 })}</StatusBadge>
                {lastRun && workflowRunIsLegacy(lastRun) && <span title={t("component.workflow_run_history.legacy")}>
                  <StatusBadge type="warning" dot>{t("component.workflow_run_history.legacy_list_metadata")}</StatusBadge>
                </span>}
                {lastRun?.lineage_status === "legacy_untrusted_incomplete" && <span title={t("component.workflow_run_history.legacy_lineage_untrusted")}>
                  <StatusBadge type="warning" dot>{t("component.workflow_run_history.legacy_lineage_label")}</StatusBadge>
                </span>}
              </> : <>{(() => {
                const ok = issues.length === 0;
                // A healthy graph says nothing; only problems get a badge.
                if (ok) return null;
                const state = errorCount > 0 ? "error" : "warning";
                return (
                  <button
                    type="button"
                    className={`workflow-editor-validation is-${state}`}
                    onClick={() => !ok && setShowIssues((v) => !v)}
                    title={ok ? t("ui.workflow_no_issues") : t("ui.workflow_issue_count", { count: issues.length })}
                    disabled={ok}
                  >
                    <span className="workflow-editor-validation-icon" aria-hidden="true">{ok ? "✓" : "!"}</span>
                    {ok ? t("ui.workflow_valid") : <span>{t("ui.workflow_issue_count", { count: issues.length })}</span>}
                  </button>
                );
              })()}
              {/* Active is the normal state; the trigger step on the canvas says how it starts. */}
              {flow.status !== "active" && (
                <StatusBadge type="gray" dot>{t("page.flows.draft")}</StatusBadge>
              )}
              </>}
            </div>
            <div className="workflow-editor-actions" role="toolbar" aria-label={t("ui.workflow_actions")}>
              {!readOnly && <Button
                className="workflow-editor-action"
                variant="primary"
                onClick={() => runStep()}
                disabled={streaming || errorCount > 0 || Boolean(workflowAiPreview)}
                title={errorCount > 0 ? t("page.flows.fix_before_running") : t("page.flows.test_run")}
                ariaLabel={streaming ? t("page.flows.starting") : t("page.flows.test_run")}
              >
                {streaming ? <LoadingSpinner size={15} /> : <IconPlay size={16} />}
                <span>{t("page.flows.test_run")}</span>
              </Button>}
              <Button
                className="workflow-editor-action"
                variant="outline"
                title={t("page.flows.history")}
                ariaLabel={t("page.flows.history")}
                onClick={() => {
                  const opening = !showRunHistory;
                  setShowRunHistory(opening);
                  if (opening && !lastRun && runs && runs.length) setRunResult(runs[0]);
                }}
              >
                <IconClock size={16} />
              </Button>
              {/* Rare and irreversible actions wait behind one menu. */}
              {!readOnly && <Dropdown
                align="right"
                trigger={(
                  <Button className="workflow-editor-action" variant="ghost" title={t("nav.more")} ariaLabel={t("nav.more")}
                    disabled={streaming || Boolean(workflowAiPreview)}>
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
                      <circle cx="3.5" cy="8" r="1.3" /><circle cx="8" cy="8" r="1.3" /><circle cx="12.5" cy="8" r="1.3" />
                    </svg>
                  </Button>
                )}
                items={[
                  { key: "deploy", label: t("page.flows.deploy"), icon: <IconUpload size={14} /> },
                  { key: "delete", label: t("page.flows.delete_flow"), icon: <IconTrash size={14} />, danger: true },
                ]}
                onSelect={(key: string) => {
                  if (key === "deploy") {
                    if (errorCount > 0) {
                      setShowIssues(true);
                      toast.error("Workflow isn't deployable", "Fix the graph errors first.");
                      return;
                    }
                    setDeployFlow(flow);
                  }
                  if (key === "delete") setDeleteTarget(flow.id);
                }}
              />}
            </div>
          </div>
        </div>

        {workflowAiPreview && (
          <AiEditPreviewControls
            className="workflow-ai-edit-controls"
            status={workflowAiPreview.status}
            changeCount={workflowAiPreview.changeCount}
            accepting={updateMutation.isPending || workflowAiCommitCoordinator.isCommitting()}
            workingLabel={t("page.flows.ai_edit_editing")}
            workingDescription={t("page.flows.ai_edit_streaming")}
            readyDescription={t("page.flows.ai_edit_review_preview")}
            onDiscard={discardWorkflowAiPreview}
            onAccept={acceptWorkflowAiPreview}
          />
        )}

        {/* Visual canvas; AI edit uses the same global floating chat as the file editors. */}
        {(() => {
        const canvasSurface = (
        <div style={{ position: "relative", flex: 1, minHeight: 0, background: "var(--surface-panel)", borderRadius: 14, overflow: "hidden" }}>
          {!readOnly && (
            <button
              type="button"
              className="workflow-canvas-add"
              onClick={() => openAddStep(steps.length)}
              disabled={Boolean(workflowAiPreview)}
            >
              <IconPlus size={14} />
              <span>{t("component.workflow_palette.title")}</span>
            </button>
          )}
          {showBanner && (
            <div
              className="workflow-run-banner"
              style={{
                position: "absolute", top: 16, left: "50%", transform: "translateX(-50%)", zIndex: 6,
                display: "flex", alignItems: "center", gap: 8, padding: "5px 12px", borderRadius: 999,
                background: "var(--surface-panel)", boxShadow: "0 1px 3px rgba(29, 29, 31,0.12)",
                fontSize: 12, fontWeight: 600, color: "var(--text-muted)",
                ...(readOnly ? { left: 16, right: 60, transform: "none", flexWrap: "wrap", borderRadius: 8 } : {}),
              }}
            >
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: runBannerColor || "#aeaeb2", animation: streaming ? "pulse 1.2s ease-in-out infinite" : "none" }} />
              <span style={{ color: "var(--text-strong)" }}>{waitingFor ? bannerStatus : workflowRunStatusLabel(bannerStatus)}</span>
              {lastRun?.status !== "completed" && runPercent !== null && <span className="mono" style={{ color: "var(--text-faint)" }}>{Math.floor(runPercent)}%</span>}
              {runBackgroundWait && <span style={{ color: "var(--text-faint)" }}>· {runBackgroundWait}</span>}
              {!streaming && lastRun?.error && <span style={{ color: "#d65f59", maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>· {lastRun.error}</span>}
              {runCanPause && (
                <button
                  type="button"
                  className="workflow-run-banner-action"
                  onClick={() => pauseMutation.mutate(lastRun.id)}
                  disabled={pauseMutation.isPending || cancelRunMutation.isPending}
                  aria-label={t("ui.pause_workflow_after_the_current_node")}
                  title={t("ui.pause_after_the_current_node_finishes")}
                >
                  <IconPause size={12} />
                  {pauseMutation.isPending ? "Pausing…" : "Pause"}
                </button>
              )}
              {runCanResume && (
                <WorkflowRunResumeControl
                  runId={lastRun.id}
                  action={lastRun.resume_action}
                  disabled={cancelRunMutation.isPending}
                  onResumed={(updated) => { setRunResult(updated); void queryClient.invalidateQueries({ queryKey: ["workflow-run-history-detail"] }); }}
                />
              )}
              {runCanStop && (
                <button
                  type="button"
                  className="workflow-run-banner-action is-danger"
                  onClick={() => setCancelRunTarget(lastRun.id)}
                  disabled={cancelRunMutation.isPending}
                  aria-label={t("ui.stop_workflow_run")}
                  title={t("ui.stop_before_another_node_starts")}
                >
                  <IconStop size={12} />
                  {t("ui.stop")}
                </button>
              )}
              {runCanRetry && (
                <button
                  type="button"
                  className="workflow-run-banner-action"
                  onClick={() => retryMutation.mutate(recoveryRun)}
                  disabled={retryMutation.isPending}
                  aria-label={t("ui.retry_workflow_from_the_authorized_checkpoint")}
                  title={t("ui.continue_from_the_authorized_recovery_checkpoint")}
                >
                  <IconRefresh size={12} />
                  {retryMutation.isPending ? "Retrying…" : "Retry"}
                </button>
              )}
              {retryControl}
              {runCanControl && recoveryAction?.blocked_reason === "external_effect_requires_reconciliation" && (
                <span role="status" title={t("component.workflow_run_history.external_effect_blocked")}>
                  {t("component.workflow_run_history.retry_needs_verification")}
                </span>
              )}
              {!streaming && lastRun && ["completed", "failed"].includes(lastRun.status) && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setShowFinalResult(true)}
                  style={{ height: 24, padding: "0 7px", fontSize: 11, whiteSpace: "nowrap", flexShrink: 0 }}
                >
                  {t("ui.view_result")}
                </Button>
              )}
              {!streaming && showNewerActiveRun && (
                <button
                  type="button"
                  className="workflow-run-banner-action"
                  onClick={() => {
                    if (readOnly) {
                      navigate(workflowRunPath(flow.id, newerActiveRun.id), { state: location.state });
                      return;
                    }
                    const next = new URLSearchParams(searchParams);
                    next.set("run", newerActiveRun.id);
                    next.delete("workflow_run");
                    setSearchParams(next, { replace: true, state: location.state });
                    setRunResult(newerActiveRun);
                  }}
                  title={t("ui.a_newer_run_of_this_flow_is_still_working_or_wai")}
                >
                  {newerActiveRun.status === "paused" ? "Newer run needs you" : "Open running run"}
                </button>
              )}
            </div>
          )}
          <WorkflowCanvas
            steps={steps as unknown as CanvasStep[]}
            agents={agentsById}
            fitOnResize={readOnly}
            statusById={statusById}
            previewById={previewById}
            outputById={outputById}
            onRunNode={readOnly || workflowAiPreview || workflowAiTurnLocked ? undefined : runSingleNode}
            issueById={readOnly ? undefined : issueById}
            onStepsChange={readOnly || workflowAiPreview || workflowAiTurnLocked ? undefined : (next) => {
              if (!flow.id) return;
              const previous = flow;
              const canvasSaveSeq = canvasSaveSeqRef.current + 1;
              canvasSaveSeqRef.current = canvasSaveSeq;
              const optimistic = { ...flow, steps: next as any };
              selectedFlowRef.current = optimistic;
              setSelectedFlow(optimistic);
              updateMutation.mutate(
                { id: flow.id, steps: next, canvasSaveSeq },
                {
                  onError: () => {
                    // Roll back only if no newer edit replaced this one.
                    if (canvasSaveSeqRef.current !== canvasSaveSeq) return;
                    selectedFlowRef.current = previous;
                    setSelectedFlow(previous);
                  },
                },
              );
            }}
            onNodeOpen={workflowAiPreview ? undefined : openNode}
            onAddFrom={readOnly || workflowAiPreview ? undefined : (id) => { setAddFromId(id); setShowAddStep(true); }}
          />
          {!readOnly && !workflowAiPreview && (
            <button
              type="button"
              className="workflow-talk-to-manor"
              onClick={openWorkflowAiEdit}
              disabled={updateMutation.isPending}
            >
              {t("page.flows.tell_manor")}
            </button>
          )}
        </div>
        );
        return (
          <div className="workflow-editor-body" style={{ flex: 1, minHeight: 0, display: "flex", gap: 12 }}>
            {canvasSurface}
          </div>
        );
        })()}

        {showFinalResult && lastRun && (
          <WorkflowFinalResultPanel
            key={lastRun.id}
            run={lastRun}
            result={finalResult}
            workflowName={selectedFlow.name}
            processedCount={runProgress.processed}
            outputPending={!runResultHasDetail}
            failure={workflowRunFailure(lastRun, steps)}
            retryControl={retryControl}
            onRunAgain={readOnly ? undefined : () => {
              setShowFinalResult(false);
              requestWorkflowRun(flow, lastRun.trigger_data || null);
            }}
            onClose={() => setShowFinalResult(false)}
            onOpenNode={(stepId) => {
              openNode(stepId);
              setShowFinalResult(false);
            }}
          />
        )}

        {/* Add node palette */}
        <Modal
          open={showAddStep}
          onClose={() => setShowAddStep(false)}
          title={t("component.workflow_palette.title")}
        >
          <WorkflowNodePalette onPick={addNode} />
        </Modal>

        {/* Step config slide-in panel */}
        {showStepPanel && editingStep && (
          <div
            onClick={() => setShowStepPanel(false)}
            style={{
              position: "fixed",
              inset: 0,
              zIndex: 50,
              display: "flex",
              justifyContent: "flex-end",
              background: "rgba(0,0,0,0.15)",
              backdropFilter: "blur(4px)",
            }}
          >
            <div
              onClick={(e) => e.stopPropagation()}
              style={{
                width: "100%",
                maxWidth: 420,
                background: "var(--surface-panel)",
                boxShadow: "0 20px 60px rgba(0,0,0,0.15)",
                height: "100%",
                overflowY: "auto",
                padding: 28,
                borderRadius: "32px 0 0 32px",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24 }}>
                <h3 style={{ fontSize: 17, fontWeight: 900, color: "var(--text-strong)", margin: 0 }}>{t("page.flows.step_config")}</h3>
                <button
                  onClick={() => setShowStepPanel(false)}
                  onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.color = "#1d1d1f"; }}
                  onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.color = "#aeaeb2"; }}
                  style={{ background: "transparent", border: "none", cursor: "pointer", color: "var(--text-muted)", transition: "color 0.2s", padding: 4 }}
                >
                  <IconClose size={16} />
                </button>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
                <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
                  <div style={{
                    width: 44,
                    height: 44,
                    borderRadius: 14,
                    background: STEP_GRADIENTS[editingStep.type],
                    color: "#fff",
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "center",
                  }}>
                    {STEP_ICONS[editingStep.type]}
                  </div>
                  <div>
                    <p style={{ fontSize: 14, fontWeight: 700, color: "var(--text-strong)", margin: 0 }}>{editingStep.name}</p>
                    <p style={{ fontSize: 11, color: "var(--text-muted)", margin: 0, textTransform: "capitalize" as const, fontWeight: 600 }}>{editingStep.type}</p>
                  </div>
                </div>
                <Input
                  label={t("page.flows.name")}
                  value={editingStep.name}
                  onChange={() => {}}
                  disabled
                />
                <Textarea
                  label={t("page.flows.configuration")}
                  value={editingStep.config ? JSON.stringify(editingStep.config, null, 2) : ""}
                  onChange={() => {}}
                  placeholder={t("page.flows.key_value")}
                  rows={5}
                />
              </div>
              <div style={{ display: "flex", justifyContent: "flex-end", gap: 12, marginTop: 24 }}>
                <Button variant="outline" onClick={() => setShowStepPanel(false)}>
                  {t("page.flows.close")}
                </Button>
                <Button variant="primary">
                  {t("action.save")}
                </Button>
              </div>
            </div>
          </div>
        )}

        {/* Run history — a time-machine: click a run to replay its statuses,
            outputs and generated images onto the canvas. */}
        {showRunHistory && (
          <div
            style={{
              position: "absolute", top: 72, right: 12, zIndex: 40, width: 300, maxHeight: "72vh",
              display: "flex", flexDirection: "column",
              background: "var(--surface-panel)", borderRadius: 16,
              boxShadow: "0 1px 3px rgba(29, 29, 31,0.10), 0 12px 32px rgba(29, 29, 31,0.10)", overflow: "hidden",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "13px 15px" }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-strong)" }}>{t("ui.runs")}</div>
              <button
                onClick={() => setShowRunHistory(false)}
                title={t("ui.close")}
                style={{ width: 26, height: 26, borderRadius: 7, border: "none", cursor: "pointer", background: "transparent", color: "var(--text-faint)", display: "flex", alignItems: "center", justifyContent: "center" }}
              >
                <IconClose size={14} />
              </button>
            </div>
            <div style={{ flex: 1, overflowY: "auto", padding: "0 10px 12px", display: "flex", flexDirection: "column", gap: 6 }}>
              {(runs || []).length === 0 ? (
                <p style={{ fontSize: 12.5, color: "var(--text-faint)", padding: "8px 6px", margin: 0 }}>{t("page.flows.no_runs")}</p>
              ) : (
                (runs || []).map((run: any) => {
                  const selected = runResult?.id === run.id;
                  const color = RUN_STATUS_COLOR[run.status] || "#aeaeb2";
                  const stepCount = Object.keys(run.step_results || {}).length;
                  return (
                    <button
                      key={run.id}
                      onClick={() => {
                        navigate(workflowRunPath(flow.id, run.id, run.workspace_id), {
                          state: { returnTo: `${location.pathname}${location.search}`, returnState: location.state },
                        });
                      }}
                      style={{
                        display: "flex", alignItems: "center", gap: 10, padding: "9px 11px", borderRadius: 10,
                        border: "none", cursor: "pointer", textAlign: "left",
                        background: selected ? "rgba(118, 118, 128, 0.16)" : "var(--surface-muted)",
                        transition: "background .15s",
                      }}
                    >
                      <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, flexShrink: 0 }} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 12.5, fontWeight: 600, color: "var(--text-strong)", textTransform: "capitalize" }}>{run.status}</div>
                        <div style={{ fontSize: 11, color: "var(--text-faint)" }}>{fmtRunTime(run.started_at)}</div>
                      </div>
                      {stepCount > 0 && <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{stepCount}</span>}
                    </button>
                  );
                })
              )}
            </div>
          </div>
        )}

        {/* Validation issues — click one to jump to the node */}
        {showIssues && issues.length > 0 && (
          <div
            style={{
              position: "absolute", top: 72, right: 12, zIndex: 41, width: 320, maxHeight: "72vh",
              display: "flex", flexDirection: "column",
              background: "var(--surface-panel)", borderRadius: 16,
              boxShadow: "0 1px 3px rgba(29, 29, 31,0.10), 0 12px 32px rgba(29, 29, 31,0.10)", overflow: "hidden",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "13px 15px" }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-strong)" }}>
                Issues <span className="mono" style={{ color: "var(--text-faint)", fontWeight: 600 }}>{issues.length}</span>
              </div>
              <button
                onClick={() => setShowIssues(false)}
                title={t("ui.close")}
                style={{ width: 26, height: 26, borderRadius: 7, border: "none", cursor: "pointer", background: "transparent", color: "var(--text-faint)", display: "flex", alignItems: "center", justifyContent: "center" }}
              >
                <IconClose size={14} />
              </button>
            </div>
            <div style={{ flex: 1, overflowY: "auto", padding: "0 10px 12px", display: "flex", flexDirection: "column", gap: 6 }}>
              {issues.map((issue, i) => {
                const color = issue.level === "error" ? "#d65f59" : "#cf9b44";
                const clickable = !!issue.nodeId;
                return (
                  <button
                    key={i}
                    onClick={() => { if (issue.nodeId) { setConfigStepId(issue.nodeId); setShowIssues(false); } }}
                    disabled={!clickable}
                    style={{
                      display: "flex", alignItems: "flex-start", gap: 9, padding: "9px 11px", borderRadius: 10,
                      border: "none", cursor: clickable ? "pointer" : "default", textAlign: "left",
                      background: "var(--surface-muted)", transition: "background .15s",
                    }}
                  >
                    <span style={{ width: 15, height: 15, borderRadius: "50%", background: color, color: "#fff", flexShrink: 0, marginTop: 1, display: "flex", alignItems: "center", justifyContent: "center", fontSize: 10, fontWeight: 800 }}>!</span>
                    <span style={{ fontSize: 12.5, lineHeight: 1.45, color: "var(--text-muted)" }}>{issue.message}</span>
                  </button>
                );
              })}
            </div>
          </div>
        )}

        {workflowIdentityModal}

        <ConfirmDialog
          open={!!deleteTarget}
          onClose={() => { if (!deleteMutation.isPending) setDeleteTarget(null); }}
          onConfirm={() => { if (deleteTarget) deleteMutation.mutate(deleteTarget); }}
          title={t("page.flows.delete_flow")}
          message={t("page.flows.delete_message")}
          confirmLabel={t("action.delete")}
          danger
          loading={deleteMutation.isPending}
          closeOnConfirm={false}
        />

        <ConfirmDialog
          open={!!cancelRunTarget}
          onClose={() => { if (!cancelRunMutation.isPending) setCancelRunTarget(null); }}
          onConfirm={() => { if (cancelRunTarget) cancelRunMutation.mutate(cancelRunTarget); }}
          title={t("ui.stop_workflow_run")}
          message="The current node may finish, including any external action already submitted. No additional nodes will start."
          confirmLabel="Stop run"
          cancelLabel="Keep running"
          danger
          loading={cancelRunMutation.isPending}
          closeOnConfirm={false}
        />

        <WorkflowDeployModal
          flow={deployFlow}
          open={!!deployFlow}
          onClose={() => setDeployFlow(null)}
        />

        {!readOnly && <WorkflowNodeConfigPanel
          step={(flow.steps || []).find((s: any) => s.id === configStepId) || null}
          lastResult={configStepId ? stepResults[configStepId] : undefined}
          runVariables={lastRun?.variables}
          currentWorkflowId={flow.id}
          nodes={(flow.steps || []).map((s: any) => ({
            id: s.id,
            name: s.name,
            type: s.type,
            targets: workflowStepTargets(s),
            outputs: workflowNodeOutputs(s),
          }))}
          onSave={async (updated) => {
            if (workflowAiPreviewRef.current || updateMutation.isPending) {
              throw new Error("Finish the current Workflow edit before saving this node.");
            }
            const current = selectedFlowRef.current;
            if (!current || current.id !== flow.id) throw new Error("Reopen this Workflow before saving.");
            await updateMutation.mutateAsync({
              id: current.id,
              steps: (current.steps || []).map((s: any) => (s.id === updated.id ? updated : s)),
            });
          }}
          onRunResult={recordSingleResult}
          onClose={() => setConfigStepId(null)}
        />}
        {runInputModal}
      </div>
    );
  }

  /* ---------------------------------------------------------------- */
  /*  Flow list view                                                   */
  /* ---------------------------------------------------------------- */

  return (
    <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: 0, overflow: "hidden", position: "relative", zIndex: 10 }}>
      {/* Header */}
      <PageHeader
        title={t("nav.flows")}
        subtitle={embedded ? undefined : t("page.flows.subtitle")}
        toolbar={(
          <SmartToolbar
            searchValue={search}
            onSearchChange={setSearch}
            searchPlaceholder={t("page.flows.search_placeholder")}
            className="w-full sm:w-[280px]"
          />
        )}
        actions={(
          <Dropdown
            align="right"
            trigger={<PageHeaderAddButton label={t("page.flows.add_flow")} caret />}
            items={[
              {
                key: "create",
                label: t("page.flows.create_flow"),
                icon: <IconPlus size={14} />,
              },
              {
                key: "templates",
                label: t("page.flows.templates"),
                icon: <IconFlow size={14} />,
              },
              {
                key: "import",
                label: t("page.flows.import_flow"),
                icon: <IconUpload size={14} />,
              },
            ]}
            onSelect={(key) => {
              if (key === "create") setShowCreateModal(true);
              if (key === "templates") setShowTemplates(true);
              if (key === "import") setShowImportModal(true);
            }}
          />
        )}
      />

      {/* Grid */}
      <div style={{ flex: 1, overflowY: "auto", padding: 0 }}>
        {isLoading ? (
          <CardGridSkeleton count={6} minWidth={260} />
        ) : filtered.length === 0 ? (
          <EmptyState
            icon={
              <IconFlow size={32} className="text-stone-300" />
            }
            title={t("page.flows.no_flows")}
            description={t("page.flows.no_flows_desc")}
          />
        ) : (
          <div style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 340px), 1fr))",
            gap: 24,
          }}>
            {filtered.map((flow: any) => {
              return (
                <CompactCard
                  key={flow.id}
                  icon={
                    <IconTile size={34}>
                      {workflowIconGlyph(flow.icon, 18)}
                    </IconTile>
                  }
                  title={
                    <HoverMarqueeText
                      className="compact-card-title"
                      text={flow.name}
                    />
                  }
                  subtitle={flow.workspace_name && flow.workspace_id
                    ? <span title={flow.description || undefined}>{flow.workspace_id.slice(-6)} · {flow.workspace_name}</span>
                    : flow.description || t(TRIGGER_LABELS[flow.trigger] || flow.trigger)}
                  meta={
                    <>
                      <span className="mono" style={{ fontSize: 11, color: "var(--text-faint)", fontWeight: 600 }}>
                        {flow.step_count ?? 0}
                      </span>
                      <span
                        title={flow.status === "active" ? t("page.flows.active") : t("page.flows.draft")}
                        style={{
                          width: 7,
                          height: 7,
                          borderRadius: "50%",
                          background: flow.status === "active" ? "#4f9c84" : "#aeaeb2",
                        }}
                      />
                    </>
                  }
                  action={
                    <div className="workflow-card-actions">
                      <button
                        className="workflow-card-action-button"
                        onClick={() => void runFromList(flow)}
                        disabled={streaming}
                        title={t("page.flows.run")}
                        aria-label={`${t("page.flows.run")} ${flow.name}`}
                      >
                        <IconPlay size={14} />
                      </button>
                      <button
                        className="workflow-card-action-button"
                        onClick={(event) => {
                          event.stopPropagation();
                          const rect = event.currentTarget.getBoundingClientRect();
                          flowContextMenu.showAt(rect.right, rect.bottom + 6, workflowContextItems(flow));
                        }}
                        title={t("page.flows.more_actions")}
                        aria-label={`${t("page.flows.more_actions")}: ${flow.name}`}
                        aria-haspopup="menu"
                      >
                        <IconMoreHorizontal size={16} />
                      </button>
                    </div>
                  }
                  onContextMenu={(event) => flowContextMenu.show(event, workflowContextItems(flow))}
                  onClick={() => openFlowEditor(flow)}
                />
              );
            })}
          </div>
        )}
      </div>

      {workflowIdentityModal}

      {/* Delete confirm */}
      <ConfirmDialog
        open={!!deleteTarget}
        onClose={() => { if (!deleteMutation.isPending) setDeleteTarget(null); }}
        onConfirm={() => { if (deleteTarget) deleteMutation.mutate(deleteTarget); }}
        title={t("page.flows.delete_flow")}
        message={t("page.flows.delete_message")}
        confirmLabel={t("action.delete")}
        danger
        loading={deleteMutation.isPending}
        closeOnConfirm={false}
      />

      {/* Create modal */}
      <Modal
        open={showCreateModal}
        onClose={() => setShowCreateModal(false)}
        title={t("page.flows.create_flow")}
        footer={
          <>
            <Button variant="outline" onClick={() => setShowCreateModal(false)}>
              {t("action.cancel")}
            </Button>
            <Button
              variant="primary"
              onClick={handleCreate}
              disabled={!formName.trim()}
              loading={createMutation.isPending}
            >
              {t("action.create")}
            </Button>
          </>
        }
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Input
            label={t("page.flows.name")}
            value={formName}
            onChange={(e) => setFormName(e.target.value)}
            placeholder={t("page.flows.name_placeholder")}
          />
          <Textarea
            label={t("page.flows.description")}
            value={formDesc}
            onChange={(e) => setFormDesc(e.target.value)}
            placeholder={t("page.flows.description_placeholder")}
            rows={3}
          />
          <div>
            <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "var(--text-muted)", marginBottom: 6 }}>{t("page.flows.trigger_type")}</label>
            <select
              value={formTrigger}
              onChange={(e) => setFormTrigger(e.target.value as any)}
              className="manor-input"
            >
              <option value="manual">{t("page.flows.trigger_manual_option")}</option>
              <option value="event">{t("page.flows.trigger_event_option")}</option>
              <option value="schedule">{t("page.flows.trigger_schedule_option")}</option>
            </select>
          </div>
        </div>
      </Modal>

      <WorkflowImportModal open={showImportModal} onClose={() => setShowImportModal(false)} />

      <WorkflowTemplates
        open={showTemplates}
        onClose={() => setShowTemplates(false)}
        onPick={(tpl) => templateMutation.mutate(tpl)}
        installingId={templateMutation.isPending ? templateMutation.variables?.id || null : null}
      />

      {/* Deploy from the list-view card drawer (the editor has its own copy). */}
      <WorkflowDeployModal
        flow={deployFlow}
        open={!!deployFlow}
        onClose={() => setDeployFlow(null)}
      />
      {flowContextMenu.menu && (
        <ContextMenu
          items={flowContextMenu.menu.items}
          x={flowContextMenu.menu.x}
          y={flowContextMenu.menu.y}
          onClose={flowContextMenu.close}
        />
      )}
      {runInputModal}
    </div>
  );
}
