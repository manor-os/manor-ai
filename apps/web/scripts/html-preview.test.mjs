import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import {
  extractLocalCssPreviewAssetRefs,
  extractLocalHtmlPreviewAssetRefs,
  htmlPreviewAssetDataUrl,
  injectHtmlPreviewNavigationGuard,
  normalizeHtmlPreviewPath,
  resolveHtmlPreviewAssetPath,
  rewriteCssPreviewAssetUrls,
  rewriteHtmlPreviewAssetUrls,
} from "../src/lib/html-preview.mjs";

test("HTML preview discovers protected sibling assets without treating page links as resources", () => {
  const html = `
    <link rel="stylesheet" href="styles/site.css?v=2">
    <script defer src="scripts/app.js"></script>
    <img src="images/hero.webp">
    <video poster="images/poster.jpg"></video>
    <object data="media/chart.svg"></object>
    <a href="story.html">Story</a>
    <div style="background-image:url('images/texture.png')"></div>
    <script src="https://cdn.example.com/library.js"></script>
  `;

  assert.deepEqual(
    extractLocalHtmlPreviewAssetRefs(html).sort(),
    [
      "images/hero.webp",
      "images/poster.jpg",
      "images/texture.png",
      "media/chart.svg",
      "scripts/app.js",
      "styles/site.css?v=2",
    ],
  );
});

test("HTML preview resolves sibling, parent, and entity-root asset paths safely", () => {
  assert.equal(
    resolveHtmlPreviewAssetPath("projects/site/pages/index.html", "../styles/site.css?v=2#main"),
    "projects/site/styles/site.css",
  );
  assert.equal(
    resolveHtmlPreviewAssetPath("projects/site/pages/index.html", "/shared/logo.svg"),
    "shared/logo.svg",
  );
  assert.equal(normalizeHtmlPreviewPath("/projects/site/../shared/./logo.svg"), "projects/shared/logo.svg");
  assert.equal(resolveHtmlPreviewAssetPath("projects/site/index.html", "https://cdn.example.com/app.js"), null);
});

test("HTML preview inlines styles and scripts while rewriting protected media URLs", () => {
  const html = `<!doctype html><html><head>
    <link rel="stylesheet" href="styles.css">
    <script defer src="app.js"></script>
  </head><body><img src="hero.png"><a href="#story">Story</a></body></html>`;
  const rewritten = rewriteHtmlPreviewAssetUrls(html, {
    "styles.css": { kind: "style", value: ".hero{background:url(blob:nested)}" },
    "app.js": { kind: "script", value: "document.body.dataset.ready = 'yes';" },
    "hero.png": { kind: "url", value: "blob:hero" },
  });

  assert.match(rewritten, /<style data-manor-preview-src="styles\.css">/);
  assert.match(rewritten, /<script data-manor-preview-src="app\.js">/);
  assert.doesNotMatch(rewritten, /\bdefer\b/);
  assert.match(rewritten, /src="blob:hero"/);
  assert.match(rewritten, /href="#story"/);
});

test("HTML preview rewrites nested stylesheet URLs and imports", () => {
  const css = `
    @import "theme/base.css";
    @font-face { src: url('../fonts/inter.woff2') format('woff2'); }
    .hero { background-image: url(../images/hero.webp); }
    .external { background-image: url(https://cdn.example.com/external.webp); }
  `;

  assert.deepEqual(
    extractLocalCssPreviewAssetRefs(css).sort(),
    ["../fonts/inter.woff2", "../images/hero.webp", "theme/base.css"],
  );
  const rewritten = rewriteCssPreviewAssetUrls(css, {
    "theme/base.css": "blob:base",
    "../fonts/inter.woff2": "blob:font",
    "../images/hero.webp": "blob:hero",
  });
  assert.match(rewritten, /@import "blob:base"/);
  assert.match(rewritten, /url\('blob:font'\)/);
  assert.match(rewritten, /url\(blob:hero\)/);
  assert.match(rewritten, /url\(https:\/\/cdn\.example\.com\/external\.webp\)/);
});

test("HTML preview embeds protected assets as sandbox-readable data URLs", () => {
  assert.equal(
    htmlPreviewAssetDataUrl({
      content: "PHN2Zz48L3N2Zz4=\n",
      encoding: "base64",
      mime_type: "image/svg+xml",
    }),
    "data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=",
  );
  assert.equal(
    htmlPreviewAssetDataUrl({
      content: "预览 ✓",
      encoding: "utf-8",
      mime_type: "text/plain",
    }),
    "data:text/plain;base64,6aKE6KeIIOKckw==",
  );
  assert.match(
    htmlPreviewAssetDataUrl({
      content: "safe",
      encoding: "utf-8",
      mime_type: "text/html;base64,attack",
    }),
    /^data:application\/octet-stream;base64,/,
  );
});

test("HTML preview keeps iframe navigation isolated", () => {
  const guarded = injectHtmlPreviewNavigationGuard("<main>Preview</main>");
  assert.match(guarded, /<base href="about:blank">/);
  assert.match(guarded, /document\.addEventListener\('submit', block, true\)/);
  assert.match(guarded, /<body><main>Preview<\/main><\/body>/);
});

