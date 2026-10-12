interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
  compact?: boolean;
  className?: string;
}

export default function EmptyState({
  icon,
  title,
  description,
  action,
  compact = false,
  className = "",
}: EmptyStateProps) {
  return (
    <div
      className={[
        "manor-empty",
        compact ? "manor-empty--compact" : "",
        className,
      ].filter(Boolean).join(" ")}
    >
      {icon && <div className="manor-empty-icon">{icon}</div>}
      <div className="manor-empty-title">{title}</div>
      {description && (
        <div className="manor-empty-description">{description}</div>
      )}
      {action && <div className="manor-empty-action">{action}</div>}
    </div>
  );
}
