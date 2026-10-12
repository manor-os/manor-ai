import { t } from "../../lib/i18n";
import { useApprovalSettings } from "../../lib/approvalQueries";
import type { ApprovalScope } from "../../lib/approvalTypes";
import { IconChevronDown, IconShield } from "../icons";
import { approvalInheritanceLabel, openApprovalSettings } from "./ApprovalSettingsPanel";
import "./approvals.css";

export default function ApprovalModeTrigger({ scope, label, compact = true }: { scope: ApprovalScope; label?: string; compact?: boolean }) {
  const query = useApprovalSettings(scope);
  const mode = query.data ? t(`approval_settings.${query.data.effective_policy.mode}`) : t(query.isPending ? "approval_settings.loading" : "approval_settings.title");
  return <button type="button" className={`approval-mode-trigger ${compact ? "approval-mode-trigger--compact" : "approval-mode-trigger--summary"}`} onClick={() => openApprovalSettings(scope, label)} aria-label={`${t("approval_settings.title")} · ${label || mode}`} title={query.data ? `${mode} · ${approvalInheritanceLabel(query.data)}` : mode}>
    <IconShield size={compact ? 16 : 14} />{!compact && <><span>{mode}</span>{query.data && <small>{approvalInheritanceLabel(query.data)}</small>}<IconChevronDown size={12} /></>}
  </button>;
}
