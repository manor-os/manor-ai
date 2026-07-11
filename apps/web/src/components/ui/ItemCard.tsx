type AccentColor = "teal" | "amber" | "red" | "blue";

interface ItemCardProps {
  icon?: React.ReactNode;
  title: string;
  subtitle?: string;
  accent?: AccentColor;
  actions?: React.ReactNode;
  onClick?: () => void;
  children?: React.ReactNode;
  className?: string;
}

export default function ItemCard({ icon, title, subtitle, accent, actions, onClick, children, className = "" }: ItemCardProps) {
  const accentClass = accent ? `item-card-accent-${accent}` : "";

  return (
    <div
      className={`item-card ${accentClass} ${className}`}
      onClick={onClick}
      style={{ cursor: onClick ? "pointer" : undefined }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
        {icon && <div className="manor-icon-box-sm">{icon}</div>}
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: "13px", fontWeight: 700, color: "#292524" }}>{title}</div>
          {subtitle && (
            <div style={{ fontSize: "12px", color: "#a8a29e", marginTop: "2px" }}>{subtitle}</div>
          )}
        </div>
        {actions && (
          <div style={{ display: "flex", alignItems: "center", gap: "8px", flexShrink: 0 }}>
            {actions}
          </div>
        )}
      </div>
      {children && <div style={{ marginTop: "12px" }}>{children}</div>}
    </div>
  );
}
