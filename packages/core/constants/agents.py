"""
Agent constants — single source of truth for agent identifiers.

MANOR_AGENT_ID: The well-known agent_id for the Manor master agent.
    Used in task assignment, channel routing, tool resolution, and UI display.

MANOR_AGENT_IDS: Set of all accepted identifiers that resolve to the master agent.

MANOR_AGENT_TYPE: The agent_type value stored on tasks/channels for the master agent.

MANOR_AGENT_NAME: Display name for the master agent.

MANOR_AGENT_FS_ID: Filesystem directory name for master agent config files.
"""

# The canonical agent_id for the manor master agent
MANOR_AGENT_ID = "manor-master"

# All IDs that resolve to the master agent (for backwards compat)
MANOR_AGENT_IDS = frozenset({"manor-master", "master"})

# The agent_type value used on task/channel models
MANOR_AGENT_TYPE = "manor_agent"

# Display name
MANOR_AGENT_NAME = "Manor AI"

# Filesystem directory for master agent config (AGENT.md, GOALS.md, etc.)
MANOR_AGENT_FS_ID = "_master"


def is_master_agent(agent_id: str | None = None, agent_type: str | None = None) -> bool:
    """Check if the given agent_id or agent_type explicitly refers to the manor master agent.

    Returns False when both are None (unassigned). Use-sites that want
    "None → master" (e.g. chat) should check `not agent_id` separately.
    """
    if agent_type == MANOR_AGENT_TYPE:
        return True
    if agent_id and agent_id in MANOR_AGENT_IDS:
        return True
    return False
