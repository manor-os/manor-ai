#!/usr/bin/env python3
"""Apply declared OAuth provider clients after the single-owner migration."""
from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from packages.core.services import oauth_provider_service as svc  # noqa: E402

_CLIENT_FIELD = re.compile(
    r"^MANOR_OAUTH_CLIENT_(.+)_(SECRET|NAME|DESCRIPTION|REDIRECT_URIS|SCOPES|"
    r"RESTRICTED_ENTITY_ID|REQUIRE_PKCE|REQUIRE_VERIFIED_EMAIL|ACCESS_TOKEN_TTL_MINUTES|CLIENT_ID)$"
)


class BootstrapError(RuntimeError):
    """A safe category, never a configuration value or database diagnostic."""


async def bootstrap(db) -> dict[str, str]:
    slugs = {
        match[1] for key, value in os.environ.items()
        if value.strip() and (match := _CLIENT_FIELD.fullmatch(key))
    }
    if not slugs:
        return {}
    expected = set()
    for slug in sorted(slugs):
        try:
            config = svc._read_client_config(slug)
        except ValueError:
            raise BootstrapError("invalid_client_configuration") from None
        if config is None or not config["secret"].strip():
            raise BootstrapError("missing_client_secret")
        if not config["redirect_uris"]:
            raise BootstrapError("missing_redirect_uris")
        if config["client_id"] in expected:
            raise BootstrapError("duplicate_client_id")
        expected.add(config["client_id"])
    actions = await svc.seed_clients_from_env(db)
    if set(actions) != expected or any(
        action not in {"seeded", "unchanged"}
        and not action.startswith(("updated(", "rotated("))
        for action in actions.values()
    ):
        raise BootstrapError("oauth_client_seed_incomplete")
    return actions


async def main() -> None:
    from packages.core.database import async_session

    async with async_session() as db:
        actions = await bootstrap(db)
    print(json.dumps({"status": "applied" if actions else "not_configured", "clients": actions}))


if __name__ == "__main__":
    # Seed failures may include SQL parameters in library diagnostics. The
    # release gate emits only safe categories and successful client actions.
    logging.disable(logging.CRITICAL)
    try:
        asyncio.run(main())
    except Exception as exc:
        code = str(exc) if isinstance(exc, BootstrapError) else type(exc).__name__
        print(json.dumps({"status": "error", "code": code}), file=sys.stderr)
        raise SystemExit(1) from None
