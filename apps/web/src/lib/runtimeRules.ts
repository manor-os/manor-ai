import { t } from "./i18n";
import type { ApprovalSelector } from "./approvalTypes";

export type RuntimeRuleTone = "orange" | "red";
export interface RuntimeRuleInference {
  effect: "ask" | "deny";
  selectors: ApprovalSelector[];
  rule_type: "approval_required" | "deny" | "guidance";
  label: string;
  tone: RuntimeRuleTone;
}
export interface RuntimeRuleDraft {
  rule_key: string;
  rule_type: RuntimeRuleInference["rule_type"];
  description: string;
  severity: "medium" | "high";
  approval_selectors: ApprovalSelector[];
  source: string;
  enabled: boolean;
}

const FILEISH_RE = /workspace.*file|file|document|doc|knowledge|知识库|文件|文档|资料/;
const CREATE_ONLY_RE = /只能.*(添加|新增|创建)|只.*(添加|新增|创建)|only\s+(add|create)|add[- ]?only|create[- ]?only/;
const MODIFY_RE = /改|修改|编辑|更新|覆盖|写入|变更|modify|edit|update|overwrite|write|change/;
const DELETE_RE = /删除|移除|delete|remove|destroy/;
const CREATE_RE = /添加|新增|创建|add|create/;
const SOCIAL_RE = /post|发帖|发\s*post|社媒|social|linkedin|twitter|tweet|\bx\b|xhs|小红书|facebook|instagram|发布/;
const EMAIL_RE = /email|e-mail|邮件|gmail|outlook/;
const MESSAGE_RE = /message|消息|wechat|微信|messenger|\bdm\b|私信/;
const EXTERNAL_RE = /对外|external|public|公开|发送|send|publish|发布/;
const APPROVAL_RE = /审核|审批|批准|同意|确认|给用户|用户同意|人工|human|review|approve|approval|consent|confirm|permission/;
const DENY_RE = /禁止|不要|不得|不准|不允许|不能|never|deny|block|don't|do not/;
const DRAFT_ONLY_RE = /只生成草稿|草稿|draft[- ]?only|draft only/;
const BROAD_SCOPE_RE = /任何|所有|全部|每次|每一步|\ball\b|any action|everything|always/;
const CATEGORIES = ["read", "browser", "files", "communication", "publishing", "deletion", "purchases", "automation", "workspace", "unknown"];

export function uniqueActionPatterns(values: string[]): string[] {
  return Array.from(new Set(values.map((value) => String(value || "").trim()).filter(Boolean)));
}

export function shouldReviewAllActions(text: string): boolean {
  const lower = text.toLowerCase();
  return BROAD_SCOPE_RE.test(lower) && (APPROVAL_RE.test(lower) || DENY_RE.test(lower));
}

export function inferRuntimeRuleFromText(text: string, includeAllActions = shouldReviewAllActions(text)): RuntimeRuleInference {
  const lower = text.toLowerCase();
  const fileish = FILEISH_RE.test(lower);
  const createOnly = CREATE_ONLY_RE.test(lower);
  const isBlock = (DENY_RE.test(lower) || createOnly) && !APPROVAL_RE.test(lower);
  const selectors: ApprovalSelector[] = [];
  const action = (action_key: string) => selectors.push({ kind: "action", action_key });
  const category = (category: string) => selectors.push({ kind: "category", category });
  if (fileish) {
    if (createOnly) {
      action("workspace.file.modify");
      action("workspace.file.delete");
    } else {
      if (MODIFY_RE.test(lower)) action("workspace.file.modify");
      if (DELETE_RE.test(lower)) action("workspace.file.delete");
      if (CREATE_RE.test(lower)) action("workspace.file.create");
      if (!selectors.length) { category("files"); category("deletion"); }
    }
  }
  if (SOCIAL_RE.test(lower)) category("publishing");
  if (EMAIL_RE.test(lower) || MESSAGE_RE.test(lower)) category("communication");
  if (!selectors.length && (EXTERNAL_RE.test(lower) || DRAFT_ONLY_RE.test(lower))) {
    category("publishing"); category("communication");
  }
  if (DELETE_RE.test(lower) && !fileish) category("deletion");
  // A broad request is represented by the policy's explicit categories. An
  // unrecognized sentence remains guidance instead of inventing a wildcard.
  if (!selectors.length && includeAllActions) CATEGORIES.forEach(category);
  const unique = selectors.filter((selector, index) => selectors.findIndex(
    (other) => JSON.stringify(other) === JSON.stringify(selector),
  ) === index);
  return {
    effect: isBlock ? "deny" : "ask", selectors: unique,
    rule_type: unique.length ? (isBlock ? "deny" : "approval_required") : "guidance",
    label: isBlock ? t("lib.runtime_rules.runtime_block") : t("lib.runtime_rules.approval_gate"),
    tone: isBlock ? "red" : "orange",
  };
}

export const HARD_BLOCK_RULE_TYPES = ["draft_only", "deny", "never_allow", "block"] as const;
export function isHardBlockRuleType(ruleType: unknown): boolean {
  return (HARD_BLOCK_RULE_TYPES as readonly string[]).includes(String(ruleType || "").trim().toLowerCase());
}

/** Only edit the source; the service recompiles derived rules atomically. */
export function softenHardBlockRule(rule: any): any {
  const next = { ...rule, rule_type: "approval_required", severity: rule?.severity === "high" ? "medium" : rule?.severity };
  delete next.runtime_enforced;
  return next;
}

export function buildRuntimeRuleFromText(text: string, source = "operator", keyPrefix = "operator"): RuntimeRuleDraft {
  const inferred = inferRuntimeRuleFromText(text);
  return {
    rule_key: `${keyPrefix}_${Date.now()}`, rule_type: inferred.rule_type,
    description: text, severity: inferred.effect === "deny" ? "high" : "medium",
    approval_selectors: inferred.selectors, source, enabled: true,
  };
}

export function runtimeRuleSelectorLabels(rule: any): string[] {
  const selectors: ApprovalSelector[] = Array.isArray(rule?.approval_selectors) ? rule.approval_selectors : [];
  return selectors.map((selector) => selector.kind === "category"
    ? t(`approval_settings.category.${selector.category}`)
    : selector.kind === "resource" ? `${selector.action_key}: ${selector.resource_ids.join(", ")}` : selector.action_key);
}
