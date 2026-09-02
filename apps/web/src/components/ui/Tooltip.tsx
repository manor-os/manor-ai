import { useRef, useState } from "react";

interface TooltipProps {
  content: string;
  children: React.ReactNode;
  position?: "top" | "bottom" | "left" | "right";
  className?: string;
  hideOnClick?: boolean;
  suppressWhenExpanded?: boolean;
}

const positionStyles: Record<string, React.CSSProperties> = {
  top: { bottom: "calc(100% + 8px)", left: "50%", transform: "translateX(-50%)" },
  bottom: { top: "calc(100% + 8px)", left: "50%", transform: "translateX(-50%)" },
  left: { right: "calc(100% + 8px)", top: "50%", transform: "translateY(-50%)" },
  right: { left: "calc(100% + 8px)", top: "50%", transform: "translateY(-50%)" },
};

const arrowStyles: Record<string, React.CSSProperties> = {
  top: {
    bottom: -4, left: "50%", transform: "translateX(-50%) rotate(45deg)",
    width: 8, height: 8, background: "var(--ink)", position: "absolute",
  },
  bottom: {
    top: -4, left: "50%", transform: "translateX(-50%) rotate(45deg)",
    width: 8, height: 8, background: "var(--ink)", position: "absolute",
  },
  left: {
    right: -4, top: "50%", transform: "translateY(-50%) rotate(45deg)",
    width: 8, height: 8, background: "var(--ink)", position: "absolute",
  },
  right: {
    left: -4, top: "50%", transform: "translateY(-50%) rotate(45deg)",
    width: 8, height: 8, background: "var(--ink)", position: "absolute",
  },
};

export default function Tooltip({
  content,
  children,
  position = "top",
  className = "",
  hideOnClick = false,
  suppressWhenExpanded = false,
}: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();
  const wrapperRef = useRef<HTMLDivElement>(null);

  function expanded() {
    return suppressWhenExpanded
      && Boolean(wrapperRef.current?.querySelector('[aria-expanded="true"]'));
  }

  function show() {
    if (expanded()) return;
    timerRef.current = setTimeout(() => setVisible(true), 300);
  }

  function hide() {
    clearTimeout(timerRef.current);
    setVisible(false);
  }

  function showImmediately() {
    if (expanded()) return;
    clearTimeout(timerRef.current);
    setVisible(true);
  }

  return (
    <div
      ref={wrapperRef}
      className={className}
      style={{ position: "relative", display: "inline-flex" }}
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocusCapture={showImmediately}
      onBlurCapture={hide}
      onClickCapture={hideOnClick ? hide : undefined}
    >
      {children}

      {visible && (
        <div
          role="tooltip"
          style={{
            position: "absolute",
            ...positionStyles[position],
            width: "max-content",
            maxWidth: 300,
            background: "var(--ink)",
            color: "var(--surface-panel)",
            fontSize: 12,
            fontWeight: 500,
            lineHeight: 1.45,
            padding: "7px 10px",
            borderRadius: 8,
            whiteSpace: "normal",
            textAlign: "left",
            boxShadow: "var(--shadow-md)",
            zIndex: 100,
            pointerEvents: "none",
            animation: "fade-in 0.15s ease",
          }}
        >
          <div style={arrowStyles[position]} />
          {content}
        </div>
      )}
    </div>
  );
}
