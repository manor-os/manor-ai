/**
 * Horizontally scrolling segmented filter — one selected at a time.
 *
 * Used for kind/status/category filters above lists.
 */

interface FilterPillOption {
  key: string;
  label: string;
  count?: number;
}

interface FilterPillsProps {
  options: FilterPillOption[];
  value: string;
  onChange: (key: string) => void;
  className?: string;
}

export default function FilterPills({ options, value, onChange, className }: FilterPillsProps) {
  return (
    <div
      className={className}
      style={{ display: "flex", gap: 6, flexWrap: "wrap" }}
    >
      {options.map((o) => {
        const active = value === o.key;
        return (
          <button
            key={o.key}
            onClick={() => onChange(o.key)}
            style={{
              padding: "4px 12px", borderRadius: 999, fontSize: 12, fontWeight: 600,
              border: "1px solid",
              borderColor: active ? "#1c1917" : "rgba(231,229,228,0.6)",
              background: active ? "#1c1917" : "rgba(255,255,255,0.75)",
              color: active ? "#fff" : "#57534e",
              cursor: "pointer", transition: "all 0.15s",
              display: "flex", alignItems: "center", gap: 6,
              minHeight: 32,
            }}
          >
            <span>{o.label}</span>
            {o.count != null && (
              <span style={{
                fontSize: 10, fontWeight: 700,
                background: active ? "rgba(255,255,255,0.25)" : "#f5f5f4",
                color: active ? "#fff" : "#78716c",
                padding: "1px 6px", borderRadius: 10,
              }}>{o.count}</span>
            )}
          </button>
        );
      })}
    </div>
  );
}
