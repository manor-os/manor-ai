import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const configSource = await readFile(
  new URL("../src/stores/config.ts", import.meta.url),
  "utf8",
);
const creditNoticeSource = await readFile(
  new URL("../src/components/ui/CreditLimitNotice.tsx", import.meta.url),
  "utf8",
);
const workspaceDraftPanelSource = await readFile(
  new URL("../src/components/WorkspaceDraftConfigurationPanel.tsx", import.meta.url),
  "utf8",
);

test("local config defaults AI credits to unlimited without masking config failures", () => {
  assert.match(configSource, /ai_credits_unlimited: boolean/);
  assert.match(configSource, /ai_credits_unlimited: true/);
  assert.match(configSource, /load_error: boolean/);
  assert.match(configSource, /loading: boolean/);
  assert.match(
    configSource,
    /catch \{[\s\S]*?set\(\{ loaded: false, loading: false, load_error: true \}\)/,
  );
  assert.doesNotMatch(configSource, /if \(!res\.ok\) \{[^}]*loaded: true/);
});

test("local unlimited policy suppresses only credit upgrade prompts", () => {
  assert.match(creditNoticeSource, /if \(creditsUnlimited\)/);
  assert.match(creditNoticeSource, /page\.settings\.unlimited_ai_credits/);
});

test("unlimited tenant credits do not disable an operator workspace budget", () => {
  assert.match(
    workspaceDraftPanelSource,
    /const hasEffectiveBudgetCap = Number\.isFinite\(budgetAmount\) && budgetAmount > 0/,
  );
  assert.doesNotMatch(workspaceDraftPanelSource, /!creditsUnlimited && Number\.isFinite/);
});
