export type WorkflowBindingRow = {
  key?: string;
  value?: unknown;
  type?: string;
  [key: string]: unknown;
};

type WorkflowStepLike = {
  id: string;
  type: string;
  config?: Record<string, unknown>;
};

const TEMPLATE_REFERENCE_RE = /\{\{\s*([A-Za-z_][\w-]*(?:\.[\w-]+)*)[^}]*\}\}/g;
const EXPRESSION_IDENTIFIER_RE = /[A-Za-z_][\w-]*/g;
const QUOTED_EXPRESSION_TEXT_RE = /'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"/g;
const LOCAL_REFERENCE_ROOTS = new Set(["item", "index", "loop", "input", "inputs"]);
const EXPRESSION_KEYWORDS = new Set(["and", "or", "not", "in", "is", "true", "false", "null", "none"]);
const NON_INPUT_CONFIG_KEYS = new Set([
  "inputs",
  "outputs",
  "run_inputs",
  "output_schema",
  "schema",
  "next",
  "true_next",
  "false_next",
  "default_next",
]);

function collectTemplateRoots(value: unknown, roots: Set<string>): void {
  if (typeof value === "string") {
    for (const match of value.matchAll(TEMPLATE_REFERENCE_RE)) {
      const root = String(match[1] || "").split(".")[0];
      if (root && !LOCAL_REFERENCE_ROOTS.has(root)) roots.add(root);
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item) => collectTemplateRoots(item, roots));
    return;
  }
  if (!value || typeof value !== "object") return;
  for (const [key, item] of Object.entries(value as Record<string, unknown>)) {
    if (NON_INPUT_CONFIG_KEYS.has(key)) continue;
    collectTemplateRoots(item, roots);
  }
}

function collectExpressionRoots(config: Record<string, unknown>, roots: Set<string>): void {
  const expressions: string[] = [];
  for (const key of ["expression", "condition"]) {
    if (typeof config[key] === "string") expressions.push(config[key] as string);
  }
  if (Array.isArray(config.cases)) {
    for (const item of config.cases) {
      if (item && typeof item === "object" && typeof (item as Record<string, unknown>).expression === "string") {
        expressions.push((item as Record<string, string>).expression);
      }
    }
  }
  for (const expression of expressions) {
    const scrubbed = expression.replace(QUOTED_EXPRESSION_TEXT_RE, "");
    for (const match of scrubbed.matchAll(EXPRESSION_IDENTIFIER_RE)) {
      const token = match[0];
      const prefix = scrubbed.slice(0, match.index).trimEnd();
      if (prefix.endsWith(".") || EXPRESSION_KEYWORDS.has(token.toLowerCase())) continue;
      if (!LOCAL_REFERENCE_ROOTS.has(token)) roots.add(token);
    }
  }
}

/** Infer the variables a node really reads from its templated configuration.
 * The runner resolves these references even when config.inputs was omitted. */
export function inferredWorkflowInputs(
  config: Record<string, unknown> | undefined,
): WorkflowBindingRow[] {
  const roots = new Set<string>();
  collectTemplateRoots(config || {}, roots);
  collectExpressionRoots(config || {}, roots);
  return [...roots].map((key) => ({ key, value: `{{${key}}}`, type: "any" }));
}

/** Return the node variables downstream nodes can consume, including implicit
 * runner contracts such as output_var and a wait node's response_variable. */
export function workflowStepOutputs(step: WorkflowStepLike): WorkflowBindingRow[] {
  const config = step.config || {};
  const explicit = Array.isArray(config.outputs)
    ? config.outputs.filter((item): item is WorkflowBindingRow => Boolean(item && typeof item === "object"))
    : null;
  const rows: WorkflowBindingRow[] = explicit ? [...explicit] : [];

  if (!explicit && ["trigger", "webhook"].includes(step.type)) {
    const entryRows = Array.isArray(config.run_inputs) ? config.run_inputs : [];
    for (const item of entryRows) {
      if (!item || typeof item !== "object") continue;
      const row = item as WorkflowBindingRow;
      const key = String(row.key || "").trim();
      if (key) rows.push({ ...row, key, value: `{{${step.id}.${key}}}` });
    }
  }

  for (const value of [config.output_var, config.response_variable]) {
    const key = String(value || "").trim();
    if (key && !rows.some((row) => String(row.key || "").trim() === key)) {
      rows.push({ key, value: "", type: "any" });
    }
  }

  if (step.type === "transform" && config.set && typeof config.set === "object" && !Array.isArray(config.set)) {
    for (const key of Object.keys(config.set as Record<string, unknown>)) {
      if (key && !rows.some((row) => String(row.key || "").trim() === key)) {
        rows.push({ key, value: `{{${key}}}`, type: "any" });
      }
    }
  }
  return rows;
}
