from types import SimpleNamespace

from packages.core.services.workspace_readiness import (
    build_workspace_readiness_report,
    declared_channel_requirements,
    missing_required_channels,
)


def test_no_declared_channels_do_not_require_channel_setup() -> None:
    report = build_workspace_readiness_report(
        operating_model={},
        subscriptions=[SimpleNamespace(id="sub_1")],
        goals=[SimpleNamespace(id="goal_1")],
        declared_provider_keys=set(),
        active_provider_keys=set(),
        configured_integrations=[],
        configured_channels=[],
        knowledge_nets=[],
        governance_policy=None,
        operating_memory="",
    )

    assert declared_channel_requirements({}) == []
    assert missing_required_channels([], {}) == []
    assert "no_channels" not in report.missing_setup_keys
    channels = next(part for part in report.parts if part.key == "channels")
    assert channels.status == "not_required"
    assert "not a setup blocker" in channels.summary


def test_built_in_channels_do_not_require_external_config() -> None:
    operating_model = {
        "channel_config": {
            "internal_channel": {
                "channel_type": "internal_chat",
                "purpose": "Internal review.",
            },
            "primary_external_channel": {
                "channel_type": "webchat",
                "purpose": "Website lead intake.",
            },
        }
    }

    assert declared_channel_requirements(operating_model) == []
    assert missing_required_channels([], operating_model) == []


def test_declared_external_channel_is_missing_until_configured() -> None:
    operating_model = {
        "channel_config": {
            "channels": [
                {
                    "role": "launch_email",
                    "channel_type": "email",
                    "provider": "gmail",
                    "purpose": "Send launch announcements.",
                }
            ]
        }
    }

    missing = missing_required_channels([], operating_model)
    assert missing == [
        {
            "role": "launch_email",
            "channel_type": "email",
            "provider": "gmail",
            "purpose": "Send launch announcements.",
            "linked_service_key": "",
        }
    ]

    report = build_workspace_readiness_report(
        operating_model=operating_model,
        subscriptions=[SimpleNamespace(id="sub_1")],
        goals=[SimpleNamespace(id="goal_1")],
        declared_provider_keys=set(),
        active_provider_keys=set(),
        configured_integrations=[],
        configured_channels=[],
        knowledge_nets=[],
        governance_policy=None,
        operating_memory="",
    )

    assert "no_channels" in report.missing_setup_keys
    assert report.missing_channel_requirements == missing

    assert missing_required_channels(
        [{"channel_type": "email", "provider": "smtp"}],
        operating_model,
    ) == []


def test_readiness_report_documents_part_roles_and_checks() -> None:
    report = build_workspace_readiness_report(
        operating_model={},
        subscriptions=[],
        goals=[],
        declared_provider_keys={"twitter_x"},
        active_provider_keys=set(),
        configured_integrations=[],
        configured_channels=[],
        knowledge_nets=[],
        governance_policy=None,
        operating_memory="",
    )

    text = report.to_prompt_text()
    assert "Agents and services: missing" in text
    assert "Role: Execution capacity" in text
    assert "Check: At least one active AgentSubscription" in text
    assert "External integrations: missing" in text
    assert report.missing_setup_keys == ["no_agents", "no_goals", "no_integrations"]
