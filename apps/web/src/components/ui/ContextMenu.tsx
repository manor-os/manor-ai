import { useState, useEffect, useRef, useCallback } from "react";
import { createPortal } from "react-dom";

export interface MenuItem {
  label: string;
  icon?: React.ReactNode;
  danger?: boolean;
  disabled?: boolean;
  divider?: boolean;
  onClick?: () => void;
}

interface ContextMenuProps {
  items: MenuItem[];
  x: number;
  y: number;
  onClose: () => void;
}

export default function ContextMenu({ items, x, y, onClose }: ContextMenuProps) {
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState({ x, y });
  const [visible, setVisible] = useState(false);

  // Adjust position to stay within viewport and trigger fade-in
  useEffect(() => {
    const el = menuRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    let adjustedX = x;
    let adjustedY = y;
    if (x + rect.width > vw - 8) adjustedX = vw - rect.width - 8;
    if (y + rect.height > vh - 8) adjustedY = vh - rect.height - 8;
    if (adjustedX < 8) adjustedX = 8;
    if (adjustedY < 8) adjustedY = 8;
    setPos({ x: adjustedX, y: adjustedY });
    // Trigger fade-in on next frame
    requestAnimationFrame(() => setVisible(true));
  }, [x, y]);

  // Close on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        onClose();
      }
    }
    document.addEventListener("mousedown", handleClick, true);
    return () => document.removeEventListener("mousedown", handleClick, true);
  }, [onClose]);

  // Close on Escape
  useEffect(() => {
    function handleKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKey);
    return () => document.removeEventListener("keydown", handleKey);
  }, [onClose]);

  return createPortal(
    <div
      ref={menuRef}
      style={{
        position: "fixed",
        left: pos.x,
        top: pos.y,
        zIndex: 9999,
        background: "var(--surface-panel)",
        border: "1px solid var(--border-default)",
        boxShadow: "var(--shadow-lg)",
        opacity: visible ? 1 : 0,
        transform: visible ? "scale(1)" : "scale(0.95)",
        transition: "opacity 0.12s ease-out, transform 0.12s ease-out",
        transformOrigin: "top left",
      }}
      className="manor-context-menu min-w-[180px] py-1.5 rounded-xl backdrop-blur-xl"
    >
      {items.map((item, i) => {
        if (item.divider) {
          return <hr key={i} className="my-1.5 border-t" style={{ borderColor: "var(--border-subtle)" }} />;
        }
        return (
          <button
            key={i}
            disabled={item.disabled}
            onClick={() => {
              if (!item.disabled && item.onClick) {
                item.onClick();
                onClose();
              }
            }}
            className={`
              manor-context-menu-item
              w-full flex items-center gap-2.5 px-3.5 py-2 text-left text-[13px] font-medium
              transition-colors duration-100 border-none bg-transparent cursor-pointer
              ${item.disabled
                ? "is-disabled cursor-not-allowed"
                : item.danger
                  ? "is-danger"
                  : ""
              }
            `}
          >
            {item.icon && (
              <span className={`flex-shrink-0 w-4 h-4 flex items-center justify-center ${
                item.disabled ? "opacity-40" : ""
              }`}>
                {item.icon}
              </span>
            )}
            <span>{item.label}</span>
          </button>
        );
      })}
    </div>,
    document.body
  );
}

// Hook for context menu state
export function useContextMenu() {
  const [menu, setMenu] = useState<{ x: number; y: number; items: MenuItem[] } | null>(null);

  const show = useCallback((e: React.MouseEvent, items: MenuItem[]) => {
    e.preventDefault();
    e.stopPropagation();
    setMenu({ x: e.clientX, y: e.clientY, items });
  }, []);

  const close = useCallback(() => setMenu(null), []);

  return { menu, show, close };
}
