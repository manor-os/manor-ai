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

# ── Legacy agent-author placeholders ──
# Strings that landed in task_logs.created_by where the writer had an agent
# and failed to record it. They are NOT a category of actor: every one of
# these rows was produced by a determinate agent — the task's agent, the
# step's resolved agent, or the master agent running as the workspace agent.
# The writer just didn't thread it through, so the row says "an agent" where
# it could have said which.
#
# Nothing writes these any more; each site now records the real agent:
#
#   "workspace-agent"  services/workspace_runtime.py — resolves the responder
#                      to task.agent_id, and to the master agent otherwise.
#   "AI Agent"         plans/executor.py — the auto "Task Completed" summary
#                      is now attributed to the task's agent, which produced
#                      the deliverables it assembles.
#   "ai-agent"         ai/runtime/task_actions.py — the create-task action
#                      now passes the calling agent, which the runtime tool
#                      context has always carried.
#   "AI Supervisor"    plans/executor.py — the plan supervisor, which is a
#                      real distinct actor (see TaskActor.SUPERVISOR), not a
#                      failure to attribute.
#
# They survive here only so the rows already in the database still resolve to
# something truthful instead of rendering as a person named "workspace-agent".
LEGACY_AGENT_AUTHOR_PLACEHOLDERS = frozenset(
    {
        "workspace-agent",
        "ai-agent",
        "ai agent",
        "ai supervisor",
        # Bare "Agent" predates the rest; the activity feed has always
        # treated it as a placeholder.
        "agent",
    }
)


def is_legacy_agent_author_placeholder(value: str | None) -> bool:
    """True for a stored author string that stands in for an agent it failed
    to name. New writes never produce one."""
    return (value or "").strip().lower() in LEGACY_AGENT_AUTHOR_PLACEHOLDERS


def agent_display_name_from_service_key(service_key: str | None) -> str:
    """Turn an agent's service key into something readable.

    A subscription with no ``name`` is still a specific agent, so its key is
    the best identity available — but "daily_progress_review" is a key, not a
    name, and it reads as machine debris in an author slot. Mirrors
    humanizeServiceKey() in apps/web/src/components/task/TaskLogItem.tsx.
    """
    raw = (service_key or "").strip()
    if not raw:
        return ""
    words = raw.replace(".", " ").replace("_", " ").replace("-", " ").split()
    return " ".join(word[:1].upper() + word[1:] for word in words)


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
