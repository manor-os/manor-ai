import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const adminApp = await readFile(new URL("../src/admin/App.tsx", import.meta.url), "utf8");
const adminApi = await readFile(new URL("../src/admin/api.ts", import.meta.url), "utf8");
const settings = await readFile(new URL("../src/pages/Settings.tsx", import.meta.url), "utf8");

test("admin MFA gate upgrades an enrolled user's current session", () => {
  assert.match(adminApi, /mfaStepUp:\s*\(totpCode: string\)/);
  assert.match(adminApi, /\/auth\/mfa\/step-up/);
  assert.match(adminApp, /<MfaAccessGate enrolled=\{enrolled\}/);
  assert.match(adminApp, /await adminApi\.mfaStepUp\(code\)/);
  assert.match(adminApp, /localStorage\.setItem\("manor_token", result\.access_token\)/);
  assert.match(adminApp, /ariaLabel="Authentication code"/);
  assert.match(adminApp, /await onVerified\(\)/);
});

test("enabling 2FA immediately stores the returned MFA-bound token", () => {
  assert.match(settings, /const result = await api\.twoFactor\.verify\(code\.trim\(\)\)/);
  assert.match(settings, /localStorage\.setItem\("manor_token", result\.access_token\)/);
  assert.match(settings, /useAuthStore\.setState\(\{ token: result\.access_token \}\)/);
});
