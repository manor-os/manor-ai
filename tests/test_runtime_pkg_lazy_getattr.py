"""Regression: the runtime package self-heals deploy version-skew re-exports.

A long-running process (worker/uvicorn/staging container mid-deploy) keeps the
boot-time __init__ cached in sys.modules. When a deploy or concurrent edit adds
a new re-export, lazily imported callers read the NEW submodule from disk but
resolve names against the STALE cached __init__ — "ImportError: cannot import
name 'runtime_query_entity_agents_action' from packages.core.ai.runtime" (hit
on staging 2026-07-16 and again locally 2026-07-18; PR #294 fixed one importer,
this fixes the class). PEP 562 __getattr__ now resolves misses by scanning the
package's submodules, so any name that exists in a submodule is found even when
the cached __init__ predates its re-export.
"""

import pytest


def test_missing_reexport_resolves_from_submodule():
    import packages.core.ai.runtime as runtime_pkg

    # Simulate the stale-init skew: the attribute vanishes from the package
    # object (as if the cached __init__ predates the re-export).
    original = runtime_pkg.runtime_query_entity_agents_action
    del runtime_pkg.runtime_query_entity_agents_action
    try:
        from packages.core.ai.runtime import runtime_query_entity_agents_action
        assert callable(runtime_query_entity_agents_action)
    finally:
        runtime_pkg.runtime_query_entity_agents_action = original


def test_resolved_name_is_cached_on_package():
    import packages.core.ai.runtime as runtime_pkg

    original = runtime_pkg.runtime_provision_agent_action
    del runtime_pkg.runtime_provision_agent_action
    try:
        resolved = runtime_pkg.runtime_provision_agent_action  # __getattr__ path
        assert callable(resolved)
        assert "runtime_provision_agent_action" in vars(runtime_pkg)
    finally:
        runtime_pkg.runtime_provision_agent_action = original


def test_truly_missing_name_still_raises():
    with pytest.raises(ImportError):
        from packages.core.ai.runtime import definitely_not_a_real_name  # noqa: F401


def test_dunder_names_not_swallowed():
    import packages.core.ai.runtime as runtime_pkg

    with pytest.raises(AttributeError):
        runtime_pkg.__not_a_thing__
