from __future__ import annotations

import copy
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from packages.core.services import apify_client as module
from packages.core.services.apify_client import (
    ApifyClient,
    ApifyError,
    normalize_items,
    prepare_collection,
)


NOW = datetime(2026, 9, 17, 12, 0, tzinfo=timezone.utc)
_REAL_ASYNC_CLIENT = httpx.AsyncClient


def profile(**kwargs):
    return prepare_collection({"mode": "profile", "profile_url": "@Example", **kwargs}, now=NOW)


def reel(**kwargs):
    return prepare_collection({"mode": "reel", "reel_url": "https://instagram.com/reel/Abc_123-/", **kwargs}, now=NOW)


def install_http(monkeypatch, handler):
    requests = []
    options = []

    def record(request):
        requests.append(request)
        return handler(request)

    def factory(**kwargs):
        options.append(kwargs)
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(record), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return requests, options


def receipt(run_id="Run123", status="RUNNING", *, cost=None):
    payload = {
        "structuredContent": {
            "runId": run_id,
            "status": status,
            "storages": {"datasets": {"default": {"id": "Data123"}}},
        }
    }
    if cost is not None:
        payload["_meta"] = {"com.apify/ActorRun": {"usageTotalUsd": cost}}
    return payload


def install_mcp(monkeypatch, tool_handler):
    calls = []

    def respond(request):
        message = json.loads(request.content)
        if message["method"] == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "serverInfo": {"name": "Apify", "version": "test"},
            }
        elif message["method"] == "notifications/initialized":
            return httpx.Response(202)
        else:
            assert message["method"] == "tools/call"
            calls.append(message["params"])
            result = tool_handler(request, message)
            if isinstance(result, httpx.Response):
                return result
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    requests, options = install_http(monkeypatch, respond)
    return calls, requests, options


@pytest.mark.parametrize("days", [7, 14, 28])
def test_profile_spec_freezes_one_creator_and_utc_window(days):
    result = profile(lookback_days=days)
    assert result["actor_id"] == "apify~instagram-scraper"
    assert result["actor_input"] == {
        "directUrls": ["https://www.instagram.com/example/"],
        "resultsType": "posts",
        "resultsLimit": 50,
        "onlyPostsNewerThan": (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z"),
        "addParentData": False,
    }
    assert result["window"]["end"] == "2026-09-17T12:00:00Z"
    assert json.loads(json.dumps(result)) == result


def test_local_time_converts_to_utc_and_search_does_not_scrape_posts():
    local = NOW.astimezone(timezone(timedelta(hours=-7)))
    assert prepare_collection({"mode": "profile", "profile_url": "example"}, now=local) == profile()
    result = prepare_collection({"mode": "search", "query": "fitness coach"}, now=NOW)
    assert result["window"] is None
    assert result["actor_input"] == {
        "search": "fitness coach",
        "searchType": "user",
        "searchLimit": 10,
        "resultsType": "details",
        "resultsLimit": 1,
    }


def test_selected_reel_has_no_date_filter_or_unrequested_paid_addons():
    result = reel()
    assert result["results_limit"] == 1
    assert result["window"] is None
    assert result["actor_id"] == "apify~instagram-reel-scraper"
    assert result["actor_input"] == {
        "username": ["https://www.instagram.com/reel/Abc_123-/"],
        "resultsLimit": 1,
        "includeTranscript": False,
        "includeDownloadedVideo": False,
        "includeSharesCount": False,
    }
    enabled = reel(include_transcript=True, include_downloaded_video=True)
    assert enabled["actor_input"]["includeTranscript"] is True
    assert enabled["actor_input"]["includeDownloadedVideo"] is True


@pytest.mark.parametrize("tracking", ["igsh=shared-token", "igshid=old-token", "utm_source=instagram&utm_medium=share"])
def test_pasted_share_links_strip_tracking_before_provider_dispatch(tracking):
    spec = prepare_collection(
        {
            "mode": "profile",
            "profile_url": f"https://www.instagram.com/example/?{tracking}",
        },
        now=NOW,
    )
    assert spec == profile()
    spec = prepare_collection(
        {
            "mode": "reel",
            "reel_url": f"https://www.instagram.com/reel/Abc_123-/?{tracking}",
        },
        now=NOW,
    )
    assert spec == reel()


@pytest.mark.parametrize(
    "source",
    [
        "http://instagram.com/example",
        "https://instagram.com.evil.test/example",
        "https://evil.test/@example",
        "https://instagram.com@evil.test/example",
        "https://user:password@instagram.com/example",
        "https://instagram.com:443/example",
        "https://instagram.com/example/../../reel/x",
        "https://instagram.com/%2e%2e",
        "https://instagram.com/example?next=https://evil.test",
        "https://instagram.com/example#secret",
        "https://instagram.com/example/reels/",
        "https://instagram.com//example/",
        "https://instagram.com./example",
        "https://127.0.0.1/example",
        "https://instagram.com/\\evil",
        "https://instagram.com/exam\nple",
        "../private",
        "@@example",
        "one,two",
        "has spaces",
        "a..b",
        ".abc",
        "abc.",
        "reel",
        "accounts",
        ["a", "b"],
        None,
    ],
)
def test_invalid_profiles_fail_before_provider_io(source):
    with pytest.raises(ApifyError) as raised:
        profile(profile_url=source)
    assert raised.value.code == "invalid_arguments"
    assert raised.value.uncertain is False


@pytest.mark.parametrize(
    "args",
    [
        {"mode": "profile", "profile_url": "x", "lookback_days": 8},
        {"mode": "profile", "profile_url": "x", "lookback_days": True},
        {"mode": "profile", "profile_url": "x", "results_limit": 201},
        {"mode": "profile", "profile_url": "x", "results_limit": 1.5},
        {"mode": "profile", "profile_url": "x", "results_limit": "2"},
        {"mode": "profile", "profile_url": "x", "results_limit": True},
        {"mode": "profile", "profile_url": "x", "results_limit": 0},
        {"mode": "search", "query": "x", "results_limit": 21},
        {"mode": "search", "query": "x", "include_transcript": True},
        {"mode": "search", "query": "x\nsecret"},
        {"mode": "search", "query": " "},
        {"mode": "reel", "reel_url": "https://instagram.com/p/ABC/"},
        {"mode": "reel", "reel_url": "https://instagram.com/reel/../"},
        {"mode": "reel", "reel_url": ["https://instagram.com/reel/ABC/"]},
        {"mode": "reel", "reel_url": "https://instagram.com/reel/ABC/", "results_limit": 2},
        {"mode": "reel", "reel_url": "https://instagram.com/reel/ABC/", "include_transcript": "true"},
        {"mode": "profile", "profile_url": "x", "actor_id": "evil"},
        {"mode": []},
        [],
    ],
)
def test_schema_cannot_expand_actor_input(args):
    with pytest.raises(ApifyError):
        prepare_collection(args, now=NOW)


def test_naive_clock_is_rejected():
    with pytest.raises(ApifyError):
        prepare_collection({"mode": "search", "query": "x"}, now=NOW.replace(tzinfo=None))


@pytest.mark.asyncio
async def test_start_calls_official_mcp_once_with_cost_and_runtime_caps(monkeypatch):
    calls, requests, options = install_mcp(monkeypatch, lambda *_: receipt())
    result = await ApifyClient("apify-test-secret").start(profile(), max_cost_usd="1.25")
    assert result == {"id": "Run123", "status": "RUNNING", "defaultDatasetId": "Data123"}
    assert len(calls) == 1
    assert calls[0] == {
        "name": "call-actor",
        "arguments": {
            "actor": "apify/instagram-scraper",
            "input": profile()["actor_input"],
            "waitSecs": 0,
            "callOptions": {"maxTotalChargeUsd": 1.25, "maxItems": 50, "timeout": 300},
        },
    }
    assert all(str(r.url) == module.APIFY_MCP_ENDPOINT for r in requests)
    assert all(r.headers["Authorization"] == "Bearer apify-test-secret" for r in requests)
    assert all("token" not in r.url.params for r in requests)
    assert len(requests) == 3
    assert all(option["timeout"] == 30 for option in options)


@pytest.mark.asyncio
@pytest.mark.parametrize("cap", ["0.1", "0.27", "0.000000001", "1.000000001", "5"])
async def test_json_numeric_budget_never_exceeds_decimal_quote(monkeypatch, cap):
    calls, _, _ = install_mcp(monkeypatch, lambda *_: receipt())
    await ApifyClient("token").start(profile(), max_cost_usd=cap)
    amount = calls[0]["arguments"]["callOptions"]["maxTotalChargeUsd"]
    assert isinstance(amount, (float, int)) and amount > 0
    assert Decimal.from_float(amount) <= Decimal(cap)
    assert Decimal(str(amount)) <= Decimal(cap)


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["actor", "extra_input", "second_profile", "paid_option", "long_window"])
async def test_forged_spec_is_rejected_without_start(monkeypatch, mutation):
    requests, _ = install_http(monkeypatch, lambda request: pytest.fail("Unexpected HTTP"))
    spec = copy.deepcopy(profile())
    if mutation == "actor":
        spec["actor_id"] = "apify~arbitrary"
    elif mutation == "extra_input":
        spec["actor_input"]["customFunction"] = "untrusted"
    elif mutation == "second_profile":
        spec["actor_input"]["directUrls"].append("https://www.instagram.com/other/")
    elif mutation == "paid_option":
        spec = reel()
        spec["actor_input"]["includeSharesCount"] = True
    else:
        spec["window"]["start"] = "2000-01-01T00:00:00Z"
    with pytest.raises(ApifyError):
        await ApifyClient("token").start(spec, max_cost_usd="1")
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "cap", ["0", "-1", "5.01", "NaN", "Infinity", "bad", None, 1, "1e99999", "1e-99999", "0.1234567891"]
)
async def test_invalid_cost_cap_rejected_before_start(monkeypatch, cap):
    requests, _ = install_http(monkeypatch, lambda request: pytest.fail("Unexpected HTTP"))
    with pytest.raises(ApifyError):
        await ApifyClient("token").start(profile(), max_cost_usd=cap)
    assert not requests


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,uncertain",
    [
        (400, False),
        (401, False),
        (403, False),
        (404, True),
        (429, False),
        (408, True),
        (500, True),
        (503, True),
        (302, True),
    ],
)
async def test_start_http_failure_is_sanitized_and_never_retried(monkeypatch, status, uncertain):
    secret = "apify-test-private"
    calls, requests, _ = install_mcp(
        monkeypatch,
        lambda *_: httpx.Response(
            status, text=f"{secret}: untrusted vendor body", headers={"Location": "https://evil.test/"}
        ),
    )
    with pytest.raises(ApifyError) as raised:
        await ApifyClient(secret).start(profile(), max_cost_usd="1")
    assert raised.value.uncertain is uncertain
    assert secret not in str(raised.value) and "vendor body" not in str(raised.value)
    assert len(calls) == 1 and len(requests) == 3
    assert all(r.url.host == "mcp.apify.com" for r in requests)


@pytest.mark.asyncio
async def test_handshake_failure_proves_no_start_was_sent(monkeypatch):
    requests, _ = install_http(monkeypatch, lambda _: httpx.Response(500, text="secret"))
    with pytest.raises(ApifyError) as raised:
        await ApifyClient("token").start(profile(), max_cost_usd="1")
    assert raised.value.uncertain is False
    assert len(requests) == 1 and json.loads(requests[0].content)["method"] == "initialize"


@pytest.mark.asyncio
async def test_start_timeout_is_uncertain_without_retry(monkeypatch):
    def fail(request, _message):
        raise httpx.ReadTimeout("secret or vendor URL must not escape", request=request)

    calls, _, _ = install_mcp(monkeypatch, fail)
    with pytest.raises(ApifyError) as raised:
        await ApifyClient("token").start(profile(), max_cost_usd="1")
    assert raised.value.uncertain is True and "secret" not in str(raised.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"structuredContent": {}},
        receipt("../../secret"),
        {"content": [{"type": "text", "text": "secret invalid body"}]},
        {"isError": True, "content": [{"type": "text", "text": "secret provider error"}]},
    ],
)
async def test_response_without_confirmed_receipt_remains_uncertain(monkeypatch, payload):
    calls, _, _ = install_mcp(monkeypatch, lambda *_: payload)
    with pytest.raises(ApifyError) as raised:
        await ApifyClient("token").start(profile(), max_cost_usd="1")
    assert raised.value.uncertain is True and "secret" not in str(raised.value)
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("code,uncertain", [(-32602, False), (-32601, False), (-32603, True), (-32600, True)])
async def test_start_jsonrpc_error_never_retries(monkeypatch, code, uncertain):
    def failure(_request, message):
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": message["id"], "error": {"code": code, "message": "secret"}}
        )

    calls, _, _ = install_mcp(monkeypatch, failure)
    with pytest.raises(ApifyError) as raised:
        await ApifyClient("token").start(profile(), max_cost_usd="1")
    assert raised.value.uncertain is uncertain and "secret" not in str(raised.value)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_valid_start_identity_survives_missing_enrichment_without_followup(monkeypatch):
    calls, _, _ = install_mcp(
        monkeypatch, lambda *_: {"structuredContent": {"runId": "Run123", "status": [], "storages": []}}
    )
    result = await ApifyClient("token").start(reel(), max_cost_usd="1")
    assert result == {"id": "Run123", "status": []}
    assert [c["name"] for c in calls] == ["call-actor"]
    assert calls[0]["arguments"]["actor"] == "apify/instagram-reel-scraper"


@pytest.mark.asyncio
async def test_error_with_valid_start_receipt_retains_identity_without_assuming_zero_cost(monkeypatch):
    payload = {**receipt(cost=0), "isError": True}
    calls, _, _ = install_mcp(monkeypatch, lambda *_: payload)
    run = await ApifyClient("token").start(profile(), max_cost_usd="1")
    assert run["id"] == "Run123" and "usageTotalUsd" not in run
    assert len(calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_key", ["id", "runId"])
async def test_optional_usage_identity_cannot_conflict_with_receipt(monkeypatch, identity_key):
    payload = receipt(cost=0)
    payload["_meta"]["com.apify/ActorRun"][identity_key] = "OtherRun"
    install_mcp(monkeypatch, lambda *_: payload)
    run = await ApifyClient("token").start(profile(), max_cost_usd="1")
    assert run["id"] == "Run123" and "usageTotalUsd" not in run
    with pytest.raises(ApifyError, match="conflicting usage identity"):
        await ApifyClient("token").get_run("Run123")


@pytest.mark.asyncio
@pytest.mark.parametrize("cost", [None, 0, 0.27, "NaN", -1])
async def test_only_namespaced_metadata_supplies_cost_without_zero_default(monkeypatch, cost):
    payload = receipt(status="SUCCEEDED", cost=cost)
    payload["structuredContent"]["usageTotalUsd"] = 0
    install_mcp(monkeypatch, lambda *_: payload)
    run = await ApifyClient("token").get_run("Run123")
    if cost is None:
        assert "usageTotalUsd" not in run
    else:
        assert run["usageTotalUsd"] == cost


@pytest.mark.asyncio
async def test_get_abort_items_use_official_contract_and_preserve_error_rows(monkeypatch):
    def respond(_request, message):
        if message["params"]["name"] == "get-dataset-items":
            return {"structuredContent": {"datasetId": "Data123", "offset": 0, "items": [{"error": "unavailable"}]}}
        return receipt(status="ABORTED", cost=0.04)

    calls, _, _ = install_mcp(monkeypatch, respond)
    client = ApifyClient("token")
    assert (await client.get_run("Run123"))["usageTotalUsd"] == 0.04
    assert (await client.abort_run("Run123"))["usageTotalUsd"] == 0.04
    assert await client.get_items("Data123", limit=20) == [{"error": "unavailable"}]
    assert calls == [
        {"name": "get-actor-run", "arguments": {"runId": "Run123", "waitSecs": 0}},
        {"name": "abort-actor-run", "arguments": {"runId": "Run123", "gracefully": False}},
        {"name": "get-actor-run", "arguments": {"runId": "Run123", "waitSecs": 0}},
        {"name": "get-dataset-items", "arguments": {"datasetId": "Data123", "limit": 20, "offset": 0, "clean": False}},
    ]


@pytest.mark.asyncio
async def test_abort_followup_failure_does_not_claim_confirmed_cancellation(monkeypatch):
    def respond(_request, message):
        if message["params"]["name"] == "abort-actor-run":
            return receipt(status="ABORTING")
        return {"isError": True}

    calls, _, _ = install_mcp(monkeypatch, respond)
    with pytest.raises(ApifyError):
        await ApifyClient("token").abort_run("Run123")
    assert [c["name"] for c in calls] == ["abort-actor-run", "get-actor-run"]


@pytest.mark.asyncio
async def test_text_only_json_mirror_is_supported(monkeypatch):
    install_mcp(
        monkeypatch, lambda *_: {"content": [{"type": "text", "text": json.dumps(receipt()["structuredContent"])}]}
    )
    assert (await ApifyClient("token").get_run("Run123"))["id"] == "Run123"


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", ["../foo", "id?token=x", "id#x", "id%2F", "id/bar", "", None])
async def test_resource_paths_are_validated_before_http(monkeypatch, bad_id):
    requests, _ = install_http(monkeypatch, lambda request: pytest.fail("Unexpected HTTP"))
    client = ApifyClient("token")
    for call in (
        lambda: client.get_run(bad_id),
        lambda: client.abort_run(bad_id),
        lambda: client.get_items(bad_id, limit=1),
    ):
        with pytest.raises(ApifyError):
            await call()
    assert not requests


@pytest.mark.asyncio
async def test_mismatched_run_and_unbounded_dataset_fail_closed(monkeypatch):
    install_mcp(monkeypatch, lambda *_: receipt("OtherRun"))
    with pytest.raises(ApifyError):
        await ApifyClient("token").get_run("Run123")
    install_mcp(monkeypatch, lambda *_: {"structuredContent": {"datasetId": "Data123", "offset": 0, "items": [{}, {}]}})
    with pytest.raises(ApifyError):
        await ApifyClient("token").get_items("Data123", limit=1)
    install_mcp(monkeypatch, lambda *_: {"structuredContent": {"datasetId": "OtherDataset", "offset": 0, "items": []}})
    with pytest.raises(ApifyError):
        await ApifyClient("token").get_items("Data123", limit=1)


def test_normalization_filters_utc_window_dedupes_and_keeps_real_media_types():
    items = [
        {"url": "https://instagram.com/reel/R1/", "type": "Video", "timestamp": "2026-09-17T05:00:00-07:00"},
        {"url": "https://www.instagram.com/reel/R1/", "type": "Video", "timestamp": "2026-09-17T12:00:00Z"},
        {"url": "https://instagram.com/p/P1/", "type": "Image", "timestamp": "2026-09-03T12:00:00Z"},
        {"url": "https://instagram.com/p/P2/", "type": "Sidecar", "timestamp": "2026-09-10T12:00:00Z"},
        {"url": "https://instagram.com/p/P3/", "type": "Video", "timestamp": "2026-09-10T12:00:00Z"},
        {"url": "https://instagram.com/p/Old/", "timestamp": "2026-09-03T11:59:59Z"},
        {"url": "https://instagram.com/p/Future/", "timestamp": "2026-09-17T12:00:01Z"},
        {"url": "https://instagram.com/p/Unknown/"},
    ]
    result = normalize_items(profile(), items)
    assert [item["media_type"] for item in result["items"]] == ["reel", "image", "carousel", "video"]
    coverage = result["coverage"]
    assert coverage["duplicate_count"] == 1
    assert coverage["out_of_window_count"] == 2
    assert coverage["missing_timestamp_count"] == 1
    assert coverage["complete"] is False
    assert coverage["status"] == "unknown"
    assert "missing_timestamps" in coverage["reasons"]


def test_coverage_never_claims_complete_and_marks_known_partial_evidence():
    assert normalize_items(profile(), [])["coverage"]["status"] == "unknown"
    result = normalize_items(profile(results_limit=2), [{"error": "sensitive raw message"}, "malformed"])
    assert result["items"] == []
    assert result["coverage"]["status"] == "partial"
    assert set(result["coverage"]["reasons"]) == {
        "result_limit_reached",
        "provider_errors",
        "invalid_items",
        "provider_completeness_unverified",
    }
    assert "sensitive" not in json.dumps(result)


def test_search_normalizes_candidates_without_copying_unrelated_private_fields():
    spec = prepare_collection({"mode": "search", "query": "example"}, now=NOW)
    result = normalize_items(
        spec,
        [
            {"username": "Example", "fullName": "Example Creator", "private": True, "api_key": "secret"},
            {"username": "example"},
            {"username": "../bad"},
        ],
    )
    assert len(result["items"]) == 1
    assert result["items"][0]["profile_url"] == "https://www.instagram.com/example/"
    assert result["items"][0]["is_private"] is True
    assert result["coverage"]["duplicate_count"] == 1
    assert "secret" not in json.dumps(result)


def test_reel_retains_missing_timestamp_without_calling_it_current():
    result = normalize_items(
        reel(),
        [
            {
                "url": "https://instagram.com/reel/Abc_123-/",
                "type": "Video",
                "transcript": "Actual transcript",
                "downloadedVideo": "https://media.example/video.mp4",
                "videoUrl": "javascript:alert(1)",
            }
        ],
    )
    item = result["items"][0]
    assert item["published_at"] is None
    assert item["transcript"] == "Actual transcript"
    assert item["downloaded_video_url"] == "https://media.example/video.mp4"
    assert item["video_url"] is None
    assert result["coverage"]["complete"] is False
    assert "result_limit_reached" not in result["coverage"]["reasons"]


def test_selected_reel_cannot_return_a_different_reel():
    result = normalize_items(reel(), [{"url": "https://instagram.com/reel/Unrelated/"}])
    assert result["items"] == []
    assert result["coverage"]["invalid_item_count"] == 1


@pytest.mark.parametrize("limit", [0, -1, 201, True, 2.5, "10"])
@pytest.mark.asyncio
async def test_dataset_limits_fail_before_http(monkeypatch, limit):
    requests, _ = install_http(monkeypatch, lambda request: pytest.fail("Unexpected HTTP"))
    with pytest.raises(ApifyError):
        await ApifyClient("token").get_items("Data123", limit=limit)
    assert not requests
