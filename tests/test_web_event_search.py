from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

import pytest

from packages.core.ai.tools import web_tools
from packages.core.services.dashboard_agent import dashboard_tool_is_read_only


EVENT_PAGE = b"""
<html><body>
  <article itemscope itemtype="https://schema.org/Event">
    <a itemprop="url" href="/events/open-air-film"><span itemprop="name">Open Air Film</span></a>
    <meta itemprop="startDate" content="2026-07-14T19:00:00-07:00">
    <meta itemprop="endDate" content="2026-07-14T21:00:00-07:00">
    <div itemprop="location"><span itemprop="name">Waterfront Park</span></div>
  </article>
  <div class="post event-card">
    <div class="title"><a href="/events/night-market" title="Night Market">Night Market</a></div>
    <div data-event-date="2026-07-15 18:00" data-event-date-end="2026-07-15 22:00"></div>
    <p>Food and local makers.</p>
  </div>
  <div class="event">
    <h4><a href="/events/museum-installation">Museum Installation</a></h4>
    <h6>Ongoing</h6>
    <p>Open throughout the week.</p>
  </div>
</body></html>
"""

TOPIC_EVENT_PAGE = b"""
<html><body>
  <article itemscope itemtype="https://schema.org/Event">
    <a itemprop="url" href="/events/robotics-builders"><span itemprop="name">Robotics Builders Meetup</span></a>
    <meta itemprop="startDate" content="2026-07-14T18:00:00-07:00">
    <div itemprop="location"><span itemprop="name">North Harbor</span></div>
  </article>
  <article itemscope itemtype="https://schema.org/Event">
    <a itemprop="url" href="/events/pottery-night"><span itemprop="name">Pottery and Tea Night</span></a>
    <meta itemprop="startDate" content="2026-07-15T19:00:00-07:00">
    <div itemprop="location"><span itemprop="name">North Harbor</span></div>
  </article>
</body></html>
"""


def test_extract_web_events_returns_structured_dates_and_urls() -> None:
    events = web_tools._extract_events_from_html(
        EVENT_PAGE,
        "https://events.example/calendar",
        "North Harbor",
        date(2026, 7, 12),
        date(2026, 7, 18),
    )

    assert [event["title"] for event in events] == [
        "Open Air Film",
        "Night Market",
    ]
    assert events[0]["start_at"].startswith("2026-07-14T19:00")
    assert events[0]["venue"] == "Waterfront Park"
    assert events[1]["url"] == "https://events.example/events/night-market"


@pytest.mark.asyncio
async def test_web_event_search_fetches_and_deduplicates_event_pages(monkeypatch) -> None:
    async def fake_search(_entity_id: str, **_kwargs):
        return json.dumps(
            {
                "results": [
                    {"url": "https://events.example/calendar"},
                    {"url": "https://events.example/calendar"},
                ]
            }
        )

    @dataclass
    class FakeFetchResult:
        content: bytes = EVENT_PAGE
        content_type: str = "text/html; charset=utf-8"
        url: str = "https://events.example/calendar"

    async def fake_fetch(_url: str, **_kwargs):
        return FakeFetchResult()

    monkeypatch.setattr(web_tools, "_web_search", fake_search)
    monkeypatch.setattr(web_tools, "fetch_url", fake_fetch)

    payload = json.loads(
        await web_tools._web_event_search(
            "entity-test",
            query="North Harbor",
            start_date="2026-07-12",
            end_date="2026-07-18",
            num_results=20,
        )
    )

    assert len(payload["events"]) == 2
    assert payload["sources_checked"] == ["https://events.example/calendar"]
    assert dashboard_tool_is_read_only("web_event_search") is True


@pytest.mark.asyncio
async def test_web_event_search_keeps_location_and_topics_separate(monkeypatch) -> None:
    async def fake_search(_entity_id: str, **_kwargs):
        return json.dumps({"results": [{"url": "https://events.example/calendar"}]})

    @dataclass
    class FakeFetchResult:
        content: bytes = TOPIC_EVENT_PAGE
        content_type: str = "text/html; charset=utf-8"
        url: str = "https://events.example/calendar"

    async def fake_fetch(_url: str, **_kwargs):
        return FakeFetchResult()

    monkeypatch.setattr(web_tools, "_web_search", fake_search)
    monkeypatch.setattr(web_tools, "fetch_url", fake_fetch)

    payload = json.loads(
        await web_tools._web_event_search(
            "entity-test",
            location="North Harbor",
            topics=["robotics"],
            start_date="2026-07-12",
            end_date="2026-07-18",
            num_results=20,
        )
    )

    assert payload["location"] == "North Harbor"
    assert payload["topics"] == ["robotics"]
    assert [event["title"] for event in payload["events"]] == [
        "Robotics Builders Meetup"
    ]
    assert payload["events"][0]["location_query"] == "North Harbor"


def test_web_event_search_schema_exposes_structured_intent() -> None:
    properties = web_tools.WEB_EVENT_SEARCH_SCHEMA["function"]["parameters"]["properties"]
    assert properties["location"]["type"] == "string"
    assert properties["topics"]["items"] == {"type": "string"}


@pytest.mark.asyncio
async def test_web_event_search_uses_only_dated_relevant_search_results(monkeypatch) -> None:
    async def fake_search(_entity_id: str, **_kwargs):
        return json.dumps(
            {
                "results": [
                    {
                        "title": "Robotics Systems Summit",
                        "url": "https://events.example/robotics-summit",
                        "snippet": "Robotics builders meet on July 18, 2026 in North Harbor.",
                    },
                    {
                        "title": "Pottery and Tea Night",
                        "url": "https://events.example/pottery-night",
                        "snippet": "A pottery workshop on July 17, 2026.",
                    },
                    {
                        "title": "North Harbor Robotics Events",
                        "url": "https://events.example/robotics-directory",
                        "snippet": "A directory of upcoming robotics events with no confirmed date.",
                    },
                ]
            }
        )

    @dataclass
    class EmptyFetchResult:
        content: bytes = b"<html></html>"
        content_type: str = "text/html; charset=utf-8"
        url: str = "https://events.example/calendar"

    async def fake_fetch(url: str, **_kwargs):
        return EmptyFetchResult(url=url)

    monkeypatch.setattr(web_tools, "_web_search", fake_search)
    monkeypatch.setattr(web_tools, "fetch_url", fake_fetch)

    payload = json.loads(
        await web_tools._web_event_search(
            "entity-test",
            location="North Harbor",
            topics=["robotics"],
            start_date="2026-07-12",
            end_date="2026-07-18",
        )
    )

    assert [event["title"] for event in payload["events"]] == [
        "Robotics Systems Summit"
    ]
    assert payload["events"][0]["start_at"].startswith("2026-07-18T00:00:00")


def test_web_event_search_is_registered() -> None:
    names = [schema["function"]["name"] for schema, _handler in web_tools.get_tools()]
    assert names == ["web_search", "web_event_search"]
