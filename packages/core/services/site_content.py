"""Readable content of published sites, for the customer-service chat connected to them.

A published snapshot is already public on the internet, so its text can be
given to the public chat agent without any Knowledge ACL decision: it reveals
nothing a visitor cannot already read. Only the live revision is used.

Nothing is truncated away: the chat always gets an index of every page, the
full text inline when the sites are small, and the website_content tool to
search every page and read any page in full, however large the site is.
"""
from __future__ import annotations

import os
import re
from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
from html.parser import HTMLParser

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from packages.core.models.site import Site
from packages.core.services import site_publisher as sp

# Below this total the full text goes inline; above it the index plus the
# website_content tool carry the content. Neither path drops any page.
INLINE_TEXT_CHARS = 12_000
READ_CHUNK_CHARS = 8_000
SEARCH_RESULTS = 8
PASSAGE_CHARS = 800
# The outline lists every page; headings are dropped only if it would exceed this.
OUTLINE_WITH_HEADINGS_CHARS = 40_000
HEADINGS_PER_PAGE = 12
TOOL_NAME = "website_content"
_SKIPPED_TAGS = {"script", "style", "noscript", "template", "svg"}
_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "header", "footer", "nav", "li", "ul", "ol",
    "h1", "h2", "h3", "h4", "h5", "h6", "br", "tr", "td", "th", "table", "form", "label",
    "button", "blockquote", "dd", "dt",
}
_SPACE_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_LINES_RE = re.compile(r"\n\s*\n+")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self._in_title = False
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")
        if tag in {"h1", "h2", "h3"}:
            self.parts.append("# ")

    def handle_endtag(self, tag):
        if tag in _SKIPPED_TAGS and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False
        elif tag in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data
            return
        self.parts.append(data)

    def text(self) -> str:
        joined = _SPACE_RE.sub(" ", "".join(self.parts))
        lines = [line.strip() for line in joined.split("\n")]
        return _BLANK_LINES_RE.sub("\n\n", "\n".join(line for line in lines if line)).strip()


def page_address(rel_path: str) -> str:
    """The address a snapshot file is served at: about.html -> /about, x/index.html -> /x/."""
    rel = rel_path.replace(os.sep, "/")
    if rel == "index.html":
        return "/"
    if rel.endswith("/index.html"):
        return "/" + rel[: -len("index.html")]
    if rel.endswith((".html", ".htm")):
        return "/" + rel.rsplit(".", 1)[0]
    return "/" + rel


@dataclass(frozen=True)
class SitePage:
    address: str   # e.g. /pricing/
    url: str       # absolute public URL
    title: str
    text: str

    @property
    def headings(self) -> tuple[str, ...]:
        return tuple(
            line[2:].strip()
            for line in self.text.splitlines()
            if line.startswith("# ") and line[2:].strip()
        )


@lru_cache(maxsize=64)
def _snapshot_pages(root: str, revision: int, base_url: str) -> tuple[SitePage, ...]:
    """Every HTML page of one immutable snapshot revision (cached: revisions never change)."""
    del revision  # part of the cache key only
    found: list[tuple[str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            if not name.lower().endswith((".html", ".htm")) or name == "404.html":
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                continue
            found.append((os.path.relpath(full, root).replace(os.sep, "/"), full))
    found.sort(key=lambda item: (item[0] != "index.html", item[0].count("/"), item[0]))
    pages = []
    for rel, full in found:
        with open(full, encoding="utf-8", errors="replace") as handle:
            parser = _TextExtractor()
            parser.feed(handle.read())
        address = page_address(rel)
        pages.append(SitePage(
            address=address,
            url=f"{base_url}{address}",
            title=parser.title.strip() or address,
            text=parser.text(),
        ))
    return tuple(pages)


def site_pages(site: Site) -> tuple[SitePage, ...]:
    root = sp.site_serving_root(site.entity_id, site)
    if not os.path.isdir(root):
        return ()
    return _snapshot_pages(os.path.realpath(root), int(site.revision), sp.site_public_url(site))


@dataclass(frozen=True)
class PublishedSiteContent:
    name: str
    url: str
    pages: tuple[SitePage, ...]

    @property
    def text_chars(self) -> int:
        return sum(len(page.text) for page in self.pages)


async def published_sites_for_webchat(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str,
    channel_config_id: str,
) -> list[PublishedSiteContent]:
    """Every live site whose customer-service chat is this webchat channel."""
    sites = (await db.scalars(
        select(Site)
        .where(
            Site.entity_id == entity_id,
            Site.workspace_id == workspace_id,
            Site.status == "active",
            Site.revision >= 1,
            Site.connections["customer_service_channel_config_id"].astext == channel_config_id,
        )
        .order_by(Site.published_at.desc().nullslast())
    )).all()
    contents = []
    for site in sites:
        pages = site_pages(site)
        if pages:
            contents.append(PublishedSiteContent(name=site.name, url=sp.site_public_url(site), pages=pages))
    return contents


def render_for_chat(sites: list[PublishedSiteContent], *, tool_available: bool) -> str:
    """Page index for every site, plus full text when it fits inline."""
    inline = sum(site.text_chars for site in sites) <= INLINE_TEXT_CHARS
    all_pages = [page for site in sites for page in site.pages]
    with_headings = (
        sum(len("\n".join(outline_lines((page,), with_headings=True))) for page in all_pages)
        <= OUTLINE_WITH_HEADINGS_CHARS
    )
    sections = []
    for site in sites:
        lines = [f"# {site.name} — {site.url} ({len(site.pages)} pages)"]
        if inline:
            for page in site.pages:
                lines.append(f"## {page.title} ({page.url})\n{page.text}")
        else:
            lines.extend(outline_lines(site.pages, with_headings=with_headings))
        sections.append("\n".join(lines))
    if not inline:
        how = (
            f"The full text is larger than fits here; below is every page with its "
            f"section headings. Before answering, call {TOOL_NAME}(action='search', "
            f"query=...) with the visitor's key words (try synonyms if nothing matches), "
            f"or pick the page whose title or headings mean what the visitor asks, then "
            f"{TOOL_NAME}(action='read', url=...) to read it in full."
            if tool_available
            else "Only the page index fits here; link visitors to the most relevant page."
        )
        sections.insert(0, how)
    return "\n\n".join(sections)


def outline_lines(pages, *, with_headings: bool) -> list[str]:
    lines = []
    for page in pages:
        line = f"- {page.title} — {page.url}"
        headings = [h for h in page.headings if h != page.title][:HEADINGS_PER_PAGE] if with_headings else []
        if headings:
            line += " — sections: " + "; ".join(h[:80] for h in headings)
        lines.append(line)
    return lines


# ── search: BM25 over passages ──────────────────────────────────────────────

_WORD_RE = re.compile(r"[a-z0-9]+")
_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]+")
_SUFFIXES = ("ingly", "edly", "ing", "ied", "ies", "ed", "es", "ly", "s")
_STOPWORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "is", "are", "be", "do",
    "does", "can", "i", "you", "your", "my", "we", "our", "it", "what", "how", "with", "at",
})


def _stem(word: str) -> str:
    for suffix in _SUFFIXES:
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix in {"ied", "ies"} else "")
    return word


def tokenize(text: str) -> list[str]:
    """Stemmed words plus CJK character bigrams (and single characters)."""
    lowered = text.lower()
    tokens = [_stem(word) for word in _WORD_RE.findall(lowered) if word not in _STOPWORDS]
    for run in _CJK_RE.findall(lowered):
        if len(run) == 1:
            tokens.append(run)
        tokens.extend(run[index:index + 2] for index in range(len(run) - 1))
    return tokens


@dataclass(frozen=True)
class _Passage:
    page: SitePage
    section: str
    text: str
    tokens: tuple[str, ...]


def _page_passages(page: SitePage) -> list[_Passage]:
    passages: list[_Passage] = []
    section = page.title
    buffer: list[str] = []

    def flush():
        body = "\n".join(buffer).strip()
        if body:
            passages.append(_Passage(page, section, body, tuple(tokenize(f"{section} {body}"))))
        buffer.clear()

    for line in page.text.splitlines():
        if line.startswith("# "):
            flush()
            section = line[2:].strip() or page.title
            continue
        while len(line) > PASSAGE_CHARS:
            buffer.append(line[:PASSAGE_CHARS])
            flush()
            line = line[PASSAGE_CHARS:]
        if sum(len(part) for part in buffer) + len(line) > PASSAGE_CHARS:
            flush()
        buffer.append(line)
    flush()
    return passages


@lru_cache(maxsize=64)
def _passages_for(pages: tuple[SitePage, ...]) -> tuple[_Passage, ...]:
    return tuple(passage for page in pages for passage in _page_passages(page))


def search_pages(sites: list[PublishedSiteContent], query: str) -> dict:
    """BM25-rank every passage of every page; fall back to the outline on no match."""
    import math
    from collections import Counter

    terms = list(dict.fromkeys(tokenize(query)))
    passages = [passage for site in sites for passage in _passages_for(site.pages)]
    results: list[dict] = []
    if terms and passages:
        doc_freq = Counter(term for passage in passages for term in set(passage.tokens))
        avg_len = sum(len(passage.tokens) for passage in passages) / len(passages) or 1.0
        count = len(passages)
        scored = []
        for passage in passages:
            freqs = Counter(passage.tokens)
            heading = set(tokenize(f"{passage.page.title} {passage.section}"))
            score = 0.0
            for term in terms:
                tf = freqs.get(term, 0)
                if not tf:
                    continue
                idf = math.log(1 + (count - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
                norm = tf * 2.2 / (tf + 1.2 * (0.25 + 0.75 * len(passage.tokens) / avg_len))
                score += idf * norm * (2.0 if term in heading else 1.0)
            if score > 0:
                scored.append((score, passage))
        scored.sort(key=lambda item: item[0], reverse=True)
        seen: set[tuple[str, str]] = set()
        for score, passage in scored:
            key = (passage.page.url, passage.section)
            if key in seen:
                continue
            seen.add(key)
            results.append({
                "title": passage.page.title,
                "section": passage.section,
                "url": passage.page.url,
                "snippet": passage.text[:PASSAGE_CHARS],
                "score": round(score, 3),
            })
            if len(results) >= SEARCH_RESULTS:
                break
    if results:
        return {"query": query, "results": results}
    all_pages = [page for site in sites for page in site.pages]
    return {
        "query": query,
        "results": [],
        "note": (
            "No page uses these words. Pick pages by meaning from this outline of every "
            "page and section, or search again with other words the site might use."
        ),
        "outline": outline_lines(all_pages, with_headings=True),
    }


def read_page(sites: list[PublishedSiteContent], url: str, offset: int = 0) -> dict:
    """Full text of one page in chunks; next_offset continues a long page."""
    wanted = str(url or "").strip()
    for site in sites:
        for page in site.pages:
            if wanted in {page.url, page.address, page.url.rstrip("/"), page.address.rstrip("/") or "/"} or (
                wanted.rstrip("/") and wanted.rstrip("/") in {page.url.rstrip("/"), page.address.rstrip("/")}
            ):
                start = max(int(offset or 0), 0)
                chunk = page.text[start:start + READ_CHUNK_CHARS]
                end = start + len(chunk)
                return {
                    "title": page.title,
                    "url": page.url,
                    "text": chunk,
                    "offset": start,
                    "total_chars": len(page.text),
                    "next_offset": end if end < len(page.text) else None,
                }
    return {
        "error": "page_not_found",
        "pages": [page.url for site in sites for page in site.pages],
    }


# ── runtime scope: which sites this public chat turn may read ───────────────

@dataclass(frozen=True)
class SiteChatScope:
    entity_id: str
    workspace_id: str
    channel_config_id: str
    conversation_id: str | None = None


_site_chat_scope: ContextVar[SiteChatScope | None] = ContextVar("site_chat_scope", default=None)


def current_site_chat_scope() -> SiteChatScope | None:
    return _site_chat_scope.get()


async def site_chat_scope_for(
    db: AsyncSession,
    *,
    entity_id: str,
    workspace_id: str | None,
    channel_config_id: str,
    conversation_id: str | None = None,
) -> SiteChatScope | None:
    """A scope only when a live site is connected to this chat channel."""
    if not workspace_id:
        return None
    connected = await db.scalar(
        select(Site.id).where(
            Site.entity_id == entity_id,
            Site.workspace_id == workspace_id,
            Site.status == "active",
            Site.revision >= 1,
            Site.connections["customer_service_channel_config_id"].astext == channel_config_id,
        ).limit(1)
    )
    if connected is None:
        return None
    return SiteChatScope(entity_id, workspace_id, channel_config_id, conversation_id)


async def guard_stream(scope: SiteChatScope | None, stream):
    """Expose website_content to the chat turn while its stream runs.

    Consumers such as the chat concurrency lease advance a stream with a new
    task per step, and each task copies the context it was created in, so the
    scope is set around every step rather than once.
    """
    if scope is None:
        async for part in stream:
            yield part
        return
    iterator = stream.__aiter__()
    while True:
        token = _site_chat_scope.set(scope)
        try:
            part = await iterator.__anext__()
        except StopAsyncIteration:
            return
        finally:
            try:
                _site_chat_scope.reset(token)
            except ValueError:  # resumed in another context; that context ends with the step
                pass
        yield part


async def run_with_scope(scope: SiteChatScope | None, awaitable):
    """Non-streaming variant of guard_stream for gateway-dispatched turns."""
    if scope is None:
        return await awaitable
    token = _site_chat_scope.set(scope)
    try:
        return await awaitable
    finally:
        _site_chat_scope.reset(token)
