import { IconSearch } from "../icons";

interface SearchInputProps {
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  className?: string;
}

export default function SearchInput({ value, onChange, placeholder = "Search...", className = "" }: SearchInputProps) {
  return (
    <div className={`relative ${className}`}>
      <IconSearch size={16} className="absolute left-3 top-1/2 -translate-y-1/2 text-stone-400 pointer-events-none" />
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className="manor-input w-full pl-9 h-9 bg-stone-50/60"
      />
    </div>
  );
}
