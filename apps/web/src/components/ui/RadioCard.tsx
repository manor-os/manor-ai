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
 *     <span style={{ marginLeft: "auto", color: "var(--text-muted)" }}>hint</span>
 *   </RadioCard>
 */

interface RadioCardProps {
  variant?: "default" | "tile";
  selected: boolean;
  onSelect: () => void;
  disabled?: boolean;
  /** Native title (browser tooltip). Useful for explaining why the row is disabled. */
  title?: string;
  /** Optional aria-label for screen readers. */
  ariaLabel?: string;
  ariaDescribedBy?: string;
  tabIndex?: number;
  className?: string;
  /** Decorative preview above the choice label; keep it non-interactive. */
  preview?: React.ReactNode;
  children: React.ReactNode;
}

export default function RadioCard({
  variant = "default",
  selected,
  onSelect,
  disabled = false,
  title,
  ariaLabel,
  ariaDescribedBy,
  tabIndex,
  className,
  preview,
  children,
}: RadioCardProps) {
  if (variant === "tile") return <button type="button" role="radio" aria-checked={selected} aria-label={ariaLabel} aria-describedby={ariaDescribedBy} tabIndex={tabIndex} disabled={disabled} onClick={onSelect} title={title} className={`radio-card-tile ${className || ""}`}>{children}</button>;
  if (preview !== undefined && preview !== null) {
    return (
      <button
        type="button"
        role="radio"
        aria-checked={selected}
        aria-label={ariaLabel}
        aria-describedby={ariaDescribedBy}
        tabIndex={tabIndex}
        className={className}
        disabled={disabled}
        onClick={onSelect}
        title={title}
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "stretch",
          gap: 12,
          width: "100%",
          height: "100%",
          padding: 12,
          border: "none",
          borderRadius: "var(--radius-card)",
          background: selected ? "var(--accent-soft)" : "var(--surface-panel)",
          boxShadow: selected
            ? "var(--shadow-sm), 0 0 0 3px var(--accent-ring)"
            : "var(--shadow-sm)",
          cursor: disabled ? "not-allowed" : "pointer",
          opacity: disabled ? 0.45 : 1,
          textAlign: "left",
          transition: "background 0.12s, box-shadow 0.12s",
          font: "inherit",
          color: "var(--text-default)",
        }}
        onMouseEnter={(e) => {
          if (disabled || selected) return;
          e.currentTarget.style.background = "var(--surface-muted)";
        }}
        onMouseLeave={(e) => {
          if (disabled || selected) return;
          e.currentTarget.style.background = "var(--surface-panel)";
        }}
      >
        <span aria-hidden="true" style={{ display: "block", flex: "1 1 auto", minWidth: 0 }}>
          {preview}
        </span>
        <span style={{ display: "flex", alignItems: "center", flexShrink: 0, gap: 10 }}>
          <span
            aria-hidden="true"
            style={{
              width: 16,
              height: 16,
              borderRadius: "50%",
              flexShrink: 0,
              border: selected
                ? "5px solid var(--accent)"
                : "1.5px solid var(--border-default)",
              background: selected ? "var(--surface-panel)" : "transparent",
              boxSizing: "border-box",
              transition: "all 0.12s",
            }}
          />
          {children}
        </span>
      </button>
    );
  }

  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      aria-label={ariaLabel}
      aria-describedby={ariaDescribedBy}
      tabIndex={tabIndex}
      className={className}
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
          border: selected ? "5px solid #436b65" : "1.5px solid #d1d1d6",
          background: selected ? "#fff" : "transparent",
          boxSizing: "border-box",
          transition: "all 0.12s",
        }}
      />
      {children}
    </button>
  );
}
