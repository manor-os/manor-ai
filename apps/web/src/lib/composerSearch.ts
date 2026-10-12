/** One identity/ranking contract for /skills, @mentions and %flows. */
export function searchComposerOptions<T>(
  items: readonly T[],
  query: string,
  options: {
    key: (item: T) => string;
    names: (item: T) => (string | null | undefined)[];
    keywords: (item: T) => (string | null | undefined)[];
    limit: number;
  },
): T[] {
  const q = query.trim().toLocaleLowerCase();
  const seen = new Set<string>();
  return items.flatMap((item, index) => {
    const key = options.key(item);
    if (!key || seen.has(key)) return [];
    seen.add(key);
    const names = options.names(item).filter(Boolean).map((name) => name!.toLocaleLowerCase());
    const keywords = options.keywords(item).filter(Boolean).join(" ").toLocaleLowerCase();
    const score = !q || names.some((name) => name === q) ? 0
      : names.some((name) => name.startsWith(q)) ? 1
      : names.some((name) => name.includes(q)) ? 2
      : keywords.includes(q) ? 3 : -1;
    return score < 0 ? [] : [{ item, score, index }];
  }).sort((a, b) => a.score - b.score || a.index - b.index)
    .slice(0, options.limit).map(({ item }) => item);
}

export interface ComposerResourceOrigin {
  entity_id?: string | null;
  workspace_id?: string | null;
  workspace_name?: string | null;
  visibility?: string | null;
}

export function composerResourceOrigin(resource: ComposerResourceOrigin, labels: {
  builtin: string; workspace: string; personal: string; shared: string;
}): string {
  if (resource.workspace_id) return resource.workspace_name || labels.workspace;
  if (resource.entity_id === null) return labels.builtin;
  return resource.visibility === "private" ? labels.personal : labels.shared;
}

/** Same display name is not the same installed resource. Disambiguate even
 * copies within one Workspace, where the source label alone is insufficient. */
export function composerIdentityLabel<T>(item: T, items: readonly T[], options: {
  key: (item: T) => string; name: (item: T) => string; source: (item: T) => string;
}): string {
  const name = options.name(item).trim().toLocaleLowerCase();
  const key = options.key(item);
  const duplicates = items.filter((other) =>
    options.key(other) !== key && options.name(other).trim().toLocaleLowerCase() === name);
  const source = options.source(item);
  if (!duplicates.length) return source;
  let length = 8;
  while (length < key.length && duplicates.some((other) => options.key(other).slice(-length) === key.slice(-length))) length++;
  return [source, key.slice(-length)].filter(Boolean).join(" · ");
}
