import base64
import hashlib
import json
from types import SimpleNamespace

import pytest

import packages.core.ai.tools.sandbox_tools as sandbox_tools


@pytest.fixture(autouse=True)
def _bound_sandbox(monkeypatch):
    """The delivery gate is under test, not the binding check (covered in
    tests/test_sandbox_binding_scope.py): every sandbox here is the caller's."""

    async def allow_access(**_kwargs):
        return None

    monkeypatch.setattr(sandbox_tools, "_sandbox_instance_access_error", allow_access)


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
        async def exec(self, *, sandbox_id, command, timeout, execution_id=None, **_queue_and_view):
            calls.append(("exec", command))
            assert sandbox_id == "sb_1"
            assert timeout == 240
            return SimpleNamespace(exit_code=1, stdout="", stderr="quality failed")

        async def read_file_base64(self, *, sandbox_id, path, **_view):
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
@pytest.mark.parametrize(
    ("kind", "skill_view"),
    [("docx", "docx"), ("docx", None), ("pptx", "pptx")],
)
async def test_save_result_accepts_its_own_gate_report_written_through_the_runner_view(
    monkeypatch, kind: str, skill_view: str | None,
) -> None:
    """The runner maps /skill onto /skills/<slot> in every command
    (``rewrite_skill_paths``), so the gate records the real path in its
    report. That is still the file the server gated, not "a different path"."""
    from tests import sandbox_runner_fakes  # noqa: F401  (puts sandbox-service on sys.path)
    from sandbox.docker_backend import rewrite_skill_paths, skill_slot

    slot = skill_slot(kind)
    path = f"/skill/projects/northstar/exports/northstar.{kind}"
    report_path = f"/skill/projects/northstar/qa/{kind}-quality.json"
    content = f"final-{kind}-bytes".encode()
    files: dict[str, bytes] = {rewrite_skill_paths(path, slot): content}

    class RunnerViewClient:
        async def exec(self, *, sandbox_id, command, timeout, execution_id=None, **_queue_and_view):
            argv = rewrite_skill_paths(command, slot).split()
            gated = argv[2]
            assert gated.startswith(f"/skills/{slot}/")
            # Like the real gates, the report names the path the gate was given.
            report = _passing_report if kind == "pptx" else _passing_docx_report
            files[argv[argv.index("--report") + 1]] = report(files[gated], path=gated)
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

        async def read_file_base64(self, *, sandbox_id, path, **_view):
            payload = files[rewrite_skill_paths(path, slot)]
            return SimpleNamespace(content_base64=base64.b64encode(payload).decode("ascii"))

        async def close(self):
            return None

    async def get_sandbox_client(_sandbox_id: str):
        return RunnerViewClient()

    async def file_view(_scope, _agent_id, _sandbox_id):
        return skill_view, "/skill"

    async def stop_at_file_mutation(**kwargs):
        assert kwargs["content_preview"]["source"] == path
        return "gate-passed"

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", get_sandbox_client)
    monkeypatch.setattr(sandbox_tools, "_agent_file_view", file_view)
    monkeypatch.setattr(sandbox_tools, "runtime_entity_file_root", lambda _entity_id: "/tmp/ent_1")
    monkeypatch.setattr(sandbox_tools, "runtime_guard_file_mutation", stop_at_file_mutation)

    result = await sandbox_tools._sandbox_save_result(
        entity_id="ent_1",
        sandbox_id="sb_1",
        file_path=path,
        filename=f"northstar.{kind}",
        artifact_role="final",
    )

    assert result == "gate-passed"
    assert report_path.replace("/skill/", f"/skills/{slot}/") in files


@pytest.mark.asyncio
async def test_save_result_reports_its_own_gate_disagreeing_as_a_retryable_platform_fault(
    monkeypatch,
) -> None:
    """The server chose the gated file and report path, so a report naming
    another file is Manor's fault: the ledger records a platform fault and
    proves nothing was saved, which lets step resilience re-run the step."""
    from packages.core.ai.runtime.tool_evidence import (
        RuntimeToolEvidence,
        runtime_unresolved_platform_error,
    )

    path = "/skill/projects/northstar/exports/northstar.docx"
    report_path = "/skill/projects/northstar/qa/docx-quality.json"
    content = b"final-docx-bytes"
    report = _passing_docx_report(content, path="/skill/projects/other/exports/northstar.docx")

    class Client:
        async def exec(self, **_kwargs):
            return SimpleNamespace(exit_code=0, stdout="", stderr="")

        async def read_file_base64(self, *, sandbox_id, path, **_view):
            payload = report if path == report_path else content
            return SimpleNamespace(content_base64=base64.b64encode(payload).decode("ascii"))

        async def close(self):
            return None

    async def get_sandbox_client(_sandbox_id: str):
        return Client()

    async def forbidden_mutation(**_kwargs):
        raise AssertionError("a blocked DOCX must not reach filesystem mutation")

    monkeypatch.setattr(sandbox_tools, "_get_client_for_sandbox", get_sandbox_client)
    monkeypatch.setattr(sandbox_tools, "runtime_guard_file_mutation", forbidden_mutation)

    result = await sandbox_tools._sandbox_save_result(
        entity_id="ent_1",
        sandbox_id="sb_1",
        file_path=path,
        filename="northstar.docx",
        artifact_role="final",
    )

    assert "platform fault" in result and "Fix the source" not in result
    evidence = RuntimeToolEvidence()
    evidence.observe_tool_result("sandbox", "call-1", result, {"action": "save_result"})
    state = evidence.checkpoint_state()
    assert runtime_unresolved_platform_error(state)["code"] == "sandbox_quality_gate_blocked"
    assert state["effect_state"] == "none"


def test_gate_report_for_another_file_is_still_rejected() -> None:
    path = "/skill/projects/northstar/exports/northstar.docx"
    content = b"final-docx-bytes"
    for elsewhere in (
        "/skills/docx/projects/other/exports/northstar.docx",
        "/skills/pptx/projects/northstar/exports/northstar.docx",
    ):
        error, _payload = sandbox_tools._docx_quality_evidence_error(
            file_path=path,
            content_bytes=content,
            gate_exit_code=0,
            report_bytes=_passing_docx_report(content, path=elsewhere),
            skill_view="docx",
        )
        assert error == "the report points to a different DOCX path"


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
