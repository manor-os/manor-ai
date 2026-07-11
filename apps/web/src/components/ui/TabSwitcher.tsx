/**
 * TabSwitcher — light segmented-control for switching views.
 *
 * Visual language: slate-50 track, white active tile with teal text,
 * subtle hover on inactive tiles. Calm + neutral so it sits well next
 * to the new Card / IconTile primitives.
 *
 * Usage:
 *   <TabSwitcher tabs={[{ key: "board", label: "Board" }]} value={view} onChange={setView} />
 *   <TabSwitcher tabs={[{ key: "a", label: "Alpha", icon: <Icon />, count: 3 }]} value={tab} onChange={setTab} />
 */

import { useEffect, useRef, type ReactNode } from "react";

interface Tab {
  key: string;
  label: string;
  icon?: ReactNode;
  count?: number;
  badge?: string;
}

interface TabSwitcherProps {
  tabs: Tab[];
  value: string;
  onChange: (key: string) => void;
  size?: "sm" | "md";
  className?: string;
  wrap?: boolean;
}

export default function TabSwitcher({ tabs, value, onChange, size = "md", className = "", wrap = false }: TabSwitcherProps) {
  const tabRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const pad = size === "sm" ? "4px 12px" : "5px 14px";
  const fs = size === "sm" ? 12 : 13;
  const r = size === "sm" ? 7 : 8;
  const controlHeight = size === "sm" ? 32 : 36;
  const buttonHeight = size === "sm" ? 28 : 32;

  useEffect(() => {
    tabRefs.current[value]?.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
      inline: "center",
    });
  }, [value]);

  return (
    <div
      className={`manor-tab-switcher ${className}`}
      style={{
        display: wrap ? "flex" : "inline-flex",
        flexWrap: wrap ? "wrap" : "nowrap",
        gap: 2,
        padding: 2,
        height: wrap ? "auto" : controlHeight,
        minHeight: controlHeight,
        width: "fit-content",
        maxWidth: "100%",
        alignSelf: "flex-start",
        boxSizing: "border-box",
        alignContent: "flex-start",
        overflowX: wrap ? "visible" : "auto",
        overflowY: "hidden",
        WebkitOverflowScrolling: "touch",
        scrollbarWidth: "none",
        background: "var(--surface-muted)",
        border: "1px solid var(--border-subtle)",
        borderRadius: r + 3,
      }}
    >
      {tabs.map((tab) => {
        const active = value === tab.key;
        return (
          <button
            key={tab.key}
            ref={(node) => {
              tabRefs.current[tab.key] = node;
            }}
            onClick={() => onChange(tab.key)}
            style={{
              display: "inline-flex", alignItems: "center", gap: 6,
              flex: "0 0 auto",
              padding: pad, borderRadius: r,
              minHeight: buttonHeight,
              height: buttonHeight,
              fontSize: fs, fontWeight: active ? 700 : 500,
              whiteSpace: "nowrap",
              border: "none", cursor: "pointer",
              transition: "background 0.15s ease, color 0.15s ease",
              background: active ? "var(--surface-panel)" : "transparent",
              color: active ? "var(--text-strong)" : "var(--text-muted)",
              boxShadow: active ? "var(--shadow-sm)" : "none",
            }}
            onMouseEnter={!active ? (e) => {
              e.currentTarget.style.color = "var(--text-strong)";
              e.currentTarget.style.background = "var(--glass-card-hover)";
            } : undefined}
            onMouseLeave={!active ? (e) => {
              e.currentTarget.style.color = "var(--text-muted)";
              e.currentTarget.style.background = "transparent";
            } : undefined}
          >
            {tab.icon && <span style={{ display: "flex", alignItems: "center" }}>{tab.icon}</span>}
            {tab.label}
            {tab.badge && (
              <span style={{
                fontSize: 9, fontWeight: 700, letterSpacing: "0.03em",
                padding: "1px 5px", borderRadius: 4, lineHeight: 1.4,
                background: "linear-gradient(135deg, #9079c2, #6d6fb2)",
                color: "#fff", textTransform: "uppercase",
              }}>
                {tab.badge}
              </span>
            )}
            {tab.count !== undefined && (
              <span style={{
                fontSize: size === "sm" ? 10 : 11, fontWeight: 600,
                padding: "0 6px", borderRadius: 6, minWidth: 16,
                lineHeight: 1.5, textAlign: "center" as const,
                background: active ? "var(--surface-muted)" : "var(--surface-sunken)",
                color: active ? "var(--text-default)" : "var(--text-muted)",
              }}>
                {tab.count}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}
