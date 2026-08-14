#!/usr/bin/env python3
"""Audit fixed Manor Catalog IDs against every managed gateway fallback."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from packages.core.constants.models import CATALOG  # noqa: E402
from packages.core.services.model_provider_handlers import (  # noqa: E402
    catalog_openrouter_contract_issues,
    catalog_vercel_contract_issues,
)


VERCEL_MODELS_URL = "https://ai-gateway.vercel.sh/v1/models"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"


def fetch_vercel_models() -> dict[str, str]:
    request = Request(
        VERCEL_MODELS_URL,
        headers={"Accept": "application/json", "User-Agent": "manor-model-audit/1"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS URL
        payload = json.load(response)
    return {
        str(item.get("id") or "").strip(): str(item.get("type") or "").strip()
        for item in payload.get("data") or []
        if isinstance(item, dict) and item.get("id")
    }


def fetch_openrouter_models() -> set[str]:
    request = Request(
        OPENROUTER_MODELS_URL,
        headers={"Accept": "application/json", "User-Agent": "manor-model-audit/1"},
    )
    with urlopen(request, timeout=30) as response:  # noqa: S310 - fixed HTTPS URL
        payload = json.load(response)
    return {
        str(item.get("id") or "").strip()
        for item in payload.get("data") or []
        if isinstance(item, dict) and item.get("id")
    }


def main() -> int:
    vercel_models = fetch_vercel_models()
    openrouter_models = fetch_openrouter_models()
    vercel_issues = catalog_vercel_contract_issues(
        CATALOG,
        available_models=vercel_models,
    )
    openrouter_issues = catalog_openrouter_contract_issues(
        CATALOG,
        available_models=openrouter_models,
    )
    issues = [*vercel_issues, *openrouter_issues]
    remote_entries = sum(1 for entries in CATALOG.values() for item in entries if item.get("deployment") != "local")
    print(
        json.dumps(
            {
                "ok": not issues,
                "vercel_models": len(vercel_models),
                "openrouter_models": len(openrouter_models),
                "remote_catalog_entries": remote_entries,
                "vercel_issues": vercel_issues,
                "openrouter_issues": openrouter_issues,
                "issues": issues,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
