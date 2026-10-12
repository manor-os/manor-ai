import { Link } from "react-router-dom";
import { emailProviderLabel, type EmailAccount } from "../../lib/emailAccounts";
import { t } from "../../lib/i18n";

export default function DefaultEmailSender({ account }: { account?: EmailAccount }) {
  return <div className="space-y-1.5 text-sm">
    <p className="m-0 font-medium text-[var(--text-default)]">{t("email_accounts.default_sender")}</p>
    {account
      ? <p className="m-0 break-all text-[var(--text-muted)]">{account.email} · {emailProviderLabel(account.provider)}</p>
      : <p role="alert" className="m-0 text-[var(--text-muted)]">{t("email_accounts.default_unavailable")}</p>}
    <Link to="/integrations?provider=email" className="inline-block text-xs text-[var(--text-muted)] underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]">{t("email_accounts.manage")}</Link>
  </div>;
}
