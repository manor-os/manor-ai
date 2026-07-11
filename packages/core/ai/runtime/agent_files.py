"""Runtime-owned facade for agent definition files."""

from __future__ import annotations

from typing import Any

from packages.core.services.agent_files import AGENT_FILE_NAMES

RUNTIME_AGENT_FILE_NAMES = AGENT_FILE_NAMES


def runtime_effective_agent_file_id(agent_id: str | None) -> str:
    """Resolve the storage id for an agent definition file set."""

    from packages.core.services.agent_files import effective_agent_id

    return effective_agent_id(agent_id)


def runtime_read_agent_file(
    *,
    entity_id: str,
    agent_id: str,
    filename: str,
    user_id: str | None = None,
) -> str | None:
    """Read an agent definition file through the Runtime boundary."""

    from packages.core.services.agent_files import read_agent_file

    return read_agent_file(entity_id, agent_id, filename, user_id=user_id)


def runtime_write_agent_file(
    *,
    entity_id: str,
    agent_id: str,
    filename: str,
    content: str,
    user_id: str | None = None,
) -> str:
    """Write an agent definition file through the Runtime boundary."""

    from packages.core.services.agent_files import write_agent_file

    return write_agent_file(
        entity_id,
        agent_id,
        filename,
        content,
        user_id=user_id,
    )


def runtime_list_agent_files(
    *,
    entity_id: str,
    agent_id: str,
    user_id: str | None = None,
) -> Any:
    """List agent definition files through the Runtime boundary."""

    from packages.core.services.agent_files import list_agent_files

    return list_agent_files(entity_id, agent_id, user_id=user_id)
