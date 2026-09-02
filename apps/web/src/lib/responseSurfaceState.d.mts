import type { ResponseSurfaceSubmissionReceipt } from "./chatStream";

export const RESPONSE_SURFACE_GENERIC_PAYLOAD_LIMIT: number;
export const RESPONSE_SURFACE_CODE_LAB_PAYLOAD_LIMIT: number;
export const RESPONSE_SURFACE_DRAFT_PAYLOAD_LIMIT: number;

export function cloneBoundedResponseSurfacePayload(
  value: unknown,
  maximum: number,
): Record<string, unknown> | null;

export function latestUnseenResponseSurfaceReceipt(
  receipts: ResponseSurfaceSubmissionReceipt[],
  seenEventIds: Set<string>,
): ResponseSurfaceSubmissionReceipt | null;

export function responseSurfaceSubmissionFailureMessageIds(
  messages: Array<{
    id?: string | null;
    role?: string | null;
    author_kind?: string | null;
    meta?: Record<string, unknown> | null;
    stream_error?: boolean | null;
    stop_reason?: string | null;
  }>,
  receiptMessageIds: Set<string>,
): Set<string>;

export function responseSurfaceSubmissionOutcomes(
  messages: Array<{
    id?: string | null;
    role?: string | null;
    author_kind?: string | null;
    meta?: Record<string, unknown> | null;
    stream_error?: boolean | null;
    stop_reason?: string | null;
  }>,
  receiptMessageIds: Set<string>,
): {
  hiddenMessageIds: Set<string>;
  outcomesByReceiptMessageId: Map<string, "failed" | "interrupted">;
  statusesByReceiptMessageId: Map<
    string,
    "pending" | "succeeded" | "failed" | "interrupted"
  >;
};
