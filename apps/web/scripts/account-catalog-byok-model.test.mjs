import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const accountSource = await readFile(
  new URL("../src/pages/Account.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);

function between(startMarker, endMarker) {
  const start = accountSource.indexOf(startMarker);
  const end = accountSource.indexOf(endMarker, start);
  assert.notEqual(start, -1, `Missing start marker: ${startMarker}`);
  assert.notEqual(end, -1, `Missing end marker: ${endMarker}`);
  return accountSource.slice(start, end);
}

test("catalog BYOK roles can edit the model id field", () => {
  const roleFlags = between(
    "const canUseCustomModel =",
    "const apiKeyError =",
  );
  const modelIdField = between("{/* Model ID */}", "{/* API Key + Base URL */}");

  assert.match(roleFlags, /const canUseCatalogByok\s*=\s*\["image",\s*"video",\s*"voice",\s*"stt"\]\.includes\(/);
  assert.match(roleFlags, /const canEditModelId\s*=\s*canUseCustomModel\s*\|\|\s*canUseCatalogByok/);
  assert.match(modelIdField, /disabled=\{!canEditModelId\}/);
  assert.match(modelIdField, /opacity:\s*canEditModelId\s*\?\s*1\s*:\s*0\.75/);
  assert.doesNotMatch(modelIdField, /disabled=\{!canUseCustomModel\}/);
});

test("saving catalog BYOK uses one atomic model and credential request", () => {
  const saveCatalogByok = between(
    "const handleSaveCatalogByok = async",
    "const handleClearApiKey = async",
  );

  assert.match(saveCatalogByok, /api\.auth\.saveCatalogModel\(\{/);
  assert.match(saveCatalogByok, /role,\s*model:\s*draft\.model\.trim\(\)/);
  assert.match(saveCatalogByok, /api_key:\s*draft\.apiKey\.trim\(\)\s*\|\|\s*undefined/);
  assert.match(saveCatalogByok, /use_saved_api_key:/);
  assert.match(saveCatalogByok, /base_url:\s*draft\.baseUrl\.trim\(\)/);
  assert.doesNotMatch(saveCatalogByok, /saveLlmApiKey|saveLlmBaseUrl|updateMyModels/);
  assert.match(saveCatalogByok, /models:\s*\{\s*\.\.\.\(prev\.models\s*\|\|\s*\{\}\),\s*\[role\]:\s*draft\.model\.trim\(\)/);
  assert.match(saveCatalogByok, /user_models:\s*\{\s*\.\.\.\(prev\.user_models\s*\|\|\s*\{\}\),\s*\[role\]:\s*draft\.model\.trim\(\)/);
});

test("API client exposes the atomic catalog model settings contract", () => {
  assert.match(apiSource, /saveCatalogModel:\s*\(data:/);
  assert.match(apiSource, /"\/auth\/me\/models\/catalog"/);
  assert.match(apiSource, /clear_api_key\?:\s*boolean/);
});

test("catalog BYOK model changes replace only provider-derived base URLs", () => {
  const modelIdField = between("{/* Model ID */}", "{/* API Key + Base URL */}");
  const compactField = modelIdField.replace(/\s+/g, " ");

  assert.match(modelIdField, /const previousAutoUrl = inferBaseUrl\(draft\.model\)/);
  assert.ok(
    compactField.includes(
      'const shouldReplaceBaseUrl = !draft.baseUrl || draft.baseUrl.replace(/\\/+$/, "") === previousAutoUrl;',
    ),
  );
});

test("Kimi catalog BYOK defaults to the international native endpoint", () => {
  const providerUrls = between(
    "const PROVIDER_BASE_URLS:",
    "const inferBaseUrl =",
  );

  assert.match(
    providerUrls,
    /moonshotai:\s*"https:\/\/api\.moonshot\.ai\/v1"/,
  );
  assert.doesNotMatch(
    providerUrls,
    /moonshotai:\s*"https:\/\/api\.moonshot\.cn\/v1"/,
  );
});
