/**
 * Chip / Pill — small tag for categories, skills, labels.
 *
 * Low-info variant compared to StatusBadge (which implies a status with
 * pulse/dot semantics). Use Chip for labels that are just data.
 */

type ChipVariant =
  | "teal"      // primary (manor)
  | "orange"    // vendor / commercial
  | "blue"      // info
  | "green"     // success / active
  | "red"       // danger
  | "slate"     // neutral / disabled
  | "purple";   // features, specials

// Chips are plain data labels, so they stay neutral by default — no colour
// where none is needed. Only `red` keeps a tint, to flag danger.
const NEUTRAL = { bg: "#f5f5f4", fg: "#57534e" };
const VARIANT_STYLES: Record<ChipVariant, { bg: string; fg: string; border?: string }> = {
  teal:   NEUTRAL,
  orange: NEUTRAL,
  blue:   NEUTRAL,
  green:  NEUTRAL,
  red:    { bg: "#f8f0ef", fg: "#a23e38" },
  slate:  NEUTRAL,
  purple: NEUTRAL,
};

interface ChipProps {
  variant?: ChipVariant;
  size?: "sm" | "md";
  onClick?: () => void;
  children: React.ReactNode;
}

export default function Chip({ variant = "teal", size = "md", onClick, children }: ChipProps) {
  const v = VARIANT_STYLES[variant];
  const padding = size === "sm" ? "2px 8px" : "3px 10px";
  const fontSize = size === "sm" ? 10 : 11;
  return (
    <span
      onClick={onClick}
      style={{
        display: "inline-flex", alignItems: "center", gap: 4,
        padding, fontSize, fontWeight: 600,
        background: v.bg, color: v.fg,
        borderRadius: 6, whiteSpace: "nowrap" as const,
        cursor: onClick ? "pointer" : "default",
      }}
    >
      {children}
    </span>
  );
}
