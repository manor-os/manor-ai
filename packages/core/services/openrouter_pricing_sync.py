"""OpenRouter pricing sync — periodic pull + local cache for runtime billing.

This module fetches model pricing from OpenRouter and stores a compact JSON
cache file. Billing paths can read this cache at runtime to stay aligned with
provider price changes without code redeploys.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
DEFAULT_CACHE_PATH = "/tmp/manor_openrouter_pricing_cache.json"


def pricing_cache_path() -> str:
    return (os.getenv("OPENROUTER_PRICING_CACHE_PATH") or DEFAULT_CACHE_PATH).strip()


def _to_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


async def sync_openrouter_pricing_cache(*, timeout_s: float = 30.0) -> dict[str, Any]:
    """Fetch OpenRouter model pricing and write local JSON cache.

    Cache shape:
      {
        "fetched_at": "...",
        "models": {
          "<model_id>": {
            "input_per_m": float | null,
            "output_per_m": float | null,
            "image_per_1k": float | null,
            "request_usd": float | null
          }
        }
      }
    """
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        resp = await client.get(OPENROUTER_MODELS_URL)
        resp.raise_for_status()
        body = resp.json()

    rows = body.get("data") or []
    models: dict[str, dict[str, float | None]] = {}
    for row in rows:
        mid = str(row.get("id") or "").strip()
        if not mid:
            continue
        pricing = row.get("pricing") or {}
        prompt = _to_float(pricing.get("prompt"))
        completion = _to_float(pricing.get("completion"))
        image = _to_float(pricing.get("image"))
        request = _to_float(pricing.get("request"))
        models[mid] = {
            "input_per_m": (prompt * 1_000_000.0) if prompt is not None else None,
            "output_per_m": (completion * 1_000_000.0) if completion is not None else None,
            "image_per_1k": (image * 1000.0) if image is not None else None,
            "request_usd": request,
        }

    payload = {
        "fetched_at": resp.headers.get("date", ""),
        "models": models,
    }
    p = Path(pricing_cache_path())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False))
    logger.info("openrouter pricing sync: wrote %d models to %s", len(models), str(p))
    return {"count": len(models), "path": str(p)}

