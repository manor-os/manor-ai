#!/usr/bin/env python3
"""Keep the ledger of web asset paths that releases have published.

The asset store keeps every file a release publishes under /assets/ forever,
and publish.sh refuses different bytes at a path it already holds (an
"Immutable asset collision" that blocks the whole release). Files under
apps/web/public/assets keep their names in the build, so changing one in
place can never deploy. The ledger records each published path's sha256,
including paths since deleted, so that mistake fails before merge:

    check   every public asset is recorded with the bytes on disk
    record  add new paths; never rewrites or drops a recorded one

To change a published file, give the new bytes a new path (for example a
-v2 suffix) and point its references there.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
PUBLIC_ASSETS = ROOT / "apps/web/public/assets"
LEDGER = ROOT / "apps/web/published-assets.sha256"


def read_ledger(ledger: Path = LEDGER) -> dict[str, str]:
    recorded: dict[str, str] = {}
    for line in ledger.read_text().splitlines():
        digest, path = line.split("  ", 1)
        recorded[path] = digest
    return recorded


def current_assets(assets: Path = PUBLIC_ASSETS) -> dict[str, str]:
    return {
        path.relative_to(assets).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(assets.rglob("*"))
        if path.is_file()
    }


def changed_in_place(recorded: dict[str, str], current: dict[str, str]) -> list[str]:
    return sorted(path for path, digest in current.items() if path in recorded and recorded[path] != digest)


def unrecorded(recorded: dict[str, str], current: dict[str, str]) -> list[str]:
    return sorted(set(current) - set(recorded))


def write_ledger(entries: dict[str, str], ledger: Path = LEDGER) -> None:
    ledger.write_text("".join(f"{digest}  {path}\n" for path, digest in sorted(entries.items())))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("operation", choices=["check", "record"])
    args = parser.parse_args()
    recorded, current = read_ledger(LEDGER), current_assets(PUBLIC_ASSETS)
    changed = changed_in_place(recorded, current)
    for path in changed:
        print(f"Published asset changed in place: assets/{path}; give the new bytes a new path", file=sys.stderr)
    if args.operation == "record":
        if changed:
            return 1
        added = unrecorded(recorded, current)
        write_ledger(recorded | {path: current[path] for path in added}, LEDGER)
        print(f"Recorded {len(added)} new published asset paths")
        return 0
    missing = unrecorded(recorded, current)
    for path in missing:
        print(f"Unrecorded public asset: assets/{path}", file=sys.stderr)
    if missing:
        print("Run: python3 scripts/web_asset_ledger.py record", file=sys.stderr)
    return 1 if changed or missing else 0


if __name__ == "__main__":
    sys.exit(main())
