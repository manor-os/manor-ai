import { NavLink } from "react-router-dom";

/**
 * URL-bound tab nav. Drop into `PageHeader.tabs` (or anywhere really)
 * when a page has sub-routes you want the user to deep-link to.
 *
 * For an in-page segmented control (no URL change), use TabSwitcher.
 *
 * Visual language matches TabSwitcher: slate-50 track, white active tile
 * with teal text, subtle hover on inactive tiles — just backed by
 * <NavLink> so each tab is a real URL the user can deep-link to.
 */

interface SectionTab {
  path: string;
  label: string;
  count?: number;
  /** Forces exact match — needed for index routes like "/team". */
  end?: boolean;
}

interface SectionTabsProps {
  tabs: SectionTab[];
  size?: "sm" | "md";
}

export default function SectionTabs({ tabs, size = "md" }: SectionTabsProps) {
  const pad = size === "sm" ? "5px 12px" : "6px 14px";
  const fs = size === "sm" ? 12 : 13;
  const r = size === "sm" ? 7 : 8;

  return (
    <nav
      role="tablist"
      style={{
        display: "inline-flex", gap: 2, padding: 3,
        background: "#f5f5f4",
        borderRadius: r + 3,
      }}
    >
      {tabs.map((tab) => (
        <NavLink
          key={tab.path}
          to={tab.path}
          end={tab.end}
          style={({ isActive }) => ({
            display: "inline-flex", alignItems: "center", gap: 6,
            padding: pad, borderRadius: r,
            fontSize: fs, fontWeight: isActive ? 700 : 500,
            textDecoration: "none",
            transition: "background 0.15s ease, color 0.15s ease",
            background: isActive ? "#ffffff" : "transparent",
            color: isActive ? "#1c1917" : "#78716c",
            boxShadow: isActive ? "0 1px 2px rgba(28,25,23,0.06)" : "none",
          })}
        >
          {({ isActive }) => (
            <>
              <span>{tab.label}</span>
              {tab.count !== undefined && (
                <span
                  style={{
                    fontSize: size === "sm" ? 10 : 11, fontWeight: 600,
                    padding: "0 6px", borderRadius: 6, minWidth: 16,
                    lineHeight: 1.5, textAlign: "center",
                    background: isActive ? "#e7e5e4" : "#efedea",
                    color: isActive ? "#44403c" : "#78716c",
                  }}
                >
                  {tab.count}
                </span>
              )}
            </>
          )}
        </NavLink>
      ))}
    </nav>
  );
}
