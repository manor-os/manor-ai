import { useEffect, useState } from "react";
import { resolveDisplayMediaUrl } from "../../lib/api";
import type { MediaRef } from "../../lib/workflowMedia";

/* Render a workflow node's output media — images inline (ComfyUI-style), video
   and audio with players, everything else as a download chip. Auth-protected
   fs URLs are loaded into object URLs via resolveDisplayMediaUrl; external /
   data URLs pass through. */

export default function MediaPreview({
  refItem,
  maxHeight = 180,
  compact = false,
}: {
  refItem: MediaRef;
  maxHeight?: number;
  compact?: boolean;
}) {
  const { url, type, name } = refItem;
  const [src, setSrc] = useState<string | null>(null);
  const [err, setErr] = useState(false);

  useEffect(() => {
    let alive = true;
    let revoke = () => {};
    setSrc(null);
    setErr(false);
    resolveDisplayMediaUrl(url)
      .then((r) => {
        if (alive) { setSrc(r.url); revoke = r.revoke; }
        else r.revoke();
      })
      .catch(() => alive && setErr(true));
    return () => { alive = false; revoke(); };
  }, [url]);

  // In compact (on-node) mode only images render; other types fall back to a chip.
  if (err || (compact && type !== "image")) return <FileChip name={name} type={type} />;

  if (!src) {
    return (
      <div
        style={{
          width: "100%", height: compact ? 96 : 120, borderRadius: 8,
          background: "var(--surface-sunken)",
          animation: "pulse 1.2s ease-in-out infinite",
        }}
      />
    );
  }

  const radius = 8;
  if (type === "image") {
    return (
      <img
        src={src}
        alt={name || "output"}
        style={{
          display: "block", width: "100%", maxHeight: compact ? 120 : maxHeight,
          objectFit: "contain", borderRadius: radius, background: "var(--surface-sunken)",
        }}
      />
    );
  }
  if (type === "video") {
    return (
      <video
        src={src}
        controls
        playsInline
        preload="metadata"
        aria-label={name || "Video output"}
        style={{ width: "100%", maxHeight, borderRadius: radius, background: "#000" }}
      />
    );
  }
  if (type === "audio") {
    return <audio src={src} controls preload="metadata" aria-label={name || "Audio output"} style={{ width: "100%" }} />;
  }
  return <FileChip name={name} type={type} href={src} />;
}

function FileChip({ name, type, href }: { name?: string; type: string; href?: string }) {
  const label = name || `${type} file`;
  const inner = (
    <span
      style={{
        display: "inline-flex", alignItems: "center", gap: 7, maxWidth: "100%",
        padding: "6px 10px", borderRadius: 8, background: "var(--surface-muted)",
        fontSize: 12, color: "var(--text-muted)", textDecoration: "none",
      }}
    >
      <svg width={14} height={14} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.7} strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0 }}>
        <path d="M14.25 2.25H6.75A1.5 1.5 0 005.25 3.75v16.5a1.5 1.5 0 001.5 1.5h10.5a1.5 1.5 0 001.5-1.5V7.5L14.25 2.25z" />
        <path d="M14.25 2.25V7.5h5.25" />
      </svg>
      <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{label}</span>
    </span>
  );
  return href ? (
    <a href={href} target="_blank" rel="noreferrer" style={{ textDecoration: "none" }}>{inner}</a>
  ) : inner;
}
