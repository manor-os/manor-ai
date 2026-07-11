/**
 * StatPill — small count badge for stats/filters.
 *
 * Usage:
 *   <StatPill label="12 tasks" />
 *   <StatPill label="3 active" color="#437f6b" bg="#dceae3" />
 *   <StatPill label="2 errors" color="#c14a44" bg="#f1dddb" />
 */

interface StatPillProps {
  label: string;
  color?: string;
  bg?: string;
}

export default function StatPill({ label, color = "#57534e", bg = "rgba(245,245,244,0.8)" }: StatPillProps) {
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
