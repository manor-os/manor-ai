import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const accountSource = await readFile(
  new URL("../src/pages/Account.tsx", import.meta.url),
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

  assert.match(roleFlags, /const canUseCatalogByok\s*=\s*\["image",\s*"video",\s*"stt"\]\.includes\(/);
  assert.match(roleFlags, /const canEditModelId\s*=\s*canUseCustomModel\s*\|\|\s*canUseCatalogByok/);
  assert.match(modelIdField, /disabled=\{!canEditModelId\}/);
  assert.match(modelIdField, /opacity:\s*canEditModelId\s*\?\s*1\s*:\s*0\.75/);
  assert.doesNotMatch(modelIdField, /disabled=\{!canUseCustomModel\}/);
});

test("saving catalog BYOK updates the selected role model", () => {
  const saveCatalogByok = between(
    "const handleSaveCatalogByok = async",
    "const handleClearApiKey = async",
  );

  assert.match(saveCatalogByok, /api\.auth\.updateMyModels\?\.\(\{\s*models:\s*\{\s*\[role\]:\s*draft\.model\.trim\(\)/);
  assert.match(saveCatalogByok, /models:\s*\{\s*\.\.\.\(prev\.models\s*\|\|\s*\{\}\),\s*\[role\]:\s*draft\.model\.trim\(\)/);
  assert.match(saveCatalogByok, /user_models:\s*\{\s*\.\.\.\(prev\.user_models\s*\|\|\s*\{\}\),\s*\[role\]:\s*draft\.model\.trim\(\)/);
});
