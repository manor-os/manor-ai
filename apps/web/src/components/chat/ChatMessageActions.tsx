import { useState } from "react";
import { t } from "../../lib/i18n";
import type { ChatMessage } from "../../lib/chatStream";
import { IconCheck, IconCopy, IconRefresh, IconThumbDown, IconThumbUp } from "../icons";

export type ChatMessageFeedbackRating = "up" | "down";

interface ChatMessageActionsProps {
  align?: "left" | "right";
  copyText?: string;
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
        <button
          type="button"
          className="chat-message-action chat-message-action--retry"
          disabled={disabled}
          onClick={() => void onRetry()}
          title={retryTitle}
          aria-label={retryTitle}
        >
          <IconRefresh size={13} aria-hidden="true" />
        </button>
      )}
      {safeCopyText && (
        <button
          type="button"
          className="chat-message-action"
          onClick={copy}
          title={copyTitle}
          aria-label={copyTitle}
        >
          {copied ? (
            <IconCheck size={13} aria-hidden="true" />
          ) : (
            <IconCopy size={13} aria-hidden="true" />
          )}
        </button>
      )}
      {canFeedback && (
        <>
          <button
            type="button"
            className={`chat-message-action ${
              feedbackValue === "up" ? "chat-message-action--active" : ""
            }`}
            disabled={disabled}
            onClick={() => void onFeedback?.("up")}
            title={t("component.chat_message_actions.thumbs_up")}
            aria-label={t("component.chat_message_actions.thumbs_up")}
          >
            <IconThumbUp size={13} aria-hidden="true" />
          </button>
          <button
            type="button"
            className={`chat-message-action ${
              feedbackValue === "down" ? "chat-message-action--active" : ""
            }`}
            disabled={disabled}
            onClick={() => void onFeedback?.("down")}
            title={t("component.chat_message_actions.thumbs_down")}
            aria-label={t("component.chat_message_actions.thumbs_down")}
          >
            <IconThumbDown size={13} aria-hidden="true" />
          </button>
        </>
      )}
    </div>
  );
}

export function isRetryableAssistantMessage(
  message: ChatMessage,
  visibleContent?: string,
): boolean {
  if (message.role !== "assistant") return false;
  if (message.stream_error || message.stop_reason === "error") return true;
  const text = (visibleContent || message.content || "").trim().toLowerCase();
  return (
    text.includes("request failed") ||
    text.includes("couldn't generate a response") ||
    text.includes("could not generate a response") ||
    text.includes("before the model could respond") ||
    text.startsWith("error:") ||
    text.startsWith("request failed")
  );
}

export function displayContentForAssistantMessage(
  message: ChatMessage,
  visibleContent?: string,
): string {
  if (!isRetryableAssistantMessage(message, visibleContent)) {
    return visibleContent || message.content || "";
  }
  return t("component.chat_message_actions.request_failed_summary");
}
