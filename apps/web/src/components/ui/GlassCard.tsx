interface GlassCardProps {
  children: React.ReactNode;
  className?: string;
  hoverable?: boolean;
  onClick?: () => void;
  onContextMenu?: React.MouseEventHandler<HTMLDivElement>;
  footer?: React.ReactNode;
  style?: React.CSSProperties;
}

export default function GlassCard({ children, className = "", hoverable = true, onClick, onContextMenu, footer, style }: GlassCardProps) {
  return (
    <div
      className={`glass-card ${hoverable ? "cursor-pointer" : ""} ${className}`}
      onClick={onClick}
      onContextMenu={onContextMenu}
      style={{ ...(!hoverable ? { transform: "none" } : {}), ...style }}
    >
      {children}
      {footer && (
        <div
          style={{
            marginTop: "16px",
          }}
        >
          {footer}
        </div>
      )}
    </div>
  );
}
