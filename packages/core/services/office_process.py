"""Process limits shared by server-side Office conversions."""

from __future__ import annotations

import os
from collections.abc import Callable


def office_conversion_file_limit(max_bytes: int) -> Callable[[], None] | None:
    """Return a child-only hard file-size limit for LibreOffice outputs."""
    if os.name != "posix":
        return None
    limit = max(1, int(max_bytes))

    def apply_limit() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))

    return apply_limit
