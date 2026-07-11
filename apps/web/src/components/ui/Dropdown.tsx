import { useEffect, useRef, useState, type KeyboardEvent, type MouseEvent as ReactMouseEvent } from "react";
import { createPortal } from "react-dom";

interface DropdownItem {
  key: string;
  label: string;
  icon?: React.ReactNode;
  danger?: boolean;
  disabled?: boolean;
}

interface DropdownProps {
  trigger: React.ReactNode;
  items: DropdownItem[];
  onSelect: (key: string) => void;
  align?: "left" | "center" | "right";
  style?: React.CSSProperties;
}

export default function Dropdown({
  trigger,
  items,
  onSelect,
  align = "left",
  style,
}: DropdownProps) {
  const [open, setOpen] = useState(false);
  const [menuPos, setMenuPos] = useState({ top: 0, left: 0 });
  const ref = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  function toggleOpen(event?: ReactMouseEvent<HTMLDivElement> | KeyboardEvent<HTMLDivElement>) {
    event?.stopPropagation();
    setOpen((v) => !v);
  }

  useEffect(() => {
    if (!open) return;

    function updatePosition() {
      const triggerRect = ref.current?.getBoundingClientRect();
      const menuRect = menuRef.current?.getBoundingClientRect();
      if (!triggerRect || !menuRect) return;

      const gap = 6;
      const viewportWidth = window.innerWidth;
      const viewportHeight = window.innerHeight;

      const preferredLeft =
        align === "right"
          ? triggerRect.right - menuRect.width
          : align === "center"
            ? triggerRect.left + (triggerRect.width - menuRect.width) / 2
          : triggerRect.left;
      const preferredTop = triggerRect.bottom + gap;

      const maxLeft = Math.max(0, viewportWidth - menuRect.width);
      const maxTop = Math.max(0, viewportHeight - menuRect.height);

      const left = Math.min(Math.max(0, preferredLeft), maxLeft);

      let top = Math.min(Math.max(0, preferredTop), maxTop);

      if (preferredTop > maxTop) {
        top = Math.max(0, triggerRect.top - gap - menuRect.height);
      }

      setMenuPos({ top, left });
    }

    function handleClick(e: MouseEvent) {
      const target = e.target as Node;
      if (ref.current?.contains(target) || menuRef.current?.contains(target)) {
        return;
      }
      setOpen(false);
    }

    const frame = window.requestAnimationFrame(updatePosition);
    document.addEventListener("mousedown", handleClick);
    window.addEventListener("resize", updatePosition);
    window.addEventListener("scroll", updatePosition, true);

    return () => {
      window.cancelAnimationFrame(frame);
      document.removeEventListener("mousedown", handleClick);
      window.removeEventListener("resize", updatePosition);
      window.removeEventListener("scroll", updatePosition, true);
    };
  }, [open, align, items]);

  return (
    <div ref={ref} style={{ position: "relative", display: "inline-block", ...style }}>
      <div
        role="button"
        tabIndex={0}
        aria-haspopup="menu"
        aria-expanded={open}
        onMouseDown={(event) => event.stopPropagation()}
        onClick={toggleOpen}
        onKeyDown={(event) => {
          if (event.key === "Enter" || event.key === " ") {
            event.preventDefault();
            toggleOpen(event);
          }
        }}
        style={{ cursor: "pointer" }}
      >
        {trigger}
      </div>

      {open &&
        createPortal(
          <div
            ref={menuRef}
            className="manor-dropdown-menu animate-slide-down"
            style={{
              position: "fixed",
              top: menuPos.top,
              left: menuPos.left,
              minWidth: 100,
              background: "var(--surface-panel)",
              backdropFilter: "none",
              WebkitBackdropFilter: "none",
              border: "1px solid var(--border-default)",
              borderRadius: 14,
              boxShadow: "var(--shadow-lg)",
              padding: "6px",
              zIndex: 10030,
            }}
            role="menu"
          >
            {items.map((item) => (
              <button
                key={item.key}
                role="menuitem"
                disabled={item.disabled}
                onClick={(event) => {
                  event.stopPropagation();
                  if (!item.disabled) {
                    onSelect(item.key);
                    setOpen(false);
                  }
                }}
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  width: "100%",
                  padding: "8px 12px",
                  fontSize: 13,
                  fontWeight: 500,
                  color: item.danger
                    ? "#a23e38"
                    : item.disabled
                      ? "var(--text-faint)"
                      : "var(--text-default)",
                  background: "transparent",
                  border: "none",
                  borderRadius: 10,
                  cursor: item.disabled ? "not-allowed" : "pointer",
                  transition: "background 0.15s",
                  textAlign: "left",
                }}
                onMouseEnter={(e) => {
                  if (!item.disabled)
                    (e.currentTarget as HTMLButtonElement).style.background =
                      item.danger
                        ? "rgba(241, 221, 219, 0.6)"
                        : "var(--surface-muted)";
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.background =
                    "transparent";
                }}
              >
                {item.icon && (
                  <span
                    style={{
                      display: "flex",
                      alignItems: "center",
                      flexShrink: 0,
                    }}
                  >
                    {item.icon}
                  </span>
                )}
                {item.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </div>
  );
}
