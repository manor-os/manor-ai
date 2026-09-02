"""Shared visibility rules for persisted conversation messages."""
from __future__ import annotations

import re

from sqlalchemy import and_, func, not_


_FILE_PERMISSION_MARKER_RE = re.compile(
    r"^\[File permission(?:\s+[^\]]*)?\]$",
    re.IGNORECASE,
)


def is_internal_file_permission_marker(content: str | None) -> bool:
    """Return whether content is a legacy approval-resume marker."""
    return bool(
        isinstance(content, str)
        and _FILE_PERMISSION_MARKER_RE.match(content.strip())
    )


def visible_user_request_predicate(role_column, content_column):
    """Build the persisted-message predicate for a visible user request."""
    trimmed_content = func.btrim(content_column)
    return and_(
        role_column == "user",
        content_column.is_not(None),
        func.length(trimmed_content) > 0,
        not_(trimmed_content.op("~*")(_FILE_PERMISSION_MARKER_RE.pattern)),
    )
