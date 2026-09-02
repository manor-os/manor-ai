import pytest

from packages.core.services.voice.work_router import (
    VoiceWorkDecisionFactory,
    VoiceWorkReplyFactory,
    classify_voice_work_followup,
)
from packages.core.services.voice.work_queue import voice_call_control_kind
from packages.core.services.voice.work_types import (
    VoiceControlReplyKind,
    VoiceWorkAction,
    VoiceWorkContext,
    VoiceWorkState,
)


@pytest.mark.parametrize(
    ("text", "kind", "expected"),
    [
        ("再发给 Alice", VoiceControlReplyKind.QUEUED, "上一项完成后"),
        ("Email it too", VoiceControlReplyKind.QUEUED, "current task"),
    ],
)
def test_voice_work_reply_factory_builds_typed_foreground_acknowledgements(
    text,
    kind,
    expected,
):
    assert expected in VoiceWorkReplyFactory().create_control(text, kind)


@pytest.mark.parametrize("text", ["嗯", "恩", "mm-hmm", "okay", "うん", "네"])
def test_backchannels_are_not_server_classified(text):
    assert voice_call_control_kind(text) is None


@pytest.mark.parametrize(
    ("text", "action"),
    [
        ("现在进度怎么样了？", VoiceWorkAction.STATUS),
        ("What did you find?", VoiceWorkAction.STATUS),
        ("取消刚才的任务", VoiceWorkAction.CANCEL),
        ("Stop the current task", VoiceWorkAction.CANCEL),
        ("别做报告了，改成给 Alice 写邮件", VoiceWorkAction.REPLACE),
        ("Change that task to checking the staging logs", VoiceWorkAction.REPLACE),
        ("Cancel that, and check the deployment instead", VoiceWorkAction.REPLACE),
        ("不对", VoiceWorkAction.CLARIFY),
        ("Also send the report to Alice", VoiceWorkAction.QUEUE),
        ("Don't stop it, just add a summary", VoiceWorkAction.QUEUE),
    ],
)
def test_voice_work_router_preserves_work_unless_control_is_explicit(text, action):
    decision = classify_voice_work_followup(
        text,
        VoiceWorkContext(request="Generate the weekly report"),
    )

    assert decision.action is action


def test_voice_status_uses_available_background_input_and_output():
    without_output = classify_voice_work_followup(
        "现在做到哪里了？",
        VoiceWorkContext(request="打开 staging 日志并定位断线原因"),
    )
    with_output = classify_voice_work_followup(
        "现在做到哪里了？",
        VoiceWorkContext(
            request="打开 staging 日志并定位断线原因",
            assistant_output="已经定位到 WebSocket 心跳超时",
        ),
    )

    assert "staging 日志" in without_output.reply
    assert "WebSocket 心跳超时" in with_output.reply
    assert "完成后" in with_output.reply


def test_voice_queue_reply_uses_real_active_and_queued_requests():
    decision = classify_voice_work_followup(
        "再把报告发给 Alice",
        VoiceWorkContext(request="生成本周销售报告"),
    )

    assert decision.action is VoiceWorkAction.QUEUE
    assert "生成本周销售报告" in decision.reply
    assert "再把报告发给 Alice" in decision.reply


def test_voice_status_reports_safe_exit_after_replacement():
    decision = classify_voice_work_followup(
        "现在进度怎么样了？",
        VoiceWorkContext(
            request="Generate the report",
            state="interrupted",
            superseded_by="replacement",
        ),
    )

    assert decision.action is VoiceWorkAction.STATUS
    assert "安全退出后" in decision.reply


def test_voice_router_carries_the_queued_replacement_for_followup_control():
    decision = classify_voice_work_followup(
        "不要做邮件了，改成发消息",
        VoiceWorkContext(
            request="Generate the report",
            state="interrupted",
            superseded_by="queued-email",
        ),
    )

    assert decision.action is VoiceWorkAction.REPLACE
    assert decision.superseded_by == "queued-email"


def test_cancel_after_completion_reports_completion_instead():
    decision = classify_voice_work_followup(
        "Stop the current task",
        VoiceWorkContext(
            request="Generate the report",
            state="completed",
            assistant_output="The report is ready",
        ),
    )

    assert decision.action is VoiceWorkAction.STATUS
    assert "just finished" in decision.reply


def test_status_reports_a_background_failure_instead_of_saying_it_is_running():
    decision = classify_voice_work_followup(
        "Any update?",
        VoiceWorkContext(request="Generate the report", state="failed"),
    )

    assert decision.action is VoiceWorkAction.STATUS
    assert "did not finish" in decision.reply


def test_voice_work_context_normalizes_persisted_state_to_enum():
    context = VoiceWorkContext(request="Generate the report", state="completed")

    assert context.state is VoiceWorkState.COMPLETED


def test_voice_work_decision_factory_builds_typed_decision():
    decision = VoiceWorkDecisionFactory().create(
        "Stop the current task",
        VoiceWorkContext(request="Generate the report"),
    )

    assert decision.action is VoiceWorkAction.CANCEL
    assert "stop the current task" in decision.reply
