"""Concurrent agent tool calls must not interleave on one browser page."""

import asyncio
import json

import pytest

from packages.core.ai.tools import browser_tools
from packages.core.services import browser_service


@pytest.mark.asyncio
async def test_parallel_browse_calls_keep_navigation_and_content_together(monkeypatch):
    class Session:
        def __init__(self):
            self.operation_lock = asyncio.Lock()
            self.url = ""

        async def navigate(self, url):
            self.url = url
            await asyncio.sleep(0)
            return {"url": url, "title": url, "status": 200}

        async def get_content(self):
            await asyncio.sleep(0)
            return self.url

    session = Session()

    async def get_session(_entity_id):
        return session

    monkeypatch.setattr(browser_tools, "_get_or_create_session", get_session)
    answers = await asyncio.gather(
        browser_tools._browse_web(entity_id="test", url="https://example.test/a"),
        browser_tools._browse_web(entity_id="test", url="https://example.test/b"),
    )

    for answer in answers:
        payload = json.loads(answer)
        assert payload["content"] == payload["url"]


@pytest.mark.asyncio
async def test_page_evaluate_has_bounded_runtime(monkeypatch):
    class HungPage:
        async def evaluate(self, _script):
            await asyncio.Event().wait()

    session = browser_service.BrowserSession("test", "entity")
    session.status = "running"
    session.page = HungPage()
    monkeypatch.setattr(browser_service, "EVALUATE_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(RuntimeError, match="Page evaluate timed out"):
        await session.evaluate("while (true) {}")
