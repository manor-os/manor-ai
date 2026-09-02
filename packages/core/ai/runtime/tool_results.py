from __future__ import annotations

from typing import Any


class RuntimeStructuredToolResult(str):
    """Display-compatible tool text carrying an authoritative typed payload."""

    structured_content: Any

    def __new__(
        cls,
        display_text: str,
        *,
        structured_content: Any,
    ) -> RuntimeStructuredToolResult:
        instance = super().__new__(cls, display_text)
        instance.structured_content = structured_content
        return instance
