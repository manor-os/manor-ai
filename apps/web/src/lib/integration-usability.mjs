/**
 * Keep stored connector state separate from current Agent usability.
 *
 * `user_connected` and `entity_connected` describe configured ownership;
 * only `agent_can_use` means the current Agent may invoke the integration.
 */
export function connectorAccessState(server) {
  if (Boolean(server?.agent_can_use)) return "usable";
  const hasStoredConnection = Boolean(
    server?.user_connected ||
      server?.entity_connected ||
      server?.connections?.length ||
      server?.entity_accounts?.length,
  );
  return hasStoredConnection ? "repair" : "connect";
}

export function integrationReadyCount(servers) {
  return (servers || []).filter((server) => Boolean(server?.agent_can_use)).length;
}

export function integrationStoredConnectionCount(servers) {
  return (servers || []).filter(
    (server) => connectorAccessState(server) !== "connect",
  ).length;
}
