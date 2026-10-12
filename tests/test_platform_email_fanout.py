"""Platform email fan-out + header-encoding tests.

Bug (staging, Gmail ``555 5.5.2``): the manor ``send_email`` platform SMTP
branch passed a *list* of recipients straight into ``send_email(to: str)``,
so ``msg["To"]`` became the Python ``repr`` of the list — a malformed
recipient Gmail refuses at RCPT. The fix fans out one clean single-recipient
message per address, RFC-2047 encodes non-ASCII subjects, and reports partial
success.

Mocks the shared SMTP transport so these tests remain independent of network
egress mode.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


def _swap_transport(mock_send):
    import packages.core.services.email_service as email_mod

    original = email_mod.smtp_transport.send_message_async
    email_mod.smtp_transport.send_message_async = mock_send
    return email_mod, original


# ── send_bulk_email: per-recipient fan-out ──────────────────────────────────


@pytest.mark.asyncio
async def test_bulk_email_sends_one_message_per_recipient():
    """A list of N recipients produces N individual sends, each with exactly
    one clean To header — never a stringified list."""
    mock_send = AsyncMock()
    email_mod, original = _swap_transport(mock_send)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("EMAIL_ENABLED", "true")
            result = await email_mod.send_bulk_email(
                ["a@corp.com", "b@corp.com", "c@corp.com"],
                "Weekly update",
                "<p>Hi</p>",
            )
        assert mock_send.call_count == 3
        tos = []
        for call in mock_send.call_args_list:
            msg = call.args[0] if call.args else call.kwargs.get("message")
            to_header = msg["To"]
            # Exactly one recipient, and never a python-list repr.
            assert "," not in to_header
            assert "[" not in to_header and "]" not in to_header
            tos.append(to_header)
        assert tos == ["a@corp.com", "b@corp.com", "c@corp.com"]
        assert result["total"] == 3
        assert result["sent"] == 3
        assert result["failed"] == []
    finally:
        email_mod.smtp_transport.send_message_async = original


@pytest.mark.asyncio
async def test_bulk_email_reports_partial_failure():
    """One failing recipient must not nuke the batch — the others still
    deliver and the failure is reported per-address."""

    async def flaky_send(*, message, **kwargs):
        if message["To"] == "bad@corp.com":
            raise RuntimeError("SMTPRecipientRefused 555 5.5.2 Syntax error")
        return None

    mock_send = AsyncMock(side_effect=flaky_send)
    email_mod, original = _swap_transport(mock_send)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("EMAIL_ENABLED", "true")
            result = await email_mod.send_bulk_email(
                ["good1@corp.com", "bad@corp.com", "good2@corp.com"],
                "Subject",
                "<p>Body</p>",
            )
        assert result["total"] == 3
        assert result["sent"] == 2
        assert len(result["failed"]) == 1
        assert result["failed"][0]["to"] == "bad@corp.com"
        assert "555" in result["failed"][0]["error"]
    finally:
        email_mod.smtp_transport.send_message_async = original


@pytest.mark.asyncio
async def test_bulk_email_accepts_single_address_string():
    """A plain string address still works and is treated as one recipient."""
    mock_send = AsyncMock()
    email_mod, original = _swap_transport(mock_send)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("EMAIL_ENABLED", "true")
            result = await email_mod.send_bulk_email(
                "solo@corp.com", "Hi", "<p>x</p>"
            )
        assert mock_send.call_count == 1
        assert result["sent"] == 1
    finally:
        email_mod.smtp_transport.send_message_async = original


@pytest.mark.asyncio
async def test_bulk_email_disabled_pretends_success():
    """EMAIL_ENABLED=false keeps the pretend-success contract for all
    recipients and never touches SMTP."""
    mock_send = AsyncMock()
    email_mod, original = _swap_transport(mock_send)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("EMAIL_ENABLED", "false")
            result = await email_mod.send_bulk_email(
                ["a@corp.com", "b@corp.com"], "Hi", "<p>x</p>"
            )
        assert mock_send.call_count == 0
        assert result["sent"] == 2
        assert result["failed"] == []
    finally:
        email_mod.smtp_transport.send_message_async = original


@pytest.mark.asyncio
async def test_announcement_email_embeds_images_and_attaches_files():
    captured = {}

    async def capture_send(*, message, **kwargs):
        captured["message"] = message

    mock_send = AsyncMock(side_effect=capture_send)
    email_mod, original = _swap_transport(mock_send)
    image_id = "01K4A1B2C3D4E5F6G7H8J9KMNP"
    file_id = "01K4A1B2C3D4E5F6G7H8J9KMNQ"
    image_url = f"/api/v1/platform/announcement-media/{image_id}/shot.png"
    file_url = f"/api/v1/platform/announcement-media/{file_id}/guide.pdf"
    image_content_id = f"announcement-{image_id}@manor.ai"
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("EMAIL_ENABLED", "true")
            ok = await email_mod.send_announcement_email(
                "reader@corp.com",
                title="Media release",
                body_md=(
                    f"![Screenshot]({image_url})\n\n"
                    f"[Download the guide]({file_url})"
                ),
                severity="info",
                media=(
                    email_mod.EmailAttachment(
                        filename="shot.png",
                        mime_type="image/png",
                        data=b"png-bytes",
                        content_id=image_content_id,
                        source_url=image_url,
                    ),
                    email_mod.EmailAttachment(
                        filename="guide.pdf",
                        mime_type="application/pdf",
                        data=b"pdf-bytes",
                        source_url=file_url,
                    ),
                ),
            )
        assert ok is True
        message = captured["message"]
        parts = list(message.walk())
        html_part = next(part for part in parts if part.get_content_type() == "text/html")
        html_body = html_part.get_payload(decode=True).decode(html_part.get_content_charset())
        assert f'src="cid:{image_content_id}"' in html_body
        assert image_url not in html_body
        assert "Download the guide — attached to this email" in html_body

        image_part = next(part for part in parts if part.get_filename() == "shot.png")
        assert image_part.get_content_disposition() == "inline"
        assert image_part["Content-ID"] == f"<{image_content_id}>"
        assert image_part.get_payload(decode=True) == b"png-bytes"

        file_part = next(part for part in parts if part.get_filename() == "guide.pdf")
        assert file_part.get_content_disposition() == "attachment"
        assert file_part.get_payload(decode=True) == b"pdf-bytes"
    finally:
        email_mod.smtp_transport.send_message_async = original


# ── RFC-2047 subject encoding ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chinese_subject_is_rfc2047_encoded():
    """A non-ASCII (Chinese) subject must serialize as an RFC-2047
    ``=?utf-8?...?=`` encoded-word, not raw UTF-8 in the header."""
    captured = {}

    async def capture_send(*, message, **kwargs):
        captured["raw"] = message.as_string()
        captured["subject_header"] = message["Subject"]
        return None

    mock_send = AsyncMock(side_effect=capture_send)
    email_mod, original = _swap_transport(mock_send)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("EMAIL_ENABLED", "true")
            ok = await email_mod.send_email(
                "user@corp.com", "每周业务简报", "<p>正文</p>"
            )
        assert ok is True
        # The serialized subject line is an RFC-2047 encoded-word.
        assert "=?utf-8?" in captured["subject_header"].lower()
        # And the raw bytes of the header are ASCII-safe.
        assert "每周业务简报" not in captured["raw"]
    finally:
        email_mod.smtp_transport.send_message_async = original


@pytest.mark.asyncio
async def test_ascii_subject_is_left_plain():
    """An ASCII subject must not be needlessly encoded (keeps existing
    template senders' headers human-readable)."""
    captured = {}

    async def capture_send(*, message, **kwargs):
        captured["subject_header"] = message["Subject"]
        return None

    mock_send = AsyncMock(side_effect=capture_send)
    email_mod, original = _swap_transport(mock_send)
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("EMAIL_ENABLED", "true")
            await email_mod.send_email("user@corp.com", "Plain subject", "<p>x</p>")
        assert captured["subject_header"] == "Plain subject"
    finally:
        email_mod.smtp_transport.send_message_async = original
