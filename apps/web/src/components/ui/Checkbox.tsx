import type { CSSProperties, ReactNode } from "react";
import { IconCheck } from "../icons";

interface CheckboxProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label?: ReactNode;
  disabled?: boolean;
  size?: "sm" | "md";
  className?: string;
  style?: CSSProperties;
  "aria-label"?: string;
}

export default function Checkbox({
  checked,
  onChange,
  label,
  disabled,
  size = "md",
  className,
  style,
  "aria-label": ariaLabel,
}: CheckboxProps) {
  const boxSize = size === "sm" ? 14 : 16;
  const iconSize = size === "sm" ? 10 : 12;

  return (
    <button
      type="button"
      role="checkbox"
      aria-checked={checked}
      aria-label={ariaLabel || (typeof label === "string" ? label : undefined)}
      disabled={disabled}
      className={className}
      onClick={(event) => {
        event.stopPropagation();
        if (!disabled) onChange(!checked);
      }}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 7,
        minWidth: 0,
        padding: 0,
        border: "none",
        background: "transparent",
        color: disabled ? "#a8a29e" : "#57534e",
        cursor: disabled ? "not-allowed" : "pointer",
        font: "inherit",
        textAlign: "left",
        opacity: disabled ? 0.62 : 1,
        ...style,
      }}
    >
      <span
        aria-hidden
        style={{
          width: boxSize,
          height: boxSize,
          borderRadius: 4,
          border: checked ? "1px solid #436b65" : "1px solid rgba(120,113,108,0.5)",
          background: checked ? "linear-gradient(135deg, #4f7d75, #436b65)" : "rgba(255,255,255,0.86)",
          boxShadow: checked ? "0 2px 6px rgba(67,107,101,0.16)" : "inset 0 1px 1px rgba(28,25,23,0.04)",
          display: "inline-flex",
          alignItems: "center",
          justifyContent: "center",
          flexShrink: 0,
          transition: "background 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease",
        }}
      >
        {checked && <IconCheck size={iconSize} style={{ color: "#fff", strokeWidth: 3 }} />}
      </span>
      {label && <span style={{ minWidth: 0 }}>{label}</span>}
    </button>
  );
}
