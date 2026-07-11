interface ProductCardProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  badge?: React.ReactNode;
  footer?: React.ReactNode;
  onClick?: () => void;
  className?: string;
}

export default function ProductCard({ icon, title, description, badge, footer, onClick, className = "" }: ProductCardProps) {
  return (
    <div
      className={`product-card ${className}`}
      onClick={onClick}
      style={{ cursor: onClick ? "pointer" : undefined }}
    >
      {icon && (
        <div className="manor-icon-box" style={{ marginBottom: "16px" }}>
          {icon}
        </div>
      )}
      {badge && <div style={{ marginBottom: "8px" }}>{badge}</div>}
      <div style={{ fontSize: "14px", fontWeight: 700, color: "#292524", marginBottom: "6px" }}>
        {title}
      </div>
      {description && (
        <div style={{ fontSize: "12px", color: "#a8a29e", lineHeight: 1.5 }}>
          {description}
        </div>
      )}
      {footer && (
        <div style={{ marginTop: "16px", width: "100%" }}>
          {footer}
        </div>
      )}
    </div>
  );
}
