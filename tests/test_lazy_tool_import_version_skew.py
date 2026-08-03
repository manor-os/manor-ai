"""Regression: lazily-imported tool modules must survive deploy version-skew.

Staging incident (2026-07-16): `provision_agent` / dashboard AI-modules use
raised ``ImportError: cannot import name 'runtime_query_entity_agents_action'
from packages.core.ai.runtime``. The worker process had been booted before the
deploy that added that re-export, so the old ``packages.core.ai.runtime``
__init__ was cached in sys.modules; the first tool use then lazily imported the
NEW ``agent_provisioning_tools.py`` from disk, whose package-root import looked
the name up on the stale cached __init__ and died.

Fix: the tools module imports from the defining submodule
(``packages.core.ai.runtime.agent_provisioning``), which is loaded fresh from
disk even when the parent package object is stale. This test simulates the
stale-parent condition and cold-imports the tools module.
"""

import importlib
import sys
import types


def test_agent_provisioning_tools_survives_stale_runtime_package(monkeypatch):
    import packages.core.ai.runtime as runtime_pkg

    # Simulate a long-running worker: the runtime package object in sys.modules
    # predates the deploy — it is a real package (has __path__, so submodules
    # can still be loaded from disk) but exposes NONE of the new re-exports.
    stale = types.ModuleType("packages.core.ai.runtime")
    stale.__path__ = runtime_pkg.__path__
    assert not hasattr(stale, "runtime_query_entity_agents_action")

    monkeypatch.setitem(sys.modules, "packages.core.ai.runtime", stale)
    # Force cold imports, as on first tool use in the worker.
    monkeypatch.delitem(
        sys.modules, "packages.core.ai.runtime.agent_provisioning", raising=False
    )
    monkeypatch.delitem(
        sys.modules, "packages.core.ai.tools.agent_provisioning_tools", raising=False
    )

    mod = importlib.import_module("packages.core.ai.tools.agent_provisioning_tools")

    assert callable(mod.runtime_query_entity_agents_action)
    assert callable(mod.runtime_provision_agent_action)
