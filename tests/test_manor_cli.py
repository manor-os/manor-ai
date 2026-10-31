"""The in-sandbox ``manor`` CLI: one command list, real handlers, clear exits.

The CLI is driven against a local stub of the sandbox CLI API, so these tests
pin what it sends and prints; ``test_sandbox_cli_api.py`` pins the server.
"""

from __future__ import annotations

import ast
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from packages.core.services.sandbox_cli import manor_cli

ROOT = Path(__file__).resolve().parents[1]


class _Stub:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict, str]] = []
        self.responses: dict[str, tuple[int, object, dict]] = {}


@pytest.fixture
def api(monkeypatch, tmp_path):
    stub = _Stub()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            endpoint = self.path.rsplit("/", 1)[-1]
            stub.requests.append((endpoint, body, self.headers.get("Authorization", "")))
            status, payload, headers = stub.responses.get(endpoint, (200, {}, {}))
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("MANOR_API_URL", f"http://127.0.0.1:{server.server_address[1]}")
    monkeypatch.setenv("MANOR_CLI_TOKEN", "mcli_test-token")
    monkeypatch.setenv("MANOR_WORKSPACE", str(workspace))
    monkeypatch.chdir(workspace)
    stub.workspace = workspace
    try:
        yield stub
    finally:
        server.shutdown()


def test_command_list_handlers_and_parser_are_one_to_one():
    names = [spec.name for spec in manor_cli.COMMANDS]
    assert len(names) == len(set(names))
    assert set(names) == set(manor_cli.HANDLERS)
    parser = manor_cli.build_parser()
    subparsers = next(
        action for action in parser._actions if action.__class__.__name__ == "_SubParsersAction"
    )
    assert set(subparsers.choices) == set(names)
    for spec in manor_cli.COMMANDS:
        assert spec.usage.startswith(f"manor {spec.name}")


def test_every_endpoint_the_cli_calls_exists_on_the_server():
    tree = ast.parse(Path(manor_cli.__file__).read_text(encoding="utf-8"))
    called = {
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_request"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    }
    from apps.api.routers.sandbox_cli import router

    served = {route.path.rsplit("/", 1)[-1] for route in router.routes}
    assert called, "the scan must find the CLI's requests"
    assert called <= served, called - served
    assert served - called == set(), "an endpoint no CLI command uses is dead surface"


def test_cli_is_stdlib_only():
    tree = ast.parse(Path(manor_cli.__file__).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    import sys

    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported


def test_dockerfile_installs_the_cli_from_its_source(tmp_path):
    dockerfile = (ROOT / "docker" / "Dockerfile.sandbox").read_text(encoding="utf-8")
    assert "COPY packages/core/services/sandbox_cli/manor_cli.py /usr/local/lib/manor/manor_cli.py" in dockerfile
    assert "/usr/local/bin/manor" in dockerfile
    # The runner mounts the shared /workspace tmpfs (one area per conversation
    # or task): the image must not claim /workspace with a link of its own.
    assert "ln -s /skill/workspace /workspace" not in dockerfile
    block = dockerfile.split("# ── manor CLI", 1)[1].split("# ── end manor CLI", 1)[0]
    assert "MANOR_CLI_TOKEN=" not in block and "ENV MANOR" not in block, "never bake a token"


def test_missing_token_exits_3(monkeypatch, capsys):
    monkeypatch.delenv("MANOR_CLI_TOKEN", raising=False)
    monkeypatch.setenv("MANOR_API_URL", "http://127.0.0.1:9")
    assert manor_cli.main(["ls"]) == manor_cli.EXIT_AUTH
    assert "only" in capsys.readouterr().err


def test_unreachable_api_exits_4(monkeypatch, capsys):
    monkeypatch.setenv("MANOR_API_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("MANOR_CLI_TOKEN", "mcli_x")
    assert manor_cli.main(["ls"]) == manor_cli.EXIT_UNREACHABLE
    assert "cannot reach" in capsys.readouterr().err


def test_ls_sends_entity_paths_and_prints_names(api, capsys):
    (api.workspace / "Knowledge").mkdir()
    api.responses["ls"] = (200, {
        "path": "Knowledge",
        "entries": [
            {"path": "Knowledge/a.md", "type": "file", "size": 3},
            {"path": "Knowledge/sub", "type": "dir", "size": None},
        ],
        "has_more": False,
    }, {})
    assert manor_cli.main(["ls", "-l", "/workspace/Knowledge"]) == 0
    out = capsys.readouterr().out
    assert "a.md" in out and "sub/" in out
    endpoint, body, auth = api.requests[-1]
    assert endpoint == "ls" and body["path"] == "Knowledge" and auth == "Bearer mcli_test-token"

    import os

    os.chdir(api.workspace / "Knowledge")
    assert manor_cli.main(["ls", "sub"]) == 0
    assert api.requests[-1][1]["path"] == "Knowledge/sub", "relative to the cwd under /workspace"


def test_server_refusal_and_expired_token_exit_codes(api, capsys):
    api.responses["read"] = (404, {"error": "not_found", "detail": "File not found: x.md"}, {})
    assert manor_cli.main(["cat", "x.md"]) == manor_cli.EXIT_REFUSED
    assert "File not found" in capsys.readouterr().err
    api.responses["read"] = (401, {"error": "token_invalid", "detail": "expired"}, {})
    assert manor_cli.main(["cat", "x.md"]) == manor_cli.EXIT_AUTH


def test_grep_prints_matches_and_exits_1_without_any(api, capsys):
    api.responses["grep"] = (200, {"matches": [{"file": "a.md", "line": 2, "text": "hit"}]}, {})
    assert manor_cli.main(["grep", "hit", "Knowledge", "--glob", "*.md"]) == 0
    assert capsys.readouterr().out.strip() == "a.md:2:hit"
    assert api.requests[-1][1] == {
        "pattern": "hit", "path": "Knowledge", "glob": "*.md", "max_matches": None, "offset": None,
    }
    api.responses["grep"] = (200, {"matches": []}, {})
    assert manor_cli.main(["grep", "nothing"]) == 1


def test_pull_writes_into_the_workspace_copy(api, capsys):
    api.responses["pull"] = (200, b"a,b\n1,2\n", {"X-Manor-Path": "Knowledge/data.csv"})
    assert manor_cli.main(["pull", "Knowledge/data.csv"]) == 0
    assert (api.workspace / "Knowledge" / "data.csv").read_bytes() == b"a,b\n1,2\n"
    assert not list(api.workspace.rglob("*.manor-pull"))


def test_push_mkdir_touch_are_local_workspace_changes(api, tmp_path):
    scratch = tmp_path / "chart.png"
    scratch.write_bytes(b"PNG")
    assert manor_cli.main(["push", str(scratch), "Reports/chart.png"]) == 0
    assert (api.workspace / "Reports" / "chart.png").read_bytes() == b"PNG"
    assert manor_cli.main(["mkdir", "-p", "Archive/2026"]) == 0
    assert (api.workspace / "Archive" / "2026").is_dir()
    assert manor_cli.main(["touch", "Archive/2026/notes.md"]) == 0
    assert (api.workspace / "Archive" / "2026" / "notes.md").is_file()
    assert api.requests == [], "local changes are saved by the bash tool, not the API"


def test_mv_cp_rm_queue_server_operations(api, capsys):
    api.responses["ops"] = (200, {"queued": True, "paths": ["a", "b"], "note": "later"}, {})
    assert manor_cli.main(["mv", "Knowledge/a.md", "/workspace/Archive/"]) == 0
    assert api.requests[-1][1] == {"op": "mv", "paths": ["Knowledge/a.md", "Archive"], "recursive": False}
    assert manor_cli.main(["rm", "-rf", "Old"]) == 0
    assert api.requests[-1][1] == {"op": "rm", "paths": ["Old"], "recursive": True}
    assert manor_cli.main(["cp", "only-one"]) == manor_cli.EXIT_USAGE


