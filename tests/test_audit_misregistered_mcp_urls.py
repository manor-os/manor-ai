from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
from pathlib import Path
from types import SimpleNamespace


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "audit_misregistered_mcp_urls.py"
SPEC = importlib.util.spec_from_file_location("audit_misregistered_mcp_urls", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
audit_misregistered_mcp_urls = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit_misregistered_mcp_urls)


def _document(**overrides):
    values = {
        "id": "doc-1",
        "entity_id": "ent-1",
        "name": "page-image.png",
        "source": "chrome",
        "fs_path": None,
        "file_size": None,
        "file_url": "https://media.licdn.com/media/page-image.png",
        "metadata_": {
            "origin": {"tool_name": "mcp__chrome__get_web_content"},
            "external": {"source_url": "https://media.licdn.com/media/page-image.png"},
        },
        "is_trashed": False,
        "trashed_at": None,
        "trashed_by": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_classify_candidate_accepts_url_only_observation_document() -> None:
    doc = _document()

    assert audit_misregistered_mcp_urls.classify_candidate(doc) == "observation_tool"


def test_classify_candidate_rejects_generated_artifact() -> None:
    doc = _document(
        source="replicate",
        metadata_={"origin": {"tool_name": "mcp__replicate__generate_image"}},
    )

    assert audit_misregistered_mcp_urls.classify_candidate(doc) is None


def test_classify_candidate_requires_opt_in_for_legacy_source_only_document() -> None:
    doc = _document(metadata_={})

    assert audit_misregistered_mcp_urls.classify_candidate(doc) is None
    assert (
        audit_misregistered_mcp_urls.classify_candidate(
            doc,
            include_legacy_source_only=True,
        )
        == "legacy_source_only"
    )


def test_mark_candidate_soft_deletes_and_records_cleanup_metadata() -> None:
    doc = _document()
    cleaned_at = datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc)

    audit_misregistered_mcp_urls.mark_candidate(
        doc,
        reason="observation_tool",
        cleaned_at=cleaned_at,
    )

    assert doc.is_trashed is True
    assert doc.trashed_at == cleaned_at
    assert doc.trashed_by == "mcp-url-audit"
    assert doc.metadata_["cleanup"] == {
        "reason": "mcp_observation_url_misregistered",
        "candidate_kind": "observation_tool",
        "cleaned_at": "2026-07-20T08:00:00+00:00",
        "original_tool_name": "mcp__chrome__get_web_content",
    }
