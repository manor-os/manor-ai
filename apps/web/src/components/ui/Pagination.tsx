interface PaginationProps {
  page: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  className?: string;
}

export default function Pagination({ page, totalPages, onPageChange, className = "" }: PaginationProps) {
  if (totalPages <= 1) return null;

  const pages = buildPages(page, totalPages);

  return (
    <div className={`flex items-center gap-1 ${className}`}>
      {/* Prev */}
      <button
        disabled={page <= 1}
        onClick={() => onPageChange(page - 1)}
        style={{
          ...btnBase,
          ...(page <= 1 ? disabledStyle : {}),
        }}
        onMouseEnter={hoverIn}
        onMouseLeave={hoverOut}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
          <path d="M15 18l-6-6 6-6" />
        </svg>
      </button>

      {pages.map((p, i) =>
        p === "..." ? (
          <span key={`ellipsis-${i}`} style={{ width: 32, textAlign: "center", color: "#a8a29e", fontSize: 13 }}>
            ...
          </span>
        ) : (
          <button
            key={p}
            onClick={() => onPageChange(p as number)}
            style={{
              ...btnBase,
              ...(p === page
                ? { background: "#1c1917", color: "#fff", fontWeight: 700 }
                : {}),
            }}
            onMouseEnter={p !== page ? hoverIn : undefined}
            onMouseLeave={p !== page ? hoverOut : undefined}
          >
            {p}
          </button>
        )
      )}

      {/* Next */}
      <button
        disabled={page >= totalPages}
        onClick={() => onPageChange(page + 1)}
        style={{
          ...btnBase,
          ...(page >= totalPages ? disabledStyle : {}),
        }}
        onMouseEnter={hoverIn}
        onMouseLeave={hoverOut}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round">
          <path d="M9 18l6-6-6-6" />
        </svg>
      </button>
    </div>
  );
}

/* --- Helpers --- */

const btnBase: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  width: 32,
  height: 32,
  borderRadius: 10,
  border: "none",
  background: "transparent",
  color: "#57534e",
  fontSize: 13,
  fontWeight: 600,
  cursor: "pointer",
  transition: "all 0.15s",
  fontFamily: "inherit",
};

const disabledStyle: React.CSSProperties = {
  opacity: 0.35,
  cursor: "not-allowed",
  pointerEvents: "none",
};

function hoverIn(e: React.MouseEvent<HTMLButtonElement>) {
  e.currentTarget.style.background = "#f5f5f4";
}
function hoverOut(e: React.MouseEvent<HTMLButtonElement>) {
  e.currentTarget.style.background = "transparent";
}

function buildPages(current: number, total: number): (number | "...")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);

  const pages: (number | "...")[] = [1];

  if (current > 3) pages.push("...");

  const start = Math.max(2, current - 1);
  const end = Math.min(total - 1, current + 1);
  for (let i = start; i <= end; i++) pages.push(i);

  if (current < total - 2) pages.push("...");

  pages.push(total);
  return pages;
}
