/**
 * IconTile — brand-tinted square that holds a provider icon or monogram,
 * with an optional status dot in the bottom-right corner.
 *
 * Used by Card and standalone wherever a provider/integration/agent
 * needs a recognisable visual chip.
 */

interface IconTileProps {
  /** Brand colour — used as the foreground and as a 10% tint for the
   *  background. Falls back to manor teal. */
  color?: string;
  /** Pixel size of the square. Defaults to 40. */
  size?: number;
  /** Optional status dot rendered in the bottom-right corner. */
  status?: { color: string; label?: string };
  /** Tooltip on hover (often a status label). */
  title?: string;
  /** Icon node or monogram text. */
  children: React.ReactNode;
  className?: string;
}

export default function IconTile({
  color = "#78716c",
  size = 40,
  status,
  title,
  children,
  className = "",
}: IconTileProps) {
  // 10% brand tint for the tile background
  const tileBg = `${color}1a`;
  const radius = Math.round(size * 0.25);
  const dot = Math.max(8, Math.round(size * 0.25));
  const fontSize = Math.round(size * 0.32);

  return (
    <div
      className={className}
      title={title || status?.label}
      style={{
        width: size, height: size, borderRadius: radius, flexShrink: 0,
        background: tileBg,
        color,
        display: "flex", alignItems: "center", justifyContent: "center",
        position: "relative" as const,
        fontWeight: 700, fontSize,
      }}
    >
      {children}
      {status && (
        <span style={{
          position: "absolute" as const,
          bottom: -2, right: -2,
          width: dot, height: dot, borderRadius: "50%",
          background: status.color,
          border: "2px solid var(--glass-card)",
        }} />
      )}
    </div>
  );
}
