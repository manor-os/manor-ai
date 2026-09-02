#!/usr/bin/env node
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  connectorAccessState,
  integrationReadyCount,
  integrationStoredConnectionCount,
} from "../src/lib/integration-usability.mjs";

const chatFooter = readFileSync(
  new URL("../src/components/ChatInputFooter.tsx", import.meta.url),
  "utf8",
);
const integrations = readFileSync(
  new URL("../src/pages/Integrations.tsx", import.meta.url),
  "utf8",
);

test("connector access state distinguishes usable, repair, and connect", () => {
  const states = [
    connectorAccessState({
      agent_can_use: true,
      user_connected: false,
      entity_connected: false,
    }),
    connectorAccessState({
      agent_can_use: false,
      user_connected: true,
      entity_connected: false,
    }),
    connectorAccessState({
      agent_can_use: false,
      user_connected: false,
      entity_connected: true,
    }),
    connectorAccessState({
      agent_can_use: false,
      connections: [{ id: "expired-oauth" }],
    }),
    connectorAccessState({
      agent_can_use: false,
      entity_accounts: [{ id: "rejected-credential" }],
    }),
    connectorAccessState({
      agent_can_use: false,
      user_connected: false,
      entity_connected: false,
    }),
  ];

  assert.deepEqual(states, [
    "usable",
    "repair",
    "repair",
    "repair",
    "repair",
    "connect",
  ]);
});

test("readiness counts only integrations the Agent can use", () => {
  const servers = [
    { agent_can_use: true, user_connected: true },
    { agent_can_use: false, user_connected: true },
    { agent_can_use: false, connections: [{ id: "expired" }] },
    { agent_can_use: false },
  ];

  assert.equal(integrationReadyCount(servers), 1);
  assert.equal(integrationStoredConnectionCount(servers), 3);
});

test("composer inserts use hints only for usable connectors", () => {
  assert.match(chatFooter, /const accessState = connectorAccessState\(server\)/);
  assert.match(chatFooter, /if \(accessState === "usable"\)/);
  assert.match(chatFooter, /navigate\(integrationSetupHref\(server\.server_key\)\)/);
  assert.doesNotMatch(chatFooter, /startConnectorAuth|oauthStart|nango\.startConnect/);
  assert.doesNotMatch(
    chatFooter,
    /const connected = Boolean\([\s\S]{0,120}server\.user_connected/,
  );
});

test("Google and generic cards use Agent usability for ready state", () => {
  assert.match(
    integrations,
    /const readyCount = integrationReadyCount\(subs\)/,
  );
  assert.match(
    integrations,
    /const hasUsableConnection = accessState === "usable"/,
  );
  assert.match(
    integrations,
    /isOAuth && server\.oauth_configured && !server\.agent_can_use/,
  );
  assert.match(
    integrations,
    /user_connected: anyUserConnected,[\s\S]{0,100}entity_connected: anyEntityConnected/,
  );
});
