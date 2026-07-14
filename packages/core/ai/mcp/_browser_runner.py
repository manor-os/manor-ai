"""Shared HTTP client for the browser-runner sidecar.

All MCP wrappers that drive a GUI-only platform via the sidecar
container (NotebookLM, Claude.ai web, ChatGPT web, …) share this
``perform()`` helper. The wrapper-side modules stay thin:

  result = await perform(
      provider="notebooklm",
      action="ask",
      params={...},
      storage_state=cookies_json,
  )

Bearer auth (when configured) and the sidecar URL are read from env
once and cached.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Callable, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


# Optional translator: takes the provider's raw result dict, returns a
# PendingAction (or None). Lifted into the MCP envelope under
# ``_pending_action`` for the chat / Plan executor to route on. See
# packages/core/ai/pending_action.py for the contract.
_PendingActionTranslator = Callable[[Dict[str, Any]], Optional[Any]]


_RUNNER_URL = os.environ.get("BROWSER_RUNNER_URL", "http://browser-runner:5200").rstrip("/")
_RUNNER_TOKEN = os.environ.get("BROWSER_RUNNER_TOKEN", "").strip()
_TIMEOUT = 120.0  # baseline — per-call runner timeouts may be longer
_HTTP_TIMEOUT_OVERHEAD = 30.0


def _http_timeout_for(timeout_ms: Optional[int]) -> "httpx.Timeout":
    read_timeout = _TIMEOUT
    if timeout_ms and timeout_ms > 0:
        read_timeout = max(read_timeout, (timeout_ms / 1000.0) + _HTTP_TIMEOUT_OVERHEAD)
    return httpx.Timeout(
        connect=10.0,
        read=read_timeout,
        write=30.0,
        pool=10.0,
    )


async def perform(
    *,
    provider: str,
    action: str,
    params: Optional[Dict[str, Any]] = None,
    storage_state: Optional[Dict[str, Any]] = None,
    timeout_ms: Optional[int] = None,
    headless: bool = True,
) -> Dict[str, Any]:
    """Call the sidecar's /perform endpoint. Returns the raw response
    JSON (with ``ok`` / ``result`` / ``error`` / ``elapsed_ms``)."""
    headers = {"Content-Type": "application/json"}
    if _RUNNER_TOKEN:
        headers["Authorization"] = f"Bearer {_RUNNER_TOKEN}"

    body: Dict[str, Any] = {
        "provider": provider,
        "action": action,
        "params": params or {},
        "headless": headless,
    }
    if storage_state:
        body["storage_state"] = storage_state
    if timeout_ms:
        body["timeout_ms"] = timeout_ms

    request_timeout = _http_timeout_for(timeout_ms)
    read_timeout = request_timeout.read or _TIMEOUT

    try:
        async with httpx.AsyncClient(timeout=request_timeout) as cx:
            r = await cx.post(f"{_RUNNER_URL}/perform", headers=headers, json=body)
    except httpx.ReadTimeout:
        return {
            "ok": False,
            "error": (
                f"Browser-runner request timed out after {int(read_timeout)}s while "
                f"waiting for {provider}/{action}. The sidecar may still be running "
                "the browser task; check `docker logs manor-os-browser-runner` for "
                "the final provider error. Increase the tool timeout if this action "
                "normally takes longer."
            ),
        }
    except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        detail = str(exc) or type(exc).__name__
        return {
            "ok": False,
            "error": (
                f"Could not connect to browser-runner at {_RUNNER_URL}. From the "
                "container/process running the MCP wrapper, verify DNS/networking "
                f"and BROWSER_RUNNER_URL. ({detail})"
            ),
        }
    except httpx.RequestError as exc:
        detail = str(exc) or type(exc).__name__
        return {
            "ok": False,
            "error": (
                f"Browser-runner request failed at {_RUNNER_URL} while calling "
                f"{provider}/{action}: {detail}"
            ),
        }
    if r.status_code >= 500:
        return {"ok": False, "error": f"sidecar HTTP {r.status_code}: {r.text[:300]}"}
    try:
        return r.json()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"sidecar returned non-JSON: {exc}"}


async def call_provider(
    *,
    provider: str,
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
    timeout_ms: int = 120_000,
    entity_id: str = "",
    save_artifacts_to_knowledge: bool = False,
    result_to_pending_action: Optional[_PendingActionTranslator] = None,
) -> Dict[str, Any]:
    """Convenience used by per-platform MCP wrappers (notebooklm,
    chatgpt_web, gemini_web, …). Handles cookie parsing + sidecar
    HTTP + MCP-shaped error/content envelope so each wrapper is
    ~30 LOC.

    ``result_to_pending_action``: optional callable invoked on the
    provider's raw result dict. When it returns a PendingAction, that
    payload is attached to the envelope under ``_pending_action`` so
    chat / Plan-executor consumers can route on user-actionable
    statuses (login walls, blocking questions, destructive
    confirmations) without parsing tool-specific JSON. See
    packages/core/ai/pending_action.py.
    """
    if not bearer_token:
        return {
            "content": [{"type": "text", "text": (
                f"{provider} cookies are missing. Export them from a "
                "browser where you're signed in (Cookie-Editor → "
                f"Export → JSON), then paste into Integrations → {provider}."
            )}],
            "isError": True,
        }
    storage_state = parse_storage_state(bearer_token)
    if not storage_state:
        return {
            "content": [{"type": "text", "text": (
                f"Could not parse the {provider} cookies. Expected "
                "either Playwright storage_state JSON or a Cookie-Editor "
                "export. Re-export and paste again."
            )}],
            "isError": True,
        }

    try:
        resp = await perform(
            provider=provider,
            action=name,
            params=arguments,
            storage_state=storage_state,
            timeout_ms=timeout_ms,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("browser-runner call failed: %s/%s", provider, name)
        return {
            "content": [{"type": "text", "text": f"{provider} call failed: {exc}"}],
            "isError": True,
        }

    if not resp.get("ok"):
        return {
            "content": [{"type": "text", "text": resp.get("error") or "browser-runner returned non-ok"}],
            "isError": True,
        }

    # Artifact persistence is explicit opt-in. Some browser providers return
    # page evidence such as images/screenshots under an `artifacts` key; that
    # should not be silently copied into the user's Knowledge base during
    # ordinary browsing. Tools that intentionally download files can enable
    # this flag and still get the old token -> saved_to rewrite.
    result = resp.get("result") or {}
    if (
        save_artifacts_to_knowledge
        and isinstance(result, dict)
        and result.get("artifacts")
    ):
        from ._knowledge_artifact import (
            parse_target_folder_from_args,
            process_result_artifacts,
        )
        try:
            result = await process_result_artifacts(
                result,
                entity_id=entity_id,
                provider=provider,
                target_folder=parse_target_folder_from_args(arguments),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("artifact post-processing failed: %s/%s", provider, name)
            return {
                "content": [{"type": "text", "text": (
                    f"{provider} call succeeded but saving downloads to "
                    f"knowledge failed: {exc}"
                )}],
                "isError": True,
            }

    import json as _j
    envelope: Dict[str, Any] = {
        "content": [{"type": "text", "text": _j.dumps(result, ensure_ascii=False, indent=2)}],
        "isError": False,
    }
    if result_to_pending_action is not None:
        try:
            # Translator inspects the post-artifact-processing result —
            # status / blocking_questions / etc are unchanged by artifact
            # rewriting, and any saved_to paths the translator wants to
            # surface are now correct.
            pending = result_to_pending_action(result)
        except Exception:  # noqa: BLE001
            logger.exception(
                "pending-action translator raised for %s/%s — dropping pending_action",
                provider, name,
            )
            pending = None
        if pending is not None:
            try:
                pending.attach_to_envelope(envelope)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "PendingAction.attach_to_envelope failed for %s/%s",
                    provider, name,
                )
    return envelope


def parse_storage_state(raw: str) -> Optional[Dict[str, Any]]:
    """Accept any of:
      - a Playwright storage_state JSON dict
      - a "Cookie-Editor" / "EditThisCookie" export (list of cookie objects)
      - a single-cookie-name=value string
      - empty / None

    Normalize to Playwright storage_state shape:
      { cookies: [{name, value, domain, path, expires, httpOnly, secure, sameSite}],
        origins: [{ origin, localStorage: [{name, value}] }] }
    """
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except Exception:
        return None

    # Already a Playwright storage_state?
    if isinstance(v, dict) and ("cookies" in v or "origins" in v):
        return v

    # Cookie-export list?
    if isinstance(v, list):
        cookies = []
        for c in v:
            if not isinstance(c, dict) or "name" not in c or "value" not in c:
                continue
            cookies.append({
                "name": c.get("name"),
                "value": c.get("value"),
                "domain": c.get("domain") or "",
                "path": c.get("path") or "/",
                "expires": float(c.get("expirationDate") or c.get("expires") or -1),
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": c.get("sameSite") or "Lax",
            })
        return {"cookies": cookies, "origins": []}

    return None
