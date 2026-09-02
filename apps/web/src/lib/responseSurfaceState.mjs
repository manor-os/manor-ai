export const RESPONSE_SURFACE_GENERIC_PAYLOAD_LIMIT = 40_000;
export const RESPONSE_SURFACE_CODE_LAB_PAYLOAD_LIMIT = 200_000;
export const RESPONSE_SURFACE_DRAFT_PAYLOAD_LIMIT = 2_000_000;

export function cloneBoundedResponseSurfacePayload(value, maximum) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return null;
  try {
    const serialized = JSON.stringify(value);
    if (serialized.length > maximum) return null;
    return JSON.parse(serialized);
  } catch {
    return null;
  }
}

export function latestUnseenResponseSurfaceReceipt(receipts, seenEventIds) {
  const latest = receipts[receipts.length - 1] || null;
  const shouldHydrate = Boolean(latest && !seenEventIds.has(latest.eventId));
  receipts.forEach((receipt) => seenEventIds.add(receipt.eventId));
  return shouldHydrate ? latest : null;
}

export function responseSurfaceSubmissionFailureMessageIds(messages, receiptMessageIds) {
  return responseSurfaceSubmissionOutcomes(messages, receiptMessageIds).hiddenMessageIds;
}

export function responseSurfaceSubmissionOutcomes(messages, receiptMessageIds) {
  const hiddenMessageIds = new Set();
  const outcomesByReceiptMessageId = new Map();
  const statusesByReceiptMessageId = new Map();
  const statusPriority = {
    pending: 0,
    succeeded: 1,
    failed: 2,
    interrupted: 3,
  };
  for (const message of messages) {
    if (!message || typeof message !== "object") continue;
    const role = String(message.role || message.author_kind || "").toLowerCase();
    if (role !== "assistant" && role !== "agent") continue;
    const meta = message.meta && typeof message.meta === "object" && !Array.isArray(message.meta)
      ? message.meta
      : {};
    const originId = typeof meta.origin_user_message_id === "string"
      ? meta.origin_user_message_id
      : "";
    if (!originId || !receiptMessageIds.has(originId)) continue;
    const streamStatus = String(meta.stream_status || "").toLowerCase();
    const stopReason = String(meta.stop_reason || message.stop_reason || "").toLowerCase();
    const interrupted = meta.stream_interrupted === true
      || streamStatus === "interrupted";
    const failed = message.stream_error === true
      || meta.stream_error === true
      || Boolean(meta.error)
      || stopReason === "error"
      || stopReason === "credit_exhausted"
      || streamStatus === "error"
      || interrupted;
    const status = interrupted
      ? "interrupted"
      : failed
        ? "failed"
        : streamStatus === "running" || streamStatus === "streaming"
          ? "pending"
          : "succeeded";
    const priorStatus = statusesByReceiptMessageId.get(originId);
    if (!priorStatus || statusPriority[status] > statusPriority[priorStatus]) {
      statusesByReceiptMessageId.set(originId, status);
    }
    if (failed) {
      outcomesByReceiptMessageId.set(
        originId,
        interrupted ? "interrupted" : "failed",
      );
      if (typeof message.id === "string" && message.id) hiddenMessageIds.add(message.id);
    }
  }
  return { hiddenMessageIds, outcomesByReceiptMessageId, statusesByReceiptMessageId };
}
