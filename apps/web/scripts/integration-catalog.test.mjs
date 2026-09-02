import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import test from "node:test";
import { build } from "esbuild";
import { QueryClient, QueryObserver } from "@tanstack/react-query";

const root = new URL("../", import.meta.url);
const read = path => readFileSync(new URL(path, root), "utf8");
const bundle = await build({
  stdin: {
    contents: `export * from './src/lib/integrationCatalog'; export { api } from './src/lib/api';`,
    resolveDir: root.pathname, loader: "ts",
  },
  bundle: true, format: "cjs", platform: "node", write: false,
  external: ["@tanstack/react-query"],
  plugins: [{
    name: "catalog-api-fixture",
    setup(context) {
      context.onResolve({ filter: /(?:^|\/)api$/ }, () => ({ path: "api", namespace: "fixture" }));
      context.onLoad({ filter: /.*/, namespace: "fixture" }, () => ({
        contents: "export const api = { integrations: { mcpServers: async () => [] } };", loader: "js",
      }));
    },
  }],
});
const compiled = { exports: {} };
new Function("require", "module", "exports", bundle.outputFiles[0].text)(createRequire(import.meta.url), compiled, compiled.exports);
const { integrationCatalogQueryOptions, INTEGRATION_CATALOG_QUERY_KEY, api } = compiled.exports;

test("Integrations and Chat deduplicate requests and share the fresh catalog", async () => {
  const client = new QueryClient();
  let calls = 0;
  let complete;
  api.integrations.mcpServers = () => {
    calls += 1;
    return new Promise(resolve => { complete = resolve; });
  };
  try {
    const integrations = client.fetchQuery(integrationCatalogQueryOptions(true));
    const chat = client.fetchQuery(integrationCatalogQueryOptions(true));
    assert.equal(calls, 1);
    complete([{ server_key: "gmail", agent_can_use: false }]);
    assert.strictEqual(await integrations, await chat);
    await client.fetchQuery(integrationCatalogQueryOptions(true));
    assert.equal(calls, 1, "fresh data should be reused for the same 60-second window");
    assert.equal(client.getQueryCache().getAll().length, 1);
  } finally { client.clear(); }
});

test("connect, repair and disconnect invalidation update every active catalog observer", async () => {
  const client = new QueryClient();
  let calls = 0;
  let ready = false;
  api.integrations.mcpServers = async () => {
    calls += 1;
    return [{ server_key: "gmail", agent_can_use: ready, user_connected: true }];
  };
  const integrations = new QueryObserver(client, integrationCatalogQueryOptions(true));
  const chat = new QueryObserver(client, integrationCatalogQueryOptions(true));
  const stopPage = integrations.subscribe(() => {});
  const stopChat = chat.subscribe(() => {});
  try {
    await client.fetchQuery(integrationCatalogQueryOptions(true));
    assert.equal(calls, 1);
    for (const usable of [true, false]) {
      ready = usable;
      await client.invalidateQueries({ queryKey: INTEGRATION_CATALOG_QUERY_KEY });
      assert.equal(integrations.getCurrentResult().data[0].agent_can_use, usable);
      assert.equal(chat.getCurrentResult().data[0].agent_can_use, usable);
    }
    assert.equal(calls, 3, "each state change must trigger one request, not one per surface");
    stopChat();
    ready = true;
    await client.invalidateQueries({ queryKey: INTEGRATION_CATALOG_QUERY_KEY });
    await client.fetchQuery(integrationCatalogQueryOptions(true));
    assert.equal(calls, 4, "reopening Chat reuses the catalog refreshed by Integrations");
    assert.equal(client.getQueryData(INTEGRATION_CATALOG_QUERY_KEY)[0].agent_can_use, true);
  } finally { stopPage(); stopChat(); client.clear(); }
});

test("unauthenticated or closed-menu observers do not load the private catalog", async () => {
  const client = new QueryClient();
  let calls = 0;
  api.integrations.mcpServers = async () => { calls += 1; return []; };
  const observer = new QueryObserver(client, integrationCatalogQueryOptions(false));
  const stop = observer.subscribe(() => {});
  try {
    await Promise.resolve();
    assert.equal(calls, 0);
    assert.equal(observer.getCurrentResult().fetchStatus, "idle");
  } finally { stop(); client.clear(); }
});

test("all three entry surfaces reuse catalog identity and the shared authorization UI", () => {
  const page = read("src/pages/Integrations.tsx");
  const chat = read("src/components/ChatInputFooter.tsx");
  const connect = read("src/components/integrations/NangoConnectButton.tsx");
  assert.match(page, /useQuery\(integrationCatalogQueryOptions\(privateApiEnabled\)\)/);
  assert.match(chat, /integrationCatalogQueryOptions\(privateApiEnabled && integrationsMenuOpen\)/);
  for (const source of [page, chat, connect]) assert.doesNotMatch(source, /composer-integrations/);
  for (const source of [page, connect]) {
    assert.match(source, /queryKey: INTEGRATION_CATALOG_QUERY_KEY/);
    assert.doesNotMatch(source, /queryKey: \["mcp-servers"\]/);
  }
  assert.match(chat, /navigate\(integrationSetupHref\(server\.server_key\)\)/);
  assert.match(chat, /if \(accessState === "connect" && server\.nango_provider_config_key\) \{\s+integrationsMenuButtonRef\.current\?\.focus\(\);\s+setNangoConnector\(server\);\s+return;/);
  assert.match(chat, /<NangoConnectButton\s+providerConfigKeys=\{\[nangoConnector\.nango_provider_config_key\]\}/);
  assert.match(chat, /onConnected=\{\(\) => setNangoConnector\(null\)\}/);
  assert.match(chat, /restoreFocusFallback=\{\(\) => integrationsMenuButtonRef\.current\?\.focus\(\)\}/);
  assert.doesNotMatch(chat, /startConnectorAuth|nango\.startConnect|oauthStart|chat-connector-modal|selectedConnector/);
  assert.match(chat, /integrationsError &&/);
  assert.match(chat, /onClick=\{\(\) => void refetchIntegrations\(\)\}/);
  assert.match(page, /<IntegrationLogo\s+provider=\{server\.server_key\}/);
  assert.match(page, /if \(!openOnLoad\) return;\s+openIntegrationDetail\(\)/);
});
