const HTML_RESOURCE_ATTRIBUTE_RE = /\b(?:src|poster|data)\s*=\s*(["'])(.*?)\1/gi;
const HTML_LINK_RESOURCE_RE = /<(?:link|use|image)\b[^>]*?\b(?:href|xlink:href)\s*=\s*(["'])(.*?)\1[^>]*>/gi;
const CSS_URL_RE = /url\(\s*(["']?)(.*?)\1\s*\)/gi;
const CSS_QUOTED_IMPORT_RE = /@import\s+(["'])(.*?)\1/gi;

export function isLocalHtmlPreviewUrl(url) {
  const trimmed = String(url || "").trim();
  if (!trimmed || trimmed.startsWith("#")) return false;
  return !/^(?:[a-z][a-z0-9+.-]*:|\/\/)/i.test(trimmed);
}

export function stripHtmlPreviewUrlSuffix(url) {
  return String(url || "").split(/[?#]/, 1)[0] || "";
}

export function normalizeHtmlPreviewPath(path) {
  const parts = [];
  for (const rawPart of String(path || "").replace(/\\/g, "/").split("/")) {
    const part = rawPart.trim();
    if (!part || part === ".") continue;
    if (part === "..") {
      parts.pop();
      continue;
    }
    parts.push(part);
  }
  return parts.join("/");
}

function htmlPreviewDirname(path) {
  const normalized = normalizeHtmlPreviewPath(path);
  const index = normalized.lastIndexOf("/");
  return index >= 0 ? normalized.slice(0, index) : "";
}

export function resolveHtmlPreviewAssetPath(currentFsPath, rawUrl) {
  if (!currentFsPath || !isLocalHtmlPreviewUrl(rawUrl)) return null;
  const rawPath = stripHtmlPreviewUrlSuffix(rawUrl);
  const cleanUrl = rawPath.replace(/^\/+/, "");
  if (!cleanUrl) return null;
  if (rawPath.startsWith("/")) return normalizeHtmlPreviewPath(cleanUrl);
  return normalizeHtmlPreviewPath(`${htmlPreviewDirname(currentFsPath)}/${cleanUrl}`);
}

function collectLocalMatches(source, pattern, urlGroup, refs) {
  pattern.lastIndex = 0;
  let match;
  while ((match = pattern.exec(source))) {
    const url = String(match[urlGroup] || "").trim();
    if (isLocalHtmlPreviewUrl(url)) refs.add(url);
  }
}

export function extractLocalHtmlPreviewAssetRefs(html) {
  const refs = new Set();
  collectLocalMatches(html, HTML_RESOURCE_ATTRIBUTE_RE, 2, refs);
  collectLocalMatches(html, HTML_LINK_RESOURCE_RE, 2, refs);
  collectLocalMatches(html, CSS_URL_RE, 2, refs);
  return [...refs];
}

export function extractLocalCssPreviewAssetRefs(css) {
  const refs = new Set();
  collectLocalMatches(css, CSS_URL_RE, 2, refs);
  collectLocalMatches(css, CSS_QUOTED_IMPORT_RE, 2, refs);
  return [...refs];
}

export function htmlPreviewAssetKind(ref, result) {
  if (result.encoding !== "utf-8") return null;
  const cleanRef = stripHtmlPreviewUrlSuffix(ref).toLowerCase();
  const mime = String(result.mime_type || "").toLowerCase();
  if (cleanRef.endsWith(".css") || mime === "text/css") return "style";
  if (
    cleanRef.endsWith(".js")
    || cleanRef.endsWith(".mjs")
    || cleanRef.endsWith(".cjs")
    || mime.includes("javascript")
    || mime === "text/ecmascript"
  ) {
    return "script";
  }
  return null;
}

function escapeHtmlPreviewAttribute(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/"/g, "&quot;")
    .replace(/</g, "&lt;");
}

function escapeHtmlRawElementText(value, tagName) {
  const closingTag = new RegExp(`</${tagName}`, "gi");
  return String(value).replace(closingTag, `<\\/${tagName}`);
}

export function rewriteCssPreviewAssetUrls(css, replacements) {
  if (!Object.keys(replacements).length) return css;
  return css
    .replace(CSS_QUOTED_IMPORT_RE, (match, quote, url) => {
      const replacement = replacements[String(url).trim()];
      return replacement ? `@import ${quote}${replacement}${quote}` : match;
    })
    .replace(CSS_URL_RE, (match, quote, url) => {
      const replacement = replacements[String(url).trim()];
      return replacement ? `url(${quote || ""}${replacement}${quote || ""})` : match;
    });
}

export function rewriteHtmlPreviewAssetUrls(html, assets) {
  if (!Object.keys(assets).length) return html;
  return html
    .replace(/<link\b([^>]*?)\bhref\s*=\s*(["'])(.*?)\2([^>]*)>/gi, (match, before, _quote, url, after) => {
      const asset = assets[String(url).trim()];
      const attributes = `${before || ""} ${after || ""}`;
      if (asset?.kind === "style" && /\brel\s*=\s*(["'])?[^"'>\s]*stylesheet/i.test(attributes)) {
        return `<style data-manor-preview-src="${escapeHtmlPreviewAttribute(String(url).trim())}">\n${escapeHtmlRawElementText(asset.value, "style")}\n</style>`;
      }
      if (asset?.kind === "url") return match.replace(String(url), asset.value);
      return match;
    })
    .replace(/<script\b([^>]*?)\bsrc\s*=\s*(["'])(.*?)\2([^>]*)>([\s\S]*?)<\/script>/gi, (match, before, _quote, url, after) => {
      const asset = assets[String(url).trim()];
      if (asset?.kind === "script") {
        const rawAttributes = `${before || ""}${after || ""}`.replace(
          /\s+\b(?:async|defer|crossorigin|integrity|referrerpolicy)\b(?:\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+))?/gi,
          "",
        ).trim();
        const attributes = rawAttributes ? ` ${rawAttributes}` : "";
        return `<script${attributes} data-manor-preview-src="${escapeHtmlPreviewAttribute(String(url).trim())}">\n${escapeHtmlRawElementText(asset.value, "script")}\n</script>`;
      }
      if (asset?.kind === "url") return match.replace(String(url), asset.value);
      return match;
    })
    .replace(/\b(src|href|poster|data)\s*=\s*(["'])(.*?)\2/gi, (match, attribute, quote, url) => {
      const replacement = assets[String(url).trim()];
      return replacement?.kind === "url"
        ? `${attribute}=${quote}${replacement.value}${quote}`
        : match;
    })
    .replace(CSS_URL_RE, (match, quote, url) => {
      const replacement = assets[String(url).trim()];
      return replacement?.kind === "url"
        ? `url(${quote || ""}${replacement.value}${quote || ""})`
        : match;
    });
}

const HTML_PREVIEW_NAVIGATION_GUARD = [
  '<base href="about:blank">',
  "<script>",
  "(() => {",
  "  const isNavigationHref = (href) => {",
  "    const value = String(href || '').trim();",
  "    return Boolean(value) && !value.startsWith('#') && !/^(?:javascript:|mailto:|tel:|data:|blob:)/i.test(value);",
  "  };",
  "  const block = (event) => {",
  "    event.preventDefault();",
  "    event.stopImmediatePropagation();",
  "  };",
  "  document.addEventListener('click', (event) => {",
  "    const target = event.target instanceof Element ? event.target : null;",
  "    if (!target) return;",
  "    const link = target.closest('a[href], area[href]');",
  "    if (link && isNavigationHref(link.getAttribute('href'))) {",
  "      block(event);",
  "      return;",
  "    }",
  "    if (target.closest('button[formaction], input[formaction], button[type=\"submit\"], input[type=\"submit\"]')) {",
  "      block(event);",
  "    }",
  "  }, true);",
  "  document.addEventListener('submit', block, true);",
  "  window.open = () => null;",
  "})();",
  "</script>",
].join("");

export function injectHtmlPreviewNavigationGuard(html) {
  if (/<head(?:\s|>)/i.test(html)) {
    return html.replace(/<head([^>]*)>/i, `<head$1>${HTML_PREVIEW_NAVIGATION_GUARD}`);
  }
  if (/<html(?:\s|>)/i.test(html)) {
    return html.replace(/<html([^>]*)>/i, `<html$1><head>${HTML_PREVIEW_NAVIGATION_GUARD}</head>`);
  }
  return `<!doctype html><html><head>${HTML_PREVIEW_NAVIGATION_GUARD}</head><body>${html}</body></html>`;
}
