"""A workflow run never keeps file contents in its variables, and an App read never loads the large run columns."""
import base64

from sqlalchemy.dialects import postgresql
from sqlalchemy import select

from packages.core.ai.workflow_runner import _variable_limit_failure
from packages.core.models.workflow import WorkflowRun
from packages.core.services.workflow_variable_limits import (
    INLINE_BINARY_MIN_CHARS,
    MAX_VARIABLE_BYTES,
    find_inline_binary,
    trim_inline_binary,
    variable_limit_error,
)
from packages.core.services.workspace_app_data import APP_RUN_COLUMNS, _app_run_options

INLINE_IMAGE = "data:image/png;base64," + base64.b64encode(b"\x89PNG" * (INLINE_BINARY_MIN_CHARS // 4)).decode()


def test_an_inline_image_read_into_a_variable_is_refused_with_the_reason():
    """The keyframe read that filled 40 MB of run variables now fails the step, with the Document to use instead."""
    step = {"id": "read_review_keyframes"}
    result = {"status": "completed", "output": {"files": [{"path": "images/01.png", "image": {"data_url": INLINE_IMAGE}}]}}
    error = _variable_limit_failure(step, result)
    assert error is not None
    assert "inline file data at files.0.image.data_url" in error
    assert "document_id" in error


def test_metadata_only_read_is_stored_normally():
    """With include_image false the read returns path and size; nothing is refused."""
    step = {"id": "read_review_keyframes"}
    result = {"status": "completed", "output": {"files": [{"path": "images/01.png", "size": 2668211,
                                                           "source_sha256": "a" * 64}]}}
    assert _variable_limit_failure(step, result) is None


def test_a_long_plain_text_value_is_not_mistaken_for_binary():
    lyrics = "We light the night, we don't look back. " * 4000
    assert find_inline_binary({"lyrics": lyrics}) is None
    assert variable_limit_error("lyrics", {"lyrics": lyrics}) is None


def test_an_oversized_structure_is_refused_before_it_is_stored():
    too_big = {"items": ["x" * 1024] * (MAX_VARIABLE_BYTES // 1024 + 1)}
    error = variable_limit_error("project_state", too_big)
    assert error is not None and "limit for a workflow variable" in error


def test_large_structured_state_under_the_limit_is_kept():
    state = {"items": ["x" * 1024] * 2000}  # about 2 MB, a real campaign plan's order of size
    assert variable_limit_error("campaign_state", state) is None


def test_trimming_keeps_the_text_and_document_ids_and_removes_only_the_binary():
    value = {"document_id": "01DOC", "path": "images/01.png", "summary": "Neon street",
             "image": {"data_url": INLINE_IMAGE}, "files": [{"name": "02.png", "data_url": INLINE_IMAGE}]}
    trimmed, count, removed = trim_inline_binary(value)
    assert count == 2
    assert removed == 2 * len(INLINE_IMAGE)
    assert trimmed["document_id"] == "01DOC" and trimmed["summary"] == "Neon street"
    assert trimmed["image"]["data_url"].startswith("[removed inline file data:")
    assert variable_limit_error("review", trimmed) is None


def test_app_run_reads_do_not_select_the_large_columns():
    statement = select(WorkflowRun).options(*_app_run_options())
    sql = str(statement.compile(dialect=postgresql.dialect()))
    select_list = sql.split(" FROM ", 1)[0]
    for column in ("variables", "step_results", "execution_trace", "definition_snapshot", "execution_snapshot"):
        assert f"workflow_runs.{column}" not in select_list, column
    for column in APP_RUN_COLUMNS:
        assert f"workflow_runs.{column}" in select_list, column
