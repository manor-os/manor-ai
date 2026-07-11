/**
 * UpgradePrompt — shown when credits are exhausted or a plan limit is hit (402 from API).
 *
 * Works for all plans — Free users see "Upgrade", Pro users see "Buy More Credits".
 *
 * Usage:
 *   <UpgradePrompt open={showUpgrade} onClose={() => setShowUpgrade(false)} message="..." />
 */
import { useNavigate } from "react-router-dom";
import Modal from "./Modal";
import { t } from "../../lib/i18n";
import type { PlanLimitKind } from "../../lib/api";
import { planLimitOffersCredits, planLimitTitle } from "../../lib/planLimit";

interface UpgradePromptProps {
  open: boolean;
  onClose: () => void;
  message?: string;
  /** Limit type — drives the title and whether "Buy Credits" is offered. */
  kind?: PlanLimitKind;
}

export default function UpgradePrompt({ open, onClose, message, kind }: UpgradePromptProps) {
  const navigate = useNavigate();
  const offersCredits = planLimitOffersCredits(kind);

  return (
    <Modal open={open} onClose={onClose} title={planLimitTitle(kind)} maxWidth="420px"
      footer={
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, width: "100%" }}>
          <button className="btn-manor-ghost" onClick={onClose} style={{ fontSize: 13, height: 36, padding: "0 16px" }}>
            {t("component.upgrade_prompt.dismiss")}
          </button>
          {offersCredits && (
            <button
              className="btn-manor-ghost"
              onClick={() => { onClose(); navigate("/settings?tab=credit"); }}
              style={{ fontSize: 13, height: 36, padding: "0 16px" }}
            >
              {t("component.upgrade_prompt.buy_credits")}
            </button>
          )}
          <button className="btn-manor" onClick={() => { onClose(); navigate("/settings?tab=plans"); }} style={{ fontSize: 13, height: 36, padding: "0 20px" }}>
            {t("component.upgrade_prompt.view_plans")}
          </button>
        </div>
      }>
      <div style={{ textAlign: "center", padding: "8px 0" }}>
        <div style={{
          width: 56, height: 56, borderRadius: 16,
          background: "linear-gradient(135deg, rgba(214,95,89,0.08), rgba(207,155,68,0.08))",
          display: "flex", alignItems: "center", justifyContent: "center",
          margin: "0 auto 16px",
        }}>
          <svg width="28" height="28" fill="none" viewBox="0 0 24 24" stroke="#d65f59" strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
            <path d="M12 9v3.75m-9.303 3.376c-.866 1.5.217 3.374 1.948 3.374h14.71c1.73 0 2.813-1.874 1.948-3.374L13.949 3.378c-.866-1.5-3.032-1.5-3.898 0L2.697 16.126zM12 15.75h.007v.008H12v-.008z" />
          </svg>
        </div>
        <p style={{ fontSize: 14, color: "#57534e", lineHeight: 1.6, margin: 0 }}>
          {message || t("component.upgrade_prompt.default_message")}
        </p>
      </div>
    </Modal>
  );
}
