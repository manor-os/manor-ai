export interface ChatMessageActionTextSource {
  role?: unknown;
  content?: unknown;
  assistant_blocks?: readonly unknown[] | null;
}

export function chatMessageActionText(
  message: ChatMessageActionTextSource,
  visibleContent?: unknown,
): string;
