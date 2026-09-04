import { stripEditorLiveEditBlocks } from "./assistant-visible-text.mjs";

export { stripEditorLiveEditBlocks } from "./assistant-visible-text.mjs";

export const EDITOR_LIVE_CHAT_EVENT = "manor:open-editor-live-chat";
export const EDITOR_LIVE_CHAT_CLOSE_EVENT = "manor:close-editor-live-chat";
export const EDITOR_LIVE_CHAT_UPDATE_EVENT = "manor:update-editor-live-chat";

export enum AiEditTargetKind {
  Document = "document",
  Diagram = "diagram",
  Project = "project",
  Workflow = "workflow",
  Audio = "audio",
  Image = "image",
  Video = "video",
}

const AI_EDIT_TARGET_KINDS = new Set<AiEditTargetKind>(
  Object.values(AiEditTargetKind),
);

export enum AiEditApplyPhase {
  Preview = "preview",
  Complete = "complete",
}

export enum AiEditPatchStreamEventKind {
  Delta = "delta",
  Commit = "commit",
}

export enum AiEditPreviewStatus {
  Animating = "animating",
  Ready = "ready",
}

export enum AiEditSessionCleanupStatus {
  Idle = "idle",
  Closing = "closing",
  Failed = "failed",
}

export const EDITOR_LIVE_STREAMING_PREVIEW_MAX_CHARS = 100_000;

/** End pointer/focus mutations before an AI preview snapshots editor state. */
export function cancelActiveEditorInteractions(): void {
  if (typeof window === "undefined") return;
  if (typeof PointerEvent === "function") {
    window.dispatchEvent(new PointerEvent("pointercancel"));
  }
  if (typeof document !== "undefined" && document.activeElement instanceof HTMLElement) {
    document.activeElement.blur();
  }
}

/** React 18 typings omit native `inert`; spread this shared lock contract. */
export function aiEditInteractionLockProps(
  locked: boolean,
): Record<string, string | boolean> {
  return locked ? { inert: "", "aria-disabled": true } : {};
}

export type AiEditTarget = {
  kind: AiEditTargetKind;
  /** Stable resource identity. Labels and routes must never be used as identity. */
  id: string;
};

export type EditorLiveApplyMeta = {
  complete: boolean;
  phase: AiEditApplyPhase;
  source: "assistant-stream";
  /** Stable for one assistant response inside a longer AI Edit session. */
  turnId?: string;
  /** Reviewable operation count that existed before this assistant response. */
  turnBasePatchCount?: number;
  streamEvent?: AiEditPatchStreamEventKind;
  mode?: "patch";
  diff?: string;
  patch?: string;
  patchCount?: number;
  sourceLabel?: string;
  signal?: AbortSignal;
};

export function nextEditorLiveChangeCount(
  previous: { changeCount?: number } | null | undefined,
  meta?: EditorLiveApplyMeta,
) {
  const previousCount = previous?.changeCount || 0;
  if (typeof meta?.patchCount !== "number") return previousCount + 1;
  return Math.max(
    previousCount,
    Math.max(0, meta.turnBasePatchCount || 0) + Math.max(0, meta.patchCount),
  );
}

export function hasReviewableEditorLivePreview(
  state: Readonly<{ changeCount: number }> | null | undefined,
) {
  return Math.max(0, state?.changeCount || 0) > 0;
}

export function shouldStreamEditorLiveDeltaPreview(contentLength: number) {
  return Number.isFinite(contentLength)
    && contentLength >= 0
    && contentLength <= EDITOR_LIVE_STREAMING_PREVIEW_MAX_CHARS;
}

export function mergeEditorLivePreviewDiff(
  previousDiff: string | undefined,
  meta: EditorLiveApplyMeta,
) {
  if (!meta.diff) return previousDiff;
  if (
    (meta.turnBasePatchCount || 0) > 0
    && previousDiff
    && previousDiff !== meta.diff
  ) return `${previousDiff}\n\n${meta.diff}`;
  return meta.diff;
}

export type AiEditCommitCoordinator = Readonly<{
  run: <T>(operation: () => Promise<T>) => Promise<T>;
  isCommitting: () => boolean;
  waitForCommit: () => Promise<void>;
}>;

export type AiEditConversationDeleteCoordinator = Readonly<{
  run: (
    owner: AiEditConversationOwnerScope,
    conversationId: string,
    operation: () => Promise<boolean>,
  ) => Promise<boolean>;
}>;

export type AiEditConversationOwnerScope = Readonly<{
  userId: string;
  entityId: string;
}>;

export function aiEditConversationOwnerKey(owner: AiEditConversationOwnerScope) {
  return JSON.stringify([owner.userId, owner.entityId]);
}

export function isSameAiEditConversationOwner(
  left: AiEditConversationOwnerScope | null | undefined,
  right: AiEditConversationOwnerScope | null | undefined,
) {
  return Boolean(
    left
    && right
    && left.userId === right.userId
    && left.entityId === right.entityId,
  );
}

/** One delete flight owns a temporary AI Edit conversation at a time. */
export function createAiEditConversationDeleteCoordinator(): AiEditConversationDeleteCoordinator {
  const inFlight = new Map<string, Promise<boolean>>();
  const completed = new Set<string>();
  const completedOrder: string[] = [];
  const completedLimit = 256;

  const rememberCompleted = (deletionKey: string) => {
    if (completed.has(deletionKey)) return;
    completed.add(deletionKey);
    completedOrder.push(deletionKey);
    if (completedOrder.length <= completedLimit) return;
    const oldest = completedOrder.shift();
    if (oldest) completed.delete(oldest);
  };

  return Object.freeze({
    run: (owner, conversationId, operation) => {
      const deletionKey = JSON.stringify([owner.userId, owner.entityId, conversationId]);
      // Conversation ids are immutable. Once this page has observed a
      // successful idempotent teardown, a later close surface or queue flush
      // must not issue another DELETE for the same host-owned session.
      if (completed.has(deletionKey)) return Promise.resolve(true);
      const existing = inFlight.get(deletionKey);
      if (existing) return existing;
      const tracked = Promise.resolve()
        .then(operation)
        .then((deleted) => {
          if (deleted) rememberCompleted(deletionKey);
          return deleted;
        })
        .finally(() => {
          if (inFlight.get(deletionKey) === tracked) {
            inFlight.delete(deletionKey);
          }
        });
      inFlight.set(deletionKey, tracked);
      return tracked;
    },
  });
}

export type AiEditSessionCleanupCoordinator = Readonly<{
  run: (operation: () => Promise<boolean>) => Promise<boolean>;
  current: () => Promise<boolean> | null;
}>;

/** Every close surface joins the same rollback transaction. */
export function createAiEditSessionCleanupCoordinator(): AiEditSessionCleanupCoordinator {
  let inFlight: Promise<boolean> | null = null;

  return Object.freeze({
    run: (operation) => {
      if (inFlight) return inFlight;
      const tracked = Promise.resolve()
        .then(operation)
        .finally(() => {
          if (inFlight === tracked) inFlight = null;
        });
      inFlight = tracked;
      return tracked;
    },
    current: () => inFlight,
  });
}

/** One shared commit lock for an editor surface and its Chat adapter. */
export function createAiEditCommitCoordinator(): AiEditCommitCoordinator {
  let pending: Promise<void> | null = null;

  return Object.freeze({
    run: async <T>(operation: () => Promise<T>) => {
      if (pending) throw new Error("AI Edit is already accepting a preview.");
      const operationPromise = Promise.resolve().then(operation);
      const tracked = operationPromise.then(() => undefined, () => undefined);
      pending = tracked;
      try {
        return await operationPromise;
      } finally {
        if (pending === tracked) pending = null;
      }
    },
    isCommitting: () => pending !== null,
    waitForCommit: async () => {
      await pending;
    },
  });
}

export type EditorLiveAdapter = Readonly<{
  target: AiEditTarget;
  read: () => string;
  getTurnPreviewState: () => Readonly<{ changeCount: number }>;
  /** Lock any existing reviewable preview before the next model turn starts. */
  beginTurn: (
    meta: EditorLiveApplyMeta,
  ) => boolean | void | Promise<boolean | void>;
  preview: (
    content: string,
    meta: EditorLiveApplyMeta,
  ) => boolean | void | Promise<boolean | void>;
  complete: (
    content: string,
    meta: EditorLiveApplyMeta,
  ) => boolean | void | Promise<boolean | void>;
  /** Restore the content captured at the start of one turn without discarding the session. */
  restore: (
    content: string,
    meta: EditorLiveApplyMeta,
  ) => boolean | void | Promise<boolean | void>;
  rollback: () => void | Promise<void>;
  isCommitting: () => boolean;
  waitForCommit: () => Promise<void>;
}>;

export type EditorLiveAdapterConfig = {
  target: AiEditTarget;
  read: () => string;
  getTurnPreviewState: EditorLiveAdapter["getTurnPreviewState"];
  beginTurn: EditorLiveAdapter["beginTurn"];
  preview: EditorLiveAdapter["preview"];
  complete: EditorLiveAdapter["complete"];
  restore?: EditorLiveAdapter["restore"];
  rollback: EditorLiveAdapter["rollback"];
  commitCoordinator?: AiEditCommitCoordinator;
};

/**
 * One construction path for every AI Edit surface. A surface is only a valid
 * AI Edit target when it implements the complete preview transaction:
 * preview -> complete -> accept in the surface, or rollback on discard/close.
 */
export function createEditorLiveAdapter(
  config: EditorLiveAdapterConfig,
): EditorLiveAdapter {
  if (!AI_EDIT_TARGET_KINDS.has(config.target.kind)) {
    throw new Error("AI Edit requires a supported target kind.");
  }
  const id = config.target.id.trim();
  if (!id) throw new Error("AI Edit requires a stable target id.");
  if (
    typeof config.beginTurn !== "function"
    || typeof config.complete !== "function"
    || typeof config.rollback !== "function"
  ) {
    throw new Error("AI Edit requires begin, complete, and rollback lifecycle hooks.");
  }
  if (typeof config.getTurnPreviewState !== "function") {
    throw new Error("AI Edit requires a turn preview state hook.");
  }
  const commitCoordinator = config.commitCoordinator || createAiEditCommitCoordinator();
  return Object.freeze({
    target: Object.freeze({ ...config.target, id }),
    read: config.read,
    getTurnPreviewState: () => {
      const changeCount = config.getTurnPreviewState().changeCount;
      if (!Number.isFinite(changeCount)) {
        throw new Error("AI Edit turn preview state requires a finite change count.");
      }
      return { changeCount: Math.max(0, Math.floor(changeCount)) };
    },
    beginTurn: async (meta) => {
      await commitCoordinator.waitForCommit();
      if (meta.signal?.aborted) return false;
      return config.beginTurn(meta);
    },
    preview: async (content, meta) => {
      await commitCoordinator.waitForCommit();
      if (meta.signal?.aborted) return false;
      const previewed = await config.preview(content, meta);
      return meta.signal?.aborted ? false : previewed;
    },
    complete: async (content, meta) => {
      await commitCoordinator.waitForCommit();
      if (meta.signal?.aborted) return false;
      const completed = await config.complete(content, meta);
      return meta.signal?.aborted ? false : completed;
    },
    restore: async (content, meta) => {
      await commitCoordinator.waitForCommit();
      if (meta.signal?.aborted) return false;
      if (config.restore) return config.restore(content, meta);
      const previewed = await config.preview(content, {
        ...meta,
        complete: false,
        phase: AiEditApplyPhase.Preview,
      });
      if (previewed === false || meta.signal?.aborted) return false;
      return config.complete(content, {
        ...meta,
        complete: true,
        phase: AiEditApplyPhase.Complete,
      });
    },
    rollback: async () => {
      await commitCoordinator.waitForCommit();
      await config.rollback();
    },
    isCommitting: commitCoordinator.isCommitting,
    waitForCommit: commitCoordinator.waitForCommit,
  });
}

export type EditorLiveTurnMetadata = Pick<
  EditorLiveChatMetadata,
  "documentName" | "fileType" | "mimeType" | "editorType" | "sourcePath"
>;

export type EditorLiveChatMetadata = {
  documentId?: string | null;
  documentName?: string | null;
  fileType?: string | null;
  mimeType?: string | null;
  editorType?: string | null;
  sourcePath?: string | null;
  instruction?: string | null;
  sessionLabel?: string | null;
  emptyDescription?: string | null;
  placeholder?: string | null;
  examples?: string[];
  getAttachmentFiles?: () => File[] | Promise<File[]>;
  supportsImageGeneration?: boolean;
  applyGeneratedImage?: (
    imageUrl: string,
    meta: EditorLiveApplyMeta,
  ) => boolean | void | Promise<boolean | void>;
  /** Resolved after read() locks the target for the current streamed turn. */
  getTurnMetadata?: () => EditorLiveTurnMetadata;
};

export type EditorLiveChatDetail = EditorLiveChatMetadata & {
  adapter: EditorLiveAdapter;
};

/**
 * Attach the saved source only when the editor cannot already provide the
 * current bytes for the turn. This avoids sending a stale Knowledge snapshot
 * beside newer in-memory content.
 */
export function shouldAttachEditorLiveSourceDocument(
  detail: EditorLiveChatDetail,
) {
  if (!detail.documentId || !detail.documentName || detail.getAttachmentFiles) return false;
  if (
    detail.adapter.target.kind === AiEditTargetKind.Audio
    || detail.adapter.target.kind === AiEditTargetKind.Video
  ) return true;
  const fileType = String(detail.fileType || "").trim().toLowerCase();
  const mimeType = String(detail.mimeType || "").split(";", 1)[0].trim().toLowerCase();
  return fileType === "pdf"
    || mimeType === "application/pdf"
    || detail.documentName.toLowerCase().endsWith(".pdf");
}

export type EditorLivePatchOperation =
  | {
      op: "replace";
      find: string;
      replace: string;
      all?: boolean;
    }
  | {
      op: "delete";
      find: string;
      all?: boolean;
    }
  | {
      op: "insert_before" | "insert_after";
      find: string;
      text: string;
    }
  | {
      op: "prepend" | "append";
      text: string;
    };

export type EditorLivePatchResult = {
  content: string;
  applied: number;
  failed: Array<{ index: number; reason: string }>;
};

function documentReference(detail: EditorLiveChatMetadata) {
  if (detail.documentName && detail.documentId) return `#${detail.documentName}`;
  return detail.documentName || "the current document";
}

export function buildEditorLiveEditPrompt(detail: EditorLiveChatMetadata = {}) {
  const docRef = documentReference(detail);
  const firstLine =
    detail.instruction?.trim() || `Tell me what to change in ${docRef}.`;

  return firstLine;
}

export function buildEditorLiveEditRequest(
  detail: EditorLiveChatMetadata,
  userRequest: string,
  currentContent: string,
) {
  void detail;
  void currentContent;
  return userRequest.trim();
}

class EditorLivePatchValidationError extends Error {
  constructor(readonly index: number, message: string) {
    super(message);
  }
}

function parsePatchOperations(patchJson: string): EditorLivePatchOperation[] {
  const parsed = JSON.parse(patchJson);
  const operations = Array.isArray(parsed) ? parsed : [parsed];
  return operations.map((raw, index) => {
    if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
      throw new EditorLivePatchValidationError(index, "Patch operation must be an object.");
    }
    const operation = raw as Record<string, unknown>;
    if (typeof operation.op !== "string") {
      throw new EditorLivePatchValidationError(index, "Patch operation is missing op.");
    }
    if (
      operation.op === "append"
      || operation.op === "prepend"
    ) {
      if (typeof operation.text !== "string") {
        throw new EditorLivePatchValidationError(index, `${operation.op} operation is missing text.`);
      }
      return operation as EditorLivePatchOperation;
    }
    if (!["replace", "delete", "insert_before", "insert_after"].includes(operation.op)) {
      throw new EditorLivePatchValidationError(index, `Unsupported patch operation: ${operation.op}`);
    }
    if (typeof operation.find !== "string" || !operation.find) {
      throw new EditorLivePatchValidationError(index, "Patch operation is missing exact find text.");
    }
    if ("all" in operation && typeof operation.all !== "boolean") {
      throw new EditorLivePatchValidationError(index, "Patch operation all must be a boolean.");
    }
    if (operation.op === "replace" && typeof operation.replace !== "string") {
      throw new EditorLivePatchValidationError(index, "Replace operation is missing replacement text.");
    }
    if (
      (operation.op === "insert_before" || operation.op === "insert_after")
      && typeof operation.text !== "string"
    ) {
      throw new EditorLivePatchValidationError(index, "Insert operation is missing text.");
    }
    return operation as EditorLivePatchOperation;
  });
}

function firstFailure(index: number, reason: string) {
  return [{ index, reason }];
}

export function applyEditorLivePatch(
  currentContent: string,
  patchJson: string,
): EditorLivePatchResult {
  let operations: EditorLivePatchOperation[];
  try {
    operations = parsePatchOperations(patchJson);
  } catch (err) {
    const index = err instanceof EditorLivePatchValidationError ? err.index : 0;
    return {
      content: currentContent,
      applied: 0,
      failed: firstFailure(index, `Invalid patch: ${(err as Error).message}`),
    };
  }

  if (operations.length === 0) {
    return {
      content: currentContent,
      applied: 0,
      failed: firstFailure(0, "Patch did not include any operations."),
    };
  }

  let draft = currentContent;
  for (let index = 0; index < operations.length; index += 1) {
    const operation = operations[index]!;
    if (operation.op === "append") {
      if (typeof operation.text !== "string") {
        return {
          content: currentContent,
          applied: index,
          failed: firstFailure(index, "Append operation is missing text."),
        };
      }
      draft += operation.text;
      continue;
    }

    if (operation.op === "prepend") {
      if (typeof operation.text !== "string") {
        return {
          content: currentContent,
          applied: index,
          failed: firstFailure(index, "Prepend operation is missing text."),
        };
      }
      draft = operation.text + draft;
      continue;
    }

    if (!("find" in operation) || typeof operation.find !== "string" || !operation.find) {
      return {
        content: currentContent,
        applied: index,
        failed: firstFailure(index, "Patch operation is missing exact find text."),
      };
    }

    const at = draft.indexOf(operation.find);
    if (at < 0) {
      return {
        content: currentContent,
        applied: index,
        failed: firstFailure(index, "Find text was not present in the current document."),
      };
    }

    if (operation.op === "delete") {
      draft = operation.all
        ? draft.split(operation.find).join("")
        : draft.slice(0, at) + draft.slice(at + operation.find.length);
      continue;
    }

    if (operation.op === "replace") {
      if (typeof operation.replace !== "string") {
        return {
          content: currentContent,
          applied: index,
          failed: firstFailure(index, "Replace operation is missing replacement text."),
        };
      }
      draft = operation.all
        ? draft.split(operation.find).join(operation.replace)
        : draft.slice(0, at) + operation.replace + draft.slice(at + operation.find.length);
      continue;
    }

    if (operation.op === "insert_before" || operation.op === "insert_after") {
      if (typeof operation.text !== "string") {
        return {
          content: currentContent,
          applied: index,
          failed: firstFailure(index, "Insert operation is missing text."),
        };
      }
      const insertAt =
        operation.op === "insert_before" ? at : at + operation.find.length;
      draft = draft.slice(0, insertAt) + operation.text + draft.slice(insertAt);
      continue;
    }

    return {
      content: currentContent,
      applied: index,
      failed: firstFailure(index, `Unsupported patch operation: ${(operation as any).op}`),
    };
  }

  return {
    content: draft,
    applied: operations.length,
    failed: [],
  };
}

function trimPatchPayload(value: string) {
  let next = value;
  if (next.startsWith("\r\n")) next = next.slice(2);
  else if (next.startsWith("\n")) next = next.slice(1);
  if (next.endsWith("\r\n")) next = next.slice(0, -2);
  else if (next.endsWith("\n")) next = next.slice(0, -1);
  return next;
}

export function extractEditorLivePatchPayload(text: string) {
  return extractEditorLivePatchPayloads(text)[0] || null;
}

export function extractEditorLivePatchPayloads(text: string) {
  const payloads: string[] = [];
  const re = /<manor-live-patch(?:\s[^>]*)?>([\s\S]*?)<\/manor-live-patch>/gi;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text))) {
    payloads.push(trimPatchPayload(match[1] || ""));
  }
  return payloads;
}

const EDITOR_LIVE_PATCH_OPEN = "<manor-live-patch";
const EDITOR_LIVE_PATCH_CLOSE = "</manor-live-patch>";

const EDITOR_LIVE_HIDDEN_TAG_PREFIXES = [
  "<manor-live-patch",
  "</manor-live-patch",
  "<manor-live-edit",
  "</manor-live-edit",
  "<manor live patch",
  "</manor live patch",
  "<manor live edit",
  "</manor live edit",
] as const;

const EDITOR_LIVE_HIDDEN_CLOSE_TAG_PREFIXES = [
  "</manor-live-patch",
  "</manor-live-edit",
  "</manor live patch",
  "</manor live edit",
] as const;

const EDITOR_LIVE_HIDDEN_OPEN_TAG_RE =
  /^<manor(?:-|\s+)live(?:-|\s+)(?:patch|edit)(?:\s[^>]*)?>$/i;
const EDITOR_LIVE_HIDDEN_CLOSE_TAG_RE =
  /^<\/manor(?:-|\s+)live(?:-|\s+)(?:patch|edit)(?:\s[^>]*)?>$/i;

enum EditorLiveVisibleProjectionState {
  Visible = "visible",
  Hidden = "hidden",
}

export type EditorLiveVisibleTextProjector = Readonly<{
  push: (chunk: string) => string;
  flush: () => string;
  reset: () => void;
}>;

export type EditorLiveChatSseProjector = Readonly<{
  projectLine: (rawLine: string) => string;
  flush: () => string;
}>;

function isPotentialEditorLiveTagFragment(
  fragment: string,
  state: EditorLiveVisibleProjectionState,
) {
  if (!fragment.startsWith("<") || fragment.includes(">")) return false;
  const normalized = fragment.toLowerCase().replace(/\s+/g, " ");
  const prefixes = state === EditorLiveVisibleProjectionState.Hidden
    ? EDITOR_LIVE_HIDDEN_CLOSE_TAG_PREFIXES
    : EDITOR_LIVE_HIDDEN_TAG_PREFIXES;
  return prefixes.some(
    (prefix) => prefix.startsWith(normalized)
      || (
        normalized.startsWith(prefix)
        && (normalized.length === prefix.length || normalized[prefix.length] === " ")
      ),
  );
}

/**
 * Project one model response into the text Chat may type for the user.
 *
 * The editor branch still consumes the complete protocol. This projector is a
 * bounded incremental state machine: fragmented tags never leak, hidden patch
 * JSON never enters the Chat typewriter queue, and each input character is
 * inspected once instead of repeatedly rescanning the complete response.
 */
export function createEditorLiveVisibleTextProjector(): EditorLiveVisibleTextProjector {
  let state = EditorLiveVisibleProjectionState.Visible;
  let tagCandidate = "";
  let hasVisibleText = false;
  let pendingWhitespace = "";

  const appendVisible = (value: string) => {
    let projected = "";
    for (const char of value) {
      if (/\s/.test(char)) {
        if (hasVisibleText) pendingWhitespace += char;
        continue;
      }
      projected += pendingWhitespace + char;
      pendingWhitespace = "";
      hasVisibleText = true;
    }
    return projected;
  };

  const push = (chunk: string) => {
    let projected = "";
    for (const char of chunk) {
      if (!tagCandidate) {
        if (char === "<") {
          tagCandidate = char;
        } else if (state === EditorLiveVisibleProjectionState.Visible) {
          projected += appendVisible(char);
        }
        continue;
      }

      tagCandidate += char;
      if (char === ">") {
        if (state === EditorLiveVisibleProjectionState.Visible) {
          if (EDITOR_LIVE_HIDDEN_OPEN_TAG_RE.test(tagCandidate)) {
            state = EditorLiveVisibleProjectionState.Hidden;
          } else if (!EDITOR_LIVE_HIDDEN_CLOSE_TAG_RE.test(tagCandidate)) {
            projected += appendVisible(tagCandidate);
          }
        } else if (EDITOR_LIVE_HIDDEN_CLOSE_TAG_RE.test(tagCandidate)) {
          state = EditorLiveVisibleProjectionState.Visible;
        }
        tagCandidate = "";
        continue;
      }

      if (!isPotentialEditorLiveTagFragment(tagCandidate, state)) {
        if (state === EditorLiveVisibleProjectionState.Visible) {
          const visibleCandidate = char === "<" ? tagCandidate.slice(0, -1) : tagCandidate;
          projected += appendVisible(visibleCandidate);
        }
        tagCandidate = char === "<" ? "<" : "";
      }
    }
    return projected;
  };

  return Object.freeze({
    push,
    flush: () => {
      let projected = "";
      if (state === EditorLiveVisibleProjectionState.Visible && tagCandidate) {
        const normalized = tagCandidate.toLowerCase();
        const isHiddenProtocolFragment = (
          normalized.startsWith("<manor") || normalized.startsWith("</manor")
        ) && isPotentialEditorLiveTagFragment(
          tagCandidate,
          EditorLiveVisibleProjectionState.Visible,
        );
        if (!isHiddenProtocolFragment) projected = appendVisible(tagCandidate);
      }
      tagCandidate = "";
      pendingWhitespace = "";
      return projected;
    },
    reset: () => {
      state = EditorLiveVisibleProjectionState.Visible;
      tagCandidate = "";
      hasVisibleText = false;
      pendingWhitespace = "";
    },
  });
}

/**
 * Build the Chat-side SSE projection for an AI Edit turn. The source stream is
 * left intact for the editor tee; only user-visible text fields are projected.
 */
export function createEditorLiveChatSseProjector(): EditorLiveChatSseProjector {
  const visibleText = createEditorLiveVisibleTextProjector();
  let currentEvent = "";
  let projectionFlushed = false;

  const flush = () => {
    if (projectionFlushed) return "";
    projectionFlushed = true;
    const textDelta = visibleText.flush();
    if (!textDelta) return "";
    return `event: text_delta\ndata: ${JSON.stringify({ text_delta: textDelta })}\n\n`;
  };

  const projectLine = (rawLine: string) => {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (line.startsWith("event: ")) {
      const nextEvent = line.slice(7).trim();
      const pendingText = nextEvent === "stream_end" ? flush() : "";
      if (nextEvent === "text_reset" || nextEvent === "summary_start") {
        visibleText.reset();
        projectionFlushed = false;
      }
      currentEvent = nextEvent;
      return `${pendingText}${rawLine}`;
    }
    if (!line.startsWith("data: ")) return rawLine;

    const rawData = line.slice(6).trim();
    if (rawData === "[DONE]") return `${flush()}${rawLine}`;
    if (
      !rawData
      || currentEvent === "error"
      || currentEvent === "text_reset"
      || currentEvent === "summary_start"
    ) {
      return rawLine;
    }

    try {
      const parsed = JSON.parse(rawData);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return rawLine;
      const textKey = (["text_delta", "token", "content"] as const).find(
        (key) => parsed[key] != null,
      );
      if (!textKey) return rawLine;
      const token = parsed[textKey];
      if (
        typeof token !== "string"
        && typeof token !== "number"
        && typeof token !== "boolean"
      ) return rawLine;
      parsed[textKey] = visibleText.push(String(token));
      return `data: ${JSON.stringify(parsed)}`;
    } catch {
      return rawLine;
    }
  };

  return Object.freeze({ projectLine, flush });
}

export function containsEditorLivePatchProtocol(text: string) {
  const lower = text.toLowerCase();
  let searchFrom = 0;
  while (searchFrom < lower.length) {
    const start = lower.indexOf("<", searchFrom);
    if (start < 0) return false;
    const suffix = lower.slice(start);
    if (
      suffix.startsWith(EDITOR_LIVE_PATCH_OPEN)
      || suffix.startsWith(EDITOR_LIVE_PATCH_CLOSE)
      || EDITOR_LIVE_PATCH_OPEN.startsWith(suffix)
      || EDITOR_LIVE_PATCH_CLOSE.startsWith(suffix)
    ) return true;
    searchFrom = start + 1;
  }
  return false;
}

/**
 * Return the number of canonical operations only after every patch tag has a
 * complete, syntactically valid JSON object or array-of-objects payload.
 * Streaming callers use this at EOF before making a temporary preview
 * reviewable; `null` means the final protocol must be rolled back.
 */
export function countCompleteEditorLivePatchOperations(text: string) {
  const lower = text.toLowerCase();
  let openTag = false;
  let completeTagCount = 0;
  let searchFrom = 0;
  while (searchFrom < lower.length) {
    const start = lower.indexOf("<", searchFrom);
    if (start < 0) break;
    const suffix = lower.slice(start);

    if (suffix.startsWith(EDITOR_LIVE_PATCH_CLOSE)) {
      if (!openTag) return null;
      openTag = false;
      completeTagCount += 1;
      searchFrom = start + EDITOR_LIVE_PATCH_CLOSE.length;
      continue;
    }

    if (suffix.startsWith(EDITOR_LIVE_PATCH_OPEN)) {
      const characterAfterName = lower[start + EDITOR_LIVE_PATCH_OPEN.length];
      if (characterAfterName === undefined) return null;
      if (characterAfterName !== ">" && !/\s/.test(characterAfterName || "")) {
        return null;
      }
      const tagEnd = lower.indexOf(">", start + EDITOR_LIVE_PATCH_OPEN.length);
      if (tagEnd < 0) return null;
      if (openTag) return null;
      openTag = true;
      searchFrom = tagEnd + 1;
      continue;
    }

    if (
      EDITOR_LIVE_PATCH_OPEN.startsWith(suffix)
      || EDITOR_LIVE_PATCH_CLOSE.startsWith(suffix)
    ) return null;

    searchFrom = start + 1;
  }
  if (openTag || completeTagCount === 0) return null;

  const payloads = extractEditorLivePatchPayloads(text);
  if (payloads.length !== completeTagCount) return null;

  let operationCount = 0;
  for (const payload of payloads) {
    let parsed: unknown;
    try {
      parsed = JSON.parse(payload);
    } catch {
      return null;
    }
    const operations = Array.isArray(parsed) ? parsed : [parsed];
    if (
      operations.some(
        (operation) =>
          !operation
          || typeof operation !== "object"
          || Array.isArray(operation),
      )
    ) return null;
    operationCount += operations.length;
  }
  return operationCount;
}

function completeJsonObjectEnd(payload: string, objectStart: number) {
  let objectDepth = 0;
  let inString = false;
  let escaped = false;
  for (let index = objectStart; index < payload.length; index += 1) {
    const char = payload[index]!;
    if (inString) {
      if (escaped) escaped = false;
      else if (char === "\\") escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') {
      inString = true;
      continue;
    }
    if (char === "{") {
      objectDepth += 1;
      continue;
    }
    if (char !== "}" || objectDepth === 0) continue;
    objectDepth -= 1;
    if (objectDepth === 0) return index;
  }
  return -1;
}

function extractCompletePatchOperations(payload: string) {
  const operations: string[] = [];
  const firstValue = payload.search(/\S/);
  if (firstValue < 0) return operations;
  const arrayPayload = payload[firstValue] === "[";
  if (!arrayPayload && payload[firstValue] !== "{") return operations;

  let cursor = firstValue + (arrayPayload ? 1 : 0);
  while (cursor < payload.length) {
    while (/\s/.test(payload[cursor] || "")) cursor += 1;
    if (cursor >= payload.length || (arrayPayload && payload[cursor] === "]")) break;
    if (payload[cursor] !== "{") break;

    const objectEnd = completeJsonObjectEnd(payload, cursor);
    if (objectEnd < 0) break;
    operations.push(payload.slice(cursor, objectEnd + 1));
    if (!arrayPayload) break;

    cursor = objectEnd + 1;
    while (/\s/.test(payload[cursor] || "")) cursor += 1;
    if (cursor >= payload.length || payload[cursor] === "]") break;
    if (payload[cursor] !== ",") break;
    cursor += 1;
  }
  return operations;
}

function withoutTrailingHighSurrogate(value: string) {
  if (!value) return value;
  const last = value.charCodeAt(value.length - 1);
  return last >= 0xd800 && last <= 0xdbff ? value.slice(0, -1) : value;
}

type PartialPatchField = Readonly<{
  value: string;
  complete: boolean;
}>;

function buildPartialPatchOperation(
  stringFields: Readonly<Record<string, PartialPatchField>>,
  booleanFields: Readonly<Record<string, boolean>>,
) {
  const op = stringFields.op?.complete ? stringFields.op.value : "";
  const all = booleanFields.all;
  if (op === "append" || op === "prepend") {
    if (!("text" in stringFields)) return null;
    return JSON.stringify({ op, text: stringFields.text!.value });
  }
  const find = stringFields.find?.complete ? stringFields.find.value : "";
  if (!find) return null;
  if (op === "replace") {
    if (!("replace" in stringFields)) return null;
    return JSON.stringify({
      op,
      find,
      replace: stringFields.replace!.value,
      ...(all === undefined ? {} : { all }),
    });
  }
  if (op === "insert_before" || op === "insert_after") {
    if (!("text" in stringFields)) return null;
    return JSON.stringify({ op, find, text: stringFields.text!.value });
  }
  return null;
}

enum StreamingPatchObjectState {
  ExpectKey = "expect_key",
  Key = "key",
  AfterKey = "after_key",
  ExpectValue = "expect_value",
  StringValue = "string_value",
  BooleanValue = "boolean_value",
  AfterValue = "after_value",
  Complete = "complete",
  Invalid = "invalid",
}

type StreamingPatchObject = Readonly<{
  push: (char: string) => void;
  isComplete: () => boolean;
  isInvalid: () => boolean;
  payload: () => string;
  partialPatch: () => string | null;
}>;

/** Parse one flat patch object once as its characters arrive. */
function createStreamingPatchObject(): StreamingPatchObject {
  const raw = ["{"];
  const stringFields = Object.create(null) as Record<string, PartialPatchField>;
  const booleanFields = Object.create(null) as Record<string, boolean>;
  const simpleEscapes: Readonly<Record<string, string>> = Object.freeze({
    '"': '"',
    "\\": "\\",
    "/": "/",
    b: "\b",
    f: "\f",
    n: "\n",
    r: "\r",
    t: "\t",
  });
  let state = StreamingPatchObjectState.ExpectKey;
  let currentKey = "";
  let stringValue = "";
  let escaped = false;
  let unicodeDigits: string | null = null;
  let booleanToken = "";

  const updateStreamingField = () => {
    if (state !== StreamingPatchObjectState.StringValue) return;
    stringFields[currentKey] = {
      value: withoutTrailingHighSurrogate(stringValue),
      complete: false,
    };
  };

  const appendStringValue = (value: string) => {
    stringValue += value;
    updateStreamingField();
  };

  const pushStringCharacter = (char: string) => {
    if (unicodeDigits !== null) {
      if (!/[0-9a-f]/i.test(char)) {
        state = StreamingPatchObjectState.Invalid;
        return;
      }
      unicodeDigits += char;
      if (unicodeDigits.length === 4) {
        appendStringValue(String.fromCharCode(Number.parseInt(unicodeDigits, 16)));
        unicodeDigits = null;
        escaped = false;
      }
      return;
    }
    if (escaped) {
      if (char === "u") {
        unicodeDigits = "";
        return;
      }
      const decoded = simpleEscapes[char];
      if (decoded === undefined) {
        state = StreamingPatchObjectState.Invalid;
        return;
      }
      appendStringValue(decoded);
      escaped = false;
      return;
    }
    if (char === "\\") {
      escaped = true;
      return;
    }
    if (char === '"') {
      if (state === StreamingPatchObjectState.Key) {
        currentKey = stringValue;
        state = StreamingPatchObjectState.AfterKey;
      } else {
        stringFields[currentKey] = { value: stringValue, complete: true };
        state = StreamingPatchObjectState.AfterValue;
      }
      stringValue = "";
      return;
    }
    if (char.charCodeAt(0) < 0x20) {
      state = StreamingPatchObjectState.Invalid;
      return;
    }
    appendStringValue(char);
  };

  return Object.freeze({
    push: (char: string) => {
      if (
        state === StreamingPatchObjectState.Complete
        || state === StreamingPatchObjectState.Invalid
      ) return;
      raw.push(char);
      if (
        state === StreamingPatchObjectState.Key
        || state === StreamingPatchObjectState.StringValue
      ) {
        pushStringCharacter(char);
        return;
      }
      if (/\s/.test(char)) return;
      if (state === StreamingPatchObjectState.ExpectKey) {
        if (char === '"') {
          stringValue = "";
          escaped = false;
          unicodeDigits = null;
          state = StreamingPatchObjectState.Key;
        } else if (char === "}") {
          state = StreamingPatchObjectState.Complete;
        } else {
          state = StreamingPatchObjectState.Invalid;
        }
        return;
      }
      if (state === StreamingPatchObjectState.AfterKey) {
        state = char === ":"
          ? StreamingPatchObjectState.ExpectValue
          : StreamingPatchObjectState.Invalid;
        return;
      }
      if (state === StreamingPatchObjectState.ExpectValue) {
        if (char === '"') {
          stringValue = "";
          escaped = false;
          unicodeDigits = null;
          stringFields[currentKey] = { value: "", complete: false };
          state = StreamingPatchObjectState.StringValue;
        } else if (char === "t" || char === "f") {
          booleanToken = char;
          state = StreamingPatchObjectState.BooleanValue;
        } else {
          state = StreamingPatchObjectState.Invalid;
        }
        return;
      }
      if (state === StreamingPatchObjectState.BooleanValue) {
        const expected = booleanToken[0] === "t" ? "true" : "false";
        booleanToken += char;
        if (!expected.startsWith(booleanToken)) {
          state = StreamingPatchObjectState.Invalid;
        } else if (booleanToken === expected) {
          booleanFields[currentKey] = expected === "true";
          state = StreamingPatchObjectState.AfterValue;
        }
        return;
      }
      if (state === StreamingPatchObjectState.AfterValue) {
        if (char === ",") state = StreamingPatchObjectState.ExpectKey;
        else if (char === "}") state = StreamingPatchObjectState.Complete;
        else state = StreamingPatchObjectState.Invalid;
      }
    },
    isComplete: () => state === StreamingPatchObjectState.Complete,
    isInvalid: () => state === StreamingPatchObjectState.Invalid,
    payload: () => raw.join(""),
    partialPatch: () => buildPartialPatchOperation(stringFields, booleanFields),
  });
}

/**
 * Extract every complete JSON operation, including operations whose enclosing
 * array or manor-live-patch tag is still streaming. Text outside the canonical
 * protocol tag is never executable.
 */
export function extractEditorLivePatchOperationPayloads(text: string) {
  const operations: string[] = [];
  const openTag = /<manor-live-patch(?:\s[^>]*)?>/gi;
  const lowerText = text.toLowerCase();
  const closeTag = "</manor-live-patch>";
  let match: RegExpExecArray | null;
  while ((match = openTag.exec(text))) {
    const payloadStart = openTag.lastIndex;
    const closeIndex = lowerText.indexOf(closeTag, payloadStart);
    const payload = text.slice(payloadStart, closeIndex < 0 ? text.length : closeIndex);
    operations.push(...extractCompletePatchOperations(payload));
    if (closeIndex < 0) break;
    openTag.lastIndex = closeIndex + closeTag.length;
  }
  return operations;
}

/** Only a fully closed and valid protocol may be replayed after stream recovery. */
export function extractRecoverableEditorLivePatchOperationPayloads(text: string) {
  const completeOperationCount = countCompleteEditorLivePatchOperations(text);
  if (completeOperationCount === null) return null;
  const operations = extractEditorLivePatchOperationPayloads(text);
  return operations.length === completeOperationCount ? operations : null;
}

export type EditorLivePatchStreamEvent = Readonly<{
  kind: AiEditPatchStreamEventKind;
  operationIndex: number;
  patch: string;
}>;

/** A validated Commit promotes, but does not repaint, its already-visible Delta. */
export function isEditorLivePatchCommitAlreadyPreviewed(
  commitOperationIndex: number,
  lastDeltaOperationIndex: number | null,
  committedContent: string,
  lastPreviewContent: string,
) {
  return commitOperationIndex === lastDeltaOperationIndex
    && committedContent === lastPreviewContent;
}

export type EditorLivePatchStream = Readonly<{
  push: (chunk: string) => EditorLivePatchStreamEvent[];
  operationCount: () => number;
  reset: () => void;
}>;

enum StreamingPatchPayloadState {
  Outside = "outside",
  Value = "value",
  Separator = "separator",
  Done = "done",
  Invalid = "invalid",
}

/** One cursor-owning parser binds temporary deltas and validated commits to one response. */
export function createEditorLivePatchStream(): EditorLivePatchStream {
  let buffer = "";
  let payloadState = StreamingPatchPayloadState.Outside;
  let payloadContainer: "unknown" | "array" | "single" = "unknown";
  let currentObject: StreamingPatchObject | null = null;
  let emittedOperationCount = 0;
  let lastDeltaKey = "";

  const resetPayload = () => {
    payloadState = StreamingPatchPayloadState.Value;
    payloadContainer = "unknown";
    currentObject = null;
    lastDeltaKey = "";
  };

  const reset = () => {
    buffer = "";
    payloadState = StreamingPatchPayloadState.Outside;
    payloadContainer = "unknown";
    currentObject = null;
    emittedOperationCount = 0;
    lastDeltaKey = "";
  };

  return Object.freeze({
    push: (chunk: string) => {
      if (chunk) buffer += chunk;
      const lower = buffer.toLowerCase();
      const events: EditorLivePatchStreamEvent[] = [];
      let cursor = 0;

      while (cursor < buffer.length) {
        if (payloadState === StreamingPatchPayloadState.Outside) {
          const tagStart = lower.indexOf("<", cursor);
          if (tagStart < 0) {
            cursor = buffer.length;
            break;
          }
          const suffix = lower.slice(tagStart);
          if (EDITOR_LIVE_PATCH_OPEN.startsWith(suffix)) {
            cursor = tagStart;
            break;
          }
          if (!suffix.startsWith(EDITOR_LIVE_PATCH_OPEN)) {
            cursor = tagStart + 1;
            continue;
          }
          const characterAfterName = lower[tagStart + EDITOR_LIVE_PATCH_OPEN.length];
          if (characterAfterName === undefined) {
            cursor = tagStart;
            break;
          }
          if (characterAfterName !== ">" && !/\s/.test(characterAfterName)) {
            cursor = tagStart + 1;
            continue;
          }
          const tagEnd = lower.indexOf(">", tagStart + EDITOR_LIVE_PATCH_OPEN.length);
          if (tagEnd < 0) {
            cursor = tagStart;
            break;
          }
          cursor = tagEnd + 1;
          resetPayload();
          continue;
        }

        if (currentObject) {
          currentObject.push(buffer[cursor]!);
          cursor += 1;
          if (currentObject.isInvalid()) {
            currentObject = null;
            payloadState = StreamingPatchPayloadState.Invalid;
            continue;
          }
          if (currentObject.isComplete()) {
            events.push({
              kind: AiEditPatchStreamEventKind.Commit,
              operationIndex: emittedOperationCount,
              patch: currentObject.payload(),
            });
            emittedOperationCount += 1;
            currentObject = null;
            payloadState = payloadContainer === "array"
              ? StreamingPatchPayloadState.Separator
              : StreamingPatchPayloadState.Done;
          }
          continue;
        }

        const suffix = lower.slice(cursor);
        if (
          suffix.length < EDITOR_LIVE_PATCH_CLOSE.length
          && EDITOR_LIVE_PATCH_CLOSE.startsWith(suffix)
        ) break;
        if (suffix.startsWith(EDITOR_LIVE_PATCH_CLOSE)) {
          cursor += EDITOR_LIVE_PATCH_CLOSE.length;
          payloadState = StreamingPatchPayloadState.Outside;
          payloadContainer = "unknown";
          lastDeltaKey = "";
          continue;
        }

        const char = buffer[cursor]!;
        if (/\s/.test(char)) {
          cursor += 1;
          continue;
        }
        if (payloadState === StreamingPatchPayloadState.Invalid) {
          cursor += 1;
          continue;
        }
        if (payloadContainer === "unknown") {
          if (char === "[") {
            payloadContainer = "array";
            cursor += 1;
          } else if (char === "{") {
            payloadContainer = "single";
            currentObject = createStreamingPatchObject();
            cursor += 1;
          } else {
            payloadState = StreamingPatchPayloadState.Invalid;
            cursor += 1;
          }
          continue;
        }
        if (payloadState === StreamingPatchPayloadState.Value) {
          if (char === "{") {
            currentObject = createStreamingPatchObject();
          } else if (payloadContainer === "array" && char === "]") {
            payloadState = StreamingPatchPayloadState.Done;
          } else {
            payloadState = StreamingPatchPayloadState.Invalid;
          }
          cursor += 1;
          continue;
        }
        if (payloadState === StreamingPatchPayloadState.Separator) {
          if (char === ",") payloadState = StreamingPatchPayloadState.Value;
          else if (char === "]") payloadState = StreamingPatchPayloadState.Done;
          else payloadState = StreamingPatchPayloadState.Invalid;
          cursor += 1;
          continue;
        }
        payloadState = StreamingPatchPayloadState.Invalid;
        cursor += 1;
      }

      buffer = buffer.slice(cursor);
      const partialPatch = currentObject?.partialPatch() || null;
      const deltaKey = partialPatch
        ? `${emittedOperationCount}:${partialPatch}`
        : "";
      if (partialPatch && deltaKey !== lastDeltaKey) {
        events.push({
          kind: AiEditPatchStreamEventKind.Delta,
          operationIndex: emittedOperationCount,
          patch: partialPatch,
        });
      }
      lastDeltaKey = deltaKey;
      return events;
    },
    operationCount: () => emittedOperationCount,
    reset,
  });
}

export type EditorLivePatchDeltaQueue = Readonly<{
  enqueue: (event: EditorLivePatchStreamEvent) => void;
  flush: () => Promise<void>;
  reset: () => Promise<void>;
  cancel: () => Promise<void>;
}>;

type EditorLiveFrameScheduler = (callback: (time: number) => void) => number;
type EditorLiveFrameCanceller = (handle: number) => void;

/** Coalesce model-token deltas to one latest preview per browser paint. */
export function createEditorLivePatchDeltaQueue(
  apply: (event: EditorLivePatchStreamEvent) => void | Promise<void>,
  scheduleFrame: EditorLiveFrameScheduler = (callback) => window.requestAnimationFrame(callback),
  cancelFrame: EditorLiveFrameCanceller = (handle) => window.cancelAnimationFrame(handle),
): EditorLivePatchDeltaQueue {
  let pending: EditorLivePatchStreamEvent | null = null;
  let frameHandle: number | null = null;
  let applying: Promise<void> | null = null;
  let flushing = false;
  let cancelled = false;

  const applyEvent = (event: EditorLivePatchStreamEvent) => {
    const task = Promise.resolve()
      .then(() => apply(event))
      .then(() => undefined, () => undefined);
    applying = task;
    void task.finally(() => {
      if (applying === task) applying = null;
      if (!flushing && !cancelled && pending && frameHandle === null) schedulePending();
    });
    return task;
  };

  const schedulePending = () => {
    if (cancelled || flushing || applying || !pending || frameHandle !== null) return;
    frameHandle = scheduleFrame(() => {
      frameHandle = null;
      const event = pending;
      pending = null;
      if (event && !cancelled) applyEvent(event);
    });
  };

  return Object.freeze({
    enqueue: (event) => {
      if (cancelled) return;
      pending = event;
      schedulePending();
    },
    flush: async () => {
      if (cancelled) return;
      flushing = true;
      if (frameHandle !== null) {
        cancelFrame(frameHandle);
        frameHandle = null;
      }
      if (applying) await applying;
      while (pending && !cancelled) {
        const event = pending;
        pending = null;
        await applyEvent(event);
      }
      flushing = false;
      schedulePending();
    },
    reset: async () => {
      if (cancelled) return;
      flushing = true;
      pending = null;
      if (frameHandle !== null) {
        cancelFrame(frameHandle);
        frameHandle = null;
      }
      if (applying) await applying;
      flushing = false;
    },
    cancel: async () => {
      cancelled = true;
      pending = null;
      if (frameHandle !== null) {
        cancelFrame(frameHandle);
        frameHandle = null;
      }
      if (applying) await applying;
    },
  });
}

/** Reopening AI Edit for the same target should preserve its conversation. */
export function isSameEditorLiveTarget(
  current: EditorLiveChatDetail | null | undefined,
  next: EditorLiveChatDetail | null | undefined,
) {
  if (!current || !next) return false;
  return current.adapter.target.kind === next.adapter.target.kind
    && current.adapter.target.id === next.adapter.target.id;
}

export function openEditorLiveChat(detail: EditorLiveChatDetail) {
  if (typeof window === "undefined") return;
  const liveEditDetail: EditorLiveChatDetail = {
    ...detail,
    sourcePath: detail.sourcePath || window.location.pathname,
  };
  window.dispatchEvent(
    new CustomEvent<EditorLiveChatDetail>(EDITOR_LIVE_CHAT_EVENT, {
      detail: liveEditDetail,
    }),
  );
}

/** Refresh callbacks for an already-open target without reopening or focusing Chat. */
export function updateEditorLiveChat(detail: EditorLiveChatDetail) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(
    new CustomEvent<EditorLiveChatDetail>(EDITOR_LIVE_CHAT_UPDATE_EVENT, {
      detail: {
        ...detail,
        sourcePath: detail.sourcePath || window.location.pathname,
      },
    }),
  );
}

export function closeEditorLiveChat() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(EDITOR_LIVE_CHAT_CLOSE_EVENT));
}
