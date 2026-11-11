/**
 * How a Workspace App shows a record as the post it will be on a platform: which frame each platform gets,
 * how long its words may be there, the copyable group of hashtags, the rows of a side-by-side layout and the
 * rows of a week laid out by platform. Pure, so the page and its tests read the same rules.
 */
import type { WorkspaceAppPostPreview, WorkspaceAppPreviewFile, WorkspaceAppPreviewPlatform } from "../../lib/api";

/** The shape a frame takes: a feed post, a 9:16 screen, a note, a video card, an article, a 1–9 grid, a pin. */
export type PreviewShape = "feed" | "vertical" | "note" | "video" | "article" | "moments" | "pin" | "post";

type FrameSpec = {
  shape: PreviewShape;
  /** The picture frame a feed post's media shows in, unless the block names another. */
  aspect?: string;
  /** How many characters each part may hold there; absent where the platform sets no practical limit. */
  limits?: { caption?: number; title?: number; summary?: number; hashtags?: number };
  /** How many lines of the caption show before "more". */
  lines?: number;
};

const FRAMES: Record<WorkspaceAppPreviewPlatform, FrameSpec> = {
  instagram: { shape: "feed", aspect: "1:1", limits: { caption: 2200, hashtags: 30 }, lines: 2 },
  instagram_reels: { shape: "vertical", limits: { caption: 2200, hashtags: 30 }, lines: 2 },
  instagram_story: { shape: "vertical", lines: 3 },
  tiktok: { shape: "vertical", limits: { caption: 4000 }, lines: 2 },
  douyin: { shape: "vertical", lines: 2 },
  youtube_shorts: { shape: "vertical", limits: { title: 100 }, lines: 2 },
  xiaohongshu: { shape: "note", aspect: "3:4", limits: { title: 20, caption: 1000 } },
  x: { shape: "feed", aspect: "16:9", limits: { caption: 280 } },
  linkedin: { shape: "feed", aspect: "4:3", limits: { caption: 3000 }, lines: 3 },
  facebook: { shape: "feed", aspect: "4:3", lines: 4 },
  youtube: { shape: "video", aspect: "16:9", limits: { title: 100, caption: 5000 }, lines: 3 },
  wechat_article: { shape: "article", limits: { title: 64, summary: 120 } },
  wechat_moments: { shape: "moments", lines: 6 },
  pinterest: { shape: "pin", aspect: "2:3", limits: { title: 100, caption: 500 }, lines: 2 },
};

const VERTICAL_SAFE_AREA: Record<string, { top: number; bottom: number; right: number }> = {
  // The parts of a 9:16 screen the platform covers with its own controls, as shares of the frame.
  instagram_story: { top: 0.13, bottom: 0.18, right: 0.04 },
  instagram_reels: { top: 0.1, bottom: 0.28, right: 0.16 },
  tiktok: { top: 0.1, bottom: 0.26, right: 0.16 },
  douyin: { top: 0.1, bottom: 0.26, right: 0.16 },
  youtube_shorts: { top: 0.1, bottom: 0.24, right: 0.16 },
};

export function previewFrame(platform: WorkspaceAppPreviewPlatform | null | undefined): FrameSpec {
  return platform ? FRAMES[platform] || { shape: "post" } : { shape: "post" };
}

/** Where a vertical screen's caption and buttons may sit without the platform's own controls over them. */
export function previewSafeArea(platform: WorkspaceAppPreviewPlatform | null | undefined) {
  return platform ? VERTICAL_SAFE_AREA[platform] || null : null;
}

// Wide characters (CJK, kana, hangul, full-width forms) and most emoji count twice on X.
const WIDE = /[ᄀ-ᅟ⺀-꓏가-힣豈-﫿︰-﹏＀-｠￠-￦]|\p{Extended_Pictographic}/u;
const URL = /https?:\/\/\S+/gi;

/** How long a text is on a platform: X counts a link as 23 and a wide character as 2; elsewhere each character once. */
export function postTextLength(platform: WorkspaceAppPreviewPlatform | null | undefined, text: string): number {
  if (platform !== "x") return Array.from(text).length;
  const links = text.match(URL) || [];
  const rest = text.replace(URL, "");
  return links.length * 23 + Array.from(rest).reduce((sum, character) => sum + (WIDE.test(character) ? 2 : 1), 0);
}

/** A group of hashtags as people paste it: "#pricing #saas". */
export function hashtagText(tags: string[] | undefined): string {
  return (tags || []).map((tag) => `#${tag}`).join(" ");
}

/** The words that go out: the caption, then its hashtags, as one text to copy or to count. */
export function postText(preview: WorkspaceAppPostPreview): string {
  const tags = hashtagText(preview.hashtags);
  return [preview.caption || "", tags].filter(Boolean).join(preview.caption && tags ? "\n\n" : "");
}

export type PreviewCount = { part: "caption" | "title" | "summary" | "hashtags"; count: number; limit: number; over: boolean };

/** Each part that has a limit on the post's platform, with how much of it is used. */
export function previewCounts(preview: WorkspaceAppPostPreview): PreviewCount[] {
  const limits = previewFrame(preview.platform).limits || {};
  const counts: PreviewCount[] = [];
  const add = (part: PreviewCount["part"], count: number) => {
    const limit = limits[part];
    if (limit && count) counts.push({ part, count, limit, over: count > limit });
  };
  add("title", preview.title ? postTextLength(preview.platform, preview.title) : 0);
  add("caption", postTextLength(preview.platform, postText(preview)));
  add("summary", preview.summary ? postTextLength(preview.platform, preview.summary) : 0);
  add("hashtags", preview.hashtags?.length || 0);
  return counts;
}

/** Each post of a thread, with its own length against X's limit. */
export function threadCounts(preview: WorkspaceAppPostPreview): Array<{ text: string; count: number; over: boolean }> {
  const limit = previewFrame(preview.platform).limits?.caption;
  return (preview.thread || []).map((text) => {
    const count = postTextLength(preview.platform, text);
    return { text, count, over: Boolean(limit && count > limit) };
  });
}

/** How a 1–9 picture grid lays out: one large, two or four in two columns, otherwise three. */
export function pictureGridColumns(count: number): number {
  if (count <= 1) return 1;
  return count === 2 || count === 4 ? 2 : 3;
}

/** The pictures and the clip a post shows: its declared parts, else the files a gallery result holds. */
export function previewMedia(preview: WorkspaceAppPostPreview, files: Array<{ document_id: string; mime_type?: string | null }> = []):
  { images: WorkspaceAppPreviewFile[]; video?: WorkspaceAppPreviewFile; cover?: WorkspaceAppPreviewFile } {
  const kind = (file: { mime_type?: string | null }) => String(file.mime_type || "").toLowerCase().split("/")[0];
  const images = preview.images?.length ? preview.images
    : files.filter((file) => kind(file) === "image").map((file) => ({ document_id: file.document_id }));
  const fileVideo = files.find((file) => kind(file) === "video");
  const video = preview.video || (fileVideo ? { document_id: fileVideo.document_id } : undefined);
  return { images, ...(video ? { video } : {}), ...(preview.cover ? { cover: preview.cover } : {}) };
}

/** The rows of a side-by-side layout: the posts of one group (a post's platform versions) together, in order. */
export function previewRows<T extends { id: string; preview?: WorkspaceAppPostPreview }>(items: T[]): Array<{ key: string; items: T[] }> {
  const rows = new Map<string, T[]>();
  for (const item of items) {
    const key = item.preview?.group || "";
    rows.set(key, [...(rows.get(key) || []), item]);
  }
  return [...rows].map(([key, members]) => ({ key, items: members }));
}

/** A week laid out by platform: one row per value, in the order they first appear; items with none last. */
export function scheduleLanes<T extends { calendar_lane?: string }>(items: T[]): Array<{ key: string; items: T[] }> {
  const lanes = new Map<string, T[]>();
  for (const item of items) {
    const key = item.calendar_lane || "";
    lanes.set(key, [...(lanes.get(key) || []), item]);
  }
  const named = [...lanes].filter(([key]) => key).map(([key, members]) => ({ key, items: members }));
  return lanes.has("") ? [...named, { key: "", items: lanes.get("")! }] : named;
}
