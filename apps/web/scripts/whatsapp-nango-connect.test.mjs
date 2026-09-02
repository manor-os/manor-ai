import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const source = await readFile(
  new URL("../src/pages/Integrations.tsx", import.meta.url),
  "utf8",
);
const apiSource = await readFile(
  new URL("../src/lib/api.ts", import.meta.url),
  "utf8",
);
const buttonSource = await readFile(
  new URL("../src/components/integrations/NangoConnectButton.tsx", import.meta.url),
  "utf8",
);
const english = await readFile(new URL("../src/lib/i18n/en.ts", import.meta.url), "utf8");
const spanish = await readFile(new URL("../src/lib/i18n/es.ts", import.meta.url), "utf8");
const chinese = await readFile(new URL("../src/lib/i18n/zh.ts", import.meta.url), "utf8");
const mcpSeed = await readFile(
  new URL("../../../packages/core/services/mcp_seed.py", import.meta.url),
  "utf8",
);
const nangoOAuth = await readFile(
  new URL("../../../apps/api/routers/nango_oauth.py", import.meta.url),
  "utf8",
);
const whatsappConfig = await readFile(
  new URL(
    "../../../packages/core/services/whatsapp_business_config.py",
    import.meta.url,
  ),
  "utf8",
);

test("WhatsApp prefers Nango Connect and does not expose a new credential form", () => {
  const whatsappBranch = source.indexOf('server.server_key === "whatsapp"');
  const nangoBranch = source.indexOf(
    "server.nango_provider_config_key && !server.entity_connected",
    whatsappBranch,
  );
  const whatsappBranchEnd = source.indexOf(
    ") : server.nango_provider_config_key && !server.entity_connected",
    nangoBranch,
  );
  const credentialBranch = source.indexOf(": isCredentials ?", nangoBranch);

  assert.ok(whatsappBranch >= 0, "the detail action should have a WhatsApp branch");
  assert.ok(nangoBranch >= 0, "the detail action should have a Nango branch");
  assert.ok(whatsappBranchEnd > nangoBranch, "the WhatsApp branch should be bounded");
  assert.ok(credentialBranch > nangoBranch, "legacy credential branch remains below Nango");
  assert.match(source, /:\s*server\.server_key === "whatsapp"\s*\?/);
  assert.doesNotMatch(
    source.slice(whatsappBranch, whatsappBranchEnd),
    /onAddApiKey/,
    "WhatsApp Business must not expose manual credential creation",
  );
  assert.match(
    source,
    /whatsapp[\s\S]*?oauth_not_configured/,
    "an unconfigured WhatsApp provider must show a deployment blocker",
  );
});

test("Nango-backed WhatsApp accounts do not expose the legacy edit action", () => {
  assert.match(
    source,
    /serverKey === "whatsapp"\s*&&\s*account\.nango_backed/,
    "the account menu must identify Nango-backed WhatsApp accounts",
  );
  assert.match(
    source,
    /!\(serverKey === "whatsapp"\s*&&\s*account\.nango_backed\)[\s\S]*?key: "edit"/,
    "the legacy Edit action must be excluded for Nango-backed WhatsApp",
  );
});

test("the single WhatsApp catalog product is named WhatsApp Business", () => {
  for (const locale of [english, spanish, chinese]) {
    assert.match(locale, /"page\.integrations\.whatsapp": "WhatsApp Business"/);
  }
  assert.equal(mcpSeed.match(/\("whatsapp",/g)?.length, 1);
  assert.match(mcpSeed, /\("whatsapp", "WhatsApp Business",/);
});

test("WhatsApp Nango Connect consumes trusted Embedded Signup results", () => {
  assert.match(apiSource, /whatsapp_waba_id\?: string/);
  assert.match(apiSource, /whatsapp_phone_number_id\?: string/);
  assert.match(apiSource, /provisioning_pending: boolean/);
  assert.match(apiSource, /provisioning_detail: string \| null/);
  assert.match(buttonSource, /window\.addEventListener\("message"/);
  assert.match(buttonSource, /https:\/\/www\.facebook\.com/);
  assert.match(buttonSource, /https:\/\/web\.facebook\.com/);
  assert.match(
    buttonSource,
    /if\s*\(\s*!pendingPopupRef\.current\s*\|\|\s*event\.source/,
  );
  assert.match(buttonSource, /event\.source\s*!==\s*pendingPopupRef\.current/);
  assert.match(buttonSource, /WA_EMBEDDED_SIGNUP/);
  assert.match(buttonSource, /embeddedSignup\.event\s*!==\s*"FINISH"/);
  assert.match(buttonSource, /embeddedSignup\.event\s*===\s*"CANCEL"/);
  assert.match(buttonSource, /embeddedSignup\.event\s*===\s*"ERROR"/);
  assert.match(buttonSource, /whatsappEmbeddedSignupOutcomeRef/);
  assert.doesNotMatch(buttonSource, /FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING/);
  assert.match(buttonSource, /whatsapp_waba_id/);
  assert.match(buttonSource, /whatsapp_phone_number_id/);
  assert.doesNotMatch(buttonSource, /NangoWhatsAppSetupPanel/);
  assert.doesNotMatch(buttonSource, /selected_phone_number_id/);
  assert.match(buttonSource, /provisioning_pending/);
  assert.match(buttonSource, /queryKey:\s*\["channel-bindings"\]/);
});

test("WhatsApp requires a v4 Cloud API Embedded Signup deployment config", () => {
  assert.match(whatsappConfig, /NANGO_PROVIDER_WHATSAPP_CONFIG_ID/);
  assert.match(nangoOAuth, /load_whatsapp_business_config/);
  assert.match(nangoOAuth, /authorization_params\[config_id\]/);
  assert.match(buttonSource, /embeddedSignup\.event\s*!==\s*"FINISH"/);
  assert.doesNotMatch(buttonSource, /FINISH_WHATSAPP_BUSINESS_APP_ONBOARDING/);
});

test("WhatsApp provisioning distinguishes ready, pending, and PIN recovery", () => {
  assert.match(apiSource, /integration_id: string \| null/);
  assert.match(apiSource, /readiness_code: string \| null/);
  assert.match(apiSource, /retryWhatsAppProvisioning/);
  assert.match(apiSource, /registration_pin: string/);
  assert.match(buttonSource, /readiness_code/);
  assert.match(buttonSource, /phone_not_registered/);
  assert.match(source, /whatsapp_readiness_code/);
  assert.match(source, /registration_pin/);
  assert.match(source, /inputMode="numeric"/);
  assert.match(source, /autoComplete="one-time-code"/);
  assert.match(source, /maxLength=\{6\}/);
});

test("WhatsApp account health renders an exact readiness checklist", () => {
  assert.match(apiSource, /reason_code: string \| null/);
  assert.match(apiSource, /checks: Record<string, HealthCheckStatus> \| null/);
  assert.match(source, /WhatsAppReadinessChecklist/);
  for (const key of [
    "oauth",
    "assets",
    "phone_registration",
    "app_subscription",
    "callback",
  ]) {
    assert.match(source, new RegExp(`"${key}"`));
  }
  assert.match(source, /whatsapp_check_\$\{key\}/);
  assert.match(
    source,
    /serverKey !== "whatsapp"[\s\S]*?key: "register-webhook"/,
    "WhatsApp readiness must not expose the generic webhook repair action",
  );
  for (const locale of [english, spanish, chinese]) {
    assert.match(locale, /"page\.integrations\.whatsapp_account_ready"/);
    assert.match(locale, /"page\.integrations\.whatsapp_check_callback"/);
  }
});
