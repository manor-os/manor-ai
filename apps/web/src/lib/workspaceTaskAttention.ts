const MAX_SEEN_TASK_ATTENTION_KEYS = 500;

function normalizedAttentionKeys(keys: readonly string[]): string[] {
  return Array.from(new Set(keys.map((key) => key.trim()).filter(Boolean)));
}

export function parseSeenTaskAttention(value: string | null | undefined): string[] {
  const serialized = String(value || "").trim();
  if (!serialized) return [];
  try {
    const parsed = JSON.parse(serialized);
    if (Array.isArray(parsed)) {
      return normalizedAttentionKeys(parsed.filter((key): key is string => typeof key === "string"));
    }
  } catch {
    // Values written before per-item tracking used a pipe-delimited snapshot.
  }
  return normalizedAttentionKeys(serialized.split("|"));
}

export function serializeSeenTaskAttention(keys: readonly string[]): string {
  return JSON.stringify(normalizedAttentionKeys(keys));
}

export function unseenTaskAttentionKeys(
  currentKeys: readonly string[],
  seenKeys: readonly string[],
): string[] {
  const seen = new Set(seenKeys);
  return normalizedAttentionKeys(currentKeys).filter((key) => !seen.has(key));
}

export function unseenTaskAttentionSeverity(
  occurrences: readonly { key: string; status: string }[],
  seenKeys: readonly string[],
): "warning" | "danger" | undefined {
  const unseen = new Set(unseenTaskAttentionKeys(
    occurrences.map((occurrence) => occurrence.key),
    seenKeys,
  ));
  const unseenOccurrences = occurrences.filter((occurrence) => unseen.has(occurrence.key));
  if (unseenOccurrences.length === 0) return undefined;
  return unseenOccurrences.some((occurrence) => (
    occurrence.status === "blocked" || occurrence.status === "failed"
  )) ? "danger" : "warning";
}

export function nextSeenTaskAttentionKeys(
  currentKeys: readonly string[],
  seenKeys: readonly string[],
  markCurrentAsSeen: boolean,
): string[] {
  const current = normalizedAttentionKeys(currentKeys);
  const seen = normalizedAttentionKeys(seenKeys);
  if (!markCurrentAsSeen) return seen;

  const currentSet = new Set(current);
  const historical = seen.filter((key) => !currentSet.has(key));
  return [...historical, ...current].slice(-Math.max(
    MAX_SEEN_TASK_ATTENTION_KEYS,
    current.length,
  ));
}
