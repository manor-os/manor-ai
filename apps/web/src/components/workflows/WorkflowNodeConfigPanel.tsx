import { useEffect, useId, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { NodeIcon, TYPE_META } from "./WorkflowCanvas";
import Modal from "../ui/Modal";
import Button from "../ui/Button";
import Input from "../ui/Input";
import Textarea from "../ui/Textarea";
import Select from "../ui/Select";
import Toggle from "../ui/Toggle";
import MediaPreview from "./MediaPreview";
import { extractMediaRefs } from "../../lib/workflowMedia";

/* Node detail view — edit a node's config (prompt / model / params), like
   n8n's NDV. Renders type-appropriate fields; unknown types fall back to a
   raw JSON config editor. */

interface Step {
  id: string;
  type: string;
  name?: string;
  config?: Record<string, any>;
  [k: string]: any;
}

type NodeRunResult = {
  status?: string;
  output?: any;
  error?: string;
  cached?: boolean;
  skipped?: boolean;
  inputs?: Record<string, any>;
  step_id?: string;
  duration_ms?: number;
  [key: string]: any;
};

type Field = {
  key: string;
  label: string;
  kind: "text" | "textarea" | "number" | "select" | "boolean" | "json" | "workspace" | "collection" | "workflow_ref" | "person" | "model";
  /** For kind "model": comma-separated catalog roles to offer (e.g. "primary,worker", "image"). Omit = all. */
  modelRole?: string;
  options?: string[];
  hint?: string;
};

// Field keys mirror what packages/core/ai/workflow_runner.py actually reads for
// each node type (the runner is the source of truth). Keep these in sync with
// the AI generator's prompt (packages/core/services/workflow_generator.py).
const SCHEMA: Record<string, Field[]> = {
  llm: [
    { key: "model", label: "Model", kind: "model", modelRole: "primary,worker", hint: "Pick from your model catalog. Blank = the account default." },
    { key: "system_prompt", label: "System prompt", kind: "textarea" },
    { key: "prompt", label: "Prompt", kind: "textarea", hint: "Click an Input parameter, or type { and press Tab to insert it." },
    { key: "temperature", label: "Temperature", kind: "number" },
    { key: "max_rounds", label: "Max rounds", kind: "number" },
  ],
  classifier: [
    { key: "model", label: "Model", kind: "model", modelRole: "primary,worker", hint: "Pick from your model catalog. Blank = the account default." },
    { key: "prompt", label: "Prompt", kind: "textarea", hint: "Runs as a tool-less agent — list the categories in the prompt." },
  ],
  condition: [{ key: "expression", label: "Expression", kind: "text", hint: "Compared with == != > < >= <=. The node's true / false branches set the routes." }],
  http: [
    { key: "method", label: "Method", kind: "select", options: ["GET", "POST", "PUT", "PATCH", "DELETE"] },
    { key: "url", label: "URL", kind: "text" },
    { key: "headers", label: "Headers", kind: "json" },
    { key: "body", label: "Body", kind: "textarea" },
  ],
  rag: [
    { key: "query", label: "Query", kind: "text", hint: "The natural-language question to retrieve relevant docs for." },
    { key: "limit", label: "Max results", kind: "number" },
    { key: "workspace_id", label: "Workspace", kind: "workspace", hint: "Empty = all knowledge; or scope to one workspace." },
    { key: "collection", label: "Collection", kind: "collection", hint: "Optional — restrict to one knowledge collection (document group)." },
  ],
  tool: [
    { key: "tool", label: "Tool name", kind: "text" },
    { key: "args", label: "Arguments", kind: "json" },
  ],
  connector: [
    { key: "tool", label: "Tool / MCP operation", kind: "text", hint: "Resolved tool name (e.g. mcp__slack__post_message)." },
    { key: "args", label: "Arguments", kind: "json" },
  ],
  image: [
    { key: "model", label: "Model", kind: "model", modelRole: "image", hint: "Image model from your catalog. Blank = the account default." },
    { key: "prompt", label: "Prompt", kind: "textarea", hint: "What to generate. Insert a parameter below to use upstream data, e.g. {{scene}}." },
    { key: "size", label: "Size", kind: "select", options: ["1024x1024", "1536x1024", "1024x1536"], hint: "Square · Landscape · Portrait. Default 1024x1024." },
    { key: "quality", label: "Quality", kind: "select", options: ["low", "medium", "high"], hint: "Higher quality takes longer. Default medium." },
    { key: "reference_url", label: "Reference image", kind: "text", hint: "Optional — Knowledge path or URL to edit or use as a style reference. Supports {{var}}." },
  ],
  audio: [
    { key: "model", label: "Model", kind: "model", modelRole: "audio,voice,sfx", hint: "Audio / voice model from your catalog. Blank = the account default." },
    { key: "prompt", label: "Prompt", kind: "textarea" },
  ],
  video: [
    { key: "model", label: "Model", kind: "model", modelRole: "video", hint: "Video model from your catalog. Blank = the account default." },
    { key: "prompt", label: "Prompt", kind: "textarea" },
    { key: "duration", label: "Duration (s)", kind: "select", options: ["4", "5", "6", "8", "10", "12", "15"] },
    { key: "resolution", label: "Resolution", kind: "select", options: ["480p", "720p", "1080p"] },
    { key: "aspect_ratio", label: "Aspect ratio", kind: "select", options: ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"] },
  ],
  media: [
    { key: "kind", label: "Kind", kind: "select", options: ["image", "video", "audio", "document", "presentation"] },
    { key: "prompt", label: "Prompt", kind: "textarea" },
  ],
  code: [
    { key: "language", label: "Language", kind: "select", options: ["python", "javascript", "bash"], hint: "Runs in a new restricted sandbox for every execution." },
    { key: "code", label: "Code", kind: "textarea", hint: "Use the input picker below to insert language-correct access code. Print one JSON value to return structured output." },
    { key: "requirements", label: "Python requirements", kind: "textarea", hint: "Optional pip requirements, one per line. Enable Network access below so the sandbox can install them." },
    { key: "code_timeout", label: "Timeout (s)", kind: "number", hint: "1–300 seconds. Default 60." },
    { key: "output_format", label: "Output format", kind: "select", options: ["auto", "json", "text"], hint: "Auto parses stdout as JSON when possible." },
    { key: "allow_network", label: "Network access", kind: "boolean", hint: "Off by default. Enable only when this code must reach an external service." },
  ],
  loop: [
    { key: "items", label: "Items", kind: "text", hint: "Expression / variable to iterate, e.g. {{panels}}." },
    { key: "item_var", label: "Item variable", kind: "text", hint: "Name each item is bound to (default: item)." },
    { key: "max_iterations", label: "Max iterations", kind: "number" },
    { key: "steps", label: "Sub-steps", kind: "json" },
  ],
  parallel: [{ key: "steps", label: "Branches (sub-steps)", kind: "json" }],
  subworkflow: [{ key: "workflow_id", label: "Workflow", kind: "workflow_ref", hint: "The workflow to run. Its inputs come from the Inputs below; its result (the child's variables) is this step's output — reach a value with {{this_step.var}}." }],
  extract: [
    { key: "input", label: "Source text", kind: "textarea", hint: "Text to extract from. Use {{name}} to pull in an upstream value." },
    { key: "schema", label: "Fields", kind: "textarea", hint: "What to extract — e.g. 'name, email, amount' or a JSON shape. Output is a JSON object." },
    { key: "model", label: "Model", kind: "text" },
  ],
  filter: [
    { key: "items", label: "Items", kind: "text", hint: "A list to filter, e.g. {{leads}}." },
    { key: "item_var", label: "Item variable", kind: "text", hint: "Name each item is bound to in the condition (default: item)." },
    { key: "condition", label: "Condition", kind: "text", hint: "Keep items where this is true, e.g. item.score >= 70. Same operators as IF." },
  ],
  aggregate: [
    { key: "items", label: "Items", kind: "text", hint: "A list to reduce, e.g. {{rows}}." },
    { key: "operation", label: "Operation", kind: "select", options: ["count", "sum", "avg", "min", "max", "join", "first", "last", "collect"] },
    { key: "field", label: "Field", kind: "text", hint: "For a list of objects — which field to aggregate (dotted ok). Blank = the item itself." },
    { key: "separator", label: "Separator", kind: "text", hint: "For join — text between values (default ', ')." },
  ],
  datetime: [
    { key: "operation", label: "Operation", kind: "select", options: ["now", "format", "add", "subtract"] },
    { key: "value", label: "Value", kind: "text", hint: "Base date (ISO). Blank / 'now' = current time. Supports {{var}}." },
    { key: "amount", label: "Amount", kind: "number", hint: "For add / subtract." },
    { key: "unit", label: "Unit", kind: "select", options: ["seconds", "minutes", "hours", "days", "weeks"] },
    { key: "format", label: "Format", kind: "text", hint: "strftime pattern, e.g. %Y-%m-%d. Blank = ISO 8601." },
  ],
  split: [
    { key: "items", label: "Items", kind: "text", hint: "A list (e.g. {{tags}}) or a delimited string to split into separate values." },
    { key: "field", label: "Field", kind: "text", hint: "For a list of objects — pluck (and flatten) this field from each." },
    { key: "separator", label: "Separator", kind: "text", hint: "For a string — split on this, e.g. ','." },
  ],
  limit: [
    { key: "items", label: "Items", kind: "text", hint: "A list to cap, e.g. {{rows}}." },
    { key: "max", label: "Max items", kind: "number", hint: "How many to keep." },
    { key: "keep", label: "Keep", kind: "select", options: ["first", "last"] },
  ],
  respond: [
    { key: "body", label: "Response body", kind: "textarea", hint: "Returned to the webhook caller. JSON ({...}) is sent as JSON; supports {{var}}." },
    { key: "status_code", label: "Status code", kind: "number", hint: "HTTP status (default 200)." },
  ],
  sort: [
    { key: "items", label: "Items", kind: "text", hint: "A list to sort, e.g. {{rows}}." },
    { key: "field", label: "Field", kind: "text", hint: "For a list of objects — sort by this field (dotted ok). Blank = the item itself." },
    { key: "order", label: "Order", kind: "select", options: ["asc", "desc"] },
  ],
  dedupe: [
    { key: "items", label: "Items", kind: "text", hint: "A list to de-duplicate, e.g. {{rows}}." },
    { key: "field", label: "Field", kind: "text", hint: "Identity field (dotted ok). Blank = the whole item." },
  ],
  stop: [
    { key: "message", label: "Error message", kind: "text", hint: "Why the workflow stops. Supports {{var}}." },
  ],
  extractfromfile: [
    { key: "input", label: "Content", kind: "textarea", hint: "Text to parse, e.g. {{http.body}} from a download." },
    { key: "format", label: "Format", kind: "select", options: ["auto", "json", "csv"] },
  ],
  transform: [{ key: "set", label: "Set variables", kind: "json", hint: '{ "var": "value or {{expr}}" }' }],
  switch: [
    { key: "cases", label: "Cases", kind: "json", hint: '[{ "expression": "status == \\"vip\\"", "next": ["stepId"] }]' },
    { key: "default_next", label: "Default → (step ids)", kind: "json" },
  ],
  merge: [
    { key: "sources", label: "Sources (step ids)", kind: "json" },
    { key: "mode", label: "Mode", kind: "select", options: ["list", "object"] },
  ],
  wait: [
    { key: "wait_type", label: "Wait type", kind: "select", options: ["approval", "timer", "event"] },
    { key: "duration_seconds", label: "Duration (s)", kind: "number", hint: "For timer waits — ≤90s runs inline; longer waits pause and resume automatically." },
    { key: "message", label: "Message", kind: "text", hint: "Shown while approval / event waits are paused." },
  ],
  notify: [
    { key: "channel", label: "Channel", kind: "text" },
    { key: "person_id", label: "Notify person", kind: "person", hint: "Optional — a team member to notify." },
    { key: "message", label: "Message", kind: "textarea" },
  ],
};

// Entry / terminal markers — no parameters, just a name. (Triggering is
// configured on the workflow's deployment, not the node.)
const NO_CONFIG_TYPES = new Set(["trigger", "webhook", "end"]);

function meta(t: string) {
  return TYPE_META[t] || { color: "#9b938c", label: t.toUpperCase() };
}

function StageOperationsSummary({ config }: { config: Record<string, any> }) {
  const operations = Array.isArray(config.operations) ? config.operations : [];
  const routes = config.routes && typeof config.routes === "object"
    ? Object.entries(config.routes as Record<string, string | null>)
    : [];

  return (
    <section aria-labelledby="workflow-stage-operations-title" style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) auto", gap: 16, alignItems: "end" }}>
        <div style={{ minWidth: 0 }}>
          <span style={{ display: "block", fontSize: 11, color: "var(--text-faint)", marginBottom: 4 }}>Entry operation</span>
          <code className="mono" style={{ display: "block", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 12, color: "var(--text-strong)" }}>
            {String(config.entry_operation_id || "-")}
          </code>
        </div>
        <div style={{ textAlign: "right" }}>
          <span style={{ display: "block", fontSize: 11, color: "var(--text-faint)", marginBottom: 4 }}>Operations</span>
          <span className="mono" style={{ fontSize: 13, fontWeight: 700, color: "var(--text-strong)" }}>{operations.length}</span>
        </div>
      </div>

      <div>
        <h3 id="workflow-stage-operations-title" style={{ margin: "0 0 8px", fontSize: 12, fontWeight: 700, color: "var(--text-strong)" }}>Operations</h3>
        <ol style={{ listStyle: "none", margin: 0, padding: 0, maxHeight: 340, overflowY: "auto", background: "var(--surface-muted)", borderRadius: "var(--radius-control)" }}>
          {operations.map((operation: Record<string, any>, index: number) => {
            const operationType = String(operation.type || "node");
            return (
              <li key={String(operation.id || index)} style={{ display: "grid", gridTemplateColumns: "24px 28px minmax(0, 1fr) auto", alignItems: "center", gap: 8, minHeight: 44, padding: "5px 10px", borderBottom: index + 1 < operations.length ? "1px solid rgba(28,25,23,0.06)" : "none" }}>
                <span className="mono" style={{ fontSize: 10.5, color: "var(--text-faint)", textAlign: "right" }}>{index + 1}</span>
                <span style={{ display: "inline-flex", alignItems: "center", justifyContent: "center", color: "var(--text-muted)" }}><NodeIcon type={operationType} size={15} /></span>
                <span style={{ minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 12, fontWeight: 600, color: "var(--text-default)" }}>{String(operation.name || operation.id || "Operation")}</span>
                <span className="mono" style={{ fontSize: 9.5, color: "var(--text-faint)" }}>{meta(operationType).label}</span>
              </li>
            );
          })}
        </ol>
      </div>

      <div>
        <h3 style={{ margin: "0 0 8px", fontSize: 12, fontWeight: 700, color: "var(--text-strong)" }}>Routes</h3>
        <dl style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) minmax(0, 1fr)", gap: "6px 14px", margin: 0 }}>
          {routes.map(([route, target]) => (
            <div key={route} style={{ display: "contents" }}>
              <dt className="mono" style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 10.5, color: "var(--text-muted)" }}>{route}</dt>
              <dd className="mono" style={{ margin: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap", fontSize: 10.5, color: target ? "var(--text-strong)" : "var(--text-faint)" }}>{target || "End"}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}

export default function WorkflowNodeConfigPanel({
  step,
  lastResult,
  runVariables,
  nodes,
  currentWorkflowId,
  onSave,
  onRunResult,
  onClose,
}: {
  step: Step | null;
  lastResult?: NodeRunResult | undefined;
  runVariables?: Record<string, any>;
  nodes?: WorkflowNodeRef[];
  currentWorkflowId?: string;
  onSave: (updated: Step) => void;
  onRunResult?: (stepId: string, result: NodeRunResult) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState(step?.name || "");
  const [config, setConfig] = useState<Record<string, any>>(step?.config || {});
  const [rawErr, setRawErr] = useState("");
  const [jsonErrs, setJsonErrs] = useState<Record<string, string>>({});
  const [liveResult, setLiveResult] = useState<NodeRunResult | null>(null);
  const [running, setRunning] = useState(false);
  const [rawRun, setRawRun] = useState(false); // Friendly (default) vs raw-JSON run view
  const [testInputValues, setTestInputValues] = useState<Record<string, string>>({});
  const [testInputErrors, setTestInputErrors] = useState<Record<string, string>>({});
  const [forId, setForId] = useState(step?.id);
  const resultRef = useRef<HTMLDivElement>(null);
  const navigate = useNavigate();
  const close = () => {
    // Force a fresh draft next time, even when the same node reopens. Cancelled
    // config edits and ephemeral test inputs must not leak into the next panel.
    setForId(undefined);
    onClose();
  };
  // close the panel, then jump to a bound resource's page
  const goResource = (to: string) => { close(); navigate(to); };

  // re-init when a different node opens
  if (step && step.id !== forId) {
    setForId(step.id);
    setName(step.name || "");
    setConfig(step.config || {});
    setRawErr("");
    setJsonErrs({});
    setLiveResult(null);
    setRunning(false);
    setRawRun(false);
    setTestInputValues({});
    setTestInputErrors({});
  }

  const effectiveResult = liveResult || lastResult;
  useEffect(() => {
    if (running || !effectiveResult?.status || !resultRef.current) return;
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    resultRef.current.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "nearest" });
  }, [running, effectiveResult?.status, effectiveResult?.output, effectiveResult?.error]);

  // Pickers refetch on open (staleTime 0) so newly-created agents / skills /
  // integrations / workspaces show up without a page reload.
  const { data: agents = [] } = useQuery({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
    enabled: step?.type === "agent",
    staleTime: 0,
  });
  // Ready-made marketplace agents — so a workspace with no agents of its own
  // can still pick one to run.
  const { data: marketplaceAgents = [] } = useQuery({
    queryKey: ["agents", "marketplace"],
    queryFn: () => api.agents.marketplace(),
    enabled: step?.type === "agent",
    staleTime: 60_000,
  });
  const { data: skills = [] } = useQuery({
    queryKey: ["skills"],
    queryFn: () => api.skills.list(),
    enabled: step?.type === "agent",
    staleTime: 0,
  });
  const { data: integrations = [] } = useQuery({
    queryKey: ["integrations"],
    queryFn: () => api.integrations.list(),
    enabled: step?.type === "connector",
    staleTime: 0,
  });
  const { data: workspaces = [] } = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api.workspaces.list(),
    enabled: step?.type === "rag",
    staleTime: 0,
  });
  const { data: groups = [] } = useQuery({
    queryKey: ["document-groups"],
    queryFn: () => api.documents.listGroups(),
    enabled: step?.type === "rag",
    staleTime: 0,
  });
  const { data: allWorkflows = [] } = useQuery({
    queryKey: ["workflows"],
    queryFn: () => api.workflows.list(),
    enabled: step?.type === "subworkflow",
    staleTime: 0,
  });
  const { data: people = [] } = useQuery({
    queryKey: ["people-directory"],
    queryFn: () => api.people.directory(),
    enabled: step?.type === "notify",
    staleTime: 0,
  });
  // Model catalog for the model pickers (chat / image / video / audio nodes).
  const { data: modelCatalog } = useQuery({
    queryKey: ["model-catalog"],
    queryFn: () => api.auth.getModelCatalog(),
    enabled: ["llm", "classifier", "agent", "extract", "image", "video", "audio", "media"].includes(step?.type || ""),
    staleTime: 60_000,
  });
  // Build a deduped option list for one or more catalog roles (comma-separated
  // in Field.modelRole, e.g. "primary,worker" for chat or "image" for images).
  // No role → every model across the catalog.
  const catalogByRole = (modelCatalog as { catalog?: Record<string, { id: string; name?: string }[]> } | undefined)?.catalog || {};
  const modelOptionsForRole = (modelRole?: string): { value: string; label: string }[] => {
    const roles = modelRole ? modelRole.split(",").map((r) => r.trim()) : Object.keys(catalogByRole);
    const seen = new Set<string>();
    const out: { value: string; label: string }[] = [];
    for (const role of roles) {
      for (const m of catalogByRole[role] || []) {
        if (m?.id && !seen.has(m.id)) { seen.add(m.id); out.push({ value: m.id, label: m.name || m.id }); }
      }
    }
    return out;
  };

  if (!step) return null;
  const m = meta(step.type);
  const fields = SCHEMA[step.type];
  const setKey = (k: string, v: any) => setConfig((c) => ({ ...c, [k]: v }));
  const isEntryNode = ["trigger", "webhook"].includes(step.type);
  const entryInputRows: WorkflowRunInputBinding[] = Array.isArray(config.run_inputs)
    ? config.run_inputs
    : Array.isArray(config.inputs)
      ? config.inputs
      : [];
  const entryOutputRows = workflowEntryOutputs(step.id, entryInputRows);
  const promptInputNames = [...new Set(
    (Array.isArray(config.inputs) ? config.inputs : [])
      .map((item: any) => String(item?.key || item?.name || "").trim())
      .filter(Boolean),
  )];
  const configuredTestInputs: Binding[] = (Array.isArray(config.inputs) ? config.inputs : [])
    .filter((item: Binding) => String(item?.key || "").trim());
  const testInputValue = (input: Binding): string => {
    const key = String(input.key || "").trim();
    if (Object.prototype.hasOwnProperty.call(testInputValues, key)) return testInputValues[key];
    const previousValue = lastResult?.inputs?.[key];
    if (previousValue !== undefined) return formatTestInputValue(previousValue);
    return formatTestInputValue(resolveTestInputDefault(input.value, runVariables));
  };

  // Strip transient raw-JSON buffers from the live config.
  const cleanConfig = (): Record<string, any> => {
    const clean: Record<string, any> = {};
    for (const [k, v] of Object.entries(config)) {
      if (k.startsWith("__raw_")) continue;
      clean[k] = (k === "inputs" || k === "outputs" || k === "run_inputs") && Array.isArray(v)
        ? v.map((row) => Object.fromEntries(
            Object.entries(row || {}).filter(([rowKey]) => !rowKey.startsWith("__")),
          ))
        : v;
    }
    return clean;
  };

  const save = () => {
    onSave({ ...step!, name, config: cleanConfig() });
    close();
  };

  // Test this node with explicit, ephemeral input values. Test values replace
  // the saved mappings only in this request; Save still persists cleanConfig().
  const runNode = async () => {
    if (!step || running) return;
    const cleaned = cleanConfig();
    const inputs: Binding[] = Array.isArray(cleaned.inputs) ? cleaned.inputs : [];
    const errors: Record<string, string> = {};
    const testValues: Record<string, string> = {};
    const testBindings = inputs.map((input) => {
      const key = String(input.key || "").trim();
      if (!key) return input;
      const rawValue = testInputValue(input);
      testValues[key] = rawValue;
      if (!rawValue.trim()) {
        errors[key] = "Provide a value before testing this node.";
        return input;
      }
      if (input.type === "json") {
        try {
          return { ...input, value: JSON.parse(rawValue) };
        } catch {
          errors[key] = "Enter valid JSON.";
          return input;
        }
      }
      if (input.type === "number" && !Number.isFinite(Number(rawValue))) {
        errors[key] = "Enter a valid number.";
      }
      return { ...input, value: rawValue };
    });
    setTestInputValues((current) => ({ ...testValues, ...current }));
    setTestInputErrors(errors);
    if (Object.keys(errors).length) return;

    setRunning(true);
    setLiveResult(null);
    onRunResult?.(step.id, { status: "running" });
    try {
      const res = await api.workflows.runNode({
        ...step,
        name,
        config: { ...cleaned, inputs: testBindings.length ? testBindings : undefined },
      }, runVariables);
      setLiveResult(res);
      onRunResult?.(step.id, res);
    } catch (e: any) {
      const failed = { status: "failed", error: e?.message || "Run failed" };
      setLiveResult(failed);
      onRunResult?.(step.id, failed);
    } finally {
      setRunning(false);
    }
  };

  return (
    <Modal
      open={!!step}
      onClose={close}
      title={`${m.label} node`}
      maxWidth="960px"
      footer={
        <div style={{ display: "flex", gap: 8, justifyContent: "space-between", alignItems: "center", width: "100%" }}>
          <Button
            variant="outline"
            onClick={runNode}
            disabled={running || step.type === "stage" || !!rawErr || Object.keys(jsonErrs).length > 0}
          >
            {running ? "Testing…" : "Test node"}
          </Button>
          <div style={{ display: "flex", gap: 8 }}>
            <Button variant="ghost" onClick={close}>Cancel</Button>
            <Button onClick={save} disabled={!!rawErr || Object.keys(jsonErrs).length > 0}>Save</Button>
          </div>
        </div>
      }
    >
      <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
        <Field label="Name">
          <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="Node name" />
        </Field>

        <div className="workflow-node-config-layout">
          <ExecutionResultPanel
            resultRef={resultRef}
            running={running}
            result={effectiveResult}
            isLive={!!liveResult}
            raw={rawRun}
            stepType={step.type}
            onToggleRaw={() => setRawRun((value) => !value)}
            testInputs={configuredTestInputs.map((input) => {
              const key = String(input.key || "").trim();
              return {
                key,
                type: input.type || "any",
                source: String(input.value || ""),
                value: testInputValue(input),
                error: testInputErrors[key],
                onChange: (value: string) => {
                  setTestInputValues((current) => ({ ...current, [key]: value }));
                  setTestInputErrors((current) => {
                    if (!current[key]) return current;
                    const next = { ...current };
                    delete next[key];
                    return next;
                  });
                },
              };
            })}
          />
          <div className="workflow-node-config-fields">

        {step.type === "unsupported" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4, padding: "9px 11px", borderRadius: 8, background: "rgba(168,162,158,0.12)" }}>
            <span style={{ fontSize: 11.5, fontWeight: 600, color: "var(--text-strong)" }}>Unsupported node — skipped at run time</span>
            <span style={{ fontSize: 11, color: "var(--text-muted)", lineHeight: 1.5 }}>
              {step.meta?.original_type
                ? <>Imported from {step.meta.source_tool || "an external tool"} as <code className="mono">{step.meta.original_type}</code>, which has no manor equivalent. The run passes over it. Replace it with a supported node (e.g. http / connector / code) to make it executable.</>
                : <>This node type isn't supported — the run skips it. Replace it with a supported node to make it executable.</>}
            </span>
          </div>
        )}

        {step.type === "stage" ? (
          <StageOperationsSummary config={config} />
        ) : step.type === "agent" ? (
          <>
            <Field
              label="Run as agent"
              hint={(agents as unknown[]).length
                ? "Pick one of your agents — it brings its own model, tools and system prompt. Leave on “None” to configure inline below."
                : "No agents of your own yet — pick a ready-made one below (or create your own on the Agents page). Or leave on “None” and configure inline."}
            >
              <Select
                value={String(config.agent_id ?? "")}
                onChange={(v) => setKey("agent_id", v || undefined)}
                placeholder="Select an agent…"
                options={[
                  { value: "", label: "— None (configure inline) —" },
                  ...(agents as { id: string; name: string }[]).map((a) => ({ value: a.id, label: a.name })),
                  ...(marketplaceAgents as { id: string; name: string }[])
                    .filter((m) => !(agents as { id: string }[]).some((a) => a.id === m.id))
                    .map((m) => ({ value: m.id, label: `${m.name} · marketplace` })),
                ]}
              />
              {config.agent_id && <OpenLink label="Open agent" onClick={() => goResource(`/agents/${config.agent_id}`)} />}
            </Field>
            <Field label="…or run a Skill" hint="If set, the skill runs instead of the agent.">
              <Select
                value={String(config.skill ?? "")}
                onChange={(v) => setKey("skill", v || undefined)}
                placeholder="No skill"
                options={[
                  { value: "", label: "No skill" },
                  ...(skills as { id: string; slug?: string; name: string; display_name?: string }[]).map((s) => ({
                    value: s.slug || s.id,
                    label: s.display_name || s.name,
                  })),
                ]}
              />
            </Field>
            <Field label="System message" hint="The agent's role / objective — its standing instructions.">
              <Textarea value={String(config.system_prompt ?? "")} onChange={(e) => setKey("system_prompt", e.target.value || undefined)} rows={3} placeholder="e.g. You are a Gmail labelling agent…" />
            </Field>
            <Field label="Prompt (user message)" hint="Click an Input parameter, or type { and press Tab to insert it.">
              <PromptInputTextarea
                value={String(config.prompt ?? config.input ?? "")}
                onChange={(value) => setKey("prompt", value)}
                inputNames={promptInputNames}
                rows={3}
              />
            </Field>
            <Field label="Model" hint="LLM the agent runs on, from your model catalog. Blank = the selected agent's default.">
              {(() => {
                const opts = modelOptionsForRole("primary,worker");
                return (
                  <>
                    <Select
                      value={String(config.model ?? "")}
                      onChange={(v) => setKey("model", v || undefined)}
                      placeholder={opts.length ? "— Agent default —" : "Loading models…"}
                      options={[
                        { value: "", label: "— Agent default —" },
                        ...opts,
                        ...(config.model && !opts.some((o) => o.value === config.model)
                          ? [{ value: String(config.model), label: `${config.model} (custom)` }]
                          : []),
                      ]}
                    />
                    <OpenLink label="Manage models" onClick={() => goResource("/account?tab=models")} />
                  </>
                );
              })()}
            </Field>
            <Field label="Tools" hint="Tools / connectors the agent may call. Imported from its attached tool nodes.">
              {Array.isArray(config.tools) && config.tools.length > 0 ? (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
                  {(config.tools as any[]).map((t, i) => (
                    <span key={i} style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 11.5, padding: "3px 6px 3px 10px", borderRadius: 999, background: "var(--surface-muted)", color: "var(--text-strong)" }}>
                      {String(t)}
                      <button
                        type="button"
                        aria-label={`Remove ${t}`}
                        onClick={() => setKey("tools", (config.tools as any[]).filter((_, j) => j !== i))}
                        style={{ border: "none", background: "transparent", cursor: "pointer", color: "var(--text-faint)", fontSize: 13, lineHeight: 1, padding: 0 }}
                      >×</button>
                    </span>
                  ))}
                </div>
              ) : (
                <span style={{ fontSize: 11.5, color: "var(--text-faint)" }}>No tools — the agent uses its default toolset.</span>
              )}
            </Field>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
              <span style={{ fontSize: 13, color: "var(--text-strong)" }}>Memory</span>
              <Toggle
                checked={!!config.memory}
                onChange={() => setConfig({ ...config, memory: config.memory ? undefined : true })}
                aria-label="Memory"
              />
            </div>
            <Field label="Max iterations" hint="Most tool-call rounds before the agent stops. Default 5.">
              <Input value={String(config.max_rounds ?? config.max_iterations ?? "")} onChange={(e) => setKey("max_rounds", e.target.value === "" ? undefined : Number(e.target.value))} placeholder="5" type="number" />
            </Field>
            {config.agent_id && !config.model && !(Array.isArray(config.tools) && config.tools.length) && (
              <p style={{ fontSize: 12, color: "var(--text-faint)", margin: 0 }}>
                Leave Model / Tools blank to inherit them from the selected agent.
              </p>
            )}
          </>
        ) : step.type === "connector" ? (
          (() => {
            // Compose mcp__<server>__<operation> from an integration + operation.
            const tool = String(config.tool ?? "");
            const parts = tool.startsWith("mcp__") ? tool.split("__") : [];
            const server = parts[1] || "";
            const operation = parts.slice(2).join("__") || "";
            const setTool = (srv: string, op: string) =>
              setKey("tool", srv ? `mcp__${srv}__${op}` : "");
            const list = (integrations as any[]).map((i) => {
              const key = i.server_key || i.provider || i.key || i.id;
              return { value: String(key), label: String(i.name || i.display_name || key) };
            });
            return (
              <>
                <Field label="Integration" hint="Resolved against this entity / workspace's connected accounts.">
                  <Select
                    value={server}
                    onChange={(v) => setTool(v, operation)}
                    placeholder={list.length ? "Select an integration…" : "No integrations connected"}
                    options={list}
                  />
                  <OpenLink label={list.length ? "Manage integrations" : "Connect an integration"} onClick={() => goResource("/integrations")} />
                </Field>
                <Field label="Operation" hint="e.g. post_message, create_issue — composes mcp__<server>__<operation>.">
                  <Input value={operation} onChange={(e) => setTool(server, e.target.value)} placeholder="operation" />
                </Field>
                {tool && <p className="mono" style={{ fontSize: 11, color: "var(--text-faint)", margin: 0 }}>{tool}</p>}
                <JsonField fieldKey="args" label="Arguments" config={config} setConfig={setConfig} jsonErrs={jsonErrs} setJsonErrs={setJsonErrs} />
              </>
            );
          })()
        ) : fields ? (
          <>
          {(step.type === "condition" || step.type === "switch") && <ExpressionHelp />}
          {(step.type === "image" || step.type === "video" || step.type === "audio" || step.type === "media") && (
            <div style={{ fontSize: 11, color: "var(--text-faint)", lineHeight: 1.5, padding: "8px 10px", borderRadius: 8, background: "var(--surface-muted)" }}>
              Choose a model from <b>Account → Models</b> for this node, or leave it blank to use the account default.
            </div>
          )}
          {fields.map((f) => (
            <Field key={f.key} label={f.label} hint={f.hint}>
              {f.kind === "textarea" ? (
                f.key === "prompt" ? (
                  <PromptInputTextarea
                    value={String(config[f.key] ?? "")}
                    onChange={(value) => setKey(f.key, value)}
                    inputNames={promptInputNames}
                    rows={4}
                  />
                ) : f.key === "code" ? (
                  <CodeInputTextarea
                    value={String(config[f.key] ?? "")}
                    onChange={(value) => setKey(f.key, value)}
                    inputNames={promptInputNames}
                    language={String(config.language || "python")}
                    rows={8}
                  />
                ) : (
                  <Textarea value={String(config[f.key] ?? "")} onChange={(e) => setKey(f.key, e.target.value)} rows={4} />
                )
              ) : f.kind === "json" ? (
                <>
                  <Textarea
                    value={
                      jsonErrs[f.key] !== undefined && (config as any)[`__raw_${f.key}`] !== undefined
                        ? (config as any)[`__raw_${f.key}`]
                        : config[f.key] === undefined
                          ? ""
                          : JSON.stringify(config[f.key], null, 2)
                    }
                    onChange={(e) => {
                      const text = e.target.value;
                      if (text.trim() === "") {
                        setConfig((c) => { const n = { ...c }; delete n[f.key]; delete (n as any)[`__raw_${f.key}`]; return n; });
                        setJsonErrs((j) => { const n = { ...j }; delete n[f.key]; return n; });
                        return;
                      }
                      try {
                        const parsed = JSON.parse(text);
                        setConfig((c) => { const n = { ...c, [f.key]: parsed }; delete (n as any)[`__raw_${f.key}`]; return n; });
                        setJsonErrs((j) => { const n = { ...j }; delete n[f.key]; return n; });
                      } catch {
                        setConfig((c) => ({ ...c, [`__raw_${f.key}`]: text }));
                        setJsonErrs((j) => ({ ...j, [f.key]: "Invalid JSON" }));
                      }
                    }}
                    rows={4}
                  />
                  {jsonErrs[f.key] && <span style={{ fontSize: 12, color: "#d65f59" }}>{jsonErrs[f.key]}</span>}
                </>
              ) : f.kind === "select" ? (
                <Select value={String(config[f.key] ?? "")} onChange={(v) => setKey(f.key, v)} placeholder="—" options={f.options || []} />
              ) : f.kind === "boolean" ? (
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", minHeight: 32 }}>
                  <span style={{ fontSize: 12, color: "var(--text-faint)" }}>{config[f.key] ? "Enabled" : "Disabled"}</span>
                  <Toggle
                    checked={!!config[f.key]}
                    onChange={() => setKey(f.key, config[f.key] ? undefined : true)}
                    aria-label={f.label}
                  />
                </div>
              ) : f.kind === "workspace" ? (
                <>
                  <Select
                    value={String(config[f.key] ?? "")}
                    onChange={(v) => setKey(f.key, v || undefined)}
                    placeholder="All knowledge"
                    options={[
                      { value: "", label: "All knowledge" },
                      ...(workspaces as { id: string; name: string }[]).map((w) => ({ value: w.id, label: w.name })),
                    ]}
                  />
                  {config[f.key] && <OpenLink label="Open workspace" onClick={() => goResource(`/workspaces/${config[f.key]}`)} />}
                </>
              ) : f.kind === "collection" ? (
                <>
                  <Select
                    value={String((config.group_ids as string[] | undefined)?.[0] ?? "")}
                    onChange={(v) => setKey("group_ids", v ? [v] : undefined)}
                    placeholder="All collections"
                    options={[
                      { value: "", label: "All collections" },
                      ...(groups as { id: string; name: string }[]).map((g) => ({ value: g.id, label: g.name })),
                    ]}
                  />
                  {(config.group_ids as string[] | undefined)?.[0] && <OpenLink label="Open knowledge" onClick={() => goResource("/knowledge")} />}
                </>
              ) : f.kind === "person" ? (
                <>
                  <Select
                    value={String(config[f.key] ?? "")}
                    onChange={(v) => setKey(f.key, v || undefined)}
                    placeholder="No one"
                    options={[
                      { value: "", label: "No one" },
                      ...(people as { id: string; display_name?: string; email: string }[]).map((p) => ({ value: p.id, label: p.display_name || p.email })),
                    ]}
                  />
                  {config[f.key] && <OpenLink label="Open team" onClick={() => goResource("/team")} />}
                </>
              ) : f.kind === "model" ? (
                (() => {
                  const opts = modelOptionsForRole(f.modelRole);
                  return (
                    <>
                      <Select
                        value={String(config[f.key] ?? "")}
                        onChange={(v) => setKey(f.key, v || undefined)}
                        placeholder={opts.length ? "— Account default —" : "Loading models…"}
                        options={[
                          { value: "", label: "— Account default —" },
                          ...opts,
                          ...(config[f.key] && !opts.some((o) => o.value === config[f.key])
                            ? [{ value: String(config[f.key]), label: `${config[f.key]} (custom)` }]
                            : []),
                        ]}
                      />
                      <OpenLink label="Manage models" onClick={() => goResource("/account?tab=models")} />
                    </>
                  );
                })()
              ) : f.kind === "workflow_ref" ? (
                <Select
                  value={String(config[f.key] ?? "")}
                  onChange={(v) => setKey(f.key, v || undefined)}
                  placeholder={allWorkflows.length ? "Select a workflow…" : "No other workflows"}
                  options={(allWorkflows as { id: string; name: string }[])
                    .filter((w) => w.id !== currentWorkflowId)
                    .map((w) => ({ value: w.id, label: w.name }))}
                />
              ) : (
                <Input
                  value={String(config[f.key] ?? "")}
                  onChange={(e) => setKey(f.key, f.kind === "number" ? (e.target.value === "" ? "" : Number(e.target.value)) : e.target.value)}
                  placeholder={f.kind === "number" ? "0" : ""}
                />
              )}
            </Field>
          ))}
          </>
        ) : NO_CONFIG_TYPES.has(step.type) ? (
          <p style={{ fontSize: 12.5, color: "var(--text-faint)", margin: 0, lineHeight: 1.5 }}>
            {step.type === "end" ? "Terminal node — ends this branch. No parameters." : "Entry node — starts the workflow. Triggering is set on the deployment."}
          </p>
        ) : step.type === "unsupported" ? null : (
          <Field label="Config (JSON)">
            <Textarea
              value={JSON.stringify(config, null, 2)}
              onChange={(e) => {
                try { setConfig(JSON.parse(e.target.value || "{}")); setRawErr(""); }
                catch { setRawErr("Invalid JSON"); }
              }}
              rows={8}
            />
            {rawErr && <span style={{ fontSize: 12, color: "#d65f59" }}>{rawErr}</span>}
          </Field>
        )}

        {/* Data flow: define this step's named inputs (mapped from upstream
            values) and named outputs (fields of its result). The step's whole
            result is always auto-available as {{<id>}}. */}
        {step.type !== "end" && step.type !== "unsupported" && step.type !== "stage" && (() => {
          const upstream = connectedUpstreamNodes(nodes || [], step.id);
          const inputRows: Binding[] = Array.isArray(config.inputs) ? (config.inputs as Binding[]) : [];
          const outputRows: Binding[] = Array.isArray(config.outputs)
            ? (config.outputs as Binding[])
            : isEntryNode
              ? entryOutputRows
            : config.output_var
              ? [{ key: String(config.output_var), value: "" }]
              : [];
          const setInputs = (rows: Binding[]) => setKey("inputs", rows.length ? rows : undefined);
          const setRunInputs = (rows: WorkflowRunInputBinding[]) => setConfig((current) => {
            const previousRows = Array.isArray(current.run_inputs)
              ? current.run_inputs as WorkflowRunInputBinding[]
              : [];
            const outputsFollowInputs = !Array.isArray(current.outputs)
              || workflowEntryOutputsMatch(step.id, previousRows, current.outputs as Binding[]);
            return {
              ...current,
              run_inputs: rows.length ? rows : [],
              ...(outputsFollowInputs ? { outputs: workflowEntryOutputs(step.id, rows) } : {}),
            };
          });
          const setOutputs = (rows: Binding[]) =>
            setConfig({
              ...config,
              outputs: isEntryNode ? rows : (rows.length ? rows : undefined),
              output_var: undefined,
            });
          // Map every upstream named output to its declared type so a typed
          // input can be checked against the output feeding it (ComfyUI-style).
          const outTypeByRef: Record<string, string> = {};
          const inputSources: BindingValueOption[] = [];
          const seenSources = new Set<string>();
          for (const n of upstream) {
            const nodeLabel = n.name || n.type;
            const wholeRef = `{{${n.id}}}`;
            if (!seenSources.has(wholeRef)) {
              seenSources.add(wholeRef);
              inputSources.push({ value: wholeRef, label: `${nodeLabel} · Entire output`, type: "any" });
            }
            for (const o of n.outputs || []) {
              if (!o.name) continue;
              const namedRef = `{{${o.name}}}`;
              if (seenSources.has(namedRef)) continue;
              seenSources.add(namedRef);
              inputSources.push({ value: namedRef, label: `${nodeLabel} · ${o.name}`, type: o.type || "any" });
            }
          }
          for (const source of inputSources) outTypeByRef[source.value] = source.type || "any";
          const validateInput = (r: Binding): string | undefined => {
            const t = r.type || "any";
            if (t === "any") return undefined;
            const srcT = outTypeByRef[String(r.value || "").trim()];
            return srcT && srcT !== "any" && srcT !== t ? `upstream is ${srcT}, this expects ${t}` : undefined;
          };
          return (
            <div style={{ display: "flex", flexDirection: "column", gap: 14, paddingTop: 8, borderTop: "1px solid rgba(28,25,23,0.06)" }}>
              <FieldGroup
                label="Inputs"
                hint={isEntryNode
                  ? undefined
                  : "Name the data this step needs, mapped from an upstream value. Pick a type to coerce + validate it. Reference an input as {{name}} in the fields above."}
              >
                {isEntryNode ? (
                  <WorkflowRunInputRows rows={entryInputRows} onChange={setRunInputs} />
                ) : (
                  <BindingRows
                    rows={inputRows}
                    onChange={setInputs}
                    keyPlaceholder="name"
                    valuePlaceholder="{{step_id}} or a literal"
                    addLabel="Add input"
                    validate={validateInput}
                    valueOptions={inputSources}
                  />
                )}
              </FieldGroup>
              <FieldGroup
                label="Outputs"
                hint={isEntryNode
                  ? undefined
                  : `This step's whole result is always available as {{${step.id}}}. Add named, typed outputs to expose specific fields downstream.`}
              >
                <BindingRows
                  rows={outputRows}
                  onChange={setOutputs}
                  keyPlaceholder="name"
                  valuePlaceholder={`{{${step.id}}} or {{${step.id}.field}}`}
                  addLabel="Add output"
                />
              </FieldGroup>
            </div>
          );
        })()}

        {/* Node settings — n8n-style execution controls: error handling,
            retry, and a free-text note. These tune how the step runs. */}
        {step.type !== "trigger" && step.type !== "webhook" && step.type !== "unsupported" && step.type !== "stage" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 12, paddingTop: 8, borderTop: "1px solid rgba(28,25,23,0.06)" }}>
            <span style={{ fontSize: 11, fontWeight: 500, letterSpacing: 0.2, color: "var(--text-faint)" }}>Settings</span>
            <Field label="On error" hint="Stop the whole run, or skip this step and continue.">
              <Select
                value={String(config.on_error ?? "stop")}
                onChange={(v) => setKey("on_error", v === "stop" ? undefined : v)}
                options={[
                  { value: "stop", label: "Stop workflow" },
                  { value: "continue", label: "Continue (skip on error)" },
                ]}
              />
            </Field>
            <Field label="Cache" hint="Reuse this step's last result on a re-run when its inputs are unchanged — ComfyUI-style, so iterating on a later node skips re-running this one.">
              <Select
                value={String(config.cache_policy ?? "auto")}
                onChange={(v) => setKey("cache_policy", v === "auto" ? undefined : v)}
                options={[
                  { value: "auto", label: "Auto (pure steps only)" },
                  { value: "cache", label: "Reuse if inputs unchanged" },
                  { value: "never", label: "Always re-run" },
                ]}
              />
            </Field>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
              <span style={{ fontSize: 13, color: "var(--text-strong)" }}>Retry on fail</span>
              <Toggle
                checked={!!config.retry_on_fail}
                onChange={() => setConfig({ ...config, retry_on_fail: config.retry_on_fail ? undefined : true })}
                aria-label="Retry on fail"
              />
            </div>
            {config.retry_on_fail && (
              <Field label="Max tries" hint="Including the first attempt (max 5). Default 3.">
                <Input
                  value={String(config.max_tries ?? "")}
                  onChange={(e) => setKey("max_tries", e.target.value === "" ? undefined : Number(e.target.value))}
                  placeholder="3"
                  type="number"
                />
              </Field>
            )}
            <Field label="Notes" hint="A note for whoever maintains this workflow. No effect on the run.">
              <Textarea
                value={String(config.notes ?? "")}
                onChange={(e) => setKey("notes", e.target.value || undefined)}
                rows={2}
              />
            </Field>
          </div>
        )}

          </div>
        </div>

      </div>
    </Modal>
  );
}

function ExecutionResultPanel({
  resultRef,
  running,
  result,
  isLive,
  raw,
  stepType,
  onToggleRaw,
  testInputs,
}: {
  resultRef: { current: HTMLDivElement | null };
  running: boolean;
  result?: NodeRunResult | null;
  isLive: boolean;
  raw: boolean;
  stepType: string;
  onToggleRaw: () => void;
  testInputs: TestInputField[];
}) {
  const hasResult = !!result?.status;
  const media = result?.error ? [] : extractMediaRefs(result?.output);
  const status = running ? "running" : result?.skipped ? "skipped" : result?.status || "not run yet";
  const statusColor = running
    ? "var(--accent)"
    : !hasResult
      ? "var(--text-faint)"
      : result?.skipped
        ? "var(--text-faint)"
        : status === "completed"
          ? "var(--accent)"
          : status === "failed"
            ? "var(--editor-danger-text)"
            : "#cf9b44";
  const subLabel: React.CSSProperties = {
    fontSize: 10.5,
    color: "var(--text-faint)",
    letterSpacing: 0.2,
  };
  const contentStyle: React.CSSProperties = {
    background: "var(--surface-panel)",
    borderRadius: "var(--radius-control)",
    padding: "9px 11px",
    maxHeight: 240,
    overflow: "auto",
  };
  const preStyle: React.CSSProperties = {
    ...contentStyle,
    margin: 0,
    fontSize: 11.5,
    lineHeight: 1.5,
    fontFamily: "var(--font-mono, monospace)",
    color: "var(--text-muted)",
    whiteSpace: "pre-wrap",
    wordBreak: "break-word",
  };
  const triggerTest = !running && status === "completed" && (stepType === "trigger" || stepType === "webhook");
  const duration = typeof result?.duration_ms === "number" ? ` · ${result.duration_ms.toFixed(1)} ms` : "";

  return (
    <div
      ref={resultRef}
      role="region"
      aria-label="Execution result"
      className="workflow-node-execution-result"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 10,
        padding: "16px",
        borderRadius: "var(--radius-control)",
        background: "var(--surface-muted)",
        boxShadow: "var(--shadow-sm)",
      }}
    >
      {testInputs.length > 0 && (
        <section className="workflow-node-test-inputs" aria-labelledby="workflow-node-test-inputs-title">
          <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
            <span id="workflow-node-test-inputs-title" style={{ fontSize: 13, fontWeight: 700, color: "var(--text-strong)" }}>Test inputs</span>
            <span style={{ fontSize: 10.5, color: "var(--text-faint)" }}>Not saved</span>
          </div>
          <p style={{ margin: 0, fontSize: 11.5, lineHeight: 1.45, color: "var(--text-muted)" }}>
            Values are prefilled from the latest run when available. Edit them for this test.
          </p>
          <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
            {testInputs.map((input, index) => {
              const multiline = input.type === "json" || input.value.includes("\n");
              return (
                <div key={`${input.key}-${index}`} style={{ display: "flex", flexDirection: "column", gap: 5 }}>
                  <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: 8 }}>
                    <label style={{ minWidth: 0, fontSize: 11.5, fontWeight: 600, color: "var(--text-strong)", wordBreak: "break-word" }}>
                      {input.key}
                    </label>
                    <span style={{ flexShrink: 0, fontSize: 10, color: "var(--text-faint)", fontFamily: "var(--font-mono, monospace)" }}>{input.type}</span>
                  </div>
                  {multiline ? (
                    <Textarea
                      value={input.value}
                      onChange={(event) => input.onChange(event.target.value)}
                      rows={3}
                      error={input.error}
                      ariaLabel={`Test value for ${input.key}`}
                    />
                  ) : (
                    <Input
                      value={input.value}
                      onChange={(event) => input.onChange(event.target.value)}
                      type={input.type === "number" ? "number" : "text"}
                      error={input.error}
                      ariaLabel={`Test value for ${input.key}`}
                      placeholder="Enter a test value"
                    />
                  )}
                  {input.source && (
                    <span title={input.source} style={{ overflow: "hidden", color: "var(--text-faint)", fontSize: 10.5, fontFamily: "var(--font-mono, monospace)", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                      Mapped from {input.source}
                    </span>
                  )}
                </div>
              );
            })}
          </div>
        </section>
      )}

      {testInputs.length > 0 && <div style={{ height: 1, background: "var(--border-subtle)" }} />}

      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 4, minWidth: 0 }}>
          <span style={{ fontSize: 13, fontWeight: 700, color: "var(--text-strong)" }}>Execution result</span>
          <span aria-live="polite" aria-atomic="true" style={{ display: "flex", alignItems: "center", gap: 7, fontSize: 11.5, color: "var(--text-muted)" }}>
            <span
              aria-hidden="true"
              style={{
                width: 7,
                height: 7,
                borderRadius: "50%",
                flexShrink: 0,
                background: statusColor,
                animation: running ? "pulse 1.2s ease-in-out infinite" : "none",
              }}
            />
            {running
              ? "Running this node…"
              : hasResult
                ? `${isLive ? "This run" : "Last run"} · ${status}${duration}${result?.cached ? " · reused from cache" : ""}`
                : "No result yet"}
          </span>
        </div>
        {!running && hasResult && result && (
          <Button
            variant="ghost"
            size="sm"
            onClick={onToggleRaw}
            title={raw ? "Show the readable result" : "Show the complete execution response"}
            ariaLabel={raw ? "Show readable result" : "Show complete JSON response"}
            style={{ flexShrink: 0, fontFamily: "var(--font-mono, monospace)" }}
          >
            {raw ? "Readable" : "{ } JSON"}
          </Button>
        )}
      </div>

      {!running && !hasResult ? (
        <div className="workflow-node-result-empty">
          <span aria-hidden="true" style={{ fontFamily: "var(--font-mono, monospace)", fontSize: 18, color: "var(--text-faint)" }}>{"{ }"}</span>
          <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
            <span style={{ fontSize: 12, fontWeight: 600, color: "var(--text-strong)" }}>Test this node to see its result</span>
            <span style={{ fontSize: 11.5, lineHeight: 1.5, color: "var(--text-muted)" }}>Output, errors, and the complete JSON response will appear here.</span>
          </div>
        </div>
      ) : running ? (
        <div className="workflow-node-result-empty">
          <span style={{ fontSize: 11.5, lineHeight: 1.5, color: "var(--text-muted)" }}>Waiting for the node to return output…</span>
        </div>
      ) : raw && result ? (
        <pre style={preStyle}>{JSON.stringify(result, null, 2)}</pre>
      ) : result ? (
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          {triggerTest && (
            <p style={{ margin: 0, fontSize: 11.5, lineHeight: 1.5, color: "var(--text-muted)" }}>
              Trigger test completed. This node only starts the flow; it does not produce business data.
            </p>
          )}
          {result.inputs && Object.keys(result.inputs).length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              <span style={subLabel}>Input — data this step received</span>
              <div style={contentStyle}><DataView data={result.inputs} /></div>
            </div>
          )}
          <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
            <span style={subLabel}>Result output</span>
            {result.error ? (
              <pre style={{ ...preStyle, color: "var(--editor-danger-text)" }}>{result.error}</pre>
            ) : media.length > 0 ? (
              media.map((item, index) => <MediaPreview key={index} refItem={item} />)
            ) : (
              <div style={contentStyle}><DataView data={result.output} /></div>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** Friendly renderer for run input/output — readable key→value rows, a real
 *  table for arrays of like-shaped objects, and indented nesting for depth.
 *  Replaces a raw ``JSON.stringify`` dump so a non-engineer can read what a
 *  step received and produced. The panel offers a "{ } json" escape hatch. */
function DataView({ data, depth = 0 }: { data: any; depth?: number }) {
  const empty: React.CSSProperties = { fontSize: 11, color: "var(--text-faint)", fontStyle: "italic" };
  if (data === null || data === undefined || data === "")
    return <span style={empty}>empty</span>;
  if (typeof data === "string")
    return <span style={{ fontSize: 11.5, lineHeight: 1.5, color: "var(--text-strong)", whiteSpace: "pre-wrap", wordBreak: "break-word" }}>{data}</span>;
  if (typeof data === "number" || typeof data === "boolean")
    return <span style={{ fontSize: 11.5, fontFamily: "var(--font-mono, monospace)", color: "var(--text-strong)" }}>{String(data)}</span>;

  if (Array.isArray(data)) {
    if (data.length === 0) return <span style={empty}>empty list</span>;
    const allObjects = data.every((d) => d && typeof d === "object" && !Array.isArray(d));
    if (allObjects && depth < 4) {
      const cols: string[] = [];
      for (const row of data) for (const k of Object.keys(row)) if (!cols.includes(k)) cols.push(k);
      if (cols.length > 0 && cols.length <= 8) return <DataTable rows={data} cols={cols} depth={depth} />;
    }
    return (
      <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
        {data.map((item, i) => (
          <div key={i} style={{ display: "flex", gap: 7 }}>
            <span style={{ fontSize: 11, color: "var(--text-faint)", fontFamily: "var(--font-mono, monospace)", flexShrink: 0 }}>{i}</span>
            <DataView data={item} depth={depth + 1} />
          </div>
        ))}
      </div>
    );
  }

  const entries = Object.entries(data as Record<string, any>);
  if (entries.length === 0) return <span style={empty}>empty</span>;
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      {entries.map(([k, v]) => {
        const nested = v !== null && typeof v === "object";
        return (
          <div
            key={k}
            style={
              nested
                ? { display: "flex", flexDirection: "column", gap: 3 }
                : { display: "grid", gridTemplateColumns: "minmax(72px, 32%) 1fr", gap: 10, alignItems: "baseline" }
            }
          >
            <span style={{ fontSize: 11, color: "var(--text-muted)", fontWeight: 500, wordBreak: "break-word" }}>{k}</span>
            <div style={nested ? { marginLeft: 6, paddingLeft: 9, borderLeft: "1px solid rgba(28,25,23,0.08)" } : undefined}>
              <DataView data={v} depth={depth + 1} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** Array-of-objects rendered as a compact, scrollable table (n8n-style). */
function DataTable({ rows, cols, depth }: { rows: any[]; cols: string[]; depth: number }) {
  const cell: React.CSSProperties = { padding: "4px 9px", fontSize: 11, color: "var(--text-strong)", verticalAlign: "top", textAlign: "left", wordBreak: "break-word", maxWidth: 240 };
  const head: React.CSSProperties = { ...cell, color: "var(--text-faint)", fontWeight: 500, whiteSpace: "nowrap", position: "sticky", top: 0, background: "var(--surface)" };
  return (
    <table style={{ borderCollapse: "collapse", width: "100%", fontFamily: "var(--font-sans, inherit)" }}>
      <thead>
        <tr>{cols.map((c) => <th key={c} style={head}>{c}</th>)}</tr>
      </thead>
      <tbody>
        {rows.map((row, i) => (
          <tr key={i} style={{ borderTop: i ? "1px solid rgba(28,25,23,0.06)" : "none" }}>
            {cols.map((c) => (
              <td key={c} style={cell}>
                {row[c] === undefined ? (
                  <span style={{ color: "var(--text-faint)" }}>—</span>
                ) : (
                  <DataView data={row[c]} depth={depth + 1} />
                )}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Inline cheat-sheet for condition / switch expressions — mirrors what the
 *  runner's evaluator supports (no eval; safe operator comparison). */
function ExpressionHelp() {
  const mono: React.CSSProperties = { fontFamily: "var(--font-mono, monospace)", color: "var(--text-strong)" };
  const Row = ({ k, v }: { k: React.ReactNode; v: string }) => (
    <div style={{ display: "flex", gap: 8 }}>
      <code style={{ ...mono, fontSize: 11.5, minWidth: 150, flexShrink: 0 }}>{k}</code>
      <span style={{ fontSize: 11.5, color: "var(--text-muted)" }}>{v}</span>
    </div>
  );
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5, padding: "10px 12px", borderRadius: 10, background: "var(--surface-muted)" }}>
      <div style={{ fontSize: 11, fontWeight: 500, letterSpacing: 0.2, color: "var(--text-faint)" }}>Expression format</div>
      <Row k={<>{"=="} {"!="} {">"} {"<"} {">="} {"<="}</>} v="compare two values" />
      <Row k="and · or" v="combine clauses (or binds loosest)" />
      <Row k={'status == "vip"'} v="variable vs quoted text" />
      <Row k="score >= 0.7" v="variable vs number" />
      <Row k="len(items) > 0" v="length of a list/text" />
      <Row k="result.ok == true" v="field of an object variable" />
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 2, lineHeight: 1.5 }}>
        Variables come from upstream steps (set an <b>Output var</b>, or a <b>Set</b> node) — reference them by bare name, no <code style={mono}>{"{{ }}"}</code>.
      </div>
    </div>
  );
}

type WorkflowNodeRef = {
  id: string;
  name?: string;
  type: string;
  targets?: string[];
  outputs?: { name: string; type?: string }[];
};

type Binding = { key?: string; value?: any; type?: string; __custom?: boolean };
type WorkflowRunInputBinding = Binding & {
  label?: string;
  required?: boolean;
  hidden?: boolean;
  placeholder?: string;
  default?: any;
  defaultValue?: any;
  schema?: Record<string, any>;
  __schemaRaw?: string;
  __schemaError?: string;
};
type BindingValueOption = { value: string; label: string; type?: string };
type TestInputField = {
  key: string;
  type: string;
  source: string;
  value: string;
  error?: string;
  onChange: (value: string) => void;
};

function formatTestInputValue(value: any): string {
  if (value === undefined || value === null) return "";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

/** Resolve an exact upstream binding against the latest workflow variables for
 *  a useful test default. Missing references stay empty so Test node asks for
 *  data instead of accidentally sending the literal `{{missing}}` to a node. */
function resolveTestInputDefault(value: any, variables?: Record<string, any>): any {
  if (typeof value !== "string") return value;
  const match = /^\s*\{\{([^{}]+)\}\}\s*$/.exec(value);
  if (!match) return value;
  if (!variables) return undefined;
  const key = match[1].trim();
  if (Object.prototype.hasOwnProperty.call(variables, key)) return variables[key];
  const parts = key.split(".");
  let current = variables[parts[0]];
  if (current === undefined) return undefined;
  for (const part of parts.slice(1)) {
    if (!current || typeof current !== "object") return undefined;
    current = current[part];
  }
  return current;
}

/** Return every transitive predecessor of the configured step. The picker must
 *  never offer downstream or disconnected nodes: their values do not exist
 *  when this step executes. */
function connectedUpstreamNodes(nodes: WorkflowNodeRef[], stepId: string): WorkflowNodeRef[] {
  const incoming = new Map<string, string[]>();
  for (const node of nodes) {
    for (const target of node.targets || []) {
      incoming.set(target, [...(incoming.get(target) || []), node.id]);
    }
  }
  const connected = new Set<string>();
  const visit = (target: string) => {
    for (const source of incoming.get(target) || []) {
      if (connected.has(source)) continue;
      connected.add(source);
      visit(source);
    }
  };
  visit(stepId);
  return nodes.filter((node) => connected.has(node.id));
}

/** Prompt editor with keyboard-completable named inputs. Typing one opening
 *  brace is enough to open suggestions; Enter or Tab inserts a complete
 *  {{input_name}} token. The buttons provide a zero-typing path. */
function PromptInputTextarea({
  value,
  onChange,
  inputNames,
  rows = 4,
}: {
  value: string;
  onChange: (value: string) => void;
  inputNames: string[];
  rows?: number;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const menuId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [replaceStart, setReplaceStart] = useState(0);
  const [activeIndex, setActiveIndex] = useState(0);
  const filteredNames = inputNames.filter((name) =>
    name.toLowerCase().includes(query.toLowerCase()),
  );

  const syncSuggestions = (nextValue: string, caret: number) => {
    const match = nextValue.slice(0, caret).match(/\{\{?([\w.-]*)$/);
    if (!match || !inputNames.length) {
      setOpen(false);
      return;
    }
    setQuery(match[1] || "");
    setReplaceStart(caret - match[0].length);
    setActiveIndex(0);
    setOpen(true);
  };

  const insertInput = (name: string, start?: number, end?: number) => {
    const textarea = textareaRef.current;
    const from = start ?? textarea?.selectionStart ?? value.length;
    const to = end ?? textarea?.selectionEnd ?? value.length;
    const token = `{{${name}}}`;
    const nextValue = value.slice(0, from) + token + value.slice(to);
    const nextCaret = from + token.length;
    onChange(nextValue);
    setOpen(false);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(nextCaret, nextCaret);
    });
  };

  return (
    <div className="workflow-prompt-input-editor">
      <div className="workflow-prompt-textarea-shell">
        <Textarea
          textareaRef={textareaRef}
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
            syncSuggestions(event.target.value, event.target.selectionStart);
          }}
          onFocus={(event) => syncSuggestions(value, event.target.selectionStart)}
          onBlur={() => setOpen(false)}
          onKeyDown={(event) => {
            if (!open) return;
            if (event.key === "ArrowDown" && filteredNames.length) {
              event.preventDefault();
              setActiveIndex((index) => (index + 1) % filteredNames.length);
            } else if (event.key === "ArrowUp" && filteredNames.length) {
              event.preventDefault();
              setActiveIndex((index) => (index - 1 + filteredNames.length) % filteredNames.length);
            } else if ((event.key === "Enter" || event.key === "Tab") && filteredNames.length) {
              event.preventDefault();
              insertInput(filteredNames[activeIndex] || filteredNames[0], replaceStart, event.currentTarget.selectionStart);
            } else if (event.key === "Escape") {
              event.preventDefault();
              setOpen(false);
            }
          }}
          rows={rows}
          ariaLabel="Prompt"
          ariaControls={open ? menuId : undefined}
          ariaExpanded={open}
          ariaAutocomplete="list"
        />
        {open && (
          <div id={menuId} className="workflow-prompt-input-menu" role="listbox" aria-label="Input parameters">
            {filteredNames.length ? filteredNames.map((name, index) => (
              <button
                key={name}
                type="button"
                role="option"
                aria-selected={index === activeIndex}
                className={`workflow-prompt-input-option${index === activeIndex ? " is-active" : ""}`}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => insertInput(name, replaceStart, textareaRef.current?.selectionStart)}
              >
                <span>{name}</span>
                <code>{`{{${name}}}`}</code>
              </button>
            )) : (
              <span className="workflow-prompt-input-empty">No matching Inputs</span>
            )}
          </div>
        )}
      </div>
      {inputNames.length ? (
        <div className="workflow-prompt-input-bar" aria-label="Quick insert Input parameters">
          <span className="workflow-prompt-input-label">Insert input</span>
          {inputNames.map((name) => (
            <button
              key={name}
              type="button"
              className="workflow-prompt-input-chip"
              title={`Insert {{${name}}} at the cursor`}
              onMouseDown={(event) => event.preventDefault()}
              onClick={() => insertInput(name)}
            >
              <code>{`{{${name}}}`}</code>
            </button>
          ))}
          <span className="workflow-prompt-input-shortcut">
            Type <kbd>{"{"}</kbd> then <kbd>Tab</kbd>
          </span>
        </div>
      ) : (
        <span className="workflow-prompt-input-empty-hint">Add an Input below to enable quick insert.</span>
      )}
    </div>
  );
}

function codeInputToken(language: string, name: string): string {
  const quotedName = JSON.stringify(name);
  if (language === "javascript") return `inputs[${quotedName}]`;
  if (language === "bash") {
    const shellName = "'" + name.replace(/'/g, "'\"'\"'") + "'";
    return `$(python -c 'import json,os,sys; print(json.load(open(os.environ["WORKFLOW_INPUTS_FILE"])).get(sys.argv[1], ""))' ${shellName})`;
  }
  return `inputs.get(${quotedName})`;
}

/** Code editor variant of the prompt input picker. `inputs.` opens a filtered
 *  menu and Tab/Enter inserts access syntax for the selected language. Chips
 *  provide the same cursor-aware insertion without typing. */
function CodeInputTextarea({
  value,
  onChange,
  inputNames,
  language,
  rows = 8,
}: {
  value: string;
  onChange: (value: string) => void;
  inputNames: string[];
  language: string;
  rows?: number;
}) {
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const menuId = useId();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [replaceStart, setReplaceStart] = useState(0);
  const [activeIndex, setActiveIndex] = useState(0);
  const normalizedLanguage = language.trim().toLowerCase();
  const filteredNames = inputNames.filter((name) =>
    name.toLowerCase().includes(query.toLowerCase()),
  );

  const syncSuggestions = (nextValue: string, caret: number) => {
    const match = nextValue.slice(0, caret).match(/inputs\.([\w.-]*)$/);
    if (!match || !inputNames.length) {
      setOpen(false);
      return;
    }
    setQuery(match[1] || "");
    setReplaceStart(caret - match[0].length);
    setActiveIndex(0);
    setOpen(true);
  };

  const insertInput = (name: string, start?: number, end?: number) => {
    const textarea = textareaRef.current;
    const from = start ?? textarea?.selectionStart ?? value.length;
    const to = end ?? textarea?.selectionEnd ?? value.length;
    const token = codeInputToken(normalizedLanguage, name);
    const nextValue = value.slice(0, from) + token + value.slice(to);
    const nextCaret = from + token.length;
    onChange(nextValue);
    setOpen(false);
    requestAnimationFrame(() => {
      textareaRef.current?.focus();
      textareaRef.current?.setSelectionRange(nextCaret, nextCaret);
    });
  };

  return (
    <div className="workflow-prompt-input-editor workflow-code-input-editor">
      <div className="workflow-prompt-textarea-shell">
        <Textarea
          className="workflow-code-textarea"
          textareaRef={textareaRef}
          value={value}
          onChange={(event) => {
            onChange(event.target.value);
            syncSuggestions(event.target.value, event.target.selectionStart);
          }}
          onFocus={(event) => syncSuggestions(value, event.target.selectionStart)}
          onBlur={() => setOpen(false)}
          onKeyDown={(event) => {
            if (!open) return;
            if (event.key === "ArrowDown" && filteredNames.length) {
              event.preventDefault();
              setActiveIndex((index) => (index + 1) % filteredNames.length);
            } else if (event.key === "ArrowUp" && filteredNames.length) {
              event.preventDefault();
              setActiveIndex((index) => (index - 1 + filteredNames.length) % filteredNames.length);
            } else if ((event.key === "Enter" || event.key === "Tab") && filteredNames.length) {
              event.preventDefault();
              insertInput(filteredNames[activeIndex] || filteredNames[0], replaceStart, event.currentTarget.selectionStart);
            } else if (event.key === "Escape") {
              event.preventDefault();
              setOpen(false);
            }
          }}
          rows={rows}
          ariaLabel="Code"
          ariaControls={open ? menuId : undefined}
          ariaExpanded={open}
          ariaAutocomplete="list"
        />
        {open && (
          <div id={menuId} className="workflow-prompt-input-menu" role="listbox" aria-label="Code input parameters">
            {filteredNames.length ? filteredNames.map((name, index) => (
              <button
                key={name}
                type="button"
                role="option"
                aria-selected={index === activeIndex}
                className={`workflow-prompt-input-option${index === activeIndex ? " is-active" : ""}`}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => insertInput(name, replaceStart, textareaRef.current?.selectionStart)}
              >
                <span>{name}</span>
                <code>{codeInputToken(normalizedLanguage, name)}</code>
              </button>
            )) : (
              <span className="workflow-prompt-input-empty">No matching Inputs</span>
            )}
          </div>
        )}
      </div>
      {inputNames.length ? (
        <div className="workflow-prompt-input-bar" aria-label="Quick insert code input parameters">
          <span className="workflow-prompt-input-label">Insert input</span>
          {inputNames.map((name) => {
            const token = codeInputToken(normalizedLanguage, name);
            return (
              <button
                key={name}
                type="button"
                className="workflow-prompt-input-chip"
                title={`Insert ${token} at the cursor`}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => insertInput(name)}
              >
                <code>{token}</code>
              </button>
            );
          })}
          <span className="workflow-prompt-input-shortcut">
            Type <kbd>inputs.</kbd> then <kbd>Tab</kbd>
          </span>
        </div>
      ) : (
        <span className="workflow-prompt-input-empty-hint">Add an Input below to enable code insert.</span>
      )}
    </div>
  );
}

/** Parameter types — ComfyUI-style typed sockets / n8n typeOptions. ``any`` is
 *  the pass-through default; the rest coerce + validate at the data-flow layer. */
const BINDING_TYPES = ["any", "text", "number", "boolean", "json", "image"];
const CUSTOM_BINDING_VALUE = "__workflow_custom_binding__";

function workflowEntryOutputType(type?: string): string {
  return String(type || "string").toLowerCase() === "string"
    ? "text"
    : String(type || "any").toLowerCase();
}

function workflowEntryOutputs(stepId: string, rows: WorkflowRunInputBinding[]): Binding[] {
  return rows.flatMap((row) => {
    const key = String(row.key || "").trim();
    return key ? [{
      key,
      type: workflowEntryOutputType(row.type),
      value: `{{${stepId}.${key}}}`,
    }] : [];
  });
}

function workflowEntryOutputsMatch(
  stepId: string,
  runInputs: WorkflowRunInputBinding[],
  outputs: Binding[],
): boolean {
  const expected = workflowEntryOutputs(stepId, runInputs);
  return JSON.stringify(outputs) === JSON.stringify(expected);
}

function WorkflowRunInputRows({
  rows,
  onChange,
}: {
  rows: WorkflowRunInputBinding[];
  onChange: (rows: WorkflowRunInputBinding[]) => void;
}) {
  const update = (index: number, patch: Partial<WorkflowRunInputBinding>) =>
    onChange(rows.map((row, rowIndex) => rowIndex === index ? { ...row, ...patch } : row));

  return (
    <div className="workflow-run-input-list">
      {rows.map((row, index) => {
        const key = String(row.key || "").trim();
        const type = String(row.type || "string").toLowerCase();
        const defaultValue = row.defaultValue ?? row.default ?? row.value ?? "";
        const schemaValue = row.__schemaRaw ?? formatTestInputValue(row.schema ?? {});
        const updateDefault = (value: string) => update(
          index,
          Object.prototype.hasOwnProperty.call(row, "defaultValue")
            ? { defaultValue: value }
            : { default: value },
        );
        return (
          <details className="workflow-run-input-row" key={index}>
            <summary>
              <span className="workflow-run-input-summary-name">{row.label || key || "New input"}</span>
              <code>{key || "unnamed"}</code>
              <span>{type}</span>
              {row.required !== false && <span>required</span>}
            </summary>
            <div className="workflow-run-input-fields">
              <Field label="Key">
                <Input
                  value={row.key ?? ""}
                  onChange={(event) => update(index, { key: event.target.value })}
                  placeholder="input_name"
                />
              </Field>
              <Field label="Label">
                <Input
                  value={row.label ?? ""}
                  onChange={(event) => update(index, { label: event.target.value })}
                  placeholder={key || "Input label"}
                />
              </Field>
              <Field label="Type">
                <Select
                  value={type}
                  onChange={(value) => update(index, { type: value })}
                  options={["string", "number", "boolean", "json"]}
                />
              </Field>
              <Field label="Default">
                {type === "json" ? (
                  <Textarea
                    value={formatTestInputValue(defaultValue)}
                    onChange={(event) => updateDefault(event.target.value)}
                    rows={3}
                    ariaLabel={`Default value for ${row.label || key || "input"}`}
                  />
                ) : (
                  <Input
                    value={formatTestInputValue(defaultValue)}
                    onChange={(event) => updateDefault(event.target.value)}
                    placeholder={type === "boolean" ? "false" : ""}
                  />
                )}
              </Field>
              <Field label="Placeholder">
                <Input
                  value={row.placeholder ?? ""}
                  onChange={(event) => update(index, { placeholder: event.target.value })}
                />
              </Field>
              {type === "json" && (
                <Field
                  label="Schema (JSON)"
                  hint="Optional JSON Schema used to render and validate structured Workflow inputs."
                >
                  <Textarea
                    value={schemaValue}
                    onChange={(event) => {
                      const raw = event.target.value;
                      if (!raw.trim()) {
                        update(index, { schema: undefined, __schemaRaw: "", __schemaError: undefined });
                        return;
                      }
                      try {
                        const parsed = JSON.parse(raw);
                        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
                          throw new Error("Schema must be an object");
                        }
                        update(index, { schema: parsed, __schemaRaw: raw, __schemaError: undefined });
                      } catch {
                        update(index, { __schemaRaw: raw, __schemaError: "Invalid JSON Schema" });
                      }
                    }}
                    rows={5}
                    ariaLabel={`Schema for ${row.label || key || "input"}`}
                  />
                  {row.__schemaError && (
                    <span style={{ fontSize: 11, color: "var(--danger)", fontWeight: 600 }}>
                      {row.__schemaError}
                    </span>
                  )}
                </Field>
              )}
              <div className="workflow-run-input-options">
                <label>
                  <span>Required</span>
                  <Toggle
                    checked={row.required !== false}
                    onChange={() => update(index, { required: row.required === false })}
                    aria-label={`Required ${row.label || key || "input"}`}
                  />
                </label>
                <label>
                  <span>Hidden</span>
                  <Toggle
                    checked={!!row.hidden}
                    onChange={() => update(index, { hidden: !row.hidden })}
                    aria-label={`Hidden ${row.label || key || "input"}`}
                  />
                </label>
                <Button
                  variant="ghost"
                  size="sm"
                  onClick={() => onChange(rows.filter((_, rowIndex) => rowIndex !== index))}
                >
                  Remove
                </Button>
              </div>
            </div>
          </details>
        );
      })}
      <button
        type="button"
        onClick={() => onChange([...rows, { key: "", label: "", type: "string", required: true, default: "" }])}
        style={ADD_BTN}
      >
        + Add input
      </button>
    </div>
  );
}

/** Editable list of {key = value : type} bindings — used for a node's named
 *  Inputs (map + coerce upstream values) and Outputs (expose typed result
 *  fields). Minimal, borderless rows in keeping with the design system. */
function BindingRows({
  rows,
  onChange,
  keyPlaceholder,
  valuePlaceholder,
  addLabel,
  validate,
  valueOptions,
}: {
  rows: Binding[];
  onChange: (rows: Binding[]) => void;
  keyPlaceholder: string;
  valuePlaceholder: string;
  addLabel: string;
  validate?: (row: Binding) => string | undefined;
  valueOptions?: BindingValueOption[];
}) {
  const update = (i: number, patch: Binding) =>
    onChange(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {rows.map((r, i) => {
        const warn = validate?.(r);
        const selectedSource = valueOptions?.find((option) => option.value === r.value);
        const customMode = valueOptions !== undefined
          && (!!r.__custom || (!!r.value && !selectedSource));
        return (
          <div key={i} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
            <div className="workflow-binding-row">
              <div className="workflow-binding-key">
                <Input value={r.key ?? ""} onChange={(e) => update(i, { key: e.target.value })} placeholder={keyPlaceholder} />
              </div>
              <span className="workflow-binding-equals">=</span>
              <div className="workflow-binding-value">
                {valueOptions === undefined ? (
                  <Input value={r.value ?? ""} onChange={(e) => update(i, { value: e.target.value })} placeholder={valuePlaceholder} />
                ) : customMode ? (
                  <div style={{ display: "flex", alignItems: "center", gap: 4 }}>
                    <Input
                      className="workflow-binding-custom-input"
                      value={r.value ?? ""}
                      onChange={(e) => update(i, { value: e.target.value, __custom: true })}
                      placeholder="Enter a literal or {{reference}}"
                    />
                    <Button
                      variant="ghost"
                      size="sm"
                      onClick={() => update(i, { value: "", __custom: false })}
                      title="Choose an upstream output"
                      ariaLabel="Choose an upstream output"
                      style={{ flexShrink: 0, padding: "0 8px" }}
                    >
                      Outputs
                    </Button>
                  </div>
                ) : (
                  <Select
                    value={selectedSource?.value || ""}
                    onChange={(value) => {
                      if (value === CUSTOM_BINDING_VALUE) {
                        update(i, { value: "", __custom: true });
                        return;
                      }
                      const source = valueOptions.find((option) => option.value === value);
                      update(i, {
                        value,
                        __custom: false,
                        ...(!r.type && source?.type && source.type !== "any" ? { type: source.type } : {}),
                      });
                    }}
                    placeholder={valueOptions.length ? "Select an upstream output…" : "Connect an upstream node…"}
                    options={[
                      ...valueOptions,
                      { value: CUSTOM_BINDING_VALUE, label: "Custom value…" },
                    ]}
                    filterable={valueOptions.length > 6}
                    dropdownMinWidth={300}
                  />
                )}
              </div>
              <div className="workflow-binding-type">
                <Select
                  value={r.type ?? "any"}
                  onChange={(v) => update(i, { type: v === "any" ? undefined : v })}
                  options={BINDING_TYPES}
                />
              </div>
              <button
                type="button"
                title="Remove"
                onClick={() => onChange(rows.filter((_, j) => j !== i))}
                className="workflow-binding-remove"
                style={{ width: 28, height: 28, borderRadius: 8, border: "none", cursor: "pointer", background: "transparent", color: "var(--text-faint)", fontSize: 16, lineHeight: 1 }}
              >
                ×
              </button>
            </div>
            {warn && <span style={{ fontSize: 10.5, color: "#c2891f", paddingLeft: 2 }}>⚠ {warn}</span>}
          </div>
        );
      })}
      <button type="button" onClick={() => onChange([...rows, { key: "", value: "" }])} style={ADD_BTN}>
        + {addLabel}
      </button>
    </div>
  );
}

/** The one "muted pill" style — shared by the Add-row buttons and the
 *  parameter chips so the panel stays to a single secondary-control look. */
const ADD_BTN: React.CSSProperties = {
  alignSelf: "flex-start", fontSize: 12, fontWeight: 500, padding: "5px 10px",
  borderRadius: 999, border: "none", cursor: "pointer",
  background: "var(--surface-muted)", color: "var(--text-muted)",
};

/** A small "Open ↗" link that jumps from a bound resource to its own page. */
function OpenLink({ onClick, label = "Open" }: { onClick: () => void; label?: string }) {
  return (
    <button
      type="button"
      onClick={onClick}
      style={{
        alignSelf: "flex-start", display: "inline-flex", alignItems: "center", gap: 3,
        fontSize: 11, padding: 0, border: "none", background: "transparent",
        color: "var(--accent, #0f766e)", cursor: "pointer", fontWeight: 500,
      }}
    >
      {label}
      <svg width={11} height={11} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        <path d="M7 17L17 7M9 7h8v8" />
      </svg>
    </button>
  );
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style={{ fontSize: 11, fontWeight: 500, letterSpacing: 0.2, color: "var(--text-faint)" }}>{label}</span>
      {children}
      {hint && <span style={{ fontSize: 11, color: "var(--text-faint)", fontWeight: 400, lineHeight: 1.4 }}>{hint}</span>}
    </label>
  );
}

function FieldGroup({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 5 }}>
      <span style={{ fontSize: 11, fontWeight: 500, letterSpacing: 0.2, color: "var(--text-faint)" }}>{label}</span>
      {children}
      {hint && <span style={{ fontSize: 11, color: "var(--text-faint)", fontWeight: 400, lineHeight: 1.4 }}>{hint}</span>}
    </div>
  );
}

/** A JSON object field with live validation (shared by the json field kind and
 *  the connector args). Keeps a raw buffer while the text is invalid. */
function JsonField({
  fieldKey, label, hint, config, setConfig, jsonErrs, setJsonErrs,
}: {
  fieldKey: string;
  label: string;
  hint?: string;
  config: Record<string, any>;
  setConfig: React.Dispatch<React.SetStateAction<Record<string, any>>>;
  jsonErrs: Record<string, string>;
  setJsonErrs: React.Dispatch<React.SetStateAction<Record<string, string>>>;
}) {
  const err = jsonErrs[fieldKey];
  const raw = config[`__raw_${fieldKey}`];
  const value = err !== undefined && raw !== undefined
    ? raw
    : config[fieldKey] === undefined ? "" : JSON.stringify(config[fieldKey], null, 2);
  return (
    <Field label={label} hint={hint}>
      <Textarea
        value={value}
        rows={4}
        onChange={(e) => {
          const text = e.target.value;
          if (text.trim() === "") {
            setConfig((c) => { const n = { ...c }; delete n[fieldKey]; delete n[`__raw_${fieldKey}`]; return n; });
            setJsonErrs((j) => { const n = { ...j }; delete n[fieldKey]; return n; });
            return;
          }
          try {
            const parsed = JSON.parse(text);
            setConfig((c) => { const n = { ...c, [fieldKey]: parsed }; delete n[`__raw_${fieldKey}`]; return n; });
            setJsonErrs((j) => { const n = { ...j }; delete n[fieldKey]; return n; });
          } catch {
            setConfig((c) => ({ ...c, [`__raw_${fieldKey}`]: text }));
            setJsonErrs((j) => ({ ...j, [fieldKey]: "Invalid JSON" }));
          }
        }}
      />
      {err && <span style={{ fontSize: 12, color: "#d65f59" }}>{err}</span>}
    </Field>
  );
}
