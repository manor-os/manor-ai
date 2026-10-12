"""Plan receipts localize their framing without altering execution evidence."""
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from packages.core.i18n import TRANSLATIONS, _locale_var, t
from packages.core.workspace_chat import notifiers


LOCALES = ("en", "zh", "es", "fr", "de", "ja", "vi")
DOCUMENT_ID = "01M28ZQE7C1AMJDX9EAXY8KCJ3"
TITLE = "English report / 原始标题 {subject}"
SUMMARY = 'Original English output — {"language": "English"}'
ARTIFACT = {"kind": "document", "value": DOCUMENT_ID, "name": "English-report.md"}
STEPS = [
    {
        "key": "write_report", "description": TITLE, "status": "done",
        "result_summary": SUMMARY, "artifacts": [ARTIFACT], "depends_on": [],
    },
    {"key": "optional_check", "status": "skipped", "depends_on": []},
]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.asyncio
async def test_completion_localizes_both_projections_and_preserves_artifact_contract(monkeypatch, locale):
    post = AsyncMock()
    completion = AsyncMock()
    monkeypatch.setattr(notifiers, "_safe_post", post)
    monkeypatch.setattr(notifiers, "_safe_post_completion", completion)
    steps = deepcopy(STEPS)
    await notifiers.notify_plan_completed(
        entity_id="entity", workspace_id="workspace", plan_id="plan", task_id="task",
        task_title=TITLE, duration_seconds=12.5, cost_usd=0.012, steps=steps, locale=locale,
    )

    post.assert_awaited_once()
    completion.assert_awaited_once()
    thread = post.await_args.kwargs
    main = completion.await_args.kwargs
    for payload in (thread, main):
        body = payload["body"]
        assert body.startswith(t("task.execution.plan_completed", locale=locale, subject=TITLE))
        assert SUMMARY in body
        assert f"[English-report.md](/viewer/{DOCUMENT_ID})" in body
        assert t("task.execution.files_saved", locale=locale) in body
        assert t("task.execution.step_skipped", locale=locale) in body
        assert "12.5s" in body and "$0.012" in body
        assert payload["refs"] == [{"type": "plan", "id": "plan"}, {"type": "task", "id": "task"}]
        assert payload["attachments"] == [{
            "name": "English-report.md", "type": "knowledge", "fileType": "md",
            "document_id": DOCUMENT_ID, "openUrl": f"/viewer/{DOCUMENT_ID}",
        }]
        assert payload["entity_id"] == "entity" and payload["workspace_id"] == "workspace"
        if locale != "en":
            assert "Task complete" not in body
            assert "What was completed" not in body
            assert "Files and outputs saved" not in body
    assert t("task.execution.parallel_layer", locale=locale, layer=1) in thread["body"]
    assert "thread_ref_kind" not in main
    assert thread["thread_ref_id"] == "plan"
    assert thread["meta"] == {"feedback_target_kind": "none"}
    assert main["meta"] == {"feedback_target_kind": "task_completion"}
    assert steps == STEPS


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.asyncio
async def test_step_success_preserves_model_language_and_localizes_saved_document_fallback(monkeypatch, locale):
    post = AsyncMock()
    monkeypatch.setattr(notifiers, "_safe_post", post)
    inputs = dict(
        entity_id="entity", workspace_id="workspace", plan_id="plan", step_id="step",
        step_key="write_report", kind="llm", description=TITLE, duration_seconds=2.0,
        cost_usd=None, agent_name="Atlas 原名", subscription_id="subscription", locale=locale,
    )
    result = {"document": {"id": DOCUMENT_ID, "name": "English-report.md"}}
    original = deepcopy(result)
    await notifiers.notify_step_done(**inputs, result_summary=SUMMARY, result=result)
    assert post.await_count == 2
    for call in post.await_args_list:
        payload = call.kwargs
        body = payload["body"]
        assert SUMMARY in body and TITLE in body and "Atlas 原名" in body
        assert t("task.execution.what_produced", locale=locale) in body
        assert t("task.execution.files_saved", locale=locale) in body
        assert payload["author_subscription_id"] == "subscription"
        assert payload["refs"] == [{"type": "plan", "id": "plan"}, {"type": "step", "id": "step"}]
        assert payload["attachments"][0]["document_id"] == DOCUMENT_ID
        if locale != "en":
            assert "Completed:" not in body and " by **" not in body
    assert result == original
    # Only a notifier-authored fallback is translated; an explicit English result stays English.
    assert notifiers.summarize_result_for_chat(result, locale=locale) == t(
        "task.execution.saved_document", locale=locale, name="English-report.md",
    )
    assert notifiers.summarize_result_for_chat({"text": SUMMARY}, locale=locale) == SUMMARY
    post.reset_mock()
    await notifiers.notify_step_done(**inputs)
    assert t("task.execution.step_successful", locale=locale) in post.await_args.kwargs["body"]


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("will_retry", (True, False))
@pytest.mark.asyncio
async def test_failed_step_keeps_retry_routing_and_original_error(monkeypatch, locale, will_retry):
    post = AsyncMock()
    monkeypatch.setattr(notifiers, "_safe_post", post)
    error = {"type": "ProviderError", "message": 'Original error {"retry": false}'}
    await notifiers.notify_step_failed(
        entity_id="entity", workspace_id="workspace", plan_id="plan", step_id="step",
        step_key="write_report", error=error, will_retry=will_retry, locale=locale,
    )
    assert post.await_count == (1 if will_retry else 2)
    assert post.await_args_list[0].kwargs["thread_ref_id"] == "plan"
    for call in post.await_args_list:
        body = call.kwargs["body"]
        assert "`write_report`" in body and error["message"] in body
        assert (t("task.execution.will_retry", locale=locale) in body) == will_retry
        if locale != "en":
            assert "failed:" not in body and "will retry" not in body
    if not will_retry:
        assert "thread_ref_kind" not in post.await_args_list[-1].kwargs


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.asyncio
async def test_plan_failure_and_held_receipts_preserve_issue_and_localize_defaults(monkeypatch, locale):
    post = AsyncMock()
    monkeypatch.setattr(notifiers, "_safe_post", post)
    scope = dict(entity_id="entity", workspace_id="workspace", plan_id="0123456789", locale=locale)
    await notifiers.notify_plan_failed(**scope, error=None)
    body = post.await_args.kwargs["body"]
    assert t("task.execution.plan_label", locale=locale, plan_id="01234567") in body
    assert t("task.execution.unknown_error", locale=locale) in body
    if locale != "en":
        assert "Task failed" not in body and "unknown error" not in body
    post.reset_mock()
    await notifiers.notify_plan_needs_attention(**scope, task_title=TITLE, issue=SUMMARY, steps=deepcopy(STEPS))
    assert post.await_count == 2
    for call in post.await_args_list:
        body = call.kwargs["body"]
        assert body.startswith(t("task.execution.plan_needs_attention", locale=locale, subject=TITLE))
        assert SUMMARY in body
        if locale != "en":
            assert "Task needs attention" not in body


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.asyncio
async def test_human_input_translates_wrapper_only_and_preserves_card_origin(monkeypatch, locale):
    post = AsyncMock()
    monkeypatch.setattr(notifiers, "_safe_post", post)
    scope = dict(
        entity_id="entity", workspace_id="workspace", plan_id="plan", step_id="step",
        step_key="write_report", approval_request_id="approval", locale=locale,
    )
    await notifiers.notify_step_needs_human(**scope, prompt=SUMMARY)
    payload = post.await_args.kwargs
    assert payload["body"] == t("task.execution.needs_input_body", locale=locale, step_key="write_report", prompt=SUMMARY)
    assert payload["pending_action"] == {
        "kind": "human_input", "step_id": "step", "plan_id": "plan", "approval_request_id": "approval",
        "input_schema": {"type": "object", "properties": {"response": {"type": "string"}}, "required": ["response"]},
    }
    action = {"kind": "needs_login", "title": TITLE, "metadata": {"url": "https://example.test/login"}}
    original = deepcopy(action)
    await notifiers.notify_step_needs_human(**scope, prompt=SUMMARY, pending_action=action)
    payload = post.await_args.kwargs
    assert payload["body"] == TITLE
    assert payload["pending_action"] == {**action, "step_id": "step", "plan_id": "plan", "approval_request_id": "approval"}
    assert action == original
    await notifiers.notify_step_needs_human(**scope, prompt="", pending_action={"kind": "needs_input"})
    assert post.await_args.kwargs["body"] == t("task.execution.needs_input_title", locale=locale, step_key="write_report")


def test_omitted_locale_stays_english_despite_ambient_locale_and_later_calls_do_not_rewrite_receipts():
    token = _locale_var.set("zh")
    try:
        kwargs = dict(plan_id="plan", task_title=TITLE, duration_seconds=1, cost_usd=None, steps=deepcopy(STEPS))
        original = notifiers._render_plan_completion_summary(**kwargs)
        assert original.startswith(f"✅ **Task complete — {TITLE}**")
        translated = notifiers._render_plan_completion_summary(**kwargs, locale="ja")
        assert "Task complete" not in translated
        assert original.startswith(f"✅ **Task complete — {TITLE}**")
    finally:
        _locale_var.reset(token)


@pytest.mark.parametrize("locale", LOCALES)
def test_all_notifier_templates_have_explicit_translations_and_preserve_placeholders(locale):
    from string import Formatter

    prefixes = (
        "plan_label", "plan_completed", "plan_needs_attention", "plan_failed", "completed_meta",
        "completed_step_count", "completed_duration", "completed_cost", "what_completed", "files_saved",
        "needs_attention", "unknown_error", "step_completed", "by_agent", "in_duration", "what_produced",
        "step_successful", "step_failed", "will_retry", "needs_input_title", "needs_input_body",
        "saved_document", "completed_step", "step_skipped", "artifact_document", "artifact_file",
        "artifact_url", "artifact_job", "artifact_output",
    )
    for suffix in prefixes:
        key = f"task.execution.{suffix}"
        assert key in TRANSLATIONS[locale]
        expected_fields = {(field, spec) for _, field, spec, _ in Formatter().parse(TRANSLATIONS["en"][key]) if field}
        actual_fields = {(field, spec) for _, field, spec, _ in Formatter().parse(TRANSLATIONS[locale][key]) if field}
        assert actual_fields == expected_fields
