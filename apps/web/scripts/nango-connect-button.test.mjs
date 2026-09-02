#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

const source = readFileSync(
  new URL("../src/components/integrations/NangoConnectButton.tsx", import.meta.url),
  "utf8",
);
const chatInputFooter = readFileSync(
  new URL("../src/components/ChatInputFooter.tsx", import.meta.url),
  "utf8",
);

test("popup close syncs exactly the connection and provider started by Nango", () => {
  const onSuccessStart = source.indexOf(
    "onSuccess: ({ nango_connect_url, connection_id, provider_config_key })",
  );
  const syncStart = source.indexOf("sync.mutate({");
  const syncEnd = source.indexOf("});", syncStart);
  const syncRequest = source.slice(syncStart, syncEnd);

  assert.ok(onSuccessStart >= 0, "start response should include the provider key");
  assert.ok(syncStart >= 0, "popup close should sync the expected connection");
  assert.match(syncRequest, /expected_connection_id: expectedConnectionId/);
  assert.match(syncRequest, /expected_provider_config_key: expectedProviderConfigKey/);
  assert.match(chatInputFooter, /navigate\(integrationSetupHref\(server\.server_key\)\)/);
  assert.match(chatInputFooter, /<NangoConnectButton/);
  assert.doesNotMatch(chatInputFooter, /nango\.startConnect|nango\.sync/);
});

test("fallback popup failure clears every pending connect state after the toast", () => {
  const onSuccessStart = source.indexOf(
    "onSuccess: ({ nango_connect_url, connection_id, provider_config_key })",
  );
  const failureStart = source.indexOf("if (!popup)", onSuccessStart);
  const failureEnd = source.indexOf("popup.location.href", failureStart);
  const failureBranch = source.slice(failureStart, failureEnd);
  const toastIndex = failureBranch.indexOf("toast.error(");

  assert.ok(onSuccessStart >= 0, "start success handler should exist");
  assert.ok(failureStart >= 0, "fallback popup failure branch should exist");
  assert.ok(failureEnd >= 0, "fallback branch should end before popup navigation");
  assert.ok(toastIndex >= 0, "fallback popup failure should show a toast");

  for (const reset of [
    "pendingPopupRef.current = null;",
    "expectedConnectionIdRef.current = null;",
    "expectedProviderConfigKeyRef.current = null;",
    "setPopupRef(null);",
  ]) {
    const resetIndex = failureBranch.indexOf(reset);
    assert.ok(resetIndex > toastIndex, `${reset} should run after the toast`);
  }
});

test("Embedded Signup preserves reconnect replacement identity", () => {
  const syncStart = source.indexOf("sync.mutate({");
  const syncEnd = source.indexOf("});", syncStart);
  const syncBody = source.slice(syncStart, syncEnd);

  assert.match(
    syncBody,
    /replace_integration_id:\s*replaceIntegrationId/,
    "Embedded Signup must keep reconnect scoped to the existing account",
  );
  assert.match(syncBody, /\.\.\.whatsappEmbeddedSignup/);
});

test("the shared connect flow has no post-OAuth WhatsApp selector", () => {
  assert.doesNotMatch(source, /finishWhatsAppSetup|setWhatsappSetup|selected_phone_number_id/);
  assert.match(source, /queryKey: INTEGRATION_CATALOG_QUERY_KEY/);
});
