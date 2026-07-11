"""Unit tests for the in-process GitHub MCP server (packages.core.ai.mcp.github).

Focus: the comment edit/delete and Actions write surfaces (dispatch / re-run /
cancel / job logs), plus schema↔handler registration parity. HTTP is faked by
swapping ``httpx.AsyncClient`` so no network is touched — assertions are on the
method, URL and body the handler would send to the GitHub REST API.
"""

from __future__ import annotations

import json

import pytest

import packages.core.ai.mcp.github as g

_TOKEN = "gh-test-token"


# ── httpx fake ───────────────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status: int = 200, json_body=None, text: str | None = None):
        self.status_code = status
        self._json = json_body
        if text is not None:
            self.text = text
        elif json_body is not None:
            self.text = json.dumps(json_body)
        else:
            self.text = ""

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


class _FakeClient:
    """Captures requests at class level; returns ``response``."""

    calls: list[dict] = []
    init_kwargs: dict = {}
    response: _FakeResp = _FakeResp(200, {"ok": True})

    def __init__(self, *_a, **kw):
        _FakeClient.init_kwargs = kw

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def request(self, method, url, headers=None, json=None):
        _FakeClient.calls.append({"method": method, "url": url, "headers": headers, "json": json})
        return _FakeClient.response

    async def get(self, url, headers=None):
        _FakeClient.calls.append({"method": "GET", "url": url, "headers": headers, "json": None})
        return _FakeClient.response


@pytest.fixture
def fake_http(monkeypatch):
    """Patch the module's httpx.AsyncClient and reset capture state."""
    _FakeClient.calls = []
    _FakeClient.init_kwargs = {}
    _FakeClient.response = _FakeResp(200, {"ok": True})
    monkeypatch.setattr(g.httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _last(fake):
    assert fake.calls, "no HTTP request was made"
    return fake.calls[-1]


# ── Registration parity ──────────────────────────────────────────────────────


def test_every_schema_has_a_handler_and_vice_versa():
    names = {t["name"] for t in g.list_tools()}
    assert names == set(g._HANDLERS), f"schema-only={names - set(g._HANDLERS)} handler-only={set(g._HANDLERS) - names}"


def test_new_tools_are_registered():
    names = {t["name"] for t in g.list_tools()}
    for n in (
        "update_comment",
        "delete_comment",
        "list_pr_review_comments",
        "update_review_comment",
        "delete_review_comment",
        "get_job_logs",
        "run_workflow",
        "rerun_workflow_run",
        "rerun_failed_jobs",
        "cancel_workflow_run",
        "list_run_artifacts",
        "get_commit_status",
        "list_check_runs",
    ):
        assert n in names and n in g._HANDLERS


# ── Comment edit / delete ────────────────────────────────────────────────────


async def test_update_comment(fake_http):
    fake_http.response = _FakeResp(200, {"id": 42, "body": "edited"})
    out = await g.call_tool(
        "update_comment",
        {"repo": "o/r", "comment_id": 42, "body": "edited"},
        _TOKEN,
    )
    call = _last(fake_http)
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/repos/o/r/issues/comments/42")
    assert call["json"] == {"body": "edited"}
    assert out["isError"] is False


async def test_delete_comment_returns_confirmation(fake_http):
    fake_http.response = _FakeResp(204, None, "")  # GitHub: 204 No Content
    out = await g.call_tool("delete_comment", {"repo": "o/r", "comment_id": 42}, _TOKEN)
    call = _last(fake_http)
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/repos/o/r/issues/comments/42")
    payload = json.loads(out["content"][0]["text"])
    assert payload["ok"] is True and "42" in payload["message"]


async def test_update_review_comment_uses_pulls_path(fake_http):
    fake_http.response = _FakeResp(200, {"id": 7})
    await g.call_tool(
        "update_review_comment",
        {"repo": "o/r", "comment_id": 7, "body": "nit fixed"},
        _TOKEN,
    )
    call = _last(fake_http)
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/repos/o/r/pulls/comments/7")
    assert call["json"] == {"body": "nit fixed"}


async def test_delete_review_comment(fake_http):
    fake_http.response = _FakeResp(204, None, "")
    await g.call_tool("delete_review_comment", {"repo": "o/r", "comment_id": 7}, _TOKEN)
    call = _last(fake_http)
    assert call["method"] == "DELETE"
    assert call["url"].endswith("/repos/o/r/pulls/comments/7")


async def test_list_pr_review_comments(fake_http):
    fake_http.response = _FakeResp(200, [])
    await g.call_tool("list_pr_review_comments", {"repo": "o/r", "number": 9}, _TOKEN)
    call = _last(fake_http)
    assert call["method"] == "GET"
    assert "/repos/o/r/pulls/9/comments" in call["url"]


# ── Actions: write surface ───────────────────────────────────────────────────


async def test_run_workflow_dispatch_with_inputs(fake_http):
    fake_http.response = _FakeResp(204, None, "")
    out = await g.call_tool(
        "run_workflow",
        {"repo": "o/r", "workflow_id": "ci.yml", "ref": "dev", "inputs": {"env": "staging"}},
        _TOKEN,
    )
    call = _last(fake_http)
    assert call["method"] == "POST"
    assert call["url"].endswith("/repos/o/r/actions/workflows/ci.yml/dispatches")
    assert call["json"] == {"ref": "dev", "inputs": {"env": "staging"}}
    payload = json.loads(out["content"][0]["text"])
    assert payload["ok"] is True


async def test_run_workflow_accepts_json_string_inputs(fake_http):
    fake_http.response = _FakeResp(204, None, "")
    await g.call_tool(
        "run_workflow",
        {"repo": "o/r", "workflow_id": "ci.yml", "ref": "main", "inputs": '{"debug": "true"}'},
        _TOKEN,
    )
    assert _last(fake_http)["json"] == {"ref": "main", "inputs": {"debug": "true"}}


async def test_run_workflow_omits_empty_inputs(fake_http):
    fake_http.response = _FakeResp(204, None, "")
    await g.call_tool(
        "run_workflow",
        {"repo": "o/r", "workflow_id": "ci.yml", "ref": "main"},
        _TOKEN,
    )
    assert _last(fake_http)["json"] == {"ref": "main"}


async def test_run_workflow_rejects_bad_json_inputs(fake_http):
    out = await g.call_tool(
        "run_workflow",
        {"repo": "o/r", "workflow_id": "ci.yml", "ref": "main", "inputs": "not-json"},
        _TOKEN,
    )
    assert "JSON object" in out["content"][0]["text"]
    assert not fake_http.calls  # bailed before any HTTP call


@pytest.mark.parametrize(
    "tool,suffix",
    [
        ("rerun_workflow_run", "/rerun"),
        ("rerun_failed_jobs", "/rerun-failed-jobs"),
        ("cancel_workflow_run", "/cancel"),
    ],
)
async def test_run_control_endpoints(fake_http, tool, suffix):
    fake_http.response = _FakeResp(201, None, "")
    out = await g.call_tool(tool, {"repo": "o/r", "run_id": 555}, _TOKEN)
    call = _last(fake_http)
    assert call["method"] == "POST"
    assert call["url"].endswith(f"/repos/o/r/actions/runs/555{suffix}")
    payload = json.loads(out["content"][0]["text"])
    assert payload["ok"] is True and "555" in payload["message"]


# ── Actions: job logs ────────────────────────────────────────────────────────


async def test_get_job_logs_follows_redirects_and_returns_text(fake_http):
    fake_http.response = _FakeResp(200, None, "line1\nline2\nERROR boom")
    out = await g.call_tool("get_job_logs", {"repo": "o/r", "job_id": 123}, _TOKEN)
    call = _last(fake_http)
    assert call["url"].endswith("/repos/o/r/actions/jobs/123/logs")
    assert fake_http.init_kwargs.get("follow_redirects") is True
    assert "ERROR boom" in out["content"][0]["text"]


async def test_get_job_logs_tails_large_output(fake_http):
    big = "x" * (g._MAX_CHARS + 5000) + "TAIL_MARKER"
    fake_http.response = _FakeResp(200, None, big)
    out = await g.call_tool("get_job_logs", {"repo": "o/r", "job_id": 1}, _TOKEN)
    text = out["content"][0]["text"]
    assert "truncated" in text and text.rstrip().endswith("TAIL_MARKER")
    assert len(text) <= g._MAX_CHARS + 64


# ── Actions: artifacts, status, checks ───────────────────────────────────────


async def test_list_run_artifacts(fake_http):
    fake_http.response = _FakeResp(200, {"total_count": 0, "artifacts": []})
    await g.call_tool("list_run_artifacts", {"repo": "o/r", "run_id": 99}, _TOKEN)
    call = _last(fake_http)
    assert call["method"] == "GET"
    assert "/repos/o/r/actions/runs/99/artifacts" in call["url"]


async def test_get_commit_status_preserves_branch_slashes(fake_http):
    fake_http.response = _FakeResp(200, {"state": "success"})
    await g.call_tool("get_commit_status", {"repo": "o/r", "ref": "feature/x"}, _TOKEN)
    call = _last(fake_http)
    assert call["method"] == "GET"
    # default quote keeps '/', matching the rest of the module's ref handling
    assert call["url"].endswith("/repos/o/r/commits/feature/x/status")


async def test_list_check_runs(fake_http):
    fake_http.response = _FakeResp(200, {"total_count": 1, "check_runs": []})
    await g.call_tool("list_check_runs", {"repo": "o/r", "ref": "abc123"}, _TOKEN)
    call = _last(fake_http)
    assert call["method"] == "GET"
    assert "/repos/o/r/commits/abc123/check-runs" in call["url"]


# ── Protocol-level guards ────────────────────────────────────────────────────


async def test_missing_required_param_is_rejected(fake_http):
    out = await g.call_tool("update_comment", {"repo": "o/r"}, _TOKEN)
    assert out["isError"] is True
    assert "Missing required params" in out["content"][0]["text"]
    assert not fake_http.calls


async def test_unknown_tool_is_rejected(fake_http):
    out = await g.call_tool("nope_not_a_tool", {}, _TOKEN)
    assert out["isError"] is True


def test_ok_or_passes_real_errors_through():
    assert json.loads(g._ok_or('{"ok": true}', "done"))["message"] == "done"
    assert g._ok_or("GitHub forbidden (rate limit ...)", "done").startswith("GitHub forbidden")
