export type MarkdownCommentMarkerNode = {
  type: string;
  value?: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: MarkdownCommentMarkerNode[];
  position?: {
    start?: { offset?: number };
    end?: { offset?: number };
  };
};

export function commentSearchParts(value: string): string[];
export function commentSearchKey(value: string): string;
export function searchableCommentQuote(value: string): string;
export function commentActionLabel(
  comment: {
    content?: string | null;
    display_name?: string | null;
    user_display_name?: string | null;
  } | null | undefined,
  fallbackQuote?: string | null,
  commentLabel?: string,
  ordinal?: number,
): string;
export function layoutCommentRanges<T extends {
  start: number;
  end: number;
}>(ranges: ReadonlyArray<T>): Array<T & { actionOffset?: number }>;
export function updateCommentMarkActiveState(
  root: ParentNode,
  activeCommentId?: string | null,
): void;
export function markdownCommentMarkerPlugin(
  comments: ReadonlyArray<{
    id: string;
    content?: string | null;
    display_name?: string | null;
    user_display_name?: string | null;
    anchor?: {
      mode?: string | null;
      quote?: string | null;
      quote_occurrence?: number | null;
    } | null;
  }>,
  ranges: ReadonlyArray<{
    id: string;
    start: number;
    end: number;
    quote?: string;
    accessibleLabel?: string;
  }>,
  activeCommentId?: string | null,
  commentLabel?: string,
): () => (tree: MarkdownCommentMarkerNode) => void;
