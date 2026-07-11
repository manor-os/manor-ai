import { useNavigate } from "react-router-dom";
import type { PlanLimitDetail } from "../../lib/api";
import { t } from "../../lib/i18n";
import { planLimitOffersCredits, planLimitTitle } from "../../lib/planLimit";

interface Props {
  detail?: PlanLimitDetail;
  compact?: boolean;
}

export default function CreditLimitNotice({ detail, compact = false }: Props) {
  const navigate = useNavigate();
  const message =
    detail?.message ||
    t("component.upgrade_prompt.default_message");
  const offersCredits = planLimitOffersCredits(detail?.kind);

  return (
    <div
      style={{
        border: "1px solid rgba(214,95,89,0.18)",
        background: "rgba(248,240,239,0.92)",
        borderRadius: 8,
        padding: compact ? "10px" : "12px",
        color: "#7f1d1d",
        boxShadow: compact ? "none" : "0 8px 24px rgba(127,29,29,0.06)",
      }}
    >
      <div style={{ display: "flex", gap: 10, alignItems: "flex-start" }}>
        <div
          aria-hidden="true"
          style={{
            width: 28,
            height: 28,
            borderRadius: 8,
            background: "#f1dddb",
            color: "#c14a44",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            flexShrink: 0,
          }}
        >
          !
        </div>
        <div style={{ minWidth: 0, flex: 1 }}>
          <div style={{ fontSize: 13, fontWeight: 700, marginBottom: 4 }}>
            {planLimitTitle(detail?.kind)}
          </div>
          <div style={{ fontSize: 12, lineHeight: 1.5, color: "#883a35" }}>
            {message}
          </div>
          {(detail?.limit != null || detail?.current != null || detail?.plan) && (
            <div style={{ fontSize: 11, marginTop: 6, color: "#a23e38" }}>
              {detail?.plan
                ? t("component.credit_limit_notice.plan_suffix").replace("{plan}", detail.plan)
                : t("component.credit_limit_notice.current_plan")}
              {detail?.current != null && detail?.limit != null
                ? ` · ${t("component.credit_limit_notice.usage")
                    .replace("{current}", detail.current.toLocaleString())
                    .replace("{limit}", detail.limit.toLocaleString())}`
                : ""}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
