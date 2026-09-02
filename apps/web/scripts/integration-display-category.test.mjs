import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

const webRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const read = (relativePath) => readFile(path.join(webRoot, relativePath), "utf8");

const [integrations, english, chinese] = await Promise.all([
  read("src/pages/Integrations.tsx"),
  read("src/lib/i18n/en.ts"),
  read("src/lib/i18n/zh.ts"),
]);

test("integration cards use a dedicated social and marketing category", () => {
  assert.match(integrations, /\| "social_marketing"/);
  assert.match(integrations, /social: "social_marketing"/);
  assert.match(integrations, /marketing: "social_marketing"/);
  assert.match(integrations, /tavily: "ai_media"/);
  assert.match(
    english,
    /"page\.integrations\.category_social_marketing": "Social & marketing"/,
  );
  assert.match(
    chinese,
    /"page\.integrations\.category_social_marketing": "社交与营销"/,
  );
});
