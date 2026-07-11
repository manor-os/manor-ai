import { type ReactNode } from "react";

/**
 * Header bar shared by the bottom-right floating panels (Manor AI chat,
 * Support). Lays out an optional leading control (e.g. a back button),
 * an avatar, the title + an optional subtitle/status line, and trailing
 * actions on the right.
 */
export default function PanelHeader({
  avatar,
  title,
  subtitle,
  leading,
  actions,
}: {
  avatar?: ReactNode;
  title: ReactNode;
  /** Status line under the title (online dot, "replying…", subtitle text). */
  subtitle?: ReactNode;
  /** Control rendered before the avatar — e.g. a back button. */
  leading?: ReactNode;
  /** Trailing controls on the right — e.g. session switcher, close. */
  actions?: ReactNode;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "14px 16px",
        borderBottom: "1px solid var(--modal-border, #f5f5f4)",
        flexShrink: 0,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
        {leading}
        {avatar}
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              fontSize: 13,
              fontWeight: 700,
              color: "var(--text-strong, #292524)",
              lineHeight: 1.3,
            }}
          >
            {title}
          </div>
          {subtitle}
        </div>
      </div>
      {actions && (
        <div style={{ display: "flex", alignItems: "center", gap: 4 }}>{actions}</div>
      )}
    </div>
  );
}
