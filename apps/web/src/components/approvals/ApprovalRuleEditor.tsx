import { Link } from "react-router-dom";
import { t } from "../../lib/i18n";
import { APPROVAL_CATEGORIES, type ApprovalCatalogItem, type ApprovalDraft, type ApprovalRule, type ApprovalRuleDraft, type ApprovalSelector } from "../../lib/approvalTypes";
import Button from "../ui/Button";
import Input from "../ui/Input";
import Select from "../ui/Select";
import Textarea from "../ui/Textarea";

export function approvalRuleLabel(rule: { selector: ApprovalSelector }, catalog: ApprovalCatalogItem[]): string {
  const selector = rule.selector;
  if (selector.kind === "category") return t(`approval_settings.category.${selector.category}`);
  const label = catalog.find(item => item.action_key === selector.action_key)?.label || selector.action_key;
  return selector.kind === "resource" ? `${label} · ${selector.resource_ids.join(", ")}` : label;
}
function sameOrBroaderDeny(deny: ApprovalRule, selector: ApprovalSelector, catalog: ApprovalCatalogItem[]) {
  if (deny.effect !== "deny") return false;
  const parent = deny.selector;
  if (parent.kind === "category") return parent.category === (selector.kind === "category" ? selector.category : catalog.find(item => item.action_key === selector.action_key)?.category);
  if (selector.kind === "category" || parent.action_key !== selector.action_key) return false;
  if (parent.kind === "action") return true;
  return selector.kind === "resource" && parent.account_id === selector.account_id && parent.resource_kind === selector.resource_kind && parent.resource_ids.some(id => selector.resource_ids.includes(id));
}
export default function ApprovalRuleEditor({ draft, catalog, inherited, derived, disabled, onChange }: {
  draft: ApprovalDraft; catalog: ApprovalCatalogItem[]; inherited: ApprovalRule[]; derived: ApprovalRule[];
  disabled: boolean; onChange: (draft: ApprovalDraft) => void;
}) {
  const categories = APPROVAL_CATEGORIES;
  const update = (index: number, patch: Partial<ApprovalRuleDraft>) => onChange({ ...draft, user_rules: draft.user_rules.map((rule, i) => i === index ? { ...rule, ...patch } : rule) });
  const readOnlyRules = [...inherited, ...derived];
  return <div className="approval-rules">
    {draft.user_rules.length === 0 && <p className="approval-note">{t("approval_settings.empty_rules")}</p>}
    {draft.user_rules.map((rule, index) => {
      const selector = rule.selector;
      const restricted = inherited.some(item => sameOrBroaderDeny(item, selector, catalog));
      const actionKey = selector.kind === "category" ? catalog[0]?.action_key || "" : selector.action_key;
      return <div className="approval-rule" key={rule.id || `draft-${index}`}>
        <div className="approval-rule-fields">
          <Select ariaLabel={t("approval_settings.selector")} value={selector.kind} disabled={disabled} options={["category", "action", "resource"].map(value => ({ value, label: t(`approval_settings.${value}`) }))} onChange={kind => update(index, { selector: kind === "category" ? { kind, category: categories[0] } : kind === "resource" ? { kind, action_key: actionKey, account_id: null, resource_kind: "", resource_ids: [] } : { kind: "action", action_key: actionKey } })} />
          {selector.kind === "category" ? <Select ariaLabel={t("approval_settings.category")} disabled={disabled} value={selector.category} options={categories.map(value => ({ value, label: t(`approval_settings.category.${value}`) }))} onChange={category => update(index, { selector: { kind: "category", category } })} />
            : <Select ariaLabel={t("approval_settings.action")} disabled={disabled} value={selector.action_key} options={[...catalog.map(item => ({ value: item.action_key, label: item.label })), ...(!catalog.some(item => item.action_key === selector.action_key) ? [{ value: selector.action_key, label: selector.action_key }] : [])]} onChange={action_key => update(index, { selector: { ...selector, action_key } })} />}
          <Select ariaLabel={t("approval_settings.effect")} value={rule.effect} disabled={disabled} options={["allow", "ask", "deny"].map(value => ({ value, label: t(`approval_settings.${value}`), disabled: restricted && value !== "deny" }))} onChange={effect => update(index, { effect: effect as ApprovalRuleDraft["effect"] })} />
        </div>
        {selector.kind === "resource" && <div className="approval-rule-targets">
          <label>{t("approval_settings.account_id")}<Input value={selector.account_id || ""} disabled={disabled} placeholder={t("approval_settings.optional")} onChange={event => update(index, { selector: { ...selector, account_id: event.target.value || null } })} /></label>
          <label>{t("approval_settings.resource_kind")}<Input value={selector.resource_kind} disabled={disabled} onChange={event => update(index, { selector: { ...selector, resource_kind: event.target.value } })} /></label>
          <label>{t("approval_settings.resource_ids")}<Textarea rows={2} value={selector.resource_ids.join("\n")} disabled={disabled} onChange={event => update(index, { selector: { ...selector, resource_ids: event.target.value.split("\n") } })} /></label>
        </div>}
        <div className="approval-rule-meta">{restricted && <span>{t("approval_settings.inherited")} · {t("approval_settings.deny")}</span>}<Button size="sm" variant="ghost" disabled={disabled} onClick={() => onChange({ ...draft, user_rules: draft.user_rules.filter((_, i) => i !== index) })}>{t("approval_settings.remove_rule")}</Button></div>
      </div>;
    })}
    {!disabled && catalog.length > 0 && <Button size="sm" variant="outline" onClick={() => onChange({ ...draft, user_rules: [...draft.user_rules, { effect: "ask", selector: { kind: "category", category: categories[0] } }] })}>{t("approval_settings.add_rule")}</Button>}
    {readOnlyRules.map(rule => <div className="approval-inherited-rule" key={`${rule.scope?.kind || "local"}:${rule.id}`}>
      <div><strong>{approvalRuleLabel(rule, catalog)}</strong><span>{t(rule.source_kind === "workspace_rule" ? "approval_settings.derived" : rule.effect === "deny" ? "approval_settings.inherited" : "approval_settings.inherited_rule")}</span></div>
      <span>{t(`approval_settings.${rule.effect}`)}</span>
      {rule.source_link?.startsWith("/") && !rule.source_link.startsWith("//") && <Link to={rule.source_link}>{t("approval_settings.edit_source")}</Link>}
    </div>)}
  </div>;
}
