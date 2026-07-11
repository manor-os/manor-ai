import { useNavigate } from "react-router-dom";

interface BreadcrumbItem {
  label: string;
  href?: string;
}

interface BreadcrumbProps {
  items: BreadcrumbItem[];
}

export default function Breadcrumb({ items }: BreadcrumbProps) {
  const navigate = useNavigate();

  return (
    <nav className="flex items-center gap-1.5 text-xs font-medium">
      {items.map((item, i) => {
        const isLast = i === items.length - 1;
        return (
          <span key={i} className="flex items-center gap-1.5">
            {i > 0 && (
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="#a8a29e"
                strokeWidth={2}
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M9 18l6-6-6-6" />
              </svg>
            )}
            {isLast || !item.href ? (
              <span className={isLast ? "text-stone-700 font-semibold" : "text-stone-400"}>
                {item.label}
              </span>
            ) : (
              <button
                onClick={() => navigate(item.href!)}
                className="text-stone-400 hover:text-manor-700 transition-colors"
                style={{ background: "none", border: "none", cursor: "pointer", padding: 0, font: "inherit" }}
              >
                {item.label}
              </button>
            )}
          </span>
        );
      })}
    </nav>
  );
}
