import {
  useEffect,
  useRef,
  useState,
  type ComponentType,
  type CSSProperties,
  type ReactNode,
} from "react";
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
  label = "Filters",
  trailing,
  style,
}: FilterBarProps) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        flexWrap: "wrap",
        marginBottom: 14,
        padding: 0,
        ...style,
      }}
    >
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 6,
          height: 36,
          padding: "0 4px",
          color: "#78716c",
          fontSize: 12,
          fontWeight: 750,
          whiteSpace: "nowrap",
        }}
      >
        <IconFilter size={14} />
        {label}
        {activeCount > 0 && (
          <span
            style={{
              minWidth: 18,
              height: 18,
              padding: "0 6px",
              borderRadius: 999,
              background: "#e5eeeb",
              color: "#436b65",
              fontSize: 10,
              lineHeight: "18px",
              textAlign: "center",
            }}
          >
            {activeCount}
          </span>
        )}
      </div>
      <div
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 8,
          flexWrap: "wrap",
          minWidth: 0,
        }}
      >
        {children}
      </div>
      {trailing && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
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
  const ref = useRef<HTMLDivElement>(null);
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

  useEffect(() => {
    if (!open) return;
    const handleClick = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
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
          height: 36,
          boxSizing: "border-box",
          padding: "0 10px",
          overflow: "hidden",
          borderRadius: 12,
          border: open
            ? "1px solid rgba(95,146,138,0.46)"
            : isActive
              ? "1px solid rgba(130,173,164,0.62)"
              : "1px solid rgba(214,211,209,0.9)",
          background: open
            ? "rgba(242,246,245,0.96)"
            : isActive
              ? "rgba(242,246,245,0.72)"
              : "rgba(255,255,255,0.86)",
          boxShadow: open
            ? "0 0 0 3px rgba(95,146,138,0.10), 0 8px 22px rgba(28,25,23,0.06)"
            : "0 5px 18px rgba(28,25,23,0.04)",
          color: "#57534e",
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
            color: isActive ? "#436b65" : "#78716c",
            fontSize: 11,
            fontWeight: 750,
            flexShrink: 0,
            minWidth: 0,
            overflow: "hidden",
            whiteSpace: "nowrap",
          }}
        >
          {Icon && (
            <Icon
              size={12}
              style={{ color: isActive ? "#436b65" : "#7f8ea3", flexShrink: 0 }}
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
            color: isActive ? "#436b65" : "#57534e",
            fontSize: 12,
            fontWeight: isActive ? 760 : 680,
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
              color: open ? "#436b65" : "#a8a29e",
              transition: "transform 0.15s ease",
              transform: open ? "rotate(180deg)" : "none",
            }}
          />
        </span>
      </button>

      {open && (
        <div
          style={{
            position: "absolute",
            top: "calc(100% + 6px)",
            left: 0,
            zIndex: 300,
            width: menuWidth,
            maxWidth: useChipMenu ? 300 : 320,
            maxHeight: useChipMenu ? undefined : 264,
            overflowY: useChipMenu ? "visible" : "auto",
            padding: useChipMenu ? 0 : 5,
            borderRadius: useChipMenu ? 16 : 12,
            border: useChipMenu ? "none" : "1px solid rgba(214,211,209,0.88)",
            background: useChipMenu ? "transparent" : "rgba(255,255,255,0.97)",
            boxShadow: useChipMenu
              ? "none"
              : "0 14px 34px rgba(28,25,23,0.12), 0 1px 0 rgba(255,255,255,0.9) inset",
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
                  border: "1px solid rgba(28,25,23,0.06)",
                  background: "#fafaf9",
                  color: "#44403c",
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
                color: "#a8a29e",
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
                      color: isSelected ? "#436b65" : "#57534e",
                      boxShadow: isSelected
                        ? "0 5px 16px rgba(95,146,138,0.12)"
                        : "0 5px 14px rgba(28,25,23,0.08)",
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
                    color: isSelected ? "#436b65" : "#44403c",
                    cursor: "pointer",
                    fontSize: 13,
                    fontWeight: isSelected ? 760 : 560,
                    textAlign: "left",
                    transition: "background 0.12s ease, color 0.12s ease",
                  }}
                  onMouseEnter={(event) => {
                    event.currentTarget.style.background = isSelected
                      ? "rgba(229,238,235,0.72)"
                      : "#fafaf9";
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
        </div>
      )}
    </div>
  );
}
