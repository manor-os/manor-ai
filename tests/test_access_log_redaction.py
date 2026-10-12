import logging
import io
import sys

import httpx
import pytest
from uvicorn.logging import AccessFormatter

from packages.core.observability.log_redaction import (
    SensitiveQueryStringFilter,
    install_sensitive_log_filter,
    redact_sensitive_log_text,
)


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


def test_uvicorn_access_formatter_keeps_required_arguments_after_redaction():
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "127.0.0.1:1234",
            "GET",
            "/ws?token=jwt-secret&workspace_id=ok",
            "1.1",
            200,
        ),
        exc_info=None,
    )

    assert SensitiveQueryStringFilter().filter(record) is True

    rendered = AccessFormatter(
        '%(client_addr)s - "%(request_line)s" %(status_code)s'
    ).format(record)
    assert "jwt-secret" not in rendered
    assert "token=<redacted>" in rendered
    assert "workspace_id=ok" in rendered
    assert "GET" in rendered
    assert "200 OK" in rendered


def test_filter_installed_on_uvicorn_websocket_logger():
    install_sensitive_log_filter()
    filters = logging.getLogger("uvicorn.error").filters

    assert any(isinstance(item, SensitiveQueryStringFilter) for item in filters)


@pytest.mark.parametrize(
    ("message", "arguments"),
    [
        ("HTTP Request: %s", (httpx.URL("https://example.test/query?apikey=secret-value&symbol=IBM"),)),
        ("apikey=%s count=%04d", ("secret-value", 7)),
        ("apikey=%(key)s", ({"key": "secret-value"},)),
    ],
)
def test_filter_redacts_rendered_format_arguments(message, arguments):
    record = logging.LogRecord("httpx", logging.INFO, __file__, 1, message, arguments, None)
    assert SensitiveQueryStringFilter().filter(record)
    rendered = record.getMessage()
    assert "secret-value" not in rendered
    assert "<redacted>" in rendered
    if "count=" in message:
        assert "count=0007" in rendered


def test_filter_redacts_traceback_and_stack_info():
    try:
        raise ValueError("failed https://example.test/?apikey=secret-value")
    except ValueError:
        exc_info = sys.exc_info()
    record = logging.LogRecord("httpx", logging.ERROR, __file__, 1, "request failed", (), exc_info)
    record.stack_info = "request https://example.test/?api_key=stack-secret"
    assert SensitiveQueryStringFilter().filter(record)
    formatted = logging.Formatter().format(record)
    assert "secret-value" not in formatted
    assert "stack-secret" not in formatted
    assert "ValueError" in formatted


@pytest.mark.parametrize("signal_name", ["after_setup_logger", "after_setup_task_logger"])
def test_celery_logging_signals_install_idempotent_filters(signal_name):
    from celery import signals
    import packages.core.celery_app  # noqa: F401

    logger = logging.Logger("review-test-worker", level=logging.INFO)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger.addHandler(handler)
    for _ in range(2):
        getattr(signals, signal_name).send(sender=None, logger=logger)
    logger.info("apikey=%s", "worker-secret")
    assert "worker-secret" not in stream.getvalue()
    assert "<redacted>" in stream.getvalue()
    assert sum(isinstance(item, SensitiveQueryStringFilter) for item in handler.filters) == 1
