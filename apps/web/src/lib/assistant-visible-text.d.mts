export function stripEditorLiveEditBlocks(value?: unknown): string;
export function redactInternalAssistantErrorDetails(value?: unknown): string;
export function visibleAssistantText(value?: unknown): string;
export function hasAppendedCanonicalFinal(
  content: string,
  finalParts: readonly string[],
  streaming: boolean,
  hasRunningProcess: boolean,
): boolean;
export function hasRunningAssistantProcess(blocks: readonly unknown[]): boolean;
