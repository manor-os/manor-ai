import { IconManorLogo } from "../icons";
import type { CSSProperties } from "react";

interface ManorAvatarProps {
  size?: number;
  className?: string;
  style?: CSSProperties;
  shape?: "circle" | "rounded";
}

/**
 * Manor AI branded avatar — dark navy circle with white "M" logo.
 * Use this for the default Manor AI agent across all chat UIs.
 */
export default function ManorAvatar({
  size = 36,
  className,
  style,
  shape = "circle",
}: ManorAvatarProps) {
  const radius = shape === "circle" ? "50%" : Math.round(size * 0.28);
  return (
    <div
      className={className}
      style={{
        width: size,
        height: size,
        minWidth: size,
        minHeight: size,
        borderRadius: radius,
        background: "#1c1917",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexShrink: 0,
        ...style,
      }}
    >
      <IconManorLogo size={Math.round(size * 0.4)} style={{ color: "#fff" }} />
    </div>
  );
}
