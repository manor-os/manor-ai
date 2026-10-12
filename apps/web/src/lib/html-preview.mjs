const HTML_RESOURCE_ATTRIBUTE_RE = /\b(?:src|poster|data)\s*=\s*(["'])(.*?)\1/gi;
const HTML_LINK_RESOURCE_RE = /<(?:link|use|image)\b[^>]*?\b(?:href|xlink:href)\s*=\s*(["'])(.*?)\1[^>]*>/gi;
const CSS_URL_RE = /url\(\s*(["']?)(.*?)\1\s*\)/gi;
const CSS_QUOTED_IMPORT_RE = /@import\s+(["'])(.*?)\1/gi;
const CLOUDFLARE_EMAIL_DECODER_ELEMENT_RE = /<script\b([^>]*?)\bsrc\s*=\s*(["'])(\/cdn-cgi\/scripts\/[^/"'<>\s]+\/cloudflare-static\/email-decode\.min\.js(?:[?#][^"'<>]*)?)\2([^>]*)>[\s\S]*?<\/script>/gi;
const CLOUDFLARE_EMAIL_DECODER_SCRIPT = [
  "(() => {",
  "  const decode = (encoded) => {",
  "    const value = String(encoded || '').trim();",
  "    if (!/^[0-9a-f]+$/i.test(value) || value.length < 4 || value.length > 1026 || value.length % 2 !== 0) return null;",
  "    const key = Number.parseInt(value.slice(0, 2), 16);",
  "    const bytes = new Uint8Array((value.length - 2) / 2);",
  "    for (let index = 2; index < value.length; index += 2) {",
  "      bytes[(index - 2) / 2] = Number.parseInt(value.slice(index, index + 2), 16) ^ key;",
  "    }",
  "    try { return new TextDecoder().decode(bytes); } catch { return null; }",
  "  };",
  "  const decodeAll = () => {",
  "    document.querySelectorAll('[data-cfemail]').forEach((element) => {",
  "      const email = decode(element.getAttribute('data-cfemail'));",
  "      if (!email) return;",
  "      element.textContent = email;",
  "      const link = element.closest('a[href^=\"/cdn-cgi/l/email-protection\"]');",
  "      if (link) link.setAttribute('href', 'mailto:' + email);",
  "    });",
  "  };",
  "  decodeAll();",
  "  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', decodeAll, { once: true });",
  "})();",
].join("\n");

export function isLocalHtmlPreviewUrl(url) {
  const trimmed = String(url || "").trim();
  if (!trimmed || trimmed.startsWith("#")) return false;
  // Cloudflare injects this origin-relative virtual path into proxied HTML.
  // It belongs to the deployment edge, not the entity filesystem.
  if (/^\/+cdn-cgi(?:\/|$)/i.test(trimmed)) return false;
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

function safeHtmlPreviewMimeType(value) {
  const mime = String(value || "").trim().toLowerCase();
  return /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/.test(mime)
    ? mime
    : "application/octet-stream";
}

function utf8Base64(value) {
  const bytes = new TextEncoder().encode(String(value));
  let binary = "";
  for (let index = 0; index < bytes.length; index += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(index, index + 0x8000));
  }
  return globalThis.btoa(binary);
}

export function htmlPreviewAssetDataUrl(result) {
  const mimeType = safeHtmlPreviewMimeType(result.mime_type);
  const payload = result.encoding === "base64"
    ? String(result.content).replace(/\s/g, "")
    : utf8Base64(result.content);
  return `data:${mimeType};base64,${payload}`;
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

function rewriteCloudflareEmailProtection(html) {
  CLOUDFLARE_EMAIL_DECODER_ELEMENT_RE.lastIndex = 0;
  return html.replace(
    CLOUDFLARE_EMAIL_DECODER_ELEMENT_RE,
    (_match, _before, _quote, url) => (
      `<script data-manor-preview-src="${escapeHtmlPreviewAttribute(url)}">\n${CLOUDFLARE_EMAIL_DECODER_SCRIPT}\n</script>`
    ),
  );
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
  const edgeSafeHtml = rewriteCloudflareEmailProtection(html);
  if (!Object.keys(assets).length) return edgeSafeHtml;
  return edgeSafeHtml
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
