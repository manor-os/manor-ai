"""
Coded HTTPExceptions — backend half of the error-i18n contract.

Why
---
FastAPI's HTTPException carries one English string. The frontend either
shows that string verbatim or its own localized "Request failed" toast.
Neither extreme is great: users in zh/es locales see English error
copy, and writing every error string in every supported language inside
the backend is the wrong layer to do it.

This helper splits the responsibility:

  · backend emits a stable ``code`` (and an English fallback message)
  · frontend looks up the code in its i18n catalog and shows the
    user the localized version
  · old clients (or non-UI callers — curl, CLI, integrations) still get
    the English message verbatim

Wire it up by raising :class:`CodedError` instead of HTTPException::

    raise CodedError(
        403,
        code="permissions.error.restricted_no_external",
        message="Restricted documents cannot be shared externally",
    )

The response body becomes::

    {"detail": {
        "code": "permissions.error.restricted_no_external",
        "message": "Restricted documents cannot be shared externally",
        "vars": null,
    }}

Use ``vars`` for variable interpolation (e.g. capability names, counts);
the frontend's ``t(code, vars)`` will substitute ``{name}`` etc.

Rolling this out is incremental — switch a handler from
``HTTPException(403, "...")`` to ``CodedError(403, code="...", message="...")``
one endpoint at a time. Both shapes are accepted by the frontend so the
two coexist during migration.
"""
from __future__ import annotations

from typing import Any, Mapping

from fastapi import HTTPException


class CodedError(HTTPException):
    """HTTPException whose ``detail`` is a structured ``{code, message, vars}`` dict.

    Args:
        status_code: HTTP status (e.g. 400, 403, 404, 410).
        code: Stable i18n key — namespace-style, e.g.
            ``"permissions.error.restricted_no_external"``. The frontend
            looks this up in its catalog; if no key resolves, falls back
            to ``message``.
        message: English fallback message. Shown to clients that don't
            translate (older frontends, CLI users, integrations, logs).
        vars: Optional substitution variables — used by the frontend's
            ``t(code, vars)`` to interpolate ``{name}`` style placeholders.
        headers: Optional response headers passed through to FastAPI.
    """

    def __init__(
        self,
        status_code: int,
        *,
        code: str,
        message: str,
        vars: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        detail: dict[str, Any] = {
            "code": code,
            "message": message,
        }
        if vars:
            detail["vars"] = dict(vars)
        super().__init__(
            status_code=status_code,
            detail=detail,
            headers=dict(headers) if headers else None,
        )
