/* Pull renderable file/media references out of a node's run output.

   Generated-file nodes return varied shapes (image gen → {image_url}, audio →
   {kind, mime_type, ...}, code → {url}/{entry_url}, etc.), so rather than match
   a fixed schema we walk the whole output and collect any URL-ish string —
   resilient to schema drift. Classified by extension / data-url mime / key. */

export type MediaType = "image" | "video" | "audio" | "file";

export interface MediaRef {
  url: string;
  type: MediaType;
  name?: string;
}

const IMG_EXT = ["png", "jpg", "jpeg", "gif", "webp", "svg", "bmp", "avif"];
const VID_EXT = ["mp4", "webm", "mov", "m4v", "ogv"];
const AUD_EXT = ["mp3", "wav", "ogg", "m4a", "flac", "aac"];

const URL_KEYS = new Set([
  "image_url", "url", "file_url", "video_url", "audio_url",
  "download_url", "src", "path", "entry_url", "thumbnail",
]);

function isUrlish(v: unknown): v is string {
  return (
    typeof v === "string" &&
    (v.startsWith("/api/v1/fs/") ||
      /^https?:\/\//.test(v) ||
      v.startsWith("data:") ||
      v.startsWith("blob:"))
  );
}

function classify(url: string, key?: string): MediaType {
  const u = url.toLowerCase();
  if (u.startsWith("data:image") || key === "image_url" || key === "thumbnail") return "image";
  if (u.startsWith("data:video") || key === "video_url") return "video";
  if (u.startsWith("data:audio") || key === "audio_url") return "audio";
  const ext = u.split(/[?#]/)[0].split(".").pop() || "";
  if (IMG_EXT.includes(ext)) return "image";
  if (VID_EXT.includes(ext)) return "video";
  if (AUD_EXT.includes(ext)) return "audio";
  return "file";
}

function fileName(url: string): string | undefined {
  if (url.startsWith("data:")) return undefined;
  try {
    const path = url.split(/[?#]/)[0];
    const last = path.split("/").filter(Boolean).pop();
    return last && last.includes(".") ? decodeURIComponent(last) : undefined;
  } catch {
    return undefined;
  }
}

/** Extract up to `limit` renderable media references from a node output. */
export function extractMediaRefs(output: unknown, limit = 6): MediaRef[] {
  let root: unknown = output;
  if (typeof output === "string") {
    const s = output.trim();
    try {
      root = JSON.parse(s);
    } catch {
      // Not JSON — scan the raw text for bare URLs.
      const refs: MediaRef[] = [];
      const re = /(https?:\/\/[^\s"')]+|\/api\/v1\/fs\/[^\s"')]+|data:[a-z]+\/[^\s"')]+)/gi;
      let m: RegExpExecArray | null;
      while ((m = re.exec(s)) && refs.length < limit) {
        refs.push({ url: m[1], type: classify(m[1]), name: fileName(m[1]) });
      }
      return refs;
    }
  }

  const refs: MediaRef[] = [];
  const seen = new Set<string>();
  const walk = (node: unknown, key?: string) => {
    if (node == null || refs.length >= limit) return;
    if (typeof node === "string") {
      if ((isUrlish(node) || (key && URL_KEYS.has(key) && node)) && !seen.has(node) && isUrlish(node)) {
        seen.add(node);
        refs.push({ url: node, type: classify(node, key), name: fileName(node) });
      }
      return;
    }
    if (Array.isArray(node)) {
      for (const n of node) walk(n);
      return;
    }
    if (typeof node === "object") {
      for (const [k, v] of Object.entries(node as Record<string, unknown>)) walk(v, k);
    }
  };
  walk(root);

  // Images first — they're the headline preview (ComfyUI-style).
  return refs.sort((a, b) => (a.type === "image" ? -1 : 0) - (b.type === "image" ? -1 : 0));
}

/** The single best preview for a node — the first image, else first media. */
export function primaryMediaRef(output: unknown): MediaRef | undefined {
  const refs = extractMediaRefs(output, 6);
  return refs.find((r) => r.type === "image") || refs[0];
}
