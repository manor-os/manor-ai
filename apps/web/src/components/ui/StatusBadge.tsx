// Minimal: neutral chip + a small coloured dot carries the meaning.
// Colour on the label itself is reserved for alert states (danger/red).
const BADGE_STYLES: Record<string, { badge: string; dot: string }> = {
  active:   { badge: "bg-stone-100 text-stone-700", dot: "bg-emerald-500" },
  inactive: { badge: "bg-stone-100 text-stone-500", dot: "bg-stone-400" },
  success:  { badge: "bg-stone-100 text-stone-700", dot: "bg-emerald-500" },
  warning:  { badge: "bg-stone-100 text-stone-700", dot: "bg-amber-500" },
  danger:   { badge: "bg-red-50 text-red-700",      dot: "bg-red-500" },
  info:     { badge: "bg-stone-100 text-stone-700", dot: "bg-stone-400" },
  teal:     { badge: "bg-stone-100 text-stone-700", dot: "bg-manor-500" },
  purple:   { badge: "bg-stone-100 text-stone-700", dot: "bg-stone-400" },
  blue:     { badge: "bg-stone-100 text-stone-700", dot: "bg-stone-400" },
  gray:     { badge: "bg-stone-100 text-stone-600", dot: "bg-stone-400" },
  red:      { badge: "bg-red-50 text-red-700",      dot: "bg-red-500" },
  green:    { badge: "bg-stone-100 text-stone-700", dot: "bg-emerald-500" },
  orange:   { badge: "bg-stone-100 text-stone-700", dot: "bg-amber-500" },
};

interface StatusBadgeProps {
  type?: string;
  dot?: boolean;
  pulse?: boolean;
  children: React.ReactNode;
}

export default function StatusBadge({ type = "info", dot = false, pulse = false, children }: StatusBadgeProps) {
  const styles = BADGE_STYLES[type] || BADGE_STYLES.info;

  return (
    <span
      className={`inline-flex items-center gap-1.5 px-2.5 py-0.5 rounded-md text-[11px] font-semibold leading-5 ${styles.badge}`}
    >
      {dot && (
        <span
          className={`w-1.5 h-1.5 rounded-full shrink-0 ${styles.dot} ${pulse ? "animate-pulse" : ""}`}
        />
      )}
      {children}
    </span>
  );
}
