"""Opt-in file deliverables validate their filing contract and tool receipts keep canonical identity."""

import pytest
from pydantic import ValidationError

from packages.core.ai.runtime.tool_execution import _tool_end_event_data
from packages.core.strategist.proposal import Deliverable


def _deliverable(**overrides):
    return Deliverable(**{
        "name": "report", "kind": "file", "shape": "DocumentResult",
        "acceptance": "Saved and verified", "usage": "Next specialist",
        "document_id_path": "/report/document_id", "workspace_folder": "projects/first",
        "require_readback": True, **overrides,
    })


@pytest.mark.parametrize("folder", ["/projects/a", "../a", "projects/../a", "projects//a", "a\\b"])
def test_file_folder_must_be_workspace_relative(folder):
    with pytest.raises(ValidationError):
        _deliverable(workspace_folder=folder)


def test_folder_contract_requires_document_selector():
    with pytest.raises(ValidationError):
        _deliverable(document_id_path=None)


def test_tool_receipts_capture_canonical_identity_and_hash():
    event = _tool_end_event_data("generate_file", {
        "document": {"document_id": "doc", "fs_path": "canonical/report.json", "source_sha256": "a" * 64},
    })
    assert event["document_id"] == "doc"
    assert event["fs_path"] == "canonical/report.json"
    assert event["source_sha256"] == "a" * 64
    read = _tool_end_event_data("read_file", {
        "document_id": "doc", "resolved_path": "canonical/report.json", "source_sha256": "a" * 64,
    })
    assert read["resolved_path"] == "canonical/report.json"
