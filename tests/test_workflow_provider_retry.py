from packages.core.services.workflow_service import provider_outage_without_external_effect


def test_a_provider_outage_during_generation_only_can_be_retried():
    result = {
        "status": "failed",
        "provider_transient": True,
        "effect_state": "confirmed",
        "tools_used": ["read_file", "generate_video", "wait_media_jobs", "merge_videos"],
    }

    assert provider_outage_without_external_effect(result) is True


def test_a_provider_outage_after_a_publish_tool_still_needs_review():
    result = {
        "status": "failed",
        "provider_transient": True,
        "effect_state": "confirmed",
        "tools_used": ["generate_video", "chrome_publish"],
    }

    assert provider_outage_without_external_effect(result) is False


def test_a_failure_that_is_not_a_provider_outage_keeps_its_gate():
    result = {"status": "failed", "effect_state": "confirmed", "tools_used": ["generate_video"]}

    assert provider_outage_without_external_effect(result) is False


def test_a_failure_with_no_recorded_tools_keeps_its_gate():
    assert provider_outage_without_external_effect({"provider_transient": True, "tools_used": []}) is False
    assert provider_outage_without_external_effect({"provider_transient": True}) is False
