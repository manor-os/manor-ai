export const ConnectorServerKind: Readonly<{
  MANAGED: "managed";
  CUSTOM: "custom";
  UNKNOWN: "unknown";
}>;

export type ConnectorServerKindValue =
  (typeof ConnectorServerKind)[keyof typeof ConnectorServerKind];

export const ConnectorAccountSelectionMode: Readonly<{
  DEFAULT: "default";
  EXACT: "exact";
  ALL: "all";
}>;

export type ConnectorAccountSelectionModeValue =
  (typeof ConnectorAccountSelectionMode)[keyof typeof ConnectorAccountSelectionMode];

type ConnectorAccountOption = { value: string; label: string };
type ConnectorAccountOperation = {
  effect?: "read" | "write" | "destructive";
  supports_all_accounts?: boolean | null;
};

export function resolveConnectorServerKind(input: {
  serverKey?: string;
  operationCatalogKind?: "managed" | "custom";
  serverCatalogKind?: "managed" | "custom";
  operationCatalogSource?: "builtin" | "cache" | "account_discovery";
  persistedKind?: unknown;
}): ConnectorServerKindValue;

export function connectorAccountError(input: {
  serverKey?: string;
  serverKind: ConnectorServerKindValue;
  accountCatalogStatus: "loading" | "error" | "ready";
  serverCatalogPresent?: boolean;
  requiresExplicitAccount?: boolean;
  selectedAccountId?: string;
  selectedAccountSelection?: string;
  callableAccountIds?: string[];
  operation?: ConnectorAccountOperation;
}): string;

export function connectorOperationSupportsAllAccounts(
  operation?: ConnectorAccountOperation,
  requiresExplicitAccount?: boolean,
): boolean;

export function connectorAccountSelectionOptions(input: {
  accounts?: ConnectorAccountOption[];
  requiresExplicitAccount?: boolean;
  selectedAccountSelection?: ConnectorAccountSelectionModeValue;
  operation?: ConnectorAccountOperation;
}): ConnectorAccountOption[];

type ConnectorInputSchema = {
  type: "object";
  properties: Record<string, Record<string, unknown>>;
  required: string[];
  [key: string]: unknown;
};

export function connectorOperationInputSchema(
  operation?: {
    input_schema?: ConnectorInputSchema;
    account_input_schemas?: Record<string, ConnectorInputSchema>;
  },
  selectedAccountId?: string,
): ConnectorInputSchema;

type ConnectorOutputSchema = Record<string, unknown>;

export function connectorOperationOutputSchema(
  operation?: {
    output_schema?: ConnectorOutputSchema | null;
    account_output_schemas?: Record<string, ConnectorOutputSchema>;
  },
  selectedAccountId?: string,
): ConnectorOutputSchema | undefined;
