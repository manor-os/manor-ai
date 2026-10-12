"""Credential-free UI links derived from exact runtime provider identities."""
from __future__ import annotations

import re
from urllib.parse import quote


def runtime_integration_setup_link(provider: str) -> dict[str, str]:
    if not re.fullmatch(r"[a-zA-Z0-9_][a-zA-Z0-9_-]{0,127}", provider or ""):
        return {}
    url = f"/integrations?provider={quote(provider, safe='')}"
    return {
        "setup_url": url,
        "setup_hint": (
            f"When asking the user to configure this integration, include a Markdown link "
            f"[{provider}]({url}); use its display name in the user's language as the label. "
            "The UI renders its icon and opens the existing setup panel. "
            "Opening this link does not connect an account or grant permissions. "
            "Do not invent an account email address or claim the connection is ready."
        ),
    }
