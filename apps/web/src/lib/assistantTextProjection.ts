import type { AssistantBlock, AssistantProcessStep } from "./chatStream";
import { hasAppendedCanonicalFinal, hasRunningAssistantProcess, visibleAssistantText } from "./assistant-visible-text.mjs";
import { formatUserFacingStructuredText } from "./taskDisplay";

export { visibleAssistantText } from "./assistant-visible-text.mjs";

export type AssistantTextProjection = {
  processOpeningText: string;
  processProgressText: string;
  progressByStepSeq: Map<number, string>;
  trailingProgressText: string;
  hasFinalText: boolean;
  authoritativeContent: string;
  shouldUseAuthoritativeContent: boolean;
  hasRunningProcess: boolean;
  recoveredFinalText: string;
  liveFinalText: string;
  liftedFinalText: string;
  fallbackFinalText: string;
  openingText: string;
  hasFinalOutput: boolean;
  liveThinkingText: string;
  commentaryText: string;
  shouldSuppressFinalText: (text: string) => boolean;
};

/** Latest user-facing thinking snippet for a still-collapsed live process. */
export function currentLiveThinkingText({
  openingText,
  processNote,
  steps,
  progressByStepSeq,
  trailingUnkeyedProgress = "",
}: {
  openingText?: string | null;
  processNote?: string | null;
  steps?: Array<{ seq?: number; assistant_text?: string | null }>;
  progressByStepSeq?: Map<number, string>;
  trailingUnkeyedProgress?: string;
}): string {
  let latest = (openingText || "").trim();
  const note = (processNote || "").trim();
  if (note) latest = note;
  (steps || []).forEach((step, index) => {
    const assistant = (step.assistant_text || "").trim();
    if (assistant) latest = assistant;
    const seq = Number(step.seq || index + 1);
    const progress = (progressByStepSeq?.get(seq) || "").trim();
    if (progress) latest = progress;
  });
  const trailing = (trailingUnkeyedProgress || "").trim();
  if (trailing) latest = trailing;
  return latest;
}

export type AssistantProcessTimelineEntry =
  | { id: string; type: "commentary"; text: string }
  | { id: string; type: "step"; stepIndex: number };

/** Keep narration next to its tool boundary, including legacy bound narration. */
export function projectAssistantProcessTimeline({
  steps,
  openingText = "",
  processNote = "",
  progressByStepSeq,
  trailingProgressText = "",
}: {
  steps: AssistantProcessStep[];
  openingText?: string;
  processNote?: string;
  progressByStepSeq: Map<number, string>;
  trailingProgressText?: string;
}): AssistantProcessTimelineEntry[] {
  const entries: AssistantProcessTimelineEntry[] = [];
  const shownText = new Set<string>();
  const appendCommentary = (id: string, text?: string | null) => {
    const trimmed = (text || "").trim();
    if (!trimmed || shownText.has(trimmed)) return;
    shownText.add(trimmed);
    entries.push({ id, type: "commentary", text: trimmed });
  };
  appendCommentary("opening", openingText);
  appendCommentary("note", processNote);
  steps.forEach((step, index) => {
    const id = step.id || `step-${index}`;
    appendCommentary(`before-${id}`, step.assistant_text);
    entries.push({ id, type: "step", stepIndex: index });
    appendCommentary(`after-${id}`, progressByStepSeq.get(Number(step.seq || index + 1)));
  });
  appendCommentary("trailing", trailingProgressText);
  return entries;
}

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
      (block) =>
        block.type === "text" &&
        block.phase === "opening",
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
  const hasFinalPhase = textBlocks.some((block) => block.phase === "final");
  const finalParts = textBlocks
    .filter((block) => block.phase === "final")
    .map((block) => block.text)
    .filter((text) => Boolean(text.trim()));
  const finalText = finalParts.join("");
  const authoritativeContent = (content || "").trim();
  const hasRunningProcess = hasRunningAssistantProcess(blocks);
  // Older producers appended the canonical answer after an earlier final
  // snapshot, sometimes more than once. Only reconcile completed snapshots
  // whose last final agrees with the body; a live suffix or stale body must
  // not replace newer structured output.
  const shouldUseAuthoritativeContent =
    Boolean(authoritativeContent) &&
    Boolean(finalText.trim()) &&
    (hasAppendedCanonicalFinal(authoritativeContent, finalParts, streaming, hasRunningProcess) ||
      (authoritativeContent.length > finalText.trim().length &&
        authoritativeContent.endsWith(finalText.trim())));
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
  const hasProcessBlock = firstProcessIndex >= 0;
  // summary_start marks the process default_collapsed; the between-tool idle
  // gap is also status=completed, but default_collapsed stays false there.
  const processIsSummarizing = blocks.some(
    (block) => block.type === "process" && block.default_collapsed === true,
  );
  const lastStepSeq = blocks.reduce((maxSeq, block) => {
    if (block.type !== "process") return maxSeq;
    (block.steps || []).forEach((step, index) => {
      const seq = Number(step.seq || index + 1);
      if (Number.isFinite(seq) && seq > maxSeq) maxSeq = seq;
    });
    return maxSeq;
  }, 0);
  const lastProgressAfterLastStep = lastStepSeq
    ? (progressByStepSeq.get(lastStepSeq) || "").trim()
    : "";
  const trailingUnkeyedProgress = textBlocks
    .filter((block) => {
      if (block.phase !== "progress" || !block.text || isStepAssistantText(block.text)) {
        return false;
      }
      const seq = typeof block.after_step_seq === "number" ? block.after_step_seq : 0;
      return !seq;
    })
    .map((block) => block.text)
    .filter(Boolean)
    .join("\n\n")
    .trim();
  const liveFinalText =
    streaming &&
    !hasFinalText &&
    !hasRunningProcess &&
    Boolean(authoritativeContent) &&
    !contentIsOnlyOpeningText &&
    !contentIsOnlyProcessText &&
    (!hasProcessBlock || processIsSummarizing)
      ? authoritativeContent
      : "";
  const leftoverHeroText =
    !streaming &&
    hasProcessBlock &&
    Boolean(authoritativeContent) &&
    !contentIsOnlyOpeningText &&
    !contentIsOnlyProcessText
      ? authoritativeContent
      : "";
  const liftedFinalText =
    !hasFinalPhase &&
    !hasRunningProcess &&
    hasProcessBlock &&
    !liveFinalText &&
    !shouldUseAuthoritativeContent &&
    (!streaming || processIsSummarizing)
      ? (lastProgressAfterLastStep || trailingUnkeyedProgress || leftoverHeroText)
      : "";
  const fallbackFinalText =
    !hasFinalText &&
    !hasRunningProcess &&
    !recoveredFinalText &&
    !liveFinalText &&
    !liftedFinalText &&
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
  const visibleProgressByStepSeq = new Map(progressByStepSeq);
  if (
    liftedFinalText &&
    lastStepSeq &&
    (visibleProgressByStepSeq.get(lastStepSeq) || "").trim() === liftedFinalText
  ) {
    visibleProgressByStepSeq.delete(lastStepSeq);
  }
  const openingText = fallbackFinalText ? "" : processOpeningText;
  const hasFinalOutput = Boolean(
    hasFinalText ||
      recoveredFinalText ||
      liveFinalText ||
      liftedFinalText ||
      fallbackFinalText ||
      (shouldUseAuthoritativeContent && authoritativeContent),
  );
  const processBlocks = blocks.filter(
    (block): block is Extract<AssistantBlock, { type: "process" }> =>
      block.type === "process",
  );
  const processSteps = processBlocks.flatMap((block) => block.steps || []);
  const processNote = processBlocks
    .map((block) => (block.note || "").trim())
    .filter(Boolean)
    .at(-1) || "";
  const liveThinkingText = hasFinalOutput
    ? ""
    : currentLiveThinkingText({
        openingText: processOpeningText,
        processNote,
        steps: processSteps,
        progressByStepSeq,
        trailingUnkeyedProgress,
      });
  const visibleProgressTexts: string[] = [];
  visibleProgressByStepSeq.forEach((text) => {
    const trimmed = (text || "").trim();
    if (trimmed && trimmed !== liftedFinalText) visibleProgressTexts.push(trimmed);
  });
  const trailingVisibleProgress =
    trailingUnkeyedProgress && trailingUnkeyedProgress !== liftedFinalText
      ? trailingUnkeyedProgress
      : "";
  if (
    trailingVisibleProgress &&
    !visibleProgressTexts.includes(trailingVisibleProgress)
  ) {
    visibleProgressTexts.push(trailingVisibleProgress);
  }
  const modelCommentary = [processOpeningText.trim(), ...visibleProgressTexts]
    .filter(Boolean)
    .join("\n\n");
  const commentaryText =
    modelCommentary ||
    ((streaming || hasRunningProcess) ? liveThinkingText : "");
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
    progressByStepSeq: visibleProgressByStepSeq,
    trailingProgressText: trailingVisibleProgress,
    hasFinalText,
    authoritativeContent,
    shouldUseAuthoritativeContent,
    hasRunningProcess,
    recoveredFinalText,
    liveFinalText,
    liftedFinalText,
    fallbackFinalText,
    openingText,
    hasFinalOutput,
    liveThinkingText,
    commentaryText,
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
    addRenderedAssistantSource(projection.liftedFinalText);
  }
  addRenderedAssistantSource(projection.fallbackFinalText);

  return sources.join("\n\n");
}
