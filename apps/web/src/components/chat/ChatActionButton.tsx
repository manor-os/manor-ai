import type { ButtonHTMLAttributes, ReactNode } from "react";

interface ChatActionButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  active?: boolean;
  retry?: boolean;
  children: ReactNode;
}

export default function ChatActionButton({
  active = false,
  retry = false,
  className = "",
  children,
  type = "button",
  ...props
}: ChatActionButtonProps) {
  const classes = [
    "chat-message-action",
    active ? "chat-message-action--active" : "",
    retry ? "chat-message-action--retry" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");

  return (
    <button type={type} className={classes} {...props}>
      {children}
    </button>
  );
}
