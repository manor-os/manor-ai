/**
 * How much of a long piece of chat content is shown before "Show N more lines".
 *
 * Each kind shows everything up to `fullUpTo` lines (or rows); longer content
 * starts with `preview` lines. Folding that would hide only a sliver is not
 * worth a click, so content whose hidden part is under `MIN_HIDDEN_LINES` or
 * under a third of the whole is shown in full.
 */
export type ContentFoldKind = "assistant" | "user" | "table" | "document";

export const CONTENT_FOLD_RULES: Record<ContentFoldKind, { fullUpTo: number; preview: number }> = {
  assistant: { fullUpTo: 60, preview: 40 },
  user: { fullUpTo: 20, preview: 12 },
  table: { fullUpTo: 15, preview: 10 },
  document: { fullUpTo: 30, preview: 20 },
};

export const MIN_HIDDEN_LINES = 8;

export type ContentFold = { visible: number; hidden: number };

/** The lines to show first, or null when the content is shown in full. */
export function contentFold(kind: ContentFoldKind, totalLines: number): ContentFold | null {
  const total = Math.max(0, Math.floor(totalLines));
  const { fullUpTo, preview } = CONTENT_FOLD_RULES[kind];
  if (total <= fullUpTo) return null;
  const hidden = total - preview;
  if (hidden < MIN_HIDDEN_LINES || hidden * 3 < total) return null;
  return { visible: preview, hidden };
}
