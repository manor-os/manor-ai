import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type WorkspaceAppAction, type WorkspaceAppActionInput } from "../../lib/api";
import { viewerPathForDocumentId } from "../../lib/fileReferences";
import { isMediaResultFile, partitionGeneratedResultFiles, type ResultFileMetadata } from "../../lib/resultFiles";
import GeneratedResultFiles from "../GeneratedResultFiles";
import RunMediaClips from "../ui/RunMediaClips";
import RunProduction, { type RunProductionConfig } from "../ui/RunProduction";
import { getLocale, t } from "../../lib/i18n";
import { getPreferredTimeZone } from "../../lib/format";
import { workflowRunPath } from "../../lib/workflowRoutes";
import { usePreviewFeatureAccess } from "../../lib/previewFeatureAccess";
import { IconArrowLeft, IconArrowRight, IconChatBubble, IconCheck, IconChevronDown, IconPause } from "../icons";
import Button from "../ui/Button";
import ConfirmDialog from "../ui/ConfirmDialog";
import WorkspaceAppSetupMaterials from "./WorkspaceAppSetupMaterials";
import { workflowFileName } from "../workflows/WorkflowKnowledgeFileField";
import {
  presetPlaceholderTokens,
  workflowSchemaChoices,
  workflowSchemaChosen,
  workflowSchemaOptionLabel,
  workflowSchemaRecordSelect,
} from "../workflows/workflowSchema";
import WorkflowRunResumeControl from "../workflows/WorkflowRunResumeControl";
import WorkflowRunRetryControl from "../workflows/WorkflowRunRetryControl";
import WorkflowStepActivityLine from "../workflows/WorkflowStepActivity";
import { StructuredReviewValue } from "../workflows/WorkflowApprovalReview";
import WorkflowErrorText from "../workflows/WorkflowErrorText";
import { workflowRunInputPageSchema, workflowRunInputPages, type WorkflowRunInputPage } from "../workflows/workflowRunInputPages";
import {
  formatWorkflowDuration,
  workflowRunDurationMs,
  workflowRunBackgroundWait,
  workflowRunTechnicalDetails,
  workflowStepActivity,
  workflowStepNeedsPerson,
  workflowRunProgressPercent,
  workflowStepStatusKey,
  workflowStepOutcome,
  type WorkflowRunAction,
  type WorkflowRunNode,
} from "../workflows/workflowRunDisplay";
import { actionIcon, fileVisual, runStatusLabel } from "./workspaceAppRunDisplay";
import {
  WorkflowSchemaFields,
  parseWorkflowSchemaDraft,
  setWorkflowValueAtPath,
  workflowInputErrorPath,
  workflowSchemaDraft,
  type WorkflowInputSchema,
} from "../workflows/WorkflowSchemaFields";

type Run = {
  id: string;
  workflow_id?: string;
  workspace_id?: string | null;
  binding_id?: string | null;
  trigger_source?: string | null;
  started_at?: string | null;
  created_at?: string | null;
  completed_at?: string | null;
  updated_at?: string | null;
  attempt_number?: number;
  status: string;
  business_outcome?: string;
  capabilities?: { can_control?: boolean };
  intervention?: WorkflowRunAction | null;
  error?: string | null;
  current_step_id?: string | null;
  current_step_name?: string | null;
  processed_count?: number | null;
  total_count?: number | null;
  artifact_count?: number | null;
  workflow_steps?: Array<{ id: string; name: string; status: string }> | null;
  variables?: Record<string, unknown>;
  step_results?: Record<string, Record<string, unknown>>;
  execution_trace?: Array<Record<string, unknown>>;
  /** The Workflow's declared production and the run's units so far. */
  production?: RunProductionConfig;
};

function fieldSchema(input: WorkspaceAppActionInput): WorkflowInputSchema {
  const base: WorkflowInputSchema = input.schema
    ? { ...input.schema }
    : input.type === "json"
      ? { type: "string", "x-workflow-type": "json", "x-ui": { control: "textarea", rows: 4 } }
      : { type: input.type || "string" };
  return {
    ...base,
    title: base.title || input.label,
    description: base.description || input.description,
    default: base.default ?? input.default,
    "x-ui": { ...(base["x-ui"] || {}), ...(input.hidden ? { hidden: true } : {}) },
  };
}

function runFiles(run?: Run): Array<{ id: string; name: string; path: string; fileType: string; mimeType: string; isFinal?: boolean }> {
  if (!run) return [];
  const finalResult = run.variables?.__result;
  const finalFiles = finalResult && typeof finalResult === "object" && !Array.isArray(finalResult)
    ? (finalResult as Record<string, unknown>).files : undefined;
  if (Array.isArray(finalFiles) && finalFiles.length > 0) {
    const seen = new Set<string>();
    const delivered = finalFiles.flatMap((value) => {
      if (!value || typeof value !== "object" || Array.isArray(value)) return [];
      const ref = value as Record<string, unknown>;
      const id = String(ref.document_id || "").trim();
      const path = viewerPathForDocumentId(id);
      if (!path || seen.has(id)) return [];
      seen.add(id);
      return [{ id, path, name: String(ref.name || ref.filename || ""), fileType: String(ref.file_type || ref.format || ""), mimeType: String(ref.mime_type || ""), isFinal: true }];
    });
    if (delivered.length > 0) return delivered;
  }
  const finalArtifacts = finalResult && typeof finalResult === "object" && !Array.isArray(finalResult)
    ? (finalResult as Record<string, unknown>).artifacts : undefined;
  if (Array.isArray(finalArtifacts) && finalArtifacts.length === 0) return [];
  const finalIds = new Set(Array.isArray(finalArtifacts) ? finalArtifacts.flatMap((ref) => {
    if (!ref || typeof ref !== "object" || Array.isArray(ref)) return [];
    const id = String((ref as Record<string, unknown>).document_id || "").trim();
    return viewerPathForDocumentId(id) ? [id] : [];
  }) : []);
  const refs: unknown[] = [];
  const collectOutput = (output: unknown, depth = 0) => {
    if (!output || typeof output !== "object" || depth > 5 || refs.length >= 50) return;
    if (Array.isArray(output)) {
      output.forEach((item) => collectOutput(item, depth + 1));
      return;
    }
    const record = output as Record<string, unknown>;
    if (record.document_id) refs.push(record);
    Object.values(record).forEach((value) => collectOutput(value, depth + 1));
  };
  for (const result of Object.values(run.step_results || {})) {
    if (Array.isArray(result.artifact_refs)) refs.push(...result.artifact_refs);
    collectOutput(result.output);
  }
  collectOutput(run.variables?.__result);
  for (const event of run.execution_trace || []) {
    if (Array.isArray(event.artifact_refs)) refs.push(...event.artifact_refs);
  }
  // What the person supplied (the recording they picked) is echoed through
  // step outputs, but it is an input, never something this run made.
  const supplied = new Set<string>();
  const collectSupplied = (value: unknown, depth = 0) => {
    if (!value || typeof value !== "object" || depth > 5) return;
    if (Array.isArray(value)) { value.forEach((item) => collectSupplied(item, depth + 1)); return; }
    const record = value as Record<string, unknown>;
    if (typeof record.document_id === "string") supplied.add(record.document_id.trim());
    Object.values(record).forEach((item) => collectSupplied(item, depth + 1));
  };
  collectSupplied((run as { trigger_data?: unknown }).trigger_data);
  const seen = new Set<string>(supplied);
  return refs.flatMap((value) => {
    if (!value || typeof value !== "object" || Array.isArray(value)) return [];
    const ref = value as Record<string, unknown>;
    const id = String(ref.document_id || "").trim();
    const path = viewerPathForDocumentId(id);
    if (!path || seen.has(id)) return [];
    seen.add(id);
    const sourcePath = String(ref.path || ref.fs_path || "");
    return [{ id, path, name: String(ref.name || ref.filename || sourcePath.split("/").pop() || ref.title || ""), fileType: String(ref.file_type || ref.format || ""), mimeType: String(ref.mime_type || ""), isFinal: finalIds.size ? finalIds.has(id) : undefined }];
  });
}

const LONG_RESULT_TEXT = 160;
// The file type a production's declared final plays as, before its file is read.
const PRODUCTION_MIME: Record<NonNullable<RunProductionConfig["media"]>, string> = {
  video: "video/mp4", audio: "audio/mpeg", image: "image/png",
};
const INLINE_OBJECT_FIELDS = 4;

function isEmptyResultValue(value: unknown): boolean {
  return value === "" || value == null || (Array.isArray(value) && value.length === 0)
    || (typeof value === "object" && !Array.isArray(value) && Object.keys(value as object).length === 0);
}

/** Text too long to read inline, a list of several items, or a list of records. */
function isLongResultValue(value: unknown): boolean {
  if (typeof value === "string") return value.length > LONG_RESULT_TEXT;
  if (Array.isArray(value)) {
    return value.length > 3 || value.some((item) => typeof item === "object" || (typeof item === "string" && item.length > 80));
  }
  return false;
}

/** Nested facts become one level of labelled fields: work.title reads as "Work Title". */
function flattenResultFields(record: Record<string, unknown>, prefix = ""): Record<string, unknown> {
  const flat: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record)) {
    const name = prefix ? `${prefix}_${key}` : key;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      Object.assign(flat, flattenResultFields(value as Record<string, unknown>, name));
    } else {
      flat[name] = value;
    }
  }
  return flat;
}

/**
 * Splits a result into the facts read inline and the detail folded away. A record
 * keeps its short facts inline; one with many fields folds whole, so the page stays short.
 */
function splitResultFields(record: Record<string, unknown>): [Record<string, unknown>, Record<string, unknown>] {
  const brief: Record<string, unknown> = {};
  const detail: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(record)) {
    if (isEmptyResultValue(value)) continue;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const [inner, folded] = splitResultFields(value as Record<string, unknown>);
      if (Object.keys(inner).length > INLINE_OBJECT_FIELDS) {
        detail[key] = value;
      } else {
        if (Object.keys(inner).length) brief[key] = inner;
        if (Object.keys(folded).length) detail[key] = folded;
      }
    } else if (isLongResultValue(value)) {
      detail[key] = value;
    } else {
      brief[key] = value;
    }
  }
  return [brief, detail];
}

function outputText(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return ""; }
}

/** Bracketed blanks an entry's starting text asks the person to fill ([event], [audience], ...) that are still there. */
export function unfilledPresetPlaceholders(template: unknown, value: unknown): string[] {
  if (typeof value !== "string") return [];
  return presetPlaceholderTokens(template).filter((blank) => value.includes(blank));
}

function isJsonText(text: string): boolean {
  const trimmed = text.trim();
  if (!/^[[{]/.test(trimmed)) return false;
  try { JSON.parse(trimmed); return true; } catch { return false; }
}

function reviewValue(value: unknown, schema?: WorkflowInputSchema): string {
  // A switch reads as Yes / No, not the stored true / false.
  if (typeof value === "boolean") {
    return t(value ? "component.workspace_app_view.check_yes" : "component.workspace_app_view.check_no");
  }
  // A choice reads as the option the person picked, not its stored value.
  if (schema?.enum?.some((option) => String(option) === String(value))) {
    return workflowSchemaOptionLabel(schema, value, String);
  }
  if (schema?.items && workflowSchemaChoices(schema)) {
    return [...workflowSchemaChosen(value)]
      .map((option) => workflowSchemaOptionLabel(schema.items!, option, String))
      .join(", ");
  }
  if (workflowSchemaRecordSelect(schema)) return [...workflowSchemaChosen(value)].join(", ");
  const text = outputText(value);
  if (!text.startsWith("[")) return text;
  try {
    const refs = JSON.parse(text) as unknown;
    if (Array.isArray(refs) && !refs.length) return "";
    if (Array.isArray(refs) && refs.length && refs.every((ref) =>
      ref && typeof ref === "object" && "document_id" in ref && "name" in ref && typeof ref.name === "string",
    )) return refs.map((ref) => ref.name).join(", ");
  } catch { /* Keep ordinary text as entered. */ }
  return text;
}

/** A picked project reads as its title, the way the picker showed it, not its id. */
function ReviewProjectName({ workspaceId, schema, projectId }: { workspaceId: string; schema?: WorkflowInputSchema; projectId: string }) {
  const ui = schema?.["x-ui"] || {};
  const projectType = String(ui.project_type || "");
  const stages = ui.allowed_stages || [];
  const candidates = useQuery({
    queryKey: ["workflow-project-candidates", workspaceId, projectType, stages.join(",")],
    queryFn: () => api.workflows.listProjectCandidates({ workspace_id: workspaceId, project_type: projectType, stages }),
    enabled: Boolean(workspaceId && projectType && stages.length),
    staleTime: 60_000,
  });
  const match = (candidates.data || []).find((candidate: { id: string; title?: string }) => candidate.id === projectId);
  return <>{match?.title || projectId}</>;
}

/** The run's steps; behind a production view they are the detail, folded until opened. */
function StepsSection({ folded, children }: { folded: boolean; children: React.ReactNode }) {
  return folded
    ? <details className="workspace-app-flow__raw"><summary>{t("component.workspace_app_view.run_steps")}</summary>{children}</details>
    : <section><h4>{t("component.workspace_app_view.run_steps")}</h4>{children}</section>;
}

function runSourceLabel(source?: string | null, bindingId?: string | null): string {
  if (source === "manual" && !bindingId) return t("component.workspace_app_view.source_direct_workflow");
  if (source === "workspace_chat") return t("component.workspace_app_view.source_chat");
  if (source === "schedule" || source === "scheduled") return t("component.workspace_app_view.source_schedule");
  if (source === "event" || source === "workspace_event") return t("component.workspace_app_view.source_automation");
  return t("component.workspace_app_view.source_manual");
}

/** A run's share of its stages for the history list; a finished run needs no figure. */
function historyProgressLabel(item: { status: string; processed_count?: number | null; total_count?: number | null }): string | null {
  if (item.status === "completed") return null;
  const percent = workflowRunProgressPercent(item.processed_count, item.total_count, item.status);
  return percent === null ? null : `${Math.floor(percent)}%`;
}

function runTime(value?: string | null): string {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleString(getLocale(), { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: getPreferredTimeZone() })
    : "";
}

export default function WorkspaceAppActionFlow({
  action, workspaceId, canStart, chatAvailable, onUseChat, onBack, onFollowProgress, initialValues,
}: {
  action: WorkspaceAppAction;
  /** Inputs already chosen where the person opened it (the episode a Remake is for). */
  initialValues?: Record<string, string>;
  workspaceId: string;
  canStart: boolean;
  chatAvailable: boolean;
  onUseChat: () => void;
  onBack: () => void;
  onFollowProgress?: () => void;
}) {
  const queryClient = useQueryClient();
  const location = useLocation();
  const navigate = useNavigate();
  // The same icon the App's grid shows for this entry.
  const ActionIcon = actionIcon(action);
  const restoredApp = location.state?.workspaceApp;
  const inputs = useMemo(() => ((action.inputs || []) as WorkspaceAppActionInput[]).filter((input) => input.key && !input.hidden), [action]);
  const schema = useMemo<WorkflowInputSchema>(() => ({
    type: "object",
    properties: Object.fromEntries(((action.inputs || []) as WorkspaceAppActionInput[]).filter((input) => input.key).map((input) => [input.key, fieldSchema(input)])),
    required: inputs.filter((input) => input.required).map((input) => input.key),
    // The Flow's rules across inputs are checked on the page that asks for them, before it starts.
    ...(action.input_rules?.length ? { allOf: action.input_rules } : {}),
  }), [action, inputs]);
  const [values, setValues] = useState<unknown>(() => workflowSchemaDraft(schema, initialValues || {}));
  const [step, setStep] = useState(0);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [runId, setRunId] = useState<string | null>(() => restoredApp?.workspaceId === workspaceId
    && restoredApp?.actionKey === action.key ? restoredApp.runId || null : null);
  const [showAllFiles, setShowAllFiles] = useState(false);
  const [fileBusy, setFileBusy] = useState(false);
  const resultPanelRef = useRef<HTMLDivElement | null>(null);
  const initialRecentRunId = useRef<string | null | undefined>(undefined);
  // Inputs that name a step are asked together, one page per step; otherwise inputs that share an
  // x-ui.page (a settings page) are asked together, and the rest one by one.
  const pages = useMemo(() => workflowRunInputPages(inputs) ?? inputs.reduce<WorkflowRunInputPage[]>((grouped, input) => {
    const page = input.schema?.["x-ui"]?.page;
    const last = grouped[grouped.length - 1];
    if (page && last && last.id === `page:${page}`) last.keys.push(input.key);
    else grouped.push({ id: page ? `page:${page}` : input.key, title: "", keys: [input.key] });
    return grouped;
  }, []), [inputs]);
  const currentPage = pages[step];
  const currentInputs = currentPage ? inputs.filter((input) => currentPage.keys.includes(input.key)) : [];
  const currentSchema: WorkflowInputSchema | null = currentPage ? workflowRunInputPageSchema(schema, currentPage.keys) : null;

  // Several entries can start one Flow (How-to steps, Tips list, ...); each
  // lists only its own runs. The Flow's own entry also keeps runs started
  // elsewhere (Chat, older runs) that no entry claims.
  const ownsUnlabelledRuns = action.key === `workflow:${action.binding_id}`;
  const recent = useQuery({
    queryKey: ["workspace-app-recent-run", workspaceId, action.binding_id, action.workflow_id, action.key],
    queryFn: ({ signal }) => api.workflows.listRuns({ workspace_id: workspaceId, binding_id: action.binding_id,
      workflow_id: action.workflow_id, include_unbound: true, app_action: action.key,
      include_unlabelled: ownsUnlabelledRuns, limit: 5 }, AbortSignal.any([signal, AbortSignal.timeout(15_000)])) as Promise<Run[]>,
    enabled: Boolean(action.binding_id && action.workflow_id),
    retry: false,
    refetchInterval: (query) => query.state.status === "error" ? 30_000 : 3000,
  });
  const isActionRun = useCallback((item: Run) => item.workspace_id === workspaceId && item.workflow_id === action.workflow_id && (
    item.binding_id === action.binding_id || (!item.binding_id && item.trigger_source === "manual")
  ), [action.binding_id, action.workflow_id, workspaceId]);
  const historyRuns = (recent.data || []).filter(isActionRun);
  const startSetup = useMutation({
    mutationFn: () => api.workspaces.reconcileBlueprintStartup(workspaceId),
    onSettled: () => { void setup.refetch(); },
  });
  const setup = useQuery({
    queryKey: ["workspace-setup-status", workspaceId],
    queryFn: () => api.workspaces.setupStatus(workspaceId),
    enabled: Boolean(workspaceId && !runId),
    staleTime: 0,
    refetchInterval: (query) => query.state.data?.automatic_setup_state && !query.state.data.ready ? 5000 : false,
  });
  useEffect(() => {
    if (recent.isPending) return;
    const latestId = recent.data?.find(isActionRun)?.id || null;
    if (initialRecentRunId.current === undefined) initialRecentRunId.current = latestId;
    else if (latestId && latestId !== initialRecentRunId.current) {
      initialRecentRunId.current = latestId;
      if (!runId) setRunId(latestId);
    }
  }, [recent.isPending, recent.data, runId, isActionRun]);
  const run = useQuery({
    queryKey: ["workspace-app-run", runId],
    queryFn: ({ signal }) => api.workflows.getRun(runId!, false, AbortSignal.any([signal, AbortSignal.timeout(15_000)])) as Promise<Run>,
    enabled: Boolean(runId),
    // A busy database or a dropped request heals on its own: retry it quietly
    // and keep showing the last state, so one failed poll never blanks the run.
    retry: 2,
    refetchInterval: (query) => ["completed", "failed", "cancelled"].includes(String(query.state.data?.status || "")) ? false : query.state.status === "error" ? 6000 : 3000,
  });
  const runUnavailable = run.isError && !run.data;
  const terminal = ["completed", "failed", "cancelled"].includes(run.data?.status || "");
  const runMismatch = Boolean(run.data && !isActionRun(run.data));
  const detail = useQuery({
    queryKey: ["workspace-app-run-detail", runId, run.data?.status],
    queryFn: ({ signal }) => api.workflows.getRun(runId!, true, AbortSignal.any([signal, AbortSignal.timeout(15_000)])) as Promise<Run>,
    enabled: Boolean(runId && terminal && !runMismatch),
    retry: 2,
  });
  const visibleRun = runMismatch ? undefined : detail.data || run.data;
  const technicalDetails = workflowRunTechnicalDetails(visibleRun, t("component.workflow_run.error_truncated"));
  const files = runFiles(runMismatch ? undefined : detail.data);
  const unnamedFileIds = files.filter((file) => !file.name).map((file) => file.id);
  const fileNames = useQuery({
    queryKey: ["workspace-app-run-file-names", runId, unnamedFileIds],
    queryFn: async () => Object.fromEntries(await Promise.all(unnamedFileIds.map(async (id) => {
      try { return [id, (await api.documents.get(id)).name] as const; }
      catch { return [id, ""] as const; }
    }))),
    enabled: terminal && unnamedFileIds.length > 0,
    staleTime: 5 * 60 * 1000,
  });
  const workflowSteps = runMismatch ? [] : run.data?.workflow_steps || [];
  const displayFiles = files.map((file) => ({ ...file, key: file.id, reference: file.path,
    name: file.name || fileNames.data?.[file.id] || t("component.workspace_app_view.file") }));
  // A run paused only for background work (an automatic retry, a render) is
  // still running; only a wait for a person needs attention.
  const backgroundWait = runMismatch ? null : workflowRunBackgroundWait(visibleRun);
  const shownStatus = backgroundWait ? "running" : visibleRun?.status;
  const isRunning = !runMismatch && !runUnavailable && shownStatus === "running";
  const progressPercent = workflowRunProgressPercent(
    visibleRun?.processed_count, visibleRun?.total_count, visibleRun?.status,
  );
  const hasRunProgress = progressPercent !== null;
  const shownPercent = progressPercent ?? 0;
  // A Workflow that declares its production is shown as what it is making.
  const production = runMismatch ? undefined : visibleRun?.production;
  // Its finished whole (a video, a song) leads the production view instead of
  // the file list: the one the Workflow names, else the run's first final file
  // of the medium it makes.
  const productionMedia = production?.media || "video";
  const isProductionFile = (file: ResultFileMetadata) => isMediaResultFile(file, productionMedia);
  const declaredFinal = production?.final ? {
    key: production.final.document_id, reference: viewerPathForDocumentId(production.final.document_id) || "",
    name: production.final.name || t("component.workspace_app_view.file"), mimeType: PRODUCTION_MIME[productionMedia],
  } : null;
  const finalResult = !production ? null : declaredFinal?.reference ? declaredFinal
    : partitionGeneratedResultFiles(displayFiles).primary.find(isProductionFile) || null;
  // With a declared final, a revision also inherits earlier versions' results;
  // those are reached through the version switcher, not listed again here.
  const listedFiles = finalResult ? displayFiles.filter((file) => file.key !== finalResult.key
    && !(declaredFinal?.reference && isProductionFile(file))) : displayFiles;
  const hasPrimaryFiles = partitionGeneratedResultFiles(listedFiles).primary.length > 0;
  useEffect(() => { setShowAllFiles(false); }, [runId]);
  useEffect(() => {
    if (runId) resultPanelRef.current?.scrollIntoView({ block: "start" });
  }, [runId]);

  const [cancelOpen, setCancelOpen] = useState(false);
  const cancelRun = useMutation({
    mutationFn: () => api.workflows.cancelRun(runId!),
    onSuccess: () => {
      setCancelOpen(false);
      void run.refetch();
      void recent.refetch();
      void queryClient.invalidateQueries({ queryKey: ["workspace-app-section", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] });
    },
  });
  const start = useMutation({
    // A request the browser could not send (the connection dropped, Manor restarting) never reached
    // the server, so it is sent again quietly instead of showing the browser's own error.
    retry: (count, error) => count < 2 && error instanceof TypeError,
    retryDelay: 1500,
    mutationFn: async () => {
      const nextErrors: Record<string, string> = {};
      const parsed = parseWorkflowSchemaDraft(schema, values, "inputs", false, nextErrors);
      Object.assign(nextErrors, placeholderErrors(inputs));
      setErrors(nextErrors);
      if (Object.keys(nextErrors).length) throw new Error(t("component.workspace_app_view.check_inputs"));
      return api.workflows.runBinding(action.binding_id!, {
        variables: parsed,
        trigger_data: { workspace_manual: true, _workspace_app_action: action.key },
        execute: false,
      }) as Promise<Run>;
    },
    onSuccess: (created) => {
      setRunId(created.id);
      void queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-workflow-runs", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-app-section", workspaceId] });
      void recent.refetch();
    },
  });

  // A preset's blanks are caught here, before any work runs on them.
  const placeholderErrors = (fields: WorkspaceAppActionInput[]) => Object.fromEntries(fields.flatMap((input) => {
    const blanks = unfilledPresetPlaceholders(input.default, (values as Record<string, unknown>)?.[input.key]);
    return blanks.length
      ? [[workflowInputErrorPath("inputs", [input.key]), t("component.workspace_app_view.fill_placeholders", { placeholders: blanks.join(", ") })]]
      : [];
  }));
  const next = () => {
    if (fileBusy) return;
    if (!currentSchema) return;
    const nextErrors: Record<string, string> = {};
    parseWorkflowSchemaDraft(currentSchema, values, "inputs", false, nextErrors);
    Object.assign(nextErrors, placeholderErrors(currentInputs));
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) return;
    setStep((previous) => previous + 1);
  };

  const flowHref = workflowRunPath(action.workflow_id || "", runId || "", workspaceId);
  // The full run page is part of Flows; the result here is complete without it.
  const flowsAccess = usePreviewFeatureAccess("flows");
  const returnTo = `${location.pathname}${location.search}${location.hash}`;
  const returnState = { ...location.state, workspaceApp: { workspaceId, actionKey: action.key, runId } };
  const runLinkState = { returnTo, returnState };
  const rememberOrigin = (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
      navigate(returnTo, { replace: true, state: returnState });
    }
  };
  const resultValue = detail.data?.variables?.__result;
  // Whatever the result says in words is shown; the data behind it stays folded.
  const resultRecord = resultValue && typeof resultValue === "object" && !Array.isArray(resultValue)
    ? resultValue as Record<string, unknown> : null;
  const resultSummary = resultRecord
    ? String([resultRecord.summary, resultRecord.reason, resultRecord.message].find((text) => typeof text === "string" && text.trim()) || "")
    : "";
  // A structured result without a sentence (a Topic Brief…) reads as its title
  // and labelled fields, the way it was reviewed.
  const resultTitle = resultRecord && !resultSummary && typeof resultRecord.title === "string"
    ? resultRecord.title.trim() : "";
  const resultFields = resultRecord && !resultSummary
    ? Object.fromEntries(Object.entries(resultRecord).filter(([key, value]) => key !== "title" && !key.startsWith("_") && !key.endsWith("_id")
      // The run status above already says it needs input or a revision.
      && !(key === "status" && (value === "needs_input" || value === "revision_required"))))
    : null;
  // Short facts read inline; long text, lists and nested data fold away, so the result fits the page.
  const [briefSplit, detailFields] = resultFields ? splitResultFields(resultFields) : [{}, {}];
  const briefFields = flattenResultFields(briefSplit);
  const result = resultSummary || outputText(resultValue);
  // Structured output is for whoever needs it; it stays folded until opened.
  const rawResult = outputText(resultValue);
  const rawIsData = Boolean(resultValue && typeof resultValue === "object") || isJsonText(rawResult);
  const setupCheck = setup.data?.incomplete_checks[0];
  const setupKind = setupCheck?.todo_kind || setupCheck?.kind;
  const needsMaterials = setupKind === "workspace_user_materials" || setupKind === "knowledge_pack_content";
  const automaticSetup = setup.data?.automatic_setup_state;
  const setupTitle = automaticSetup === "failed" ? t("component.workspace_chat.assets_initialization_failed")
    : automaticSetup === "retrying" ? t("component.workspace_chat.assets_initialization_retrying")
    : automaticSetup ? t("component.workspace_chat.assets_initializing") : t("component.workspace_app_view.setup_required");
  const identityMissing = setupKind === "workspace_identity_assets" ? [
    setupCheck?.asset_ready === false && t("component.workspace_app_view.setup_character"),
    setupCheck?.narrator_ready === false && t("component.workspace_app_view.setup_narrator"),
    setupCheck?.manifest_ready === false && t("component.workspace_app_view.setup_shared_assets"),
  ].filter(Boolean) : [];
  const setupQuestion = String(setupCheck?.question || (identityMissing.length
    ? t("component.workspace_app_view.setup_assets_missing", { items: identityMissing.join(", ") })
    : setupCheck?.reason || t("component.workspace_app_view.setup_hint")));
  const setupQuestionParts = setupQuestion.match(/^(.+?[.!?。！？])\s+(.+)$/s);
  const setupLead = setupQuestionParts?.[1] || setupQuestion;
  const setupDetails = setupQuestionParts?.[2] || "";
  const setupHref = `/workspaces/${encodeURIComponent(workspaceId)}?tab=${needsMaterials ? "documents" : "settings"}${needsMaterials ? "" : "&setup=1"}`;
  const setupFailure = start.error instanceof Error && start.error.message.includes("Workspace setup is incomplete");
  return <div className="workspace-app-flow">
    <button type="button" className="workspace-app-flow__back" onClick={onBack}><IconArrowLeft size={16} />{t("component.workspace_app_view.all_actions")}</button>
    <header className="workspace-app-flow__header">
      <span className="workspace-app-flow__mark" aria-hidden="true"><ActionIcon size={20} /></span>
      <h2>{action.title}</h2>
      {action.description && <p className="workspace-app-flow__intro">{action.description}</p>}
    </header>

    {!runId && (setup.isPending || setup.isError || !setup.data?.ready) ? <>
      <div className="workspace-app-flow__panel" role={setup.isError ? "alert" : "status"}>
        <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.before_start")}</span>
        <h3>{setup.isPending ? t("component.workspace_app_view.checking_setup") : setup.isError ? t("component.workspace_app_view.setup_check_failed") : needsMaterials ? t("component.workspace_app_view.add_materials_title") : setupTitle}</h3>
        {!setup.isPending && !setup.isError && <p>{setupLead}</p>}
        {!setup.isPending && !setup.isError && automaticSetup && <p>{t(!setup.data?.automatic_setup_task_id
          ? "component.workspace_app_view.setup_not_started_hint"
          : automaticSetup === "failed" ? "component.workspace_app_view.setup_failed_hint" : "component.workspace_app_view.setup_automatic_hint")}</p>}
        {!setup.isPending && !setup.isError && setupDetails && <details className="workspace-app-setup__details"><summary>{t("component.workspace_app_view.setup_more_details")}</summary><p>{setupDetails}</p></details>}
        {!setup.isPending && !setup.isError && setupKind === "workspace_user_materials" && setupCheck?.key && <WorkspaceAppSetupMaterials key={setupCheck.key} workspaceId={workspaceId} checkKey={setupCheck.key} acceptsText={setupCheck.accepts_text === true} canStart={canStart} />}
        {!setup.isPending && !setup.isError && needsMaterials && setupKind !== "workspace_user_materials" && <p>{t("component.workspace_app_view.materials_in_knowledge")}</p>}
        <div className="workspace-app-flow__buttons">
          {!setup.isPending && <Button variant="ghost" onClick={() => void setup.refetch()}>{t("component.workspace_app_view.check_again")}</Button>}
          {!setup.isPending && !setup.isError && setup.data?.automatic_setup_task_id && <Link className="workspace-app-flow__details" to={`/tasks/${encodeURIComponent(setup.data.automatic_setup_task_id)}`}>{t("component.workspace_app_view.view_setup_task")} <IconArrowRight size={15} /></Link>}
          {/* Setup that never started, or stopped, is started again here; Settings has nothing to act on. */}
          {!setup.isPending && !setup.isError && automaticSetup && (!setup.data?.automatic_setup_task_id || automaticSetup === "failed") && <Button variant="primary" loading={startSetup.isPending} disabled={!canStart} onClick={() => startSetup.mutate()}>{t(setup.data?.automatic_setup_task_id ? "component.workspace_app_view.retry_setup" : "component.workspace_app_view.start_setup")}</Button>}
          {!setup.isPending && !setup.isError && !automaticSetup && !setup.data?.automatic_setup_task_id && setupKind !== "workspace_user_materials" && <Link className="workspace-app-flow__details" to={setupHref}>{t(needsMaterials ? "component.workspace_app_view.add_materials" : "component.workspace_app_view.open_setup")} <IconArrowRight size={15} /></Link>}
        </div>
      </div>
    </> : !runId ? <>
      {chatAvailable && step === 0 && <Button variant="ghost" size="sm" onClick={onUseChat} disabled={!canStart}>
        <IconChatBubble size={14} aria-hidden="true" />{t("component.workspace_app_view.focus_chat")}
      </Button>}
      <div className="workspace-app-flow__steps" aria-label={t("component.workspace_app_view.progress")}>
        <span className={step < pages.length ? "is-current" : "is-complete"}>{t("component.workspace_app_view.step_input")}</span>
        <span className={step === pages.length ? "is-current" : ""}>{t("component.workspace_app_view.step_review")}</span>
        <span>{t("component.workspace_app_view.step_result")}</span>
      </div>
      {currentPage && currentSchema ? <div className="workspace-app-flow__panel" key={currentPage.id}>
        {(currentPage.title || pages.length > 1) && <span className="workspace-app-flow__kicker">
          {[currentPage.title, pages.length > 1 ? `${step + 1} / ${pages.length}` : ""].filter(Boolean).join(" · ")}
        </span>}
        <WorkflowSchemaFields
          rootKey="inputs" schema={currentSchema} value={values} errors={errors}
          workspaceId={workspaceId} workflowId={action.workflow_id} disabled={!canStart}
          onFileBusyChange={setFileBusy}
          onChange={(path, value) => {
            setValues((previous: unknown) => setWorkflowValueAtPath(previous, path, value));
            setErrors((previous) => ({ ...previous, [workflowInputErrorPath("inputs", path)]: "" }));
          }}
        />
        <div className="workspace-app-flow__buttons">
          {step > 0 && <Button variant="ghost" onClick={() => setStep(step - 1)}>{t("component.workspace_app_view.back")}</Button>}
          <Button variant="primary" onClick={next} disabled={!canStart || fileBusy}>{t("component.workspace_app_view.next")} <IconArrowRight size={15} /></Button>
        </div>
      </div> : <div className="workspace-app-flow__panel">
        <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.step_review")}</span>
        <h3>{t("component.workspace_app_view.ready_to_run")}</h3>
        {inputs.length > 0 && <dl className="workspace-app-flow__review">{inputs.map((input) => {
          const value = reviewValue((values as Record<string, unknown>)?.[input.key], input.schema as WorkflowInputSchema | undefined);
          const control = input.schema?.["x-ui"]?.control;
          const display = control === "knowledge_file" ? workflowFileName(value)
            : control === "workflow_project_select" && value
              ? <ReviewProjectName workspaceId={workspaceId} schema={input.schema as WorkflowInputSchema | undefined} projectId={value} />
              : value;
          return <div key={input.key}><dt>{input.label}</dt><dd>{display || "—"}</dd></div>;
        })}</dl>}
        {start.error && <p role="alert" className="workspace-app-flow__error">{setupFailure ? t("component.workspace_app_view.setup_required")
          : start.error instanceof Error && !(start.error instanceof TypeError) ? start.error.message : t("component.workspace_app_view.start_error")}</p>}
        <div className="workspace-app-flow__buttons">
          {pages.length > 0 && <Button variant="ghost" onClick={() => setStep(pages.length - 1)} disabled={start.isPending}>{t("component.workspace_app_view.back")}</Button>}
          <Button variant="primary" onClick={() => start.mutate()} disabled={!canStart || !action.binding_id || start.isPending} loading={start.isPending}>{t("component.workspace_app_view.run")}</Button>
        </div>
      </div>}
    </> : <div ref={resultPanelRef} className={`workspace-app-flow__panel${shownStatus === "paused" ? " workspace-app-flow__panel--attention" : ""}`} role="status">
      <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.step_result")}</span>
      <h3 className={isRunning ? "workspace-app-flow__running-heading" : undefined}>
        {isRunning && <span className="workspace-app-flow__running-indicator" aria-hidden="true" />}
        {shownStatus === "paused" && <span className="workspace-app-flow__attention-dot" aria-hidden="true" />}
        {runMismatch ? t("component.workspace_app_view.run_mismatch") : runUnavailable ? t("component.workspace_app_view.status_unavailable") : run.isPending ? t("component.workspace_app_view.loading_run") : backgroundWait ? t("component.workspace_app_view.queued")
          // A run that makes something says so while it works.
          : production && shownStatus === "running" ? t("component.workspace_app_view.generating")
          : runStatusLabel(shownStatus || "pending", visibleRun?.business_outcome)}
      </h3>
      {!runMismatch && visibleRun && <p>{runSourceLabel(visibleRun.trigger_source, visibleRun.binding_id)}{runTime(visibleRun.started_at || visibleRun.created_at) ? ` · ${runTime(visibleRun.started_at || visibleRun.created_at)}` : ""}</p>}
      {visibleRun?.current_step_name && !terminal && <p>{visibleRun.current_step_name}</p>}
      {isRunning && !production && <div
        className={`workspace-app-flow__run-progress${hasRunProgress ? " is-determinate" : ""}`}
        role="progressbar"
        aria-label={t("component.workspace_app_view.progress")}
        aria-valuemin={hasRunProgress ? 0 : undefined}
        aria-valuemax={hasRunProgress ? 100 : undefined}
        aria-valuenow={hasRunProgress ? Math.round(shownPercent) : undefined}
      >
        <span className="workspace-app-flow__run-progress-track" aria-hidden="true"><span style={hasRunProgress ? { width: `${shownPercent}%` } : undefined} /></span>
        {hasRunProgress && <span className="workspace-app-flow__run-progress-count" aria-hidden="true">{Math.floor(shownPercent)}%</span>}
      </div>}
      {!isRunning && !production && visibleRun?.status !== "completed" && hasRunProgress && <p>{Math.floor(shownPercent)}%</p>}
      {visibleRun?.error && <WorkflowErrorText className="workspace-app-flow__error" text={visibleRun.error} />}
      {technicalDetails && <details className="workspace-app-flow__raw">
        <summary>{t("component.workflow_run.technical_details")}</summary>
        <pre className="workspace-app-flow__output">{technicalDetails}</pre>
      </details>}
      {runUnavailable && <p role="alert" className="workspace-app-flow__error">{t("component.workspace_app_view.run_load_error")}</p>}
      {terminal && detail.isError && !detail.data && !run.data && <p role="alert" className="workspace-app-flow__error">{t("component.workspace_app_view.run_load_error")}</p>}
      {runId && !runMismatch && (production
        ? <RunProduction runId={runId} config={production} steps={workflowSteps} currentStepId={visibleRun?.current_step_id || null}
          status={backgroundWait ? "queued" : shownStatus || "pending"} startedAt={visibleRun?.started_at || visibleRun?.created_at} finalResult={finalResult}
          canRevise={run.data?.capabilities?.can_control === true}
          onOpenRun={(id) => { setRunId(id); void recent.refetch(); }} />
        : <RunMediaClips runId={runId} running={!terminal} />)}
      {hasPrimaryFiles && <GeneratedResultFiles files={listedFiles} />}
      {result && <section><h4>{t("component.workspace_app_view.output")}</h4>
        {resultSummary && <p className="workspace-app-flow__summary">{resultSummary}</p>}
        {resultTitle && <p className="workspace-app-flow__result-title">{resultTitle}</p>}
        {Object.keys(briefFields).length > 0 && <div className="workspace-app-flow__fields workspace-app-flow__fields--compact chat-workflow-review">
          <StructuredReviewValue value={briefFields} />
        </div>}
        {Object.keys(detailFields).length > 0 && <details className="workspace-app-flow__raw">
          <summary>{t("component.workspace_app_view.details")}</summary>
          <div className="workspace-app-flow__fields chat-workflow-review">
            <StructuredReviewValue value={detailFields} />
          </div>
        </details>}
        {rawIsData
          ? <details className="workspace-app-flow__raw">
            <summary>{t("component.workflow_review.original_data")}</summary>
            <pre className="workspace-app-flow__output">{rawResult}</pre>
          </details>
          : !resultSummary && <pre className="workspace-app-flow__output">{rawResult}</pre>}
      </section>}
      {!hasPrimaryFiles && files.length > 0 && <section>
        <div className="workspace-app-flow__files-heading"><h4>{t("component.workspace_app_view.files")}</h4><span>{files.length}</span></div>
        <ul id="workspace-app-flow-files" className="workspace-app-flow__files">{(showAllFiles ? files : files.slice(0, 6)).map((file) => {
          const name = file.name || fileNames.data?.[file.id] || t("component.workspace_app_view.file");
          const { extension, Icon } = fileVisual(name);
          return <li key={file.id}><Link to={file.path} title={name}>
            <span className="workspace-app-flow__file-icon" aria-hidden="true"><Icon size={18} /></span>
            <span className="workspace-app-flow__file-copy"><strong>{name}</strong>{extension && <small>{extension}</small>}</span>
            <IconArrowRight size={15} className="workspace-app-flow__file-arrow" aria-hidden="true" />
          </Link></li>;
        })}</ul>
        {files.length > 6 && <button type="button" className="workspace-app-flow__files-toggle" aria-expanded={showAllFiles} aria-controls="workspace-app-flow-files" onClick={() => setShowAllFiles((value) => !value)}>
          {showAllFiles ? t("component.workspace_app_view.show_fewer_files") : t("component.workspace_app_view.show_more_files").replace("{count}", String(files.length - 6))}
          <IconChevronDown size={15} className={showAllFiles ? "is-open" : ""} aria-hidden="true" />
        </button>}
      </section>}
      {workflowSteps.length > 0 && <StepsSection folded={Boolean(production)}>
        <ol className="workspace-app-flow__timeline">{workflowSteps.map((item, index) => {
          // A check that answered no did not pass, a step that could not do its job
          // is blocked, and one that needs the person waits on them, even though
          // all of them finished running.
          const node = { status: item.status as WorkflowRunNode["status"], ...workflowStepOutcome(item as Record<string, unknown>) };
          const { activity } = item.status === "running" ? workflowStepActivity(item as Record<string, unknown>) : {};
          const failedCheck = node.status === "completed" && node.answer === "no";
          const blocked = node.status === "completed" && node.outcome === "blocked";
          const needsPerson = workflowStepNeedsPerson(node);
          return <li key={item.id} className={failedCheck ? "is-check-no" : blocked ? "is-failed" : needsPerson ? "is-paused" : `is-${item.status}`}>
            <span className="workspace-app-flow__timeline-mark" aria-hidden="true">
              {failedCheck || blocked ? "–" : needsPerson ? <IconPause size={12} /> : item.status === "completed" ? <IconCheck size={13} /> : index + 1}
            </span>
            <span>{item.name}</span>
            <small>{t(workflowStepStatusKey(node, !terminal))}</small>
            {activity && <WorkflowStepActivityLine activity={activity} className="workspace-app-flow__activity" />}
          </li>;
        })}</ol>
      </StepsSection>}
      {terminal && !detail.isPending && !detail.isError && !result && files.length === 0 && (visibleRun?.artifact_count || 0) === 0 && !visibleRun?.error && <p>{t("component.workspace_app_view.no_output")}</p>}
      {terminal && !detail.isPending && !detail.isError && (visibleRun?.artifact_count || 0) > 0 && files.length === 0 && <p>{t("component.workspace_app_view.no_viewable_files")}</p>}
      <div className="workspace-app-flow__buttons workspace-app-flow__result-actions">
          {runId && !runMismatch && run.data?.capabilities?.can_control
            && (run.data.status === "failed" || (run.data.status === "cancelled" && (
              (run.data.intervention?.kind === "workflow_retry" && run.data.intervention.options?.includes("retry"))
              || (run.data.intervention?.kind === "workflow_effect_review" && run.data.intervention.options?.some((option) => option === "review" || option === "review_applied"))
            )) || (
              // A finished run that is waiting on the person (e.g. missing
              // material) continues from its own form, not a fresh run.
              run.data.status === "completed"
              && (run.data.business_outcome === "needs_input" || run.data.business_outcome === "revision_required")
              && run.data.intervention?.kind === "workflow_retry" && run.data.intervention.options?.includes("retry")
            )) &&
            <WorkflowRunRetryControl
              reviewRequired={run.data?.intervention?.kind === "workflow_effect_review" && run.data.intervention.options?.some((option) => option === "review" || option === "review_applied")}
              key={runId}
              runId={runId}
              workspaceId={workspaceId}
              bindingId={action.binding_id}
              workflowId={action.workflow_id}
              businessOutcome={run.data.business_outcome}
              disabled={!canStart || runUnavailable}
              size="sm"
              title={run.data.business_outcome === "needs_input" ? t("component.workflow_run.input_guidance") : t("component.workspace_app_view.retry_help")}
              onRetried={(retried) => { setRunId(retried.id); void recent.refetch(); }}
            />}
          {terminal &&
            <Button variant="ghost" size="sm" title={t("component.workspace_app_view.new_run_help")} onClick={() => { setRunId(null); setStep(0); start.reset(); }}>
              {t("component.workspace_app_view.run_again")}
            </Button>
          }
          {(runUnavailable || (terminal && detail.isError && !detail.data)) && <Button variant="ghost" loading={run.isFetching || detail.isFetching} onClick={() => { void run.refetch(); if (terminal) void detail.refetch(); void recent.refetch(); }}>{t("component.workspace_app_view.refresh_status")}</Button>}
          {runId && visibleRun?.status === "paused" && !backgroundWait && !runMismatch && <WorkflowRunResumeControl
            runId={runId}
            onResumed={() => { void run.refetch(); void recent.refetch(); }}
          />}
          {/* A run started here can be stopped here, whether or not Chat shows it. */}
          {runId && !terminal && visibleRun && !runMismatch && run.data?.capabilities?.can_control &&
            <Button variant="ghost" size="sm" onClick={() => setCancelOpen(true)}>{t("component.workflow_run.cancel_confirm_action")}</Button>}
          {!terminal && visibleRun && !runMismatch && onFollowProgress && <Button variant="ghost" size="sm" title={t("component.workspace_app_view.background_progress")} onClick={onFollowProgress}>
            <IconChatBubble size={14} aria-hidden="true" />{t("component.workspace_app_view.follow_in_chat")}
          </Button>}
          {!runMismatch && flowsAccess.enabled && <Link className="workspace-app-flow__details" to={flowHref} state={runLinkState} onClick={rememberOrigin}>{t("component.workspace_app_view.full_run")} <IconArrowRight size={15} /></Link>}
      </div>
      <ConfirmDialog
        open={cancelOpen}
        onClose={() => setCancelOpen(false)}
        onConfirm={() => cancelRun.mutate()}
        title={t("component.workflow_run.cancel_confirm_title")}
        message={t("component.workflow_run.cancel_confirm_message")}
        confirmLabel={t("component.workflow_run.cancel_confirm_action")}
        cancelLabel={t("component.workflow_run.cancel_confirm_keep_running")}
        danger
        loading={cancelRun.isPending}
        closeOnConfirm={false}
        error={cancelRun.error instanceof Error ? cancelRun.error.message : undefined}
      />
    </div>}
    {historyRuns.length > 0 && <details className="workspace-app-flow__history" aria-label={t("component.workspace_app_view.history")}>
      {/* Past runs are there when wanted, folded otherwise: one line says how they went. */}
      <summary>
        <span>{t("component.workspace_app_view.history")}</span>
        <span className="workspace-app-flow__history-count mono">{historyRuns.length}</span>
        <span className="workspace-app-flow__history-tally">
          {Object.entries(historyRuns.reduce<Record<string, number>>((tally, item) => {
            const label = runStatusLabel(item.status, item.business_outcome);
            tally[label] = (tally[label] || 0) + 1;
            return tally;
          }, {})).map(([label, count]) => `${count} ${label}`).join(" · ")}
        </span>
        <IconChevronDown size={14} aria-hidden="true" />
      </summary>
      {historyRuns.map((item) => {
        const progressLabel = historyProgressLabel(item);
        const duration = workflowRunDurationMs({ id: item.id, status: item.status,
          started_at: item.started_at || undefined, completed_at: item.completed_at || undefined,
          updated_at: item.updated_at || undefined });
        const time = runTime(item.started_at || item.created_at);
        // Picking the run already shown still brings it into view; otherwise nothing seems to happen.
        return <button type="button" key={item.id} onClick={() => item.id === runId
          ? resultPanelRef.current?.scrollIntoView({ block: "start", behavior: "smooth" })
          : setRunId(item.id)}
          aria-current={runId === item.id ? "true" : undefined}>
          <span className="workspace-app-flow__history-copy">
            <span className="workspace-app-flow__history-heading"><strong>{action.title}</strong></span>
            <span className="workspace-app-flow__history-meta">
              <span>{runSourceLabel(item.trigger_source, item.binding_id)}</span>
              {time && <time dateTime={item.started_at || item.created_at || undefined}>{time}</time>}
              {duration !== null && <span>{t("component.workflow_run_history.duration")}: {formatWorkflowDuration(duration)}</span>}
              {(item.attempt_number || 1) > 1 && <span>{t("component.workflow_run_history.attempts", { count: item.attempt_number! })}</span>}
              {progressLabel && <span>{progressLabel}</span>}
            </span>
            {item.current_step_name && ["pending", "running", "paused", "failed"].includes(item.status) && <span className="workspace-app-flow__history-step">{t(item.status === "failed"
              ? "component.workflow_run_history.failed_node" : "component.workflow_run_history.current_node")}: {item.current_step_name}</span>}
          </span>
          <span className="workspace-app-flow__history-status" data-status={item.status}>{runStatusLabel(item.status, item.business_outcome)}</span>
          <IconArrowRight size={14} aria-hidden="true" />
        </button>;
      })}
    </details>}
  </div>;
}
