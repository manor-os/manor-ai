#!/usr/bin/env python3
from __future__ import annotations

import asyncio

from packages.core.services.nango_bootstrap import seed_nango_from_env


def main() -> None:
    result = asyncio.run(seed_nango_from_env())
    providers = result.get("providers") or {}
    webhook = str(result.get("webhook") or "")
    hmac = str(result.get("hmac") or "missing")
    failures = [value for value in providers.values() if str(value).startswith("error:")]

    for provider_key in sorted(providers):
        status = str(providers[provider_key]).split(":", 1)[0]
        print(f"{provider_key}={status}")
    print(f"webhook={webhook.split(':', 1)[0] or 'missing'}")
    print(f"hmac={hmac.split(':', 1)[0] or 'missing'}")

    if (
        result.get("error")
        or result.get("skipped")
        or webhook.startswith("error:")
        or hmac in ("", "missing")
        or hmac.startswith("error:")
        or failures
    ):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
