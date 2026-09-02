import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";

const root = new URL("../", import.meta.url);
const read = path => readFileSync(new URL(path, root), "utf8");
const bundled = await build({
  stdin: {
    contents: `
      import { createElement } from 'react';
      import { renderToStaticMarkup } from 'react-dom/server';
      import { MemoryRouter } from 'react-router-dom';
      import IntegrationLogo from './src/components/IntegrationLogo';
      import { IconGoogle, IconYouTube } from './src/components/icons';
      import InlineIntegrationLink from './src/components/InlineIntegrationLink';
      export * from './src/lib/brands/catalog';
      export { additionalMarks } from './src/lib/brands/marks';
      export * from './src/lib/integrationSetupLinks';
      export const renderLogo = (provider) => renderToStaticMarkup(createElement(IntegrationLogo, { provider }));
      export const renderLegacyGoogle = () => [IconGoogle, IconYouTube].map(icon => renderToStaticMarkup(createElement(icon)));
      export const renderLink = (provider) => renderToStaticMarkup(createElement(MemoryRouter, {}, createElement(InlineIntegrationLink, { provider })));
    `,
    resolveDir: root.pathname,
    loader: "tsx",
  },
  bundle: true, format: "cjs", platform: "node", jsx: "automatic", write: false,
  loader: { ".css": "empty", ".svg": "file", ".png": "file", ".webp": "file" },
  outdir: new URL(".brand-test-output/", root).pathname,
  assetNames: "assets/[name]-[hash]", publicPath: "/",
  define: { "process.env.NODE_ENV": '"production"' },
});
const compiledModule = { exports: {} };
new Function("require", "module", "exports", bundled.outputFiles.find(file => file.path.endsWith(".js")).text)(
  createRequire(import.meta.url), compiledModule, compiledModule.exports,
);
const {
  INTEGRATION_BRANDS, additionalMarks, resolveIntegrationBrand, normalizeBrandKey,
  integrationSetupHref, parseIntegrationSetupLink, renderLogo, renderLink, renderLegacyGoogle,
} = compiledModule.exports;

// Read the production seed rather than maintaining a second provider catalog.
const seed = read("../../packages/core/services/mcp_seed.py").split("\n]\n")[0];
const managed = [
  ...[...seed.matchAll(/^    \("([^"]+)"/gm)].map(match => match[1]),
  ...[...seed.matchAll(/^    \(OfficialRemoteMCPProvider\.(\w+)\.value/gm)].map(match => match[1].toLowerCase()),
];
const functionalProviders = new Set([
  "email", "webhook", "manor_mcp_calendar", "manor_mcp_minutes", "manor_mcp_admin",
  "knowledge_local", "chrome_knowledge_local", "manor_mcp_file_engine",
]);

test("every managed provider has a brand mark or an explicit non-brand glyph", () => {
  assert.ok(managed.length >= 20, "seed parser must cover the Cloud or OSS catalog");
  for (const provider of managed) {
    const brand = resolveIntegrationBrand(provider);
    assert.ok(brand, `missing registry entry: ${provider}`);
    assert.match(brand.color, /^#[\da-f]{6}$/i);
    assert.ok(functionalProviders.has(provider) ? brand.glyph : brand.icon, provider);
    assert.match(renderLogo(provider), /data-integration-brand=/, provider);
  }
});

test("visual aliases share the exact mark but preserve setup provider identity", () => {
  for (const [alias, canonical] of [
    ["wechat_personal", "wechat"], ["wechat_official", "wechat"],
    ["tiktok_shop", "tiktok"], ["twitter_x", "x"], ["google_mail", "gmail"],
    ["chrome", "googlechrome"], ["codex_cli", "openai"], ["claude_code", "claude"],
    ["outlook", "microsoftoutlook"], ["ms_teams", "microsoftteams"],
    ["ms_excel", "microsoftexcel"], ["alpaca_market_data", "alpaca"],
    ["google_search", "google"], ["google_my_business", "googlebusinessprofile"],
    ["google_contacts", "googlecontacts"], ["gemini_cli", "googlegemini"],
  ]) {
    assert.strictEqual(resolveIntegrationBrand(alias)?.icon, resolveIntegrationBrand(canonical)?.icon, alias);
    assert.equal(resolveIntegrationBrand(alias)?.color, resolveIntegrationBrand(canonical)?.color, alias);
    assert.equal(parseIntegrationSetupLink(integrationSetupHref(alias), "https://manor.example"), alias);
  }
});

test("MCP and n8n connector identities resolve without guessing unknown brands", () => {
  assert.equal(normalizeBrandKey("mcp__google_drive__list_files"), "google_drive");
  assert.equal(normalizeBrandKey("n8n-nodes-base.gmailTool"), "gmail");
  assert.equal(normalizeBrandKey("n8n-nodes-base.microsoftOutlookTrigger"), "microsoftoutlook");
  for (const unknown of [null, "", "unknown-mcp", "constructor", "__proto__", "toString", "<svg onload=alert(1)>"]) {
    assert.equal(resolveIntegrationBrand(unknown), undefined);
    assert.match(renderLogo(unknown), /data-integration-brand="unknown"/);
  }
  assert.equal(resolveIntegrationBrand("email").icon, null, "generic mail must not impersonate Gmail");
});

test("brand assets are local, dimensioned, passive and covered by source records", () => {
  const sources = JSON.parse(read("src/lib/brands/sources.json"));
  const markNames = [...read("src/lib/brands/marks.ts").matchAll(/export const (si\w+):/g)].map(match => match[1]);
  for (const name of [...markNames, ...Object.keys(additionalMarks)]) {
    assert.match(sources[name]?.source || "", /^https:\/\//, `missing source: ${name}`);
  }
  for (const [key, brand] of Object.entries(INTEGRATION_BRANDS)) {
    if (!brand.icon) continue;
    const { path, src } = brand.icon;
    assert.ok(Boolean(path) !== Boolean(src), key);
    if (path) { assert.match(path, /^[Mm]/, key); continue; }
    if (src.startsWith("data:image/")) {
      assert.match(src, /^data:image\/(png|x-icon);base64,[\w+/=]+$/, key);
      const bytes = Buffer.from(src.split(",")[1], "base64");
      assert.ok(bytes.length > 40, key);
      assert.ok(bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) || bytes.subarray(0,4).equals(Buffer.from([0,0,1,0])), key);
      continue;
    }
    // esbuild retains Vite's import query; Vite's real URL emission is tested separately.
    const assetPath = src.replace(/\?no-inline$/, "");
    assert.match(assetPath, /^\/assets\/[a-z-]+-[\w-]+\.(svg|png|webp)$/, key);
    const asset = bundled.outputFiles.find(file => file.path.endsWith(assetPath));
    assert.ok(asset, `missing emitted asset: ${key}`);
    if (!assetPath.endsWith(".svg")) {
      const bytes = Buffer.from(asset.contents);
      if (assetPath.endsWith(".png")) {
        assert.ok(bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])), key);
      } else {
        assert.equal(bytes.toString("ascii", 0, 4), "RIFF", key);
        assert.equal(bytes.toString("ascii", 8, 12), "WEBP", key);
      }
      continue;
    }
    const svg = asset.text;
    assert.match(svg, /viewBox=/, key);
    assert.doesNotMatch(svg, /<!DOCTYPE|<script|<foreignObject|\bon\w+=|(?:href|src)=["']https?:/i, key);
  }
});

test("Google products use original full-color local artwork, including legacy icon wrappers", () => {
  for (const provider of [
    "google", "gmail", "google_calendar", "google_drive", "google_sheets", "google_docs",
    "googletasks", "googlechrome", "googlegemini", "youtube", "googlemaps",
    "googlecontacts", "googleplay", "googlebusinessprofile", "googlenews",
  ]) {
    const icon = resolveIntegrationBrand(provider)?.icon;
    assert.ok(icon?.src, `${provider} must use full-color artwork, not a single filled path`);
    assert.ok(!icon.monochrome, `${provider} must not be recolored in dark mode`);
    assert.match(renderLogo(provider), /<img /, provider);
    assert.doesNotMatch(renderLogo(provider), /data-monochrome|data-dark-ink|data-bright-ink/, provider);
  }
  for (const markup of renderLegacyGoogle()) {
    assert.match(markup, /<img [^>]*src="\/assets\//);
    assert.match(markup, /alt=""/);
  }
});

test("all integration surfaces reuse the catalog and shared renderer", () => {
  for (const file of ["src/components/InlineIntegrationLink.tsx", "src/components/ChatInputFooter.tsx", "src/pages/Integrations.tsx", "src/components/workflows/WorkflowCanvas.tsx"]) {
    const source = read(file);
    assert.match(source, /import IntegrationLogo from/, file);
    assert.match(source, /<IntegrationLogo/, file);
    assert.doesNotMatch(source, /\b(?:MCP_ICON|MCP_LOGO_COLOR|BRAND_SI|COMPOSER_INTEGRATION_ICON|COMPOSER_INTEGRATION_LOGO_COLOR)\b/, file);
  }
  assert.match(read("src/components/ChatInputFooter.tsx"), /COMPOSER_INTEGRATION_PROVIDERS\.has\(server\.server_key\)/);
  const legacy = read("src/components/icons.tsx");
  assert.match(legacy, /resolveIntegrationBrand\(brandKey\)/);
  assert.match(read("src/lib/workflowBrand.ts"), /resolveIntegrationBrand\(raw\)/);
});

test("monochrome logos remain legible on light and dark surfaces", () => {
  assert.match(renderLogo("anthropic"), /data-dark-ink="true"/);
  assert.match(renderLogo("slack"), /data-dark-ink="true"/);
  assert.match(renderLogo("mailchimp"), /data-bright-ink="true"/);
  const css = read("src/components/IntegrationLogo.css");
  assert.match(css, /html\[data-theme="dark"\] \.integration-logo\[data-dark-ink="true"\]/);
  assert.match(css, /fill: var\(--text-strong\)/);
});

test("inline chips retain provider names and destinations without external-link buttons", () => {
  for (const provider of managed) {
    const html = renderLink(provider);
    assert.ok(html.includes(`href="${integrationSetupHref(provider)}"`), provider);
    assert.match(html, /inline-file-reference-card__name/);
    assert.match(html, /aria-hidden="true"/);
    assert.doesNotMatch(html, /inline-file-reference-card__external|target="_blank"/);
  }
});
