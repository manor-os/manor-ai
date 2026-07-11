"""Best-effort step output repair before JSON Schema validation.

Workers and agent loops can produce the right artifact or answer while still
missing the exact JSON envelope a plan step requested. Keep the dispatcher
strict at the lease boundary, but allow a small, deterministic repair pass for
common shapes like fenced JSON, generated file references, draft counts, and
note titles.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import posixpath
import re
from typing import Any


_TEXT_KEYS = (
    "text",
    "value",
    "result",
    "content",
    "output",
    "answer",
    "final",
    "message",
)
_ARRAY_UNWRAP_KEYS = ("result", "items", "data", "records", "rows")
_FILE_LIST_KEYS = ("files", "artifacts", "documents", "images")
_FILE_PATH_KEYS = ("path", "fs_path", "saved_to", "file_path", "filename")
_FILE_URL_KEYS = ("file_url", "document_url", "image_url", "video_url", "result_url", "url")
_REFERENCE_ONLY_KEYS = {
    "context",
    "sources",
    "source_count",
    "scope",
    "groups",
    "knowledge_nets",
    "entries",
    "matches",
}
_PATH_RE = re.compile(
    r"(?P<path>(?:[\w\u4e00-\u9fff .&()@+-]+/)+"
    r"[\w\u4e00-\u9fff .&()@+-]+\."
    r"(?:md|txt|json|csv|html|docx|pptx|pdf|png|jpe?g|webp|mp4))",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)


def coerce_step_output_for_schema(schema: dict | None, result: Any) -> Any:
    """Return a schema-friendlier output without hiding real failures.

    The function only fills fields when the raw result contains concrete
    evidence. If a required field cannot be inferred, it stays missing and the
    normal JSON Schema validator will fail the step.
    """
    if not isinstance(schema, dict) or result is None:
        return result

    schema_type = _schema_type(schema)
    coerced = _parse_wrapped_json(result, schema=schema)

    if schema_type == "array":
        return _coerce_array_output(schema, coerced)
    if schema_type != "object":
        return coerced

    props = _schema_properties(schema)
    required = set(schema.get("required") or [])
    if isinstance(coerced, list):
        wrapped = _wrap_list_for_single_array_property(coerced, props, required)
        if wrapped is not None:
            coerced = wrapped

    if not isinstance(coerced, dict):
        coerced = {"text": str(coerced)}

    output = dict(coerced)

    _merge_parseable_text_payload(output, props, required)
    _coerce_existing_property_types(output, props)
    _infer_artifact_scalar_fields(output, props, required)
    _infer_files(output, props, required)
    _infer_summary(output, props, required)
    _infer_draft_count(output, props, required)
    _infer_named_count_fields(output, props, required)
    _infer_note_titles(output, props, required)
    _infer_product_angle(output, props, required)
    _infer_url_fields(output, props, required)
    _infer_social_publish_fields(output, props, required)
    _infer_markdown_fields(output, props, required)
    _infer_single_required_string_field(output, props, required)
    _infer_topic_lists(output, props, required)
    _infer_batch_summary(output, props, required)

    return _filter_additional_properties(schema, output)


def _schema_type(schema: dict) -> str | None:
    value = schema.get("type")
    if isinstance(value, str):
        return value
    if isinstance(value, list) and value:
        return str(value[0])
    return None


def _schema_properties(schema: dict) -> dict[str, Any]:
    props = schema.get("properties")
    return props if isinstance(props, dict) else {}


def _field_requested(name: str, props: dict[str, Any], required: set[str]) -> bool:
    return name in required or name in props


def _parse_wrapped_json(result: Any, *, schema: dict | None = None) -> Any:
    if isinstance(result, str):
        parsed = _parse_json_from_text(result, schema=schema)
        return parsed if parsed is not None else result

    if isinstance(result, dict):
        for key in _TEXT_KEYS:
            value = result.get(key)
            if not isinstance(value, str):
                continue
            parsed = _parse_json_from_text(value, schema=schema)
            if parsed is not None:
                return parsed

    return result


def _wrap_list_for_single_array_property(
    values: list[Any],
    props: dict[str, Any],
    required: set[str],
) -> dict[str, Any] | None:
    array_keys = [
        key
        for key in required
        if isinstance(props.get(key), dict) and _schema_type(props[key]) == "array"
    ]
    if len(array_keys) != 1:
        return None
    return {array_keys[0]: values}


def _coerce_array_output(schema: dict, result: Any) -> Any:
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        for key in _ARRAY_UNWRAP_KEYS:
            value = result.get(key)
            if isinstance(value, list):
                return value
    return result


def _merge_parseable_text_payload(
    output: dict[str, Any],
    props: dict[str, Any],
    required: set[str],
) -> None:
    wanted = set(props) | required
    if wanted and required.issubset(output):
        return

    for key in _TEXT_KEYS:
        value = output.get(key)
        if not isinstance(value, str):
            continue
        parsed = _parse_json_from_text(
            value,
            schema={"type": "object", "required": list(required), "properties": props},
        )
        if not isinstance(parsed, dict):
            continue
        if wanted and not (wanted & set(parsed)):
            continue
        for parsed_key, parsed_value in parsed.items():
            output.setdefault(parsed_key, parsed_value)


def _coerce_existing_property_types(output: dict[str, Any], props: dict[str, Any]) -> None:
    for key, prop_schema in props.items():
        if key not in output or not isinstance(prop_schema, dict):
            continue
        prop_type = _schema_type(prop_schema)
        value = output[key]
        if prop_type == "string" and value is not None and not isinstance(value, str):
            output[key] = _stringify(value)
        elif prop_type == "integer" and not isinstance(value, int):
            parsed = _parse_int(value)
            if parsed is not None:
                output[key] = parsed
        elif prop_type == "array" and isinstance(value, str):
            parsed = _parse_json_from_text(value)
            if isinstance(parsed, list):
                output[key] = parsed


def _infer_artifact_scalar_fields(
    output: dict[str, Any],
    props: dict[str, Any],
    required: set[str],
) -> None:
    """Copy artifact evidence across common alias fields.

    Tool-backed workers do not all agree on whether a generated artifact is
    called `path`, `fs_path`, `url`, `result_url`, or a typed URL field. When a
    schema asks for one scalar artifact field, use only concrete values that
    already appeared in the result payload.
    """
    wanted = {
        "fs_path",
        "path",
        "file_path",
        "document_id",
        "file_url",
        "document_url",
        "image_url",
        "video_url",
        "result_url",
        "url",
    }
    if not any(_field_requested(name, props, required) for name in wanted):
        return

    refs = _collect_file_refs(output)
    if not refs:
        return

    for key, prop_schema in props.items():
        if key not in wanted or not _field_requested(key, props, required) or _has_value(output.get(key)):
            continue
        if not isinstance(prop_schema, dict) or _schema_type(prop_schema) != "string":
            continue

        value = _artifact_scalar_value_for_key(key, refs)
        if value:
            output[key] = value


def _fs_path_from_api_url(value: Any) -> str | None:
    """Workspace-relative fs path from a canonical ``/api/v1/fs/<entity>/<rel>``
    Manor file URL.

    These URLs serve an on-disk file whose entity-relative path is the suffix
    after the entity id, so the fs path is recoverable. Returns None for the
    ``/public/`` variant and for any other URL — so arbitrary URLs are never
    treated as filesystem paths.
    """
    if not isinstance(value, str):
        return None
    raw = value.split("?", 1)[0].strip().lstrip("/")
    prefix = "api/v1/fs/"
    if not raw.startswith(prefix):
        return None
    rest = raw[len(prefix):]
    if rest.startswith("public/"):
        return None
    _entity, _, rel = rest.partition("/")
    rel = rel.strip("/")
    return rel or None


def _artifact_scalar_value_for_key(key: str, refs: list[dict[str, str]]) -> str | None:
    if key in {"fs_path", "path", "file_path"}:
        for ref in refs:
            path = ref.get("path")
            if (
                path
                and not path.startswith(("http://", "https://"))
                and not path.startswith(("/api/", "api/"))
                and not (ref.get("url") == path)
            ):
                return path
        # Canonical /api/v1/fs/<entity>/<rel> URLs encode the fs path; recover it
        # rather than leaving fs_path missing (a real prod failure on mixed refs).
        for ref in refs:
            for candidate in (ref.get("path"), ref.get("url")):
                derived = _fs_path_from_api_url(candidate)
                if derived:
                    return derived
        return None

    if key == "document_id":
        for ref in refs:
            if ref.get("document_id"):
                return ref["document_id"]
        return None

    if key in {"file_url", "document_url"}:
        for ref in refs:
            if ref.get("url"):
                return ref["url"]
        for ref in refs:
            if ref.get("path"):
                return ref["path"]
        return None

    if key in {"image_url", "video_url", "result_url", "url"}:
        for ref in refs:
            if ref.get("url"):
                return ref["url"]
        return None

    return None


def _infer_files(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    if not _field_requested("files", props, required):
        return
    files_schema = props.get("files") if isinstance(props.get("files"), dict) else {}
    if isinstance(output.get("files"), list) and output["files"]:
        output["files"] = _format_file_refs_for_schema(
            _normalize_file_refs(output["files"]),
            files_schema,
        )
        return

    refs = _collect_file_refs(output)
    if refs:
        output["files"] = _format_file_refs_for_schema(refs, files_schema)


def _infer_summary(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    if not _field_requested("summary", props, required):
        return
    if isinstance(output.get("summary"), str) and output["summary"].strip():
        return

    text = _best_text(output)
    if text:
        output["summary"] = _truncate_summary(text)


def _infer_draft_count(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    if not _field_requested("draft_count", props, required):
        return
    existing = _parse_int(output.get("draft_count"))
    if existing is not None:
        output["draft_count"] = existing
        return

    for key in ("drafts", "x_drafts", "posts", "notes", "note_titles", "items"):
        value = output.get(key)
        if isinstance(value, list):
            output["draft_count"] = len(value)
            return

    text = _best_text(output)
    if not text:
        return

    explicit = re.search(
        r"(?:draft_count|draft count|草稿数量|笔记数量|篇数)\s*[:：=]\s*(\d+)",
        text,
        re.IGNORECASE,
    )
    if explicit:
        output["draft_count"] = int(explicit.group(1))
        return

    heading_matches = re.findall(
        r"(?:^|\n)\s*(?:#{1,4}\s*)?(?:draft|post|note|草稿|笔记)\s*[-#：:\s]*\d+",
        text,
        re.IGNORECASE,
    )
    if heading_matches:
        output["draft_count"] = len(heading_matches)
        return

    count_match = re.search(r"(\d+)\s*(?:drafts?|posts?|notes?|条|篇|个)\b", text, re.IGNORECASE)
    if count_match:
        output["draft_count"] = int(count_match.group(1))


def _infer_named_count_fields(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    text = _best_text(output)
    for key, prop_schema in props.items():
        if key == "draft_count" or key not in required:
            continue
        if not isinstance(prop_schema, dict) or _schema_type(prop_schema) != "integer":
            continue
        existing = _parse_int(output.get(key))
        if existing is not None:
            output[key] = existing
            continue
        if not text:
            continue
        labels = {
            key,
            key.replace("_", " "),
            key.replace("_", "-"),
        }
        match = re.search(
            rf"(?:{'|'.join(re.escape(label) for label in labels)})\s*[:：=]\s*(\d+)",
            text,
            re.IGNORECASE,
        )
        if match:
            output[key] = int(match.group(1))


def _infer_note_titles(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    if not _field_requested("note_titles", props, required):
        return
    titles = _string_list(output.get("note_titles"))
    if not titles:
        for key in ("titles", "notes", "drafts", "items"):
            titles = _titles_from_sequence(output.get(key))
            if titles:
                break

    if not titles:
        titles = _titles_from_text(_best_text(output))

    titles = _apply_array_bounds(titles, props.get("note_titles"))
    if titles:
        output["note_titles"] = titles


def _infer_product_angle(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    wanted = {"primary_angle_name", "value_proposition", "messaging_pillars"}
    if not any(_field_requested(name, props, required) for name in wanted):
        return

    text = _best_text(output)
    if not text:
        return

    if _field_requested("primary_angle_name", props, required) and not output.get("primary_angle_name"):
        value = _line_value(
            text,
            (
                "primary angle",
                "primary_angle_name",
                "angle name",
                "主推角度",
                "核心角度",
                "角度名称",
            ),
        )
        if value:
            output["primary_angle_name"] = value

    if _field_requested("value_proposition", props, required) and not output.get("value_proposition"):
        value = _line_value(
            text,
            (
                "value proposition",
                "value_proposition",
                "核心价值",
                "价值主张",
                "卖点",
            ),
        )
        if value:
            output["value_proposition"] = value

    if _field_requested("messaging_pillars", props, required) and not output.get("messaging_pillars"):
        pillars = _string_list(output.get("messaging_pillars"))
        if not pillars:
            pillars = _list_after_heading(
                text,
                (
                    "messaging pillars",
                    "messaging_pillars",
                    "内容支柱",
                    "传播支柱",
                    "信息支柱",
                ),
            )
        pillars = _apply_array_bounds(pillars, props.get("messaging_pillars"))
        if pillars:
            output["messaging_pillars"] = pillars


def _infer_markdown_fields(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    text = _best_text(output)
    if not _looks_like_markdown_document(text):
        return
    for key, prop_schema in props.items():
        if not _field_requested(key, props, required) or output.get(key):
            continue
        if not isinstance(prop_schema, dict) or _schema_type(prop_schema) != "string":
            continue
        if key.endswith("_markdown") or key in {
            "report_markdown",
            "research_report_markdown",
            "memory_entry_markdown",
            "strategy_log_entry_markdown",
            "drafts_document_markdown",
        } or key.endswith("_report"):
            output[key] = text


def _infer_url_fields(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    text = _best_text(output)
    if not text:
        return
    urls = [match.group(0).rstrip(".,;，。；)") for match in _URL_RE.finditer(text)]
    if not urls:
        return
    for key, prop_schema in props.items():
        if not _field_requested(key, props, required) or output.get(key):
            continue
        if not isinstance(prop_schema, dict) or _schema_type(prop_schema) != "string":
            continue
        if _looks_like_url_field(key):
            output[key] = urls[0]


def _infer_social_publish_fields(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    wanted = {
        "tweet_id",
        "published_at",
        "status",
        "post_url",
        "tweet_url",
        "post_text",
        "tweet_text",
    }
    if not any(_field_requested(name, props, required) for name in wanted):
        return

    payload = _tweet_publish_payload(output)
    if not payload:
        return

    tweet_id = _tweet_payload_value(payload, ("tweet_id", "id"))
    if not tweet_id:
        return

    if _field_requested("tweet_id", props, required) and not _has_value(output.get("tweet_id")):
        output["tweet_id"] = tweet_id

    status = _tweet_payload_value(payload, ("status",)) or _default_publish_status(props.get("status"))
    if _field_requested("status", props, required) and not _has_value(output.get("status")) and status:
        output["status"] = status

    published_at = _tweet_payload_value(payload, ("published_at", "created_at"))
    if (
        not published_at
        and "published_at" in required
        and _field_requested("published_at", props, required)
    ):
        published_at = _utc_now_iso()
    if _field_requested("published_at", props, required) and not _has_value(output.get("published_at")) and published_at:
        output["published_at"] = published_at

    post_text = _tweet_payload_value(payload, ("post_text", "tweet_text", "text"))
    for key in ("post_text", "tweet_text"):
        if _field_requested(key, props, required) and not _has_value(output.get(key)) and post_text:
            output[key] = post_text

    tweet_url = _tweet_payload_value(payload, ("tweet_url", "post_url", "url"))
    if not tweet_url:
        tweet_url = f"https://x.com/i/web/status/{tweet_id}"
    for key in ("tweet_url", "post_url"):
        if _field_requested(key, props, required) and not _has_value(output.get(key)):
            output[key] = tweet_url


def _tweet_publish_payload(output: dict[str, Any]) -> dict[str, Any] | None:
    payloads = [output]
    content = output.get("content")
    if isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                continue
            parsed = _parse_json_from_text(str(block.get("text") or ""))
            if isinstance(parsed, dict):
                payloads.append(parsed)
    for key in ("result", "response", "tool_result", "payload"):
        value = output.get(key)
        if isinstance(value, dict):
            payloads.append(value)

    for payload in payloads:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        tweet = payload.get("tweet") if isinstance(payload.get("tweet"), dict) else {}
        if _looks_like_tweet_publish_payload(payload, data, tweet):
            return payload
    return None


def _looks_like_tweet_publish_payload(
    payload: dict[str, Any],
    data: dict[str, Any],
    tweet: dict[str, Any],
) -> bool:
    if payload.get("tweet_id") or payload.get("tweet_url"):
        return True
    if data.get("edit_history_tweet_ids"):
        return True
    if data.get("id") and data.get("text") and payload.get("_simulated") is True:
        return True
    if tweet.get("id") and tweet.get("text"):
        return True
    return False


def _tweet_payload_value(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    candidates: list[dict[str, Any]] = [payload]
    for key in ("data", "tweet", "post", "result"):
        value = payload.get(key)
        if isinstance(value, dict):
            candidates.append(value)
    for candidate in candidates:
        value = _first_string(candidate, keys)
        if value:
            return value
    return None


def _default_publish_status(schema: Any) -> str:
    if isinstance(schema, dict):
        enum_values = [str(value) for value in (schema.get("enum") or []) if isinstance(value, str)]
        for preferred in ("published", "posted", "success", "completed", "ok"):
            if preferred in enum_values:
                return preferred
        if enum_values:
            return enum_values[0]
    return "published"


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    return True


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _infer_single_required_string_field(
    output: dict[str, Any],
    props: dict[str, Any],
    required: set[str],
) -> None:
    missing_required = [key for key in required if not output.get(key)]
    if len(missing_required) != 1:
        return
    key = missing_required[0]
    prop_schema = props.get(key)
    if not isinstance(prop_schema, dict) or _schema_type(prop_schema) != "string":
        return
    if _looks_like_url_field(key):
        return

    text = _best_text(output)
    if text:
        output[key] = text


def _looks_like_url_field(key: str) -> bool:
    key_l = str(key or "").lower()
    return key_l == "url" or key_l.endswith("_url") or key_l.endswith("url")


def _infer_topic_lists(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    text = _best_text(output)
    if not text:
        return
    for key, prop_schema in props.items():
        if "topic" not in key.lower() or output.get(key):
            continue
        if not _field_requested(key, props, required):
            continue
        if not isinstance(prop_schema, dict) or _schema_type(prop_schema) != "array":
            continue
        topics = _topics_from_text(text)
        topics = _apply_array_bounds(topics, prop_schema)
        if topics:
            output[key] = topics


def _infer_batch_summary(output: dict[str, Any], props: dict[str, Any], required: set[str]) -> None:
    if not _field_requested("batch_summary", props, required):
        return
    scripts = output.get("scripts")
    if not isinstance(scripts, list) or not scripts:
        return

    summary = output.get("batch_summary")
    if not isinstance(summary, dict):
        summary = {}
    else:
        summary = dict(summary)

    summary_schema = props.get("batch_summary") if isinstance(props.get("batch_summary"), dict) else {}
    summary_props = _schema_properties(summary_schema) if isinstance(summary_schema, dict) else {}

    if _summary_field_requested("total_scripts", summary_props) and not _has_value(summary.get("total_scripts")):
        summary["total_scripts"] = len(scripts)
    if _summary_field_requested("format_distribution", summary_props) and not _has_value(
        summary.get("format_distribution")
    ):
        summary["format_distribution"] = _count_script_field(scripts, "format")
    if (
        _summary_field_requested("content_pillar_distribution", summary_props)
        and not _has_value(summary.get("content_pillar_distribution"))
    ):
        summary["content_pillar_distribution"] = _count_script_field(scripts, "content_pillar")
    if _summary_field_requested("primary_target_metrics", summary_props) and not _has_value(
        summary.get("primary_target_metrics")
    ):
        summary["primary_target_metrics"] = _unique_script_strings(scripts, "target_metric")
    if _summary_field_requested("top_hypotheses", summary_props) and not _has_value(summary.get("top_hypotheses")):
        summary["top_hypotheses"] = _unique_script_strings(scripts, "hypothesis")[:5]
    if (
        _summary_field_requested("recommended_publish_order", summary_props)
        and not _has_value(summary.get("recommended_publish_order"))
    ):
        summary["recommended_publish_order"] = _script_publish_order(scripts)

    if summary:
        output["batch_summary"] = summary


def _summary_field_requested(name: str, summary_props: dict[str, Any]) -> bool:
    return not summary_props or name in summary_props


def _count_script_field(scripts: list[Any], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for script in scripts:
        if not isinstance(script, dict):
            continue
        value = script.get(key)
        if isinstance(value, str) and value.strip():
            label = value.strip()
            counts[label] = counts.get(label, 0) + 1
    return counts


def _unique_script_strings(scripts: list[Any], key: str) -> list[str]:
    values: list[str] = []
    for script in scripts:
        if not isinstance(script, dict):
            continue
        value = script.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
    return _dedupe_strings(values)


def _script_publish_order(scripts: list[Any]) -> list[str]:
    order: list[str] = []
    for index, script in enumerate(scripts, start=1):
        if not isinstance(script, dict):
            continue
        script_id = script.get("script_id")
        if isinstance(script_id, str) and script_id.strip():
            order.append(script_id.strip())
        else:
            order.append(str(index))
    return order


def _filter_additional_properties(schema: dict, output: dict[str, Any]) -> dict[str, Any]:
    if schema.get("additionalProperties") is not False:
        return output
    props = _schema_properties(schema)
    return {key: value for key, value in output.items() if key in props}


def _collect_file_refs(value: Any) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if any(key in node for key in _REFERENCE_ONLY_KEYS):
                return
            ref = _file_ref_from_dict(node)
            if ref:
                refs.append(ref)
            for key, child in node.items():
                if key in _REFERENCE_ONLY_KEYS:
                    continue
                if key in _FILE_LIST_KEYS and isinstance(child, list):
                    refs.extend(_normalize_file_refs(child))
                else:
                    walk(child)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str):
            refs.extend(_file_refs_from_text(node))

    walk(value)
    return _dedupe_file_refs(refs)


def _normalize_file_refs(values: list[Any]) -> list[dict[str, str]]:
    refs: list[dict[str, str]] = []
    for item in values:
        if isinstance(item, dict):
            ref = _file_ref_from_dict(item)
            if ref:
                refs.append(ref)
        elif isinstance(item, str):
            refs.extend(_file_refs_from_text(item) or [_file_ref_from_value(item)])
    return _dedupe_file_refs(refs)


def _format_file_refs_for_schema(refs: list[dict[str, str]], files_schema: Any) -> list[Any]:
    if _array_items_are_strings(files_schema):
        return [value for ref in refs if (value := _file_ref_scalar_value(ref))]
    item_props = _array_item_object_properties(files_schema)
    if item_props:
        return [_project_file_ref_onto_item(ref, item_props) for ref in refs]
    return refs


def _array_items_are_strings(schema: Any) -> bool:
    if not isinstance(schema, dict):
        return False
    items = schema.get("items")
    return isinstance(items, dict) and _schema_type(items) == "string"


def _array_item_object_properties(schema: Any) -> dict[str, Any]:
    """Return the declared properties of an array's object items, else {}."""
    if not isinstance(schema, dict):
        return {}
    items = schema.get("items")
    if isinstance(items, dict) and _schema_type(items) == "object":
        return _schema_properties(items)
    return {}


def _project_file_ref_onto_item(ref: dict[str, str], item_props: dict[str, Any]) -> dict[str, str]:
    """Re-key a canonical file ref onto the property names an items schema asks for.

    The collector always normalizes references to canonical keys (``name``,
    ``path``, ``url``, ``document_id``), but plans request the same evidence
    under names like ``fs_path``, ``file_url``, or ``document_url``. Fill each
    declared string property from the ref using the same alias rules as the
    scalar artifact path — including the guard that a filesystem ``fs_path``
    never borrows a URL. Existing keys are preserved so no evidence is dropped,
    and a property stays missing (failing validation) when no value of the right
    kind exists, so real failures are not hidden.
    """
    projected = dict(ref)
    for key, prop_schema in item_props.items():
        if _has_value(projected.get(key)):
            continue
        if not isinstance(prop_schema, dict) or _schema_type(prop_schema) != "string":
            continue
        value = _artifact_scalar_value_for_key(key, [ref])
        if value:
            projected[key] = value
    return projected


def _file_ref_scalar_value(ref: dict[str, str]) -> str | None:
    return ref.get("path") or ref.get("url") or ref.get("document_id") or ref.get("name")


def _file_ref_from_dict(item: dict[str, Any]) -> dict[str, str] | None:
    path = _first_string(item, _FILE_PATH_KEYS)
    url = _first_string(item, _FILE_URL_KEYS)
    document_id = _first_string(item, ("document_id", "id"))
    value = path or url or document_id
    if not value:
        return None

    ref = _file_ref_from_value(value)
    if item.get("name"):
        ref["name"] = str(item["name"])
    if path:
        ref["path"] = path
    if url:
        ref["url"] = url
    if document_id and "document_id" in item:
        ref["document_id"] = document_id
    return ref


def _file_refs_from_text(text: str) -> list[dict[str, str]]:
    refs = [_file_ref_from_value(match.group("path")) for match in _PATH_RE.finditer(text)]
    refs.extend(_file_ref_from_value(match.group(0)) for match in _URL_RE.finditer(text))
    return _dedupe_file_refs(refs)


def _file_ref_from_value(value: Any) -> dict[str, str]:
    raw = str(value or "").strip().strip("`'\".,;，。；)")
    name = posixpath.basename(raw.split("?", 1)[0].rstrip("/")) or "artifact"
    ref = {"name": name, "path": raw}
    if raw.startswith(("http://", "https://")):
        ref["url"] = raw
    return ref


def _dedupe_file_refs(refs: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    out: list[dict[str, str]] = []
    for ref in refs:
        key = ref.get("path") or ref.get("url") or ref.get("document_id") or ref.get("name")
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(ref)
    return out


def _first_string(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _best_text(output: dict[str, Any]) -> str:
    for key in (*_TEXT_KEYS, "summary"):
        value = output.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key, value in output.items():
        if not isinstance(value, str) or not value.strip():
            continue
        key_l = key.lower()
        if key_l.endswith("_markdown") or key_l in {
            "report_markdown",
            "research_report_markdown",
            "memory_entry_markdown",
            "strategy_log_entry_markdown",
            "drafts_document_markdown",
        }:
            return value.strip()
    for value in output.values():
        if isinstance(value, str) and _looks_like_markdown_document(value):
            return value.strip()
    return ""


def _truncate_summary(text: str, limit: int = 500) -> str:
    cleaned = re.sub(r"\s+", " ", text).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _parse_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        match = re.search(r"\d+", value)
        if match:
            return int(match.group(0))
    return None


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return str(value)


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            title = _first_string(item, ("title", "name", "heading"))
            if title:
                out.append(title)
    return out


def _titles_from_sequence(value: Any) -> list[str]:
    return _string_list(value)


def _titles_from_text(text: str) -> list[str]:
    if not text:
        return []
    titles: list[str] = []
    patterns = [
        r"(?:^|\n)\s*(?:title|note title|标题|笔记标题)\s*[:：]\s*(.+)",
        r"(?:^|\n)\s*\d+[.)、]\s*(.+)",
        r"(?:^|\n)\s*[-*]\s*(.+)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            title = match.group(1).strip().strip("`\"' ")
            if title and len(title) <= 120:
                titles.append(title)
        if titles:
            break
    return _dedupe_strings(titles)


def _apply_array_bounds(values: list[str], schema: Any) -> list[str]:
    if not values:
        return []
    if not isinstance(schema, dict):
        return values
    max_items = schema.get("maxItems")
    min_items = schema.get("minItems")
    if isinstance(max_items, int):
        values = values[:max_items]
    if isinstance(min_items, int) and len(values) < min_items:
        return []
    return values


def _looks_like_markdown_document(text: str) -> bool:
    if not text or len(text.strip()) < 120:
        return False
    if re.search(r"(?m)^\s*#{1,4}\s+\S+", text):
        return True
    if re.search(r"(?m)^\s*(?:[-*]|\d+[.)、])\s+\S+", text):
        return True
    return len(text.strip()) >= 500


def _topics_from_text(text: str) -> list[str]:
    headings = (
        "recommended topics",
        "recommended topics for next 7 days",
        "topics for next 7 days",
        "topic recommendations",
        "选题推荐",
        "推荐选题",
        "推荐话题",
        "未来7天推荐选题",
        "下周选题",
    )
    topics = _list_after_heading(text, headings)
    if topics:
        return _dedupe_strings([_clean_topic_line(item) for item in topics if _clean_topic_line(item)])

    candidates: list[str] = []
    for line in text.splitlines():
        cleaned = _clean_topic_line(line)
        if not cleaned:
            continue
        if re.search(r"(选题|topic|angle|hook|标题|内容角度)", cleaned, re.IGNORECASE):
            candidates.append(cleaned)
    return _dedupe_strings(candidates[:10])


def _clean_topic_line(line: str) -> str:
    cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
    cleaned = cleaned.strip("`\"' ")
    if not cleaned or len(cleaned) > 180:
        return ""
    if cleaned.endswith(":") or cleaned.endswith("："):
        return ""
    return cleaned


def _line_value(text: str, labels: tuple[str, ...]) -> str | None:
    escaped = "|".join(re.escape(label) for label in labels)
    pattern = rf"(?:^|\n)\s*(?:[-*]\s*)?(?:{escaped})\s*[:：]\s*(.+)"
    match = re.search(pattern, text, re.IGNORECASE)
    if not match:
        return None
    return match.group(1).strip().strip("`\"' ")


def _list_after_heading(text: str, labels: tuple[str, ...]) -> list[str]:
    escaped = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?:^|\n)\s*(?:#{{1,4}}\s*)?(?:{escaped})\s*[:：]?\s*\n([\s\S]+)",
        text,
        re.IGNORECASE,
    )
    if not match:
        return []
    block = match.group(1).split("\n\n", 1)[0]
    values = []
    for line in block.splitlines():
        cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)、])\s*", "", line).strip()
        if cleaned:
            values.append(cleaned)
    return _dedupe_strings(values)


def _dedupe_strings(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        key = value.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _parse_json_from_text(text: str, *, schema: dict | None = None) -> Any:
    return parse_json_from_text_for_schema(text, schema=schema)


def parse_json_from_text_for_schema(text: str, *, schema: dict | None = None) -> Any:
    stripped = text.strip()
    if not stripped:
        return None

    parsed: list[tuple[int, Any]] = []
    for index, candidate in enumerate(_json_candidates(stripped)):
        try:
            parsed.append((index, json.loads(candidate)))
        except Exception:
            continue
    if not parsed:
        return None
    if not isinstance(schema, dict) or len(parsed) == 1:
        return parsed[0][1]

    ranked = sorted(
        parsed,
        key=lambda item: (_schema_candidate_score(schema, item[1]), -item[0]),
        reverse=True,
    )
    best_index, best_value = ranked[0]
    if _schema_has_required(schema) and not _candidate_has_required_shape(schema, best_value) and best_index != 0:
        return None
    return best_value


def _schema_candidate_score(schema: dict, value: Any) -> int:
    schema_type = _schema_type(schema)
    if schema_type == "object":
        props = _schema_properties(schema)
        required = {str(key) for key in (schema.get("required") or [])}
        if isinstance(value, dict):
            keys = set(value)
            required_hits = len(required & keys)
            if required and required_hits == 0:
                return 0
            score = 10 + required_hits * 100 + len(set(props) & keys)
            if required and required.issubset(keys):
                score += 1000
            if "batch_summary" in required and "scripts" in keys and isinstance(value.get("scripts"), list):
                score += 250
            return score
        if isinstance(value, list):
            array_key = _single_required_array_property_key(props, required)
            if not array_key:
                return 0
            return 50 + _array_bounds_score(props.get(array_key), value)
        return 0

    if schema_type == "array":
        if isinstance(value, list):
            return 100 + _array_bounds_score(schema, value)
        if isinstance(value, dict):
            return max(
                (80 + _array_bounds_score(schema, candidate))
                for key in _ARRAY_UNWRAP_KEYS
                if isinstance((candidate := value.get(key)), list)
            ) if any(isinstance(value.get(key), list) for key in _ARRAY_UNWRAP_KEYS) else 0
        return 0

    return 1


def _schema_has_required(schema: dict) -> bool:
    required = schema.get("required")
    return isinstance(required, list) and any(str(key).strip() for key in required)


def _candidate_has_required_shape(schema: dict, value: Any) -> bool:
    if _schema_type(schema) != "object":
        return True
    required = {str(key) for key in (schema.get("required") or [])}
    if not required:
        return True

    props = _schema_properties(schema)
    if isinstance(value, dict):
        missing = required - set(value)
        if not missing:
            return True
        return missing == {"batch_summary"} and isinstance(value.get("scripts"), list)
    if isinstance(value, list):
        array_key = _single_required_array_property_key(props, required)
        if not array_key:
            return False
        missing = required - {array_key}
        return not missing or missing == {"batch_summary"}
    return False


def _single_required_array_property_key(props: dict[str, Any], required: set[str]) -> str | None:
    keys = [
        key
        for key in required
        if isinstance(props.get(key), dict) and _schema_type(props[key]) == "array"
    ]
    return keys[0] if len(keys) == 1 else None


def _array_bounds_score(schema: Any, value: list[Any]) -> int:
    if not isinstance(schema, dict):
        return 0
    score = 0
    min_items = schema.get("minItems")
    max_items = schema.get("maxItems")
    if isinstance(min_items, int):
        score += 100 if len(value) >= min_items else -100
    if isinstance(max_items, int):
        score += 100 if len(value) <= max_items else -100
    return score


def _json_candidates(text: str) -> list[str]:
    candidates = [text]
    fence_pattern = r"```(?:json)?\s*([\s\S]*?)```"
    candidates.extend(match.group(1).strip() for match in re.finditer(fence_pattern, text, re.IGNORECASE))

    decoder = json.JSONDecoder()
    for idx, ch in enumerate(text):
        if ch not in "[{":
            continue
        try:
            value, end = decoder.raw_decode(text[idx:])
        except Exception:
            continue
        if isinstance(value, (dict, list)):
            candidates.append(text[idx : idx + end])

    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique
