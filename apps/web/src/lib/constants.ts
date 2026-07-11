/**
 * Global constants — single source of truth.
 * Mirrors packages/core/constants/agents.py for the frontend.
 */

// ── Manor Master Agent ──
/** The canonical agent_id for the Manor master agent */
export const MANOR_AGENT_ID = "manor-master";
/** The agent_type value stored on tasks/channels */
export const MANOR_AGENT_TYPE = "manor_agent";
/** Display name for the master agent */
export const MANOR_AGENT_NAME = "Manor AI";

/** Check if an agent_id or agent_type refers to the Manor master agent */
export function isMasterAgent(agentId?: string | null, agentType?: string | null): boolean {
  if (agentType === MANOR_AGENT_TYPE) return true;
  if (agentId === MANOR_AGENT_ID || agentId === "master") return true;
  return false;
}
