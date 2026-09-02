from __future__ import annotations

from typing import Any


def runtime_inspect_file_engine_parameters() -> dict[str, Any]:
    """Return the public inspect schema through the Runtime boundary."""

    from packages.core.ai.tools.file_tools import INSPECT_FILE_ENGINE_SCHEMA

    return INSPECT_FILE_ENGINE_SCHEMA["function"]["parameters"]


def runtime_patch_file_parameters() -> dict[str, Any]:
    """Return the public patch schema through the Runtime boundary."""

    from packages.core.ai.tools.file_tools import PATCH_FILE_SCHEMA

    return PATCH_FILE_SCHEMA["function"]["parameters"]


async def runtime_inspect_file_engine(*, entity_id: str, **kwargs: Any) -> str:
    """Inspect file-engine capabilities using the canonical tool handler."""

    from packages.core.ai.tools.file_tools import _inspect_file_engine

    return await _inspect_file_engine(entity_id=entity_id, **kwargs)


async def runtime_generate_file(*, entity_id: str, user_id: str, **kwargs: Any) -> str:
    """Generate a file using the canonical tool handler."""

    from packages.core.ai.tools.generate_file.tool import _generate_file_handler

    return await _generate_file_handler(
        entity_id=entity_id,
        user_id=user_id,
        **kwargs,
    )


async def runtime_patch_file(*, entity_id: str, user_id: str, **kwargs: Any) -> str:
    """Patch a file using the canonical tool handler."""

    from packages.core.ai.tools.file_tools import _patch_file

    return await _patch_file(
        entity_id=entity_id,
        user_id=user_id,
        **kwargs,
    )
