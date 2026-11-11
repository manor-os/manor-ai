import { t } from "../../lib/i18n";
import RecommendedOptions from "./RecommendedOptions";

/** One open authorization that shares its `batch_key` with others. */
export interface BatchItem {
  /** The card's identity: the message id for a message card, else the request id. */
  id: string;
  /** "action": a message's `pending_action`; "hitl": a tool-call request on a message. */
  kind: "action" | "hitl";
  msgId: string;
  summary: string;
}

export interface BatchGroup {
  key: string;
  items: BatchItem[];
}

type BatchSourceMessage = {
  id: string;
  resolved_at?: string | null;
  content?: string | null;
  pending_action?: Record<string, unknown> | null;
  hitl_requests?: Array<Record<string, unknown>> | null;
};

function firstLine(...values: unknown[]): string {
  const text = values.find((value): value is string => typeof value === "string" && Boolean(value.trim())) || "";
  return text.trim().split("\n")[0].slice(0, 120);
}

/**
 * Open authorizations of the same kind, in timeline order, two or more per
 * group. Message cards and tool-call requests both count.
 */
export function collectPendingBatches(messages: BatchSourceMessage[]): BatchGroup[] {
  const groups = new Map<string, BatchItem[]>();
  const add = (key: unknown, item: BatchItem) => {
    if (typeof key !== "string" || !key) return;
    groups.set(key, [...(groups.get(key) || []), item]);
  };
  for (const message of messages) {
    const action = message.pending_action;
    if (action && !message.resolved_at) {
      const who = typeof action.sender_name === "string" ? `${action.sender_name} · ` : "";
      add(action.batch_key, {
        id: message.id, kind: "action", msgId: message.id,
        summary: who + firstLine(action.reply_text, action.prompt, message.content),
      });
    }
    for (const request of message.hitl_requests || []) {
      // A run parked on its approval is decided against that run, one card
      // at a time; it is never folded into a batch.
      if (!request || request.resolved || !request.id || request.runtime_run_id) continue;
      add(request.batch_key, {
        id: String(request.id), kind: "hitl", msgId: message.id,
        summary: firstLine(request.prompt, request.action),
      });
    }
  }
  return [...groups.entries()]
    .filter(([, items]) => items.length > 1)
    .map(([key, items]) => ({ key, items }));
}

const APPROVE_ALL = "approve_all";
const REVIEW_EACH = "review_each";
const HOLD_ALL = "decline_all";

/**
 * Several open authorizations of the same kind asked as one question:
 * approve them all (recommended), look at each, or hold them all. Each card
 * still resolves through its own path.
 */
export default function PendingBatchQuestion({ group, onDecideAll, onReviewEach, disabled }: {
  group: BatchGroup;
  onDecideAll: (decision: "approve" | "reject") => void;
  onReviewEach: () => void;
  disabled?: boolean;
}) {
  const labels: Record<string, string> = {
    [APPROVE_ALL]: t("recommendation.batch.approve_all"),
    [REVIEW_EACH]: t("recommendation.batch.review_each"),
    [HOLD_ALL]: t("recommendation.batch.decline_all"),
  };
  const title = t("recommendation.batch.title", { count: group.items.length });
  return (
    <div className="chat-hitl-batch" role="group" aria-label={title}>
      <p className="chat-hitl-batch__title">{title}</p>
      <ol className="chat-hitl-batch__items">
        {group.items.map((item) => <li key={item.id}>{item.summary}</li>)}
      </ol>
      <RecommendedOptions
        question={{ options: [APPROVE_ALL, REVIEW_EACH, HOLD_ALL], allow_other: false }}
        options={[APPROVE_ALL, REVIEW_EACH, HOLD_ALL]}
        labelFor={(option) => labels[option] || option}
        onChoose={(option) => {
          if (option === REVIEW_EACH) onReviewEach();
          else onDecideAll(option === APPROVE_ALL ? "approve" : "reject");
        }}
        disabled={disabled}
      />
    </div>
  );
}
