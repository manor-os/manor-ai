interface InputProps {
  label?: string;
  error?: string;
  value: string;
  onChange: (e: React.ChangeEvent<HTMLInputElement>) => void;
  placeholder?: string;
  type?: string;
  disabled?: boolean;
  className?: string;
  autoFocus?: boolean;
  onFocus?: (e: React.FocusEvent<HTMLInputElement>) => void;
}

export default function Input({
  label,
  error,
  value,
  onChange,
  placeholder,
  type = "text",
  disabled = false,
  className = "",
  autoFocus = false,
  onFocus,
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
        onFocus={onFocus}
        className="manor-input"
        style={error ? { background: "var(--surface-panel)", boxShadow: "0 0 0 3px rgba(214,95,89,0.22)" } : undefined}
      />
      {error && (
        <p className="mt-1 text-xs font-medium text-red-600">{error}</p>
      )}
    </div>
  );
}
