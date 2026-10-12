from __future__ import annotations

import importlib.util
import json
from copy import deepcopy
from pathlib import Path

import httpx
import pytest

_PATH = Path(__file__).parents[1] / ".agents/skills/manor-workspace-blueprint/scripts/compare_roundtrip.py"
_SPEC = importlib.util.spec_from_file_location("blueprint_roundtrip", _PATH)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)

_VERIFIER_PATH = (
    Path(__file__).parents[1]
    / ".agents/skills/manor-workspace-blueprint/scripts/verify_roundtrip.py"
)
_VERIFIER_SPEC = importlib.util.spec_from_file_location("blueprint_roundtrip_verifier", _VERIFIER_PATH)
assert _VERIFIER_SPEC and _VERIFIER_SPEC.loader
_VERIFIER = importlib.util.module_from_spec(_VERIFIER_SPEC)
_VERIFIER_SPEC.loader.exec_module(_VERIFIER)


def test_roundtrip_ignores_generated_ids_and_install_metadata() -> None:
    source = {
        "recipe": {
            "workflows": [{"slug": "daily", "steps": [{"id": "start", "next": []}]}],
            "scheduled_jobs": [{"job_id": "daily", "execution_target": {"service_key": "x"}}],
        }
    }
    installed = {
        "recipe": {
            "workflows": [{"id": "01INSTALLED", "slug": "daily", "steps": [{"id": "start", "next": []}]}],
            "scheduled_jobs": [{"job_id": "daily-1ABC23XY", "execution_target": {"service_key": "x", "binding_id": "01BINDING"}}],
        },
        "settings": {"_blueprint": {"install_mode": "live"}},
    }
    assert _MODULE.compare(source, installed) == []


def test_roundtrip_reports_workflow_graph_change() -> None:
    source = {"recipe": {"workflows": [{"slug": "daily", "steps": [{"id": "a", "next": ["b"]}, {"id": "b", "next": []}]}]}}
    installed = {"recipe": {"workflows": [{"slug": "daily", "steps": [{"id": "a", "next": []}, {"id": "b", "next": []}]}]}}
    assert _MODULE.compare(source, installed)


def test_roundtrip_reports_portable_step_id_change() -> None:
    source = {"recipe": {"workflows": [{"slug": "daily", "steps": [{"id": "produce", "next": []}]}]}}
    installed = {"recipe": {"workflows": [{"slug": "daily", "steps": [{"id": "publish", "next": []}]}]}}
    assert _MODULE.compare(source, installed)


def test_roundtrip_preserves_hyphens_in_portable_job_id() -> None:
    source = {"recipe": {"scheduled_jobs": [{"job_id": "morning-draft"}]}}
    installed = {
        "recipe": {"scheduled_jobs": [{"job_id": "morning-draft-1ABC23XY"}]}
    }
    changed = {"recipe": {"scheduled_jobs": [{"job_id": "morning-post-1ABC23XY"}]}}

    assert _MODULE.compare(source, installed) == []
    assert _MODULE.compare(source, changed)


def test_roundtrip_excludes_one_shot_install_declarations() -> None:
    source = {
        "manifest": {"title": "Source", "kind": "studio"},
        "contract": {
            "variables": [{"key": "brand", "required": True}],
            "channels": [{"channel_type": "telegram"}],
        },
        "recipe": {"workflows": []},
        "policy": {
            "governance": {"max_risk_level": "medium"},
            "post_install_checks": [{"kind": "check_agent_callable"}],
            "expected_baseline": {"runnable_in_simulation": True},
        },
    }
    installed = {
        "manifest": {"title": "Remix", "kind": "studio"},
        "contract": {"variables": [], "channels": []},
        "recipe": {"workflows": []},
        "policy": {"governance": {"max_risk_level": "medium"}},
    }
    assert _MODULE.compare(source, installed) == []


def _external_skill_payload(slug: str) -> dict:
    return {"embedded": {"agents": [{"slug": "writer", "skill_bindings": [slug],
        "skill_binding_refs": [{"slug": slug, "marketplace_source": "manor",
                                "marketplace_id": "content-planner", "version": "1.0"}]}]}}


def test_roundtrip_ignores_generated_external_skill_alias_without_mutating_payload() -> None:
    source = _external_skill_payload("planner")
    installed = _external_skill_payload("planner_2")
    original = deepcopy(installed)
    assert _MODULE.compare(source, installed) == []
    assert installed == original


def test_roundtrip_preserves_external_skill_identity_version_and_binding_presence() -> None:
    source = _external_skill_payload("planner")
    for key in ("marketplace_source", "marketplace_id", "version"):
        installed = _external_skill_payload("planner_2")
        installed["embedded"]["agents"][0]["skill_binding_refs"][0][key] = "different"
        assert _MODULE.compare(source, installed)
    installed = _external_skill_payload("planner_2")
    installed["embedded"]["agents"][0]["skill_bindings"] = []
    assert _MODULE.compare(source, installed)


def test_roundtrip_does_not_normalize_legacy_embedded_or_ambiguous_skill_aliases() -> None:
    source = _external_skill_payload("planner")
    installed = _external_skill_payload("planner_2")
    for payload in (source, installed):
        payload["embedded"]["agents"][0]["skill_binding_refs"] = []
    assert _MODULE.compare(source, installed)
    source = _external_skill_payload("planner")
    installed = _external_skill_payload("planner_2")
    for payload in (source, installed):
        payload["embedded"]["skills"] = [{"slug": "planner"}]
    assert _MODULE.compare(source, installed)
    source = _external_skill_payload("planner")
    installed = _external_skill_payload("planner_2")
    for payload in (source, installed):
        refs = payload["embedded"]["agents"][0]["skill_binding_refs"]
        refs.append({**refs[0], "marketplace_id": "another-planner"})
    assert _MODULE.compare(source, installed)


def _payload() -> dict:
    return {
        "manifest": {"kind": "studio"},
        "embedded": {"skills": [], "agents": [], "knowledge_packs": []},
        "recipe": {"workflows": []},
        "policy": {"governance": {}},
    }


def _identity() -> dict:
    return {"id": "user", "entity_id": "entity"}


def test_http_roundtrip_uses_nonpersistent_export_and_deletes_only_installed_workspace(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []
    payload = _payload()
    export_options = {
        "title": "Verification", "knowledge_pack_mode": "inline_text",
        "knowledge_document_ids": ["sourceDocument"],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/api/v1/auth/me":
            return httpx.Response(200, json=_identity())
        if request.url.path == "/api/v1/workspaces/source/export-blueprint-payload":
            assert json.loads(request.content) == export_options
            return httpx.Response(200, json=payload)
        if request.url.path == "/api/v1/blueprints/install-payload":
            assert json.loads(request.content) == {
                "payload": payload, "mode": "simulate",
                "workspace_source": {"workspace_id": "source", "export_options": export_options},
            }
            return httpx.Response(201, json={"workspace_id": "installed", "todos": []})
        if request.url.path == "/api/v1/workspaces/installed/export-blueprint-payload":
            assert json.loads(request.content) == {
                key: value for key, value in export_options.items() if key != "knowledge_document_ids"
            }
            return httpx.Response(200, json=payload)
        if request.method == "DELETE" and request.url.path == "/api/v1/workspaces/installed":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _VERIFIER.verify(
            client, origin="https://manor.test", source_workspace_id="source", run_dir=tmp_path / "run",
            export_options=export_options,
            install_options={"payload": {}, "workspace_source": {"workspace_id": "other"}},
        )

    paths = [request.url.path for request in requests]
    assert "/api/v1/workspaces/source/export-blueprint" not in paths
    assert paths.count("/api/v1/workspaces/source/export-blueprint-payload") == 1
    assert "/api/v1/workspaces/installed" in paths
    state = json.loads((tmp_path / "run/run-state.json").read_text())
    assert state["cleaned_workspace_ids"] == ["installed"]
    assert state["cleanup_pending_workspace_ids"] == []
    assert (tmp_path / "run/source-payload.json").exists()
    assert (tmp_path / "run/installed-payload.json").exists()


@pytest.mark.parametrize("scenario", ["supplied", "defaults", "configuration_drift"])
def test_http_roundtrip_compares_personalized_configuration_and_still_detects_drift(
    tmp_path: Path, scenario: str,
) -> None:
    source = _payload()
    source["contract"] = {"variables": [
        {"key": "brand_name", "default": "Example"},
        {"key": "limit", "default": 3},
        {"key": "enabled", "default": True},
        {"key": "topics", "default": ["intro"]},
        {"key": "style", "default": {"tone": "calm"}},
    ]}
    source["recipe"]["operating_model"] = {
        "context": "Running for {{brand_name}}; {{topics}}; {{undeclared}}.",
        "settings": {
            "limit": "{{limit}}", "enabled": "{{enabled}}",
            "topics": "{{topics}}", "style": "{{style}}",
        },
    }
    original = deepcopy(source)
    supplied = {"brand_name": "Acme", "limit": 5} if scenario != "defaults" else {}
    expected = deepcopy(source)
    expected["recipe"]["operating_model"] = {
        "context": (
            'Running for Example; ["intro"]; {{undeclared}}.' if scenario == "defaults"
            else 'Running for Acme; ["intro"]; {{undeclared}}.'
        ),
        "settings": {
            "limit": 3 if scenario == "defaults" else 5, "enabled": True,
            "topics": ["intro"], "style": {"tone": "calm"},
        },
    }
    installed = deepcopy(expected)
    if scenario == "configuration_drift":
        installed["recipe"]["operating_model"]["settings"]["limit"] = 99
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/me":
            return httpx.Response(200, json=_identity())
        if request.url.path == "/api/v1/workspaces/source/export-blueprint-payload":
            return httpx.Response(200, json=source)
        if request.url.path == "/api/v1/blueprints/install-payload":
            body = json.loads(request.content)
            assert body["payload"] == original
            assert body["variable_values"] == supplied
            return httpx.Response(201, json={"workspace_id": "installed", "todos": []})
        if request.url.path == "/api/v1/workspaces/installed/export-blueprint-payload":
            return httpx.Response(200, json=installed)
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        def verify() -> None:
            _VERIFIER.verify(
                client, origin="https://manor.test", source_workspace_id="source",
                run_dir=tmp_path / "run", install_options={"variable_values": supplied},
            )

        if scenario == "configuration_drift":
            with pytest.raises(_VERIFIER.VerificationError, match="runtime projections differ"):
                verify()
        else:
            verify()

    assert deleted == ["/api/v1/workspaces/installed"]
    assert json.loads((tmp_path / "run/source-payload.json").read_text()) == original
    assert json.loads((tmp_path / "run/expected-payload.json").read_text()) == expected
    assert source == original
    state = json.loads((tmp_path / "run/run-state.json").read_text())
    assert state["cleaned_workspace_ids"] == ["installed"]
    assert state["cleanup_pending_workspace_ids"] == []


def test_http_roundtrip_rejects_invalid_variables_before_install(tmp_path: Path) -> None:
    source = _payload()
    source["contract"] = {"variables": [{"key": "limit", "default": 3}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/me":
            return httpx.Response(200, json=_identity())
        if request.url.path == "/api/v1/workspaces/source/export-blueprint-payload":
            return httpx.Response(200, json=source)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(_VERIFIER.VerificationError, match="install variables are invalid"):
            _VERIFIER.verify(
                client, origin="https://manor.test", source_workspace_id="source",
                run_dir=tmp_path / "run", install_options={"variable_values": {"limit": "invalid"}},
            )
        _VERIFIER.cleanup_only(client, origin="https://manor.test", run_dir=tmp_path / "run")
    state = json.loads((tmp_path / "run/run-state.json").read_text())
    assert not state.get("install_in_flight")
    assert not state.get("install_response_uncertain")


@pytest.mark.parametrize(
    ("failure", "expected_error"),
    [
        ("source_changed", "payload install failed"),
        ("comparison", "runtime projections differ"),
        ("reexport", "installed payload export failed"),
        ("blocking_todos", "blocking todos"),
        ("malformed_todos", "malformed todos"),
    ],
)
def test_http_roundtrip_failure_cleans_only_confirmed_installs(
    tmp_path: Path,
    failure: str,
    expected_error: str,
) -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/me":
            return httpx.Response(200, json=_identity())
        if request.url.path == "/api/v1/workspaces/source/export-blueprint-payload":
            return httpx.Response(200, json=_payload())
        if request.url.path == "/api/v1/blueprints/install-payload":
            if failure == "source_changed":
                return httpx.Response(409, json={"detail": {"code": "blueprint_workspace_source_mismatch"}})
            if failure == "blocking_todos":
                return httpx.Response(
                    201,
                    json={"workspace_id": "installed", "todos": [{"blocking": True}]},
                )
            if failure == "malformed_todos":
                return httpx.Response(201, json={"workspace_id": "installed", "todos": [{}]})
            return httpx.Response(201, json={"workspace_id": "installed", "todos": []})
        if request.url.path == "/api/v1/workspaces/installed/export-blueprint-payload":
            if failure == "reexport":
                return httpx.Response(500, json={"detail": "export failed"})
            changed = _payload()
            changed["recipe"] = {"workflows": [{"slug": "changed", "steps": []}]}
            return httpx.Response(200, json=changed)
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(_VERIFIER.VerificationError, match=expected_error):
            _VERIFIER.verify(client, origin="https://manor.test", source_workspace_id="source", run_dir=tmp_path / "run")

    assert deleted == ([] if failure == "source_changed" else ["/api/v1/workspaces/installed"])
    state = json.loads((tmp_path / "run/run-state.json").read_text())
    assert state["install_response_uncertain"] is False
    assert state["cleanup_pending_workspace_ids"] == []


@pytest.mark.parametrize(
    ("failure", "expected_exception", "expected_error"),
    [
        ("5xx", _VERIFIER.VerificationError, "payload install failed"),
        ("network", _VERIFIER.VerificationError, "response was lost"),
        ("malformed_id", _VERIFIER.VerificationError, "safe new workspace_id"),
        ("interrupt", KeyboardInterrupt, None),
    ],
)
def test_uncertain_install_refuses_cleanup_retry(
    tmp_path: Path,
    failure: str,
    expected_exception: type[BaseException],
    expected_error: str | None,
) -> None:
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/me":
            return httpx.Response(200, json=_identity())
        if request.url.path == "/api/v1/workspaces/source/export-blueprint-payload":
            return httpx.Response(200, json=_payload())
        if request.url.path == "/api/v1/blueprints/install-payload":
            if failure == "network":
                raise httpx.ConnectError("connection lost", request=request)
            if failure == "malformed_id":
                return httpx.Response(201, json={"workspace_id": {"id": "unknown"}, "todos": []})
            if failure == "interrupt":
                raise KeyboardInterrupt
            return httpx.Response(500, json={"detail": "upstream error"})
        if request.method == "DELETE":
            deleted.append(request.url.path)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    run_dir = tmp_path / "run"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(expected_exception, match=expected_error):
            _VERIFIER.verify(
                client,
                origin="https://manor.test",
                source_workspace_id="source",
                run_dir=run_dir,
            )
        with pytest.raises(_VERIFIER.VerificationError, match="manually reconcile"):
            _VERIFIER.cleanup_only(client, origin="https://manor.test", run_dir=run_dir)

    state = json.loads((run_dir / "run-state.json").read_text())
    assert state["install_response_uncertain"] is True
    assert state["install_in_flight"] is True
    assert deleted == []


def test_failed_cleanup_remains_durable_and_cleanup_only_retries_exact_id(tmp_path: Path) -> None:
    phase = "verify"
    account = _identity()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/auth/me":
            return httpx.Response(200, json=account)
        if request.url.path == "/api/v1/workspaces/source/export-blueprint-payload":
            return httpx.Response(200, json=_payload())
        if request.url.path == "/api/v1/blueprints/install-payload":
            return httpx.Response(201, json={"workspace_id": "installed", "todos": []})
        if request.url.path == "/api/v1/workspaces/installed/export-blueprint-payload":
            return httpx.Response(200, json=_payload())
        if request.method == "DELETE" and request.url.path == "/api/v1/workspaces/installed":
            return httpx.Response(500 if phase == "verify" else 204)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    run_dir = tmp_path / "run"
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(_VERIFIER.VerificationError, match="cleanup failed"):
            _VERIFIER.verify(client, origin="https://manor.test", source_workspace_id="source", run_dir=run_dir)
        phase = "retry"
        account = {"id": "other-user", "entity_id": "other-entity"}
        with pytest.raises(_VERIFIER.VerificationError, match="account does not match"):
            _VERIFIER.cleanup_only(client, origin="https://manor.test", run_dir=run_dir)
        account = _identity()
        _VERIFIER.cleanup_only(client, origin="https://manor.test", run_dir=run_dir)

    state = json.loads((run_dir / "run-state.json").read_text())
    assert state["source_workspace_id"] == "source"
    assert state["cleanup_pending_workspace_ids"] == []
    assert state["cleaned_workspace_ids"] == ["installed"]
