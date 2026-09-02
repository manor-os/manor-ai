import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { build } from "esbuild";

const bundled = await build({
  entryPoints: [new URL("../src/lib/integrationSetupLinks.ts", import.meta.url).pathname],
  bundle: true, format: "esm", platform: "node", write: false,
});
const { integrationSetupHref, parseIntegrationSetupLink } = await import(
  `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString("base64")}`
);
const origin = "https://manor.example";

test("setup links preserve exact provider keys and accept only internal setup destinations", () => {
  for (const provider of ["gmail", "google_calendar", "chrome", "custom-mcp", "custom_MCP"]) {
    const href = integrationSetupHref(provider);
    assert.equal(parseIntegrationSetupLink(href, origin), provider);
    assert.equal(parseIntegrationSetupLink(`${origin}${href}`, origin), provider);
  }
});

test("untrusted external, malformed and ambiguous links never become setup chips", () => {
  for (const href of [
    "https://evil.example/integrations?provider=gmail", "//evil.example/integrations?provider=gmail",
    "https://user:secret@manor.example/integrations?provider=gmail",
    "/integrations?provider=gmail&provider=chrome", "/integrations?provider=",
    "/integrations?provider=..%2Fgmail", "/integrations?provider=gmail%0A",
    "/integrations/oauth/start?provider=gmail", "/integrations", "javascript:alert(1)",
    `/integrations?provider=${"a".repeat(129)}`,
  ]) assert.equal(parseIntegrationSetupLink(href, origin), null, href);
});

const read = (file) => readFileSync(new URL(`../src/${file}`, import.meta.url), "utf8");
test("all chat renderers reuse the branded link and existing configuration drawer", () => {
  assert.match(read("components/ChatMarkdown.tsx"), /parseIntegrationSetupLink\(href, window\.location\.origin\)/);
  assert.match(read("components/ChatMarkdown.tsx"), /<InlineIntegrationLink provider=/);
  const link = read("components/InlineIntegrationLink.tsx");
  assert.match(link, /<IntegrationLogo provider=\{provider\}/);
  assert.match(link, /<Link/);
  assert.match(link, /to=\{integrationSetupHref\(provider\)\}/);
  assert.match(link, /inline-file-reference-card__name/);
  assert.doesNotMatch(link, /IconExternalLink|inline-file-reference-card__external/);
  assert.doesNotMatch(link, /oauthStart|mutation|window\.open|dangerouslySetInnerHTML/);
  const page = read("pages/Integrations.tsx");
  assert.match(page, /server\.server_key === requestedProvider/);
  assert.match(page, /if \(!openOnLoad\) return;\s+openIntegrationDetail\(\)/);
  assert.match(page, /setup_link_unavailable/);
  assert.match(page, /if \(useDetailStore\.getState\(\)\.payload\?\.key === detailKey\) closeDetail\(\)/);
});
