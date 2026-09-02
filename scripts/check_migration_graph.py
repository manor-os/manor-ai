"""Validate the Alembic revision graph without loading the test application."""

from __future__ import annotations

import sys
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "packages/core/migrations"))
    script = ScriptDirectory.from_config(config)

    try:
        heads = script.get_heads()
    except KeyError as exc:
        missing_revision = exc.args[0] if exc.args else "<unknown>"
        raise SystemExit(
            "Alembic migration graph references missing parent revision "
            f"{missing_revision!r}."
        ) from exc

    if len(heads) != 1:
        raise SystemExit(f"Expected one Alembic head; found {heads!r}.")
    print(f"Alembic migration graph OK: head={heads[0]}")


if __name__ == "__main__":
    main()
