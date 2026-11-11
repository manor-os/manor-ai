import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useStoredQueryData } from "../../lib/appViewSnapshots";
import { api, type WorkspaceAppAction, type WorkspaceAppActionInput, type WorkspaceConnectionRequirement, type WorkspaceSetupStatus } from "../../lib/api";
import { displayReadInterval, setupReadInterval } from "../../lib/readRefreshPolicy";
import { usePageVisible } from "../../lib/usePageVisible";
import { selectedWorkflowRunRefetchInterval } from "../../lib/workspaceLiveRefresh";
import { viewerPathForDocumentId } from "../../lib/fileReferences";
import { isMediaResultFile, isRenderedCopy, partitionGeneratedResultFiles, type ResultFileMetadata } from "../../lib/resultFiles";
import ChatMarkdown from "../ChatMarkdown";
import GeneratedResultFiles from "../GeneratedResultFiles";
import InlineFileReferenceCard from "../InlineFileReferenceCard";
import InlineResultDocument, { isInlineResultDocument } from "../InlineResultDocument";
import RunMediaClips from "../ui/RunMediaClips";
import RunProduction, { type RunProductionConfig } from "../ui/RunProduction";
import { getLocale, t } from "../../lib/i18n";
import { getPreferredTimeZone } from "../../lib/format";
import { workflowRunPath } from "../../lib/workflowRoutes";
import { usePreviewFeatureAccess } from "../../lib/previewFeatureAccess";
import { IconArrowLeft, IconArrowRight, IconChatBubble, IconCheck, IconChevronDown, IconPause } from "../icons";
import { clock } from "../ui/AudioWaveform";
import Button from "../ui/Button";
import ConfirmDialog from "../ui/ConfirmDialog";
import WorkspaceAppSetupMaterials from "./WorkspaceAppSetupMaterials";
import DeliveryFileBrowser from "./DeliveryFileBrowser";
import IntegrationConnectRow from "../integrations/IntegrationConnectRow";
import { workflowFileName } from "../workflows/WorkflowKnowledgeFileField";
import {
  presetPlaceholderTokens,
  workflowSchemaChoices,
  workflowSchemaChosen,
  workflowSchemaOptionLabel,
  workflowSchemaProjectSelect,
  workflowSchemaRecordSelect,
  workflowSchemaControl,
} from "../workflows/workflowSchema";
import { formDraftKey, useFormDraft } from "../../lib/formDraft";
import { requestInputValueText } from "../../lib/requestInputForm";
import FormDraftNotice from "../ui/FormDraftNotice";
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
import { actionIcon, fileVisual, runStatusLabel, workflowProjectResultView } from "./workspaceAppRunDisplay";
import {
  WorkflowSchemaFields,
  parseWorkflowSchemaDraft,
  setWorkflowValueAtPath,
  workflowInputErrorPath,
  workflowSchemaDraft,
  workflowSchemaFieldShown,
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
  trigger_data?: Record<string, unknown> | null;
  step_results?: Record<string, Record<string, unknown>>;
  execution_trace?: Array<Record<string, unknown>>;
  /** The Workflow's declared production and the run's units so far. */
  production?: RunProductionConfig;
};

const WorkspaceAppDecisionDialog = lazy(() => import("./WorkspaceAppDecisionDialog"));

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

function runFiles(run?: Run): Array<{ id: string; name: string; path: string; fileType: string; mimeType: string; isFinal?: boolean; group?: string; role?: string }> {
  if (!run) return [];
  // What the run delivered, as the server decided it for Chat and the App's
  // output shelves: one answer, so the result shows the files its shelf shows.
  const summary = run.trigger_data?._workflow_history_summary;
  const deliverables = summary && typeof summary === "object" ? (summary as Record<string, unknown>).deliverables : undefined;
  // The files the run saved under their sections (a group and role), by document, for the deliverables that lack them.
  const savedSections = new Map<string, { group?: string; role?: string }>();
  const savedFiles = (run.variables?.__result as Record<string, unknown> | undefined)?.files;
  if (Array.isArray(savedFiles)) {
    for (const value of savedFiles) {
      if (!value || typeof value !== "object" || Array.isArray(value)) continue;
      const ref = value as Record<string, unknown>;
      savedSections.set(String(ref.document_id || ""), {
        ...(typeof ref.group === "string" && ref.group ? { group: ref.group } : {}),
        ...(typeof ref.role === "string" && ref.role ? { role: ref.role } : {}),
      });
    }
  }
  if (Array.isArray(deliverables) && deliverables.length > 0) {
    const listed = deliverables.flatMap((value) => {
      if (!value || typeof value !== "object" || Array.isArray(value)) return [];
      const ref = value as Record<string, unknown>;
      const id = String(ref.document_id || "").trim();
      const path = viewerPathForDocumentId(id);
      const section = savedSections.get(id) || {};
      return path ? [{ id, path, name: String(ref.name || ""), fileType: String(ref.file_type || ""), mimeType: String(ref.mime_type || ""), isFinal: true, ...section }] : [];
    });
    if (listed.length > 0) return listed;
  }

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
      // A delivered file keeps the group and role it was saved under (a release's sections).
      return [{ id, path, name: String(ref.name || ref.filename || ""), fileType: String(ref.file_type || ref.format || ""), mimeType: String(ref.mime_type || ""), isFinal: true,
        ...(typeof ref.group === "string" && ref.group ? { group: ref.group } : {}),
        ...(typeof ref.role === "string" && ref.role ? { role: ref.role } : {}) }];
    });
    if (delivered.length > 0) return delivered;
  }
  // A result that lists no files delivered none, as the server reads it:
  // what its steps read on the way (Knowledge) is not its output.
  if (Array.isArray(finalFiles) && finalFiles.length === 0) return [];
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

// The file type a production's declared final plays as, before its file is read.
const PRODUCTION_MIME: Record<NonNullable<RunProductionConfig["media"]>, string> = {
  video: "video/mp4", audio: "audio/mpeg", image: "image/png",
};
/** A setup check that asks for an integration, as the App's connection card shows it; else null. */
function setupConnection(check: WorkspaceSetupStatus["incomplete_checks"][number]): WorkspaceConnectionRequirement | null {
  const integration = check.kind === "integration_provider"
    || (check.kind === "blueprint_install_todo" && check.todo_kind === "missing_integration");
  const provider = typeof check.provider === "string" ? check.provider.trim() : "";
  if (!integration || !provider) return null;
  return {
    key: String(check.key || provider), kind: "integration", provider, label: "", required: true, ready: false,
    reason: String(check.reason || ""), service_keys: [],
    setup_kind: typeof check.setup_kind === "string" ? check.setup_kind : null,
  };
}

const INLINE_OBJECT_FIELDS = 4;

function isEmptyResultValue(value: unknown): boolean {
  return value === "" || value == null || (Array.isArray(value) && value.length === 0)
    || (typeof value === "object" && !Array.isArray(value) && Object.keys(value as object).length === 0);
}

// Fields that say what a result was made from or how sure it is (its Knowledge
// refs, citations, the searches behind it, open gaps): detail, not the result.
const SOURCE_RESULT_FIELD = /(^|_)(refs?|references|citations|sources?|queries|signals?|evidence|gaps|trace|debug|checklist|checks(_passed)?)$/i;
// Whole documents are heavy; past this many a result's other documents stay one press away.
const WHOLE_RESULT_DOCUMENTS = 3;
// Fields that hold the delivered files themselves: the files show above, never as fields.
const FILE_RESULT_FIELD = /^(files|final_files|artifacts|deliverables|assets|outputs|receipt|previews|final_artifacts)$/i;
// A run state word ("accepted", "completed") the run's own status already says.
const STATE_RESULT_VALUE = /^(accepted|approved|completed|complete|done|delivered|ok|success|succeeded|ready)$/i;
// Text past this length reads as a document of its own, not a field.
const DOCUMENT_RESULT_TEXT = 1200;
const INLINE_LIST_ITEMS = 8;

/**
 * Whether a field is the result's detail rather than what it says: its sources,
 * a document-length text, a long list, or a list of records. A thesis, an
 * audience or a handful of key questions are what the result is and stay in view.
 */
function isLongResultValue(value: unknown, key = ""): boolean {
  if (SOURCE_RESULT_FIELD.test(key)) return true;
  if (typeof value === "string") return value.length > DOCUMENT_RESULT_TEXT;
  if (Array.isArray(value)) {
    return value.length > INLINE_LIST_ITEMS || value.some((item) => typeof item === "object" || (typeof item === "string" && item.length > 400));
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
    if (isEmptyResultValue(value) || FILE_RESULT_FIELD.test(key)) continue;
    if (/(^|_)status$/i.test(key) && typeof value === "string" && STATE_RESULT_VALUE.test(value.trim())) continue;
    if (value && typeof value === "object" && !Array.isArray(value)) {
      const [inner, folded] = splitResultFields(value as Record<string, unknown>);
      if (Object.keys(inner).length > INLINE_OBJECT_FIELDS) {
        detail[key] = value;
      } else {
        if (Object.keys(inner).length) brief[key] = inner;
        if (Object.keys(folded).length) detail[key] = folded;
      }
    } else if (isLongResultValue(value, key)) {
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
  // A choice reads as the option the person picked, not its stored value, a yes/no choice included.
  if (schema?.enum?.some((option) => String(option) === String(value))) {
    return workflowSchemaOptionLabel(schema, value, String);
  }
  // A switch reads as Yes / No, not the stored true / false.
  if (typeof value === "boolean") {
    return t(value ? "component.workspace_app_view.check_yes" : "component.workspace_app_view.check_no");
  }
  // A part of a song reads as "the AI picks" or its times, not the stored object.
  if (schema?.["x-ui"]?.control === "audio_clip" && value && typeof value === "object") {
    const clip = value as { auto?: unknown; start_seconds?: unknown; end_seconds?: unknown };
    if (clip.auto === true) return t("component.workflow_audio_clip.mode_auto");
    const start = Number(clip.start_seconds);
    const end = Number(clip.end_seconds);
    if (clip.start_seconds !== "" && clip.end_seconds !== "" && Number.isFinite(start) && Number.isFinite(end)) {
      return `${clock(start)}–${clock(end)}`;
    }
  }
  if (schema?.items && workflowSchemaChoices(schema)) {
    return [...workflowSchemaChosen(value)]
      .map((option) => workflowSchemaOptionLabel(schema.items!, option, String))
      .join(", ");
  }
  if (workflowSchemaRecordSelect(schema)) return [...workflowSchemaChosen(value)].join(", ");
  // Controls with a shape of their own (a date range, tags, rows, a signature) read as their values.
  const control = workflowSchemaControl(schema);
  if (control === "date_range" || control === "tags" || control === "line_items" || control === "signature") {
    return requestInputValueText(control === "tags" ? [...workflowSchemaChosen(value)] : value,
      t("component.workspace_app_view.check_yes"), t("component.workspace_app_view.check_no"));
  }
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

/** Picked projects read as their titles, the way the picker showed them, not their ids. */
function ReviewProjectName({ workspaceId, schema, projectIds }: { workspaceId: string; schema?: WorkflowInputSchema; projectIds: string[] }) {
  const ui = schema?.["x-ui"] || {};
  const projectType = String(ui.project_type || "");
  const stages = ui.allowed_stages || [];
  const multiple = Boolean(workflowSchemaProjectSelect(schema)?.multiple);
  const visible = usePageVisible();
  const candidates = useQuery({
    queryKey: ["workflow-project-candidates", workspaceId, projectType, stages.join(","), multiple],
    queryFn: () => api.workflows.listProjectCandidates({
      workspace_id: workspaceId, project_type: projectType, stages, ...(multiple ? { limit: 500 } : {}),
    }),
    enabled: Boolean(workspaceId && projectType) && visible,
    staleTime: 60_000,
  });
  const titles = new Map((candidates.data || []).map((candidate: { id: string; title?: string }) => [candidate.id, candidate.title]));
  return <>{projectIds.map((id) => titles.get(id) || id).join(", ")}</>;
}

/** The run's steps; behind a production view or once it completed they are the detail, folded until opened. A run that stopped keeps them open to show where. */
function StepsSection({ folded, children }: { folded: boolean; children: React.ReactNode }) {
  return folded
    ? <details className="workspace-app-flow__raw"><summary>{t("component.workspace_app_view.run_steps")}</summary>{children}</details>
    : <section><h4>{t("component.workspace_app_view.run_steps")}</h4>{children}</section>;
}

// Where a result keeps the piece it wrote (an article, a script), in words long enough to read as one.
const RESULT_BODY_KEYS = ["markdown", "body_markdown", "article_markdown", "body", "content"] as const;
const RESULT_BODY_MIN_LENGTH = 280;

/** The piece a result wrote, shown whole instead of only inside its raw data. */
function resultBody(record: Record<string, unknown> | null): string {
  if (!record) return "";
  const text = RESULT_BODY_KEYS.map((key) => record[key]).find((value) => typeof value === "string" && value.trim().length >= RESULT_BODY_MIN_LENGTH);
  return typeof text === "string" ? text.trim() : "";
}

/** Where a run came from; empty for the usual case, a person starting it from this App. */
function runSourceLabel(source?: string | null, bindingId?: string | null): string {
  if (source === "manual" && !bindingId) return t("component.workspace_app_view.source_direct_workflow");
  if (source === "workspace_chat") return t("component.workspace_app_view.source_chat");
  if (source === "schedule" || source === "scheduled") return t("component.workspace_app_view.source_schedule");
  if (source === "event" || source === "workspace_event") return t("component.workspace_app_view.source_automation");
  return "";
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
  initialValues?: Record<string, unknown>;
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
  const visible = usePageVisible();
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
  // An unsent run form survives a reload; one opened with chosen inputs (a Remake) starts from those instead.
  const draft = useFormDraft<unknown>(
    initialValues && Object.keys(initialValues).length ? null : formDraftKey("app-action", workspaceId, action.binding_id || action.key),
    () => workflowSchemaDraft(schema, initialValues || {}),
  );
  const { values, setValues } = draft;
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
    enabled: Boolean(action.binding_id && action.workflow_id) && visible,
    retry: false,
    refetchInterval: (query) => displayReadInterval(query.state, 60_000),
    refetchOnWindowFocus: "always",
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
    queryKey: ["workspace-setup-status", workspaceId, action.workflow_id],
    queryFn: ({ signal }) => api.workspaces.setupStatus(workspaceId, signal, action.workflow_id || undefined),
    enabled: Boolean(workspaceId && !runId) && visible,
    staleTime: 0,
    retry: false,
    refetchInterval: (query) => displayReadInterval(
      query.state,
      setupReadInterval(query.state.data),
    ),
    refetchOnWindowFocus: "always",
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
  // A run opened before shows what it last showed at once, then refreshes.
  const storedRun = useStoredQueryData<Run>(["workspace-app-run", runId], Boolean(runId));
  const run = useQuery({
    ...storedRun.initial,
    queryKey: ["workspace-app-run", runId],
    queryFn: ({ signal }) => api.workflows.getRun(runId!, false, AbortSignal.any([signal, AbortSignal.timeout(15_000)])) as Promise<Run>,
    enabled: Boolean(runId) && visible,
    // A failed poll keeps the last state; the next scheduled read heals it.
    retry: false,
    refetchInterval: (query) => {
      const status = String(query.state.data?.status || "");
      return displayReadInterval(query.state, selectedWorkflowRunRefetchInterval(status));
    },
    refetchOnWindowFocus: "always",
  });
  storedRun.useSave(run.data, run.dataUpdatedAt);
  const runUnavailable = run.isError && !run.data;
  const terminal = ["completed", "failed", "cancelled"].includes(run.data?.status || "");
  const runMismatch = Boolean(run.data && !isActionRun(run.data));
  useEffect(() => {
    if (!runId || !terminal || runMismatch) return;
    // Completed actions can change both the selected object's data and the
    // collection containing it. Returning must not briefly resurrect a deletion.
    for (const key of ["workspace-app-data", "workspace-app-data-contexts", "workspace-app-section"]) {
      void queryClient.invalidateQueries({ queryKey: [key, workspaceId], refetchType: "all" });
    }
  }, [runId, terminal, runMismatch, workspaceId, queryClient]);
  const storedDetail = useStoredQueryData<Run>(["workspace-app-run-detail", runId, run.data?.status], Boolean(runId && terminal && !runMismatch));
  const detail = useQuery({
    ...storedDetail.initial,
    queryKey: ["workspace-app-run-detail", runId, run.data?.status],
    queryFn: ({ signal }) => api.workflows.getRun(runId!, true, AbortSignal.any([signal, AbortSignal.timeout(15_000)])) as Promise<Run>,
    enabled: Boolean(runId && terminal && !runMismatch) && visible,
    retry: 2,
  });
  storedDetail.useSave(detail.data, detail.dataUpdatedAt);
  const visibleRun = runMismatch ? undefined : detail.data || run.data;
  // A run started from the App answers its decisions on chat cards. Its panel offers the card
  // that waits for this run, or for a sub-flow it runs (a music video inside a campaign).
  const pausedHere = Boolean(runId) && visibleRun?.status === "paused";
  const pendingDecisions = useQuery({
    queryKey: ["workspace-pending-attention", workspaceId],
    queryFn: ({ signal }) => api.workspaces.chat.listPendingAttention(workspaceId, signal),
    enabled: pausedHere && visible,
    refetchInterval: pausedHere ? 15_000 : false,
  });
  const waitingDecision = pausedHere
    ? pendingDecisions.data?.items.find((item) => item.workflow_run_id === runId || item.workflow_host_run_id === runId)
    : undefined;
  const [decisionOpen, setDecisionOpen] = useState(false);
  const technicalDetails = workflowRunTechnicalDetails(visibleRun, t("component.workflow_run.error_truncated"));
  const files = runFiles(runMismatch ? undefined : detail.data);
  const unnamedFileIds = files.filter((file) => !file.name).map((file) => file.id);
  const fileNames = useQuery({
    queryKey: ["workspace-app-run-file-names", runId, unnamedFileIds],
    queryFn: async () => Object.fromEntries(await Promise.all(unnamedFileIds.map(async (id) => {
      try { return [id, (await api.documents.get(id)).name] as const; }
      catch { return [id, ""] as const; }
    }))),
    enabled: terminal && unnamedFileIds.length > 0 && visible,
    staleTime: 5 * 60 * 1000,
  });
  const workflowSteps = runMismatch ? [] : run.data?.workflow_steps || [];
  const displayFiles = files.map((file) => ({ ...file, key: file.id, reference: file.path,
    name: file.name || fileNames.data?.[file.id] || t("component.workspace_app_view.file") }));
  // A run paused only for background work (an automatic retry, a render) is
  // still running; only a wait for a person needs attention, including one in a sub-flow.
  const backgroundWait = runMismatch || waitingDecision ? null : workflowRunBackgroundWait(visibleRun);
  const shownStatus = backgroundWait ? "running" : visibleRun?.status;
  // Done and asking nothing more of the person: the result is the whole story.
  const settledRun = shownStatus === "completed" && !/needs_input|revision_required/.test(visibleRun?.business_outcome || "");
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
  // The result's own documents read or play here in full (a deck, a PDF, a web
  // page, the text, a recording); pictures and video keep their result cards.
  const primaryFiles = partitionGeneratedResultFiles(listedFiles).primary;
  // A delivery that names its sections (a release's groups) browses as one list instead of stacked documents.
  const browsedFiles = listedFiles.filter((file) => file.group);
  const wholeFiles = primaryFiles.filter((file) => !browsedFiles.includes(file) && isInlineResultDocument(file) && !isRenderedCopy(file, primaryFiles)).slice(0, WHOLE_RESULT_DOCUMENTS);
  const cardFiles = listedFiles.filter((file) => !wholeFiles.includes(file) && !browsedFiles.includes(file));
  const hasPrimaryFiles = primaryFiles.length > 0;
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
      draft.clear();
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
  const storedResult = detail.data?.variables?.__result;
  const resultValue = workflowProjectResultView(storedResult) ?? storedResult;
  // Whatever the result says in words is shown; the data behind it stays folded.
  const resultRecord = resultValue && typeof resultValue === "object" && !Array.isArray(resultValue)
    ? resultValue as Record<string, unknown> : null;
  // The declared completion message explains the business result even when
  // the final tool returned only a cleanup count.
  const endMessage = detail.data?.status === "completed" ? detail.data.step_results?.[detail.data.current_step_id || ""]?.completion_message : null;
  const completionSummary = typeof endMessage === "string" ? endMessage.trim() : "";
  const resultSummary = (resultRecord
    ? String([resultRecord.summary, resultRecord.reason, resultRecord.message].find((text) => typeof text === "string" && text.trim()) || "")
    : "") || completionSummary;
  // A structured result without a sentence (a Topic Brief…) reads as its title
  // and labelled fields, the way it was reviewed.
  const resultTitle = resultRecord && !resultSummary && typeof resultRecord.title === "string"
    ? resultRecord.title.trim() : "";
  const body = resultBody(resultRecord);
  const bodyTitle = body && typeof resultRecord?.title === "string" ? resultRecord.title.trim() : "";
  const resultFields = resultRecord && !resultSummary
    ? Object.fromEntries(Object.entries(resultRecord).filter(([key, value]) => key !== "title" && !key.startsWith("_") && !key.endsWith("_id")
      // The run status above already says it needs input or a revision.
      && !(key === "status" && (value === "needs_input" || value === "revision_required"))))
    : null;
  // Short facts read inline; long text, lists and nested data fold away, so the result fits the page.
  const [briefSplit, detailFields] = resultFields ? splitResultFields(resultFields) : [{}, {}];
  const briefFields = flattenResultFields(briefSplit);
  // A brief that reads as text (a thesis, a few questions) stacks; short facts sit side by side.
  const briefReads = Object.values(briefFields).some((value) => Array.isArray(value) || (typeof value === "string" && value.length > 160));
  const result = resultSummary || outputText(resultValue);
  // Structured output is for whoever needs it; it stays folded until opened.
  const rawResult = outputText(storedResult);
  const rawIsData = Boolean(storedResult && typeof storedResult === "object") || isJsonText(rawResult);
  // Integrations it needs are connected right here, with the same card the App shows.
  const setupConnections = (setup.data?.incomplete_checks || []).map(setupConnection)
    .filter((item): item is WorkspaceConnectionRequirement => Boolean(item));
  const onlyConnections = setupConnections.length > 0
    && setupConnections.length === (setup.data?.incomplete_checks || []).length;
  const setupCheck = setup.data?.incomplete_checks.find((check) => !setupConnection(check))
    || setup.data?.incomplete_checks[0];
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
        <h3>{setup.isPending ? t("component.workspace_app_view.checking_setup") : setup.isError ? t("component.workspace_app_view.setup_check_failed") : onlyConnections ? t("component.workspace_app_view.connect_to_start") : needsMaterials ? t("component.workspace_app_view.add_materials_title") : setupTitle}</h3>
        {!setup.isPending && !setup.isError && !onlyConnections && <p>{setupLead}</p>}
        {!setup.isPending && !setup.isError && setupConnections.length > 0 && <ul className="integration-connect-list">
          {setupConnections.map((requirement) => <IntegrationConnectRow key={requirement.key} workspaceId={workspaceId}
            requirement={requirement} onConnected={() => void setup.refetch()} />)}
        </ul>}
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
          {!setup.isPending && !setup.isError && !automaticSetup && !setup.data?.automatic_setup_task_id && setupKind !== "workspace_user_materials" && !onlyConnections && <Link className="workspace-app-flow__details" to={setupHref}>{t(needsMaterials ? "component.workspace_app_view.add_materials" : "component.workspace_app_view.open_setup")} <IconArrowRight size={15} /></Link>}
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
        {draft.restored && step === 0 && <FormDraftNotice onDiscard={draft.reset} disabled={!canStart} />}
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
        {inputs.length > 0 && <dl className="workspace-app-flow__review">{inputs
          // Settings left to the AI are not asked, so they are not listed either.
          .filter((input) => workflowSchemaFieldShown(input.schema as WorkflowInputSchema | undefined, values))
          .map((input) => {
          const value = reviewValue((values as Record<string, unknown>)?.[input.key], input.schema as WorkflowInputSchema | undefined);
          const control = input.schema?.["x-ui"]?.control;
          const display = control === "knowledge_file" ? workflowFileName(value)
            : control === "workflow_project_select" && value
              ? <ReviewProjectName workspaceId={workspaceId} schema={input.schema as WorkflowInputSchema | undefined}
                projectIds={[...workflowSchemaChosen((values as Record<string, unknown>)?.[input.key])]} />
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
      {/* A finished result needs no "Result" or "Completed" over it; a state still going on or waiting does. */}
      {!settledRun && <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.step_result")}</span>}
      {!settledRun && <h3 className={isRunning ? "workspace-app-flow__running-heading" : undefined}>
        {isRunning && <span className="workspace-app-flow__running-indicator" aria-hidden="true" />}
        {shownStatus === "paused" && <span className="workspace-app-flow__attention-dot" aria-hidden="true" />}
        {runMismatch ? t("component.workspace_app_view.run_mismatch") : runUnavailable ? t("component.workspace_app_view.status_unavailable") : run.isPending ? t("component.workspace_app_view.loading_run") : backgroundWait ? t("component.workspace_app_view.queued")
          // A run that makes something says so while it works.
          : production && shownStatus === "running" ? t("component.workspace_app_view.generating")
          : runStatusLabel(shownStatus || "pending", visibleRun?.business_outcome)}
      </h3>}
      {!runMismatch && visibleRun && <p>{[runSourceLabel(visibleRun.trigger_source, visibleRun.binding_id), runTime(visibleRun.started_at || visibleRun.created_at)].filter(Boolean).join(" · ")}</p>}
      {/* What waits for the person says so; otherwise the step the run is on. */}
      {(waitingDecision?.summary || visibleRun?.current_step_name) && !terminal && <p>{waitingDecision?.summary || visibleRun?.current_step_name}</p>}
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
      {browsedFiles.length > 0 && <DeliveryFileBrowser files={browsedFiles} />}
      {wholeFiles.map((file) => <div key={file.key} className="video-result-files__document">
        <InlineResultDocument documentId={file.id} name={file.name} mimeType={file.mimeType} fileType={file.fileType} />
        <InlineFileReferenceCard reference={file.reference} label={file.name} fileType={file.fileType} mimeType={file.mimeType} compact trustedReference />
      </div>)}
      {hasPrimaryFiles && partitionGeneratedResultFiles(cardFiles).primary.length > 0 && <GeneratedResultFiles files={cardFiles} />}
      {result && <section>
        {resultSummary && <p className="workspace-app-flow__summary">{resultSummary}</p>}
        {resultTitle && <p className="workspace-app-flow__result-title">{resultTitle}</p>}
        {body && <article className="workspace-app-flow__article">
          {bodyTitle && <h2 className="workspace-app-flow__article-title">{bodyTitle}</h2>}
          <ChatMarkdown content={body} />
        </article>}
        {Object.keys(briefFields).length > 0 && <div className={`workspace-app-flow__fields workspace-app-flow__fields--compact${briefReads ? " workspace-app-flow__fields--reads" : ""} chat-workflow-review`}>
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
      {workflowSteps.length > 0 && <StepsSection folded={Boolean(production) || run.data?.status === "completed"}>
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
          {runId && visibleRun?.status === "paused" && !backgroundWait && !runMismatch && (waitingDecision
            ? <Button variant="primary" size="sm" onClick={() => setDecisionOpen(true)}>{t("component.workspace_app_view.queue_review")}</Button>
            : <WorkflowRunResumeControl
              runId={runId}
              onResumed={() => { void run.refetch(); void recent.refetch(); }}
            />)}
          {decisionOpen && waitingDecision && <Suspense fallback={null}>
            <WorkspaceAppDecisionDialog workspaceId={workspaceId} messageId={waitingDecision.message_id}
              title={waitingDecision.summary || t("component.workspace_app_view.queue_untitled")}
              onClose={() => { setDecisionOpen(false); void run.refetch(); void pendingDecisions.refetch(); void recent.refetch(); }} />
          </Suspense>}
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
              {runSourceLabel(item.trigger_source, item.binding_id) && <span>{runSourceLabel(item.trigger_source, item.binding_id)}</span>}
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
