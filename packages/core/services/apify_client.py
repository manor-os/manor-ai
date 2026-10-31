"""Bounded Instagram collection through reviewed Apify Actors.

This module owns input and official MCP contracts only. The calling service owns
authorization, durable run identity, credit admission, and reconciliation.
Starting a run is deliberately never retried here.

Provider contracts: https://github.com/apify/apify-mcp-server/tree/master/src/tools
and https://apify.com/apify/instagram-reel-scraper/input-schema.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from packages.core.ai.mcp._remote import RemoteMCPClient, RemoteMCPError


APIFY_MCP_ENDPOINT = "https://mcp.apify.com?tools=call-actor,get-actor-run,get-dataset-items,abort-actor-run"
_SCRAPER = "apify~instagram-scraper"
_REELS = "apify~instagram-reel-scraper"
_ID = re.compile(r"[A-Za-z0-9]{1,64}\Z")
_HANDLE = re.compile(r"[A-Za-z0-9_](?:[A-Za-z0-9_.]{0,28}[A-Za-z0-9_])?\Z")
_SHORTCODE = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_RESERVED = {
    "accounts",
    "about",
    "api",
    "developer",
    "direct",
    "explore",
    "legal",
    "p",
    "reel",
    "reels",
    "stories",
    "tv",
    "web",
}
_MAX_ITEMS = 200


class ApifyError(ValueError):
    """Safe error; uncertain means a start may already have created a run."""

    def __init__(self, code: str, message: str, uncertain: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.uncertain = uncertain


def _invalid(message: str) -> ApifyError:
    return ApifyError("invalid_arguments", message)


def _text(value: Any, *, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise _invalid(f"{field} must be a nonempty string of at most {maximum} characters.")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _invalid(f"{field} cannot contain control characters.")
    return value.strip()


def _integer(value: Any, *, field: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise _invalid(f"{field} must be an integer between 1 and {maximum}.")
    return value


def _boolean(value: Any, *, field: str) -> bool:
    if type(value) is not bool:
        raise _invalid(f"{field} must be a boolean.")
    return value


def _instagram_path(value: str) -> list[str]:
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise _invalid("Use a valid HTTPS Instagram URL.") from None
    # Exact netloc also excludes credentials, ports, IDNs, and trailing dots.
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"instagram.com", "www.instagram.com"}:
        raise _invalid("Use an HTTPS URL on instagram.com or www.instagram.com.")
    # Instagram's share links commonly carry tracking parameters. Discard
    # those when rebuilding the canonical URL; never forward them to Apify.
    if (
        any(
            key not in {"igsh", "igshid"} and not key.startswith("utm_")
            for key, _ in parse_qsl(parsed.query, keep_blank_values=True)
        )
        or parsed.fragment
        or "%" in parsed.path
        or "\\" in parsed.path
    ):
        raise _invalid("Instagram URLs cannot include non-tracking queries, fragments, or encoded paths.")
    path = parsed.path.removesuffix("/")
    return path.removeprefix("/").split("/")


def _profile_url(value: Any) -> str:
    value = _text(value, field="profile_url", maximum=250)
    if value.startswith("https://"):
        parts = _instagram_path(value)
        if len(parts) != 1:
            raise _invalid("Provide one Instagram profile URL.")
        handle = parts[0]
    else:
        handle = value.removeprefix("@")
    if not _HANDLE.fullmatch(handle) or ".." in handle or handle.lower() in _RESERVED:
        raise _invalid("Provide a valid Instagram handle or profile URL.")
    return f"https://www.instagram.com/{handle.lower()}/"


def _reel_url(value: Any) -> str:
    value = _text(value, field="reel_url", maximum=250)
    parts = _instagram_path(value)
    if len(parts) != 2 or parts[0] != "reel" or not _SHORTCODE.fullmatch(parts[1]):
        raise _invalid("Provide one Instagram URL in the form /reel/SHORTCODE/.")
    return f"https://www.instagram.com/reel/{parts[1]}/"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def prepare_collection(arguments: dict, *, now: datetime) -> dict:
    """Create an allowlisted, JSON-serializable Actor request from user inputs."""
    if not isinstance(arguments, dict):
        raise _invalid("Collection arguments must be an object.")
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        raise _invalid("Collection time must include a timezone.")
    mode = arguments.get("mode")
    if not isinstance(mode, str) or mode not in {"search", "profile", "reel"}:
        raise _invalid("mode must be search, profile, or reel.")
    allowed = {"mode", "results_limit"} | {
        "search": {"query"},
        "profile": {"profile_url", "lookback_days"},
        "reel": {"reel_url", "include_transcript", "include_downloaded_video"},
    }[mode]
    if set(arguments) - allowed:
        raise _invalid("Collection contains unsupported fields for its mode.")
    maximum = {"search": 20, "profile": _MAX_ITEMS, "reel": 1}[mode]
    default = {"search": 10, "profile": 50, "reel": 1}[mode]
    limit = _integer(arguments.get("results_limit", default), field="results_limit", maximum=maximum)
    window = None
    actor_id = _SCRAPER
    if mode == "search":
        query = _text(arguments.get("query"), field="query", maximum=100)
        actor_input = {
            "search": query,
            "searchType": "user",
            "searchLimit": limit,
            "resultsType": "details",
            "resultsLimit": 1,
        }
    elif mode == "profile":
        days = arguments.get("lookback_days", 14)
        if type(days) is not int or days not in {7, 14, 28}:
            raise _invalid("lookback_days must be 7, 14, or 28.")
        end = now.astimezone(timezone.utc)
        window = {"start": _iso(end - timedelta(days=days)), "end": _iso(end)}
        actor_input = {
            "directUrls": [_profile_url(arguments.get("profile_url"))],
            "resultsType": "posts",
            "resultsLimit": limit,
            "onlyPostsNewerThan": window["start"],
            "addParentData": False,
        }
    else:
        actor_id = _REELS
        actor_input = {
            "username": [_reel_url(arguments.get("reel_url"))],
            "resultsLimit": 1,
            "includeTranscript": _boolean(arguments.get("include_transcript", False), field="include_transcript"),
            "includeDownloadedVideo": _boolean(
                arguments.get("include_downloaded_video", False), field="include_downloaded_video"
            ),
            "includeSharesCount": False,
        }
    return {"mode": mode, "actor_id": actor_id, "actor_input": actor_input, "results_limit": limit, "window": window}


def _date(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None
    except (ValueError, OverflowError):
        return None


def _validated_spec(spec: Any) -> dict:
    """Reconstruct the public input so forged specs cannot widen the Actor call."""
    if not isinstance(spec, dict) or not isinstance(spec.get("actor_input"), dict):
        raise _invalid("A prepared collection specification is required.")
    actor_input = spec["actor_input"]
    mode = spec.get("mode")
    args = {"mode": mode, "results_limit": spec.get("results_limit")}
    now = datetime.now(timezone.utc)
    if mode == "search":
        args["query"] = actor_input.get("search")
    elif mode == "profile":
        urls = actor_input.get("directUrls")
        window = spec.get("window")
        if not isinstance(urls, list) or len(urls) != 1 or not isinstance(window, dict):
            raise _invalid("The collection must contain one profile and a UTC window.")
        start, end = _date(window.get("start")), _date(window.get("end"))
        if start is None or end is None:
            raise _invalid("The collection window must include valid UTC dates.")
        span = end - start
        if span not in {timedelta(days=7), timedelta(days=14), timedelta(days=28)}:
            raise _invalid("The collection window must span 7, 14, or 28 days.")
        args.update(profile_url=urls[0], lookback_days=span.days)
        now = end
    elif mode == "reel":
        urls = actor_input.get("username")
        if not isinstance(urls, list) or len(urls) != 1:
            raise _invalid("The collection must contain one Reel URL.")
        args.update(
            reel_url=urls[0],
            include_transcript=actor_input.get("includeTranscript"),
            include_downloaded_video=actor_input.get("includeDownloadedVideo"),
        )
    canonical = prepare_collection(args, now=now)
    if canonical != spec:
        raise _invalid("Collection specification does not match the reviewed Actor contract.")
    return canonical


def _resource_id(value: Any) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise _invalid("Provider resource identity is invalid.")
    return value


class ApifyClient:
    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not token.strip() or any(ord(c) < 33 or ord(c) > 126 for c in token.strip()):
            raise ApifyError("credentials_unavailable", "A valid Apify API token is required.")
        self._token = token.strip()

    async def _call(self, tool: str, arguments: dict, *, starting: bool = False) -> dict:
        client = RemoteMCPClient(APIFY_MCP_ENDPOINT, self._token, timeout=30.0, token_in="header")
        # A failed handshake cannot have started an Actor. Separate it from the
        # one tools/call request so the ledger can release an unspent admission.
        try:
            await client.initialize()
        except Exception:
            raise ApifyError("provider_unavailable", "Apify MCP connection could not be established.") from None
        try:
            payload = await client.call_tool(tool, arguments, timeout=30.0)
        except RemoteMCPError as exc:
            rejected = exc.code in {-32001, -32601, -32602, 400, 401, 403, 405, 413, 415, 422, 429}
            raise ApifyError(
                "provider_request_failed",
                "Apify MCP did not confirm the operation.",
                uncertain=starting and not rejected,
            ) from None
        except Exception:
            raise ApifyError(
                "provider_unavailable",
                "Apify MCP did not return a confirmed response.",
                uncertain=starting,
            ) from None
        if starting and isinstance(payload, dict) and payload.get("isError"):
            data = self._structured(payload, starting=True)
            run_id = data.get("runId")
            if isinstance(run_id, str) and _ID.fullmatch(run_id):
                # An error plus a receipt still proves there is a run to save.
                return payload
        if not isinstance(payload, dict) or payload.get("isError"):
            # Official call-actor can return a tool error after start succeeded
            # (e.g. enrichment failed). Its text alone never proves zero spend.
            raise ApifyError(
                "provider_request_failed",
                "Apify MCP could not confirm the operation outcome.",
                uncertain=starting,
            )
        return payload

    @staticmethod
    def _structured(payload: dict, *, starting: bool = False) -> dict:
        data = payload.get("structuredContent")
        if data is None:
            # The official server mirrors structuredContent in content[0].
            content = payload.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                first = content[0]
                if first.get("type") == "text" and isinstance(first.get("text"), str):
                    try:
                        data = json.loads(first["text"])
                    except (ValueError, UnicodeError):
                        pass
        if not isinstance(data, dict):
            raise ApifyError(
                "invalid_provider_response",
                "Apify MCP returned an invalid structured response.",
                uncertain=starting,
            )
        return data

    @classmethod
    def _run(cls, payload: dict, *, starting: bool = False, expected_id: str | None = None) -> dict:
        data = cls._structured(payload, starting=starting)
        run_id = data.get("runId")
        if not isinstance(run_id, str) or not _ID.fullmatch(run_id) or (expected_id and run_id != expected_id):
            raise ApifyError("invalid_provider_response", "Apify returned an invalid run identity.", uncertain=starting)
        # Preserve a valid start receipt immediately. The collection service
        # durably binds id before validating other fields or settling costs.
        run = {"id": run_id, "status": data.get("status")}
        storages = data.get("storages")
        datasets = storages.get("datasets") if isinstance(storages, dict) else None
        dataset = datasets.get("default") if isinstance(datasets, dict) else None
        if isinstance(dataset, dict) and "id" in dataset:
            run["defaultDatasetId"] = dataset["id"]
        # get-actor-run exposes authoritative run usage in namespaced MCP
        # metadata, not structuredContent. Missing cost must never become zero.
        meta = payload.get("_meta")
        usage = meta.get("com.apify/ActorRun") if isinstance(meta, dict) else None
        if isinstance(usage, dict) and any(usage[key] != run_id for key in ("id", "runId") if key in usage):
            if not starting:
                raise ApifyError("invalid_provider_response", "Apify returned a conflicting usage identity.")
            usage = None  # Preserve the start receipt; reconcile cost separately.
        if not payload.get("isError") and isinstance(usage, dict) and "usageTotalUsd" in usage:
            run["usageTotalUsd"] = usage["usageTotalUsd"]
        return run

    async def start(self, spec: dict, *, max_cost_usd: str) -> dict:
        spec = _validated_spec(spec)
        try:
            if not isinstance(max_cost_usd, str) or len(max_cost_usd) > 32:
                raise InvalidOperation
            cap = Decimal(max_cost_usd)
            if not cap.is_finite() or cap <= 0 or cap > 5 or cap != cap.quantize(Decimal("0.000000001")):
                raise InvalidOperation
        except (InvalidOperation, ValueError):
            raise _invalid(
                "max_cost_usd must be a positive decimal up to 5 USD with at most nine decimal places."
            ) from None
        # MCP requires a JSON number. Round the binary float toward zero when
        # necessary, so neither its value nor JSON spelling exceeds the quote.
        numeric_cap = float(cap)
        if Decimal.from_float(numeric_cap) > cap or Decimal(str(numeric_cap)) > cap:
            numeric_cap = math.nextafter(numeric_cap, 0.0)
        payload = await self._call(
            "call-actor",
            {
                "actor": spec["actor_id"].replace("~", "/"),
                "input": spec["actor_input"],
                "waitSecs": 0,
                "callOptions": {"timeout": 300, "maxItems": spec["results_limit"], "maxTotalChargeUsd": numeric_cap},
            },
            starting=True,
        )
        # No follow-up request here: first persist the run identity even when
        # status/cost enrichment is incomplete or temporarily unavailable.
        return self._run(payload, starting=True)

    async def get_run(self, run_id: str) -> dict:
        run_id = _resource_id(run_id)
        return self._run(await self._call("get-actor-run", {"runId": run_id, "waitSecs": 0}), expected_id=run_id)

    async def abort_run(self, run_id: str) -> dict:
        run_id = _resource_id(run_id)
        # Abort acknowledges the request, but currently omits usage metadata.
        # Confirm the actual state and authoritative cost using get-actor-run.
        self._run(await self._call("abort-actor-run", {"runId": run_id, "gracefully": False}), expected_id=run_id)
        return await self.get_run(run_id)

    async def get_items(self, dataset_id: str, *, limit: int) -> list:
        dataset_id = _resource_id(dataset_id)
        limit = _integer(limit, field="limit", maximum=_MAX_ITEMS)
        data = self._structured(
            await self._call(
                "get-dataset-items",
                {"datasetId": dataset_id, "limit": limit, "offset": 0, "clean": False},
            )
        )
        items = data.get("items")
        if (
            data.get("datasetId") != dataset_id
            or data.get("offset") != 0
            or not isinstance(items, list)
            or len(items) > limit
        ):
            raise ApifyError("invalid_provider_response", "Apify returned an invalid dataset page.")
        return items


def _bounded_text(value: Any, maximum: int = 1000) -> str | None:
    return value[:maximum] if isinstance(value, str) else None


def _https_url(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 4096 or any(ord(c) < 32 for c in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password:
            return value
    except ValueError:
        pass
    return None


def _media_type(item: dict, permalink: str | None) -> str:
    kind = str(item.get("type") or item.get("__typename") or "").lower()
    if kind in {"sidecar", "graphsidecar", "carousel", "carousel_album"}:
        return "carousel"
    if str(item.get("productType") or item.get("product_type") or "").lower() == "clips" or (
        permalink and "/reel/" in permalink
    ):
        return "reel"
    if kind in {"video", "graphvideo"}:
        return "video"
    if kind in {"image", "graphimage", "photo"}:
        return "image"
    return "unknown"


def _public_count(value: Any) -> int | None:
    """A displayed engagement counter; hidden (-1) or missing stays unknown, never zero."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value < 0 or value != int(value) or value > 10**12:
        return None
    return int(value)


def normalize_items(spec: dict, items: list) -> dict:
    """Filter/dedupe evidence without ever claiming provider completeness."""
    spec = _validated_spec(spec)
    if not isinstance(items, list):
        raise _invalid("Dataset items must be an array.")
    limit = spec["results_limit"]
    coverage = {
        "status": "unknown",
        "complete": False,
        "reasons": [],
        "window": spec["window"],
        "received_count": len(items),
        "returned_count": 0,
        "out_of_window_count": 0,
        "missing_timestamp_count": 0,
        "duplicate_count": 0,
        "provider_error_count": 0,
        "invalid_item_count": 0,
    }
    reasons: list[str] = coverage["reasons"]
    if len(items) > limit or (spec["mode"] != "reel" and len(items) == limit):
        reasons.append("result_limit_reached")
    normalized: list[dict] = []
    seen: set[str] = set()
    window = spec["window"]
    start = _date(window["start"]) if window else None
    end = _date(window["end"]) if window else None
    for item in items[:limit]:
        if not isinstance(item, dict):
            coverage["invalid_item_count"] += 1
            continue
        if item.get("error") or item.get("errorDescription") or item.get("errorMessage"):
            coverage["provider_error_count"] += 1
            continue
        if spec["mode"] == "search":
            try:
                profile = _profile_url(item.get("username"))
            except ApifyError:
                coverage["invalid_item_count"] += 1
                continue
            entry = {
                "source_id": _bounded_text(item.get("id"), 100),
                "profile_url": profile,
                "username": profile.rstrip("/").split("/")[-1],
                "full_name": _bounded_text(item.get("fullName"), 200),
                "biography": _bounded_text(item.get("biography")),
                "profile_picture_url": _https_url(item.get("profilePicUrlHD") or item.get("profilePicUrl")),
                "is_private": item.get("private") if type(item.get("private")) is bool else None,
                "media_type": "profile",
            }
            identity = profile
        else:
            permalink = _https_url(item.get("url"))
            shortcode = item.get("shortCode")
            if permalink is not None:
                try:
                    parts = _instagram_path(permalink)
                    if len(parts) != 2 or parts[0] not in {"p", "reel", "tv"} or not _SHORTCODE.fullmatch(parts[1]):
                        raise _invalid("Invalid media URL.")
                    permalink = f"https://www.instagram.com/{parts[0]}/{parts[1]}/"
                except ApifyError:
                    permalink = None
            if permalink is None and isinstance(shortcode, str) and _SHORTCODE.fullmatch(shortcode):
                prefix = (
                    "reel" if str(item.get("productType") or item.get("product_type") or "").lower() == "clips" else "p"
                )
                permalink = f"https://www.instagram.com/{prefix}/{shortcode}/"
            if permalink is None:
                coverage["invalid_item_count"] += 1
                continue
            if spec["mode"] == "reel":
                requested_shortcode = spec["actor_input"]["username"][0].rstrip("/").split("/")[-1]
                if permalink.rstrip("/").split("/")[-1] != requested_shortcode:
                    coverage["invalid_item_count"] += 1
                    continue
            timestamp = _date(item.get("timestamp"))
            if timestamp is None:
                coverage["missing_timestamp_count"] += 1
                if window:
                    continue
            if window and timestamp is not None and not (start <= timestamp <= end):
                coverage["out_of_window_count"] += 1
                continue
            entry = {
                "source_id": _bounded_text(item.get("id"), 100),
                "permalink": permalink,
                "shortcode": permalink.rstrip("/").split("/")[-1],
                "creator": _bounded_text(item.get("ownerUsername"), 100),
                "published_at": _iso(timestamp) if timestamp else None,
                "media_type": _media_type(item, permalink),
                "caption": _bounded_text(item.get("caption"), 20_000),
                # Counts as observed by the provider at collection time.
                "metrics": {
                    "likes": _public_count(item.get("likesCount")),
                    "comments": _public_count(item.get("commentsCount")),
                    "views": _public_count(item.get("videoViewCount")),
                    "plays": _public_count(item.get("videoPlayCount")),
                },
                "thumbnail_url": _https_url(item.get("displayUrl")),
                "video_url": _https_url(item.get("videoUrl")),
                "transcript": _bounded_text(item.get("transcript"), 50_000),
                "downloaded_video_url": _https_url(item.get("downloadedVideo")),
            }
            identity = entry["shortcode"]
        if identity in seen:
            coverage["duplicate_count"] += 1
            continue
        seen.add(identity)
        normalized.append(entry)
    for key, reason in (
        ("provider_error_count", "provider_errors"),
        ("invalid_item_count", "invalid_items"),
        ("missing_timestamp_count", "missing_timestamps"),
    ):
        if coverage[key]:
            reasons.append(reason)
    if any(reason in reasons for reason in ("result_limit_reached", "provider_errors", "invalid_items")):
        coverage["status"] = "partial"
    reasons.append("provider_completeness_unverified")
    coverage["returned_count"] = len(normalized)
    return {"items": normalized, "coverage": coverage}
