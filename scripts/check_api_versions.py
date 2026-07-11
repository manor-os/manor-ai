"""CI guard: fail when a pinned external API version is near EOL.

Reads ``packages.core.external_api_versions.ALL`` and computes how far
into each vendor's EOL window the current pin is. Output is human-
readable and exit code is the gate signal:

  exit 0   — all pins healthy (< warn threshold of EOL)
  exit 0   — warn-only (>= warn, < fail threshold) — prints WARN lines
  exit 1   — at least one pin past the fail threshold

Defaults:
  warn  at 60% of vendor's documented EOL window
  fail  at 90% of vendor's documented EOL window

Override via env vars ``API_VERSION_WARN_PCT`` / ``API_VERSION_FAIL_PCT``
(integers 0-100).

Usage:
  python scripts/check_api_versions.py
  python scripts/check_api_versions.py --json    # machine-readable
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

# Importable as ``python -m`` because we add the repo root to sys.path
# when run directly from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from packages.core.external_api_versions import ALL


def _pct_env(name: str, default: int) -> float:
    raw = os.environ.get(name, "")
    try:
        v = int(raw) if raw else default
    except ValueError:
        v = default
    return max(0, min(100, v)) / 100.0


WARN_PCT = _pct_env("API_VERSION_WARN_PCT", 60)
FAIL_PCT = _pct_env("API_VERSION_FAIL_PCT", 90)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = parser.parse_args()

    today = date.today()
    rows = []
    fail_count = 0
    warn_count = 0
    for v in ALL:
        pct = v.stale_pct(today)
        age = v.age_days(today)
        lt = v.lifetime_days()
        if pct >= FAIL_PCT:
            level, fail_count = "FAIL", fail_count + 1
        elif pct >= WARN_PCT:
            level, warn_count = "WARN", warn_count + 1
        else:
            level = "OK"
        rows.append({
            "name": v.name,
            "value": v.value,
            "released": v.released.isoformat(),
            "age_days": age,
            "eol_days": lt,
            "stale_pct": round(pct * 100, 1),
            "level": level,
            "notes": v.notes,
        })

    if args.json:
        print(json.dumps({
            "today": today.isoformat(),
            "warn_pct": int(WARN_PCT * 100),
            "fail_pct": int(FAIL_PCT * 100),
            "fail_count": fail_count,
            "warn_count": warn_count,
            "rows": rows,
        }, indent=2))
    else:
        # Plain human report — column widths picked for typical CI logs.
        print(f"External API version check — today={today.isoformat()}  "
              f"warn>={int(WARN_PCT*100)}%  fail>={int(FAIL_PCT*100)}%")
        print("-" * 90)
        print(f"  {'STATUS':6}  {'VENDOR':22}  {'PIN':12}  {'AGE':>10}  {'STALE':>7}")
        print("-" * 90)
        for r in rows:
            print(
                f"  {r['level']:6}  {r['name']:22}  {r['value']:12}  "
                f"{r['age_days']:>4}d/{r['eol_days']:>4}d  {r['stale_pct']:>5}%"
            )
            if r["level"] != "OK":
                print(f"            ↳ {r['notes']}")
        print("-" * 90)
        if fail_count:
            print(
                f"FAIL: {fail_count} pin(s) past {int(FAIL_PCT*100)}% of EOL — bump in "
                "packages/core/external_api_versions.py before merge."
            )
        elif warn_count:
            print(f"WARN: {warn_count} pin(s) entering staleness — schedule a bump.")
        else:
            print("All pins healthy.")

    return 1 if fail_count else 0


if __name__ == "__main__":
    sys.exit(main())
