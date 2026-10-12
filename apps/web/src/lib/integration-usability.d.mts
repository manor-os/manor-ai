export type ConnectorAccessState = "usable" | "repair" | "connect";
export type IntegrationSetupState = "ready" | "checking" | "unavailable";

export interface IntegrationSetupSource {
  setup_pending?: boolean | null;
  setup_unavailable?: boolean | null;
}

export function integrationSetupState(
  server: IntegrationSetupSource | null | undefined,
): IntegrationSetupState;

export interface IntegrationUsabilitySource {
  agent_can_use?: boolean | null;
  user_connected?: boolean | null;
  entity_connected?: boolean | null;
  connections?: unknown[] | null;
  entity_accounts?: unknown[] | null;
}

export function connectorAccessState(
  server: IntegrationUsabilitySource | null | undefined,
): ConnectorAccessState;

export function integrationReadyCount(
  servers: IntegrationUsabilitySource[] | null | undefined,
): number;

export function integrationStoredConnectionCount(
  servers: IntegrationUsabilitySource[] | null | undefined,
): number;
