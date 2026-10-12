import { useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useDetailStore, type DetailAction } from "../../stores/detail";
import { trapDialogTabKey } from "./Modal";

function isTopDetailLayer(): boolean {
  const renderedLayers = document.body.querySelectorAll<HTMLElement>(
    "[data-manor-modal-layer-id]",
  );
  return renderedLayers.item(renderedLayers.length - 1)?.dataset.manorModalLayerId
    === "detail-drawer";
}

/**
 * DetailDrawer — the global detail pop-up. Mounted once in the app shell
 * (see main.tsx) and portaled to document.body; driven entirely by the
 * `detail` store. Cards/rows call `openDetail({ icon, title, subtitle, badges,
 * body, actions })` to show it.
 *
 * Follows the design system: a centered, borderless frosted-glass card on a
 * dimmed scrim, low-saturation tokens throughout; Esc / scrim-click to close.
 */
export default function DetailDrawer() {
  const payload = useDetailStore((s) => s.payload);
  const closeDetail = useDetailStore((s) => s.closeDetail);
  const open = !!payload;
  const drawerRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const returnFocusFrameRef = useRef<number | null>(null);
  const payloadRef = useRef(payload);
  payloadRef.current = payload;

  useEffect(() => {
    if (!open) return;
    if (returnFocusFrameRef.current !== null) {
      window.cancelAnimationFrame(returnFocusFrameRef.current);
      returnFocusFrameRef.current = null;
    }
    const activeElement = document.activeElement;
    returnFocusRef.current = payloadRef.current?.returnFocusTo
      ?? (activeElement instanceof HTMLElement ? activeElement : null);
    const onKey = (event: KeyboardEvent) => {
      const drawer = drawerRef.current;
      if (!drawer || !isTopDetailLayer()) return;
      if (event.key === "Escape") {
        const target = event.target;
        if (
          target instanceof Element
          && target.closest('[data-manor-popup-open="true"]')
        ) {
          return;
        }
        event.preventDefault();
        event.stopPropagation();
        closeDetail();
        return;
      }
      if (event.key === "Tab") {
        event.stopPropagation();
        trapDialogTabKey(event, drawer);
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      const returnFocusTo = returnFocusRef.current;
      returnFocusRef.current = null;
      returnFocusFrameRef.current = window.requestAnimationFrame(() => {
        returnFocusFrameRef.current = null;
        if (
          returnFocusTo?.isConnected
          && !returnFocusTo.closest("[inert]")
          && !document.body.querySelector("[data-manor-modal-layer-id]")
        ) {
          returnFocusTo.focus();
        }
      });
    };
  }, [open, closeDetail]);

  // Keep the final return target for the drawer's whole lifetime, but move
  // focus into every replacement payload (for example list → detail → list).
  // Otherwise the focused card can unmount and leave focus on document.body.
  useEffect(() => {
    if (!payload) return;
    const focusFrame = window.requestAnimationFrame(() => {
      const drawer = drawerRef.current;
      if (!drawer?.contains(document.activeElement)) {
        closeButtonRef.current?.focus();
      }
    });
    return () => window.cancelAnimationFrame(focusFrame);
  }, [payload]);

  if (!payload || typeof document === "undefined") return null;
  const width = payload.width ?? 520;

  return createPortal(
    <div
      className="detail-scrim"
      data-manor-modal-layer-id="detail-drawer"
      onClick={closeDetail}
      role="presentation"
    >
      <aside
        ref={drawerRef}
        className="detail-drawer"
        style={{ width: `min(${width}px, calc(100vw - 32px))` }}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={typeof payload.title === "string" ? payload.title : undefined}
        tabIndex={-1}
      >
        <button
          ref={closeButtonRef}
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
    </div>,
    document.body,
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
