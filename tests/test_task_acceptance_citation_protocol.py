"""Inspection citations and acceptance checks share one evidence target."""
from __future__ import annotations

import json

import pytest

from packages.core.services import task_acceptance_review as acceptance


def _accepted(snapshot, *, pointer: str, quote: str):
    return acceptance.parse_task_acceptance_review(json.dumps({
        "verdict": "accepted",
        "evidence": "The cited delivered content satisfies this requirement.",
        "issues": [],
        "checks": [{"criterion": "The delivered content is present.",
                    "pointer": pointer, "quote": quote}],
    }, ensure_ascii=False), snapshot)


def _text_citations(page):
    return {
        item["citation_pointer"]: item
        for item in page["citations"]
        if item["value_type"] == "string"
    }


def test_nested_step_leaf_is_advertised_with_exact_json_pointer_and_raw_text():
    text = '镜头一："开场"\n路径 C:\\reels\\draft\n镜头二。'
    pointer = "/steps/0/result/reel~1clips~0draft/0/script"
    snapshot = acceptance.acceptance_snapshot({
        "steps": [{"key": "delivery", "result": {
            "reel/clips~draft": [{"script": text}],
        }}],
        "artifacts": [],
    })

    step_page = acceptance.inspect_task_delivery(snapshot, kind="step", index=0)
    assert _text_citations(step_page)[pointer]["value_type"] == "string"
    evidence_page = acceptance.inspect_task_delivery(
        snapshot, kind="evidence", pointer=pointer,
    )
    assert evidence_page["pointer"] == pointer
    assert evidence_page["evidence_digest"] == step_page["evidence_digest"]
    assert evidence_page["content"] == text
    assert evidence_page["total_chars"] == len(text)
    assert evidence_page["next_offset"] is None

    review = _accepted(snapshot, pointer=pointer, quote='"开场"\n路径 C:\\reels')
    assert review.verdict is acceptance.TaskAcceptanceVerdict.ACCEPTED


def test_long_escaped_text_is_paged_as_raw_citable_value_with_bounded_results():
    text = ('镜头："台词"\nC:\\素材\\参考\n' * 420) + "最后一句。"
    pointer = "/steps/0/result/scene/script"
    snapshot = acceptance.acceptance_snapshot({
        "steps": [{"result": {"scene": {"script": text}}}],
        "artifacts": [],
    })

    step_page = acceptance.inspect_task_delivery(snapshot, kind="step", index=0)
    assert pointer in _text_citations(step_page)
    parts = []
    offset = 0
    while True:
        page = acceptance.inspect_task_delivery(
            snapshot, kind="evidence", pointer=pointer, offset=offset,
        )
        assert page["pointer"] == pointer
        assert page["offset"] == offset
        assert page["returned_chars"] == len(page["content"])
        assert len(json.dumps(page, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
        parts.append(page["content"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] > offset
        offset = page["next_offset"]
    assert len(parts) > 1
    assert "".join(parts) == text
    assert _accepted(snapshot, pointer=pointer, quote="最后一句。")


def test_artifact_text_uses_the_same_raw_evidence_protocol():
    text = '引用原文："对照"\n后续内容\\注释'
    pointer = "/artifacts/0/text"
    snapshot = acceptance.acceptance_snapshot({
        "steps": [],
        "artifacts": [{"document_id": "doc-1", "text": text, "text_truncated": False}],
    })

    manifest = acceptance.inspect_task_delivery(snapshot, kind="manifest")
    artifact_entry = manifest["catalog"][0]
    assert artifact_entry["citation_pointer"] == pointer
    assert artifact_entry["value_type"] == "string"
    page = acceptance.inspect_task_delivery(snapshot, kind="evidence", pointer=pointer)
    assert page["content"] == text
    assert page["evidence_digest"] == snapshot.evidence_digest
    assert _accepted(snapshot, pointer=pointer, quote='"对照"\n后续内容')


def test_catalog_does_not_make_unverified_or_missing_values_citable():
    snapshot = acceptance.acceptance_snapshot({
        "task": {"description": "A required phrase that the Step never delivered."},
        "steps": [{"params": {"prompt": "A required phrase"},
                   "result": {"actual": "Delivered phrase"}}],
        "artifacts": [{"document_id": "doc-1", "text_truncated": True}],
    })
    step_page = acceptance.inspect_task_delivery(snapshot, kind="step", index=0)
    assert "/steps/0/result/actual" in _text_citations(step_page)
    assert all(not target.startswith("/steps/0/params") for target in _text_citations(step_page))
    with pytest.raises(ValueError):
        acceptance.inspect_task_delivery(
            snapshot, kind="evidence", pointer="/steps/0/params/prompt",
        )
    with pytest.raises(ValueError):
        acceptance.inspect_task_delivery(
            snapshot, kind="evidence", pointer="/artifacts/0/text",
        )
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        _accepted(snapshot, pointer="/steps/0/result", quote="Delivered phrase")
    assert caught.value.code == "invalid_assessment"
    assert "citation" in caught.value.details["cause"]


def test_stale_citation_cannot_validate_a_changed_delivery_and_read_pages_remain_required():
    old = acceptance.acceptance_snapshot({
        "steps": [{"result": {"script": "First script"}}],
        "artifacts": [{"document_id": "doc-1", "text": "First artifact"}],
    })
    changed = acceptance.acceptance_snapshot({
        "steps": [{"result": {"script": "Second script"}}],
        "artifacts": [{"document_id": "doc-1", "text": "First artifact"}],
    })
    pointer = "/steps/0/result/script"
    old_page = acceptance.inspect_task_delivery(old, kind="evidence", pointer=pointer)
    new_page = acceptance.inspect_task_delivery(changed, kind="evidence", pointer=pointer)
    assert old_page["evidence_digest"] != new_page["evidence_digest"]
    assert "step:0:0" in acceptance.required_delivery_inspections(changed)
    assert "artifact:0:0" in acceptance.required_delivery_inspections(changed)
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        _accepted(changed, pointer=pointer, quote="First script")
    assert caught.value.code == "invalid_assessment"
    assert caught.value.details["cause"] == "citation quote is not present in delivered evidence"


@pytest.mark.parametrize("raw,cause", [
    ('{"verdict": "accepted"}', "unexpected review fields"),
    ('{"verdict":"accepted","verdict":"accepted","evidence":"ok","issues":[]}', "duplicate review key"),
    ('{"verdict":"accepted","evidence":"ok","issues":[],"checks":[]}', "acceptance requires grounded evidence"),
    ('{"verdict":', "Expecting value"),
])
def test_invalid_assessment_keeps_specific_bounded_reason_without_accepting(raw, cause):
    snapshot = acceptance.acceptance_snapshot({"steps": [], "artifacts": []})
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(raw, snapshot)
    assert caught.value.code == "invalid_assessment"
    assert cause in caught.value.details["cause"]
    assert len(caught.value.details["cause"]) <= 2000
    assert caught.value.retryable is False


def test_invalid_assessment_diagnostic_does_not_expose_credentials_in_model_values():
    snapshot = acceptance.acceptance_snapshot({"steps": [], "artifacts": []})
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(json.dumps({
            "verdict": "api_key=sk-do-not-expose-credential", "evidence": "ok", "issues": [],
        }), snapshot)
    assert "sk-do-not-expose-credential" not in json.dumps(caught.value.details)
    assert "<redacted>" in caught.value.details["cause"]


@pytest.mark.parametrize("pointer,quote,field,cause,expected", [
    ("/steps/0/result/report/1/topic", "invented quote", "quote",
     "citation quote is not present", "exact substring of the raw evidence string"),
    ("/steps/0/result/report/9/topic", "anything", "pointer",
     "cited evidence does not exist", None),
    ("/steps/0/params/prompt", "anything", "pointer",
     "citation is not delivered result or artifact content", None),
    ("/steps/0/result/report", '{"topic":"valid first"}', "quote",
     "citation quote is not present", "complete JSON value, preserving types and array order"),
    ("/steps/0/result/verified", "1", "quote",
     "citation quote is not present", "exact JSON representation of the evidence value"),
    ("/steps/0/result/report/1/topic", "", "quote",
     "empty or oversized acceptance citation", None),
])
def test_invalid_citation_identifies_exact_check_and_field_without_reflecting_text(
    pointer, quote, field, cause, expected,
):
    snapshot = acceptance.acceptance_snapshot({"steps": [{"result": {
        "report": [{"topic": "valid first"}, {"topic": "private delivered text"}],
        "verified": True,
    }}]})
    raw = json.dumps({
        "verdict": "accepted", "evidence": "Reviewed", "issues": [],
        "checks": [
            {"criterion": "first", "pointer": "/steps/0/result/report/0/topic", "quote": "valid first"},
            {"criterion": "second", "pointer": pointer, "quote": quote},
        ],
    })
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        acceptance.parse_task_acceptance_review(raw, snapshot)
    details = caught.value.details
    assert caught.value.code == "invalid_assessment"
    assert details["path"] == f"/assessment/checks/1/{field}"
    assert details["citation_index"] == 1
    assert details["source_pointer"] == pointer
    assert cause in details["cause"]
    assert details.get("expected") == expected
    assert "private delivered text" not in json.dumps(details)
    assert "invented quote" not in json.dumps(details)
    assert json.loads(raw)["checks"][1] == {"criterion": "second", "pointer": pointer, "quote": quote}


def test_citation_source_pointer_diagnostic_redacts_credentials():
    snapshot = acceptance.acceptance_snapshot({"steps": [{"result": {}}]})
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        _accepted(snapshot, pointer="/steps/0/result/api_key=sk-do-not-expose-credential", quote="absent")
    assert caught.value.details["path"] == "/assessment/checks/0/pointer"
    assert "sk-do-not-expose-credential" not in json.dumps(caught.value.details)


def test_large_leaf_catalog_has_bounded_continuation_pages_without_losing_targets():
    result = {f"section-{index:03d}": f"Delivered section {index}" for index in range(120)}
    snapshot = acceptance.acceptance_snapshot({
        "steps": [{"result": result}], "artifacts": [],
    })
    step_page = acceptance.inspect_task_delivery(snapshot, kind="step", index=0)
    assert step_page["result_pointer"] == "/steps/0/result"
    assert "pointer" not in step_page
    assert step_page["citations_next_offset"] is not None
    assert len(json.dumps(step_page, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS

    citations = list(step_page["citations"])
    offset = step_page["citations_next_offset"]
    while offset is not None:
        page = acceptance.inspect_task_delivery(
            snapshot, kind="citations", index=0, offset=offset,
        )
        assert page["offset"] == offset
        assert page["evidence_digest"] == snapshot.evidence_digest
        assert len(json.dumps(page, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
        citations.extend(page["citations"])
        assert page["next_offset"] is None or page["next_offset"] > offset
        offset = page["next_offset"]
    assert [item["citation_pointer"] for item in citations] == [
        f"/steps/0/result/section-{index:03d}" for index in range(120)
    ]
    assert all(item["value_type"] == "string" for item in citations)


def test_result_navigation_pointer_does_not_masquerade_as_a_text_citation():
    snapshot = acceptance.acceptance_snapshot({
        "steps": [{"result": {"script": "A quoted delivered sentence."}}],
        "artifacts": [],
    })
    page = acceptance.inspect_task_delivery(snapshot, kind="step", index=0)
    assert page["result_pointer"] == "/steps/0/result"
    assert "pointer" not in page
    assert {item["citation_pointer"] for item in page["citations"]} == {
        "/steps/0/result/script",
    }
    with pytest.raises(acceptance.TaskAcceptanceError) as caught:
        _accepted(snapshot, pointer=page["result_pointer"], quote="delivered sentence")
    assert caught.value.code == "invalid_assessment"


def test_json_pointer_tilde_one_spelling_is_decoded_once():
    snapshot = acceptance.acceptance_snapshot({
        "steps": [{"result": {"~1": "This key is a literal tilde followed by one."}}],
        "artifacts": [],
    })
    pointer = "/steps/0/result/~01"
    page = acceptance.inspect_task_delivery(snapshot, kind="step", index=0)
    assert pointer in _text_citations(page)
    evidence = acceptance.inspect_task_delivery(snapshot, kind="evidence", pointer=pointer)
    assert evidence["content"] == "This key is a literal tilde followed by one."
    assert _accepted(snapshot, pointer=pointer, quote="literal tilde followed by one")
    with pytest.raises(ValueError):
        acceptance.inspect_task_delivery(
            snapshot, kind="evidence", pointer="/steps/0/result/~1",
        )


def test_citation_catalog_omits_pointers_longer_than_validator_limit():
    prefix = "/steps/0/result/"
    allowed_key = "a" * (1000 - len(prefix))
    oversized_key = "b" * (1001 - len(prefix))
    snapshot = acceptance.acceptance_snapshot({
        "steps": [{"result": {allowed_key: "Allowed", oversized_key: "Not citable"}}],
        "artifacts": [],
    })
    page = acceptance.inspect_task_delivery(snapshot, kind="step", index=0)
    pointers = set(_text_citations(page))
    assert prefix + allowed_key in pointers
    assert prefix + oversized_key not in pointers
    assert len(json.dumps(page, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
    assert _accepted(snapshot, pointer=prefix + allowed_key, quote="Allowed")
    with pytest.raises(ValueError):
        acceptance.inspect_task_delivery(
            snapshot, kind="evidence", pointer=prefix + oversized_key,
        )


def test_highly_escaped_evidence_offsets_count_raw_characters():
    text = ('"\\\n\t\r' * 1_600) + "raw-tail"
    pointer = "/steps/0/result/script"
    snapshot = acceptance.acceptance_snapshot({
        "steps": [{"result": {"script": text}}], "artifacts": [],
    })
    offset = 0
    pages = 0
    while True:
        page = acceptance.inspect_task_delivery(
            snapshot, kind="evidence", pointer=pointer, offset=offset,
        )
        next_offset = page["next_offset"]
        end = next_offset if next_offset is not None else len(text)
        assert page["content"] == text[offset:end]
        assert page["returned_chars"] == end - offset
        assert len(json.dumps(page, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
        pages += 1
        if next_offset is None:
            break
        offset = next_offset
    assert pages > 1


def test_json_escaped_pointer_metadata_cannot_overflow_inspection_page():
    # The raw pointer fits the input limit, but its JSON representation does not.
    snapshot = acceptance.acceptance_snapshot({
        "steps": [{"result": {"\u0000" * 600: "ok", "normal": "visible"}}],
        "artifacts": [],
    })
    first = acceptance.inspect_task_delivery(snapshot, kind="step", index=0)
    assert len(json.dumps(first, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
    assert first["citation_hints_omitted"] == 1
    assert _text_citations(first)["/steps/0/result/normal"]["value_type"] == "string"
    directory = acceptance.inspect_task_delivery(snapshot, kind="citations", index=0)
    assert len(json.dumps(directory, ensure_ascii=False)) <= acceptance.INSPECTION_RESULT_MAX_CHARS
    assert directory["citation_hints_omitted"] == 1
