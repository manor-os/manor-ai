"""Email credentials must survive real-world copy-paste.

Google shows an app password as four space-separated groups
(``abcd efgh ijkl mnop``); iCloud and Yahoo do the same. Users paste the
displayed form, and every one of those providers then answers IMAP LOGIN
with ``[AUTHENTICATIONFAILED] Invalid credentials`` — indistinguishable
from a genuinely wrong password. A stray trailing space or newline from
any copy-paste has the same effect.

These providers' app passwords never legitimately contain spaces, so we
strip them. For unknown/self-hosted servers we only trim the edges,
where no server accepts whitespace either, and leave the interior alone
in case a passphrase genuinely contains a space.
"""

from __future__ import annotations

from packages.core.services.email_credentials import normalize_email_credentials


def test_gmail_app_password_spaces_removed():
    creds = normalize_email_credentials({
        "imap_host": "imap.gmail.com",
        "username": "alice@gmail.com",
        "password": "abcd efgh ijkl mnop",
    })
    assert creds["password"] == "abcdefghijklmnop"


def test_icloud_app_password_spaces_removed():
    creds = normalize_email_credentials({
        "imap_host": "imap.mail.me.com",
        "username": "alice@icloud.com",
        "password": "abcd efgh ijkl mnop",
    })
    assert creds["password"] == "abcdefghijklmnop"


def test_unknown_host_keeps_interior_spaces():
    """A self-hosted server may use a real passphrase."""
    creds = normalize_email_credentials({
        "imap_host": "mail.selfhosted.example",
        "username": "alice@selfhosted.example",
        "password": "  correct horse battery  ",
    })
    assert creds["password"] == "correct horse battery"


def test_edges_trimmed_everywhere():
    creds = normalize_email_credentials({
        "imap_host": "  imap.fastmail.com \n",
        "smtp_host": " smtp.fastmail.com ",
        "username": " alice@fastmail.com ",
        "password": " secret\n",
        "from_address": " Alice <alice@fastmail.com> ",
    })
    assert creds["imap_host"] == "imap.fastmail.com"
    assert creds["smtp_host"] == "smtp.fastmail.com"
    assert creds["username"] == "alice@fastmail.com"
    assert creds["password"] == "secret"
    assert creds["from_address"] == "Alice <alice@fastmail.com>"


def test_gmail_detected_from_smtp_host_alone():
    """Send-only Gmail bundles have no imap_host."""
    creds = normalize_email_credentials({
        "smtp_host": "smtp.gmail.com",
        "username": "alice@gmail.com",
        "password": "abcd efgh ijkl mnop",
    })
    assert creds["password"] == "abcdefghijklmnop"


def test_non_string_values_untouched():
    creds = normalize_email_credentials({
        "imap_host": "imap.gmail.com",
        "imap_port": 993,
        "use_ssl_imap": True,
        "username": "alice@gmail.com",
        "password": "abcd efgh ijkl mnop",
    })
    assert creds["imap_port"] == 993
    assert creds["use_ssl_imap"] is True


def test_secret_sentinel_is_never_rewritten():
    """The ``__unchanged__`` marker must reach the merge logic intact."""
    creds = normalize_email_credentials({
        "imap_host": "imap.gmail.com",
        "username": "alice@gmail.com",
        "password": "__unchanged__",
    })
    assert creds["password"] == "__unchanged__"


def test_input_dict_not_mutated():
    original = {
        "imap_host": "imap.gmail.com",
        "username": "alice@gmail.com",
        "password": "abcd efgh ijkl mnop",
    }
    normalize_email_credentials(original)
    assert original["password"] == "abcd efgh ijkl mnop"


def test_empty_and_missing_fields_are_safe():
    assert normalize_email_credentials({}) == {}
    creds = normalize_email_credentials({"username": None, "password": ""})
    assert creds["username"] is None
    assert creds["password"] == ""
