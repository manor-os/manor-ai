import LoadingSpinner from "./LoadingSpinner";

interface ButtonProps {
  variant?: "primary" | "outline" | "ghost" | "danger" | "teal-light";
  size?: "sm" | "md" | "lg";
  loading?: boolean;
  disabled?: boolean;
  children: React.ReactNode;
  onClick?: (e: React.MouseEvent<HTMLButtonElement>) => void;
  className?: string;
  style?: React.CSSProperties;
  type?: "button" | "submit";
  title?: string;
  ariaLabel?: string;
}

const variantClass: Record<string, string> = {
  primary: "btn-manor",
  outline: "btn-manor-outline",
  ghost: "btn-manor-ghost",
  danger: "btn-manor-danger",
  "teal-light": "btn-manor-teal-light",
};

const sizeStyles: Record<string, React.CSSProperties> = {
  sm: { height: 30, padding: "0 12px", fontSize: 12, borderRadius: 8 },
  md: {},
  lg: { height: "auto", padding: "13px 20px", fontSize: 14, borderRadius: 16, fontWeight: 700 },
};

export default function Button({
  variant = "primary",
  size = "md",
  loading = false,
  disabled = false,
  children,
  onClick,
  className = "",
  style,
  type = "button",
  title,
  ariaLabel,
}: ButtonProps) {
  const base = size === "lg" && variant === "primary" ? "btn-manor-lg" : variantClass[variant];
  const isDisabled = disabled || loading;

  return (
    <button
      type={type}
      className={`${base} ${className}`}
      style={{
        ...(size !== "lg" ? sizeStyles[size] : {}),
        ...(isDisabled ? { opacity: 0.55, cursor: "not-allowed", pointerEvents: "none" } : {}),
        ...style,
      }}
      disabled={isDisabled}
      onClick={onClick}
      title={title}
      aria-label={ariaLabel}
    >
      {loading && <LoadingSpinner size={size === "sm" ? 14 : 16} />}
      {children}
    </button>
  );
}
