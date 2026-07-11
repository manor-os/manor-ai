"""Unit tests for the announcement email Markdown renderer.

These are pure sync unit tests — no DB, no HTTP client, no fixtures.
They live in a separate file because test_platform_announcements.py has a
module-level ``pytestmark`` that parametrizes the ``client`` fixture with
``indirect=True``, which would error when applied to tests that don't
accept a ``client`` parameter.
"""

from packages.core.services.email_service import _render_announcement_body_html

# ── Markdown renderer (unit) ─────────────────────────────────────────


def test_md_renders_image():
    out = _render_announcement_body_html("![diagram](https://x.com/a.png)")
    assert '<img src="https://x.com/a.png" alt="diagram"' in out


def test_md_ignores_non_https_image():
    out = _render_announcement_body_html("![x](http://insecure.com/a.png)")
    assert "<img" not in out


def test_md_renders_table():
    md = "| Plan | Price |\n|---|---|\n| Pro | $20 |"
    out = _render_announcement_body_html(md)
    assert "<table" in out
    assert "<th>Plan</th>" in out
    assert "<td>Pro</td>" in out


def test_md_renders_hr():
    out = _render_announcement_body_html("above\n\n---\n\nbelow")
    assert "<hr" in out


def test_md_still_escapes_raw_html():
    out = _render_announcement_body_html('<script>alert(1)</script>')
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
