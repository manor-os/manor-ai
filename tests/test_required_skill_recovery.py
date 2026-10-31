from types import SimpleNamespace


def test_strategist_gets_bounded_concrete_failure_evidence():
    from packages.core.ai.runtime.strategist import _format_failed_task_evidence

    ctx = SimpleNamespace(recent_tasks=[SimpleNamespace(
        id="task", title="Rights review", status="failed", actual_output={
            "supervisor_evidence": "Missing required Skill evidence",
            "steps": [{"error": {"type": "RequiredSkillBlocked",
                                  "message": "Missing eligibility_matrix_ref " + "x" * 4000}}],
        },
    )])
    evidence = _format_failed_task_evidence(ctx)
    assert "eligibility_matrix_ref" in evidence
    assert "RequiredSkillBlocked" in evidence
    assert len(evidence) < 2500
