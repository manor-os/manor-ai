from __future__ import annotations

import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


def script_directory(root: Path | None = None) -> ScriptDirectory:
    root = root or Path(__file__).resolve().parents[1]
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "packages/core/migrations"))
    return ScriptDirectory.from_config(config)
