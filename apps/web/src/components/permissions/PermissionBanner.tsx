/**
 * PermissionBanner — P0 global element.
 *
 * Explains *why* a user is seeing a constrained view (legal hold, quarantine,
 * PII auto-upgrade, missing access). Shown at the top of an affected page or
 * resource detail.
 *
 * See docs/PERMISSIONS_UX_DESIGN_ZH.md §1.4.
 */
import type { ReactNode } from "react";
import { IconWarning, IconLock } from "../icons";
import { t } from "../../lib/i18n";

export type PermissionBannerReason =
  | "legal_hold"
  | "quarantine"
  | "pii"
  | "no_access"
  | "viewer_only"
  | "client_view";

interface ActionDef {
  label: string;
  onClick: () => void;
  variant?: "primary" | "secondary";
}

interface Props {
  reason: PermissionBannerReason;
  /** Override the default copy if the page wants context-specific wording. */
  message?: ReactNode;
  /** 0-2 action buttons. Keep it small — banners are not toolbars. */
  actions?: ActionDef[];
}

const REASON_DEFAULTS: Record<
  PermissionBannerReason,
  {
    icon: "warning" | "lock";
    bg: string;
    border: string;
    fg: string;
    messageKey: string;
  }
> = {
  legal_hold: {
    icon: "warning",
    bg: "#f3ecd6",
    border: "#cf9b44",
    fg: "#76502c",
    messageKey: "permissions.banner.legal_hold",
  },
  quarantine: {
    icon: "warning",
    bg: "#f9f4ec",
    border: "#d3873f",
    fg: "#7c4a2e",
    messageKey: "permissions.banner.quarantine",
  },
  pii: {
    icon: "warning",
    bg: "#f9f4ec",
    border: "#d3873f",
    fg: "#7c4a2e",
    messageKey: "permissions.banner.pii",
  },
  no_access: {
    icon: "lock",
    bg: "#f5f5f4",
    border: "#a8a29e",
    fg: "#44403c",
    messageKey: "permissions.banner.no_access",
  },
  viewer_only: {
    icon: "lock",
    bg: "#f3f6fa",
    border: "#8aa9d1",
    fg: "#1e3a8a",
    messageKey: "permissions.banner.viewer_only",
  },
  client_view: {
    icon: "lock",
    bg: "#f5f3ff",
    border: "#a78bfa",
    fg: "#5b21b6",
    messageKey: "permissions.banner.client_view",
  },
};

export default function PermissionBanner({ reason, message, actions }: Props) {
  const cfg = REASON_DEFAULTS[reason];
  const Icon = cfg.icon === "lock" ? IconLock : IconWarning;
  return (
    <div
      role="status"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 12,
        padding: "10px 14px",
        background: cfg.bg,
        borderLeft: `3px solid ${cfg.border}`,
        color: cfg.fg,
        borderRadius: 6,
        fontSize: 13,
        lineHeight: 1.4,
      }}
    >
      <Icon size={16} />
      <span style={{ flex: 1 }}>{message ?? t(cfg.messageKey)}</span>
      {actions && actions.length > 0 && (
        <span style={{ display: "inline-flex", gap: 8 }}>
          {actions.slice(0, 2).map((a, i) => (
            <button
              key={i}
              type="button"
              onClick={a.onClick}
              style={{
                padding: "4px 10px",
                fontSize: 12,
                fontWeight: 600,
                background:
                  a.variant === "primary" ? cfg.fg : "transparent",
                color: a.variant === "primary" ? cfg.bg : cfg.fg,
                border: a.variant === "primary"
                  ? "none"
                  : `1px solid ${cfg.border}`,
                borderRadius: 5,
                cursor: "pointer",
                whiteSpace: "nowrap",
              }}
            >
              {a.label}
            </button>
          ))}
        </span>
      )}
    </div>
  );
}
