import type { SearchResult } from "./types";

type GlobalSearchRecord = {
  id?: unknown;
  type?: unknown;
  name?: unknown;
  title?: unknown;
  preview?: unknown;
  snippet?: unknown;
  score?: unknown;
};

export type GlobalSearchResponse = Record<string, GlobalSearchRecord[] | undefined>;

export function globalSearchPath(query: string, limit?: number): string {
  const params = new URLSearchParams({ q: query });
  if (limit) params.set("limit", String(limit));
  return `/search?${params}`;
}

export function normalizeGlobalSearchResponse(response: GlobalSearchResponse): SearchResult[] {
  return Object.values(response).flatMap((records) =>
    (records ?? []).flatMap((record) => {
      if (typeof record.id !== "string" || typeof record.type !== "string") return [];

      const title = typeof record.title === "string"
        ? record.title
        : typeof record.name === "string" ? record.name : record.id;
      const snippet = typeof record.snippet === "string"
        ? record.snippet
        : typeof record.preview === "string" ? record.preview : undefined;
      const score = typeof record.score === "number" ? record.score : undefined;

      return [{
        id: record.id,
        type: record.type,
        title,
        ...(snippet ? { snippet } : {}),
        ...(score !== undefined ? { score } : {}),
      }];
    }),
  );
}
