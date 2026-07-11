import { useEffect } from "react";
import { useDetailStore, type DetailAction } from "../../stores/detail";

/**
 * DetailDrawer — the global detail pop-up. Mounted once at the app root
 * (see main.tsx); driven entirely by the `detail` store. Cards/rows call
 * `openDetail({ icon, title, subtitle, badges, body, actions })` to show it.
 *
 * Follows the design system: a centered, borderless frosted-glass card on a
 * dimmed scrim, low-saturation tokens throughout; Esc / scrim-click to close.
 */
export default function DetailDrawer() {
  const payload = useDetailStore((s) => s.payload);
  const closeDetail = useDetailStore((s) => s.closeDetail);
  const open = !!payload;

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeDetail();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, closeDetail]);

  if (!payload) return null;
  const width = payload.width ?? 520;

  return (
    <div
      className="detail-scrim"
      onClick={closeDetail}
      role="presentation"
    >
      <aside
        className="detail-drawer"
        style={{ width: `min(${width}px, calc(100vw - 32px))` }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={typeof payload.title === "string" ? payload.title : undefined}
      >
        <button
          className="detail-drawer-close"
          onClick={closeDetail}
          aria-label="Close"
        >
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2.2} strokeLinecap="round">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>

        <div className="detail-drawer-header">
          <div className="detail-drawer-heading">
            {payload.icon && <div className="detail-drawer-icon">{payload.icon}</div>}
            <div className="detail-drawer-titlewrap">
              <div className="detail-drawer-title">{payload.title}</div>
              {payload.subtitle && (
                <div className="detail-drawer-subtitle">{payload.subtitle}</div>
              )}
              {payload.badges && (
                <div className="detail-drawer-badges">
                  {payload.badges}
                </div>
              )}
            </div>
          </div>
        </div>

        <div className="detail-drawer-body">{payload.body}</div>

        {payload.actions ? (
          <div className="detail-drawer-footer">{payload.actions}</div>
        ) : (
          <DetailActions
            primary={payload.primaryAction}
            secondary={payload.secondaryActions}
            danger={payload.dangerAction}
          />
        )}
      </aside>
    </div>
  );
}

/**
 * Footer action layout — handles 1..N actions gracefully:
 *  - primary: prominent, full-width
 *  - secondary: an equal-width grid (auto-fits, never ragged)
 *  - danger: a separate, low-key row so it can't be fat-fingered
 */
function DetailActions({
  primary, secondary, danger,
}: {
  primary?: DetailAction;
  secondary?: DetailAction[];
  danger?: DetailAction;
}) {
  const secs = (secondary || []).filter(Boolean);
  if (!primary && secs.length === 0 && !danger) return null;
  // Secondary + danger render as compact icon buttons with a hover tooltip,
  // keeping the pop-up clean; the primary action keeps its label.
  const labelText = (l: DetailAction["label"]) =>
    typeof l === "string" ? l : undefined;
  return (
    <div className="detail-drawer-actions">
      {primary && (
        <button
          className="detail-action-primary"
          onClick={primary.onClick}
          disabled={primary.disabled}
        >
          {primary.icon}
          {primary.label}
        </button>
      )}
      {(secs.length > 0 || danger) && (
        <div className="detail-action-iconrow">
          <div className="detail-action-icons">
            {secs.map((a, i) => (
              <button
                key={i}
                className="detail-action-icon"
                title={labelText(a.label)}
                aria-label={labelText(a.label)}
                onClick={a.onClick}
                disabled={a.disabled}
              >
                {a.icon}
              </button>
            ))}
          </div>
          {danger && (
            <button
              className="detail-action-icon is-danger"
              title={labelText(danger.label)}
              aria-label={labelText(danger.label)}
              onClick={danger.onClick}
              disabled={danger.disabled}
            >
              {danger.icon}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
