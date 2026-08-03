/**
 * StatPill — small count badge for stats/filters.
 *
 * Usage:
 *   <StatPill label="12 tasks" />
 *   <StatPill label="3 active" color="var(--accent)" bg="var(--accent-soft)" />
 */

interface StatPillProps {
  label: string;
  color?: string;
  bg?: string;
}

export default function StatPill({
  label,
  color = "var(--text-faint)",
  bg = "var(--surface-muted)",
}: StatPillProps) {
  return (
    <span style={{
      fontSize: 11, fontWeight: 600,
      color, padding: "4px 10px", borderRadius: 20, background: bg,
      fontVariantNumeric: "tabular-nums",
    }}>
      {label}
    </span>
  );
}
