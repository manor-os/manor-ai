"""Regression tests for the MCP correctness sweep.

Two house-convention bugs were fixed family-wide:
  (b) non-2xx upstream responses must surface as ``isError: True`` — they used
      to be ``return``ed as plain strings, which the dispatcher wrapped as a
      *successful* tool result, so the agent couldn't tell failure from success.
  (a) the required-param guard rejected falsy-but-valid values (e.g. setting an
      Excel cell to ``0``); it now only treats ``None``/``""`` as missing.

HTTP is faked by swapping each module's ``httpx.AsyncClient``.
"""

from __future__ import annotations

import json

import pytest

import packages.core.ai.mcp.gmail as gmail
import packages.core.ai.mcp.ms_excel as xl
import packages.core.ai.mcp.linkedin as li


class _Resp:
    def __init__(self, status, json_body=None, text=""):
        self.status_code = status
        self._j = json_body
        self.text = text or (json.dumps(json_body) if json_body is not None else "")
        self.headers = {}

    @property
    def is_success(self):
        return 200 <= self.status_code < 300

    def json(self):
        if self._j is None:
            raise ValueError("no json")
        return self._j


class _Client:
    calls: list = []
    resp = _Resp(200, {"ok": True})

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def request(self, method, url, headers=None, json=None, params=None):
        _Client.calls.append({"method": method, "url": url, "json": json, "params": params})
        return _Client.resp

    # linkedin uses verb-specific methods
    async def get(self, url, headers=None):
        _Client.calls.append({"method": "GET", "url": url})
        return _Client.resp

    async def post(self, url, headers=None, json=None):
        _Client.calls.append({"method": "POST", "url": url, "json": json})
        return _Client.resp


@pytest.fixture
def fake(monkeypatch):
    _Client.calls = []
    _Client.resp = _Resp(200, {"ok": True})
    for mod in (gmail, xl, li):
        monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)
    return _Client


# ── bug-b: non-2xx surfaces as isError (was reported as success) ──────────────


async def test_gmail_non_2xx_surfaces_as_error(fake):
    fake.resp = _Resp(403, text="forbidden")
    out = await gmail.call_tool("list_messages", {"query": "x"}, "tok")
    assert out["isError"] is True
    assert "forbidden" in out["content"][0]["text"].lower()


async def test_excel_non_2xx_surfaces_as_error(fake):
    fake.resp = _Resp(403, text="nope")
    out = await xl.call_tool(
        "update_cell",
        {"file_id": "f", "worksheet": "Sheet1", "address": "B5", "value": 1},
        "tok",
    )
    assert out["isError"] is True


async def test_linkedin_non_2xx_surfaces_as_error(fake):
    fake.resp = _Resp(401, text="bad token")
    out = await li.call_tool("get_profile", {}, "tok")
    assert out["isError"] is True


# ── bug-a: falsy-but-valid required params accepted ───────────────────────────


async def test_excel_update_cell_value_zero_allowed(fake):
    fake.resp = _Resp(200, {"values": [[0]]})
    out = await xl.call_tool(
        "update_cell",
        {"file_id": "f", "worksheet": "Sheet1", "address": "B5", "value": 0},
        "tok",
    )
    assert out["isError"] is False
    assert fake.calls[-1]["json"] == {"values": [[0]]}


# ── gmail pagination cursor now actually sent ─────────────────────────────────


async def test_gmail_list_messages_forwards_page_token(fake):
    fake.resp = _Resp(200, {"messages": []})
    await gmail.call_tool("list_messages", {"query": "x", "page_token": "PG2"}, "tok")
    assert fake.calls[-1]["params"]["pageToken"] == "PG2"
