import { useState, useEffect } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams, useNavigate } from "react-router-dom";
import { api } from "../lib/api";
import type { SearchResult } from "../lib/types";
import PageHeader from "../components/ui/PageHeader";
import LoadingSpinner from "../components/ui/LoadingSpinner";
import EmptyState from "../components/ui/EmptyState";
import { IconChevronRight } from "../components/icons";
import { t } from "../lib/i18n";

/* ── type icon/badge config ────────────────────────────── */

const TYPE_CONFIG: Record<string, { color: string; bg: string; icon: React.ReactNode; route: (id: string) => string }> = {
  task: {
    color: "#436b65",
    bg: "#f2f6f5",
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        <path d="M9 11l3 3L22 4" />
        <path d="M21 12v7a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h11" />
      </svg>
    ),
    route: (id) => `/tasks/${id}`,
  },
  document: {
    color: "#4869ac",
    bg: "#f3f6fa",
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z" />
        <polyline points="14 2 14 8 20 8" />
        <line x1="16" y1="13" x2="8" y2="13" />
        <line x1="16" y1="17" x2="8" y2="17" />
      </svg>
    ),
    route: () => "/knowledge",
  },
  agent: {
    color: "#6f4ba8",
    bg: "#f5f3ff",
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        <path d="M12 2a7 7 0 017 7c0 2.38-1.19 4.47-3 5.74V17a2 2 0 01-2 2h-4a2 2 0 01-2-2v-2.26C6.19 13.47 5 11.38 5 9a7 7 0 017-7z" />
        <path d="M9 21h6" />
      </svg>
    ),
    route: (id) => `/agents/${id}`,
  },
  conversation: {
    color: "#b27c34",
    bg: "#faf7ef",
    icon: (
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
        <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z" />
      </svg>
    ),
    route: () => `/chat/history`,
  },
};

const DEFAULT_TYPE_CONFIG = {
  color: "#78716c",
  bg: "#fafaf9",
  icon: (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <circle cx="11" cy="11" r="8" />
      <line x1="21" y1="21" x2="16.65" y2="16.65" />
    </svg>
  ),
  route: () => "/",
};

/* ── group results by type ─────────────────────────────── */

function groupByType(results: SearchResult[]): Record<string, SearchResult[]> {
  const groups: Record<string, SearchResult[]> = {};
  for (const r of results) {
    const key = r.type.toLowerCase();
    if (!groups[key]) groups[key] = [];
    groups[key].push(r);
  }
  return groups;
}

/* ── type label ────────────────────────────────────────── */

function typeLabel(type: string): string {
  const labels: Record<string, string> = {
    task: "Tasks",
    document: "Documents",
    agent: "Agents",
    conversation: "Conversations",
  };
  return labels[type] || type.charAt(0).toUpperCase() + type.slice(1) + "s";
}

/* ── main component ────────────────────────────────────── */

export default function SearchResults() {
  const [searchParams, setSearchParams] = useSearchParams();
  const navigate = useNavigate();
  const queryFromUrl = searchParams.get("q") || "";
  const [inputValue, setInputValue] = useState(queryFromUrl);

  // Sync input when URL changes externally
  useEffect(() => {
    setInputValue(queryFromUrl);
  }, [queryFromUrl]);

  const { data: results, isLoading, isFetched } = useQuery<SearchResult[]>({
    queryKey: ["search", queryFromUrl],
    queryFn: () => api.search.global(queryFromUrl),
    enabled: !!queryFromUrl.trim(),
  });

  const grouped = results ? groupByType(results) : {};
  const groupKeys = Object.keys(grouped);
  const totalResults = results?.length ?? 0;

  function handleSearch(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = inputValue.trim();
    if (trimmed) {
      setSearchParams({ q: trimmed });
    }
  }

  function handleResultClick(result: SearchResult) {
    const config = TYPE_CONFIG[result.type.toLowerCase()] || DEFAULT_TYPE_CONFIG;
    navigate(config.route(result.id));
  }

  return (
    <div>
      <PageHeader
        title={t("page.search.title")}
        subtitle={
          queryFromUrl
            ? isFetched
              ? `${totalResults} ${totalResults !== 1 ? t("page.search.results_for") : t("page.search.result_for")} "${queryFromUrl}"`
              : `${t("page.search.searching_for")} "${queryFromUrl}"...`
            : t("page.search.across_workspace")
        }
      />

      {/* Search bar */}
      <form onSubmit={handleSearch} style={{ marginBottom: 24 }}>
        <div style={{ display: "flex", gap: 12, maxWidth: 640 }}>
          <div style={{ flex: 1, position: "relative" }}>
            <svg
              style={{
                position: "absolute",
                left: 14,
                top: "50%",
                transform: "translateY(-50%)",
                width: 16,
                height: 16,
                color: "#a8a29e",
                pointerEvents: "none",
              }}
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
            >
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
            <input
              type="text"
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              placeholder={t("page.search.placeholder")}
              className="manor-input"
              style={{ paddingLeft: 40, height: 44, fontSize: 14 }}
              autoFocus
            />
          </div>
          <button
            type="submit"
            disabled={!inputValue.trim()}
            className="btn-manor"
            style={{ height: 44, paddingLeft: 20, paddingRight: 20, opacity: !inputValue.trim() ? 0.5 : 1 }}
          >
            {t("action.search")}
          </button>
        </div>
      </form>

      {/* Loading */}
      {isLoading && (
        <div style={{ display: "flex", alignItems: "center", justifyContent: "center", padding: "64px 0" }}>
          <LoadingSpinner size={28} />
        </div>
      )}

      {/* Empty state — no query */}
      {!queryFromUrl && !isLoading && (
        <EmptyState
          icon={
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d6d3d1" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
          }
          title={t("page.search.enter_query")}
          description={t("page.search.enter_query_desc")}
        />
      )}

      {/* Empty state — no results */}
      {queryFromUrl && isFetched && !isLoading && totalResults === 0 && (
        <EmptyState
          icon={
            <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="#d6d3d1" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z" />
            </svg>
          }
          title={t("page.search.no_results")}
          description={`${t("page.search.no_results_desc_prefix")} "${queryFromUrl}". ${t("page.search.no_results_desc_suffix")}`}
        />
      )}

      {/* Results grouped by type */}
      {!isLoading && groupKeys.length > 0 && (
        <div style={{ display: "flex", flexDirection: "column", gap: 24 }}>
          {groupKeys.map((type) => {
            const items = grouped[type];
            const config = TYPE_CONFIG[type] || DEFAULT_TYPE_CONFIG;
            return (
              <div key={type}>
                {/* Section header */}
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
                  <span
                    style={{
                      width: 28,
                      height: 28,
                      borderRadius: 8,
                      background: config.bg,
                      color: config.color,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                    }}
                  >
                    {config.icon}
                  </span>
                  <h3 style={{ fontSize: 14, fontWeight: 700, color: "#292524", margin: 0 }}>
                    {typeLabel(type)}
                  </h3>
                  <span
                    style={{
                      fontSize: 11,
                      fontWeight: 600,
                      color: "#a8a29e",
                      background: "#f5f5f4",
                      borderRadius: 10,
                      padding: "2px 8px",
                    }}
                  >
                    {items.length}
                  </span>
                </div>

                {/* Result cards */}
                <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  {items.map((result) => (
                    <button
                      key={`${result.type}-${result.id}`}
                      onClick={() => handleResultClick(result)}
                      className="glass-card"
                      style={{
                        cursor: "pointer",
                        textAlign: "left",
                        border: "none",
                        width: "100%",
                        transition: "all 0.2s",
                      }}
                      onMouseEnter={(e) => {
                        e.currentTarget.style.transform = "translateY(-1px)";
                        e.currentTarget.style.boxShadow = "0 4px 16px rgba(0,0,0,0.06)";
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.transform = "none";
                        e.currentTarget.style.boxShadow = "none";
                      }}
                    >
                      <div style={{ display: "flex", alignItems: "flex-start", gap: 12 }}>
                        {/* Type badge */}
                        <span
                          style={{
                            width: 32,
                            height: 32,
                            borderRadius: 10,
                            background: config.bg,
                            color: config.color,
                            display: "flex",
                            alignItems: "center",
                            justifyContent: "center",
                            flexShrink: 0,
                            marginTop: 2,
                          }}
                        >
                          {config.icon}
                        </span>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 2 }}>
                            <h4
                              style={{
                                fontSize: 13,
                                fontWeight: 700,
                                color: "#292524",
                                margin: 0,
                                overflow: "hidden",
                                textOverflow: "ellipsis",
                                whiteSpace: "nowrap",
                              }}
                            >
                              {result.title}
                            </h4>
                            <span
                              style={{
                                fontSize: 10,
                                fontWeight: 600,
                                color: config.color,
                                background: config.bg,
                                padding: "1px 6px",
                                borderRadius: 6,
                                flexShrink: 0,
                                textTransform: "capitalize",
                              }}
                            >
                              {result.type}
                            </span>
                          </div>
                          {result.snippet && (
                            <p style={{ fontSize: 12, color: "#78716c", margin: 0, lineHeight: 1.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              {result.snippet}
                            </p>
                          )}
                        </div>
                        {/* Score */}
                        {result.score !== undefined && result.score !== null && (
                          <span
                            style={{
                              fontSize: 11,
                              fontWeight: 600,
                              color: "#a8a29e",
                              flexShrink: 0,
                              marginTop: 2,
                            }}
                          >
                            {Math.round(result.score * 100)}%
                          </span>
                        )}
                        {/* Arrow */}
                        <span style={{ flexShrink: 0, marginTop: 6, color: "#d6d3d1" }}>
                          <IconChevronRight size={16} />
                        </span>
                      </div>
                    </button>
                  ))}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
