"""The one file-reference contract: build, parse and reply-link rewriting."""
from __future__ import annotations

import pytest

from packages.core.services.file_refs import (
    ParsedFileRef,
    file_ref,
    is_chat_upload_storage_path,
    is_internal_file_target,
    parse_file_reference,
    rewrite_reply_file_links,
    signed_file_url_capability,
)


def test_file_ref_for_unsynced_file_encodes_the_path_once():
    ref = file_ref(entity_id="ent1", fs_path="images/my deck (1).pptx", mime="application/x")
    assert ref == {
        "name": "my deck (1).pptx",
        "fs_path": "images/my deck (1).pptx",
        "open_url": "/api/v1/fs/ent1/images/my%20deck%20%281%29.pptx",
        "markdown_link": "[my deck (1).pptx](/api/v1/fs/ent1/images/my%20deck%20%281%29.pptx)",
        "mime": "application/x",
    }


def test_file_ref_for_document_opens_in_the_viewer():
    ref = file_ref(entity_id="ent1", fs_path="Reports/q3.docx", document_id="01DOC", name="Q3 report")
    assert ref["open_url"] == "/viewer/01DOC"
    assert ref["markdown_link"] == "[Q3 report](/viewer/01DOC)"
    assert ref["document_id"] == "01DOC"


@pytest.mark.parametrize("address, expected", [
    ("/api/v1/fs/ent1/images/my%20deck.pptx", ParsedFileRef("ent1", "images/my deck.pptx")),
    ("/api/v1/fs/ent1/images/my deck.pptx", ParsedFileRef("ent1", "images/my deck.pptx")),
    ("https://app.example/api/v1/fs/ent1/a/b.png", ParsedFileRef("ent1", "a/b.png")),
    ("/viewer/01DOC", ParsedFileRef("ctx", "", "01DOC")),
    ("/documents/01DOC", ParsedFileRef("ctx", "", "01DOC")),
    ("/workspace/Reports/q3.docx", ParsedFileRef("ctx", "Reports/q3.docx")),
    ("</api/v1/fs/ent1/a b.png>", ParsedFileRef("ent1", "a b.png")),
])
def test_parse_accepts_every_address_the_platform_hands_out(address, expected):
    assert parse_file_reference(address, entity_id="ctx") == expected


@pytest.mark.parametrize("address", [
    "sandbox:/skill/projects/x/deck.pptx",
    "file:///tmp/a.png",
    "/skill/projects/x/deck.pptx",
    "/tmp/out.png",
    "/mnt/data/out.png",
    "/api/v1/fs/public/token123",
    "/api/v1/fs/ent1/../etc/passwd",
    "https://example.com/report.pdf",
    "Reports/q3.docx",  # relative paths only for tool inputs
])
def test_parse_rejects_what_is_not_an_openable_address(address):
    assert parse_file_reference(address, entity_id="ctx") is None


def test_relative_paths_are_accepted_for_tool_inputs():
    assert parse_file_reference("Knowledge/Reports/q3.docx", entity_id="e", allow_relative=True) == \
        ParsedFileRef("e", "Reports/q3.docx")
    assert parse_file_reference("Reports/q3.docx", entity_id="e", allow_relative=True) == \
        ParsedFileRef("e", "Reports/q3.docx")


def test_internal_targets_and_upload_paths():
    assert is_internal_file_target("sandbox:/skill/a.pptx")
    assert is_internal_file_target("$SANDBOX_OUTPUT_DIR/a.png")
    assert not is_internal_file_target("/api/v1/fs/e/a.png")
    assert is_chat_upload_storage_path("uploads/chat/0123456789abcdef.png")
    assert is_chat_upload_storage_path("uploads/chat/0123456789abcdef/fedcba9876543210.pdf")
    assert not is_chat_upload_storage_path("uploads/other/0123456789abcdef.png")


@pytest.mark.parametrize("value,expected", [
    ("https://app.example/api/v1/fs/public/claims.signature/poster.jpg", "claims.signature"),
    ("/api/v1/fs/public/opaque-token?download=1#preview", "opaque-token"),
    ("/api/v1/fs/public/", None),
    ("/api/v1/fs/public//poster.jpg", None),
    ("https://example.com/?next=/api/v1/fs/public/token", None),
    ("https://[invalid/api/v1/fs/public/token", None),
    (None, None),
])
def test_signed_file_capability_extraction_is_only_for_redaction(value, expected):
    assert signed_file_url_capability(value) == expected
    if expected:
        assert parse_file_reference(value) is None


def _resolver(known: dict[ParsedFileRef, dict]):
    async def resolve(parsed):
        return known.get(parsed)
    return resolve


@pytest.mark.asyncio
async def test_reply_links_resolve_to_canonical_addresses_or_lose_the_link():
    deck = file_ref(entity_id="ent1", fs_path="Decks/AI deck.pptx", document_id="01DECK")
    resolve = _resolver({
        ParsedFileRef("", "", "01DECK"): deck,
        ParsedFileRef("ent1", "Decks/AI deck.pptx"): deck,
    })
    text = (
        "Done: [AI deck](/api/v1/fs/ent1/Decks/AI deck.pptx).\n"
        "Old copy: [old](sandbox:/skill/projects/p/old.pptx)\n"
        "Bare: sandbox:/skill/projects/p/raw.pptx and /viewer/01DECK.\n"
        "Missing: [ghost](/viewer/01GHOST)\n"
        "Web: [site](https://example.com/a.pdf)\n"
        "Code: `sandbox:/skill/x.pptx`"
    )
    result = await rewrite_reply_file_links(text, resolve)
    assert result.text == (
        "Done: [AI deck](/viewer/01DECK).\n"
        "Old copy: old\n"
        "Bare: raw.pptx and [AI deck.pptx](/viewer/01DECK).\n"
        "Missing: ghost\n"
        "Web: [site](https://example.com/a.pdf)\n"
        "Code: `sandbox:/skill/x.pptx`"
    )
    assert result.unresolved == (
        "sandbox:/skill/projects/p/old.pptx",
        "sandbox:/skill/projects/p/raw.pptx",
        "/viewer/01GHOST",
    )


@pytest.mark.asyncio
async def test_reply_without_file_links_is_unchanged():
    text = "No files here, just [docs](https://docs.example.com) and `code`."
    result = await rewrite_reply_file_links(text, _resolver({}))
    assert result.text == text
    assert result.unresolved == ()


def _fixture_cases():
    import json
    from pathlib import Path

    return json.loads((Path(__file__).parent / "fixtures" / "file_reference_cases.json").read_text())["cases"]


@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda case: str(case.get("input"))[:60])
def test_backend_parser_agrees_with_the_shared_web_fixture(case):
    parsed = parse_file_reference(case["input"])
    expected = case["expected"]
    if expected["kind"] is None:
        assert parsed is None
    elif expected["kind"] == "fs":
        assert parsed == ParsedFileRef(expected.get("entity", ""), expected["fs_path"])
    else:
        assert parsed is not None and parsed.document_id == expected["document_id"]
