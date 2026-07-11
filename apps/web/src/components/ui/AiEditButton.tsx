import type { ButtonHTMLAttributes } from "react";
import { IconSparkles } from "../icons";

type AiEditButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  label?: string;
  iconSize?: number;
};

export default function AiEditButton({
  label = "AI edit",
  iconSize = 15,
  className,
  children,
  type = "button",
  ...props
}: AiEditButtonProps) {
  return (
    <button
      {...props}
      type={type}
      className={["ai-edit-button", className].filter(Boolean).join(" ")}
    >
      <span className="ai-edit-button__glow" aria-hidden="true" />
      <IconSparkles size={iconSize} />
      <span className="ai-edit-button__label">{children ?? label}</span>
    </button>
  );
}
