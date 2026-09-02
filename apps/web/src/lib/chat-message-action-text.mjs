import { visibleAssistantText } from "./assistant-visible-text.mjs";

function textValue(value) {
  return typeof value === "string" ? value.trim() : "";
}

/**
 * Return the text represented by a chat row's visible assistant output.
 *
 * Structured assistant blocks are authoritative once a final block exists.
 * The longer persisted content is retained only when it is the same answer
 * with leading context, matching AssistantMessageBlocks rendering semantics.
 */
export function chatMessageActionText(message, visibleContent) {
  const rawDirect = textValue(visibleContent) || textValue(message?.content);
  if (message?.role !== "assistant") return rawDirect;
  const direct = visibleAssistantText(rawDirect);

  const textBlocks = Array.isArray(message?.assistant_blocks)
    ? message.assistant_blocks.filter(
        (block) =>
          block &&
          typeof block === "object" &&
          block.type === "text" &&
          textValue(block.text),
      )
    : [];
  const finalParts = textBlocks
    .filter((block) => block.phase === "final")
    .map((block) => visibleAssistantText(block.text))
    .filter(Boolean);
  const finalText = finalParts.join("\n\n").trim();
  const compactFinalText = finalParts.join("").trim();

  if (finalText) {
    const directContainsFinal =
      direct.length > compactFinalText.length &&
      (direct.endsWith(finalText) || direct.endsWith(compactFinalText));
    return directContainsFinal
      ? direct
      : finalText;
  }
  if (direct) return direct;

  return textBlocks
    .filter((block) => block.phase !== "opening" && block.phase !== "progress")
    .map((block) => visibleAssistantText(block.text))
    .join("\n\n")
    .trim();
}
