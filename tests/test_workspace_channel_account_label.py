from types import SimpleNamespace

from apps.api.routers.workspaces import _connection_account_label


def test_email_channel_account_label_prefers_the_address_over_a_friendly_name():
    channel_config = SimpleNamespace(channel_type="email", name="email")
    integration = SimpleNamespace(
        config={
            "name": "Fundraising inbox",
            "from_address": "calvinlin@manorai.xyz",
        }
    )

    assert _connection_account_label(
        channel_config=channel_config,
        integration=integration,
    ) == "calvinlin@manorai.xyz"


def test_non_email_channel_account_label_keeps_the_friendly_identity():
    channel_config = SimpleNamespace(channel_type="slack", name="slack")
    oauth_account = SimpleNamespace(
        profile={"team_name": "Manor AI", "email": "owner@example.com"},
        provider_user_id="U123",
    )

    assert _connection_account_label(
        channel_config=channel_config,
        oauth_account=oauth_account,
    ) == "Manor AI"
