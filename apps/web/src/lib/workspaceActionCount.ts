import type { Workspace } from "./types";

function positiveStat(value: unknown) {
  const count = Number(value || 0);
  return Number.isFinite(count) && count > 0 ? count : 0;
}

/** Canonical open decisions, regardless of personal read receipts. */
export function workspaceChatActionCount(ws: Pick<Workspace, "stats">) {
  const stats = ws.stats || {};
  const hasChatActionStats = [
    stats.chat_pending_actions,
    stats.proposal_actions,
    stats.failed_actions,
  ].some((value) => value !== undefined && value !== null);

  if (!hasChatActionStats) {
    return positiveStat(stats.pending_actions ?? stats.hitl_tasks ?? 0);
  }
  return (
    positiveStat(stats.chat_pending_actions) +
    positiveStat(stats.proposal_actions) +
    positiveStat(stats.failed_actions)
  );
}
