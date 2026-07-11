import { useRef, useState } from "react";

interface TooltipProps {
  content: string;
  children: React.ReactNode;
  position?: "top" | "bottom" | "left" | "right";
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
    width: 8, height: 8, background: "#292524", position: "absolute",
  },
  bottom: {
    top: -4, left: "50%", transform: "translateX(-50%) rotate(45deg)",
    width: 8, height: 8, background: "#292524", position: "absolute",
  },
  left: {
    right: -4, top: "50%", transform: "translateY(-50%) rotate(45deg)",
    width: 8, height: 8, background: "#292524", position: "absolute",
  },
  right: {
    left: -4, top: "50%", transform: "translateY(-50%) rotate(45deg)",
    width: 8, height: 8, background: "#292524", position: "absolute",
  },
};

export default function Tooltip({ content, children, position = "top" }: TooltipProps) {
  const [visible, setVisible] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout>>();

  function show() {
    timerRef.current = setTimeout(() => setVisible(true), 300);
  }

  function hide() {
    clearTimeout(timerRef.current);
    setVisible(false);
  }

  return (
    <div
      style={{ position: "relative", display: "inline-flex" }}
      onMouseEnter={show}
      onMouseLeave={hide}
    >
      {children}

      {visible && (
        <div
          style={{
            position: "absolute",
            ...positionStyles[position],
            background: "#292524",
            color: "#fff",
            fontSize: 12,
            fontWeight: 500,
            padding: "5px 10px",
            borderRadius: 8,
            whiteSpace: "nowrap",
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
