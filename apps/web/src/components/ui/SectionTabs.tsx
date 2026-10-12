import type { ReactNode } from "react";
import { NavLink } from "react-router-dom";

/**
 * URL-bound tab nav. Drop into `PageHeader.tabs` (or anywhere really)
 * when a page has sub-routes you want the user to deep-link to.
 *
 * For an in-page segmented control (no URL change), use TabSwitcher.
 *
 * Visual language matches TabSwitcher's neutral track and active tile.
 * Each tab is a real URL with native link keyboard navigation.
 */

interface SectionTab {
  path: string;
  label: string;
  count?: number;
  badge?: ReactNode;
  /** Forces exact match — needed for index routes like "/team". */
  end?: boolean;
}

interface SectionTabsProps {
  tabs: SectionTab[];
  size?: "sm" | "md";
  ariaLabel?: string;
}

export default function SectionTabs({ tabs, size = "md", ariaLabel }: SectionTabsProps) {
  const pad = size === "sm" ? "5px 12px" : "6px 14px";
  const fs = size === "sm" ? 12 : 13;
  const r = size === "sm" ? 7 : 8;

  return (
    <nav
      aria-label={ariaLabel}
      style={{
        display: "inline-flex", flexWrap: "wrap", gap: 2, padding: 3,
        width: "fit-content", maxWidth: "100%",
        background: "var(--surface-muted)",
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
            background: isActive ? "var(--surface-panel)" : "transparent",
            color: isActive ? "var(--text-strong)" : "var(--text-muted)",
            boxShadow: isActive ? "var(--shadow-sm)" : "none",
          })}
        >
          {({ isActive }) => (
            <>
              <span>{tab.label}</span>
              {tab.badge}
              {tab.count !== undefined && (
                <span
                  style={{
                    fontSize: size === "sm" ? 10 : 11, fontWeight: 600,
                    padding: "0 6px", borderRadius: 6, minWidth: 16,
                    lineHeight: 1.5, textAlign: "center",
                    background: "var(--surface-muted)",
                    color: isActive ? "var(--text-strong)" : "var(--text-muted)",
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
