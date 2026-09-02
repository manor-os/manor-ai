import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

import packages.core.ai.tools.sandbox_tools as sandbox_tools


def _passing_report(content: bytes, *, path: str) -> bytes:
    return json.dumps(
        {
            "status": "pass",
            "quality_score": 96,
            "minimum_score": 90,
            "pptx": path,
            "mode": "editable",
            "slide_count": 4,
            "metrics": {
                "pptx_sha256": hashlib.sha256(content).hexdigest(),
                "pptx_size_bytes": len(content),
                "rendered_slide_count": 4,
            },
            "errors": [],
            "warnings": ["visual inspection reminder"],
        }
    ).encode("utf-8")


def _passing_docx_report(content: bytes, *, path: str) -> bytes:
    return json.dumps(
        {
            "status": "pass",
            "quality_score": 95,
            "minimum_score": 90,
            "docx": path,
            "page_count": 3,
            "metrics": {
                "docx_sha256": hashlib.sha256(content).hexdigest(),
                "docx_size_bytes": len(content),
                "rendered_page_count": 3,
            },
            "errors": [],
        }
    ).encode("utf-8")


def test_project_export_context_only_matches_builtin_pptx_exports() -> None:
    assert sandbox_tools._pptx_project_export_context(
        "/skill/projects/northstar/exports/northstar.pptx",
        "northstar.pptx",
    ) == (
        "/skill/projects/northstar",
        "/skill/projects/northstar/qa/final-render",
        "/skill/projects/northstar/qa/pptx-quality.json",
    )
    assert sandbox_tools._pptx_project_export_context(
        "/skill/projects/northstar/drafts/northstar.pptx",
        "northstar.pptx",
    ) is None
    assert sandbox_tools._pptx_project_export_context(
        "/tmp/northstar.pptx",
        "northstar.pptx",
    ) is None


def test_quality_evidence_is_bound_to_exact_pptx_bytes() -> None:
    path = "/skill/projects/northstar/exports/northstar.pptx"
    content = b"final-pptx-bytes"
    error, payload = sandbox_tools._pptx_quality_evidence_error(
        file_path=path,
        content_bytes=content,
        gate_exit_code=0,
        report_bytes=_passing_report(content, path=path),
    )
    assert error is None
    assert payload and payload["quality_score"] == 96

    stale_error, _payload = sandbox_tools._pptx_quality_evidence_error(
        file_path=path,
        content_bytes=content + b"-changed",
        gate_exit_code=0,
        report_bytes=_passing_report(content, path=path),
    )
    assert stale_error == "the report is stale or belongs to a different PPTX"


def test_docx_export_context_and_evidence_are_bound_to_exact_bytes() -> None:
    path = "/skill/projects/northstar/exports/northstar.docx"
    content = b"final-docx-bytes"
    assert sandbox_tools._docx_project_export_context(path, "northstar.docx") == (
        "/skill/projects/northstar",
        "/skill/projects/northstar/qa/final-render",
        "/skill/projects/northstar/qa/docx-quality.json",
    )
    assert sandbox_tools._docx_project_export_context(
        "/skill/projects/northstar/drafts/northstar.docx",
        "northstar.docx",
    ) is None

    error, payload = sandbox_tools._docx_quality_evidence_error(
        file_path=path,
        content_bytes=content,
        gate_exit_code=0,
        report_bytes=_passing_docx_report(content, path=path),
    )
    assert error is None
    assert payload and payload["quality_score"] == 95

    stale_error, _payload = sandbox_tools._docx_quality_evidence_error(
        file_path=path,
        content_bytes=content + b"-changed",
        gate_exit_code=0,
        report_bytes=_passing_docx_report(content, path=path),
    )
    assert stale_error == "the report is stale or belongs to a different DOCX"


@pytest.mark.asyncio
async def test_save_result_blocks_failed_pptx_before_filesystem_mutation(monkeypatch) -> None:
    path = "/skill/projects/northstar/exports/northstar.pptx"
    report_path = "/skill/projects/northstar/qa/pptx-quality.json"
    content = b"invalid-pptx"
    failed_report = json.dumps(
        {
            "status": "fail",
            "quality_score": 76,
            "minimum_score": 90,
            "pptx": path,
            "mode": "editable",
            "slide_count": 10,
            "metrics": {
                "pptx_sha256": hashlib.sha256(content).hexdigest(),
                "pptx_size_bytes": len(content),
                "rendered_slide_count": 10,
                "layout_defects": [
                    {
                        "slide": 2,
                        "kind": "text-safe-boundary",
                        "shapes": [{"shape_id": 7, "text": "Clipped heading"}],
                    }
                ],
            },
            "errors": ["Text crosses the slide boundary on slide(s): 2, 3"],
            "warnings": [],
            "repair_actions": [
                {
                    "code": "repair-bounds",
                    "action": "Move the affected region back inside the slide canvas.",
                }
            ],
        }
    ).encode("utf-8")
    calls: list[tuple[str, str]] = []

    class FakeSandboxClient:
        async def exec(self, *, sandbox_id, command, timeout):
            calls.append(("exec", command))
            assert sandbox_id == "sb_1"
            assert timeout == 240
            return SimpleNamespace(exit_code=1, stdout="", stderr="quality failed")

        async def read_file_base64(self, *, sandbox_id, path):
            calls.append(("read", path))
            payload = failed_report if path == report_path else content
            return SimpleNamespace(content_base64=base64.b64encode(payload).decode("ascii"))

        async def close(self):
            calls.append(("close", ""))

    async def forbidden_mutation(**_kwargs):
        raise AssertionError("failed PPTX must not reach filesystem mutation")

    async def get_sandbox_client(sandbox_id: str):
        assert sandbox_id == "sb_1"
        return FakeSandboxClient()

    monkeypatch.setattr(
        sandbox_tools,
        "_get_client_for_sandbox",
        get_sandbox_client,
    )
    monkeypatch.setattr(sandbox_tools, "runtime_guard_file_mutation", forbidden_mutation)

    result = await sandbox_tools._sandbox_save_result(
        entity_id="ent_1",
        sandbox_id="sb_1",
        file_path=path,
        filename="northstar.pptx",
        artifact_role="final",
    )

    assert "PPTX_FINAL_QUALITY_GATE_BLOCKED" in result
    assert "Text crosses the slide boundary" in result
    assert "Required repair" in result
    assert "Move the affected region back inside" in result
    assert "slide 2: text-safe-boundary" in result
    assert "shape 7 'Clipped heading'" in result
    assert "--mode auto" in calls[0][1]
    assert calls[-1][0] == "close"


@pytest.mark.asyncio
async def test_save_result_rejects_final_pptx_outside_canonical_project_export(monkeypatch) -> None:
    monkeypatch.setattr(
        sandbox_tools,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("sandbox must not be opened")),
    )

    result = await sandbox_tools._sandbox_save_result(
        entity_id="ent_1",
        sandbox_id="sb_1",
        file_path="/tmp/deck.pptx",
        filename="deck.pptx",
        artifact_role="final",
    )

    assert "PPTX_FINAL_QUALITY_GATE_BLOCKED" in result
    assert "/skill/projects/<project>/exports/" in result


@pytest.mark.asyncio
async def test_save_result_rejects_knowledge_pptx_outside_canonical_project_export(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        sandbox_tools,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("sandbox must not be opened")),
    )

    result = await sandbox_tools._sandbox_save_result(
        entity_id="ent_1",
        sandbox_id="sb_1",
        file_path="/tmp/deck.pptx",
        filename="deck.pptx",
        save_to_knowledge=True,
    )

    assert "PPTX_FINAL_QUALITY_GATE_BLOCKED" in result
    assert "/skill/projects/<project>/exports/" in result


@pytest.mark.asyncio
async def test_save_result_rejects_final_docx_outside_canonical_project_export(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        sandbox_tools,
        "_get_client",
        lambda: (_ for _ in ()).throw(AssertionError("sandbox must not be opened")),
    )

    result = await sandbox_tools._sandbox_save_result(
        entity_id="ent_1",
        sandbox_id="sb_1",
        file_path="/tmp/document.docx",
        filename="document.docx",
        artifact_role="final",
    )

    assert "DOCX_FINAL_QUALITY_GATE_BLOCKED" in result
    assert "/skill/projects/<project>/exports/" in result
