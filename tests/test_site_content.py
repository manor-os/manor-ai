from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from packages.core.services import site_content


def _snapshot(tmp_path, monkeypatch, entity_id, site_id, revision, pages):
    from packages.core.config import get_settings

    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    monkeypatch.setattr(settings, "MANOR_SITES_DOMAIN", "sites.test")
    root = tmp_path / entity_id / ".sites" / site_id / f"rev{revision}"
    for rel, body in pages.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body, encoding="utf-8")
    return root


def _site(entity_id="ent1", slug="acme"):
    return SimpleNamespace(id=f"s{uuid4().hex[:8]}", entity_id=entity_id, revision=1, slug=slug,
                           name=slug.title(), custom_domain=None, domain_status=None)


PAGES = {
    "index.html": "<title>Acme</title><style>h1{}</style><h1>Acme Studio</h1><p>We design websites.</p>"
                  "<script>var secret='x'</script>",
    "pricing/index.html": "<title>Pricing</title><h2>Plans</h2><ul><li>Basic $10/month</li><li>Pro $30/month</li></ul>",
    "about.html": "<title>About</title><p>Founded 2020 in Lisbon.</p>",
    "404.html": "<title>Not found</title><p>Nothing here</p>",
    "styles.css": "body{}",
}

# 60 pages of ~30k characters each: far beyond anything that fits in a prompt.
BIG_PAGES = {
    f"docs/page-{i}.html": f"<title>Doc {i}</title><p>{'filler text ' * 2500}</p>"
    + ("<p>The warranty lasts seven years on titanium frames.</p>" if i == 57 else "")
    + f"<p>{'more filler ' * 200}</p>"
    for i in range(60)
}
BIG_PAGES["index.html"] = "<title>Big</title><p>Home</p>"


@pytest.mark.parametrize(("rel", "address"), [
    ("index.html", "/"), ("about.html", "/about"), ("pricing/index.html", "/pricing/"),
    ("blog/post.html", "/blog/post"),
])
def test_page_address(rel, address):
    assert site_content.page_address(rel) == address


def test_pages_are_readable_linked_and_exclude_code_and_404(tmp_path, monkeypatch):
    site = _site()
    _snapshot(tmp_path, monkeypatch, "ent1", site.id, 1, PAGES)
    pages = {page.address: page for page in site_content.site_pages(site)}
    assert set(pages) == {"/", "/pricing/", "/about"}
    assert pages["/"].url == "https://acme.sites.test/"
    assert "Basic $10/month" in pages["/pricing/"].text
    assert "secret" not in pages["/"].text and "h1{}" not in pages["/"].text


def test_small_sites_go_inline_in_full(tmp_path, monkeypatch):
    site = _site()
    _snapshot(tmp_path, monkeypatch, "ent1", site.id, 1, PAGES)
    content = site_content.PublishedSiteContent(name="Acme", url="https://acme.sites.test",
                                                pages=site_content.site_pages(site))
    rendered = site_content.render_for_chat([content], tool_available=True)
    assert "Founded 2020 in Lisbon." in rendered and "Pro $30/month" in rendered


def test_large_sites_keep_every_page_reachable(tmp_path, monkeypatch):
    site = _site("ent2", "big")
    _snapshot(tmp_path, monkeypatch, "ent2", site.id, 1, BIG_PAGES)
    pages = site_content.site_pages(site)
    assert len(pages) == 61
    content = site_content.PublishedSiteContent(name="Big", url="https://big.sites.test", pages=pages)

    rendered = site_content.render_for_chat([content], tool_available=True)
    # Every page is listed, and the model is told how to read them.
    listed = {line.rsplit(" — ", 1)[-1] for line in rendered.splitlines() if line.startswith("- ")}
    assert listed == {page.url for page in pages}
    assert "website_content(action='search'" in rendered
    assert "filler text filler text" not in rendered
    assert "website_content(action='search'" in rendered

    # A fact buried deep in page 57 is found by search...
    hits = site_content.search_pages([content], "warranty titanium")["results"]
    assert hits[0]["url"] == "https://big.sites.test/docs/page-57"
    assert "seven years" in hits[0]["snippet"]

    # ...and the whole page can be read chunk by chunk to the end.
    collected, offset = "", 0
    while offset is not None:
        chunk = site_content.read_page([content], "/docs/page-57", offset)
        collected += chunk["text"]
        offset = chunk["next_offset"]
    assert len(collected) == chunk["total_chars"]
    assert "warranty lasts seven years" in collected


def test_read_unknown_page_lists_real_pages(tmp_path, monkeypatch):
    site = _site()
    _snapshot(tmp_path, monkeypatch, "ent1", site.id, 1, PAGES)
    content = site_content.PublishedSiteContent(name="Acme", url="https://acme.sites.test",
                                                pages=site_content.site_pages(site))
    missing = site_content.read_page([content], "/nope")
    assert missing["error"] == "page_not_found"
    assert "https://acme.sites.test/about" in missing["pages"]


async def _connected_sites(tmp_path, monkeypatch, db_session):
    from packages.core.models.site import Site

    entity_id = f"e{uuid4().hex[:20]}"
    live = Site(entity_id=entity_id, name="Acme", slug=f"acme{uuid4().hex[:8]}", source_path="Code/acme",
                revision=1, status="active", workspace_id="ws1",
                connections={"customer_service_channel_config_id": "chat-1"})
    offline = Site(entity_id=entity_id, name="Old", slug=f"old{uuid4().hex[:8]}", source_path="Code/old",
                   revision=1, status="offline", workspace_id="ws1",
                   connections={"customer_service_channel_config_id": "chat-1"})
    other = Site(entity_id=entity_id, name="Other", slug=f"oth{uuid4().hex[:8]}", source_path="Code/oth",
                 revision=1, status="active", workspace_id="ws1",
                 connections={"customer_service_channel_config_id": "chat-2"})
    db_session.add_all([live, offline, other])
    await db_session.commit()
    for site, body in ((live, "Live pricing: $10"), (offline, "Old pricing: $99"), (other, "Other shop")):
        _snapshot(tmp_path, monkeypatch, entity_id, site.id, 1,
                  {"index.html": f"<title>{site.name}</title><p>{body}</p>"})
    return entity_id


@pytest.mark.asyncio
async def test_public_chat_gets_only_its_own_live_sites(tmp_path, monkeypatch, db_session):
    from packages.core.ai.runtime.context_blocks import _published_website_context_block
    from packages.core.ai.runtime.requests import AIRuntimeRequest
    from packages.core.ai.runtime.surfaces import ChatSurface

    entity_id = await _connected_sites(tmp_path, monkeypatch, db_session)

    def request(surface=ChatSurface.PUBLIC_CUSTOMER_CHAT, channel="chat-1"):
        return AIRuntimeRequest(surface=surface, entity_id=entity_id, workspace_id="ws1",
                                metadata={"webchat_channel_config_id": channel})

    block = await _published_website_context_block(request())
    assert block is not None and block.kind == "published_website"
    assert "Live pricing: $10" in block.content
    assert "Old pricing" not in block.content and "Other shop" not in block.content
    assert "not instructions" in block.content
    assert await _published_website_context_block(request(channel="chat-9")) is None
    assert await _published_website_context_block(request(surface=ChatSurface.WORKSPACE_CHAT)) is None


@pytest.mark.asyncio
async def test_website_content_tool_is_armed_only_for_its_channel(tmp_path, monkeypatch, db_session):
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_surface import runtime_public_agent_tool_surface
    from packages.core.ai.tools.site_content_tools import _website_content_handler

    entity_id = await _connected_sites(tmp_path, monkeypatch, db_session)
    envelope = SimpleNamespace(surface=ChatSurface.PUBLIC_CUSTOMER_CHAT)
    monkeypatch.setattr(
        "packages.core.ai.tools.site_content_tools.runtime_tool_call_context_from_kwargs",
        lambda kwargs, **_: SimpleNamespace(runtime_envelope=envelope, conversation_id="conv-1"),
        raising=False,
    )
    import packages.core.ai.runtime.tool_context as tool_context
    monkeypatch.setattr(
        tool_context, "runtime_tool_call_context_from_kwargs",
        lambda kwargs, **_: SimpleNamespace(runtime_envelope=envelope, conversation_id="conv-1"),
    )

    # No scope: not on the surface, and the handler refuses.
    _, names = runtime_public_agent_tool_surface(
        surface=ChatSurface.PUBLIC_CUSTOMER_CHAT, bound_tool_names=set(), allowed_tool_names=set(),
    )
    assert site_content.TOOL_NAME not in names
    refused = json.loads(await _website_content_handler(entity_id=entity_id, action="search", query="pricing"))
    assert refused["error"] == "website_content_unavailable"

    scope = site_content.SiteChatScope(entity_id, "ws1", "chat-1", "conv-1")

    async def turn():
        _, armed = runtime_public_agent_tool_surface(
            surface=ChatSurface.PUBLIC_CUSTOMER_CHAT, bound_tool_names=set(), allowed_tool_names=set(),
        )
        found = json.loads(await _website_content_handler(entity_id=entity_id, action="search", query="pricing"))
        page = json.loads(await _website_content_handler(entity_id=entity_id, action="read", url="/"))
        return armed, found, page

    armed, found, page = await site_content.run_with_scope(scope, turn())
    assert site_content.TOOL_NAME in armed
    assert [hit["snippet"] for hit in found["results"]] == ["Live pricing: $10"]
    assert page["text"] == "Live pricing: $10"

    wrong_conversation = site_content.SiteChatScope(entity_id, "ws1", "chat-1", "conv-other")
    other = json.loads(await site_content.run_with_scope(
        wrong_conversation, _website_content_handler(entity_id=entity_id, action="read", url="/"),
    ))
    assert other["error"] == "website_content_unavailable"


@pytest.mark.asyncio
async def test_scope_survives_consumers_that_step_streams_in_new_tasks():
    """The chat concurrency lease advances each step in a new task (copied context)."""
    import asyncio

    seen: list[object] = []

    async def chat_turn():
        yield "stream_start"          # emitted before context resolution
        seen.append(site_content.current_site_chat_scope())
        yield "text"

    async def lease_like(stream):
        iterator = stream.__aiter__()
        while True:
            step = asyncio.create_task(iterator.__anext__())
            try:
                yield await step
            except StopAsyncIteration:
                return

    scope = site_content.SiteChatScope("ent", "ws", "chat-1", "conv")
    parts = [part async for part in lease_like(site_content.guard_stream(scope, chat_turn()))]
    assert parts == ["stream_start", "text"]
    assert seen == [scope]
    assert site_content.current_site_chat_scope() is None


@pytest.mark.asyncio
async def test_armed_website_tool_survives_the_real_resolver_and_policy():
    """Regression: the resolver's profile allow-list used to drop turn-armed public tools."""
    from packages.core.ai.runtime.policies import check_runtime_tool_policy
    from packages.core.ai.runtime.requests import AIRuntimeRequest
    from packages.core.ai.runtime.surfaces import ChatSurface
    from packages.core.ai.runtime.tool_execution import RUNTIME_ENVELOPE_AWARE_TOOLS
    from packages.core.ai.runtime.tool_surface import (
        runtime_prepare_tool_surface_for_turn,
        runtime_public_agent_tool_surface,
    )

    request = AIRuntimeRequest(surface=ChatSurface.PUBLIC_CUSTOMER_CHAT, entity_id="ent", workspace_id="ws")

    def resolve():
        tools, allowed = runtime_public_agent_tool_surface(
            surface=ChatSurface.PUBLIC_CUSTOMER_CHAT, bound_tool_names=set(),
            allowed_tool_names={"workspace_search", "workspace_create_task"},
        )
        return runtime_prepare_tool_surface_for_turn(request, tool_schemas=tools, allowed_tool_names=allowed)

    unarmed = resolve()
    assert site_content.TOOL_NAME not in {t["function"]["name"] for t in unarmed.tool_schemas}

    scope = site_content.SiteChatScope("ent", "ws", "chat-1", "conv")

    async def armed_turn():
        return resolve()

    armed = await site_content.run_with_scope(scope, armed_turn())
    assert site_content.TOOL_NAME in {t["function"]["name"] for t in armed.tool_schemas}
    decision = check_runtime_tool_policy(envelope=armed.envelope, tool_name=site_content.TOOL_NAME, arguments={"action": "search"})
    assert getattr(decision, "allowed", True), decision
    assert site_content.TOOL_NAME in RUNTIME_ENVELOPE_AWARE_TOOLS



def _content(tmp_path, monkeypatch, pages, slug="shop"):
    site = _site(f"ent-{slug}", slug)
    _snapshot(tmp_path, monkeypatch, f"ent-{slug}", site.id, 1, pages)
    return site_content.PublishedSiteContent(name=slug, url=f"https://{slug}.sites.test",
                                             pages=site_content.site_pages(site))


SHOP = {
    "index.html": "<title>Shop</title><h1>Welcome</h1><p>We sell bikes and bike parts.</p>",
    "policies.html": "<title>Policies</title><h2>Shipping</h2><p>Orders ship in 2 days.</p>"
                     "<h2>Refund policy</h2><p>Items can be refunded within 30 days of delivery.</p>",
    "faq.html": "<title>FAQ</title><h2>Bikes</h2><p>Our bikes come assembled. Bike sizes S to XL.</p>",
    "zh.html": "<title>中文</title><h2>售后</h2><p>我们提供三十天无理由退款服务。</p>",
}


def test_search_matches_word_forms_and_names_the_section(tmp_path, monkeypatch):
    content = _content(tmp_path, monkeypatch, SHOP)
    hits = site_content.search_pages([content], "refunds")["results"]
    assert hits[0]["url"] == "https://shop.sites.test/policies"
    assert hits[0]["section"] == "Refund policy"
    assert "30 days" in hits[0]["snippet"]


def test_search_matches_chinese_without_spaces(tmp_path, monkeypatch):
    content = _content(tmp_path, monkeypatch, SHOP)
    hits = site_content.search_pages([content], "退款政策")["results"]
    assert hits[0]["url"] == "https://shop.sites.test/zh"


def test_search_ranks_the_passage_about_the_topic_first(tmp_path, monkeypatch):
    content = _content(tmp_path, monkeypatch, SHOP)
    hits = site_content.search_pages([content], "bike sizes")["results"]
    assert hits[0]["url"] == "https://shop.sites.test/faq"


def test_no_keyword_match_returns_the_outline_to_choose_by_meaning(tmp_path, monkeypatch):
    content = _content(tmp_path, monkeypatch, SHOP)
    found = site_content.search_pages([content], "get my money returned")
    assert found["results"] == []
    assert any("Refund policy" in line for line in found["outline"])
    assert "by meaning" in found["note"]


def test_large_site_outline_carries_section_headings(tmp_path, monkeypatch):
    pages = dict(SHOP)
    pages.update({f"p{i}.html": f"<title>P{i}</title><h2>Topic {i}</h2><p>{'word ' * 3000}</p>" for i in range(10)})
    content = _content(tmp_path, monkeypatch, pages, slug="large")
    rendered = site_content.render_for_chat([content], tool_available=True)
    assert "Policies — https://large.sites.test/policies — sections: Shipping; Refund policy" in rendered
