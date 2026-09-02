import { useState, type CSSProperties, type ReactNode } from "react";
import { resolveIntegrationBrand, type IntegrationGlyph } from "../lib/brands/catalog";
import { IconConnection, IconEmail, IconFolder, IconManorLogo, IconTerminal, IconWebhook } from "./icons";
import "./IntegrationLogo.css";

const GLYPHS = {
  connection: IconConnection,
  email: IconEmail,
  webhook: IconWebhook,
  folder: IconFolder,
  terminal: IconTerminal,
  manor: IconManorLogo,
} satisfies Record<IntegrationGlyph, typeof IconConnection>;

/** Decorative logo: the containing link/card owns its accessible name. */
export default function IntegrationLogo({
  provider, size = 18, fallback,
}: {
  provider?: string | null;
  size?: number;
  fallback?: ReactNode;
}) {
  const brand = resolveIntegrationBrand(provider);
  const [failedSrc, setFailedSrc] = useState<string>();
  const color = brand?.color || "#78716c";
  const rgb = color.slice(1).match(/../g)?.map(value => parseInt(value, 16)) || [120, 113, 108];
  const brightness = (rgb[0] * 299 + rgb[1] * 587 + rgb[2] * 114) / 1000;
  const icon = brand?.icon;
  if (brand && icon?.src && failedSrc !== icon.src) {
    return <img src={icon.src} width={size} height={size} alt="" aria-hidden="true"
      data-integration-brand={brand.key} draggable={false}
      className="integration-logo" data-monochrome={icon.monochrome || undefined}
      style={{ display: "block", flexShrink: 0, objectFit: "contain" }}
      onError={() => setFailedSrc(icon.src)} />;
  }
  if (brand && icon?.path) {
    return <svg width={size} height={size} viewBox={icon.viewBox || "0 0 24 24"}
      fill={color} aria-hidden="true" focusable="false" data-integration-brand={brand.key}
      className="integration-logo" data-dark-ink={brightness < 75 || undefined}
      data-bright-ink={brightness > 190 || undefined}
      style={{ "--integration-logo-color": color, display: "block", flexShrink: 0 } as CSSProperties}>
      <path d={icon.path} />
    </svg>;
  }
  const Glyph = GLYPHS[brand?.glyph || "connection"];
  return <span aria-hidden="true" data-integration-brand={brand?.key || "unknown"}
    style={{ display: "inline-flex", flexShrink: 0, color }}>
    {fallback ?? <Glyph size={size} />}
  </span>;
}
