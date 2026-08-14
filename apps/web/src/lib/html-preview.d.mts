export interface HtmlPreviewFsReadResult {
  content: string;
  encoding: string;
  mime_type?: string;
}

export type HtmlPreviewAssetReplacement =
  | { kind: "url"; value: string }
  | { kind: "style"; value: string }
  | { kind: "script"; value: string };

export function isLocalHtmlPreviewUrl(url: string): boolean;
export function stripHtmlPreviewUrlSuffix(url: string): string;
export function normalizeHtmlPreviewPath(path: string): string;
export function resolveHtmlPreviewAssetPath(currentFsPath: string | null | undefined, rawUrl: string): string | null;
export function extractLocalHtmlPreviewAssetRefs(html: string): string[];
export function extractLocalCssPreviewAssetRefs(css: string): string[];
export function htmlPreviewAssetKind(
  ref: string,
  result: HtmlPreviewFsReadResult,
): HtmlPreviewAssetReplacement["kind"] | null;
export function rewriteCssPreviewAssetUrls(css: string, replacements: Record<string, string>): string;
export function rewriteHtmlPreviewAssetUrls(
  html: string,
  assets: Record<string, HtmlPreviewAssetReplacement>,
): string;
export function injectHtmlPreviewNavigationGuard(html: string): string;
