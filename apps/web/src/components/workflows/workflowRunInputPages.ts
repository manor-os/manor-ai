import type { WorkflowInputSchema } from "./workflowSchema";
import type { WorkflowRunActionInput } from "./workflowRunDisplay";

/** One page of a starter form: the inputs that share a `step`, in declared order. */
export interface WorkflowRunInputPage {
  id: string;
  title: string;
  keys: string[];
}

/**
 * Groups a starter's inputs into pages by their `step`, in the order each step
 * first appears. Returns null when no input names a step, so the form stays one page.
 */
export function workflowRunInputPages(inputs: WorkflowRunActionInput[]): WorkflowRunInputPage[] | null {
  if (!inputs.some((input) => input.step)) return null;
  const pages = new Map<string, WorkflowRunInputPage>();
  for (const input of inputs) {
    const id = input.step || "";
    let page = pages.get(id);
    if (!page) {
      page = { id, title: "", keys: [] };
      pages.set(id, page);
    }
    page.title ||= input.step_label || "";
    page.keys.push(input.key);
  }
  return [...pages.values()];
}

/**
 * The schema of one page. Only the constraints whose fields all sit on this page
 * are kept here; the rest are checked when the whole form is submitted.
 */
export function workflowRunInputPageSchema(
  schema: WorkflowInputSchema,
  keys: string[],
): WorkflowInputSchema {
  const onPage = new Set(keys);
  // A rule is about the inputs it requires or constrains, however deep (anyOf of const answers).
  const fieldsOf = (branch: WorkflowInputSchema | undefined): string[] => branch ? [
    ...(branch.required || []), ...Object.keys(branch.properties || {}),
    ...[...(branch.anyOf || []), ...(branch.allOf || [])].flatMap(fieldsOf),
    ...[branch.if, branch.then, branch.else].flatMap(fieldsOf),
  ] : [];
  const within = (branches: WorkflowInputSchema[]) => branches.every((branch) => fieldsOf(branch).every((key) => onPage.has(key)));
  const page: WorkflowInputSchema = {
    ...schema,
    properties: Object.fromEntries(keys.flatMap((key) => (schema.properties?.[key] ? [[key, schema.properties[key]]] : []))),
    required: (schema.required || []).filter((key) => onPage.has(key)),
    "x-ui": { ...schema["x-ui"], order: keys },
  };
  delete page.allOf;
  delete page.anyOf;
  delete page.if;
  delete page.then;
  delete page.else;
  // Each rule is checked on the page that asks for all of its inputs.
  const pageRules = (schema.allOf || []).filter((branch) => within([branch]));
  if (pageRules.length) page.allOf = pageRules;
  if (schema.anyOf && within(schema.anyOf)) page.anyOf = schema.anyOf;
  if (schema.if && within([schema.if, schema.then, schema.else].filter(Boolean) as WorkflowInputSchema[])) {
    page.if = schema.if;
    page.then = schema.then;
    page.else = schema.else;
  }
  return page;
}
