interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
}

export default function EmptyState({ icon, title, description, action }: EmptyStateProps) {
  return (
    <div className="manor-empty">
      {icon && <div className="manor-empty-icon">{icon}</div>}
      <div style={{ fontSize: "15px", fontWeight: 700, color: "#57534e", marginBottom: "6px" }}>
        {title}
      </div>
      {description && (
        <div style={{ fontSize: "13px", color: "#a8a29e", marginBottom: action ? "16px" : undefined }}>
          {description}
        </div>
      )}
      {action && <div>{action}</div>}
    </div>
  );
}
