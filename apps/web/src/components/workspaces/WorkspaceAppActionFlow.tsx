import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, type WorkspaceAppAction, type WorkspaceAppActionInput } from "../../lib/api";
import { viewerPathForDocumentId } from "../../lib/fileReferences";
import { partitionGeneratedResultFiles } from "../../lib/resultFiles";
import GeneratedResultFiles from "../GeneratedResultFiles";
import { getLocale, t } from "../../lib/i18n";
import { getPreferredTimeZone } from "../../lib/format";
import { workflowRunPath } from "../../lib/workflowRoutes";
import { IconArrowLeft, IconArrowRight, IconAudioWave, IconChatBubble, IconCheck, IconChevronDown, IconCode, IconDocument, IconFlow, IconImage } from "../icons";
import Button from "../ui/Button";
import WorkspaceAppSetupMaterials from "./WorkspaceAppSetupMaterials";
import { workflowFileName } from "../workflows/WorkflowKnowledgeFileField";
import WorkflowRunResumeControl from "../workflows/WorkflowRunResumeControl";
import WorkflowRunRetryControl from "../workflows/WorkflowRunRetryControl";
import { formatWorkflowDuration, workflowRunDurationMs, workflowRunStatusPresentation, type WorkflowRunAction, type WorkflowRunView } from "../workflows/workflowRunDisplay";
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
  current_step_name?: string | null;
  processed_count?: number | null;
  total_count?: number | null;
  artifact_count?: number | null;
  workflow_steps?: Array<{ id: string; name: string; status: string }> | null;
  variables?: Record<string, unknown>;
  step_results?: Record<string, Record<string, unknown>>;
  execution_trace?: Array<Record<string, unknown>>;
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
  const seen = new Set<string>();
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

function fileVisual(name: string) {
  const extension = name.match(/\.([^.]+)$/)?.[1]?.toLowerCase() || "";
  let Icon = IconDocument;
  if (/^(png|jpe?g|gif|webp|svg|heic)$/.test(extension)) Icon = IconImage;
  else if (/^(mp3|wav|flac|m4a|ogg|aac|aiff)$/.test(extension)) Icon = IconAudioWave;
  else if (/^(json|csv|tsv|xml|yaml|yml)$/.test(extension)) Icon = IconCode;
  return { extension: extension.toUpperCase(), Icon };
}

function outputText(value: unknown): string {
  if (value === undefined || value === null || value === "") return "";
  if (typeof value === "string") return value;
  try { return JSON.stringify(value, null, 2); } catch { return ""; }
}

function reviewValue(value: unknown): string {
  const text = outputText(value);
  if (!text.startsWith("[")) return text;
  try {
    const refs = JSON.parse(text) as unknown;
    if (Array.isArray(refs) && refs.length && refs.every((ref) =>
      ref && typeof ref === "object" && "document_id" in ref && "name" in ref && typeof ref.name === "string",
    )) return refs.map((ref) => ref.name).join(", ");
  } catch { /* Keep ordinary text as entered. */ }
  return text;
}

function runStatusLabel(status: string, businessOutcome?: string): string {
  const presentation = workflowRunStatusPresentation({ status: status as WorkflowRunView["status"], businessOutcome });
  if (presentation.labelKey !== status) return t(`component.workflow_run.status.${presentation.labelKey}`);
  if (status === "completed") return t("component.workspace_app_view.completed");
  if (status === "failed") return t("component.workspace_app_view.failed");
  if (status === "cancelled") return t("component.workspace_app_view.cancelled");
  if (status === "paused") return t("component.workspace_app_view.needs_attention");
  if (status === "skipped") return t("component.workspace_app_view.skipped");
  if (status === "pending") return t("component.workspace_app_view.waiting");
  return t("component.workspace_app_view.running");
}

function runSourceLabel(source?: string | null): string {
  if (source === "workspace_chat") return t("component.workspace_app_view.source_chat");
  if (source === "schedule" || source === "scheduled") return t("component.workspace_app_view.source_schedule");
  if (source === "event" || source === "workspace_event") return t("component.workspace_app_view.source_automation");
  return t("component.workspace_app_view.source_manual");
}

function runTime(value?: string | null): string {
  const date = value ? new Date(value) : null;
  return date && !Number.isNaN(date.getTime())
    ? date.toLocaleString(getLocale(), { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZone: getPreferredTimeZone() })
    : "";
}

export default function WorkspaceAppActionFlow({
  action, workspaceId, canStart, chatAvailable, onUseChat, onBack, onFollowProgress,
}: {
  action: WorkspaceAppAction;
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
  const restoredApp = location.state?.workspaceApp;
  const inputs = useMemo(() => ((action.inputs || []) as WorkspaceAppActionInput[]).filter((input) => input.key && !input.hidden), [action]);
  const schema = useMemo<WorkflowInputSchema>(() => ({
    type: "object",
    properties: Object.fromEntries(((action.inputs || []) as WorkspaceAppActionInput[]).filter((input) => input.key).map((input) => [input.key, fieldSchema(input)])),
    required: inputs.filter((input) => input.required).map((input) => input.key),
  }), [action, inputs]);
  const [values, setValues] = useState<unknown>(() => workflowSchemaDraft(schema, {}));
  const [step, setStep] = useState(0);
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [runId, setRunId] = useState<string | null>(() => restoredApp?.workspaceId === workspaceId
    && restoredApp?.actionKey === action.key ? restoredApp.runId || null : null);
  const [showAllFiles, setShowAllFiles] = useState(false);
  const [fileBusy, setFileBusy] = useState(false);
  const resultPanelRef = useRef<HTMLDivElement | null>(null);
  const initialRecentRunId = useRef<string | null | undefined>(undefined);
  const currentInput = inputs[step];
  const currentSchema: WorkflowInputSchema | null = currentInput ? {
    type: "object",
    properties: { [currentInput.key]: fieldSchema(currentInput) },
    required: currentInput.required ? [currentInput.key] : [],
  } : null;

  const recent = useQuery({
    queryKey: ["workspace-app-recent-run", workspaceId, action.binding_id],
    queryFn: ({ signal }) => api.workflows.listRuns({ workspace_id: workspaceId, binding_id: action.binding_id, limit: 5 }, AbortSignal.any([signal, AbortSignal.timeout(15_000)])) as Promise<Run[]>,
    enabled: Boolean(action.binding_id),
    retry: false,
    refetchInterval: (query) => query.state.status === "error" ? 30_000 : 3000,
  });
  const historyRuns = (recent.data || []).filter((item) =>
    item.binding_id === action.binding_id && item.workspace_id === workspaceId && item.workflow_id === action.workflow_id,
  );
  const setup = useQuery({
    queryKey: ["workspace-setup-status", workspaceId],
    queryFn: () => api.workspaces.setupStatus(workspaceId),
    enabled: Boolean(workspaceId && !runId),
    staleTime: 0,
    refetchInterval: (query) => query.state.data?.automatic_setup_state && !query.state.data.ready ? 5000 : false,
  });
  useEffect(() => {
    if (recent.isPending) return;
    const latestId = recent.data?.find((item) => item.binding_id === action.binding_id
      && item.workspace_id === workspaceId && item.workflow_id === action.workflow_id)?.id || null;
    if (initialRecentRunId.current === undefined) initialRecentRunId.current = latestId;
    else if (latestId && latestId !== initialRecentRunId.current) {
      initialRecentRunId.current = latestId;
      if (!runId) setRunId(latestId);
    }
  }, [recent.isPending, recent.data, runId, action.binding_id, action.workflow_id, workspaceId]);
  const run = useQuery({
    queryKey: ["workspace-app-run", runId],
    queryFn: ({ signal }) => api.workflows.getRun(runId!, false, AbortSignal.any([signal, AbortSignal.timeout(15_000)])) as Promise<Run>,
    enabled: Boolean(runId),
    retry: false,
    refetchInterval: (query) => ["completed", "failed", "cancelled"].includes(String(query.state.data?.status || "")) ? false : query.state.status === "error" ? 30_000 : 3000,
  });
  const terminal = ["completed", "failed", "cancelled"].includes(run.data?.status || "");
  const runMismatch = Boolean(run.data && (
    run.data.binding_id !== action.binding_id
    || run.data.workflow_id !== action.workflow_id
    || run.data.workspace_id !== workspaceId
  ));
  const detail = useQuery({
    queryKey: ["workspace-app-run-detail", runId, run.data?.status],
    queryFn: ({ signal }) => api.workflows.getRun(runId!, true, AbortSignal.any([signal, AbortSignal.timeout(15_000)])) as Promise<Run>,
    enabled: Boolean(runId && terminal && !runMismatch),
    retry: false,
  });
  const visibleRun = runMismatch ? undefined : detail.data || run.data;
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
  const hasPrimaryFiles = partitionGeneratedResultFiles(displayFiles).primary.length > 0;
  const isRunning = !runMismatch && !run.isError && visibleRun?.status === "running";
  const processedCount = visibleRun?.processed_count;
  const totalCount = visibleRun?.total_count;
  const hasRunProgress = typeof processedCount === "number" && typeof totalCount === "number" && totalCount > 0;
  const progressPercent = hasRunProgress ? Math.min(100, Math.max(0, processedCount / totalCount * 100)) : 0;
  useEffect(() => { setShowAllFiles(false); }, [runId]);
  useEffect(() => {
    if (runId) resultPanelRef.current?.scrollIntoView({ block: "start" });
  }, [runId]);

  const start = useMutation({
    mutationFn: async () => {
      const nextErrors: Record<string, string> = {};
      const parsed = parseWorkflowSchemaDraft(schema, values, "inputs", false, nextErrors);
      setErrors(nextErrors);
      if (Object.keys(nextErrors).length) throw new Error(t("component.workspace_app_view.check_inputs"));
      return api.workflows.runBinding(action.binding_id!, {
        variables: parsed,
        trigger_data: { workspace_manual: true },
        execute: false,
      }) as Promise<Run>;
    },
    onSuccess: (created) => {
      setRunId(created.id);
      void queryClient.invalidateQueries({ queryKey: ["workspace-chat", workspaceId] });
      void queryClient.invalidateQueries({ queryKey: ["workspace-workflow-runs", workspaceId] });
      void recent.refetch();
    },
  });

  const next = () => {
    if (fileBusy) return;
    if (!currentSchema) return;
    const nextErrors: Record<string, string> = {};
    parseWorkflowSchemaDraft(currentSchema, values, "inputs", false, nextErrors);
    setErrors(nextErrors);
    if (Object.keys(nextErrors).length) return;
    setStep((previous) => previous + 1);
  };

  const flowHref = workflowRunPath(action.workflow_id || "", runId || "", workspaceId);
  const returnTo = `${location.pathname}${location.search}${location.hash}`;
  const returnState = { ...location.state, workspaceApp: { workspaceId, actionKey: action.key, runId } };
  const runLinkState = { returnTo, returnState };
  const rememberOrigin = (event: React.MouseEvent<HTMLAnchorElement>) => {
    if (event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey) {
      navigate(returnTo, { replace: true, state: returnState });
    }
  };
  const resultValue = detail.data?.variables?.__result;
  const resultSummary = resultValue && typeof resultValue === "object" && !Array.isArray(resultValue)
    ? String((resultValue as Record<string, unknown>).summary || "") : "";
  const result = resultSummary || outputText(resultValue);
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
    <span className="workspace-app-flow__mark" aria-hidden="true"><IconFlow size={22} /></span>
    <h2>{action.title}</h2>
    {action.description && <p className="workspace-app-flow__intro">{action.description}</p>}

    {!runId && (setup.isPending || setup.isError || !setup.data?.ready) ? <>
      <div className="workspace-app-flow__panel" role={setup.isError ? "alert" : "status"}>
        <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.before_start")}</span>
        <h3>{setup.isPending ? t("component.workspace_app_view.checking_setup") : setup.isError ? t("component.workspace_app_view.setup_check_failed") : needsMaterials ? t("component.workspace_app_view.add_materials_title") : setupTitle}</h3>
        {!setup.isPending && !setup.isError && <p>{setupLead}</p>}
        {!setup.isPending && !setup.isError && automaticSetup && <p>{t(automaticSetup === "failed"
          ? "component.workspace_app_view.setup_failed_hint" : "component.workspace_app_view.setup_automatic_hint")}</p>}
        {!setup.isPending && !setup.isError && setupDetails && <details className="workspace-app-setup__details"><summary>{t("component.workspace_app_view.setup_more_details")}</summary><p>{setupDetails}</p></details>}
        {!setup.isPending && !setup.isError && setupKind === "workspace_user_materials" && setupCheck?.key && <WorkspaceAppSetupMaterials key={setupCheck.key} workspaceId={workspaceId} checkKey={setupCheck.key} acceptsText={setupCheck.accepts_text === true} canStart={canStart} />}
        {!setup.isPending && !setup.isError && needsMaterials && setupKind !== "workspace_user_materials" && <p>{t("component.workspace_app_view.materials_in_knowledge")}</p>}
        <div className="workspace-app-flow__buttons">
          {!setup.isPending && <Button variant="ghost" onClick={() => void setup.refetch()}>{t("component.workspace_app_view.check_again")}</Button>}
          {!setup.isPending && !setup.isError && setup.data?.automatic_setup_task_id && <Link className="workspace-app-flow__details" to={`/tasks/${encodeURIComponent(setup.data.automatic_setup_task_id)}`}>{t("component.workspace_app_view.view_setup_task")} <IconArrowRight size={15} /></Link>}
          {!setup.isPending && !setup.isError && !setup.data?.automatic_setup_task_id && setupKind !== "workspace_user_materials" && <Link className="workspace-app-flow__details" to={setupHref}>{t(needsMaterials ? "component.workspace_app_view.add_materials" : "component.workspace_app_view.open_setup")} <IconArrowRight size={15} /></Link>}
        </div>
      </div>
    </> : !runId ? <>
      {chatAvailable && step === 0 && <Button variant="ghost" size="sm" onClick={onUseChat} disabled={!canStart}>
        <IconChatBubble size={14} aria-hidden="true" />{t("component.workspace_app_view.focus_chat")}
      </Button>}
      <div className="workspace-app-flow__steps" aria-label={t("component.workspace_app_view.progress")}>
        <span className={step < inputs.length ? "is-current" : "is-complete"}>{t("component.workspace_app_view.step_input")}</span>
        <span className={step === inputs.length ? "is-current" : ""}>{t("component.workspace_app_view.step_review")}</span>
        <span>{t("component.workspace_app_view.step_result")}</span>
      </div>
      {currentInput && currentSchema ? <div className="workspace-app-flow__panel" key={currentInput.key}>
        <span className="workspace-app-flow__kicker">{step + 1} / {inputs.length}</span>
        <WorkflowSchemaFields
          rootKey="inputs" schema={currentSchema} value={values} errors={errors}
          workspaceId={workspaceId} disabled={!canStart}
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
          const value = reviewValue((values as Record<string, unknown>)?.[input.key]);
          const display = input.schema?.["x-ui"]?.control === "knowledge_file" ? workflowFileName(value) : value;
          return <div key={input.key}><dt>{input.label}</dt><dd>{display || "—"}</dd></div>;
        })}</dl>}
        {start.error && <p role="alert" className="workspace-app-flow__error">{setupFailure ? t("component.workspace_app_view.setup_required") : start.error instanceof Error ? start.error.message : t("component.workspace_app_view.start_error")}</p>}
        <div className="workspace-app-flow__buttons">
          {inputs.length > 0 && <Button variant="ghost" onClick={() => setStep(inputs.length - 1)} disabled={start.isPending}>{t("component.workspace_app_view.back")}</Button>}
          <Button variant="primary" onClick={() => start.mutate()} disabled={!canStart || !action.binding_id || start.isPending} loading={start.isPending}>{t("component.workspace_app_view.run")}</Button>
        </div>
      </div>}
    </> : <div ref={resultPanelRef} className="workspace-app-flow__panel" role="status">
      <span className="workspace-app-flow__kicker">{t("component.workspace_app_view.step_result")}</span>
      <h3 className={isRunning ? "workspace-app-flow__running-heading" : undefined}>
        {isRunning && <span className="workspace-app-flow__running-indicator" aria-hidden="true" />}
        {runMismatch ? t("component.workspace_app_view.run_mismatch") : run.isError ? t("component.workspace_app_view.status_unavailable") : run.isPending ? t("component.workspace_app_view.loading_run") : runStatusLabel(visibleRun?.status || "pending", visibleRun?.business_outcome)}
      </h3>
      {!runMismatch && visibleRun && <p>{runSourceLabel(visibleRun.trigger_source)}{runTime(visibleRun.started_at || visibleRun.created_at) ? ` · ${runTime(visibleRun.started_at || visibleRun.created_at)}` : ""}</p>}
      {visibleRun?.current_step_name && !terminal && <p>{visibleRun.current_step_name}</p>}
      {isRunning && <div
        className={`workspace-app-flow__run-progress${hasRunProgress ? " is-determinate" : ""}`}
        role="progressbar"
        aria-label={t("component.workspace_app_view.progress")}
        aria-valuemin={hasRunProgress ? 0 : undefined}
        aria-valuemax={hasRunProgress ? totalCount : undefined}
        aria-valuenow={hasRunProgress ? Math.min(totalCount, Math.max(0, processedCount)) : undefined}
      >
        <span className="workspace-app-flow__run-progress-track" aria-hidden="true"><span style={hasRunProgress ? { width: `${progressPercent}%` } : undefined} /></span>
        {hasRunProgress && <span className="workspace-app-flow__run-progress-count" aria-hidden="true">{processedCount} / {totalCount}</span>}
      </div>}
      {!isRunning && visibleRun?.status !== "completed" && typeof processedCount === "number" && typeof totalCount === "number" && <p>{processedCount} / {totalCount}</p>}
      {visibleRun?.error && <p className="workspace-app-flow__error">{visibleRun.error}</p>}
      {run.isError && <p role="alert" className="workspace-app-flow__error">{t("component.workspace_app_view.run_load_error")}</p>}
      {terminal && detail.isError && <p role="alert" className="workspace-app-flow__error">{t("component.workspace_app_view.run_load_error")}</p>}
      {hasPrimaryFiles && <GeneratedResultFiles files={displayFiles} />}
      {result && <section><h4>{t("component.workspace_app_view.output")}</h4>{resultSummary
        ? <p className="workspace-app-flow__summary">{resultSummary}</p>
        : <pre className="workspace-app-flow__output">{result}</pre>}
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
      {workflowSteps.length > 0 && <section><h4>{t("component.workspace_app_view.run_steps")}</h4>
        <ol className="workspace-app-flow__timeline">{workflowSteps.map((item, index) => <li key={item.id} className={`is-${item.status}`}>
          <span className="workspace-app-flow__timeline-mark" aria-hidden="true">{item.status === "completed" ? <IconCheck size={13} /> : index + 1}</span>
          <span>{item.name}</span><small>{runStatusLabel(item.status)}</small>
        </li>)}</ol>
      </section>}
      {terminal && !detail.isPending && !detail.isError && !result && files.length === 0 && (visibleRun?.artifact_count || 0) === 0 && !visibleRun?.error && <p>{t("component.workspace_app_view.no_output")}</p>}
      {terminal && !detail.isPending && !detail.isError && (visibleRun?.artifact_count || 0) > 0 && files.length === 0 && <p>{t("component.workspace_app_view.no_viewable_files")}</p>}
      <div className="workspace-app-flow__buttons workspace-app-flow__result-actions">
          {runId && !runMismatch && run.data?.capabilities?.can_control && run.data?.intervention?.kind === "workflow_retry" &&
            <WorkflowRunRetryControl
              key={runId}
              runId={runId}
              workspaceId={workspaceId}
              bindingId={action.binding_id}
              workflowId={action.workflow_id}
              businessOutcome={run.data.business_outcome}
              disabled={!canStart || run.isError}
              size="sm"
              title={run.data.business_outcome === "needs_input" ? t("component.workflow_run.input_guidance") : t("component.workspace_app_view.retry_help")}
              onRetried={(retried) => { setRunId(retried.id); void recent.refetch(); }}
            />}
          {terminal &&
            <Button variant="ghost" size="sm" title={t("component.workspace_app_view.new_run_help")} onClick={() => { setRunId(null); setStep(0); start.reset(); }}>
              {t("component.workspace_app_view.run_again")}
            </Button>
          }
          {(run.isError || detail.isError) && <Button variant="ghost" loading={run.isFetching || detail.isFetching} onClick={() => { void run.refetch(); if (terminal) void detail.refetch(); void recent.refetch(); }}>{t("component.workspace_app_view.refresh_status")}</Button>}
          {runId && visibleRun?.status === "paused" && !runMismatch && <WorkflowRunResumeControl
            runId={runId}
            onResumed={() => { void run.refetch(); void recent.refetch(); }}
          />}
          {!terminal && visibleRun && !runMismatch && onFollowProgress && <Button variant="ghost" size="sm" title={t("component.workspace_app_view.background_progress")} onClick={onFollowProgress}>
            <IconChatBubble size={14} aria-hidden="true" />{t("component.workspace_app_view.follow_in_chat")}
          </Button>}
          {!runMismatch && <Link className="workspace-app-flow__details" to={flowHref} state={runLinkState} onClick={rememberOrigin}>{t("component.workspace_app_view.full_run")} <IconArrowRight size={15} /></Link>}
      </div>
    </div>}
    {historyRuns.length > 0 && <section className="workspace-app-flow__history" aria-label={t("component.workspace_app_view.history")}>
      <h3>{t("component.workspace_app_view.history")}</h3>
      {historyRuns.map((item) => {
        const duration = workflowRunDurationMs({ id: item.id, status: item.status,
          started_at: item.started_at || undefined, completed_at: item.completed_at || undefined,
          updated_at: item.updated_at || undefined });
        const time = runTime(item.started_at || item.created_at);
        return <button type="button" key={item.id} onClick={() => setRunId(item.id)}
          aria-current={runId === item.id ? "true" : undefined}>
          <span className="workspace-app-flow__history-copy">
            <span className="workspace-app-flow__history-heading"><strong>{action.title}</strong></span>
            <span className="workspace-app-flow__history-meta">
              <span>{runSourceLabel(item.trigger_source)}</span>
              {time && <time dateTime={item.started_at || item.created_at || undefined}>{time}</time>}
              {duration !== null && <span>{t("component.workflow_run_history.duration")}: {formatWorkflowDuration(duration)}</span>}
              {(item.attempt_number || 1) > 1 && <span>{t("component.workflow_run_history.attempts", { count: item.attempt_number! })}</span>}
              {item.status !== "completed" && typeof item.processed_count === "number" && typeof item.total_count === "number" && item.total_count > 0
                && <span>{t("component.workflow_run_history.steps_progress", { count: item.processed_count, total: item.total_count })}</span>}
            </span>
            {item.current_step_name && ["pending", "running", "paused", "failed"].includes(item.status) && <span className="workspace-app-flow__history-step">{t(item.status === "failed"
              ? "component.workflow_run_history.failed_node" : "component.workflow_run_history.current_node")}: {item.current_step_name}</span>}
          </span>
          <span className="workspace-app-flow__history-status" data-status={item.status}>{runStatusLabel(item.status, item.business_outcome)}</span>
          <IconArrowRight size={15} aria-hidden="true" />
        </button>;
      })}
    </section>}
  </div>;
}
