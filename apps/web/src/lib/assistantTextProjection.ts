import type { AssistantBlock } from "./chatStream";
import { visibleAssistantText } from "./assistant-visible-text.mjs";
import { formatUserFacingStructuredText } from "./taskDisplay";

export { visibleAssistantText } from "./assistant-visible-text.mjs";

export type AssistantTextProjection = {
  processOpeningText: string;
  processProgressText: string;
  progressByStepSeq: Map<number, string>;
  hasFinalText: boolean;
  authoritativeContent: string;
  shouldUseAuthoritativeContent: boolean;
  hasRunningProcess: boolean;
  recoveredFinalText: string;
  liveFinalText: string;
  fallbackFinalText: string;
  openingText: string;
  hasFinalOutput: boolean;
  shouldSuppressFinalText: (text: string) => boolean;
};

/**
 * Resolve the text surfaces rendered around structured assistant process blocks.
 * Keep this projection shared with attachment-card dedupe so legacy recovered
 * answers and structured final answers are treated consistently.
 */
export function projectAssistantText(
  content: string | null | undefined,
  blocks: AssistantBlock[],
  streaming: boolean,
): AssistantTextProjection {
  const firstProcessIndex = blocks.findIndex((block) => block.type === "process");
  const stepAssistantTexts = blocks
    .filter(
      (block): block is Extract<AssistantBlock, { type: "process" }> =>
        block.type === "process",
    )
    .flatMap((block) => block.steps || [])
    .map((step) => step.assistant_text?.trim())
    .filter(Boolean) as string[];
  const textBlocks = blocks.filter(
    (block): block is Extract<AssistantBlock, { type: "text" }> =>
      block.type === "text",
  );
  const isStepAssistantText = (text: string) => {
    const trimmed = text.trim();
    return Boolean(
      trimmed && stepAssistantTexts.some((stepText) => stepText === trimmed),
    );
  };
  const processOpeningText = blocks
    .filter(
      (block, index) =>
        block.type === "text" &&
        block.phase === "opening" &&
        (firstProcessIndex < 0 || index < firstProcessIndex),
    )
    .map((block) => ("text" in block ? block.text : ""))
    .filter(Boolean)
    .join("");
  const processProgressText = textBlocks
    .filter((block) => block.phase === "progress")
    .map((block) => block.text)
    .filter((text) => text && !isStepAssistantText(text))
    .join("\n\n");
  const progressByStepSeq = new Map<number, string>();
  textBlocks
    .filter((block) => block.phase === "progress")
    .forEach((block) => {
      const seq = typeof block.after_step_seq === "number" ? block.after_step_seq : 0;
      const text = block.text || "";
      if (!seq || !text || isStepAssistantText(text)) return;
      const existing = progressByStepSeq.get(seq);
      progressByStepSeq.set(seq, existing ? `${existing}\n\n${text}` : text);
    });
  const legacyPostProcessOpeningText = blocks
    .filter(
      (block, index) =>
        block.type === "text" &&
        block.phase !== "opening" &&
        block.phase !== "progress" &&
        block.phase !== "final" &&
        firstProcessIndex >= 0 &&
        index > firstProcessIndex,
    )
    .map((block) => ("text" in block ? block.text : ""))
    .filter((text) => text && !isStepAssistantText(text))
    .join("");
  const hasFinalText = textBlocks.some(
    (block) => block.phase === "final" && Boolean(block.text),
  );
  const finalText = textBlocks
    .filter((block) => block.phase === "final")
    .map((block) => block.text)
    .filter(Boolean)
    .join("");
  const authoritativeContent = (content || "").trim();
  const shouldUseAuthoritativeContent =
    Boolean(authoritativeContent) &&
    Boolean(finalText.trim()) &&
    authoritativeContent.length > finalText.trim().length &&
    authoritativeContent.endsWith(finalText.trim());
  const hasRunningProcess = blocks.some(
    (block) =>
      block.type === "process" &&
      (block.status === "running" ||
        block.status === "pending" ||
        (block.steps || []).some(
          (step) => step.status === "running" || step.status === "pending",
        )),
  );
  const recoveredFinalText =
    !hasRunningProcess && !shouldUseAuthoritativeContent
      ? legacyPostProcessOpeningText
      : "";
  const contentIsOnlyOpeningText = Boolean(
    processOpeningText.trim() &&
      authoritativeContent === processOpeningText.trim(),
  );
  const processText = `${processOpeningText}${processProgressText}`.trim();
  const contentIsOnlyProcessText = Boolean(
    processText &&
      (authoritativeContent === processText ||
        processText.endsWith(authoritativeContent)),
  );
  const liveFinalText =
    streaming &&
    !hasFinalText &&
    !hasRunningProcess &&
    Boolean(authoritativeContent) &&
    !contentIsOnlyOpeningText &&
    !contentIsOnlyProcessText
      ? authoritativeContent
      : "";
  const fallbackFinalText =
    !hasFinalText &&
    !hasRunningProcess &&
    !recoveredFinalText &&
    !liveFinalText &&
    !authoritativeContent
      ? textBlocks
          .filter(
            (block) =>
              block.phase !== "opening" && block.phase !== "progress",
          )
          .map((block) => block.text)
          .filter((text) => text && !isStepAssistantText(text))
          .filter(Boolean)
          .join("")
      : "";
  const openingText = fallbackFinalText ? "" : processOpeningText;
  const hasFinalOutput = Boolean(
    hasFinalText ||
      recoveredFinalText ||
      liveFinalText ||
      fallbackFinalText ||
      (shouldUseAuthoritativeContent && authoritativeContent),
  );
  const shouldSuppressFinalText = (text: string) => {
    const trimmed = text.trim();
    if (shouldUseAuthoritativeContent) return true;
    return Boolean(
      recoveredFinalText && trimmed && recoveredFinalText.includes(trimmed),
    );
  };

  return {
    processOpeningText,
    processProgressText,
    progressByStepSeq,
    hasFinalText,
    authoritativeContent,
    shouldUseAuthoritativeContent,
    hasRunningProcess,
    recoveredFinalText,
    liveFinalText,
    fallbackFinalText,
    openingText,
    hasFinalOutput,
    shouldSuppressFinalText,
  };
}

/** Collect the Markdown text that the assistant message can render as output. */
export function markdownContentWithRenderedAssistantFinalText(
  content: unknown,
  assistantBlocks: unknown,
  streaming = false,
): string {
  const hasAssistantBlockInput =
    Array.isArray(assistantBlocks) && assistantBlocks.length > 0;
  const source = visibleAssistantText(
    typeof content === "string" ? content : "",
  );
  const blocks = Array.isArray(assistantBlocks)
    ? assistantBlocks.flatMap<AssistantBlock>((value) => {
        if (!value || typeof value !== "object") return [];
        const block = value as Record<string, unknown>;
        if (block.type === "text" && typeof block.text === "string") {
          return [{
            ...block,
            text: visibleAssistantText(block.text),
          } as unknown as AssistantBlock];
        }
        if (block.type !== "process") return [];
        const steps = Array.isArray(block.steps)
          ? block.steps.flatMap((value) => {
              if (!value || typeof value !== "object") return [];
              const step = value as Record<string, unknown>;
              return [{
                ...step,
                assistant_text:
                  typeof step.assistant_text === "string"
                    ? visibleAssistantText(step.assistant_text)
                    : undefined,
              }];
            })
          : [];
        return [{ ...block, steps } as unknown as AssistantBlock];
      })
    : [];
  const projection = projectAssistantText(source, blocks, streaming);
  const sources: string[] = [];
  const addSource = (value: unknown) => {
    if (typeof value === "string" && value.trim()) sources.push(value);
  };
  const addRenderedAssistantSource = (value: unknown) => {
    if (typeof value !== "string" || !value.trim()) return;
    addSource(formatUserFacingStructuredText(value));
  };

  if (!hasAssistantBlockInput) {
    addSource(source);
  } else {
    blocks.forEach((block) => {
      if (
        block?.type === "text" &&
        block.phase === "final" &&
        !projection.shouldSuppressFinalText(block.text)
      ) {
        addRenderedAssistantSource(block.text);
      }
    });
    if (projection.shouldUseAuthoritativeContent) {
      addRenderedAssistantSource(projection.authoritativeContent);
    }
  }
  addRenderedAssistantSource(projection.recoveredFinalText);
  if (hasAssistantBlockInput) {
    addRenderedAssistantSource(projection.liveFinalText);
  }
  addRenderedAssistantSource(projection.fallbackFinalText);

  return sources.join("\n\n");
}
