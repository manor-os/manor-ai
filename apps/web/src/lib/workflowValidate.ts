/* Static validation for a workflow graph — surfaced before you run so config
   gaps and broken wiring are caught early. Pure + synchronous. Mirrors the
   config keys the runner reads (see packages/core/ai/workflow_runner.py). */

export type IssueLevel = "error" | "warning";

export interface ValidationIssue {
  level: IssueLevel;
  nodeId?: string;
  nodeName?: string;
  message: string;
}

interface Step {
  id: string;
  type: string;
  name?: string;
  config?: Record<string, any>;
  next?: string[];
  true_next?: string[];
  false_next?: string[];
}

const ENTRY_TYPES = new Set(["trigger", "webhook"]);
const TERMINAL_TYPES = new Set(["end"]);

// Required config key per node type → issue level when missing/empty.
const REQUIRED: Record<string, { key: string; level: IssueLevel; label: string }[]> = {
  http: [{ key: "url", level: "error", label: "URL" }],
  image: [{ key: "prompt", level: "error", label: "prompt" }],
  video: [{ key: "prompt", level: "error", label: "prompt" }],
  audio: [{ key: "prompt", level: "error", label: "prompt" }],
  media: [{ key: "prompt", level: "warning", label: "prompt" }],
  tool: [{ key: "tool", level: "error", label: "tool name" }],
  connector: [{ key: "tool", level: "error", label: "tool / operation" }],
  llm: [{ key: "prompt", level: "warning", label: "prompt" }],
  classifier: [{ key: "prompt", level: "warning", label: "prompt" }],
  rag: [{ key: "query", level: "warning", label: "query" }],
  code: [{ key: "code", level: "error", label: "code" }],
  condition: [{ key: "expression", level: "warning", label: "expression" }],
  agent: [{ key: "agent_id", level: "warning", label: "agent" }],
  notify: [{ key: "message", level: "warning", label: "message" }],
};

function isEmpty(v: any): boolean {
  return v === undefined || v === null || (typeof v === "string" && v.trim() === "") ||
    (Array.isArray(v) && v.length === 0);
}

export function validateWorkflow(steps: Step[]): ValidationIssue[] {
  const issues: ValidationIssue[] = [];
  if (!steps || steps.length === 0) return issues;

  // Sticky notes are canvas annotations. They do not participate in entry,
  // connectivity, configuration, or reachability validation.
  const executableSteps = steps.filter((step) => step.type !== "note");
  const ids = new Set(executableSteps.map((s) => s.id));
  const name = (s: Step) => s.name || s.type;
  const outEdges = (s: Step) => [
    ...(s.next || []),
    ...(s.true_next || []),
    ...(s.false_next || []),
    ...(s.type === "switch"
      ? [...((s.config?.cases || []).flatMap((c: any) => c?.next || [])), ...(s.config?.default_next || [])]
      : []),
  ];

  // 1) Entry point.
  if (!executableSteps.some((s) => ENTRY_TYPES.has(s.type))) {
    issues.push({ level: "error", message: "No trigger — the workflow has no entry point." });
  }

  // 2) Dangling edges + 3) required config + 5) dead ends.
  const incoming = new Map<string, number>();
  for (const s of executableSteps) {
    for (const target of outEdges(s)) {
      if (!ids.has(target)) {
        issues.push({ level: "error", nodeId: s.id, nodeName: name(s), message: `"${name(s)}" connects to a node that no longer exists.` });
      } else {
        incoming.set(target, (incoming.get(target) || 0) + 1);
      }
    }
    for (const req of REQUIRED[s.type] || []) {
      // An agent can either reference a saved Agent or be configured inline
      // with its own prompt/model. Imported n8n AI agents use the inline form.
      if (s.type === "agent" && req.key === "agent_id" && !isEmpty(s.config?.prompt)) continue;
      if (isEmpty(s.config?.[req.key])) {
        issues.push({ level: req.level, nodeId: s.id, nodeName: name(s), message: `"${name(s)}" is missing ${req.label}.` });
      }
    }
    if (s.type === "switch" && isEmpty(s.config?.cases)) {
      issues.push({ level: "warning", nodeId: s.id, nodeName: name(s), message: `"${name(s)}" has no switch cases.` });
    }
    if (
      s.type === "code" &&
      s.config?.language &&
      !["python", "javascript", "bash"].includes(String(s.config.language).toLowerCase())
    ) {
      issues.push({
        level: "error",
        nodeId: s.id,
        nodeName: name(s),
        message: `"${name(s)}" uses an unsupported code language.`,
      });
    }
    // Dead end: non-terminal node with no outgoing edges.
    if (!TERMINAL_TYPES.has(s.type) && outEdges(s).length === 0) {
      issues.push({ level: "warning", nodeId: s.id, nodeName: name(s), message: `"${name(s)}" has no outgoing connection.` });
    }
  }

  // 4) Unreachable nodes (no incoming edge and not an entry).
  for (const s of executableSteps) {
    if (!ENTRY_TYPES.has(s.type) && !incoming.get(s.id)) {
      issues.push({ level: "warning", nodeId: s.id, nodeName: name(s), message: `"${name(s)}" isn't connected to the flow.` });
    }
  }

  return issues;
}

/** Worst issue level per node id, for canvas badges. */
export function issuesByNode(issues: ValidationIssue[]): Record<string, IssueLevel> {
  const map: Record<string, IssueLevel> = {};
  for (const i of issues) {
    if (!i.nodeId) continue;
    if (i.level === "error" || !map[i.nodeId]) map[i.nodeId] = i.level;
  }
  return map;
}
