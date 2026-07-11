/**
 * InlineTips — a small, low-chrome hint shown inside empty states so users
 * learn what a surface can do. One tip is picked at random per mount; tips
 * that don't apply to the current context are filtered out first.
 */
import { useEffect, useMemo, useState } from "react";
import { t } from "../../lib/i18n";

const DISMISS_PREFIX = "manor_tip_dismissed:";
const COMPOSER_ROTATE_MS = 8000;

export type InlineTipsSurface = "general_chat" | "inbox" | "workspace_chat" | "task_comment";
export type InlineTipsPlacement = "composer" | "empty_state";

export interface InlineTipsContext {
  /** Workspace has at least one deployed agent (gates @mention tips). */
  hasAgents?: boolean;
}

interface TipDef {
  /** i18n key for the tip body. */
  key: string;
  /** Optional gate — tip is dropped when this returns false. */
  requires?: (ctx: InlineTipsContext) => boolean;
}

const TIPS: Record<InlineTipsSurface, TipDef[]> = {
  general_chat: [
    { key: "component.inline_tips.chat_mention" },
    { key: "component.inline_tips.chat_attach" },
    { key: "component.inline_tips.chat_skill" },
    { key: "component.inline_tips.chat_artifacts" },
    { key: "component.inline_tips.chat_mode" },
  ],
  workspace_chat: [
    { key: "component.inline_tips.ws_mention", requires: (c) => Boolean(c.hasAgents) },
    { key: "component.inline_tips.ws_feedback" },
    { key: "component.inline_tips.ws_tasks" },
    { key: "component.inline_tips.ws_assign" },
    { key: "component.inline_tips.ws_review" },
    { key: "component.inline_tips.ws_files" },
    { key: "component.inline_tips.ws_autorun" },
  ],
  inbox: [
    { key: "component.inline_tips.inbox_all" },
    { key: "component.inline_tips.inbox_reply" },
    { key: "component.inline_tips.inbox_unread" },
    { key: "component.inline_tips.inbox_search" },
  ],
  task_comment: [
    { key: "component.inline_tips.task_ai_resolve" },
    { key: "component.inline_tips.task_steer" },
    { key: "component.inline_tips.task_unblock" },
    { key: "component.inline_tips.task_discuss" },
    { key: "component.inline_tips.task_attach" },
    { key: "component.inline_tips.task_markdown" },
    { key: "component.inline_tips.task_send" },
  ],
};

export default function InlineTips({
  surface,
  context,
  placement = "empty_state",
  dismissible,
  autoRotateMs,
  className,
}: {
  surface: InlineTipsSurface;
  context?: InlineTipsContext;
  /** Placement controls behavior:
   *  - composer: persistent, dismissible, rotating tips above a chat composer
   *  - empty_state: one-off helper rendered only while the parent is empty */
  placement?: InlineTipsPlacement;
  /** Render a × that hides the tip for good (remembered in localStorage).
   *  Defaults to true for composer tips and false for empty-state tips. */
  dismissible?: boolean;
  /** When set, cycle to the next tip every N ms (paused on hover). Only
   *  meaningful for persistent placements. Defaults to 8s for composer tips. */
  autoRotateMs?: number;
  className?: string;
}) {
  const shouldDismiss = dismissible ?? placement === "composer";
  const rotateMs = autoRotateMs ?? (placement === "composer" ? COMPOSER_ROTATE_MS : undefined);
  const storageKey = `${DISMISS_PREFIX}${surface}:${placement}`;
  const [dismissed, setDismissed] = useState(() => {
    if (!shouldDismiss) return false;
    try {
      return window.localStorage.getItem(storageKey) === "1";
    } catch {
      return false;
    }
  });

  const hasAgents = context?.hasAgents;
  const pool = useMemo(
    () => TIPS[surface].filter((def) => !def.requires || def.requires({ hasAgents })),
    [surface, hasAgents],
  );

  // Random pick per visit; auto-rotation jumps to another random tip each
  // tick, never repeating the current one back-to-back.
  const [index, setIndex] = useState(() => Math.floor(Math.random() * 9973));
  const [paused, setPaused] = useState(false);

  useEffect(() => {
    if (!rotateMs || paused || dismissed || pool.length <= 1) return;
    const id = window.setInterval(() => {
      setIndex((prev) => {
        const len = pool.length;
        const cur = ((prev % len) + len) % len;
        let next = Math.floor(Math.random() * (len - 1));
        if (next >= cur) next += 1; // uniform over the other len-1 tips
        return next;
      });
    }, rotateMs);
    return () => window.clearInterval(id);
  }, [rotateMs, paused, dismissed, pool]);

  const tip = pool.length > 0 ? pool[index % pool.length] : null;

  if (dismissed || !tip) return null;

  const handleDismiss = () => {
    try {
      window.localStorage.setItem(storageKey, "1");
    } catch {
      // Private browsing / storage restrictions shouldn't break the UI.
    }
    setDismissed(true);
  };

  return (
    <div
      className={["inline-tip", `inline-tip--${placement}`, className].filter(Boolean).join(" ")}
      role="note"
      onMouseEnter={rotateMs ? () => setPaused(true) : undefined}
      onMouseLeave={rotateMs ? () => setPaused(false) : undefined}
    >
      <span className="inline-tip-fade" key={tip.key}>
        <svg
          className="inline-tip-icon"
          width="15"
          height="15"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth={1.7}
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="M12 18v-5.25m0 0a6.01 6.01 0 0 0 1.5-.189m-1.5.189a6.01 6.01 0 0 1-1.5-.189m3.75 7.478a12.06 12.06 0 0 1-4.5 0m3.75 2.383a14.406 14.406 0 0 1-3 0M14.25 18v-.192c0-.983.658-1.823 1.508-2.316a7.5 7.5 0 1 0-7.517 0c.85.493 1.509 1.333 1.509 2.316V18" />
        </svg>
        <span>
          <span className="inline-tip-label">{t("component.inline_tips.label")}</span>
          {" · "}
          {t(tip.key)}
        </span>
      </span>
      {shouldDismiss && (
        <button
          type="button"
          className="inline-tip-dismiss"
          aria-label={t("component.inline_tips.dismiss")}
          title={t("component.inline_tips.dismiss")}
          onClick={handleDismiss}
        >
          ×
        </button>
      )}
    </div>
  );
}
