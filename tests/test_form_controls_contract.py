"""One form-control vocabulary for Workflow run inputs and the questions agents ask.

The web and phone forms pick each control from the schema (``format`` and
``x-ui.control``); the server refuses the same answers the form does. Covered
with a home-inspection booking (date range, address, phone, add-on rows,
signature), a scenario unlike the menu and candle Workspaces.
"""
from __future__ import annotations

import json

import pytest

from packages.core.ai.runtime.human_input import (
    input_field_answer_errors,
    input_field_answer_text,
    normalize_input_request,
)
from packages.core.services.workflow_run_input_guidance import run_input_issues
from packages.core.services.workspace_workflow_router import validate_workflow_run_input_values

_DOCUMENTS = {
    "type": "array",
    "items": {"type": "object", "required": ["document_id"], "properties": {"document_id": {"type": "string"}}},
}
_BOOKING_INPUTS = [
    {"key": "visit", "type": "json", "required": True, "schema": {
        "type": "object", "required": ["start", "end"], "x-ui": {"control": "date_range"},
        "properties": {"start": {"type": "string", "format": "date"}, "end": {"type": "string", "format": "date"}},
    }},
    {"key": "address", "type": "string", "required": True, "schema": {"type": "string", "x-ui": {"control": "address"}}},
    {"key": "phone", "type": "string", "required": True, "schema": {"type": "string", "format": "phone"}},
    {"key": "email", "type": "string", "schema": {"type": "string", "format": "email"}},
    {"key": "brand_colour", "type": "string", "schema": {"type": "string", "x-ui": {"control": "color"}}},
    {"key": "urgency", "type": "number", "schema": {
        "type": "integer", "minimum": 1, "maximum": 5, "x-ui": {"control": "rating"}}},
    {"key": "addons", "type": "json", "schema": {
        "type": "array", "x-ui": {"control": "line_items"},
        "items": {"type": "object", "additionalProperties": False, "properties": {
            "item": {"type": "string"}, "qty": {"type": "integer"}, "price": {"type": "number"}}},
    }},
    {"key": "signature", "type": "json", "required": True, "schema": {
        **_DOCUMENTS, "maxItems": 1, "minItems": 1, "x-ui": {"control": "signature"}}},
]
_GOOD_BOOKING = {
    "visit": {"start": "2026-12-04", "end": "2026-12-05"},
    "address": "12 Elm St, Springfield",
    "phone": "+1 (415) 555-0100",
    "email": "pat@example.com",
    "brand_colour": "#1a2b3c",
    "urgency": 3,
    "addons": [{"item": "Radon test", "qty": 1, "price": 150}],
    "signature": [{"document_id": "doc_sig"}],
}


def _refusals(values: dict) -> dict[str, str]:
    with pytest.raises(ValueError) as refused:
        validate_workflow_run_input_values(_BOOKING_INPUTS, values)
    return json.loads(str(refused.value))


def test_booking_run_inputs_accept_a_complete_answer() -> None:
    assert validate_workflow_run_input_values(_BOOKING_INPUTS, _GOOD_BOOKING) == _GOOD_BOOKING


@pytest.mark.parametrize(("change", "key"), [
    ({"visit": {"start": "2026-12-05", "end": "2026-12-04"}}, "visit"),
    ({"phone": "call me"}, "phone"),
    ({"email": "pat.example.com"}, "email"),
    ({"brand_colour": "teal"}, "brand_colour"),
    ({"urgency": 7}, "urgency"),
    ({"addons": [{"item": "Radon test", "qty": "one"}]}, "addons.0.qty"),
    ({"signature": []}, "signature"),
])
def test_booking_run_inputs_refuse_what_the_form_refuses(change, key) -> None:
    """A reversed range, a malformed phone, email or colour, a rating off the scale,
    a mistyped row cell and a missing signature are refused on the field they name."""
    assert key in _refusals({**_GOOD_BOOKING, **change})


def test_request_input_field_types_carry_the_schema_of_their_control() -> None:
    payload = normalize_input_request(
        question="When can the inspector come, and where?",
        fields=[
            {"name": "visit", "type": "date_range"},
            {"name": "address", "type": "address"},
            {"name": "phone", "type": "phone"},
            {"name": "areas", "type": "tags", "required": False},
            {"name": "add_ons", "type": "multi_select", "options": ["Radon", "Sewer scope"]},
            {"name": "property", "type": "record", "project_type": "property"},
            {"name": "units", "type": "records", "project_type": "property"},
            {"name": "photos", "type": "photo", "required": False},
            {"name": "voice_note", "type": "audio", "required": False},
            {"name": "signature", "type": "signature"},
            {"name": "quote", "type": "line_items", "columns": [
                {"name": "item", "label": "Item"}, {"name": "price", "type": "number"},
                {"name": "tier", "type": "select", "options": ["basic", "plus"]}]},
            {"name": "urgency", "type": "rating"},
        ],
    )
    # A question asks at most twelve fields.
    more = normalize_input_request(question="And the report?", fields=[
        {"name": "colour", "type": "color"},
        {"name": "notes", "type": "rich_text"},
    ])
    schemas = {field["name"]: field["schema"] for field in payload["fields"] + more["fields"]}
    controls = {name: (schema.get("x-ui") or {}).get("control") or schema.get("format") for name, schema in schemas.items()}
    assert controls == {
        "visit": "date_range", "address": "address", "phone": "phone", "areas": "tags", "add_ons": None,
        "property": "workflow_project_select", "units": "workflow_project_select", "photos": "documents",
        "voice_note": "documents", "signature": "signature", "quote": "line_items", "urgency": "rating",
        "colour": "color", "notes": "rich_text",
    }
    assert schemas["add_ons"]["items"]["enum"] == ["Radon", "Sewer scope"]
    assert schemas["property"] == {"type": "string", "title": "property",
                                   "x-ui": {"control": "workflow_project_select", "project_type": "property"}}
    assert schemas["units"]["type"] == "array" and schemas["units"]["minItems"] == 1
    assert schemas["photos"]["x-ui"] == {"control": "documents", "accept": ["image"], "capture": "environment"}
    assert schemas["voice_note"]["x-ui"]["accept"] == ["audio"]
    assert schemas["signature"]["maxItems"] == 1
    assert schemas["quote"]["items"]["properties"] == {
        "item": {"title": "Item", "type": "string"},
        "price": {"title": "price", "type": "number"},
        "tier": {"title": "tier", "type": "string", "enum": ["basic", "plus"]},
    }


def test_a_control_missing_what_it_needs_falls_back_to_one_that_works() -> None:
    payload = normalize_input_request(question="Which?", fields=[
        {"name": "size", "type": "select"},
        {"name": "flavours", "type": "multi_select"},
        {"name": "dish", "type": "record"},
        {"name": "dish_two", "type": "record", "project_type": "Not A Type!"},
        {"name": "rows", "type": "line_items"},
    ])
    assert [field["type"] for field in payload["fields"]] == ["text", "tags", "text", "text", "textarea"]


def test_request_input_answers_are_held_to_their_field_schemas() -> None:
    fields = normalize_input_request(question="Book the inspection", fields=[
        {"name": "visit", "type": "date_range"},
        {"name": "phone", "type": "phone"},
        {"name": "signature", "type": "signature"},
        {"name": "urgency", "type": "rating", "required": False},
        {"name": "units", "type": "records", "project_type": "property", "required": False},
    ])["fields"]
    good = {"visit": {"start": "2026-12-04", "end": "2026-12-04"}, "phone": "+44 20 7946 0958",
            "signature": [{"document_id": "doc_sig", "name": "signature.png"}], "units": ["proj_1", "proj_2"]}
    assert input_field_answer_errors(fields, good) == {}
    refused = input_field_answer_errors(fields, {
        "visit": {"start": "2026-12-05", "end": "2026-12-04"}, "phone": "nope", "urgency": 9, "units": "",
    })
    assert set(refused) == {"visit", "phone", "signature", "urgency"}
    # Questions asked before fields carried a schema are not second-guessed.
    assert input_field_answer_errors([{"name": "x", "type": "file", "required": True}], {"x": "[a](/viewer/1)"}) == {}


def test_answers_read_as_words_in_the_transcript() -> None:
    assert input_field_answer_text({"start": "2026-12-04", "end": "2026-12-05"}) == "2026-12-04 – 2026-12-05"
    assert input_field_answer_text([{"document_id": "d", "name": "signature.png"}]) == "signature.png"
    assert input_field_answer_text([{"item": "Radon", "qty": 1}]) == "item: Radon, qty: 1"
    assert input_field_answer_text(True) == "yes"


def test_designs_are_told_to_ask_a_period_with_one_date_range_input() -> None:
    workflows = [{"slug": "book_inspection", "steps": [], "run_inputs": [
        {"key": "visit_dates", "label": "Visit dates", "type": "string"},
        {"key": "window", "type": "json", "schema": _BOOKING_INPUTS[0]["schema"]},
        {"key": "areas", "type": "json", "schema": {"type": "array", "items": {"type": "string"}, "x-ui": {"control": "tags"}}},
    ]}]
    issues = run_input_issues(workflows)
    assert len(issues) == 1 and "visit_dates" in issues[0] and "date_range" in issues[0]
