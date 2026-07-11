"""Single source of truth for date-based external API version pins.

Some vendors version their APIs by date (Meta Graph, LinkedIn, GitHub,
Stripe). Versions stay live only a finite window — Meta retains
graph-api versions for ~24 months, LinkedIn rotates every ~12 months,
GitHub keeps date-headers basically forever. Pinning these literally
inside each MCP wrapper means a quiet bug clock: a year later the
provider returns 400 with no warning.

This module is the **only** place these pins live. Each entry carries:

  * ``value``      — the literal version string we send (e.g. ``"v22.0"``)
  * ``released``   — when the vendor first published this version
  * ``eol_months`` — vendor's documented EOL window from release
  * ``notes``      — link to the vendor's changelog so a future-you can
                     verify the next bump

CI runs ``scripts/check_api_versions.py`` against this module and
fails when any pin enters the last 10% of its EOL window. That gives
~2 months of lead time on Meta, ~1 month on LinkedIn — well above the
typical 1-day notice we'd otherwise get from a 400 in production.

Bumping
───────
1. Read the vendor's changelog (``notes`` link below).
2. Edit ``value`` + ``released`` here.
3. Run ``python scripts/check_api_versions.py``  (no errors expected).
4. Open a PR. CI will re-run the same check.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List


@dataclass(frozen=True)
class APIVersion:
    """One vendor-version pin with metadata for staleness detection."""
    name: str
    value: str
    released: date
    eol_months: int
    notes: str = ""

    def age_days(self, today: date) -> int:
        return (today - self.released).days

    def lifetime_days(self) -> int:
        # 30-day months are an approximation; vendors don't publish to
        # the day, and being slightly conservative is what we want.
        return self.eol_months * 30

    def stale_pct(self, today: date) -> float:
        """0.0 = brand new, 1.0 = at EOL, >1.0 = past EOL."""
        lt = self.lifetime_days()
        return 0.0 if lt <= 0 else self.age_days(today) / lt


# ── Meta Graph API (Facebook Pages, WhatsApp Cloud, Messenger, Marketing) ──
#
# Meta releases ~3 versions/year; each is supported for ~24 months from
# release. Used by:
#   - packages/core/ai/mcp/facebook.py
#   - packages/core/services/channels/whatsapp_adapter.py
#   - packages/core/services/integration_health.py (WhatsApp probes)

META_GRAPH = APIVersion(
    name="Meta Graph API",
    value="v22.0",
    released=date(2025, 1, 21),
    eol_months=24,
    notes="https://developers.facebook.com/docs/graph-api/changelog",
)


# ── LinkedIn REST API ──────────────────────────────────────────────────────
#
# Header `LinkedIn-Version: YYYYMM`. Versions retire ~12 months after
# release. Used by:
#   - packages/core/ai/mcp/linkedin.py

LINKEDIN = APIVersion(
    name="LinkedIn API",
    value="202509",
    released=date(2025, 9, 1),
    eol_months=12,
    notes="https://learn.microsoft.com/en-us/linkedin/marketing/versioning",
)


# ── GitHub API ─────────────────────────────────────────────────────────────
#
# Header `X-GitHub-Api-Version: YYYY-MM-DD`. GitHub retains date headers
# essentially forever (their docs say "indefinitely"); we still keep
# this in the central registry so the CI report shows it green.
# Used by:
#   - packages/core/ai/mcp/github.py

GITHUB = APIVersion(
    name="GitHub API",
    value="2022-11-28",
    released=date(2022, 11, 28),
    eol_months=120,
    notes="https://docs.github.com/en/rest/overview/api-versions",
)


# ── All entries — referenced by scripts/check_api_versions.py ──────────────

ALL: List[APIVersion] = [META_GRAPH, LINKEDIN, GITHUB]
