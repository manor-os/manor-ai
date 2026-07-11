"""Compatibility wrapper for the composite generate_file tool.

Implementation now lives in ``packages.core.ai.tools.generate_file`` so each
artifact type can evolve independently while callers keep importing this module.
"""
from __future__ import annotations

from typing import Any

from .generate_file import common, office_common
from .generate_file.common import _merge_params, _scope_workspace_output_name
from .generate_file.office_common import _invoke_builtin_skill
from .generate_file.schema import (
    GENERATE_FILE_SCHEMA,
    VIDEO_ASPECT_RATIO_CHOICES,
    VIDEO_DURATION_CHOICES,
    VIDEO_RESOLUTION_CHOICES,
    VIDEO_SEEDANCE_FAST_RESOLUTION_CHOICES,
    _CAPABILITIES,
)
from .generate_file.tool import _generate_file_handler as _package_generate_file_handler


async def _generate_file_handler(
    entity_id: str = "",
    user_id: str = "",
    conversation_id: str = "",
    **kwargs: Any,
) -> str:
    common._scope_workspace_output_name = _scope_workspace_output_name
    office_common._invoke_builtin_skill = _invoke_builtin_skill
    return await _package_generate_file_handler(
        entity_id=entity_id,
        user_id=user_id,
        conversation_id=conversation_id,
        **kwargs,
    )


def get_tools() -> list[tuple[dict[str, Any], Any]]:
    return [(GENERATE_FILE_SCHEMA, _generate_file_handler)]
