import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import {
  ConnectorAccountSelectionMode,
  ConnectorServerKind,
  connectorAccountError,
  connectorAccountSelectionOptions,
  connectorOperationInputSchema,
  connectorOperationOutputSchema,
  resolveConnectorServerKind,
} from "../src/lib/workflow-connector-accounts.mjs";

const integrationsSource = await readFile(
  new URL("../src/pages/Integrations.tsx", import.meta.url),
  "utf8",
);
const dropdownSource = await readFile(
  new URL("../src/components/ui/Dropdown.tsx", import.meta.url),
  "utf8",
);
const workflowNodeSource = await readFile(
  new URL("../src/components/workflows/WorkflowNodeConfigPanel.tsx", import.meta.url),
  "utf8",
);
const agentEditSource = await readFile(
  new URL("../src/components/AgentEditModal.tsx", import.meta.url),
  "utf8",
);

test("integration account rows render one combined status indicator", () => {
  assert.equal(
    [...integrationsSource.matchAll(/<ConnectionStatusPip\b/g)].length,
    2,
  );
  assert.doesNotMatch(integrationsSource, /<HealthPip\b|<WiringPip\b/);
});

test("an untested health check does not make a connected account look disconnected", () => {
  assert.match(
    integrationsSource,
    /credOk === false[\s\S]*?!connected[\s\S]*?wiring && wireOk === false[\s\S]*?"#54a176"/,
  );
  assert.match(
    integrationsSource,
    /if \(untested\)[\s\S]*?not_tested_yet_use_menu_test_connection/,
  );
});

test("an unavailable inbound check is distinct from an explicitly disabled webhook", () => {
  assert.match(
    integrationsSource,
    /wireOk === true[\s\S]*?inbound_registered[\s\S]*?wireOk === false[\s\S]*?inbound_not_working[\s\S]*?inbound_status_unavailable/,
  );
  assert.match(
    integrationsSource,
    /wireOk === null \|\| wireOk === undefined[\s\S]*?webhook_status_unavailable/,
  );
});

// A red "AUTH FAILED" chip tells the user something broke but not what,
// and the health check's detail — which names the actual cause, e.g. a
// username missing its domain or an app password being required — was
// only reachable by hovering for a native tooltip. Tooltips do not exist
// on touch, and nobody hovers a status chip on the off-chance. The
// reason has to be on screen next to the failure.
test("a failed account shows why it failed, not just that it failed", () => {
  assert.match(
    integrationsSource,
    /const failureDetail =[\s\S]*?health[\s\S]*?ok === false[\s\S]*?detail/,
  );
  // Rendered as page content, not only as a title attribute.
  assert.match(
    integrationsSource,
    /\{failureDetail && \([\s\S]*?\{failureDetail\}/,
  );
});

test("both account row kinds surface the reason, not just the entity one", () => {
  // EntityAccountRow (IMAP/SMTP, API keys) and ConnectionRow (OAuth) share
  // the same status pip and had the same blind spot. Fixing one and not
  // the other just moves the dead end.
  assert.equal(
    [...integrationsSource.matchAll(/const failureDetail =/g)].length,
    2,
  );
  assert.equal(
    [...integrationsSource.matchAll(/\{failureDetail && \(/g)].length,
    2,
  );
});

test("unavailable catalog accounts cannot look connected or expose runtime actions", () => {
  assert.match(
    integrationsSource,
    /const unavailable = !account\.runtime_callable[\s\S]*?connected=\{!unavailable && account\.status === "active"\}/,
  );
  assert.match(
    integrationsSource,
    /const unavailable = !connection\.runtime_callable[\s\S]*?connected=\{!unavailable && !expired\}/,
  );
  assert.match(
    integrationsSource,
    /showActions[\s\S]*?account\.runtime_callable[\s\S]*?key: "test"/,
  );
  assert.match(
    integrationsSource,
    /showActions && connection\.runtime_callable[\s\S]*?key: "test"/,
  );
  assert.match(integrationsSource, /availability === "load_failed"/);
});

test("shared callable accounts can become my default without management actions", () => {
  assert.equal(
    [...integrationsSource.matchAll(/const canSetDefault =/g)].length,
    2,
  );
  assert.equal(
    [...integrationsSource.matchAll(/\{\(showActions \|\| canSetDefault\) && \(/g)].length,
    2,
  );
  assert.equal(
    [...integrationsSource.matchAll(/\.\.\.\(canSetDefault/g)].length,
    2,
  );
});

test("the failure reason stays readable instead of being clipped to one line", () => {
  // The row label uses nowrap + ellipsis; the detail must not inherit it
  // or a multi-cause hint becomes "IMAP login failed: b'[AUTHENTICA…".
  assert.match(
    integrationsSource,
    /\{failureDetail && \([\s\S]*?whiteSpace: "normal"/,
  );
});

test("an open integration detail refreshes when account health changes", () => {
  // DetailDrawer stores a React-node snapshot. A successful manual test
  // refreshes mcp-servers, so the detail-rebuild effect must observe the
  // nested account arrays or the drawer keeps showing the previous failure.
  assert.match(
    integrationsSource,
    /useEffect\(\(\) => \{[\s\S]*?currentDetailKey !== detailKey[\s\S]*?openIntegrationDetail\(\)[\s\S]*?server\.connections,[\s\S]*?server\.entity_accounts,/,
  );
});

// The provider badge said "ready" while the account under it said AUTH
// FAILED. "ready" is agent_can_use, which the backend now clears when a
// provider has actually refused the credentials — so the label resolves
// to "needs attention" on its own. The dot has to follow it, or the card
// shows a green light next to a warning.
test("the provider status dot agrees with the label it sits next to", () => {
  assert.match(
    integrationsSource,
    /const statusColor = server\.requires_explicit_account[\s\S]*?"#cf9b44"[\s\S]*?server\.agent_can_use[\s\S]*?"#d6d3d1"/,
  );
  assert.doesNotMatch(
    integrationsSource,
    /const statusColor = isReadyConnection \?/,
  );
});

test("partial account registries stay callable but require an exact account", () => {
  assert.match(
    agentEditSource,
    /requiresExplicitAccount \? "Account required" : ready \? "Ready"/,
  );
  assert.match(
    workflowNodeSource,
    /runtime_callable !== false[\s\S]*?connectorAccountSelectionOptions\(\{/,
  );
  assert.match(workflowNodeSource, /connectorAccountError\(\{/);
  assert.match(
    workflowNodeSource,
    /requiresExplicitAccount:\s*\([\s\S]*?connectorServerInfo\?\.requires_explicit_account[\s\S]*?catalogSelectedOperation\?\.requires_explicit_account[\s\S]*?\)/,
  );
  assert.match(connectorAccountError({
    serverKey: "gmail",
    serverKind: ConnectorServerKind.MANAGED,
    accountCatalogStatus: "ready",
    serverCatalogPresent: true,
    requiresExplicitAccount: true,
  }), /Select a callable account/);
  assert.match(
    workflowNodeSource,
    /disabled=\{running[\s\S]*?connectorAccountInvalid[\s\S]*?<Button onClick=\{save\} disabled=\{[\s\S]*?connectorAccountInvalid/,
  );
});

test("agent capability summary counts MCP servers while details count actions", () => {
  assert.match(
    agentEditSource,
    /const selectedMcpServerCount = Array\.from\(mcpToolsByServer\.values\(\)\)\.filter\([\s\S]*?tools\.some\([\s\S]*?selectedToolIdSet\.has\(tool\.id\)/,
  );
  assert.match(
    agentEditSource,
    /label: "MCP",\s*count: selectedMcpServerCount/,
  );
  assert.match(
    agentEditSource,
    /\{selectedMcpActionCount\}\/\{mcpActionCount\} actions selected/,
  );
});

test("workflow account selection supports default, exact, and read-only all", () => {
  const accounts = [
    { value: "account-1", label: "Primary" },
    { value: "account-2", label: "Secondary" },
  ];
  const readOptions = connectorAccountSelectionOptions({
    accounts,
    operation: { effect: "read", supports_all_accounts: true },
  });
  assert.deepEqual(readOptions, [
    { value: "", label: "Default account" },
    { value: ConnectorAccountSelectionMode.ALL, label: "All connected accounts" },
    ...accounts,
  ]);
  assert.deepEqual(connectorAccountSelectionOptions({
    accounts,
    operation: { effect: "write", supports_all_accounts: true },
  }), [{ value: "", label: "Default account" }, ...accounts]);
  assert.deepEqual(connectorAccountSelectionOptions({
    accounts,
    requiresExplicitAccount: true,
    operation: { effect: "read", supports_all_accounts: true },
  }), accounts);
  const oneAccount = [{ value: "account-1", label: "Primary" }];
  assert.deepEqual(connectorAccountSelectionOptions({
    accounts: oneAccount,
    selectedAccountSelection: ConnectorAccountSelectionMode.ALL,
    operation: { effect: "read", supports_all_accounts: true },
  }), [
    { value: "", label: "Default account" },
    { value: ConnectorAccountSelectionMode.ALL, label: "All connected accounts" },
    ...oneAccount,
  ]);
  assert.match(connectorAccountError({
    serverKey: "gmail",
    serverKind: ConnectorServerKind.MANAGED,
    accountCatalogStatus: "ready",
    serverCatalogPresent: true,
    selectedAccountSelection: ConnectorAccountSelectionMode.ALL,
    callableAccountIds: accounts.map((account) => account.value),
    operation: { effect: "write", supports_all_accounts: true },
  }), /All connected accounts/);
  assert.match(
    workflowNodeSource,
    /integration_account_selection[\s\S]*?ConnectorAccountSelectionMode\.ALL/,
  );
  assert.match(
    workflowNodeSource,
    /accountSelectionOptions\.length > 2\s*\|\|\s*selectedAccountSelectionMode !== ConnectorAccountSelectionMode\.DEFAULT/,
  );
});

test("workflow connector output contracts follow the selected account", () => {
  const operation = {
    output_schema: { type: "object", required: ["id"] },
    account_output_schemas: {
      secondary: { type: "object", required: ["updated"] },
    },
  };
  assert.deepEqual(
    connectorOperationOutputSchema(operation, "secondary"),
    operation.account_output_schemas.secondary,
  );
  assert.deepEqual(
    connectorOperationOutputSchema(operation, "default"),
    operation.output_schema,
  );
  assert.match(workflowNodeSource, /connectorOperationOutputSchema\(/);
  assert.match(workflowNodeSource, /\.output_schema\s*=/);
});

test("native connector nodes require an operation before save or test", () => {
  assert.match(workflowNodeSource, /const connectorOperationInvalid =/);
  assert.match(
    workflowNodeSource,
    /disabled=\{running[\s\S]*?connectorOperationInvalid[\s\S]*?<Button onClick=\{save\} disabled=\{[\s\S]*?connectorOperationInvalid/,
  );
});

test("workflow account labels use ownership instead of connection storage type", () => {
  assert.match(
    workflowNodeSource,
    /account\.ownership === "shared" \? "Shared" : "Mine"/,
  );
  assert.match(
    workflowNodeSource,
    /display_name[^\n]*accountOwnershipLabel\(account\)[^\n]*· Personal/,
  );
  assert.match(
    workflowNodeSource,
    /display_name[^\n]*accountOwnershipLabel\(account\)[^\n]*· Entity/,
  );
});

test("custom workflow connectors stay editable when the account catalog is unavailable", () => {
  assert.equal(resolveConnectorServerKind({
    serverKey: "customer-mcp",
    operationCatalogKind: ConnectorServerKind.CUSTOM,
    operationCatalogSource: "builtin",
  }), ConnectorServerKind.CUSTOM);
  assert.equal(connectorAccountError({
    serverKey: "customer-mcp",
    serverKind: ConnectorServerKind.CUSTOM,
    accountCatalogStatus: "error",
  }), "");
  assert.equal(resolveConnectorServerKind({
    serverKey: "customer-mcp",
    persistedKind: ConnectorServerKind.CUSTOM,
  }), ConnectorServerKind.CUSTOM);
  assert.equal(resolveConnectorServerKind({
    serverKey: "customer-mcp",
    serverCatalogKind: ConnectorServerKind.CUSTOM,
  }), ConnectorServerKind.CUSTOM);
  assert.match(
    workflowNodeSource,
    /serverCatalogKind: connectorServerInfo\?\.server_kind/,
  );
  assert.doesNotMatch(workflowNodeSource, /managedServerKeys/);
  assert.match(
    workflowNodeSource,
    /disabled=\{running[\s\S]*?connectorAccountInvalid[\s\S]*?<Button onClick=\{save\} disabled=\{[\s\S]*?connectorAccountInvalid/,
  );
});

test("managed and unclassified workflow connectors fail closed during account loading", () => {
  const managed = resolveConnectorServerKind({
    serverKey: "gmail",
    operationCatalogKind: ConnectorServerKind.MANAGED,
  });
  assert.equal(managed, ConnectorServerKind.MANAGED);
  assert.equal(resolveConnectorServerKind({
    serverKey: "gmail",
    operationCatalogSource: "account_discovery",
  }), ConnectorServerKind.MANAGED);
  assert.equal(resolveConnectorServerKind({
    serverKey: "gmail",
    operationCatalogKind: ConnectorServerKind.MANAGED,
    persistedKind: ConnectorServerKind.CUSTOM,
  }), ConnectorServerKind.MANAGED);
  assert.equal(resolveConnectorServerKind({
    serverKey: "customer-mcp",
    operationCatalogKind: ConnectorServerKind.CUSTOM,
    managedServerKeys: ["customer-mcp"],
  }), ConnectorServerKind.CUSTOM);
  assert.match(connectorAccountError({
    serverKey: "gmail",
    serverKind: managed,
    accountCatalogStatus: "loading",
  }), /still loading/);
  assert.match(connectorAccountError({
    serverKey: "gmail",
    serverKind: managed,
    accountCatalogStatus: "error",
  }), /could not be validated/);
  assert.match(connectorAccountError({
    serverKey: "gmail",
    serverKind: managed,
    accountCatalogStatus: "ready",
  }), /accounts for this integration are unavailable/);
  assert.match(connectorAccountError({
    serverKey: "unresolved",
    serverKind: ConnectorServerKind.UNKNOWN,
    accountCatalogStatus: "ready",
  }), /could not be classified/);
});

test("managed workflow connectors validate exact callable accounts", () => {
  const base = {
    serverKey: "gmail",
    serverKind: ConnectorServerKind.MANAGED,
    accountCatalogStatus: "ready",
    serverCatalogPresent: true,
    callableAccountIds: ["account-1"],
  };
  assert.match(connectorAccountError({
    ...base,
    requiresExplicitAccount: true,
  }), /Select a callable account/);
  assert.equal(connectorAccountError({
    ...base,
    requiresExplicitAccount: true,
    selectedAccountId: "account-1",
  }), "");
  assert.match(connectorAccountError({
    ...base,
    selectedAccountId: "account-2",
  }), /selected account is unavailable/);
});

test("workflow connector fields follow the selected account contract", () => {
  const fallback = {
    type: "object",
    properties: { title: { type: "string" } },
    required: ["title"],
  };
  const secondary = {
    type: "object",
    properties: { record_id: { type: "string" } },
    required: ["record_id"],
  };
  const operation = {
    input_schema: fallback,
    account_input_schemas: { "account-2": secondary },
  };

  assert.equal(connectorOperationInputSchema(operation, "account-2"), secondary);
  assert.equal(connectorOperationInputSchema(operation, "account-1"), fallback);
  assert.match(
    workflowNodeSource,
    /connectorOperationInputSchema\([\s\S]*?selectedOperation,[\s\S]*?selectedAccount/,
  );
});

test("the integration row overflow menu uses an accessible button", () => {
  assert.match(
    integrationsSource,
    /<button[\s\S]*?className="integration-row-menu-trigger"[\s\S]*?aria-label=\{t\("action\.more"\)\}/,
  );
});

test("dropdown menus render above detail and modal surfaces", () => {
  assert.match(dropdownSource, /zIndex:\s*20010/);
  assert.match(
    dropdownSource,
    /closest<HTMLElement>\([\s\S]*?\.detail-scrim, \.manor-dialog-overlay[\s\S]*?portalTarget[\s\S]*?createPortal\([\s\S]*?portalTarget/,
  );
});
