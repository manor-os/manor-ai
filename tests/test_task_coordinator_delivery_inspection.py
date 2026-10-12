"""The V3 coordinator can inspect all delivered evidence without a second reviewer."""
from __future__ import annotations

import json

from packages.core.services.task_acceptance_review import (
    INSPECTION_CHUNK_CHARS,
    acceptance_snapshot,
    inspect_task_delivery,
    required_delivery_inspections,
)


def test_manifest_and_content_pages_cover_every_delivery_byte() -> None:
    text = "A" * (2 * INSPECTION_CHUNK_CHARS + 1)
    payload = {
        "task": {"title": "Inspect delivered work"},
        "plan_id": "plan-1",
        "steps": [
            {"key": f"step-{index}", "status": "done", "result": {"text": f"Result {index}"}}
            for index in range(13)
        ],
        "artifacts": [{"document_id": "doc-1", "text": text}],
    }
    snapshot = acceptance_snapshot(payload)

    first_manifest = inspect_task_delivery(snapshot, kind="manifest")
    second_manifest = inspect_task_delivery(snapshot, kind="manifest", offset=12)
    assert first_manifest["evidence_digest"] == snapshot.evidence_digest
    assert first_manifest["step_count"] == 13
    assert first_manifest["artifact_count"] == 1
    assert len(first_manifest["catalog"]) == 12
    assert first_manifest["next_offset"] == 12
    assert len(second_manifest["catalog"]) == 2
    assert second_manifest["next_offset"] is None

    step = inspect_task_delivery(snapshot, kind="step", index=12)
    assert step["result_pointer"] == "/steps/12/result"
    assert step["citation_pointer"] == "/steps/12/result/text"
    assert json.loads(step["content"]) == {"text": "Result 12"}

    pages = []
    offset = 0
    while True:
        page = inspect_task_delivery(snapshot, kind="artifact", index=0, offset=offset)
        assert page["pointer"] == "/artifacts/0/text"
        assert page["evidence_digest"] == snapshot.evidence_digest
        assert len(page["content"]) <= INSPECTION_CHUNK_CHARS
        pages.append(page["content"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    assert "".join(pages) == text
    assert required_delivery_inspections(snapshot) == [
        "manifest:0", "manifest:12",
        *(f"step:{index}:0" for index in range(13)),
        "artifact:0:0", f"artifact:0:{INSPECTION_CHUNK_CHARS}",
        f"artifact:0:{2 * INSPECTION_CHUNK_CHARS}",
    ]
