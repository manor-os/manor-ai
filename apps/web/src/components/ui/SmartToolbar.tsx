import { useState, useRef, useEffect } from "react";
import { IconSearch, IconSort, IconFilter } from "../icons";

interface SortOption {
  key: string;
  label: string;
}

interface FilterOption {
  key: string;
  label: string;
}

interface SmartToolbarProps {
  searchValue: string;
  onSearchChange: (value: string) => void;
  searchPlaceholder?: string;
  sortOptions?: SortOption[];
  sortValue?: string;
  onSortChange?: (key: string) => void;
  filterOptions?: FilterOption[];
  filterValue?: string;
  onFilterChange?: (key: string) => void;
  className?: string;
}

export default function SmartToolbar({
  searchValue,
  onSearchChange,
  searchPlaceholder = "Search...",
  sortOptions,
  sortValue,
  onSortChange,
  filterOptions,
  filterValue,
  onFilterChange,
  className = "",
}: SmartToolbarProps) {
  const [sortOpen, setSortOpen] = useState(false);
  const [filterOpen, setFilterOpen] = useState(false);
  const sortRef = useRef<HTMLDivElement>(null);
  const filterRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (sortRef.current && !sortRef.current.contains(e.target as Node)) setSortOpen(false);
      if (filterRef.current && !filterRef.current.contains(e.target as Node)) setFilterOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  return (
    <div
      className={`smart-toolbar flex h-9 min-w-0 items-center overflow-hidden rounded-[11px] backdrop-blur-[12px] ${className}`}
      style={{
        background: "color-mix(in srgb, var(--surface-panel) 76%, transparent)",
        border: "1px solid var(--border-default)",
      }}
    >
      {/* Search section */}
      <div className="flex min-w-0 items-center flex-1 px-3">
        <IconSearch size={15} className="shrink-0" style={{ color: "var(--text-faint)" }} />
        <input
          type="text"
          value={searchValue}
          onChange={(e) => onSearchChange(e.target.value)}
          placeholder={searchPlaceholder}
          className="h-[34px] min-w-0 flex-1 border-none bg-transparent px-2 text-[13px] font-medium outline-none"
          style={{ color: "var(--text-strong)" }}
        />
      </div>

      {/* Sort section */}
      {sortOptions && onSortChange && (
        <>
          <div className="w-px h-5" style={{ background: "var(--border-subtle)" }} />
          <div ref={sortRef} className="relative">
            <button
              onClick={() => { setSortOpen(!sortOpen); setFilterOpen(false); }}
              className="smart-toolbar-trigger flex h-[34px] cursor-pointer items-center gap-1 border-none bg-transparent px-3 text-xs font-semibold"
            >
              <IconSort size={14} className="w-3.5 h-3.5" />
              Sort
            </button>
            {sortOpen && (
              <div className="smart-toolbar-menu absolute top-[calc(100%+4px)] right-0 backdrop-blur-[12px] rounded-[10px] p-1 z-10 min-w-[140px] animate-[slide-down_0.15s_ease-out]">
                {sortOptions.map((opt) => (
                  <button
                    key={opt.key}
                    onClick={() => { onSortChange(opt.key); setSortOpen(false); }}
                    className={[
                      "smart-toolbar-option block w-full py-1.5 px-3 rounded-lg text-xs font-medium border-none cursor-pointer text-left transition-colors duration-150",
                      sortValue === opt.key ? "is-active" : "",
                    ].join(" ")}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </>
      )}

      {/* Filter section */}
      {filterOptions && onFilterChange && (
        <>
          <div className="w-px h-5" style={{ background: "var(--border-subtle)" }} />
          <div ref={filterRef} className="relative">
            <button
              onClick={() => { setFilterOpen(!filterOpen); setSortOpen(false); }}
              className="smart-toolbar-trigger flex h-[34px] cursor-pointer items-center gap-1 border-none bg-transparent px-3 text-xs font-semibold"
            >
              <IconFilter size={14} className="w-3.5 h-3.5" />
              Filter
            </button>
            {filterOpen && (
              <div className="smart-toolbar-menu absolute top-[calc(100%+4px)] right-0 backdrop-blur-[12px] rounded-[10px] p-1 z-10 min-w-[140px] animate-[slide-down_0.15s_ease-out]">
                {filterOptions.map((opt) => (
                  <button
                    key={opt.key}
                    onClick={() => { onFilterChange(opt.key); setFilterOpen(false); }}
                    className={[
                      "smart-toolbar-option block w-full py-1.5 px-3 rounded-lg text-xs font-medium border-none cursor-pointer text-left transition-colors duration-150",
                      filterValue === opt.key ? "is-active" : "",
                    ].join(" ")}
                  >
                    {opt.label}
                  </button>
                ))}
              </div>
            )}
          </div>
        </>
      )}
    </div>
  );
}
