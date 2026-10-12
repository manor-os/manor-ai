"""Bounded, site-independent conditions for an already executed browser action.

Attachment evidence certifies a new local preview in the upload area, never
remote processing, file-content identity, or publication.
"""

import mimetypes
from typing import Any


def _items(value: Any) -> list[dict]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _requested_checked(value: Any) -> bool | None:
    # These are the boolean input values accepted by the extension's
    # fill_or_select, not a classifier for page text or user intent.
    normalized = " ".join(str(value).split()).lower()
    if normalized in {"true", "1", "yes", "y", "on", "checked", "select", "selected", "enable", "enabled", "勾选", "选中", "开启", "是"}:
        return True
    if normalized in {"false", "0", "no", "n", "off", "unchecked", "unselect", "unselected", "disable", "disabled", "取消", "取消勾选", "未选中", "关闭", "否"}:
        return False
    return None


def observation_fields(payload: dict) -> dict:
    fields = {}
    feedback = _items(payload.get("page_feedback"))
    for item in _items(payload.get("semantic_refs")) + _items(payload.get("input_candidates")):
        selector = item.get("selector")
        if isinstance(selector, str) and selector and any(key in item for key in ("value", "checked", "checked_state")):
            key = (selector, item.get("frame_id", 0))
            fields.setdefault(
                key,
                {
                    k: item[k]
                    for k in (
                        "selector",
                        "frame_id",
                        "role",
                        "value",
                        "value_truncated",
                        "checked",
                        "checked_state",
                        "type",
                        "scope_id",
                    )
                    if k in item
                },
            )
    return {
        "fields": list(fields.values())[:96], "uploads": _items(payload.get("upload_candidates"))[:12],
        # The extension caps this collection at eight. At saturation, absence
        # cannot establish that a later message is new (even on textless pages).
        "feedback_complete": isinstance(payload.get("page_feedback"), list) and len(feedback) < 8
                             and not any(item.get("text_truncated") for item in feedback),
        "feedback": [{key: item[key] for key in ("scope_id", "frame_id", "text", "severity", "text_truncated") if key in item}
                     for item in feedback[:8]],
    }


def action_scope(payload: dict) -> dict | None:
    for key in ("upload_observation", "target_resolution"):
        target = payload.get(key)
        if isinstance(target, dict) and isinstance(target.get("scope_id"), str) and target["scope_id"]:
            return {"scope_id": target["scope_id"], "frame_id": target.get("frame_id", 0)}
    return None


def scoped_feedback(record: dict, after: dict, expected: str) -> list[dict]:
    scope = record.get("scope")
    if not scope or (record.get("before") or {}).get("feedback_complete") is not True:
        return []

    def matches(item: dict) -> bool:
        return (item.get("scope_id") == scope["scope_id"]
                and item.get("frame_id", 0) == scope["frame_id"]
                and expected in " ".join(str(item.get("text") or "").split()))

    if any(matches(item) for item in (record.get("before") or {}).get("feedback", [])):
        return []
    return [item for item in after.get("feedback", []) if matches(item) and not item.get("text_truncated")]


def condition_for_action(tool: str, args: dict, payload: dict, before: dict) -> dict | None:
    if tool == "mcp__chrome__upload" and payload.get("action_executed") is True:
        baseline = payload.get("upload_observation")
        files = _items(payload.get("submitted_files"))
        if not isinstance(baseline, dict) or not baseline.get("scope_id") or baseline.get("complete") is not True:
            return None
        kinds = [(mimetypes.guess_type(str(item.get("name") or ""))[0] or "").split("/")[0] for item in files]
        if not kinds or len(kinds) > 10 or any(kind not in {"image", "video", "audio"} for kind in kinds):
            return None
        return {"kind": "attachment_preview", "baseline": baseline, "kinds": sorted(kinds)}
    if tool == "mcp__chrome__fill_or_select" and isinstance(args.get("value"), (str, int, float)):
        target = payload.get("target_resolution")
        target = target if isinstance(target, dict) else {}
        selector = target.get("selector") or args.get("selector")
        frame = target.get("frame_id", 0)
        candidates = [
            field
            for field in before.get("fields", [])
            if field.get("selector") == selector and field.get("frame_id", 0) == frame
        ]
        if len(candidates) == 1 and candidates[0].get("type") != "password" and len(str(args["value"])) <= 1000:
            identity = {
                "selector": selector,
                "frame_id": frame,
                "role": candidates[0].get("role"),
                **({"scope_id": target["scope_id"]} if target.get("scope_id") else {}),
            }
            if candidates[0].get("role") in {"checkbox", "radio", "switch"} or candidates[0].get("type") in {"checkbox", "radio"}:
                checked = _requested_checked(args["value"])
                if checked is None:
                    return None
                return {"kind": "field_checked", **identity, "checked": checked}
            return {"kind": "field_value", **identity, "value": str(args["value"])}
    return None


def observed_condition(condition: dict, after: dict, payload: dict) -> dict | None:
    if any(item.get("severity") == "error" for item in _items(payload.get("page_feedback"))):
        return None
    if condition["kind"] in {"field_value", "field_checked"}:
        fields = [
            field
            for field in after.get("fields", [])
            if field.get("selector") == condition["selector"]
            and field.get("frame_id", 0) == condition["frame_id"]
            and field.get("role") == condition["role"]
            and (not condition.get("scope_id") or field.get("scope_id") == condition["scope_id"])
        ]
        if condition["kind"] == "field_checked":
            if len(fields) == 1:
                # Mixed and missing states must never collapse to unchecked.
                checked = fields[0].get("checked_state", fields[0].get("checked"))
                if isinstance(checked, bool) and checked is condition["checked"]:
                    return {"condition": "field_checked", "checked": checked}
            return None
        if (
            len(fields) == 1
            and fields[0].get("value_truncated") is not True
            and fields[0].get("value") == condition["value"]
        ):
            return {"condition": "field_value", "text": condition["value"]}
        return None
    baseline = condition["baseline"]
    areas = [
        item.get("upload_observation")
        for item in after.get("uploads", [])
        if item.get("frame_id", 0) == baseline.get("frame_id", 0)
    ]
    areas = [area for area in areas if isinstance(area, dict) and area.get("scope_id") == baseline["scope_id"]]
    if not areas or any(area.get("complete") is not True for area in areas):
        return None
    # Multiple inputs may share one editor; their observations must agree.
    if any(area != areas[0] for area in areas[1:]):
        return None
    previous = {item.get("source") for item in _items(baseline.get("media"))}
    media = {
        item.get("source"): item
        for item in _items(areas[0].get("media"))
        if isinstance(item.get("source"), str) and item["source"] not in previous
    }
    if len(media) != len(condition["kinds"]) or any(
        not source.startswith("blob:") or item.get("ready") is not True for source, item in media.items()
    ):
        return None
    if sorted(str(item.get("kind")) for item in media.values()) != condition["kinds"]:
        return None
    return {"condition": "attachment_preview", "scope_id": baseline["scope_id"], "count": len(media)}
