export type ApprovalMode = "confirm_changes" | "risk_based" | "automatic";
export type ApprovalEffect = "allow" | "ask" | "deny";
// Mirrors ApprovalCategory in the shared policy schema. Categories exist even
// when providers have no representative action in the server action catalog.
export const APPROVAL_CATEGORIES = ["read", "browser", "files", "communication", "publishing", "deletion", "purchases", "automation", "workspace", "unknown"] as const;
export type ApprovalScope = { kind: "account" | "personal" | "workspace" | "room"; id?: string; entity_id?: string };
export type ApprovalSelector =
  | { kind: "category"; category: string }
  | { kind: "action"; action_key: string }
  | { kind: "resource"; action_key: string; account_id: string | null; resource_kind: string; resource_ids: string[] };
export type ApprovalRuleDraft = { id?: string; effect: ApprovalEffect; selector: ApprovalSelector };
export type ApprovalRule = ApprovalRuleDraft & {
  id: string; source_kind: "user" | "workspace_rule"; source_id?: string | null;
  scope?: ApprovalScope; editable?: boolean; deletable?: boolean; source_link?: string;
};
export type ApprovalDraft = { mode: ApprovalMode | null; user_rules: ApprovalRuleDraft[] };
export type ApprovalDecision = { outcome: ApprovalEffect; reason_code: string; rule_id?: string | null; scope?: ApprovalScope; source?: string };
export type ApprovalCatalogItem = { category: string; action_key: string; label: string; impact: string };
export type ApprovalSnapshot = {
  scope: ApprovalScope; revision: number; version: string;
  policy: { mode: ApprovalMode | null; rules: ApprovalRule[] };
  effective_policy: { mode: ApprovalMode; rules: ApprovalRule[] };
  inherited_mode: ApprovalMode | null;
  root_scope: ApprovalScope | null;
  policy_versions: { scope: ApprovalScope; revision: number }[];
  inheritance: "root" | "following" | "custom";
  capabilities: { can_view: boolean; can_edit_root: boolean; can_edit_local: boolean; can_save_rule: boolean };
  action_catalog: ApprovalCatalogItem[];
  effective_rows: (ApprovalCatalogItem & { decision: ApprovalDecision })[]; saved_rules: ApprovalRule[]; inherited_rules: ApprovalRule[];
  rule_sources: unknown[];
  /** Set when an approval conversion could not represent some old settings exactly. */
  approval_review?: { required: boolean; reasons: Record<string, unknown>[]; plan_mode: string } | null;
};
export type ApprovalWrite = { expected_version: string; draft: ApprovalDraft };
export type ApprovalCommandProjection = {
  request_ref: { kind: "hitl"; id: string }; status: string; digest: string;
  available_decisions: string[]; scope?: ApprovalScope; policy_version?: string;
  rule_options: { id: string; action_key: string; rule: ApprovalRuleDraft; coverage: "exact_resource" | "all_targets_for_action" }[];
};
export type ApprovalSaveRuleCommand = {
  /** ``approve_and_save_rule`` decides and saves atomically (Task approvals);
   * ``save_rule`` only saves, and the card's own approve follows. */
  request: { kind: "hitl"; id: string }; choice: "approve_and_save_rule" | "save_rule";
  expectedDigest: string; idempotencyKey: string; expectedPolicyVersion: string; optionId: string;
};
export type ApprovalCommandResult = {
  request_ref: { kind: "hitl"; id: string }; status: string; digest: string;
  changed_scope: ApprovalScope; policy: ApprovalSnapshot; idempotent: boolean;
};
export type ApprovalPreview = {
  before: ApprovalSnapshot; after: ApprovalSnapshot;
  samples: { action_key: string; before: ApprovalDecision; after: ApprovalDecision }[];
  rules_added: number; rules_removed: number; representative_only: boolean;
};
export type ApprovalOverrides = { items: { scope: ApprovalScope; label: string; snapshot: ApprovalSnapshot }[]; total: number; limit: number; offset: number; following_count: number; custom_count: number };
export function approvalResourcePath(scope: ApprovalScope): string {
  if (scope.kind === "personal") return "/approval-settings/personal";
  if (!scope.id) throw new Error("Approval scope is missing its resource identity");
  const collection = scope.kind === "account" ? "accounts" : scope.kind === "workspace" ? "workspaces" : "rooms";
  return `/approval-settings/${collection}/${encodeURIComponent(scope.id)}`;
}
export function approvalDraft(snapshot: ApprovalSnapshot): ApprovalDraft {
  return { mode: snapshot.policy.mode, user_rules: snapshot.policy.rules.filter(rule => rule.source_kind === "user" && rule.editable !== false).map(({ id, effect, selector }) => ({ id, effect, selector })) };
}
