"""Run the isolated Blueprint approval lifecycle smoke.

The regression uses real SQLAlchemy rows and production exporter, installer,
configuration upgrade/revert, report and promote services. It covers both
simulation and live installs, account binding, operator mode/exceptions,
portable business rules, semantic re-export parity and conflict detection.
This is a focused approval smoke. General subscription, schedule, custom-field,
channel/session setup and credential-export contracts remain covered by
``tests/test_blueprint_v11_e2e.py``, ``tests/test_blueprint_exporter_embedded.py``,
``tests/test_blueprint_installer_workflows.py`` and
``tests/test_blueprint_install_safety.py`` in the PostgreSQL regression suite.
It does not replace those broader checks.

Only Redis is replaced. Pytest loads no repository conftest, so its shared
PostgreSQL/Redis fixtures and cleanup hooks cannot run from this entry point.

Run with: uv run python -m packages.core.blueprints._smoke
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys


def main() -> int:
    root = Path(__file__).resolve().parents[3]
    return subprocess.call([
        sys.executable, "-m", "pytest", "--noconftest", "-q",
        "tests/test_blueprint_unified_approval_roundtrip.py",
        "tests/test_blueprint_unified_approval_upgrade.py",
    ], cwd=root)


if __name__ == "__main__":
    raise SystemExit(main())
