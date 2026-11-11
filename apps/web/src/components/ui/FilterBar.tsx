import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ComponentType,
  type CSSProperties,
  type ReactNode,
} from "react";
import { createPortal } from "react-dom";
import { t } from "../../lib/i18n";
import { IconCheck, IconChevronDown, IconFilter } from "../icons";

type FilterIcon = ComponentType<{
  size?: number;
  className?: string;
  style?: CSSProperties;
}>;

export interface FilterSelectOption<T extends string = string> {
  key: T;
  label: string;
  icon?: ReactNode;
}

interface FilterBarProps {
  children: ReactNode;
  activeCount?: number;
  label?: string;
  trailing?: ReactNode;
  style?: CSSProperties;
}

interface FilterSelectProps<T extends string> {
  label: string;
  Icon?: FilterIcon;
  options: FilterSelectOption<T>[];
  value: T;
  onChange: (value: T) => void;
  width?: number;
  valueMinWidth?: number;
  dropdownMinWidth?: number;
  showSelectedIcon?: boolean;
  filterable?: boolean;
}

export function FilterBar({
  children,
  activeCount = 0,
  label,
  trailing,
  style,
}: FilterBarProps) {
  // Filters stay folded behind one button; any active filter keeps them open
  // so what narrows the list is always visible.
  const [expanded, setExpanded] = useState(false);
  const open = expanded || activeCount > 0;
  return (
    <div
      className="filter-bar"
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        flexWrap: "wrap",
        marginBottom: 12,
        padding: 0,
        ...style,
      }}
    >
      <button
        type="button"
        className={`filter-bar-toggle ${open ? "is-open" : ""}`}
        aria-expanded={open}
        onClick={() => setExpanded((value) => !value)}
      >
        <IconFilter size={13} />
        {label ?? t("component.filter_bar.filters")}
        {activeCount > 0 && <span className="filter-bar-count">{activeCount}</span>}
      </button>
      {open && (
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            flexWrap: "wrap",
            minWidth: 0,
          }}
        >
          {children}
        </div>
      )}
      {trailing && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            marginLeft: "auto",
          }}
        >
          {trailing}
        </div>
      )}
    </div>
  );
}

export function FilterSelect<T extends string>({
  label,
  Icon,
  options,
  value,
  onChange,
  width = 144,
  valueMinWidth = 56,
  dropdownMinWidth = 180,
  showSelectedIcon = false,
  filterable = false,
}: FilterSelectProps<T>) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [menuPosition, setMenuPosition] = useState<{
    left: number;
    top: number;
    width: number;
    maxHeight: number;
  } | null>(null);
  const ref = useRef<HTMLDivElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const isActive = value !== "all";
  const selectedOption = options.find((option) => option.key === value);
  const selectedLabel = selectedOption?.label || value;
  const filteredOptions = query.trim()
    ? options.filter((option) =>
        option.label.toLowerCase().includes(query.trim().toLowerCase()),
      )
    : options;
  const useChipMenu = !filterable && !showSelectedIcon && options.length <= 8;
  const menuWidth = useChipMenu
    ? Math.max(width, Math.min(300, Math.max(dropdownMinWidth, 248)))
    : Math.max(width, dropdownMinWidth);

  const updateMenuPosition = useCallback(() => {
    const anchor = ref.current?.getBoundingClientRect();
    if (!anchor) return;
    const gutter = 12;
    const gap = 6;
    const width = Math.min(menuWidth, window.innerWidth - gutter * 2);
    const left = Math.max(gutter, Math.min(anchor.left, window.innerWidth - gutter - width));
    const below = Math.max(0, window.innerHeight - anchor.bottom - gap - gutter);
    const above = Math.max(0, anchor.top - gap - gutter);
    const contentHeight = menuRef.current?.scrollHeight ?? 0;
    const openAbove = contentHeight > below && above > below;
    const maxHeight = openAbove ? above : below;
    const top = openAbove
      ? anchor.top - gap - Math.min(contentHeight, maxHeight)
      : anchor.bottom + gap;
    setMenuPosition((current) =>
      current?.left === left && current.top === top
      && current.width === width && current.maxHeight === maxHeight
        ? current
        : { left, top, width, maxHeight },
    );
  }, [menuWidth]);

  useLayoutEffect(() => {
    if (!open) return;
    updateMenuPosition();
    window.addEventListener("resize", updateMenuPosition);
    window.addEventListener("scroll", updateMenuPosition, true);
    return () => {
      window.removeEventListener("resize", updateMenuPosition);
      window.removeEventListener("scroll", updateMenuPosition, true);
    };
  }, [open, updateMenuPosition]);

  useLayoutEffect(() => {
    if (open && menuPosition && menuRef.current) updateMenuPosition();
  }, [open, menuPosition, filteredOptions.length, updateMenuPosition]);

  useEffect(() => {
    if (!open) return;
    const handleClick = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)
        && !menuRef.current?.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    const handleEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", handleClick);
    document.addEventListener("keydown", handleEscape);
    return () => {
      document.removeEventListener("mousedown", handleClick);
      document.removeEventListener("keydown", handleEscape);
    };
  }, [open]);

  useEffect(() => {
    if (open && filterable) {
      inputRef.current?.focus();
    }
  }, [filterable, open]);

  return (
    <div
      ref={ref}
      style={{
        position: "relative",
        width,
        minWidth: width,
        flex: "0 0 auto",
      }}
    >
      <button
        type="button"
        aria-expanded={open}
        onClick={() => {
          setOpen((current) => !current);
          setQuery("");
        }}
        style={{
          display: "grid",
          gridTemplateColumns: "auto minmax(0, 1fr) 16px",
          alignItems: "center",
          columnGap: 7,
          width: "100%",
          height: 30,
          boxSizing: "border-box",
          padding: "0 10px",
          overflow: "hidden",
          borderRadius: 999,
          border: 0,
          background: open || isActive
            ? "rgba(118,118,128,0.16)"
            : "rgba(118,118,128,0.08)",
          boxShadow: "none",
          color: "var(--text-muted)",
          cursor: "pointer",
          textAlign: "left",
          transition:
            "border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease",
        }}
      >
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            color: "var(--text-muted)",
            fontSize: 12,
            fontWeight: 400,
            flexShrink: 0,
            minWidth: 0,
            overflow: "hidden",
            whiteSpace: "nowrap",
          }}
        >
          {Icon && (
            <Icon
              size={12}
              style={{ color: "var(--text-muted)", flexShrink: 0 }}
            />
          )}
          {label}
        </span>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            minWidth: 0,
            overflow: "hidden",
            color: "var(--text-strong)",
            fontSize: 12,
            fontWeight: isActive ? 600 : 400,
          }}
        >
          {showSelectedIcon && isActive && selectedOption?.icon && (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                flexShrink: 0,
              }}
            >
              {selectedOption.icon}
            </span>
          )}
          <span
            style={{
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {selectedLabel}
          </span>
        </span>
        <span
          style={{
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            width: 16,
            height: 16,
            justifySelf: "end",
          }}
        >
          <IconChevronDown
            size={12}
            style={{
              color: open ? "#436b65" : "#aeaeb2",
              transition: "transform 0.15s ease",
              transform: open ? "rotate(180deg)" : "none",
            }}
          />
        </span>
      </button>

      {open && menuPosition && createPortal(
        <div
          ref={menuRef}
          style={{
            position: "fixed",
            top: menuPosition.top,
            left: menuPosition.left,
            zIndex: 3000,
            width: menuPosition.width,
            maxHeight: Math.min(menuPosition.maxHeight, 264),
            overflowY: "auto",
            padding: useChipMenu ? 0 : 5,
            borderRadius: useChipMenu ? 16 : 12,
            border: useChipMenu ? "none" : "1px solid rgba(214,211,209,0.88)",
            background: useChipMenu ? "transparent" : "rgba(255,255,255,0.97)",
            boxShadow: useChipMenu
              ? "none"
              : "0 14px 34px rgba(29, 29, 31,0.12), 0 1px 0 rgba(255,255,255,0.9) inset",
            backdropFilter: useChipMenu ? "none" : "blur(18px)",
            animation: "dialog-in 0.12s ease-out",
          }}
        >
          {filterable && (
            <div style={{ padding: "3px 3px 7px" }}>
              <input
                ref={inputRef}
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder={t("component.select.search")}
                style={{
                  width: "100%",
                  height: 30,
                  padding: "0 9px",
                  borderRadius: 8,
                  border: "0.5px solid rgba(29, 29, 31,0.06)",
                  background: "var(--surface-muted)",
                  color: "var(--text-muted)",
                  fontSize: 12,
                  fontWeight: 600,
                  outline: "none",
                  boxSizing: "border-box",
                }}
              />
            </div>
          )}

          {filteredOptions.length === 0 ? (
            <div
              style={{
                padding: "12px 10px",
                color: "var(--text-faint)",
                fontSize: 12,
                fontWeight: 650,
                textAlign: "center",
              }}
            >
              {t("component.select.no_results")}
            </div>
          ) : useChipMenu ? (
            <div
              style={{
                display: "flex",
                alignItems: "center",
                gap: 6,
                flexWrap: "wrap",
              }}
            >
              {filteredOptions.map((option) => {
                const isSelected = option.key === value;
                return (
                  <button
                    key={option.key}
                    type="button"
                    onClick={() => {
                      onChange(option.key);
                      setOpen(false);
                      setQuery("");
                    }}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 6,
                      height: 29,
                      maxWidth: "100%",
                      padding: "0 9px",
                      borderRadius: 999,
                      border: isSelected
                        ? "1px solid rgba(95,146,138,0.40)"
                        : "1px solid rgba(214,211,209,0.88)",
                      background: isSelected
                        ? "rgba(229,238,235,0.92)"
                        : "rgba(255,255,255,0.96)",
                      color: isSelected ? "#436b65" : "#6e6e73",
                      boxShadow: isSelected
                        ? "0 5px 16px rgba(95,146,138,0.12)"
                        : "0 5px 14px rgba(29, 29, 31,0.08)",
                      cursor: "pointer",
                      fontSize: 12,
                      fontWeight: isSelected ? 760 : 620,
                      whiteSpace: "nowrap",
                      transition:
                        "background 0.12s ease, border-color 0.12s ease, color 0.12s ease",
                    }}
                    onMouseEnter={(event) => {
                      if (!isSelected) {
                        event.currentTarget.style.background = "#fff";
                        event.currentTarget.style.borderColor =
                          "rgba(168,162,158,0.78)";
                      }
                    }}
                    onMouseLeave={(event) => {
                      if (!isSelected) {
                        event.currentTarget.style.background =
                          "rgba(255,255,255,0.96)";
                        event.currentTarget.style.borderColor =
                          "rgba(214,211,209,0.88)";
                      }
                    }}
                  >
                    {option.icon && (
                      <span
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          flexShrink: 0,
                        }}
                      >
                        {option.icon}
                      </span>
                    )}
                    <span
                      style={{ overflow: "hidden", textOverflow: "ellipsis" }}
                    >
                      {option.label}
                    </span>
                    {isSelected && (
                      <IconCheck
                        size={12}
                        style={{ color: "#436b65", flexShrink: 0 }}
                      />
                    )}
                  </button>
                );
              })}
            </div>
          ) : (
            filteredOptions.map((option) => {
              const isSelected = option.key === value;
              return (
                <button
                  key={option.key}
                  type="button"
                  onClick={() => {
                    onChange(option.key);
                    setOpen(false);
                    setQuery("");
                  }}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    justifyContent: "space-between",
                    gap: 10,
                    width: "100%",
                    height: 34,
                    padding: "0 9px",
                    borderRadius: 9,
                    border: "none",
                    background: isSelected
                      ? "rgba(242,246,245,0.92)"
                      : "transparent",
                    color: isSelected ? "#436b65" : "#48484a",
                    cursor: "pointer",
                    fontSize: 13,
                    fontWeight: isSelected ? 760 : 560,
                    textAlign: "left",
                    transition: "background 0.12s ease, color 0.12s ease",
                  }}
                  onMouseEnter={(event) => {
                    event.currentTarget.style.background = isSelected
                      ? "rgba(229,238,235,0.72)"
                      : "#fafafa";
                  }}
                  onMouseLeave={(event) => {
                    event.currentTarget.style.background = isSelected
                      ? "rgba(242,246,245,0.92)"
                      : "transparent";
                  }}
                >
                  <span
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: 8,
                      minWidth: 0,
                    }}
                  >
                    {option.icon && (
                      <span
                        style={{
                          display: "inline-flex",
                          alignItems: "center",
                          flexShrink: 0,
                        }}
                      >
                        {option.icon}
                      </span>
                    )}
                    <span
                      style={{
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                        whiteSpace: "nowrap",
                      }}
                    >
                      {option.label}
                    </span>
                  </span>
                  {isSelected && (
                    <IconCheck
                      size={14}
                      style={{ color: "#436b65", flexShrink: 0 }}
                    />
                  )}
                </button>
              );
            })
          )}
        </div>,
        document.body,
      )}
    </div>
  );
}
