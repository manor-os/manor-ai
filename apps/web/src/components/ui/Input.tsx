interface InputProps {
  label?: string;
  /** Persistent guidance under the field — unlike a placeholder it
   *  survives the user starting to type. */
  hint?: string;
  error?: string;
  value: string;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  placeholder?: string;
  type?: string;
  disabled?: boolean;
  className?: string;
  autoFocus?: boolean;
  autoComplete?: string;
  inputMode?: React.HTMLAttributes<HTMLInputElement>["inputMode"];
  maxLength?: number;
  pattern?: string;
  onFocus?: (e: React.FocusEvent<HTMLInputElement>) => void;
  ariaLabel?: string;
  min?: number | string;
  max?: number | string;
  step?: number | string;
}

export default function Input({
  label,
  hint,
  error,
  value,
  onChange,
  placeholder,
  type = "text",
  disabled = false,
  className = "",
  autoFocus = false,
  autoComplete,
  inputMode,
  maxLength,
  pattern,
  onFocus,
  ariaLabel,
  min,
  max,
  step,
}: InputProps) {
  return (
    <div className={className}>
      {label && (
        <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-1.5">
          {label}
        </label>
      )}
      <input
        type={type}
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        disabled={disabled}
        autoFocus={autoFocus}
        autoComplete={autoComplete}
        inputMode={inputMode}
        maxLength={maxLength}
        pattern={pattern}
        onFocus={onFocus}
        aria-label={ariaLabel}
        min={min}
        max={max}
        step={step}
        className="manor-input"
        style={error ? { background: "var(--surface-panel)", boxShadow: "0 0 0 3px rgba(214,95,89,0.22)" } : undefined}
      />
      {hint && !error && (
        <p className="mt-1.5 text-xs leading-relaxed text-stone-500">{hint}</p>
      )}
      {error && (
        <p className="mt-1 text-xs font-medium text-red-600">{error}</p>
      )}
    </div>
  );
}
