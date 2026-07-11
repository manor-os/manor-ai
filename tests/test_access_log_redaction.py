import logging

from apps.api.main import SensitiveQueryStringFilter, redact_sensitive_log_text


def test_redacts_sensitive_query_values_from_text():
    text = (
        '192.0.2.1 - "WebSocket /ws?token=jwt-secret&workspace_id=ok'
        '&access_token=oauth-secret&code=auth-code HTTP/1.1" [accepted]'
    )

    redacted = redact_sensitive_log_text(text)

    assert "jwt-secret" not in redacted
    assert "oauth-secret" not in redacted
    assert "auth-code" not in redacted
    assert "workspace_id=ok" in redacted
    assert "token=<redacted>" in redacted
    assert "access_token=<redacted>" in redacted
    assert "code=<redacted>" in redacted


def test_redacts_sensitive_query_values_from_log_record_args():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "WebSocket %s" [accepted]',
        args=("127.0.0.1:1234", "/ws?token=jwt-secret&tab=goals"),
        exc_info=None,
    )

    assert SensitiveQueryStringFilter().filter(record) is True

    rendered = record.getMessage()
    assert "jwt-secret" not in rendered
    assert "token=<redacted>" in rendered
    assert "tab=goals" in rendered


def test_filter_installed_on_uvicorn_websocket_logger():
    filters = logging.getLogger("uvicorn.error").filters

    assert any(isinstance(item, SensitiveQueryStringFilter) for item in filters)
