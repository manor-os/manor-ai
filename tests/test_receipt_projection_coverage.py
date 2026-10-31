"""Every registered tool call the classifier says creates or modifies files must
project receipts, or carry a reasoned exemption.

Guard the class, not the instance: the scan covers every tool in the ToolPool
(plus the classifier's fixed table), classified per action argument the tool's
schema enumerates, so a new file-producing tool or action without a projection
fails here instead of silently falling back to model-retyped IDs.
"""

from __future__ import annotations

from functools import lru_cache

from packages.core.ai.runtime import approval_classifier
from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory
from packages.core.ai.runtime.receipts import RECEIPT_PROJECTIONS

# Tools that create/modify files but intentionally have no receipt yet, each
# with the reason. Shrink this list; never grow it without a reason.
RECEIPT_EXEMPT = {
    "generate_video": (
        "async provider job: success is status=pending + job_id with no file identity; "
        "the file receipt is projected by wait_media_jobs when the job completes"
    ),
}

# Schema arguments whose enum values change what a call does; each value is
# classified on its own, so a file-writing action of a mixed tool is not hidden
# behind its read-only default.
_ACTION_ARGUMENTS = ("action", "operation", "kind", "mode")


def _schema_variants(schema: dict) -> list[dict]:
    function = schema.get("function") if isinstance(schema.get("function"), dict) else schema
    properties = (function.get("parameters") or {}).get("properties") or {}
    variants: list[dict] = [{}]
    for key in _ACTION_ARGUMENTS:
        enum = (properties.get(key) or {}).get("enum")
        if isinstance(enum, list):
            variants += [{key: value} for value in enum]
    return variants


def _is_file_producing(name: str, arguments: dict) -> bool:
    try:
        action = classify_runtime_tool(name, arguments).action
    except Exception:
        return False
    return (
        getattr(action, "resource_kind", None) == "file"
        and getattr(action, "operation", None) in {"create", "modify"}
    )


@lru_cache(maxsize=1)
def _file_producing_calls() -> dict[str, tuple[str, ...]]:
    """{projected tool name: (call descriptions that classify as file create/modify)}.

    A composite call (sandbox action=save_result) is projected as the tool it
    resolves to, exactly as RuntimeToolEvidence projects it.
    """
    from packages.core.ai.tool_pool import ToolPool

    pool = ToolPool()
    pool.initialize()
    schemas = dict(pool.registered_tool_schemas(include_undiscoverable=True))
    for name in approval_classifier._FIXED_TOOL_ACTIONS:
        schemas.setdefault(name, {})
    out: dict[str, set[str]] = {}
    for name, schema in schemas.items():
        for arguments in _schema_variants(schema):
            if not _is_file_producing(name, arguments):
                continue
            projected = RuntimeCompositeToolCallFactory.create(name, arguments).tool_name
            out.setdefault(projected, set()).add(f"{name}({arguments})")
    return {name: tuple(sorted(calls)) for name, calls in out.items()}


def test_the_scan_sees_registered_tools_and_their_actions():
    calls = _file_producing_calls()
    # Registered tools outside the fixed table, and file-writing actions of
    # mixed tools, are in scope (they were missed by a fixed-table-only scan).
    assert "patch_file" in calls
    assert "code_project({'action': 'create'})" in calls["code_project"]
    assert "sandbox({'action': 'save_result'})" in calls["sandbox_save_result"]


def test_every_file_producing_tool_has_a_projection_or_a_reason():
    missing = {
        name: calls for name, calls in sorted(_file_producing_calls().items())
        if name not in RECEIPT_PROJECTIONS and name not in RECEIPT_EXEMPT
    }
    assert missing == {}, f"add a ReceiptProjection or an exemption with a reason: {missing}"


def test_exemptions_are_not_stale():
    stale = sorted(name for name in RECEIPT_EXEMPT if name in RECEIPT_PROJECTIONS)
    assert stale == []
    gone = sorted(set(RECEIPT_EXEMPT) - set(_file_producing_calls()))
    assert gone == [], f"exempted tools no longer classified as file-producing: {gone}"
