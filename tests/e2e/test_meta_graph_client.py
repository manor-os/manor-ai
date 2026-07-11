"""E2E for ``packages.core.services.meta_graph.MetaGraphClient``.

Two layers:

  * Wiring — module-level singleton ``graph`` is configured against the
    central pin (``META_GRAPH.value``).
  * Live — a real call to ``graph.facebook.com`` with a bogus token
    must come back as ``MetaGraphError`` with Meta's actual error code
    embedded (Meta returns 190 for "Invalid OAuth access token").

The live call is marked ``network`` so offline runs can skip with
``-m "not network"``.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.e2e, pytest.mark.manual]


def test_default_client_uses_central_pin() -> None:
    from packages.core.external_api_versions import META_GRAPH
    from packages.core.services.meta_graph import graph

    assert graph.version == META_GRAPH.value, (
        "module-level `graph` must be initialized with the central pin "
        "so version bumps propagate without per-caller updates"
    )
    assert graph.base.endswith(f"/{META_GRAPH.value}")


def test_dependents_share_the_central_pin() -> None:
    """All current Manor callers of Meta Graph must reference the
    central pin, directly or via the shared client."""
    from packages.core.external_api_versions import META_GRAPH
    from packages.core.ai.mcp import facebook
    from packages.core.services.channels import facebook_adapter, whatsapp_adapter
    from packages.core.services import integration_health

    assert facebook._API_VERSION == META_GRAPH.value
    assert whatsapp_adapter.DEFAULT_API_VERSION == META_GRAPH.value
    assert META_GRAPH.value in integration_health._META_BASE
    # facebook_adapter holds a private quick-timeout client; its
    # version field comes from the same pin.
    assert facebook_adapter._graph_quick.version == META_GRAPH.value


@pytest.mark.network
@pytest.mark.asyncio
async def test_invalid_token_raises_meta_graph_error() -> None:
    """Sanity-check the live wire: Meta should return error code 190
    for a bogus token, and our client should raise ``MetaGraphError``
    with that code surfaced — proving error-envelope parsing still
    works against the real API."""
    from packages.core.services.meta_graph import graph, MetaGraphError

    with pytest.raises(MetaGraphError) as exc:
        await graph.get("/me", token="not_a_real_token")
    assert exc.value.code == 190, (
        f"Meta usually returns code=190 for invalid tokens; got {exc.value.code} (message: {exc.value.message!r})"
    )
    assert "OAuth" in exc.value.message or "token" in exc.value.message.lower()


@pytest.mark.network
@pytest.mark.asyncio
async def test_pin_is_still_supported_by_meta() -> None:
    """A liveness check on the version pin itself: if Meta ever
    sunsets ``META_GRAPH.value``, the request URL would 404 instead
    of returning the usual 401/190 token error. This catches that
    before the CI freshness probe does."""
    from packages.core.services.meta_graph import graph, MetaGraphError

    with pytest.raises(MetaGraphError) as exc:
        await graph.get("/me", token="not_a_real_token")
    # 404 = endpoint doesn't exist (i.e. version path is wrong);
    # 190 / 4xx = endpoint exists, just rejected our token.
    assert exc.value.code != 404, (
        f"Meta returned 404 — the pinned version {graph.version!r} may have "
        f"been retired. Bump META_GRAPH in external_api_versions.py."
    )
