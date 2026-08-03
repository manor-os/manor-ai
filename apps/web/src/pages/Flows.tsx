import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useSearchParams } from "react-router-dom";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { t } from "../lib/i18n";
import { useToastStore } from "../stores/toast";
import PageHeader, { PageHeaderAddButton } from "../components/ui/PageHeader";
import Modal from "../components/ui/Modal";
import ConfirmDialog from "../components/ui/ConfirmDialog";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import Button from "../components/ui/Button";
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
  IconPlay,
  IconPlus,
  IconTrash,
  IconUpload,
} from "../components/icons";
import SmartToolbar from "../components/ui/SmartToolbar";
import WorkflowImportModal from "../components/workflows/WorkflowImportModal";
import WorkflowDeployModal from "../components/workflows/WorkflowDeployModal";
import WorkflowCanvas, { NodeIcon, type CanvasStep } from "../components/workflows/WorkflowCanvas";
import WorkflowNodePalette from "../components/workflows/WorkflowNodePalette";
import WorkflowNodeConfigPanel from "../components/workflows/WorkflowNodeConfigPanel";
import MediaPreview from "../components/workflows/MediaPreview";
import AiEditButton from "../components/ui/AiEditButton";
import WorkflowTemplates, { type WorkflowTemplate } from "../components/workflows/WorkflowTemplates";
import { closeEditorLiveChat, openEditorLiveChat } from "../lib/editorLiveChat";
import { parseWorkflowLiveEdit, serializeWorkflowLiveEdit } from "../lib/workflowLiveEdit";
import { extractMediaRefs, primaryMediaRef, type MediaRef } from "../lib/workflowMedia";
import { validateWorkflow, issuesByNode } from "../lib/workflowValidate";

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface FlowStep {
  id: string;
  type: "agent" | "tool" | "condition" | "wait" | "notify" | "transform";
  name: string;
  status?: "pending" | "running" | "done" | "error";
  config?: Record<string, unknown>;
}

interface Flow {
  id: string;
  name: string;
  description: string;
  icon?: string;
  trigger: "manual" | "event" | "schedule";
  trigger_type?: "manual" | "event" | "schedule" | "webhook" | "mcp" | "internal";
  trigger_config?: Record<string, unknown>;
  variables?: Record<string, unknown>;
  category?: string | null;
  tags?: string[];
  status: "active" | "draft";
  steps: FlowStep[];
  last_run?: string;
  created_by?: string | null;
  created_at: string;
  updated_at?: string | null;
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
  { value: "flow", label: "Workflow" },
  { value: "llm", label: "AI" },
  { value: "image", label: "Image" },
  { value: "video", label: "Video" },
  { value: "rag", label: "Knowledge" },
  { value: "classifier", label: "Classify" },
  { value: "http", label: "Web" },
  { value: "code", label: "Code" },
  { value: "notify", label: "Notify" },
  { value: "wait", label: "Schedule" },
] as const;

function workflowIconGlyph(icon?: string, size = 18) {
  return icon && icon !== "flow"
    ? <NodeIcon type={icon} size={size} />
    : <IconFlow size={size} />;
}

const STATUS_DOT: Record<string, string> = {
  pending: "#a8a29e",
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
  return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function formatWorkflowTimestamp(iso?: string | null): string {
  if (!iso) return "—";
  const value = new Date(iso);
  if (Number.isNaN(value.getTime())) return iso;
  return value.toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
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
        <span>{t("page.flows.details_loading")}</span>
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

type WorkflowRunInput = {
  key: string;
  label: string;
  type: "string" | "number" | "boolean" | "json";
  required: boolean;
  placeholder?: string;
  defaultValue: string;
};

function runInputDefault(value: unknown, variables?: Record<string, unknown>): unknown {
  if (typeof value !== "string") return value;
  const match = /^\s*\{\{([^{}]+)\}\}\s*$/.exec(value);
  if (!match) return value;
  const parts = match[1].trim().split(".");
  let current: unknown = variables?.[parts[0]];
  for (const part of parts.slice(1)) {
    if (!current || typeof current !== "object") return undefined;
    current = (current as Record<string, unknown>)[part];
  }
  return current;
}

function formatRunInputValue(value: unknown): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    try { return JSON.stringify(value, null, 2); } catch { return String(value); }
  }
  return String(value);
}

/** Inputs declared on the entry node are runtime values, not saved workflow
 * settings. Imported n8n Form Triggers use this same schema. */
export function workflowRunInputs(flow: Pick<Flow, "steps" | "variables">): WorkflowRunInput[] {
  const entries = (flow.steps || []).filter((step: any) => ["trigger", "webhook"].includes(step.type));
  const seen = new Set<string>();
  const result: WorkflowRunInput[] = [];
  for (const step of entries) {
    const rows = Array.isArray(step.config?.run_inputs)
      ? step.config.run_inputs
      : Array.isArray(step.config?.inputs) ? step.config.inputs : [];
    for (const row of rows) {
      const key = String(row?.key || row?.name || "").trim();
      if (!key || seen.has(key)) continue;
      seen.add(key);
      const rawType = String(row?.type || "string").toLowerCase();
      const type: WorkflowRunInput["type"] = ["number", "boolean", "json"].includes(rawType)
        ? rawType as WorkflowRunInput["type"]
        : "string";
      const rawDefault = row?.defaultValue ?? row?.default ?? row?.value;
      const formattedDefault = formatRunInputValue(runInputDefault(rawDefault, flow.variables));
      result.push({
        key,
        label: String(row?.label || key),
        type,
        required: row?.required ?? row?.requiredField ?? true,
        placeholder: row?.placeholder ? String(row.placeholder) : undefined,
        defaultValue: type === "boolean" && formattedDefault === "" ? "false" : formattedDefault,
      });
    }
  }
  return result;
}

function workflowNodeOutputs(step: any): { name: string; type: string }[] {
  const configured = Array.isArray(step.config?.outputs) ? step.config.outputs : null;
  const rows = configured ?? (
    ["trigger", "webhook"].includes(step.type) && Array.isArray(step.config?.run_inputs)
      ? step.config.run_inputs
      : []
  );
  const named = rows
    .map((output: any) => ({
      name: String(output?.key || output?.name || "").trim(),
      type: String(output?.type || "any").toLowerCase() === "string" ? "text" : output?.type || "any",
    }))
    .filter((output: any) => output.name);
  return [
    ...named,
    ...(step.config?.output_var ? [{ name: String(step.config.output_var), type: "any" }] : []),
  ];
}

type WorkflowFinalResult = {
  stepId: string | null;
  stepName: string;
  output: unknown;
};

function workflowStepTargets(step: any): string[] {
  return [...new Set([
    ...(step?.next || []),
    ...(step?.true_next || []),
    ...(step?.false_next || []),
    ...((Array.isArray(step?.config?.cases) ? step.config.cases : [])
      .flatMap((item: any) => item?.next || [])),
    ...(step?.config?.default_next || []),
  ].filter(Boolean))] as string[];
}

function hasWorkflowOutput(value: unknown): boolean {
  return value !== undefined && value !== null && value !== "";
}

/** Resolve the business output for both new runs (End passes data through) and
 * legacy runs where End only returned its own label. */
export function resolveWorkflowFinalResult(steps: any[], run: any): WorkflowFinalResult | null {
  const results = run?.step_results || {};
  const terminals = steps.filter((step) => step.type === "end" || workflowStepTargets(step).length === 0);

  for (const terminal of terminals) {
    const output = results[terminal.id]?.output;
    if (hasWorkflowOutput(output) && output !== terminal.name) {
      return { stepId: terminal.id, stepName: terminal.name || "End", output };
    }
    const predecessor = [...steps].reverse().find((step) =>
      workflowStepTargets(step).includes(terminal.id)
      && results[step.id]?.status === "completed"
      && !results[step.id]?.skipped
      && hasWorkflowOutput(results[step.id]?.output),
    );
    if (predecessor) {
      return {
        stepId: predecessor.id,
        stepName: predecessor.name || predecessor.id,
        output: results[predecessor.id].output,
      };
    }
  }

  if (hasWorkflowOutput(run?.variables?.__result)) {
    return { stepId: null, stepName: "Workflow", output: run.variables.__result };
  }

  for (const [stepId, result] of Object.entries(results).reverse()) {
    const step = steps.find((candidate) => candidate.id === stepId);
    const typed = result as any;
    if (!step || ["trigger", "webhook", "end", "note"].includes(step.type)) continue;
    if (typed?.status === "completed" && !typed?.skipped && hasWorkflowOutput(typed?.output)) {
      return { stepId, stepName: step.name || stepId, output: typed.output };
    }
  }
  return null;
}

function WorkflowFinalResultPanel({
  run,
  result,
  onClose,
  onOpenNode,
}: {
  run: any;
  result: WorkflowFinalResult | null;
  onClose: () => void;
  onOpenNode: (stepId: string) => void;
}) {
  const failed = run?.status === "failed";
  const output = failed ? run?.error : result?.output;
  const media = failed ? [] : extractMediaRefs(output, 3);
  const runInputs = Object.entries(run?.trigger_data || {}).filter(
    ([key, value]) => key && value !== undefined,
  );
  return (
    <section
      className="workflow-final-result"
      role="region"
      aria-label="Workflow final result"
      aria-live="polite"
    >
      <div className="workflow-final-result-header">
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 14, fontWeight: 750, color: "var(--text-strong)" }}>Final result</div>
          <div className="workflow-final-result-meta">
            <span
              aria-hidden="true"
              className={`workflow-final-result-dot${failed ? " is-failed" : ""}`}
            />
            <span style={{ textTransform: "capitalize" }}>{run?.status || "completed"}</span>
            {result?.stepName && <span>· {result.stepName}</span>}
            {run?.completed_at && <span>· {fmtRunTime(run.completed_at)}</span>}
          </div>
        </div>
        <button
          type="button"
          className="workflow-final-result-close"
          onClick={onClose}
          title="Close final result"
          aria-label="Close final result"
        >
          <IconClose size={15} />
        </button>
      </div>
      {runInputs.length > 0 && (
        <section className="workflow-final-result-inputs" aria-labelledby="workflow-final-result-inputs-title">
          <div id="workflow-final-result-inputs-title" className="workflow-final-result-section-title">
            Run inputs
          </div>
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
        </section>
      )}
      <div className={`workflow-final-result-content${failed ? " is-failed" : ""}`}>
        {!hasWorkflowOutput(output) ? (
          <span style={{ color: "var(--text-faint)", fontSize: 12 }}>This run did not produce a final output.</span>
        ) : media.length > 0 ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {media.map((item, index) => <MediaPreview key={`${item.type}-${index}`} refItem={item} maxHeight={360} />)}
          </div>
        ) : (
          <pre className={typeof output === "string" ? "is-text" : undefined}>
            {typeof output === "string" ? output : JSON.stringify(output, null, 2)}
          </pre>
        )}
      </div>
      {result?.stepId && (
        <div className="workflow-final-result-actions">
          <Button variant="outline" size="sm" onClick={() => onOpenNode(result.stepId!)}>
            Open source node
          </Button>
        </div>
      )}
    </section>
  );
}


/* ------------------------------------------------------------------ */
/*  Component                                                          */
/* ------------------------------------------------------------------ */

export default function Flows() {
  const queryClient = useQueryClient();
  const toast = useToastStore();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedWorkflowId = searchParams.get("workflow");

  const [search, setSearch] = useState("");
  const [selectedFlow, setSelectedFlow] = useState<Flow | null>(null);
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [showImportModal, setShowImportModal] = useState(false);
  const [showTemplates, setShowTemplates] = useState(false);
  const [deployFlow, setDeployFlow] = useState<Flow | null>(null);
  const [configStepId, setConfigStepId] = useState<string | null>(null);
  const [addFromId, setAddFromId] = useState<string | null>(null);
  const [canvasFull, setCanvasFull] = useState(false);
  const [showIssues, setShowIssues] = useState(false);
  const [runResult, setRunResult] = useState<any>(null);
  const [showFinalResult, setShowFinalResult] = useState(false);
  const openedFinalRunRef = useRef<string | null>(null);
  const [liveStatus, setLiveStatus] = useState<Record<string, string>>({});
  const [streaming, setStreaming] = useState(false);
  const [runInputFlow, setRunInputFlow] = useState<Flow | null>(null);
  const [runInputValues, setRunInputValues] = useState<Record<string, string>>({});
  const [runInputErrors, setRunInputErrors] = useState<Record<string, string>>({});
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
  const [showIdentityModal, setShowIdentityModal] = useState(false);
  const [identityTarget, setIdentityTarget] = useState<Flow | null>(null);
  const [identityName, setIdentityName] = useState("");
  const [identityDescription, setIdentityDescription] = useState("");
  const [identityIcon, setIdentityIcon] = useState("flow");
  const [identityNameError, setIdentityNameError] = useState("");
  const selectedFlowRef = useRef<Flow | null>(null);
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

  const { data: flows, isLoading } = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api.workflows.list(),
  });

  // Workspace cards deep-link to a concrete workflow. Keep the selected
  // editor in the URL so refresh/back navigation preserves that context.
  useEffect(() => {
    if (!requestedWorkflowId || !flows?.length) return;
    if (selectedFlow?.id === requestedWorkflowId) return;
    const requested = (flows as Flow[]).find((flow) => flow.id === requestedWorkflowId);
    if (requested) setSelectedFlow(requested);
  }, [flows, requestedWorkflowId, selectedFlow?.id]);

  const closeWorkflow = () => {
    closeEditorLiveChat();
    setSelectedFlow(null);
    const next = new URLSearchParams(searchParams);
    next.delete("workflow");
    setSearchParams(next, { replace: true });
  };

  const { data: runs } = useQuery({
    queryKey: ["workflow-runs", selectedFlow?.id],
    queryFn: () => (selectedFlow ? api.workflows.runs(selectedFlow.id) : Promise.resolve([])),
    enabled: !!selectedFlow,
    // A long timer resumes in Celery, outside this browser request. Keep the
    // active run fresh until the worker reaches a terminal state so the canvas
    // does not remain stuck on the initial "Paused" response.
    refetchInterval: (query) => {
      const latest = (query.state.data as any[] | undefined)?.[0];
      return latest && (latest.status === "running" || latest.status === "paused") ? 2_000 : false;
    },
    refetchIntervalInBackground: true,
  });

  // Opening a flow should immediately restore its newest run onto the canvas;
  // requiring a separate History click made successful results look lost.
  useEffect(() => {
    if (!selectedFlow || !runs?.length) return;
    if (runResult?.workflow_id !== selectedFlow.id) setRunResult(runs[0]);
  }, [runs, runResult?.workflow_id, selectedFlow?.id]);

  // Reveal each completed/failed run once. Closing stays respected until a
  // different run is selected or a new run completes.
  useEffect(() => {
    if (!selectedFlow || runResult?.workflow_id !== selectedFlow.id) return;
    if (!["completed", "failed"].includes(runResult?.status)) return;
    if (openedFinalRunRef.current === runResult.id) return;
    openedFinalRunRef.current = runResult.id;
    setShowFinalResult(true);
  }, [runResult?.id, runResult?.status, runResult?.workflow_id, selectedFlow?.id]);

  useEffect(() => {
    if (!runResult || !runs?.length) return;
    const refreshed = runs.find((run: any) => run.id === runResult.id);
    if (!refreshed) return;
    const resultCount = Object.keys(runResult.step_results || {}).length;
    const refreshedCount = Object.keys(refreshed.step_results || {}).length;
    if (
      refreshed.status !== runResult.status ||
      refreshed.completed_at !== runResult.completed_at ||
      refreshedCount !== resultCount
    ) {
      setRunResult(refreshed);
    }
  }, [runs, runResult]);

  // Create a workflow from a template and jump straight into its editor.
  const templateMutation = useMutation({
    mutationFn: (tpl: WorkflowTemplate) =>
      api.workflows.create({ name: tpl.name, description: tpl.description, icon: tpl.icon, trigger_type: tpl.trigger_type, steps: tpl.steps }),
    onSuccess: (created: any) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      setShowTemplates(false);
      setSelectedFlow(created);
      toast.success("Created from template");
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

  const resumeMutation = useMutation({
    mutationFn: (runId: string) => api.workflows.resumeRun(runId),
    onSuccess: (updated: any) => {
      setRunResult(updated);
      queryClient.invalidateQueries({ queryKey: ["workflow-runs"] });
      toast.success(updated.status === "completed" ? "Workflow completed" : "Workflow resumed");
    },
    onError: (e: any) => {
      toast.error("Couldn't resume workflow", e?.message || "Please try again.");
    },
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, ...changes }: { id: string; [key: string]: any }) =>
      api.workflows.update(id, changes),
    onSuccess: (updated: any) => {
      queryClient.invalidateQueries({ queryKey: ["workflows"] });
      queryClient.invalidateQueries({ queryKey: ["workflow-metadata", updated.id] });
      if (selectedFlowRef.current?.id === updated.id) {
        setSelectedFlow(updated);
      }
      if (identityTarget?.id === updated.id) {
        setIdentityTarget(updated);
      }
    },
    // Saves (add node, AI edit, config, drag) previously failed silently — the
    // API layer suppresses 401/403 toasts, so an expired session made clicking
    // "do nothing". Surface every failure with an actionable message.
    onError: (e: any) => {
      if (e?.status === 401 || e?.status === 403) {
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
    closeDetail();
    setSelectedFlow(flow);
    const next = new URLSearchParams(searchParams);
    next.set("workflow", flow.id);
    setSearchParams(next, { replace: true });
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

  const openWorkflowAiEdit = () => {
    const current = selectedFlowRef.current;
    if (!current) return;
    openEditorLiveChat({
      documentName: current.name,
      fileType: "workflow",
      mimeType: "application/vnd.manor.workflow+json",
      editorType: "Workflow",
      sessionLabel: `Workflow: ${current.name}`,
      emptyDescription: `Describe what to change. I will update ${current.name} directly on the canvas as the answer streams.`,
      placeholder: `Describe how to change ${current.name}...`,
      examples: ["Add an LLM node", "Add a branch", "Fix connections", "Add error handling"],
      getContent: () => serializeWorkflowLiveEdit(selectedFlowRef.current || current),
      applyContent: (content) => {
        const active = selectedFlowRef.current;
        if (!active || active.id !== current.id) {
          throw new Error("The workflow is no longer open.");
        }
        const update = parseWorkflowLiveEdit(content);
        updateMutation.mutate(
          { id: active.id, ...update },
          {
            onSuccess: (saved: any) => {
              selectedFlowRef.current = saved;
              toast.success(`AI updated · ${saved.steps?.length || 0} node${saved.steps?.length === 1 ? "" : "s"}`);
            },
          },
        );
      },
    });
  };

  // Add a node of `type`, linking it from `addFromId` (a node's "+" output) or,
  // failing that, the end of the current chain.
  const addNode = (type: string) => {
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
      const label = type.charAt(0).toUpperCase() + type.slice(1);
      if (addFromId) {
        // added from a node's "+" output handle → wire it from that node
        const source = steps.find((s: any) => s.id === addFromId);
        if (source) source.next.push(id);
        steps.push({ id, type, name: label, config: {}, next: [] });
      } else {
        // top "Add node" button → drop an UNCONNECTED node next to the rightmost
        // node (never auto-wire it to the end/terminal node). The user draws the
        // connection by dragging an edge to it.
        const positioned = steps.filter((s: any) => s.position);
        const anchor = positioned.length
          ? positioned.reduce((a: any, b: any) => (b.position.x > a.position.x ? b : a))
          : null;
        const position = anchor ? { x: anchor.position.x + 260, y: anchor.position.y } : { x: 120, y: 340 };
        steps.push({ id, type, name: label, config: {}, next: [], position });
      }
    }
    setShowAddStep(false);
    setAddFromId(null);
    updateMutation.mutate(
      { id: selectedFlow.id, steps },
      { onSuccess: () => toast.success(type === "note" ? "Note added" : "Node added") },
    );
  };

  const visibleFlows = (flows || []).filter((flow: Flow) => flow.trigger_type !== "internal");
  const filtered = visibleFlows.filter(
    (f: any) =>
      f.name?.toLowerCase().includes(search.toLowerCase()) ||
      f.description?.toLowerCase().includes(search.toLowerCase()),
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
    try {
      const finalRun = await api.workflows.runStream(
        flow.id,
        (nodeId, status) => setLiveStatus((previous) => ({ ...previous, [nodeId]: status })),
        { trigger_data: triggerData },
      );
      if (finalRun) setRunResult(finalRun);
      queryClient.invalidateQueries({ queryKey: ["workflow-runs"] });
    } catch (error: any) {
      toast.error("Workflow couldn't run", error?.message || "Please check the inputs and try again.");
    } finally {
      setStreaming(false);
      setLiveStatus({});
    }
  };

  const requestWorkflowRun = (flow: Flow) => {
    const inputs = workflowRunInputs(flow);
    if (!inputs.length) {
      void executeWorkflow(flow);
      return;
    }
    closeDetail();
    setSelectedFlow(flow);
    setRunInputValues(Object.fromEntries(inputs.map((input) => [input.key, input.defaultValue])));
    setRunInputErrors({});
    setRunInputFlow(flow);
  };

  const submitWorkflowRun = () => {
    if (!runInputFlow || streaming) return;
    const inputs = workflowRunInputs(runInputFlow);
    const errors: Record<string, string> = {};
    const triggerData: Record<string, unknown> = {};
    for (const input of inputs) {
      const value = runInputValues[input.key] ?? "";
      if (!value.trim()) {
        if (input.required) errors[input.key] = "Provide a value before running this workflow.";
        continue;
      }
      if (input.type === "number") {
        const parsed = Number(value);
        if (!Number.isFinite(parsed)) errors[input.key] = "Enter a valid number.";
        else triggerData[input.key] = parsed;
      } else if (input.type === "boolean") {
        triggerData[input.key] = value === "true";
      } else if (input.type === "json") {
        try { triggerData[input.key] = JSON.parse(value); }
        catch { errors[input.key] = "Enter valid JSON."; }
      } else {
        triggerData[input.key] = value;
      }
    }
    setRunInputErrors(errors);
    if (Object.keys(errors).length) return;
    const flow = runInputFlow;
    setRunInputFlow(null);
    void executeWorkflow(flow, triggerData);
  };

  const runInputModal = (
    <Modal
      open={!!runInputFlow}
      onClose={() => { if (!streaming) setRunInputFlow(null); }}
      title={runInputFlow ? `Run ${runInputFlow.name}` : "Run workflow"}
      maxWidth="560px"
      footer={(
        <>
          <Button variant="ghost" onClick={() => setRunInputFlow(null)} disabled={streaming}>Cancel</Button>
          <Button onClick={submitWorkflowRun} loading={streaming}>Run workflow</Button>
        </>
      )}
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
        <p style={{ margin: 0, color: "var(--text-muted)", fontSize: 13, lineHeight: 1.55 }}>
          Provide the entry data for this run. These test values are sent to the trigger and are not saved to the workflow.
        </p>
        {runInputFlow && workflowRunInputs(runInputFlow).map((input, index) => (
          <div key={input.key} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
            <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
              <label htmlFor={`workflow-run-input-${index}`} style={{ fontSize: 12.5, fontWeight: 700, color: "var(--text-strong)" }}>
                {input.label}{input.required ? " *" : ""}
              </label>
              <span style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 10.5, color: "var(--text-faint)" }}>{input.type}</span>
            </div>
            {input.type === "json" ? (
              <Textarea
                value={runInputValues[input.key] ?? ""}
                onChange={(event) => {
                  setRunInputValues((current) => ({ ...current, [input.key]: event.target.value }));
                  setRunInputErrors((current) => ({ ...current, [input.key]: "" }));
                }}
                rows={4}
                placeholder={input.placeholder || "Enter a JSON value"}
                error={runInputErrors[input.key]}
                ariaLabel={`Run value for ${input.label}`}
              />
            ) : input.type === "boolean" ? (
              <select
                id={`workflow-run-input-${index}`}
                value={runInputValues[input.key] || "false"}
                onChange={(event) => setRunInputValues((current) => ({ ...current, [input.key]: event.target.value }))}
                className="manor-input"
                aria-label={`Run value for ${input.label}`}
              >
                <option value="false">False</option>
                <option value="true">True</option>
              </select>
            ) : (
              <Input
                value={runInputValues[input.key] ?? ""}
                onChange={(event) => {
                  setRunInputValues((current) => ({ ...current, [input.key]: event.target.value }));
                  setRunInputErrors((current) => ({ ...current, [input.key]: "" }));
                }}
                type={input.type === "number" ? "number" : "text"}
                placeholder={input.placeholder || "Enter a value"}
                error={runInputErrors[input.key]}
                autoFocus={index === 0}
                ariaLabel={`Run value for ${input.label}`}
              />
            )}
          </div>
        ))}
      </div>
    </Modal>
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
          <div className="workflow-icon-options" role="radiogroup" aria-label="Workflow icon">
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
        runStatusById[id] = rr.skipped ? "skipped" : rr.status;
        // a one-line preview of what this node produced, shown on the card after
        // a run — so you can see each node's result without opening it.
        let out = "";
        if (rr.skipped) out = "skipped — needs config";
        else if (rr.error) out = String(rr.error);
        else if (rr.output == null || rr.output === "") out = "(no output)";
        else out = typeof rr.output === "string" ? rr.output : JSON.stringify(rr.output);
        outputById[id] = out.replace(/\s+/g, " ").trim().slice(0, 90);
      }
      const ref = r && typeof r === "object" ? primaryMediaRef((r as any).output) : undefined;
      if (ref) previewById[id] = ref;
    }
    // While a streaming run is in flight, live per-node events drive the canvas.
    const statusById = streaming ? liveStatus : runStatusById;

    // Static validation — config gaps and broken wiring, surfaced before a run.
    const issues = validateWorkflow(steps as any[]);
    const issueById = issuesByNode(issues);
    const errorCount = issues.filter((i) => i.level === "error").length;
    const triggerKind = flow.trigger || flow.trigger_type || "manual";

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
    // Notes are canvas annotations, not runtime steps. Keep them out of both
    // sides of the progress fraction so a completed five-node flow reads 5/5
    // even when it also contains sticky notes.
    const executableSteps = steps.filter((step: any) => step.type !== "note");
    const doneCount = executableSteps.filter((step: any) => statusById[step.id] === "completed").length;
    const showBanner = streaming || !!lastRun;
    const bannerStatus = streaming ? "running" : lastRun?.status;
    const runBannerColor = streaming
      ? "#0f766e"
      : lastRun
        ? lastRun.status === "completed" ? "#4f9c84" : lastRun.status === "failed" ? "#d65f59" : "#cf9b44"
        : null;
    return (
      <div style={{ height: "100%", display: "flex", flexDirection: "column", padding: "1rem", overflow: "hidden", position: "relative", zIndex: 10 }}>
        {/* Back + editor identity + a consistent metadata/action toolbar. */}
        <div className="workflow-editor-header">
          <button
            type="button"
            className="workflow-editor-back"
            aria-label="Back to flows"
            title="Back to flows"
            onClick={() => {
              closeWorkflow();
            }}
          >
            <IconChevronLeft size={16} />
          </button>
          <div className="workflow-editor-identity">
            <IconTile className="workflow-editor-identity-icon" size={40}>
              {workflowIconGlyph(flow.icon, 20)}
            </IconTile>
            <div className="workflow-editor-heading">
              <h1>
                <button
                  type="button"
                  className="workflow-editor-identity-edit"
                  onClick={() => openWorkflowIdentityEditor()}
                  aria-label="Edit workflow name, description, and icon"
                  title="Edit workflow details"
                >
                  <span>{flow.name}</span>
                  <IconEdit size={15} />
                </button>
              </h1>
              <p>{flow.description || "Add a description"}</p>
            </div>
          </div>
          <div className="workflow-editor-controls">
            <div className="workflow-editor-meta" aria-label="Workflow status">
              {(() => {
                const ok = issues.length === 0;
                const state = ok ? "valid" : errorCount > 0 ? "error" : "warning";
                return (
                  <button
                    type="button"
                    className={`workflow-editor-validation is-${state}`}
                    onClick={() => !ok && setShowIssues((v) => !v)}
                    title={ok ? "No issues" : `${issues.length} issue${issues.length === 1 ? "" : "s"}`}
                    disabled={ok}
                  >
                    <span className="workflow-editor-validation-icon" aria-hidden="true">{ok ? "✓" : "!"}</span>
                    {ok ? "Valid" : <span className="mono">{issues.length} issues</span>}
                  </button>
                );
              })()}
              <StatusBadge type={flow.status === "active" ? "active" : "gray"} dot>
                {flow.status === "active" ? t("page.flows.active") : t("page.flows.draft")}
              </StatusBadge>
              <StatusBadge type="gray">
                {t(TRIGGER_LABELS[triggerKind] || triggerKind)}
              </StatusBadge>
            </div>
            <div className="workflow-editor-actions" aria-label="Workflow actions">
              <AiEditButton className="workflow-editor-action" onClick={openWorkflowAiEdit} />
              <Button className="workflow-editor-action" variant="outline" onClick={() => openAddStep(steps.length)}>
                + Node
              </Button>
              <Button
                className="workflow-editor-action"
                variant="outline"
                onClick={() => {
                  if (errorCount > 0) {
                    setShowIssues(true);
                    toast.error("Workflow isn't deployable", "Fix the graph errors first.");
                    return;
                  }
                  setDeployFlow(flow);
                }}
              >
                Deploy
              </Button>
              <Button
                className="workflow-editor-action"
                variant="primary"
                onClick={() => runStep()}
                disabled={streaming || errorCount > 0}
                title={errorCount > 0 ? "Fix workflow errors before running" : undefined}
              >
                {streaming ? t("page.flows.starting") : t("page.flows.run")}
              </Button>
              <Button
                className="workflow-editor-action workflow-editor-action-history"
                variant="outline"
                onClick={() => {
                  const opening = !showRunHistory;
                  setShowRunHistory(opening);
                  if (opening && !lastRun && runs && runs.length) setRunResult(runs[0]);
                }}
              >
                {t("page.flows.history")}
              </Button>
              <Button
                className="workflow-editor-action workflow-editor-action-delete"
                variant="danger"
                onClick={() => setDeleteTarget(flow.id)}
                disabled={streaming || deleteMutation.isPending}
                loading={deleteMutation.isPending && deleteTarget === flow.id}
                title={t("page.flows.delete_flow")}
              >
                <IconTrash size={14} />
                {t("action.delete")}
              </Button>
            </div>
          </div>
        </div>

        {/* Visual canvas; AI edit uses the same global floating chat as the file editors. */}
        {(() => {
        const canvasSurface = (
        <div style={{ position: "relative", flex: 1, minHeight: 0, background: "#fff", borderRadius: 24, padding: 8, boxShadow: "0 4px 24px rgba(0,0,0,0.04)" }}>
          <button
            onClick={() => setCanvasFull((f) => !f)}
            title={canvasFull ? "Exit fullscreen" : "Fullscreen"}
            style={{
              position: "absolute", top: 16, right: 16, zIndex: 6,
              width: 34, height: 34, borderRadius: 9, border: "none", cursor: "pointer",
              background: "var(--surface-panel)", color: "var(--text-muted)",
              boxShadow: "0 1px 3px rgba(28,25,23,0.12)",
              display: "flex", alignItems: "center", justifyContent: "center",
            }}
          >
            <svg width={16} height={16} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.6} strokeLinecap="round" strokeLinejoin="round">
              {canvasFull ? (
                <path d="M9 9V4.5M9 9H4.5M9 9L3.75 3.75M15 9h4.5M15 9V4.5M15 9l5.25-5.25M9 15v4.5M9 15H4.5M9 15l-5.25 5.25M15 15h4.5M15 15v4.5m0-4.5l5.25 5.25" />
              ) : (
                <path d="M3.75 3.75v4.5m0-4.5h4.5m-4.5 0L9 9M3.75 20.25v-4.5m0 4.5h4.5m-4.5 0L9 15M20.25 3.75h-4.5m4.5 0v4.5m0-4.5L15 9m5.25 11.25h-4.5m4.5 0v-4.5m0 4.5L15 15" />
              )}
            </svg>
          </button>
          {showBanner && (
            <div
              style={{
                position: "absolute", top: 16, left: "50%", transform: "translateX(-50%)", zIndex: 6,
                display: "flex", alignItems: "center", gap: 8, padding: "5px 12px", borderRadius: 999,
                background: "var(--surface-panel)", boxShadow: "0 1px 3px rgba(28,25,23,0.12)",
                fontSize: 12, fontWeight: 600, color: "var(--text-muted)",
              }}
            >
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: runBannerColor || "#a8a29e", animation: streaming ? "pulse 1.2s ease-in-out infinite" : "none" }} />
              <span style={{ color: "var(--text-strong)", textTransform: "capitalize" }}>{bannerStatus}</span>
              <span className="mono" style={{ color: "var(--text-faint)" }}>{doneCount}/{executableSteps.length}</span>
              {!streaming && lastRun?.error && <span style={{ color: "#d65f59", maxWidth: 240, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>· {lastRun.error}</span>}
              {!streaming && lastRun?.status === "paused" && (
                <button
                  type="button"
                  onClick={() => resumeMutation.mutate(lastRun.id)}
                  disabled={resumeMutation.isPending}
                  style={{
                    border: "none", borderRadius: 7, padding: "3px 8px", cursor: "pointer",
                    background: "rgba(207,155,68,0.14)", color: "#9a6d1f", fontSize: 11.5, fontWeight: 700,
                  }}
                >
                  {resumeMutation.isPending ? "Resuming…" : "Resume"}
                </button>
              )}
              {!streaming && lastRun && ["completed", "failed"].includes(lastRun.status) && (
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => setShowFinalResult(true)}
                  style={{ height: 24, padding: "0 7px", fontSize: 11 }}
                >
                  View result
                </Button>
              )}
            </div>
          )}
          <WorkflowCanvas
            steps={steps as unknown as CanvasStep[]}
            statusById={statusById}
            previewById={previewById}
            outputById={outputById}
            onRunNode={runSingleNode}
            issueById={issueById}
            onStepsChange={(next) =>
              flow.id && updateMutation.mutate({ id: flow.id, steps: next })
            }
            onNodeOpen={(id) => setConfigStepId(id)}
            onAddFrom={(id) => { setAddFromId(id); setShowAddStep(true); }}
            onAddNode={() => { setAddFromId(null); setShowAddStep(true); }}
          />
        </div>
        );
        const editorBody = (
          <div
            style={
              canvasFull
                ? { position: "fixed", inset: 0, zIndex: 1000, background: "var(--surface-app)", padding: 8, display: "flex", gap: 8 }
                : { flex: 1, minHeight: 0, display: "flex", gap: 12 }
            }
          >
            {canvasSurface}
          </div>
        );
        return canvasFull ? createPortal(editorBody, document.body) : editorBody;
        })()}

        {showFinalResult && lastRun && (
          <WorkflowFinalResultPanel
            run={lastRun}
            result={finalResult}
            onClose={() => setShowFinalResult(false)}
            onOpenNode={(stepId) => {
              setConfigStepId(stepId);
              setShowFinalResult(false);
            }}
          />
        )}

        {/* Add node palette */}
        <Modal
          open={showAddStep}
          onClose={() => setShowAddStep(false)}
          title="Add node"
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
                background: "#fff",
                boxShadow: "0 20px 60px rgba(0,0,0,0.15)",
                height: "100%",
                overflowY: "auto",
                padding: 28,
                borderRadius: "32px 0 0 32px",
              }}
            >
              <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 24 }}>
                <h3 style={{ fontSize: 17, fontWeight: 900, color: "#1c1917", margin: 0 }}>{t("page.flows.step_config")}</h3>
                <button
                  onClick={() => setShowStepPanel(false)}
                  onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.color = "#1c1917"; }}
                  onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.color = "#a8a29e"; }}
                  style={{ background: "transparent", border: "none", cursor: "pointer", color: "#a8a29e", transition: "color 0.2s", padding: 4 }}
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
                    <p style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: 0 }}>{editingStep.name}</p>
                    <p style={{ fontSize: 11, color: "#a8a29e", margin: 0, textTransform: "capitalize" as const, fontWeight: 600 }}>{editingStep.type}</p>
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
              position: "fixed", top: 74, right: 24, zIndex: 40, width: 300, maxHeight: "72vh",
              display: "flex", flexDirection: "column",
              background: "var(--surface-panel)", borderRadius: 16,
              boxShadow: "0 1px 3px rgba(28,25,23,0.10), 0 12px 32px rgba(28,25,23,0.10)", overflow: "hidden",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "13px 15px" }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-strong)" }}>Runs</div>
              <button
                onClick={() => setShowRunHistory(false)}
                title="Close"
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
                  const color = RUN_STATUS_COLOR[run.status] || "#a8a29e";
                  const stepCount = Object.keys(run.step_results || {}).length;
                  return (
                    <button
                      key={run.id}
                      onClick={() => setRunResult(run)}
                      style={{
                        display: "flex", alignItems: "center", gap: 10, padding: "9px 11px", borderRadius: 10,
                        border: "none", cursor: "pointer", textAlign: "left",
                        background: selected ? "rgba(15,118,110,0.08)" : "var(--surface-muted)",
                        boxShadow: selected ? "inset 0 0 0 1.5px rgba(15,118,110,0.40)" : "none",
                        transition: "background .15s",
                      }}
                    >
                      <span style={{ width: 8, height: 8, borderRadius: "50%", background: color, flexShrink: 0 }} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{ fontSize: 12.5, fontWeight: 600, color: "var(--text-strong)", textTransform: "capitalize" }}>{run.status}</div>
                        <div style={{ fontSize: 11, color: "var(--text-faint)" }}>{fmtRunTime(run.started_at)}</div>
                      </div>
                      <span className="mono" style={{ fontSize: 11, color: "var(--text-faint)" }}>{stepCount}</span>
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
              position: "fixed", top: 74, right: 24, zIndex: 41, width: 320, maxHeight: "72vh",
              display: "flex", flexDirection: "column",
              background: "var(--surface-panel)", borderRadius: 16,
              boxShadow: "0 1px 3px rgba(28,25,23,0.10), 0 12px 32px rgba(28,25,23,0.10)", overflow: "hidden",
            }}
          >
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "13px 15px" }}>
              <div style={{ fontSize: 13, fontWeight: 700, color: "var(--text-strong)" }}>
                Issues <span className="mono" style={{ color: "var(--text-faint)", fontWeight: 600 }}>{issues.length}</span>
              </div>
              <button
                onClick={() => setShowIssues(false)}
                title="Close"
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

        <WorkflowDeployModal
          flow={deployFlow}
          open={!!deployFlow}
          onClose={() => setDeployFlow(null)}
        />

        <WorkflowNodeConfigPanel
          step={(flow.steps || []).find((s: any) => s.id === configStepId) || null}
          lastResult={configStepId ? stepResults[configStepId] : undefined}
          runVariables={lastRun?.variables}
          currentWorkflowId={flow.id}
          nodes={(flow.steps || []).map((s: any) => ({
            id: s.id,
            name: s.name,
            type: s.type,
            targets: [...new Set([
              ...(s.next || []),
              ...(s.true_next || []),
              ...(s.false_next || []),
              ...((Array.isArray(s.config?.cases) ? s.config.cases : []).flatMap((item: any) => item?.next || [])),
              ...(s.config?.default_next || []),
            ].filter(Boolean))] as string[],
            outputs: workflowNodeOutputs(s),
          }))}
          onSave={(updated) =>
            updateMutation.mutate({
              id: flow.id,
              steps: (flow.steps || []).map((s: any) => (s.id === updated.id ? updated : s)),
            })
          }
          onRunResult={recordSingleResult}
          onClose={() => setConfigStepId(null)}
        />
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
        subtitle={t("page.flows.subtitle")}
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
            gridTemplateColumns: "repeat(auto-fill, minmax(min(100%, 260px), 1fr))",
            gap: 24,
          }}>
            {filtered.map((flow: any) => {
              const stepCount = (flow.steps || []).length;
              const flowErrorCount = validateWorkflow(flow.steps || [])
                .filter((issue) => issue.level === "error").length;
              return (
                <CompactCard
                  key={flow.id}
                  icon={
                    <IconTile size={34}>
                      {workflowIconGlyph(flow.icon, 18)}
                    </IconTile>
                  }
                  title={flow.name}
                  subtitle={flow.description || t(TRIGGER_LABELS[flow.trigger] || flow.trigger)}
                  meta={
                    <>
                      <span className="mono" style={{ fontSize: 11, color: "var(--text-faint)", fontWeight: 600 }}>
                        {stepCount}
                      </span>
                      <span
                        title={flow.status === "active" ? t("page.flows.active") : t("page.flows.draft")}
                        style={{
                          width: 7,
                          height: 7,
                          borderRadius: "50%",
                          background: flow.status === "active" ? "#4f9c84" : "#a8a29e",
                        }}
                      />
                    </>
                  }
                  action={
                    <div className="workflow-card-actions">
                      <button
                        className="workflow-card-action-button"
                        onClick={() => { if (flowErrorCount === 0) requestWorkflowRun(flow); }}
                        disabled={streaming || flowErrorCount > 0}
                        title={flowErrorCount > 0 ? "Fix workflow errors before running" : t("page.flows.run")}
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
            <label style={{ display: "block", fontSize: 13, fontWeight: 600, color: "#57534e", marginBottom: 6 }}>{t("page.flows.trigger_type")}</label>
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
