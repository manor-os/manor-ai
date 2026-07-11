import { useEffect, useState } from "react";
import { api, resolveDisplayMediaUrl } from "../lib/api";
import type { ChatMessage } from "../lib/chatStream";
import { t } from "../lib/i18n";
import type { Document } from "../lib/types";
import {
  getChatBoxModeConfig,
  type ChatBoxMode,
} from "./ChatModeSelector";

export type ChatMessageDisplayChip = {
  key: string;
  label: string;
  title?: string;
  kind: "mode" | "setting" | "skill" | "reference" | "attachment";
};

export type ChatMessageDisplayReference = {
  key: string;
  name: string;
  id?: string;
  kind: "image" | "video" | "audio" | "file";
  fileType?: string;
  mimeType?: string;
  url?: string;
  previewUrl?: string;
};

export type ParsedUserMessageDisplay = {
  cleanContent: string;
  chips: ChatMessageDisplayChip[];
  references: ChatMessageDisplayReference[];
};

const CHAT_BOX_MODE_KEYS = new Set<ChatBoxMode>([
  "auto",
  "image",
  "video",
  "audio",
  "document",
  "slides",
  "sheet",
  "website",
  "research",
]);

function normalizeMode(value: string | undefined): ChatBoxMode | undefined {
  const normalized = String(value || "")
    .trim()
    .toLowerCase()
    .replace(/\s+/g, "_");
  if (!normalized) return undefined;
  if (normalized === "图片" || normalized === "image_generation") return "image";
  if (normalized === "视频" || normalized === "video_generation") return "video";
  if (normalized === "音频" || normalized === "audio_generation") return "audio";
  if (normalized === "文档") return "document";
  if (normalized === "幻灯片" || normalized === "presentation") return "slides";
  if (normalized === "表格" || normalized === "spreadsheet") return "sheet";
  if (CHAT_BOX_MODE_KEYS.has(normalized as ChatBoxMode)) {
    return normalized as ChatBoxMode;
  }
  return undefined;
}

function modeLabel(value: string | undefined) {
  const mode = normalizeMode(value);
  return mode ? getChatBoxModeConfig(mode).label : String(value || "").trim();
}

function safeParsePayload(raw: unknown): Record<string, unknown> {
  if (!raw) return {};
  if (typeof raw === "object" && !Array.isArray(raw)) {
    return raw as Record<string, unknown>;
  }
  if (typeof raw !== "string") return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function compactValue(value: unknown): string {
  if (value == null || value === "") return "";
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return String(value).replace(/_/g, " ").trim();
}

function compactDuration(value: unknown): string {
  const text = compactValue(value);
  if (!text) return "";
  return /s$/i.test(text) ? text : `${text}s`;
}

function displayPayloadValue(key: string, value: unknown): string {
  const raw = compactValue(value);
  if (!raw) return "";
  const normalized = raw.toLowerCase().replace(/\s+/g, "_");
  if (key === "output_type") {
    if (normalized === "single_clip" || normalized === "clip") {
      return t("component.chat_message.output_single_clip");
    }
    if (normalized === "final" || normalized === "multi_clip_final") {
      return t("component.chat_message.output_final");
    }
    if (normalized === "edit" || normalized === "editable") {
      return t("component.chat_message.output_edit");
    }
  }
  if (key === "reference_policy") {
    if (normalized === "hash_references") {
      return t("component.chat_message.reference_all_refs");
    }
    if (normalized === "smart_references") {
      return t("component.chat_message.reference_smart_refs");
    }
    if (normalized === "first_last_frames") {
      return t("component.chat_message.reference_first_last");
    }
    if (normalized === "smart_multiframe") {
      return t("component.chat_message.reference_smart_multiframe");
    }
  }
  return raw;
}

function payloadTitle(key: string): string {
  if (key === "aspect_ratio") return t("component.chat_message.aspect_ratio");
  if (key === "resolution") return t("component.chat_message.resolution");
  if (key === "output_type") return t("component.chat_message.output_type");
  if (key === "reference_policy") return t("component.chat_message.reference_policy");
  return key.replace(/_/g, " ");
}

function payloadChips(payload: Record<string, unknown>): ChatMessageDisplayChip[] {
  const chips: ChatMessageDisplayChip[] = [];
  const duration =
    payload.clip_duration_seconds || payload.duration_seconds || payload.duration;
  const durationText = compactDuration(duration);
  if (durationText) {
    chips.push({
      key: "duration",
      kind: "setting",
      label: durationText,
      title: t("component.chat_message.duration"),
    });
  }
  for (const key of ["aspect_ratio", "resolution", "output_type", "reference_policy"]) {
    const value = displayPayloadValue(key, payload[key]);
    if (!value) continue;
    chips.push({
      key,
      kind: "setting",
      label: value,
      title: payloadTitle(key),
    });
  }
  return chips.slice(0, 5);
}

function pushUnique(chips: ChatMessageDisplayChip[], chip: ChatMessageDisplayChip) {
  const normalized = `${chip.kind}:${chip.label}`.toLowerCase();
  if (chips.some((item) => `${item.kind}:${item.label}`.toLowerCase() === normalized)) {
    return;
  }
  chips.push(chip);
}

function extensionFromName(name: string): string {
  const ext = name.split(".").pop() || "";
  return ext.toLowerCase();
}

function inferReferenceKind(
  name: string,
  mimeType?: string,
  fileType?: string,
): ChatMessageDisplayReference["kind"] {
  const mime = (mimeType || "").toLowerCase();
  const ext = (fileType || extensionFromName(name)).toLowerCase();
  if (mime.startsWith("image/") || /^(jpe?g|png|webp|gif|avif|heic|heif)$/i.test(ext)) {
    return "image";
  }
  if (mime.startsWith("video/") || /^(mp4|mov|m4v|webm|avi|mkv)$/i.test(ext)) {
    return "video";
  }
  if (mime.startsWith("audio/") || /^(mp3|wav|m4a|aac|flac|ogg|opus)$/i.test(ext)) {
    return "audio";
  }
  return "file";
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function pushReference(
  references: ChatMessageDisplayReference[],
  ref: Omit<ChatMessageDisplayReference, "key" | "kind"> & {
    kind?: ChatMessageDisplayReference["kind"];
  },
) {
  const name = String(ref.name || "").trim();
  if (!name) return;
  const kind = ref.kind || inferReferenceKind(name, ref.mimeType, ref.fileType);
  const previewKey = ref.previewUrl
    ? `${name}:${ref.previewUrl.length}:${ref.previewUrl.slice(-32)}`
    : "";
  const key = ref.id || ref.url || previewKey || name;
  const normalized = key.toLowerCase();
  if (references.some((item) => item.key.toLowerCase() === normalized)) return;
  references.push({
    key,
    name,
    id: ref.id,
    kind,
    fileType: ref.fileType,
    mimeType: ref.mimeType,
    url: ref.url,
    previewUrl: ref.previewUrl,
  });
}

function stripReferenceTokens(text: string, references: ChatMessageDisplayReference[]) {
  let next = text;
  for (const ref of references) {
    const escaped = escapeRegExp(`#${ref.name}`);
    next = next.replace(new RegExp(`(^|\\s)${escaped}(?=\\s|$|[，。,.;!?])`, "gu"), " ");
  }
  const fileHashPattern =
    /(^|\s)#([^\s#]+?\.(?:jpg|jpeg|png|webp|gif|avif|heic|heif|mp4|mov|m4v|webm|mp3|wav|m4a|aac|flac|ogg|opus|pdf|docx|pptx|xlsx|csv|json|txt|md))(?=\s|$|[，。,.;!?])/giu;
  next = next.replace(fileHashPattern, (_match, prefix: string, name: string) => {
    pushReference(references, { name });
    return prefix || " ";
  });
  return next
    .replace(/[ \t]{2,}/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n[ \t]+/g, "\n");
}

function referenceFromAttachment(
  attachment: NonNullable<ChatMessage["attachments"]>[number],
) {
  return {
    name: attachment.name,
    id: attachment.id,
    fileType: attachment.fileType,
    mimeType: attachment.mimeType,
    previewUrl: attachment.previewUrl,
  };
}

function documentReferenceKind(doc: Document): ChatMessageDisplayReference["kind"] {
  return inferReferenceKind(doc.name, doc.mime_type || undefined, doc.file_type || undefined);
}

const REFERENCE_DOCUMENT_CACHE_TTL_MS = 30 * 60 * 1000;
const referenceDocumentCache = new Map<
  string,
  { expiresAt: number; document: Document | null }
>();
const referenceDocumentInflight = new Map<string, Promise<Document | null>>();

function referenceDocumentCacheKey(refItem: ChatMessageDisplayReference) {
  if (refItem.id) return `id:${refItem.id}`;
  const name = refItem.name.trim().toLowerCase();
  const url = (refItem.url || "").trim();
  return `lookup:${refItem.kind}:${name}:${url}`;
}

export async function resolveChatMessageReferenceDocument(
  refItem: ChatMessageDisplayReference,
): Promise<Document | null> {
  const cacheKey = referenceDocumentCacheKey(refItem);
  const now = Date.now();
  const cached = referenceDocumentCache.get(cacheKey);
  if (cached && cached.expiresAt > now) return cached.document;
  if (cached) referenceDocumentCache.delete(cacheKey);

  const inflight = referenceDocumentInflight.get(cacheKey);
  if (inflight) return inflight;

  const lookup = (async () => {
    if (refItem.id) {
      try {
        return await api.documents.get(refItem.id);
      } catch {
        // Older history can carry stale/missing ids; fall through to name search.
      }
    }

    const name = refItem.name.trim();
    if (!name) return null;
    try {
      const docs = await api.documents.list({ search: name, limit: 10 });
      const exactName = name.toLowerCase();
      const items = docs.items || [];
      const kindMatches = items.filter((doc) => documentReferenceKind(doc) === refItem.kind);
      return (
        kindMatches.find((doc) => doc.name.toLowerCase() === exactName) ||
        items.find((doc) => doc.name.toLowerCase() === exactName) ||
        kindMatches[0] ||
        items[0] ||
        null
      );
    } catch {
      return null;
    }
  })();

  referenceDocumentInflight.set(cacheKey, lookup);
  try {
    const document = await lookup;
    referenceDocumentCache.set(cacheKey, {
      document,
      expiresAt: Date.now() + REFERENCE_DOCUMENT_CACHE_TTL_MS,
    });
    if (refItem.id && document) {
      referenceDocumentCache.set(`id:${document.id}`, {
        document,
        expiresAt: Date.now() + REFERENCE_DOCUMENT_CACHE_TTL_MS,
      });
    }
    if (referenceDocumentCache.size > 200) {
      const expiredAt = Date.now();
      for (const [key, entry] of referenceDocumentCache) {
        if (entry.expiresAt <= expiredAt || referenceDocumentCache.size > 160) {
          referenceDocumentCache.delete(key);
        }
      }
    }
    return document;
  } finally {
    referenceDocumentInflight.delete(cacheKey);
  }
}

export function parseUserMessageDisplay(msg: ChatMessage): ParsedUserMessageDisplay {
  const content = typeof msg.content === "string" ? msg.content : "";
  const lines = content.split(/\r?\n/);
  const keptLines: string[] = [];
  const chips: ChatMessageDisplayChip[] = [];
  const references: ChatMessageDisplayReference[] = [];
  let referenceCount = 0;
  let attachmentLineCount = 0;
  let parsedPayload: Record<string, unknown> = safeParsePayload(msg.chatModePayload);

  for (const attachment of msg.attachments || []) {
    pushReference(references, referenceFromAttachment(attachment));
  }

  const explicitMode = normalizeMode(msg.chatMode);
  if (explicitMode) {
    pushUnique(chips, {
      key: `mode-${explicitMode}`,
      kind: "mode",
      label: modeLabel(explicitMode),
      title: t("component.chat_message.mode"),
    });
  }

  for (const line of lines) {
    const trimmed = line.trim();
    const modeMatch = /^\[Mode:\s*(.+?)\]$/i.exec(trimmed);
    if (modeMatch) {
      pushUnique(chips, {
        key: `mode-${modeMatch[1]}`,
        kind: "mode",
        label: modeLabel(modeMatch[1]),
        title: t("component.chat_message.mode"),
      });
      continue;
    }

    const settingsMatch = /^\[Mode settings:\s*(\{.*\})\]$/i.exec(trimmed);
    if (settingsMatch) {
      parsedPayload = {
        ...safeParsePayload(settingsMatch[1]),
        ...parsedPayload,
      };
      continue;
    }

    const skillMatch = /^\[Skill:\s*(.+?)\]$/i.exec(trimmed);
    if (skillMatch) {
      for (const label of skillMatch[1].split(",")) {
        const clean = label.trim();
        if (clean) {
          pushUnique(chips, {
            key: `skill-${clean}`,
            kind: "skill",
            label: clean,
            title: t("component.chat_message.skill"),
          });
        }
      }
      continue;
    }

    const attachedMatch = /^\[Attached:\s*(.+?)\]$/i.exec(trimmed);
    if (attachedMatch) {
      attachmentLineCount += attachedMatch[1].split(",").filter((item) => item.trim()).length;
      continue;
    }

    const referenceLineMatch =
      /^\[(Image|Video|Audio|File)(?: from KB)?:\s*(.+?)\s*(?:→|->)\s*(\S+).*?\]$/i.exec(
        trimmed,
      );
    if (referenceLineMatch) {
      pushReference(references, {
        name: referenceLineMatch[2],
        url: referenceLineMatch[3],
        kind: referenceLineMatch[1].toLowerCase() as ChatMessageDisplayReference["kind"],
      });
      referenceCount += 1;
      continue;
    }

    if (/^\[Referenced people:\s*.+\]$/i.test(trimmed)) {
      continue;
    }

    keptLines.push(line);
  }

  for (const skill of msg.manualSkills || []) {
    if (!skill?.name) continue;
    pushUnique(chips, {
      key: `skill-${skill.id || skill.name}`,
      kind: "skill",
      label: skill.name,
      title: t("component.chat_message.skill"),
    });
  }

  for (const chip of payloadChips(parsedPayload)) {
    pushUnique(chips, chip);
  }

  const cleanContent = stripReferenceTokens(
    keptLines.join("\n").replace(/\n{3,}/g, "\n\n"),
    references,
  )
    .replace(/\n{3,}/g, "\n\n")
    .trim();

  const visibleReferenceCount = references.length || referenceCount;
  if (visibleReferenceCount > 0) {
    pushUnique(chips, {
      key: "references",
      kind: "reference",
      label: t("component.chat_message.refs_count", { count: visibleReferenceCount }),
      title: t("component.chat_message.references"),
    });
  }
  if (attachmentLineCount > 0 && !(msg.attachments || []).length) {
    pushUnique(chips, {
      key: "attachments",
      kind: "attachment",
      label: t("component.chat_message.attachments_count", {
        count: attachmentLineCount,
      }),
    });
  }

  return {
    cleanContent,
    chips,
    references,
  };
}

function referenceBadge(ref: ChatMessageDisplayReference) {
  if (ref.kind === "image") return "IMG";
  if (ref.kind === "video") return "VID";
  if (ref.kind === "audio") return "AUD";
  return (ref.fileType || extensionFromName(ref.name) || "FILE").toUpperCase().slice(0, 4);
}

function revokeObjectUrl(url: string) {
  if (url.startsWith("blob:")) URL.revokeObjectURL(url);
}

function ChatMessageReferenceThumb({ refItem }: { refItem: ChatMessageDisplayReference }) {
  const [thumbUrl, setThumbUrl] = useState<string>("");

  useEffect(() => {
    let cancelled = false;
    let revokeThumb = () => {};
    setThumbUrl("");
    if (refItem.kind !== "image" && refItem.kind !== "video") {
      return () => {};
    }

    const loadDocumentThumb = async (doc: { id: string }, kind = refItem.kind) => {
      const url =
        kind === "video"
          ? await api.documents.videoThumbnail(doc.id, { cache: true })
          : await api.documents.imageThumbnail(doc.id, { cache: true });
      return { url, revoke: () => revokeObjectUrl(url) };
    };

    const loadThumb = async () => {
      if (refItem.kind === "image" && refItem.previewUrl) {
        return { url: refItem.previewUrl, revoke: () => {} };
      }
      if (refItem.id) {
        try {
          return await loadDocumentThumb({ id: refItem.id });
        } catch {
          // Try URL/name fallback below.
        }
      }
      if (refItem.kind === "image" && refItem.url) {
        try {
          return await resolveDisplayMediaUrl(refItem.url);
        } catch {
          // Try document lookup below.
        }
      }
      const doc = await resolveChatMessageReferenceDocument(refItem);
      if (doc) {
        const kind = documentReferenceKind(doc);
        if (kind === "image" || kind === "video") {
          return loadDocumentThumb(doc, kind);
        }
      }
      return null;
    };

    loadThumb()
      .then((resolved) => {
        if (!resolved) return;
        if (cancelled) {
          resolved.revoke();
          return;
        }
        revokeThumb = resolved.revoke;
        setThumbUrl(resolved.url);
      })
      .catch(() => {
        if (!cancelled) setThumbUrl("");
      });
    return () => {
      cancelled = true;
      revokeThumb();
    };
  }, [refItem.id, refItem.kind, refItem.url, refItem.previewUrl]);

  if (thumbUrl) {
    return (
      <img
        className="chat-message-reference-thumb-img"
        src={thumbUrl}
        alt={t("component.chat_message.reference_thumbnail")}
      />
    );
  }

  return (
    <span className={`chat-message-reference-thumb chat-message-reference-thumb--${refItem.kind}`}>
      {referenceBadge(refItem)}
    </span>
  );
}

export function ChatMessageReferenceStrip({
  references,
  align = "left",
  onOpenReference,
}: {
  references: ChatMessageDisplayReference[];
  align?: "left" | "right";
  onOpenReference?: (refItem: ChatMessageDisplayReference) => void;
}) {
  if (!references.length) return null;
  return (
    <div
      className={`chat-message-reference-strip ${
        align === "right" ? "chat-message-reference-strip--right" : ""
      }`}
      aria-label={t("component.chat_message.references")}
    >
      {references.slice(0, 8).map((refItem) => (
        onOpenReference ? (
          <button
            key={refItem.key}
            type="button"
            className={`chat-message-reference-card chat-message-reference-card--${refItem.kind}`}
            title={refItem.name}
            onClick={() => onOpenReference(refItem)}
          >
            <ChatMessageReferenceThumb refItem={refItem} />
            <span className="chat-message-reference-name">{refItem.name}</span>
          </button>
        ) : (
          <span
            key={refItem.key}
            className={`chat-message-reference-card chat-message-reference-card--${refItem.kind}`}
            title={refItem.name}
          >
            <ChatMessageReferenceThumb refItem={refItem} />
            <span className="chat-message-reference-name">{refItem.name}</span>
          </span>
        )
      ))}
    </div>
  );
}

export function ChatMessageMetaChips({
  chips,
  align = "left",
}: {
  chips: ChatMessageDisplayChip[];
  align?: "left" | "right";
}) {
  if (!chips.length) return null;
  return (
    <div
      className={`chat-message-meta-chips ${
        align === "right" ? "chat-message-meta-chips--right" : ""
      }`}
    >
      {chips.map((chip) => (
        <span
          key={`${chip.kind}-${chip.key}`}
          className={`chat-message-meta-chip chat-message-meta-chip--${chip.kind}`}
          title={chip.title}
        >
          {chip.label}
        </span>
      ))}
    </div>
  );
}
