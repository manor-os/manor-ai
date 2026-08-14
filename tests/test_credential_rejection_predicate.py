"""Telling "the provider rejected these credentials" apart from "we
could not reach the provider".

Only the first justifies taking an integration away from agents. A DNS
blip, a timeout, or a 502 says nothing about whether the stored secret is
good, and disabling a working integration because the network hiccupped
is worse than letting one call fail. So the predicate has to be positive
about auth rejection and silent about everything else — a false positive
here silently removes a capability the user configured correctly.
"""

from __future__ import annotations

import pytest

from packages.core.services.integration_health import is_credential_rejection


@pytest.mark.parametrize("detail", [
    # IMAP — what Gmail returns for a wrong password, a normal account
    # password used instead of an app password, or a bare username.
    "IMAP login failed: b'[AUTHENTICATIONFAILED] Invalid credentials (Failure)'",
    # SMTP — Gmail's rejection, seen on the same account.
    "SMTP login failed: (535, b'5.7.8 Username and Password not accepted.')",
    # Bearer providers — _test_with_bearer's wording for 401/403.
    "401 — token rejected; reconnect.",
    "403 — token rejected; reconnect.",
    # Mixed: one half of an email bundle rejected is still a rejection.
    "IMAP login OK (imap.gmail.com:993); SMTP login failed: (535, b'5.7.8 "
    "Username and Password not accepted.')",
    # Case must not matter.
    "imap login failed: [authenticationfailed] invalid credentials",
])
def test_recognizes_credential_rejection(detail):
    assert is_credential_rejection(detail) is True


@pytest.mark.parametrize("detail", [
    # Transient / environmental — the secret may well be fine.
    "Network error: [Errno -2] Name or service not known",
    "Network error: timed out",
    "Cannot reach IMAP imap.gmail.com:993: [Errno 111] Connection refused",
    "HTTP 502: Bad Gateway",
    "HTTP 503: upstream temporarily unavailable",
    "Network error: The read operation timed out",
    # Config problems the user must fix, but not a rejected secret —
    # these must not be swallowed into the auth bucket either.
    "Missing username / password.",
    "Missing imap_host / smtp_host.",
    # Healthy.
    "IMAP login OK (imap.gmail.com:993); SMTP login OK (smtp.gmail.com:587)",
    "reachable + authorized",
])
def test_does_not_flag_transient_or_unrelated(detail):
    assert is_credential_rejection(detail) is False


@pytest.mark.parametrize("detail", ["", None])
def test_empty_detail_is_not_a_rejection(detail):
    assert is_credential_rejection(detail) is False


def test_latency_like_numbers_do_not_trigger_it():
    """A bare '535' must not match something that merely contains it."""
    assert is_credential_rejection("Network error after 535 ms") is False
