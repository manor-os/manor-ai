#!/usr/bin/env python3
"""Verify a Workspace payload round-trip without creating a Marketplace draft.

Set ``MANOR_API_ORIGIN`` and ``MANOR_API_TOKEN``, then run:

    verify_roundtrip.py SOURCE_WORKSPACE_ID --run-dir /tmp/manor-roundtrip/run-1

The run directory is durable evidence and may be reused only with
``--cleanup-only``. The verifier standard-soft-deletes *only* IDs recorded from
a successful ``install-payload`` response; normal Workspace recovery retention
and later purge still apply. A connection failure while installing is recorded
as uncertain: it is deliberately not retried or cleaned up by guessing an ID.
``--export-options`` applies source export toggles. Selected source knowledge
document IDs are intentionally omitted for the installed re-export because the
installed copies have new IDs; an explicit empty selection remains empty.
Installation supplies the source and export options for server-side verification.
Cloud raw-import subscription requirements do not apply to a verified source;
ordinary Workspace capacity and setup gates still apply. A changed source fails
closed before installation instead of silently verifying a different snapshot.
Variable personalization uses this checkout's canonical installer resolver;
run with the repository's Python environment, including when verifying a remote API.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from urllib.parse import urlsplit
from pathlib import Path
from typing import Any

import httpx

_SCRIPT_DIR = Path(__file__).resolve().parent
_COMPARE_SPEC = importlib.util.spec_from_file_location(
    "manor_blueprint_roundtrip_compare", _SCRIPT_DIR / "compare_roundtrip.py"
)
assert _COMPARE_SPEC and _COMPARE_SPEC.loader
_COMPARE_MODULE = importlib.util.module_from_spec(_COMPARE_SPEC)
_COMPARE_SPEC.loader.exec_module(_COMPARE_MODULE)
compare = _COMPARE_MODULE.compare


_STATE_FILE = "run-state.json"


class VerificationError(RuntimeError):
    """The API result cannot establish round-trip parity safely."""


def _expected_payload(source: dict[str, Any], variable_values: dict[str, Any] | None) -> dict[str, Any]:
    if not (source.get("contract") or {}).get("variables") and not variable_values:
        return source
    # Direct script invocation puts scripts/, not the repository, on sys.path.
    # Load the shared resolver lazily so --help and cleanup need no runtime imports.
    repository_root = str(_SCRIPT_DIR.parents[3])
    if repository_root not in sys.path:
        sys.path.insert(0, repository_root)
    from packages.core.blueprints.installer import InstallError, resolve_install_variables

    try:
        resolved, _ = resolve_install_variables(source, variable_values)
    except InstallError as exc:
        raise VerificationError(f"install variables are invalid: {exc}") from exc
    return resolved


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_state(run_dir: Path) -> dict[str, Any]:
    path = run_dir / _STATE_FILE
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise VerificationError(f"cannot read durable run state: {exc}") from exc
    if not isinstance(value, dict):
        raise VerificationError("durable run state must be an object")
    return value


def _save_state(run_dir: Path, state: dict[str, Any]) -> None:
    path = run_dir / _STATE_FILE
    temporary = path.with_suffix(".tmp")
    serialized = (json.dumps(state, indent=2, sort_keys=True) + "\n").encode("utf-8")
    with temporary.open("wb") as output:
        output.write(serialized)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(path)


def _origin(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise VerificationError("API origin must be a bare http(s) origin")
    return f"{parsed.scheme}://{parsed.netloc}"


def _payload(response: httpx.Response, *, operation: str) -> dict[str, Any]:
    try:
        response.raise_for_status()
        value = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise VerificationError(f"{operation} failed: {exc}") from exc
    if not isinstance(value, dict):
        raise VerificationError(f"{operation} returned a non-object payload")
    return value


def _account_identity(client: httpx.Client, *, origin: str) -> dict[str, str]:
    value = _payload(client.get(f"{origin}/api/v1/auth/me"), operation="authenticated identity lookup")
    user_id = str(value.get("id") or "").strip()
    entity_id = str(value.get("entity_id") or "").strip()
    if not user_id or not entity_id:
        raise VerificationError("authenticated identity lookup did not return id and entity_id")
    return {"user_id": user_id, "entity_id": entity_id}


def _cleanup(client: httpx.Client, *, origin: str, run_dir: Path, state: dict[str, Any]) -> list[str]:
    pending = list(state.get("cleanup_pending_workspace_ids") or [])
    installed = state.get("installed_workspace_ids") or []
    source = state.get("source_workspace_id")
    if (
        not all(isinstance(item, str) and item.isalnum() for item in pending)
        or len(set(pending)) != len(pending)
        or not isinstance(installed, list)
        or not set(pending).issubset(set(installed))
        or source in pending
    ):
        raise VerificationError("run state has unsafe cleanup IDs; refusing to delete")
    failures: list[str] = []
    for workspace_id in pending:
        try:
            response = client.delete(f"{origin}/api/v1/workspaces/{workspace_id}")
        except httpx.HTTPError as exc:
            failures.append(f"{workspace_id}: {exc}")
            continue
        if response.status_code not in (204, 404):
            failures.append(f"{workspace_id}: delete returned {response.status_code}")
            continue
        state["cleanup_pending_workspace_ids"].remove(workspace_id)
        state.setdefault("cleaned_workspace_ids", []).append(workspace_id)
        _save_state(run_dir, state)
    state["cleanup_failures"] = failures
    _save_state(run_dir, state)
    return failures


def cleanup_only(client: httpx.Client, *, origin: str, run_dir: Path) -> None:
    state = _read_state(run_dir)
    if state.get("origin") != origin:
        raise VerificationError("API origin does not match the recorded run")
    if state.get("account") != _account_identity(client, origin=origin):
        raise VerificationError("authenticated account does not match the recorded run")
    if state.get("install_response_uncertain") or state.get("install_in_flight"):
        raise VerificationError(
            "install response was lost; manually reconcile the exact Workspace before cleanup can succeed"
        )
    failures = _cleanup(client, origin=origin, run_dir=run_dir, state=state)
    if failures:
        raise VerificationError("cleanup still pending: " + "; ".join(failures))


def verify(
    client: httpx.Client,
    *,
    origin: str,
    source_workspace_id: str,
    run_dir: Path,
    install_options: dict[str, Any] | None = None,
    export_options: dict[str, Any] | None = None,
) -> None:
    """Run the portable export/install/export comparison and clean the install."""
    if not source_workspace_id or not source_workspace_id.isalnum():
        raise VerificationError("source_workspace_id must be an alphanumeric Workspace ID")
    try:
        run_dir.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise VerificationError("run directory already exists; use --cleanup-only to retry cleanup") from exc
    state: dict[str, Any] = {
        "version": 1,
        "origin": origin,
        "source_workspace_id": source_workspace_id,
        "cleanup_pending_workspace_ids": [],
        "cleaned_workspace_ids": [],
    }
    _save_state(run_dir, state)
    try:
        state["account"] = _account_identity(client, origin=origin)
        _save_state(run_dir, state)
        source_export_options = dict(export_options or {})
        source = _payload(
            client.post(
                f"{origin}/api/v1/workspaces/{source_workspace_id}/export-blueprint-payload",
                json=source_export_options,
            ),
            operation="source payload export",
        )
        _write_json(run_dir / "source-payload.json", source)

        request = {
            "mode": "simulate",
            **(install_options or {}),
            "payload": source,
            "workspace_source": {
                "workspace_id": source_workspace_id,
                "export_options": source_export_options,
            },
        }
        expected = _expected_payload(source, request.get("variable_values"))
        _write_json(run_dir / "expected-payload.json", expected)
        state["install_in_flight"] = True
        state["install_response_uncertain"] = True
        _save_state(run_dir, state)
        try:
            install_response = client.post(f"{origin}/api/v1/blueprints/install-payload", json=request)
        except httpx.HTTPError as exc:
            state["install_error"] = str(exc)
            _save_state(run_dir, state)
            raise VerificationError("install response was lost; refusing to retry or guess a Workspace ID") from exc
        # A client-visible 4xx proves the request was rejected.  A 5xx can be
        # emitted after a remote commit, so it remains an unresolved install.
        if 400 <= install_response.status_code < 500:
            state["install_in_flight"] = False
            state["install_response_uncertain"] = False
            state["install_error"] = f"payload install returned {install_response.status_code}"
            _save_state(run_dir, state)
        install = _payload(install_response, operation="payload install")
        workspace_id = install.get("workspace_id")
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or not workspace_id.isalnum()
            or workspace_id == source_workspace_id
        ):
            raise VerificationError("payload install did not return a safe new workspace_id")
        state["installed_workspace_ids"] = [workspace_id]
        state["cleanup_pending_workspace_ids"] = [workspace_id]
        state["install_in_flight"] = False
        state["install_response_uncertain"] = False
        _save_state(run_dir, state)
        _write_json(run_dir / "install-response.json", install)

        todos = install.get("todos")
        if not isinstance(todos, list) or not all(
            isinstance(todo, dict) and isinstance(todo.get("blocking"), bool)
            for todo in todos
        ):
            raise VerificationError("payload install returned malformed todos")
        blocking = [todo for todo in todos if todo["blocking"]]
        if blocking:
            raise VerificationError("install returned blocking todos; parity is not established")
        installed_export_options = dict(source_export_options)
        # Source document IDs belong to the original Workspace. The installed
        # copy has new IDs, so let its exporter select copied safe starters;
        # an explicit empty list remains an intentional exclusion.
        if installed_export_options.get("knowledge_document_ids"):
            installed_export_options.pop("knowledge_document_ids")
        installed = _payload(
            client.post(
                f"{origin}/api/v1/workspaces/{workspace_id}/export-blueprint-payload",
                json=installed_export_options,
            ),
            operation="installed payload export",
        )
        _write_json(run_dir / "installed-payload.json", installed)
        differences = compare(expected, installed)
        if differences:
            raise VerificationError("; ".join(differences))
    finally:
        failures = _cleanup(client, origin=origin, run_dir=run_dir, state=state)
        if failures and sys.exc_info()[0] is None:
            raise VerificationError("cleanup failed: " + "; ".join(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source_workspace_id", nargs="?")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cleanup-only", action="store_true")
    parser.add_argument("--mode", choices=("simulate", "live"), default="simulate")
    parser.add_argument("--install-options", type=Path, help="JSON object merged into install request")
    parser.add_argument("--export-options", type=Path, help="JSON object sent to payload export")
    args = parser.parse_args()
    origin = _origin(os.environ.get("MANOR_API_ORIGIN", ""))
    token = os.environ.get("MANOR_API_TOKEN", "").strip()
    if not token:
        parser.error("MANOR_API_TOKEN is required")
    if args.cleanup_only:
        if args.source_workspace_id:
            parser.error("source_workspace_id is not used with --cleanup-only")
        options: dict[str, Any] = {}
        export_options: dict[str, Any] = {}
    elif not args.source_workspace_id:
        parser.error("source_workspace_id is required")
    else:
        try:
            options = json.loads(args.install_options.read_text(encoding="utf-8")) if args.install_options else {}
            export_options = json.loads(args.export_options.read_text(encoding="utf-8")) if args.export_options else {}
        except (OSError, json.JSONDecodeError) as exc:
            parser.error(f"cannot read options: {exc}")
        if not isinstance(options, dict):
            parser.error("--install-options must contain a JSON object")
        if not isinstance(export_options, dict):
            parser.error("--export-options must contain a JSON object")
        options["mode"] = args.mode
    with httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=30.0) as client:
        try:
            if args.cleanup_only:
                cleanup_only(client, origin=origin, run_dir=args.run_dir)
            else:
                verify(
                    client,
                    origin=origin,
                    source_workspace_id=args.source_workspace_id,
                    run_dir=args.run_dir,
                    install_options=options,
                    export_options=export_options,
                )
        except VerificationError as exc:
            print(f"FAIL: {exc}", file=sys.stderr)
            return 1
    print("Cleanup complete" if args.cleanup_only else "Blueprint payload round-trip passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
