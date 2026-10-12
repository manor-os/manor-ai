import Button from "./Button";

export function FeatureDiscoveryBadge({
  label,
  onClick,
  expanded,
  controls,
}: {
  label: string;
  onClick?: () => void;
  expanded?: boolean;
  controls?: string;
}) {
  if (!onClick) {
    return (
      <span className="inline-flex shrink-0 items-center rounded-full bg-[var(--surface-sunken)] px-2 py-0.5 text-[10px] font-semibold normal-case tracking-normal text-[var(--text-muted)]">
        {label}
      </span>
    );
  }
  return (
    <button
      type="button"
      onClick={onClick}
      aria-expanded={expanded}
      aria-controls={expanded ? controls : undefined}
      className="min-h-8 shrink-0 whitespace-nowrap rounded-full bg-[var(--surface-sunken)] px-2 text-[10px] font-semibold normal-case tracking-normal text-[var(--text-muted)] hover:text-[var(--text-strong)] focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-[var(--accent-ring)]"
    >
      {label}
    </button>
  );
}

/** A small in-context hint; the caller owns its per-user visibility state. */
export default function FeatureDiscoveryHint({
  id,
  title,
  description,
  actionLabel,
  laterLabel,
  onAction,
  onDismiss,
}: {
  id: string;
  title: string;
  description: string;
  actionLabel: string;
  laterLabel: string;
  onAction: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      id={id}
      role="region"
      aria-label={title}
      className="mx-2 my-2 min-w-0 rounded-[var(--radius-card)] bg-[var(--surface-muted)] p-3 shadow-[var(--shadow-sm)]"
    >
      <div className="text-xs font-semibold text-[var(--text-strong)]">{title}</div>
      <p className="mt-1 text-xs leading-relaxed text-[var(--text-muted)]">{description}</p>
      <div className="mt-3 flex flex-wrap gap-1.5">
        <Button size="sm" onClick={onAction}>{actionLabel}</Button>
        <Button size="sm" variant="ghost" onClick={onDismiss}>{laterLabel}</Button>
      </div>
    </div>
  );
}
