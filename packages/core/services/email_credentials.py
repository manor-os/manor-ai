"""Normalization for hand-entered IMAP/SMTP credential bundles.

Mail servers answer a whitespace-damaged password with the same
``Invalid credentials`` they use for a genuinely wrong one, so a stray
space costs the user a long debugging session. The one place that can
prevent it is the write path — normalize once, before the bundle is
sealed into the credential store.
"""
from __future__ import annotations

from typing import Any, Dict

# Marker the API uses for "keep the stored secret"; it must reach the
# merge logic byte-for-byte.
_SECRET_SENTINEL = "__unchanged__"

_TRIMMED_FIELDS = (
    "imap_host", "smtp_host", "host",
    "username", "email", "password",
    "from_address", "from_email",
)

# Providers that issue fixed-length app passwords and display them in
# space-separated groups. None of them permits a space in the actual
# secret, so interior whitespace is always display formatting.
_APP_PASSWORD_HOST_MARKERS = (
    "gmail", "googlemail", "google",
    "mail.me.com", "icloud",
    "yahoo",
)


def _uses_app_passwords(creds: Dict[str, Any]) -> bool:
    hosts = " ".join(
        str(creds.get(key) or "").lower()
        for key in ("imap_host", "smtp_host", "host")
    )
    return any(marker in hosts for marker in _APP_PASSWORD_HOST_MARKERS)


def normalize_email_credentials(creds: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy with copy-paste damage removed.

    Edge whitespace is trimmed on every text field. Interior spaces are
    removed from the password only for providers whose app passwords are
    displayed in groups — elsewhere a passphrase may legitimately
    contain one.
    """
    out = dict(creds)

    for key in _TRIMMED_FIELDS:
        value = out.get(key)
        if isinstance(value, str):
            out[key] = value.strip()

    password = out.get("password")
    if (
        isinstance(password, str)
        and password != _SECRET_SENTINEL
        and _uses_app_passwords(out)
    ):
        out["password"] = "".join(password.split())

    return out
