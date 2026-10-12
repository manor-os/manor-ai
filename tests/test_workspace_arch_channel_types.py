"""The architect's channel_type enum must cover every real channel adapter.

When it does not, the model has to fall back to ``other`` for a platform Manor
actually supports — that is how a workspace ended up declaring X as ``other``
and showing an unsatisfiable "Attach an active other channel account" notice
while a ``twitter_x`` adapter existed all along.
"""
from __future__ import annotations


def test_architect_channel_types_cover_every_adapter_and_builtin():
    import packages.core.services.channels  # noqa: F401  (registers adapters on import)
    from packages.core.ai.tools.workspace_arch_tools import CHANNEL_TYPES, UNBACKED_CHANNEL_TYPES
    from packages.core.services.channels.base import registered_channel_types
    from packages.core.services.workspace_readiness import (
        BUILT_IN_CHANNEL_TYPES,
        UNBACKED_CHANNEL_TYPES as READINESS_UNBACKED,
    )

    adapters = {key for key in registered_channel_types() if key}
    missing = (adapters | BUILT_IN_CHANNEL_TYPES) - set(CHANNEL_TYPES)
    assert missing == set(), f"architect enum lacks channel types: {sorted(missing)}"
    assert "other" in CHANNEL_TYPES
    assert UNBACKED_CHANNEL_TYPES == READINESS_UNBACKED
