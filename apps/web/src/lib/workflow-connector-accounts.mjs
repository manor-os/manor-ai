export const ConnectorServerKind = Object.freeze({
  MANAGED: "managed",
  CUSTOM: "custom",
  UNKNOWN: "unknown",
});

export const ConnectorAccountSelectionMode = Object.freeze({
  DEFAULT: "default",
  EXACT: "exact",
  ALL: "all",
});

const knownKinds = new Set([
  ConnectorServerKind.MANAGED,
  ConnectorServerKind.CUSTOM,
]);

export function resolveConnectorServerKind({
  serverKey,
  operationCatalogKind,
  serverCatalogKind,
  operationCatalogSource,
  persistedKind,
}) {
  if (!String(serverKey || "").trim()) return ConnectorServerKind.UNKNOWN;
  if (knownKinds.has(operationCatalogKind)) return operationCatalogKind;
  if (knownKinds.has(serverCatalogKind)) return serverCatalogKind;
  // Source remains a rolling-deploy fallback for an older API response.
  if (operationCatalogSource === "builtin") return ConnectorServerKind.MANAGED;
  if (operationCatalogSource === "account_discovery") return ConnectorServerKind.MANAGED;
  if (operationCatalogSource === "cache") return ConnectorServerKind.CUSTOM;
  if (knownKinds.has(persistedKind)) return persistedKind;
  return ConnectorServerKind.UNKNOWN;
}

export function connectorAccountError({
  serverKey,
  serverKind,
  accountCatalogStatus,
  serverCatalogPresent = false,
  requiresExplicitAccount = false,
  selectedAccountId = "",
  selectedAccountSelection = "",
  callableAccountIds = [],
  operation,
}) {
  if (!String(serverKey || "").trim() || serverKind === ConnectorServerKind.CUSTOM) {
    return "";
  }
  if (accountCatalogStatus === "loading") {
    return "Connected accounts are still loading. Wait before saving or testing this connector.";
  }
  if (accountCatalogStatus === "error") {
    return "Connected accounts could not be validated. Reload integrations before saving or testing this connector.";
  }
  if (serverKind === ConnectorServerKind.UNKNOWN) {
    return "This connector could not be classified. Reload integrations before saving or testing this connector.";
  }
  if (!serverCatalogPresent) {
    return "Connected accounts for this integration are unavailable. Reload integrations before saving or testing this connector.";
  }
  if (requiresExplicitAccount && !selectedAccountId) {
    return "Select a callable account before saving or testing this connector.";
  }
  if (
    selectedAccountSelection === ConnectorAccountSelectionMode.ALL
    && !connectorOperationSupportsAllAccounts(operation, requiresExplicitAccount)
  ) {
    return "All connected accounts are available only for compatible read operations.";
  }
  if (selectedAccountId && !callableAccountIds.includes(selectedAccountId)) {
    return "The selected account is unavailable. Choose a callable account before saving or testing.";
  }
  return "";
}

export function connectorOperationSupportsAllAccounts(
  operation,
  requiresExplicitAccount = false,
) {
  return Boolean(
    !requiresExplicitAccount
    && operation?.effect === "read"
    && operation?.supports_all_accounts !== false
  );
}

export function connectorAccountSelectionOptions({
  accounts = [],
  requiresExplicitAccount = false,
  selectedAccountSelection = ConnectorAccountSelectionMode.DEFAULT,
  operation,
}) {
  if (requiresExplicitAccount) return [...accounts];
  const options = [{ value: "", label: "Default account" }];
  if (
    (
      accounts.length > 1
      || selectedAccountSelection === ConnectorAccountSelectionMode.ALL
    )
    && connectorOperationSupportsAllAccounts(operation, requiresExplicitAccount)
  ) {
    options.push({
      value: ConnectorAccountSelectionMode.ALL,
      label: "All connected accounts",
    });
  }
  return [...options, ...accounts];
}

export function connectorOperationInputSchema(operation, selectedAccountId = "") {
  const accountId = String(selectedAccountId || "").trim();
  const accountSchemas = operation?.account_input_schemas;
  if (
    accountId
    && accountSchemas
    && typeof accountSchemas === "object"
    && accountSchemas[accountId]
    && typeof accountSchemas[accountId] === "object"
  ) {
    return accountSchemas[accountId];
  }
  return operation?.input_schema || { type: "object", properties: {}, required: [] };
}

export function connectorOperationOutputSchema(operation, selectedAccountId = "") {
  const accountId = String(selectedAccountId || "").trim();
  const accountSchemas = operation?.account_output_schemas;
  if (
    accountId
    && accountSchemas
    && typeof accountSchemas === "object"
    && accountSchemas[accountId]
    && typeof accountSchemas[accountId] === "object"
  ) {
    return accountSchemas[accountId];
  }
  return operation?.output_schema && typeof operation.output_schema === "object"
    ? operation.output_schema
    : undefined;
}
