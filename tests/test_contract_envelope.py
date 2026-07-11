from packages.core.contracts.envelope import Success, Failure, StepResult


def test_success_carries_data_and_is_ok():
    r = Success({"files": []})
    assert r.ok is True
    assert r.data == {"files": []}
    assert r.to_dict() == {"ok": True, "data": {"files": []}}


def test_failure_carries_reason_and_is_not_ok():
    r = Failure("sandbox tool unavailable", detail={"tool": "docx"})
    assert r.ok is False
    assert r.reason == "sandbox tool unavailable"
    assert r.to_dict() == {"ok": False, "reason": "sandbox tool unavailable", "detail": {"tool": "docx"}}


def test_step_result_union_type_exists():
    assert StepResult is not None
