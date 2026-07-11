import { t } from "./i18n";

export type GovernanceRuleField = "hitl_required_actions" | "never_allow_actions";
export type GovernanceCapabilityField = "hitl_required_capabilities" | "never_allow_capabilities";

export type RuntimeRuleTone = "orange" | "red";

export interface RuntimeRuleInference {
  field: GovernanceRuleField;
  patterns: string[];
  capabilityField: GovernanceCapabilityField;
  capabilityPatterns: string[];
  rule_type: "approval_required" | "deny" | "draft_only";
  label: string;
  tone: RuntimeRuleTone;
}

export interface RuntimeRuleDraft {
  rule_key: string;
  rule_type: RuntimeRuleInference["rule_type"];
  description: string;
  severity: "medium" | "high";
  action_patterns: string[];
  capability_patterns: string[];
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
const BROAD_SCOPE_RE = /任何|所有|全部|每次|每一步|all|any action|everything|always/;

export function uniqueActionPatterns(values: string[]): string[] {
  return Array.from(new Set(values.map((v) => String(v || "").trim()).filter(Boolean)));
}

export function runtimeCapabilityForActionPattern(pattern: string): string | null {
  const key = String(pattern || "").trim();
  if (!key) return null;
  if (key.startsWith("workspace.task.")) return "workspace.task";
  if (key.startsWith("workspace.knowledge.")) return "workspace.knowledge";
  if (key.startsWith("workspace.rule.") || key.startsWith("workspace.strategist.") || key.startsWith("workspace.operation.")) return "workspace.governance";
  if (key.startsWith("workspace.file.")) return "file.write";
  if (key.startsWith("workspace.automation.")) return "automation.manage";
  if (key.startsWith("sandbox.")) return "sandbox.execute";
  if (key.startsWith("social_post.")) return "external.social";
  if (key.startsWith("email.")) return "external.email";
  if (key.startsWith("external_message.") || key === "channel.reply") return "external.message";
  if (key === "cli.exec") return "cli.execute";
  if (key.startsWith("workspace.")) return "manor.composite";
  return null;
}

export function runtimeCapabilitiesForActionPatterns(patterns: string[]): string[] {
  return uniqueActionPatterns(
    patterns
      .map((pattern) => runtimeCapabilityForActionPattern(pattern))
      .filter((pattern): pattern is string => Boolean(pattern)),
  );
}

export function inferRuntimeActionPatterns(text: string, explicitPatterns: string[] = []): string[] {
  const lower = text.toLowerCase();
  const patterns: string[] = [...explicitPatterns];
  const fileish = FILEISH_RE.test(lower);
  const createOnly = CREATE_ONLY_RE.test(lower);

  if (patterns.length === 0 && fileish) {
    if (createOnly) {
      patterns.push("workspace.file.modify", "workspace.file.delete", "workspace.file.write");
    } else {
      if (MODIFY_RE.test(lower)) patterns.push("workspace.file.modify");
      if (DELETE_RE.test(lower)) patterns.push("workspace.file.delete");
      if (CREATE_RE.test(lower)) patterns.push("workspace.file.create");
    }
  }

  if (patterns.length === 0 || SOCIAL_RE.test(lower)) {
    if (SOCIAL_RE.test(lower)) patterns.push("social_post.publish");
  }
  if (patterns.length === 0 || EMAIL_RE.test(lower)) {
    if (EMAIL_RE.test(lower)) patterns.push("email.send");
  }
  if (patterns.length === 0 || MESSAGE_RE.test(lower)) {
    if (MESSAGE_RE.test(lower)) patterns.push("external_message.send");
  }

  if ((EXTERNAL_RE.test(lower) || DRAFT_ONLY_RE.test(lower)) && patterns.length === 0) {
    patterns.push("social_post.publish", "email.send", "external_message.send");
  }
  if (DELETE_RE.test(lower) && (!fileish || SOCIAL_RE.test(lower) || EMAIL_RE.test(lower) || MESSAGE_RE.test(lower) || EXTERNAL_RE.test(lower))) {
    patterns.push("social_post.delete", "email.delete");
  }

  return uniqueActionPatterns(patterns);
}

export function inferRuntimeRuleFromText(text: string, fallbackToWildcard = true): RuntimeRuleInference {
  const lower = text.toLowerCase();
  const createOnly = CREATE_ONLY_RE.test(lower);
  const draftOnly = DRAFT_ONLY_RE.test(lower);
  const denyish = DENY_RE.test(lower) || createOnly || draftOnly;
  const approvalish = APPROVAL_RE.test(lower);
  const patterns = inferRuntimeActionPatterns(text);
  const effectivePatterns = uniqueActionPatterns(patterns.length > 0 ? patterns : (fallbackToWildcard ? ["*"] : []));
  const isBlock = denyish && !approvalish;
  const capabilityPatterns = runtimeCapabilitiesForActionPatterns(effectivePatterns);

  return {
    field: isBlock ? "never_allow_actions" : "hitl_required_actions",
    patterns: effectivePatterns,
    capabilityField: isBlock ? "never_allow_capabilities" : "hitl_required_capabilities",
    capabilityPatterns,
    rule_type: draftOnly ? "draft_only" : isBlock ? "deny" : "approval_required",
    label: isBlock ? "Runtime block" : "Approval gate",
    tone: isBlock ? "red" : "orange",
  };
}

export function shouldFallbackToWildcardRule(text: string): boolean {
  const lower = text.toLowerCase();
  return BROAD_SCOPE_RE.test(lower) && (APPROVAL_RE.test(lower) || DENY_RE.test(lower));
}

export function buildRuntimeRuleFromText(text: string, source = "operator", keyPrefix = "operator"): RuntimeRuleDraft {
  const inferred = inferRuntimeRuleFromText(text);
  return {
    rule_key: `${keyPrefix}_${Date.now()}`,
    rule_type: inferred.rule_type,
    description: text,
    severity: inferred.field === "never_allow_actions" ? "high" : "medium",
    action_patterns: inferred.patterns,
    capability_patterns: inferred.capabilityPatterns,
    source,
    enabled: true,
  };
}

export function inferRuntimeRuleEnforcement(rule: any): {
  label: RuntimeRuleInference["label"];
  tone: RuntimeRuleTone;
  patterns: string[];
  capabilityPatterns: string[];
} | null {
  const text = String(`${rule?.description || ""} ${rule?.rule_key || ""} ${rule?.scope || ""}`);
  const explicit = Array.isArray(rule?.action_patterns) ? uniqueActionPatterns(rule.action_patterns) : [];
  const explicitCapabilities = Array.isArray(rule?.capability_patterns) ? uniqueActionPatterns(rule.capability_patterns) : [];
  const patterns = inferRuntimeActionPatterns(text, explicit);
  const capabilityPatterns = explicitCapabilities.length > 0 ? explicitCapabilities : runtimeCapabilitiesForActionPatterns(patterns);
  if (patterns.length === 0 && capabilityPatterns.length === 0) return null;

  const lower = text.toLowerCase();
  const ruleType = String(rule?.rule_type || "").toLowerCase();
  const approvalish = APPROVAL_RE.test(lower);
  const denyish = DENY_RE.test(lower) || DRAFT_ONLY_RE.test(lower) || CREATE_ONLY_RE.test(lower);

  if (["approval_required", "hitl_required", "require_approval", "review_required"].includes(ruleType) || approvalish) {
    return { label: t("lib.runtime_rules.approval_gate"), tone: "orange", patterns, capabilityPatterns };
  }
  if (["deny", "never_allow", "block", "draft_only"].includes(ruleType) || denyish || rule?.severity === "block" || rule?.severity === "deny") {
    return { label: t("lib.runtime_rules.runtime_block"), tone: "red", patterns, capabilityPatterns };
  }
  return null;
}
