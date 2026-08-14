from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from packages.core.ai.workflow_runner import WorkflowRunner
from packages.core.services.workflow_chat_projection import workflow_result_chat_projection
from packages.core.services.workflow_publication_receipts import (
    PUBLICATION_RECEIPT_SCHEMA,
    PublicationReceiptError,
    normalize_publication_receipt,
    publication_payload_hash,
    publication_receipts_from_step_results,
)
from packages.core.services.workflow_service import validate_workflow_steps


NOW = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)


def _verified_receipt(**overrides):
    return {
        "platform": "LinkedIn",
        "verification_status": "verified",
        "external_id": "urn:li:share:123",
        "published_url": "https://www.linkedin.com/feed/update/urn:li:share:123",
        "published_at": "2026-08-03T11:59:00Z",
        **overrides,
    }


def test_publication_receipt_computes_payload_hash_and_default_evidence():
    receipt = normalize_publication_receipt(
        _verified_receipt(),
        payload={"text": "hello", "tags": ["workflow"]},
        observed_at=NOW,
    )

    assert receipt["schema_version"] == PUBLICATION_RECEIPT_SCHEMA
    assert receipt["payload_hash"] == publication_payload_hash({
        "tags": ["workflow"],
        "text": "hello",
    })
    assert receipt["evidence"] == [{
        "kind": "published_url",
        "url": "https://www.linkedin.com/feed/update/urn:li:share:123",
    }]
    assert receipt["fallback_used"] == "none"
    assert receipt["verified_at"] == "2026-08-03T12:00:00Z"


def test_publication_receipt_does_not_promote_unverified_provider_success():
    with pytest.raises(PublicationReceiptError, match="not verified"):
        normalize_publication_receipt(
            _verified_receipt(verification_status="unverified"),
            payload={"text": "hello"},
            observed_at=NOW,
        )


def test_verified_publication_requires_external_identity():
    with pytest.raises(PublicationReceiptError, match="external_id or published_url"):
        normalize_publication_receipt(
            _verified_receipt(external_id=None, published_url=None),
            payload={"text": "hello"},
            observed_at=NOW,
        )


def test_receipt_node_returns_failed_result_for_incomplete_contract():
    result = WorkflowRunner()._execute_publication_receipt_step(
        {
            "id": "verify_publish",
            "type": "publication_receipt",
            "config": {
                "receipt": {
                    "platform": "Medium",
                    "verification_status": "verified",
                },
                "payload": {"title": "Draft"},
            },
        },
        {},
    )

    assert result["status"] == "failed"
    assert result["code"] == "publication_receipt_invalid"


def test_workflow_validation_accepts_publication_receipt_node():
    validation = validate_workflow_steps([
        {
            "id": "start",
            "type": "trigger",
            "name": "Start",
            "config": {},
            "next": ["verify_publish"],
        },
        {
            "id": "verify_publish",
            "type": "publication_receipt",
            "name": "Verify publication",
            "config": {
                "receipt": "{{publisher_receipt}}",
                "payload": "{{approved_payload}}",
            },
            "next": [],
        },
    ])

    assert validation["errors"] == []


def test_run_receipts_are_deduplicated_and_projected_to_chat():
    receipt = normalize_publication_receipt(
        _verified_receipt(),
        payload={"text": "hello"},
        observed_at=NOW,
    )
    step_results = {
        "receipt": {"status": "completed", "output": receipt},
        "duplicate": {"status": "completed", "output": receipt},
    }
    assert publication_receipts_from_step_results(step_results) == [receipt]

    run = SimpleNamespace(
        id="01RUN",
        workflow_id="01WORKFLOW",
        status="completed",
        definition_snapshot={"name": "Publish LinkedIn"},
        step_results=step_results,
    )
    copy, reference = workflow_result_chat_projection(run, receipt)

    assert copy.startswith("Publication verified: LinkedIn.")
    assert reference["kind"] == "publication_receipt"
    assert reference["publication_summary"] == {
        "count": 1,
        "verified": 1,
        "platforms": ["LinkedIn"],
    }
