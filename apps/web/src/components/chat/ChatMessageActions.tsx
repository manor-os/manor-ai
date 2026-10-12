import { useState } from "react";
import { t } from "../../lib/i18n";
import {
  LLM_CONFIGURATION_ERROR_MESSAGE,
  formatByokProviderErrorMessage,
  formatFilePermissionErrorMessage,
  type ChatMessage,
} from "../../lib/chatStream";
import { IconCheck, IconCopy, IconRefresh, IconThumbDown, IconThumbUp } from "../icons";
import ChatActionButton from "./ChatActionButton";
import { ReadAloudButton } from "./ChatVoiceControls";
import type { ChatVoiceScope } from "../../lib/chatVoice";

export { chatMessageActionText } from "../../lib/chat-message-action-text.mjs";

export type ChatMessageFeedbackRating = "up" | "down";

interface ChatMessageActionsProps {
  align?: "left" | "right";
  copyText?: string;
  speechText?: string;
  voiceScope?: ChatVoiceScope;
  copyLabel?: string;
  canRetry?: boolean;
  retryLabel?: string;
  feedbackValue?: ChatMessageFeedbackRating | null;
  disabled?: boolean;
  onRetry?: () => void | Promise<void>;
  onFeedback?: (rating: ChatMessageFeedbackRating) => void | Promise<void>;
}

export default function ChatMessageActions({
  align = "left",
  copyText,
  speechText,
  voiceScope,
  copyLabel,
  canRetry = false,
  retryLabel,
  feedbackValue,
  disabled = false,
  onRetry,
  onFeedback,
}: ChatMessageActionsProps) {
  const [copied, setCopied] = useState(false);
  const safeCopyText = (copyText || "").trim();
  const retryTitle = retryLabel || t("component.chat_message_actions.retry");
  const copyTitle = copied
    ? t("component.chat_message_actions.copied")
    : copyLabel || t("action.copy");
  const canFeedback = Boolean(onFeedback);
  if (!safeCopyText && !canRetry && !canFeedback) return null;

  const copy = async () => {
    if (!safeCopyText) return;
    await navigator.clipboard.writeText(safeCopyText);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  };

  return (
    <div
      className={`chat-message-actions ${
        align === "right" ? "chat-message-actions--right" : ""
      }`}
    >
      {canRetry && onRetry && (
        <ChatActionButton
          retry
          disabled={disabled}
          onClick={() => void onRetry()}
          title={retryTitle}
          aria-label={retryTitle}
        >
          <IconRefresh size={13} aria-hidden="true" />
        </ChatActionButton>
      )}
      {safeCopyText && (
        <ChatActionButton
          onClick={copy}
          title={copyTitle}
          aria-label={copyTitle}
        >
          {copied ? (
            <IconCheck size={13} aria-hidden="true" />
          ) : (
            <IconCopy size={13} aria-hidden="true" />
          )}
        </ChatActionButton>
      )}
      {speechText?.trim() && <ReadAloudButton text={speechText} scope={voiceScope} disabled={disabled} />}
      {canFeedback && (
        <>
          <ChatActionButton
            active={feedbackValue === "up"}
            aria-pressed={feedbackValue === "up"}
            disabled={disabled}
            onClick={() => void onFeedback?.("up")}
            title={t("component.chat_message_actions.thumbs_up")}
            aria-label={t("component.chat_message_actions.thumbs_up")}
          >
            <IconThumbUp size={13} aria-hidden="true" />
          </ChatActionButton>
          <ChatActionButton
            active={feedbackValue === "down"}
            aria-pressed={feedbackValue === "down"}
            disabled={disabled}
            onClick={() => void onFeedback?.("down")}
            title={t("component.chat_message_actions.thumbs_down")}
            aria-label={t("component.chat_message_actions.thumbs_down")}
          >
            <IconThumbDown size={13} aria-hidden="true" />
          </ChatActionButton>
        </>
      )}
    </div>
  );
}

function assistantContentLooksLikeFailure(text: string): boolean {
  const normalized = text.trim().toLowerCase();
  if (!normalized) return false;
  return (
    normalized.includes("request failed") ||
    normalized.includes("couldn't generate a response") ||
    normalized.includes("could not generate a response") ||
    normalized.includes("before the model could respond") ||
    normalized.startsWith("error:") ||
    normalized.startsWith("request failed")
  );
}

function assistantMessageHasSpecialFailureDisplay(
  message: ChatMessage,
  content: string,
): boolean {
  return (
    message.meta?.error === "llm_configuration_missing"
    || message.meta?.error === "byok_provider_error"
    || content === LLM_CONFIGURATION_ERROR_MESSAGE
    || content.endsWith(`\n\nError detail: ${LLM_CONFIGURATION_ERROR_MESSAGE}`)
    || Boolean(formatByokProviderErrorMessage(content))
    || Boolean(formatFilePermissionErrorMessage(content))
  );
}

function recoverableAssistantAnswer(
  message: ChatMessage,
  visibleContent?: string,
): string {
  const content = (visibleContent || message.content || "").trim();
  if (content && !assistantContentLooksLikeFailure(content)) return content;
  const finalText = (message.assistant_blocks || [])
    .filter((block) => block.type === "text" && block.phase === "final")
    .map((block) => ("text" in block ? block.text.trim() : ""))
    .filter(Boolean)
    .join("\n\n")
    .trim();
  if (finalText && !assistantContentLooksLikeFailure(finalText)) return finalText;
  return "";
}

export function isRetryableAssistantMessage(
  message: ChatMessage,
  visibleContent?: string,
): boolean {
  if (message.role !== "assistant") return false;
  if (message.meta?.error === "final_response_validation_failed") return true;
  const content = visibleContent || message.content || "";
  if (
    !assistantMessageHasSpecialFailureDisplay(message, content)
    && recoverableAssistantAnswer(message, visibleContent)
  ) {
    return false;
  }
  if (message.stream_error || message.stop_reason === "error") return true;
  return assistantContentLooksLikeFailure(content);
}

export function displayContentForAssistantMessage(
  message: ChatMessage,
  visibleContent?: string,
): string {
  const content = visibleContent || message.content || "";
  if (message.role === "assistant" && (
    message.meta?.error === "llm_configuration_missing"
    || content === LLM_CONFIGURATION_ERROR_MESSAGE
    || content.endsWith(`\n\nError detail: ${LLM_CONFIGURATION_ERROR_MESSAGE}`)
  )) {
    // This fixed public recovery instruction contains no provider diagnostic.
    // Other failures retain the existing generic summary and redaction path.
    return LLM_CONFIGURATION_ERROR_MESSAGE;
  }
  if (message.role === "assistant") {
    const permissionContent = formatFilePermissionErrorMessage(content);
    if (permissionContent) return permissionContent;
    // The owner's own provider failed: say so, with the structured detail,
    // instead of the generic "did not finish" summary.
    const byokContent = formatByokProviderErrorMessage(content)
      ?? (message.meta?.error === "byok_provider_error"
        ? formatByokProviderErrorMessage("byok_provider_error")
        : null);
    if (byokContent) return byokContent;
  }
  const recovered = recoverableAssistantAnswer(message, visibleContent);
  if (recovered) return recovered;
  if (!isRetryableAssistantMessage(message, visibleContent)) {
    return content;
  }
  return t("component.chat_message_actions.request_failed_summary");
}
