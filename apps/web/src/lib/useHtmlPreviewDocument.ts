import { useEffect, useMemo, useState } from "react";
import { api } from "./api";
import {
  extractLocalCssPreviewAssetRefs,
  extractLocalHtmlPreviewAssetRefs,
  htmlPreviewAssetDataUrl,
  htmlPreviewAssetKind,
  injectHtmlPreviewNavigationGuard,
  resolveHtmlPreviewAssetPath,
  rewriteCssPreviewAssetUrls,
  rewriteHtmlPreviewAssetUrls,
  type HtmlPreviewAssetReplacement,
  type HtmlPreviewFsReadResult,
} from "./html-preview.mjs";
import { useIsolatedHtmlPreview } from "./useIsolatedHtmlPreview";

const MAX_STYLESHEET_DEPTH = 8;
const EMPTY_PREVIEW_DOCUMENT = "<!doctype html><html><head></head><body></body></html>";
const EMPTY_PREVIEW_REPLACEMENTS: Record<string, HtmlPreviewAssetReplacement> = {};
const EMPTY_TEXT_OVERRIDES: Record<string, string> = {};

interface HtmlPreviewAssetState {
  key: string;
  replacements: Record<string, HtmlPreviewAssetReplacement>;
  failedAssetCount: number;
}

interface HtmlPreviewLoadContext {
  failedAssetCount: number;
}

async function readPreviewAsset(
  path: string,
  textOverrides: Record<string, string>,
): Promise<HtmlPreviewFsReadResult> {
  if (Object.prototype.hasOwnProperty.call(textOverrides, path)) {
    return {
      content: textOverrides[path],
      encoding: "utf-8",
      mime_type: "",
    };
  }
  return api.fs.read(path);
}

async function rewriteProtectedStylesheet(
  css: string,
  stylesheetPath: string,
  context: HtmlPreviewLoadContext,
  ancestors: Set<string>,
  depth: number,
  textOverrides: Record<string, string>,
): Promise<string> {
  if (depth >= MAX_STYLESHEET_DEPTH) return css;
  const refs = extractLocalCssPreviewAssetRefs(css);
  if (!refs.length) return css;

  const replacements: Record<string, string> = {};
  await Promise.all(refs.map(async (ref) => {
    const path = resolveHtmlPreviewAssetPath(stylesheetPath, ref);
    if (!path || ancestors.has(path)) return;
    try {
      const result = await readPreviewAsset(path, textOverrides);
      const kind = htmlPreviewAssetKind(ref, result);
      if (kind === "style") {
        const nestedCss = await rewriteProtectedStylesheet(
          result.content,
          path,
          context,
          new Set([...ancestors, path]),
          depth + 1,
          textOverrides,
        );
        replacements[ref] = htmlPreviewAssetDataUrl({
          content: nestedCss,
          encoding: "utf-8",
          mime_type: result.mime_type || "text/css",
        });
        return;
      }
      replacements[ref] = htmlPreviewAssetDataUrl(result);
    } catch {
      context.failedAssetCount += 1;
    }
  }));
  return rewriteCssPreviewAssetUrls(css, replacements);
}

async function loadHtmlPreviewAssets(
  refs: string[],
  fsPath: string,
  textOverrides: Record<string, string>,
): Promise<{ replacements: Record<string, HtmlPreviewAssetReplacement>; context: HtmlPreviewLoadContext }> {
  const replacements: Record<string, HtmlPreviewAssetReplacement> = {};
  const context: HtmlPreviewLoadContext = { failedAssetCount: 0 };

  await Promise.all(refs.map(async (ref) => {
    const path = resolveHtmlPreviewAssetPath(fsPath, ref);
    if (!path) return;
    try {
      const result = await readPreviewAsset(path, textOverrides);
      const kind = htmlPreviewAssetKind(ref, result);
      if (kind === "style") {
        replacements[ref] = {
          kind,
          value: await rewriteProtectedStylesheet(result.content, path, context, new Set([path]), 0, textOverrides),
        };
        return;
      }
      if (kind === "script") {
        replacements[ref] = { kind, value: result.content };
        return;
      }
      replacements[ref] = { kind: "url", value: htmlPreviewAssetDataUrl(result) };
    } catch {
      context.failedAssetCount += 1;
    }
  }));

  return { replacements, context };
}

export interface HtmlPreviewDocumentResult {
  previewUrl: string | null;
  isResolvingAssets: boolean;
  isPreparingPreview: boolean;
  failedAssetCount: number;
  previewError: string | null;
  retryPreview: () => void;
}

export function useHtmlPreviewDocument(
  content: string,
  fsPath: string | null | undefined,
  enabled = true,
  textOverrides: Record<string, string> = EMPTY_TEXT_OVERRIDES,
): HtmlPreviewDocumentResult {
  const refs = useMemo(
    () => (enabled ? extractLocalHtmlPreviewAssetRefs(content) : []),
    [content, enabled],
  );
  const refKey = useMemo(() => refs.slice().sort().join("\n"), [refs]);
  const overrideKey = useMemo(() => Object.entries(textOverrides)
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([path, value]) => `${path}\0${value}`)
    .join("\0\0"), [textOverrides]);
  const assetKey = enabled ? `${fsPath || ""}\0${refKey}\0${overrideKey}` : "disabled";
  const [assetState, setAssetState] = useState<HtmlPreviewAssetState>({
    key: enabled && !fsPath ? assetKey : "",
    replacements: {},
    failedAssetCount: 0,
  });
  useEffect(() => {
    if (!enabled || !fsPath || !refs.length) {
      setAssetState({ key: assetKey, replacements: {}, failedAssetCount: 0 });
      return;
    }

    let cancelled = false;
    void loadHtmlPreviewAssets(refs, fsPath, textOverrides).then(({ replacements, context }) => {
      if (cancelled) return;
      setAssetState({ key: assetKey, replacements, failedAssetCount: context.failedAssetCount });
    });

    return () => {
      cancelled = true;
    };
  }, [assetKey, enabled, fsPath, refKey, textOverrides]);

  const isResolvingAssets = Boolean(enabled && fsPath && refs.length && assetState.key !== assetKey);
  const activeReplacements = assetState.key === assetKey
    ? assetState.replacements
    : EMPTY_PREVIEW_REPLACEMENTS;
  const previewDocument = useMemo(() => {
    if (!enabled) return content;
    if (isResolvingAssets) return EMPTY_PREVIEW_DOCUMENT;
    return injectHtmlPreviewNavigationGuard(rewriteHtmlPreviewAssetUrls(content, activeReplacements));
  }, [activeReplacements, content, enabled, isResolvingAssets]);
  const isolatedPreview = useIsolatedHtmlPreview(previewDocument, !isResolvingAssets);

  return {
    previewUrl: isolatedPreview.previewUrl,
    isResolvingAssets,
    isPreparingPreview: isolatedPreview.isPreparingPreview,
    failedAssetCount: assetState.key === assetKey ? assetState.failedAssetCount : 0,
    previewError: isolatedPreview.previewError,
    retryPreview: isolatedPreview.retryPreview,
  };
}
