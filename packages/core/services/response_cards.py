"""Read-only cards an agent can show inside a Chat reply.

They travel as response surface templates and are drawn natively by the client,
so every value here is untrusted model output: bounded, typed, and with links
limited to the web and Manor's own file addresses.
"""
from __future__ import annotations

import math
import re
from typing import Any

RESPONSE_CARD_TEMPLATE_IDS = frozenset({
    "card.chart",
    "card.comment",
    "card.delivery",
    "card.email",
    "card.event",
    "card.map",
    "card.person",
    "card.post",
    "card.product",
    "card.search_results",
    "card.stock",
})

_LINK = re.compile(r"^(?:https?://[^\s<>\"']+|/viewer/[0-9A-Za-z_-]+|/api/v1/fs/[^\s<>\"']+)$")
_EMAIL_STATUSES = frozenset({"received", "draft"})
_DELIVERY_STATUSES = frozenset({"sent", "published", "scheduled", "failed"})
_POST_STATUSES = frozenset({"draft", "scheduled", "published", "failed"})
_POST_FORMATS = frozenset({"vertical_video", "horizontal_video", "square_image", "portrait_image", "text"})
_COMMENT_STATUSES = frozenset({"draft", "scheduled", "sent", "failed"})
_CHART_TYPES = frozenset({"line", "bar", "share", "ranking"})
_MAX_CATEGORIES = {"line": 24, "bar": 24, "share": 8, "ranking": 12}
_EVENT_STATUSES = frozenset({"proposed", "confirmed", "cancelled"})
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class _Reader:
    """Reads one object's fields, recording every problem instead of stopping at the first."""

    def __init__(self, value: Any, path: str, errors: list[dict[str, str]]) -> None:
        self.path = path
        self.errors = errors
        self.value = value if isinstance(value, dict) else {}
        if not isinstance(value, dict):
            self.fail("object", f"{path} must be an object.")

    def fail(self, code: str, message: str, path: str | None = None) -> None:
        self.errors.append({"code": code, "message": message, "path": path or self.path})

    def text(self, key: str, maximum: int, *, required: bool = False) -> str:
        raw = self.value.get(key)
        path = f"{self.path}.{key}"
        if raw is None or raw == "":
            if required:
                self.fail("required", f"{path} is required.", path)
            return ""
        if not isinstance(raw, str):
            self.fail("string", f"{path} must be a string.", path)
            return ""
        text = raw.strip()
        if required and not text:
            self.fail("required", f"{path} is required.", path)
        if len(text) > maximum:
            self.fail("length", f"{path} exceeds {maximum} characters.", path)
        return text[:maximum]

    def choice(self, key: str, allowed: frozenset[str], *, default: str | None = None) -> str:
        raw = self.value.get(key)
        path = f"{self.path}.{key}"
        if raw is None and default is not None:
            return default
        if not isinstance(raw, str) or raw not in allowed:
            self.fail("choice", f"{path} must be one of {', '.join(sorted(allowed))}.", path)
            return default or ""
        return raw

    def link(self, key: str) -> str:
        link = self.text(key, 2_000)
        if link and not _LINK.fullmatch(link):
            path = f"{self.path}.{key}"
            self.fail("link", f"{path} must be an http(s) link or a Manor file address.", path)
            return ""
        return link

    def count(self, key: str) -> int | None:
        raw = self.value.get(key)
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            path = f"{self.path}.{key}"
            self.fail("count", f"{path} must be a non-negative whole number.", path)
            return None
        return raw

    def items(self, key: str, minimum: int, maximum: int) -> list[Any]:
        raw = self.value.get(key)
        path = f"{self.path}.{key}"
        if raw is None and minimum == 0:
            return []
        if not isinstance(raw, list) or not minimum <= len(raw) <= maximum:
            self.fail("items", f"{path} must hold {minimum}-{maximum} items.", path)
            return []
        return raw


def _compact(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value not in ("", None, [])}


def _email(reader: _Reader) -> dict[str, Any]:
    recipients = []
    for index, raw in enumerate(reader.items("to", 0, 10)):
        if isinstance(raw, str) and raw.strip() and len(raw) <= 160:
            recipients.append(raw.strip())
        else:
            reader.fail("recipient", "Recipients must be short text.", f"{reader.path}.to[{index}]")
    attachments = []
    for index, raw in enumerate(reader.items("attachments", 0, 10)):
        item = _Reader(raw, f"{reader.path}.attachments[{index}]", reader.errors)
        attachments.append(_compact({"name": item.text("name", 160, required=True), "url": item.link("url")}))
    props = _compact({
        "status": reader.choice("status", _EMAIL_STATUSES, default="received"),
        "from_name": reader.text("from_name", 160),
        "from_address": reader.text("from_address", 254),
        "to": recipients,
        "subject": reader.text("subject", 300, required=True),
        "sent_at": reader.text("sent_at", 40),
        "snippet": reader.text("snippet", 600),
        "body": reader.text("body", 6_000),
        "attachments": attachments,
        "url": reader.link("url"),
    })
    if not props.get("snippet") and not props.get("body"):
        reader.fail("email_content", "An email card needs a snippet or a body.")
    return props


def _delivery(reader: _Reader) -> dict[str, Any]:
    items = []
    for index, raw in enumerate(reader.items("items", 1, 20)):
        item = _Reader(raw, f"{reader.path}.items[{index}]", reader.errors)
        items.append(_compact({
            "label": item.text("label", 120, required=True),
            "status": item.choice("status", _DELIVERY_STATUSES),
            "count": item.count("count"),
            "detail": item.text("detail", 160),
            "url": item.link("url"),
        }))
    return {"items": items}


def _search_results(reader: _Reader) -> dict[str, Any]:
    results = []
    for index, raw in enumerate(reader.items("results", 1, 10)):
        item = _Reader(raw, f"{reader.path}.results[{index}]", reader.errors)
        results.append(_compact({
            "title": item.text("title", 200, required=True),
            "source": item.text("source", 120, required=True),
            "snippet": item.text("snippet", 300),
            "when": item.text("when", 40),
            "url": item.link("url"),
        }))
    total = reader.count("total")
    if total is not None and total < len(results):
        reader.fail("total", "total cannot be smaller than the results shown.", f"{reader.path}.total")
    return _compact({"results": results, "total": total})


def _post(reader: _Reader) -> dict[str, Any]:
    posts = []
    for index, raw in enumerate(reader.items("posts", 1, 6)):
        item = _Reader(raw, f"{reader.path}.posts[{index}]", reader.errors)
        media = None
        if item.value.get("media") is not None:
            media_reader = _Reader(item.value.get("media"), f"{item.path}.media", reader.errors)
            media = _compact({"name": media_reader.text("name", 160, required=True), "url": media_reader.link("url")})
        images = []
        for image_index, raw_image in enumerate(item.items("images", 0, 20)):
            image = _Reader(raw_image, f"{item.path}.images[{image_index}]", reader.errors)
            images.append(_compact({"name": image.text("name", 160, required=True), "url": image.link("url")}))
        metrics = []
        for metric_index, raw_metric in enumerate(item.items("metrics", 0, 4)):
            metric = _Reader(raw_metric, f"{item.path}.metrics[{metric_index}]", reader.errors)
            metrics.append({"label": metric.text("label", 24, required=True), "value": metric.text("value", 24, required=True)})
        posts.append(_compact({
            "platform": item.text("platform", 40, required=True),
            "account": item.text("account", 80),
            "format": item.choice("format", _POST_FORMATS, default="text"),
            "title": item.text("title", 200),
            "text": item.text("text", 2_000),
            "media": media,
            "images": images,
            "status": item.choice("status", _POST_STATUSES),
            "status_detail": item.text("status_detail", 160),
            "metrics": metrics,
            "url": item.link("url"),
        }))
    return {"posts": posts}


def _comment(reader: _Reader) -> dict[str, Any]:
    comments = []
    for index, raw in enumerate(reader.items("comments", 1, 20)):
        item = _Reader(raw, f"{reader.path}.comments[{index}]", reader.errors)
        reply = None
        if item.value.get("reply") is not None:
            reply_reader = _Reader(item.value.get("reply"), f"{item.path}.reply", reader.errors)
            reply = _compact({
                "author": reply_reader.text("author", 80),
                "text": reply_reader.text("text", 1_000, required=True),
                "status": reply_reader.choice("status", _COMMENT_STATUSES),
            })
        status = item.choice("status", _COMMENT_STATUSES) if item.value.get("status") is not None else ""
        comments.append(_compact({
            "author": item.text("author", 80, required=True),
            "text": item.text("text", 1_000, required=True),
            "when": item.text("when", 40),
            "likes": item.count("likes"),
            "status": status,
            "url": item.link("url"),
            "reply": reply,
        }))
    total = reader.count("total")
    if total is not None and total < len(comments):
        reader.fail("total", "total cannot be smaller than the comments shown.", f"{reader.path}.total")
    return _compact({
        "platform": reader.text("platform", 40, required=True),
        "account": reader.text("account", 80),
        "post_title": reader.text("post_title", 200),
        "post_url": reader.link("post_url"),
        "comments": comments,
        "total": total,
    })


def _number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _person(reader: _Reader) -> dict[str, Any]:
    return _compact({
        "name": reader.text("name", 120, required=True),
        "title": reader.text("title", 120),
        "organization": reader.text("organization", 120),
        "email": reader.text("email", 254),
        "phone": reader.text("phone", 40),
        "note": reader.text("note", 300),
        "url": reader.link("url"),
    })


def _event(reader: _Reader) -> dict[str, Any]:
    events = []
    for index, raw in enumerate(reader.items("events", 1, 8)):
        item = _Reader(raw, f"{reader.path}.events[{index}]", reader.errors)
        date = item.text("date", 10, required=True)
        if date and not _DATE.fullmatch(date):
            item.fail("date", "date must be YYYY-MM-DD.", f"{item.path}.date")
        attendees = []
        for attendee_index, attendee in enumerate(item.items("attendees", 0, 20)):
            if isinstance(attendee, str) and attendee.strip() and len(attendee) <= 80:
                attendees.append(attendee.strip())
            else:
                item.fail("attendee", "Attendees must be short text.", f"{item.path}.attendees[{attendee_index}]")
        events.append(_compact({
            "title": item.text("title", 200, required=True),
            "date": date,
            "time": item.text("time", 40),
            "location": item.text("location", 200),
            "attendees": attendees,
            "status": item.choice("status", _EVENT_STATUSES, default="confirmed"),
            "url": item.link("url"),
        }))
    return {"events": events}


def _stock(reader: _Reader) -> dict[str, Any]:
    def number(key: str, *, required: bool = False) -> float | None:
        raw = reader.value.get(key)
        if raw is None and not required:
            return None
        if not _number(raw):
            reader.fail("number", f"{reader.path}.{key} must be a finite number.", f"{reader.path}.{key}")
            return None
        return raw

    history = reader.items("history", 0, 240)
    if history and (len(history) < 2 or not all(_number(value) for value in history)):
        reader.fail("history", "history needs at least two finite numbers.", f"{reader.path}.history")
        history = []
    return _compact({
        "name": reader.text("name", 120, required=True),
        "symbol": reader.text("symbol", 20, required=True),
        "price": number("price", required=True),
        "currency": reader.text("currency", 8),
        "change": number("change"),
        "change_percent": number("change_percent"),
        "as_of": reader.text("as_of", 60),
        "history": history,
        "url": reader.link("url"),
    })


def _product(reader: _Reader) -> dict[str, Any]:
    products = []
    for index, raw in enumerate(reader.items("products", 1, 6)):
        item = _Reader(raw, f"{reader.path}.products[{index}]", reader.errors)
        image = None
        if item.value.get("image") is not None:
            image_reader = _Reader(item.value.get("image"), f"{item.path}.image", reader.errors)
            image = _compact({"name": image_reader.text("name", 160, required=True), "url": image_reader.link("url")})
        products.append(_compact({
            "name": item.text("name", 200, required=True),
            "price": item.text("price", 40),
            "seller": item.text("seller", 120),
            "detail": item.text("detail", 160),
            "image": image,
            "url": item.link("url"),
        }))
    return {"products": products}


def _map(reader: _Reader) -> dict[str, Any]:
    places = []
    for index, raw in enumerate(reader.items("places", 1, 12)):
        item = _Reader(raw, f"{reader.path}.places[{index}]", reader.errors)
        lat, lng = item.value.get("lat"), item.value.get("lng")
        located = _number(lat) and _number(lng) and -90 <= lat <= 90 and -180 <= lng <= 180
        if not located and (lat is not None or lng is not None):
            item.fail("coordinates", "lat and lng must both be valid coordinates.", item.path)
        places.append(_compact({
            "name": item.text("name", 160, required=True),
            "address": item.text("address", 240),
            "detail": item.text("detail", 160),
            "lat": lat if located else None,
            "lng": lng if located else None,
            "url": item.link("url"),
        }))
    return {"places": places}


def _chart(reader: _Reader) -> dict[str, Any]:
    chart_type = reader.choice("chart_type", _CHART_TYPES)
    limit = _MAX_CATEGORIES.get(chart_type, 24)
    categories = []
    for index, raw in enumerate(reader.items("categories", 1, limit)):
        if isinstance(raw, str) and raw.strip() and len(raw) <= 40:
            categories.append(raw.strip())
        else:
            reader.fail("category", "Categories must be short text.", f"{reader.path}.categories[{index}]")
    # A chart with more series than this stops being readable; split it instead.
    series_limit = 3 if chart_type == "line" else 1
    series = []
    for index, raw in enumerate(reader.items("series", 1, series_limit)):
        item = _Reader(raw, f"{reader.path}.series[{index}]", reader.errors)
        values = item.items("values", 1, 24)
        if len(values) != len(categories) or not all(_number(value) for value in values):
            item.fail("values", "A series needs one finite number per category.", f"{item.path}.values")
            values = []
        if chart_type == "share" and any(_number(value) and value < 0 for value in values):
            item.fail("values", "Shares cannot be negative.", f"{item.path}.values")
        series.append({"name": item.text("name", 40, required=True), "values": values})
    reference = None
    if reader.value.get("reference") is not None:
        line = _Reader(reader.value.get("reference"), f"{reader.path}.reference", reader.errors)
        if not _number(line.value.get("value")):
            line.fail("value", "A reference line needs a finite value.", f"{line.path}.value")
        reference = {"label": line.text("label", 40, required=True), "value": line.value.get("value")}
    return _compact({
        "chart_type": chart_type,
        "categories": categories,
        "series": series,
        "unit": reader.text("unit", 12),
        "reference": reference,
    })


_NORMALIZERS = {
    "card.chart": _chart,
    "card.comment": _comment,
    "card.delivery": _delivery,
    "card.email": _email,
    "card.event": _event,
    "card.map": _map,
    "card.person": _person,
    "card.post": _post,
    "card.product": _product,
    "card.search_results": _search_results,
    "card.stock": _stock,
}


def normalize_response_card(template_id: str, props: Any, errors: list[dict[str, str]]) -> dict[str, Any]:
    return _NORMALIZERS[template_id](_Reader(props, "render.props", errors))
