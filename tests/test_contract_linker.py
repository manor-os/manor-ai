from packages.core.contracts.linker import lint_plan, repair_plan


def _step(key, kind="llm", output_shape=None, refs=None):
    return {"key": key, "kind": kind, "output_shape": output_shape, "input_refs": refs or []}


def test_missing_output_shape_on_llm_step_is_flagged():
    issues = lint_plan([_step("a", kind="llm", output_shape=None)])
    assert any(i.kind == "missing_output_shape" and i.step_key == "a" for i in issues)


def test_reference_to_absent_key_is_flagged():
    steps = [
        _step("a", kind="llm", output_shape="TextResult"),
        _step("b", kind="llm", output_shape="TextResult", refs=[("a", "fs_path")]),
    ]
    issues = lint_plan(steps)
    assert any(i.kind == "dangling_reference" and i.step_key == "b" for i in issues)


def test_valid_reference_passes():
    steps = [
        _step("a", kind="llm", output_shape="ArtifactResult"),
        _step("b", kind="llm", output_shape="TextResult", refs=[("a", "files")]),
    ]
    assert lint_plan(steps) == []


def test_reference_into_unshaped_producer_is_flagged():
    # Real production ReferenceError: `consolidate_packages` reads
    # `${{ steps.pkg_T5_003.result.content }}`, but `pkg_T5_003` (subagent) has
    # no derivable output shape — `content` can't be inferred — so the consumed
    # value is not guaranteed producible. The linker must flag this as a blocking
    # gap on the consumer, not silently dispatch it (where it dies at runtime as
    # `ReferenceError: key 'content' missing`).
    steps = [
        _step("pkg_T5_003", kind="subagent", output_shape=None),
        _step("consolidate_packages", kind="llm", output_shape="TextResult", refs=[("pkg_T5_003", "content")]),
    ]
    repaired, remaining = repair_plan(steps)
    assert any(i.step_key == "consolidate_packages" and i.kind == "dangling_reference" for i in remaining)


def test_bare_reference_into_unshaped_producer_is_ok():
    # A bare `${{ steps.X.result }}` ref (whole result, ref_field is None) is fine
    # even when X has no shape — the consumer takes the entire upstream output.
    steps = [
        _step("a", kind="subagent", output_shape=None),
        _step("b", kind="llm", output_shape="TextResult", refs=[("a", None)]),
    ]
    assert not any(i.kind == "dangling_reference" for i in lint_plan(steps))


def test_repair_infers_artifact_shape_from_fs_path_reference():
    steps = [
        _step("a", kind="llm", output_shape=None),
        _step("b", kind="llm", output_shape="TextResult", refs=[("a", "fs_path")]),
    ]
    repaired, remaining = repair_plan(steps)
    a = next(s for s in repaired if s["key"] == "a")
    assert a["output_shape"] in ("ArtifactResult", "DocumentResult")
    assert remaining == []


def test_repair_infers_draftpack_from_drafts_reference():
    steps = [
        _step("draft", kind="subagent", output_shape=None),
        _step("save", kind="llm", output_shape="TextResult", refs=[("draft", "drafts")]),
    ]
    repaired, _ = repair_plan(steps)
    assert next(s for s in repaired if s["key"] == "draft")["output_shape"] == "DraftPack"


def test_repair_leaves_unfixable_gap_as_remaining_issue():
    steps = [
        _step("a", kind="llm", output_shape="TextResult"),
        _step("b", kind="llm", output_shape="TextResult", refs=[("a", "sku_table")]),
    ]
    repaired, remaining = repair_plan(steps)
    assert any(i.kind == "dangling_reference" for i in remaining)
