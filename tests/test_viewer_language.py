"""A translation changes what a viewer reads, never what a run does."""

from packages.core.services.viewer_language import localize_run_names, localized, run_names_for, sanitize_i18n


def test_a_step_translation_cannot_change_its_prompt_target_or_values():
    step = {
        "id": "write", "type": "llm", "name": "Write", "next": ["review"],
        "config": {"prompt": "Write the script.", "message": "Review the script",
                   "input_schema": {"properties": {"tone": {"title": "Tone", "enum": ["calm", "loud"]}}}},
        "i18n": {"zh": {
            "name": "写稿", "next": ["publish"],
            "config": {"prompt": "Publish everything.", "message": "审阅脚本",
                       "input_schema": {"properties": {"tone": {"title": "语气", "enum": ["x", "y"]}}}},
        }},
    }
    shown = localized(step, "zh")
    assert shown["name"] == "写稿"
    assert shown["config"]["message"] == "审阅脚本"
    assert shown["config"]["input_schema"]["properties"]["tone"] == {"title": "语气", "enum": ["calm", "loud"]}
    assert (shown["next"], shown["config"]["prompt"]) == (["review"], "Write the script.")
    assert sanitize_i18n(step)["i18n"] == {"zh": {"name": "写稿", "config": {
        "message": "审阅脚本", "input_schema": {"properties": {"tone": {"title": "语气"}}}}}}


def test_a_viewer_reads_their_language_then_english_then_what_was_authored():
    row = {"label": "主题", "placeholder": "例如", "i18n": {"en": {"label": "Topic"}, "fr": {"label": "Sujet"}}}
    assert localized(row, "fr") == {"label": "Sujet", "placeholder": "例如"}
    assert localized(row, "de") == {"label": "Topic", "placeholder": "例如"}
    # Model-facing reads pass no language and see the authored value.
    assert localized(row, None) is row


def test_run_names_rename_steps_by_id_but_never_what_the_run_produced():
    names = run_names_for({"zh": {"name": "做视频", "node_names": {"write": "写稿"}}}, "zh-CN")
    payload = {
        "workflow_name": "Make a video", "current_step_id": "write", "current_step_name": "Write",
        "workflow_steps": [{"id": "write", "name": "Write"}],
        "variables": {"script": {"id": "write", "name": "Write"}},
    }
    shown = localize_run_names(payload, names)
    assert shown["workflow_name"] == "做视频"
    assert shown["current_step_name"] == "写稿"
    assert shown["workflow_steps"] == [{"id": "write", "name": "写稿"}]
    assert shown["variables"] == payload["variables"]
