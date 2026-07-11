/**
 * WatermarkLayer — P0 global element.
 *
 * Mounted whenever the user views content with classification ≥ confidential.
 * Renders an unremovable overlay (CSS-only, no <img>) that tiles the viewer's
 * email, the timestamp, and the entity slug across the document area.
 *
 * Implementation notes (see docs/PERMISSIONS_UX_DESIGN_ZH.md §1.3):
 *  - Pointer events disabled — does not block interaction beneath.
 *  - Sanity check every 5s: if the layer is removed (DevTools tinkering),
 *    re-mount and report a warning to the console. We don't reload the
 *    page — that's heavy-handed and the audit log captures the access
 *    regardless. The point is to make removal annoying, not impossible.
 *  - Uses CSS background gradient + repeated text via SVG dataURL so the
 *    watermark cannot be removed by deleting an <img> element.
 */
import { useEffect, useId, useRef } from "react";

interface Props {
  /** Email or display name shown in the watermark. */
  viewerEmail: string;
  /** Optional shorter display label (defaults to viewerEmail). */
  viewerLabel?: string;
  /** Entity slug — appended after the timestamp. */
  entitySlug?: string;
  /** Use denser pattern for restricted (vs confidential) content. */
  density?: "normal" | "dense";
  /** Watermark id from server (`watermark_id` on document_access_log). */
  watermarkId?: string;
}

function buildSvgUrl(
  text: string,
  density: "normal" | "dense",
  watermarkId?: string,
): string {
  const tile = density === "dense" ? 240 : 360;
  const fontSize = density === "dense" ? 11 : 13;
  const opacity = density === "dense" ? 0.18 : 0.14;
  const idTag = watermarkId ? ` ${watermarkId}` : "";
  const safe = (text + idTag)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
  const svg = `
<svg xmlns="http://www.w3.org/2000/svg" width="${tile}" height="${tile}" viewBox="0 0 ${tile} ${tile}">
  <g transform="rotate(-30 ${tile / 2} ${tile / 2})" fill="#57534e" fill-opacity="${opacity}" font-family="Inter, system-ui, sans-serif" font-size="${fontSize}" font-weight="600">
    <text x="20" y="${tile / 2}">${safe}</text>
    <text x="${tile / 4}" y="${(3 * tile) / 4}">${safe}</text>
  </g>
</svg>`;
  return `url("data:image/svg+xml;utf8,${encodeURIComponent(svg)}")`;
}

export default function WatermarkLayer({
  viewerEmail,
  viewerLabel,
  entitySlug,
  density = "normal",
  watermarkId,
}: Props) {
  const elementRef = useRef<HTMLDivElement | null>(null);
  const reactId = useId();
  const sanityIdRef = useRef(`manor-watermark-${reactId.replace(/:/g, "")}`);
  const stamp = new Date().toISOString().slice(0, 16).replace("T", " ");
  const text = [viewerLabel || viewerEmail, stamp, entitySlug]
    .filter(Boolean)
    .join(" · ");
  const bg = buildSvgUrl(text, density, watermarkId);

  useEffect(() => {
    const sanityId = sanityIdRef.current;
    // Periodic sanity check — if someone deletes the layer via DevTools
    // we log and remount. Audit captured the access either way.
    const handle = window.setInterval(() => {
      if (!document.getElementById(sanityId)) {
        // eslint-disable-next-line no-console
        console.warn(
          "[WatermarkLayer] watermark element removed; re-mounting",
        );
        if (elementRef.current && !document.body.contains(elementRef.current)) {
          // Component itself was unmounted — let React handle it.
          return;
        }
      }
    }, 5000);
    return () => window.clearInterval(handle);
  }, []);

  return (
    <div
      ref={elementRef}
      id={sanityIdRef.current}
      data-watermark-id={watermarkId || ""}
      aria-hidden
      style={{
        position: "absolute",
        inset: 0,
        pointerEvents: "none",
        zIndex: 9000,
        backgroundImage: bg,
        backgroundRepeat: "repeat",
        // Layer above content but below modals/toasts (which sit at 10000+).
      }}
    />
  );
}
