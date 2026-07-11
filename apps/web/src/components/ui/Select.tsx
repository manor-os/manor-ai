import { useState, useRef, useEffect, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { t } from "../../lib/i18n";

interface SelectOption {
  value: string;
  label: string;
  icon?: ReactNode;
}

interface SelectProps {
  value: string;
  onChange: (value: string) => void;
  options: SelectOption[] | string[];
  placeholder?: string;
  filterable?: boolean;
  style?: React.CSSProperties;
  buttonStyle?: React.CSSProperties;
  openButtonStyle?: React.CSSProperties;
  dropdownStyle?: React.CSSProperties;
  optionStyle?: React.CSSProperties;
  dropdownMinWidth?: number;
  showSelectedIcon?: boolean;
  selectedOptionColor?: string;
  selectedOptionCheckColor?: string;
  disabled?: boolean;
}

function nonPositionalDropdownStyle(style?: React.CSSProperties): React.CSSProperties {
  if (!style) return {};
  const {
    position: _position,
    top: _top,
    right: _right,
    bottom: _bottom,
    left: _left,
    zIndex: _zIndex,
    ...rest
  } = style;
  return rest;
}

export default function Select({
  value,
  onChange,
  options,
  placeholder,
  filterable,
  style,
  buttonStyle,
  openButtonStyle,
  dropdownStyle,
  optionStyle,
  dropdownMinWidth = 140,
  showSelectedIcon = false,
  selectedOptionColor = "#436b65",
  selectedOptionCheckColor = "#436b65",
  disabled = false,
}: SelectProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  // The menu is portaled to <body> with fixed positioning so it's never clipped
  // by a parent's overflow (e.g. inside a modal). Position tracks the trigger.
  const [coords, setCoords] = useState<{ top: number; left: number; width: number } | null>(null);

  const normalizedOptions: SelectOption[] = options.map((o) =>
    typeof o === "string" ? { value: o, label: o } : o
  );

  const selectedLabel = normalizedOptions.find((o) => o.value === value)?.label || value || placeholder || "";
  const selectedIcon = normalizedOptions.find((o) => o.value === value)?.icon;

  const filtered = query
    ? normalizedOptions.filter((o) => o.label.toLowerCase().includes(query.toLowerCase()))
    : normalizedOptions;
  const safeDropdownStyle = nonPositionalDropdownStyle(dropdownStyle);

  // Position the portaled menu under the trigger; keep it tracking on scroll/resize.
  useEffect(() => {
    if (!open) return;
    const place = () => {
      const r = triggerRef.current?.getBoundingClientRect();
      if (r) {
        const menuWidth = Math.max(r.width, dropdownMinWidth);
        const viewportPadding = 12;
        const maxLeft = Math.max(viewportPadding, window.innerWidth - menuWidth - viewportPadding);
        setCoords({
          top: r.bottom + 4,
          left: Math.min(Math.max(viewportPadding, r.left), maxLeft),
          width: r.width,
        });
      }
    };
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open, dropdownMinWidth]);

  // Close on outside click — the menu lives in a portal, so check both it and the trigger.
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      const target = e.target as Node;
      if (ref.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  // Focus input on open
  useEffect(() => {
    if (open && filterable && inputRef.current) inputRef.current.focus();
  }, [open, filterable]);

  return (
    <div ref={ref} style={{ position: "relative", ...style }}>
      {/* Trigger */}
      <button
        ref={triggerRef}
        type="button"
        disabled={disabled}
        onClick={() => {
          if (disabled) return;
          setOpen(!open);
          setQuery("");
        }}
        className={`manor-input manor-select-trigger${disabled ? " is-disabled" : ""}${open ? " is-open" : ""}`}
        style={{
          width: "100%", textAlign: "left", cursor: disabled ? "not-allowed" : "pointer",
          display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8,
          color: value ? "var(--text-default)" : "var(--text-faint)",
          opacity: disabled ? 0.52 : 1,
          ...buttonStyle,
          ...(open ? { borderColor: "var(--accent)", boxShadow: "0 0 0 3px var(--accent-ring)", background: "var(--surface-panel)" } : {}),
          ...(open ? openButtonStyle : {}),
        }}
      >
        {showSelectedIcon && selectedIcon && (
          <span style={{ display: "inline-flex", alignItems: "center", flexShrink: 0 }}>
            {selectedIcon}
          </span>
        )}
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {selectedLabel}
        </span>
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="var(--text-faint)" strokeWidth={2.5} style={{ flexShrink: 0, transition: "transform 0.2s", transform: open ? "rotate(180deg)" : "none" }}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M19.5 8.25l-7.5 7.5-7.5-7.5" />
        </svg>
      </button>

      {/* Dropdown — portaled to <body> so a parent's overflow never clips it. */}
      {open && coords && createPortal(
        <div ref={menuRef} className="manor-select-menu" style={{
          position: "fixed", top: coords.top, left: coords.left, width: coords.width, zIndex: 100000,
          background: "var(--surface-panel)", backdropFilter: "blur(28px) saturate(140%)",
          WebkitBackdropFilter: "blur(28px) saturate(140%)",
          border: "1px solid var(--border-default)", borderRadius: 14,
          boxShadow: "var(--shadow-lg)",
          minWidth: dropdownMinWidth,
          maxHeight: 240, overflowY: "auto", padding: 4,
          animation: "dialog-in 0.15s ease-out",
          ...safeDropdownStyle,
        }}>
          {/* Search */}
          {filterable && (
            <div style={{ padding: "4px 4px 6px" }}>
              <input
                ref={inputRef}
                className="manor-select-search"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder={t("component.select.search")}
                style={{
                  width: "100%", height: 32, padding: "0 10px", fontSize: 13, fontWeight: 500,
                  border: "none", borderRadius: 8, outline: "none", color: "var(--text-default)",
                  background: "var(--surface-muted)", boxSizing: "border-box",
                }}
              />
            </div>
          )}

          {/* Options */}
          {filtered.length === 0 && (
            <div style={{ padding: "12px 14px", fontSize: 13, color: "var(--text-faint)", textAlign: "center" }}>{t("component.select.no_results")}</div>
          )}
          {filtered.map((o) => {
            const isSelected = o.value === value;
            return (
              <button
                key={o.value}
                type="button"
                className={`manor-select-option${isSelected ? " is-selected" : ""}`}
                onClick={() => { onChange(o.value); setOpen(false); setQuery(""); }}
                style={{
                  display: "flex", alignItems: "center", justifyContent: "space-between",
                  width: "100%", padding: "0 14px", height: 36, fontSize: 13, fontWeight: isSelected ? 600 : 400,
                  color: isSelected ? selectedOptionColor : "var(--text-default)", background: "transparent",
                  border: "none", borderRadius: 8, cursor: "pointer", textAlign: "left",
                  transition: "background 0.1s",
                  ...optionStyle,
                }}
                onMouseEnter={(e) => { (e.currentTarget).style.background = "var(--surface-muted)"; }}
                onMouseLeave={(e) => { (e.currentTarget).style.background = "transparent"; }}
              >
                <span style={{ display: "inline-flex", alignItems: "center", gap: 8, minWidth: 0 }}>
                  {o.icon && (
                    <span style={{ display: "inline-flex", alignItems: "center", flexShrink: 0 }}>
                      {o.icon}
                    </span>
                  )}
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{o.label}</span>
                </span>
                {isSelected && (
                  <svg width="14" height="14" viewBox="0 0 24 24" fill={selectedOptionCheckColor} style={{ flexShrink: 0 }}>
                    <path d="M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z" />
                  </svg>
                )}
              </button>
            );
          })}
        </div>,
        document.body,
      )}
    </div>
  );
}
