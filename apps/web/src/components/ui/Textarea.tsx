interface TextareaProps {
  label?: string;
  error?: string;
  value: string;
  onChange: (e: React.ChangeEvent<HTMLTextAreaElement>) => void;
  placeholder?: string;
  rows?: number;
  disabled?: boolean;
  className?: string;
  onBlur?: (e: React.FocusEvent<HTMLTextAreaElement>) => void;
}

export default function Textarea({
  label,
  error,
  value,
  onChange,
  placeholder,
  rows = 4,
  disabled = false,
  className = "",
  onBlur,
}: TextareaProps) {
  return (
    <div className={className}>
      {label && (
        <label className="block text-xs font-semibold text-stone-500 uppercase tracking-wide mb-1.5">
          {label}
        </label>
      )}
      <textarea
        value={value}
        onChange={onChange}
        placeholder={placeholder}
        rows={rows}
        disabled={disabled}
        onBlur={onBlur}
        className="manor-textarea"
        style={error ? { background: "var(--surface-panel)", boxShadow: "0 0 0 3px rgba(214,95,89,0.22)" } : undefined}
      />
      {error && (
        <p className="mt-1 text-xs font-medium text-red-600">{error}</p>
      )}
    </div>
  );
}
