export interface WorkspacePresentationRule {
  terms: string[];
  iconKey: string;
  label: string;
  bg: string;
  fg: string;
}

export interface WorkspacePresentationHaystackSource {
  name?: string | null;
  description?: string | null;
  category?: string | null;
  kind?: string | null;
  identity_label?: string | null;
  property_type?: string | null;
  primary_work?: string | null;
  operating_context?: string | null;
  attribute_tags?: string[] | null;
}

export const WORKSPACE_PRESENTATION_RULES: WorkspacePresentationRule[];

export function matchesWorkspaceTerm(haystack: string, term: string): boolean;

export function workspaceHaystack(ws: WorkspacePresentationHaystackSource): string;

export function matchWorkspacePresentationRule(
  ws: WorkspacePresentationHaystackSource,
): WorkspacePresentationRule | null;
