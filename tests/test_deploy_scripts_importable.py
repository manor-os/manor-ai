"""Guard: deploy-time scripts must still import.

``docker/entrypoint.sh`` runs these with ``|| echo "... (non-fatal)"``, so an
ImportError never fails the deploy and never surfaces anywhere. That is how
``scripts/init_db.py`` kept importing ``ALWAYS_LOADED`` from
``packages.core.ai.tool_pool`` for days after 1f46fdc25 moved it to
``packages.core.ai.runtime.tool_visibility``: every deploy silently skipped
the system tool_definitions seed.

Importing is enough — these modules define functions and guard execution
behind ``if __name__ == "__main__"``, so nothing connects to a database here.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ENTRYPOINT = Path("docker/entrypoint.sh")

# Scripts entrypoint.sh invokes on every container start.
DEPLOY_SCRIPTS = ["scripts/init_db.py"]


def _import_script(path: str):
    spec = importlib.util.spec_from_file_location(f"_deploy_probe_{Path(path).stem}", path)
    assert spec and spec.loader, f"cannot load {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("script", DEPLOY_SCRIPTS)
def test_deploy_script_imports_cleanly(script):
    """A stale import here is invisible at deploy time — catch it in CI."""
    assert Path(script).is_file(), f"{script} is missing"
    _import_script(script)


def test_init_db_seeds_from_the_real_always_loaded_set():
    module = _import_script("scripts/init_db.py")
    from packages.core.ai.runtime.tool_visibility import ALWAYS_LOADED

    assert module.ALWAYS_LOADED is ALWAYS_LOADED, (
        "init_db must seed tool_definitions from the live ALWAYS_LOADED set"
    )
    assert ALWAYS_LOADED, "ALWAYS_LOADED should not be empty"


def test_every_entrypoint_python_script_is_covered():
    """If entrypoint.sh gains another python script, cover it here too."""
    if not ENTRYPOINT.is_file():
        pytest.skip("entrypoint.sh not present in this checkout")
    invoked = {
        f"scripts/{name}"
        for name in _entrypoint_script_names(ENTRYPOINT.read_text(encoding="utf-8"))
    }
    uncovered = invoked - set(DEPLOY_SCRIPTS)
    assert not uncovered, (
        f"entrypoint.sh runs {sorted(uncovered)} at deploy time with failures "
        "swallowed; add them to DEPLOY_SCRIPTS so a stale import fails CI"
    )


def _entrypoint_script_names(source: str) -> set[str]:
    import re

    return set(re.findall(r"python3?\s+scripts/([A-Za-z0-9_]+\.py)", source))
