/**
 * Toggle — the one on/off switch.
 *
 * Usage:
 *   <Toggle checked={enabled} onChange={() => setEnabled(!enabled)} />
 *   <Toggle checked={on} onChange={toggle} size="sm" />
 */

interface ToggleProps {
  checked: boolean;
  onChange: () => void;
  size?: "sm" | "md";
  disabled?: boolean;
  "aria-label"?: string;
}

export default function Toggle({ checked, onChange, size = "md", disabled, "aria-label": ariaLabel }: ToggleProps) {
  const w = size === "sm" ? 32 : 38;
  const h = size === "sm" ? 18 : 22;
  const dot = size === "sm" ? 14 : 16;
  const pad = size === "sm" ? 2 : 3;

  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={ariaLabel}
      onClick={(e) => { e.stopPropagation(); if (!disabled) onChange(); }}
      disabled={disabled}
      style={{
        width: w,
        minWidth: w,
        height: h,
        padding: 0,
        borderRadius: h / 2,
        border: "none", cursor: disabled ? "not-allowed" : "pointer",
        position: "relative",
        display: "inline-block",
        flexShrink: 0,
        appearance: "none",
        WebkitAppearance: "none",
        transition: "background 0.2s ease",
        background: checked ? "var(--accent-fill)" : "rgba(120, 120, 128, 0.24)",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      <span style={{
        position: "absolute",
        top: pad,
        left: checked ? w - dot - pad : pad,
        width: dot, height: dot, borderRadius: "50%",
        background: "var(--toggle-knob, #fff)",
        transition: "left 0.25s cubic-bezier(0.4,0,0.2,1)",
        boxShadow: "0 1px 3px rgba(0,0,0,0.12)",
      }} />
    </button>
  );
}
