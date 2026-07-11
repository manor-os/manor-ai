/**
 * RadioCard — single radio choice rendered as a clickable card.
 *
 * Hides the native radio button (which never theme-matches) and draws a
 * custom teal dot indicator on the left, then lets the caller drop in
 * whatever layout they need (icon + label + side hint, badge + hint,
 * etc.) via `children`.
 *
 * Usage:
 *   <RadioCard
 *     selected={value === "workspace"}
 *     onSelect={() => setValue("workspace")}
 *     disabled={disabled}
 *     title="Folder rule prevents this"
 *   >
 *     <VisibilityIcon visibility="workspace" />
 *     <span>Workspace</span>
 *     <span style={{ marginLeft: "auto", color: "#a8a29e" }}>hint</span>
 *   </RadioCard>
 */

interface RadioCardProps {
  selected: boolean;
  onSelect: () => void;
  disabled?: boolean;
  /** Native title (browser tooltip). Useful for explaining why the row is disabled. */
  title?: string;
  /** Optional aria-label for screen readers. */
  ariaLabel?: string;
  children: React.ReactNode;
}

export default function RadioCard({
  selected,
  onSelect,
  disabled = false,
  title,
  ariaLabel,
  children,
}: RadioCardProps) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={onSelect}
      title={title}
      style={{
        display: "flex",
        alignItems: "center",
        gap: 10,
        width: "100%",
        padding: "8px 12px",
        borderRadius: 8,
        background: selected ? "rgba(67,107,101,0.06)" : "transparent",
        border: selected
          ? "1px solid rgba(67,107,101,0.3)"
          : "1px solid transparent",
        cursor: disabled ? "not-allowed" : "pointer",
        opacity: disabled ? 0.45 : 1,
        textAlign: "left",
        transition: "background 0.12s, border-color 0.12s",
        font: "inherit",
        color: "inherit",
      }}
      onMouseEnter={(e) => {
        if (disabled || selected) return;
        (e.currentTarget as HTMLElement).style.background = "rgba(245,245,244,0.6)";
      }}
      onMouseLeave={(e) => {
        if (disabled || selected) return;
        (e.currentTarget as HTMLElement).style.background = "transparent";
      }}
    >
      {/* Custom radio indicator */}
      <span
        aria-hidden
        style={{
          width: 16,
          height: 16,
          borderRadius: "50%",
          flexShrink: 0,
          border: selected ? "5px solid #436b65" : "1.5px solid #d6d3d1",
          background: selected ? "#fff" : "transparent",
          boxSizing: "border-box",
          transition: "all 0.12s",
        }}
      />
      {children}
    </button>
  );
}
