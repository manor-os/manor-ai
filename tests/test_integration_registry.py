"""Canonical Integration registry contracts shared by health and Stats."""
import pytest

from packages.core.goals import measurers
from packages.core.integrations.registry import (
    canonical_integration_key,
    get_integration_spec,
    health_checker_for,
    integration_key_aliases,
)
from packages.core.services import integration_health
from packages.core.services.integration_health import classify_nango_health
from packages.core.services.mcp_seed import _MCP_CATALOG
from packages.core.stats.library import list_library_entries


def test_integration_registry_owns_aliases_and_stat_capabilities() -> None:
    spec = get_integration_spec("x")

    assert spec is not None
    assert spec.key == "twitter_x"
    assert canonical_integration_key("x-twitter") == "twitter_x"
    assert {
        "twitter_x", "twitter", "twitterx", "x", "x_twitter",
    }.issubset(integration_key_aliases("twitter_x"))
    assert spec.stat_measurer_key == "twitter_x"
    assert spec.stat_metric_keys == {
        "followers_count", "following_count", "tweet_count", "listed_count",
    }
    assert measurers.get("x") is measurers.get("twitter_x")


def test_every_health_checker_is_registered_by_canonical_key() -> None:
    for provider, checker in integration_health._TESTS.items():
        spec = get_integration_spec(provider)
        assert spec is not None
        assert spec.key == canonical_integration_key(provider)
        assert health_checker_for(provider) is checker

    assert health_checker_for("x") is integration_health.test_twitter_x


def test_every_builtin_catalog_provider_has_a_registry_identity() -> None:
    for provider, *_ in _MCP_CATALOG:
        spec = get_integration_spec(provider)
        assert spec is not None
        assert spec.key == canonical_integration_key(provider)


def test_every_integration_library_metric_is_declared_by_its_registry_spec() -> None:
    integration_entries = [
        entry for entry in list_library_entries()
        if entry.collector_type == "integration"
    ]

    assert integration_entries
    for entry in integration_entries:
        assert entry.integration_key is not None
        spec = get_integration_spec(entry.integration_key.value)
        assert spec is not None
        assert entry.collector_config["metric_key"] in spec.stat_metric_keys
        assert spec.stat_measurer is not None


def test_unknown_integration_key_remains_extensible_but_unmonitored() -> None:
    assert canonical_integration_key("Custom Vendor") == "custom_vendor"
    assert integration_key_aliases("Custom Vendor") == {"custom_vendor"}
    assert get_integration_spec("Custom Vendor") is None
    assert health_checker_for("Custom Vendor") is None


@pytest.mark.asyncio
async def test_health_status_distinguishes_known_unsupported_and_unknown() -> None:
    unsupported = await integration_health.run_test("aider", {})
    unknown = await integration_health.run_test("provider_typo", {})

    assert unsupported["ok"] is None
    assert unsupported["monitoring_status"] == "unsupported"
    assert unsupported["provider_key"] == "aider"
    assert unknown["ok"] is None
    assert unknown["monitoring_status"] == "unknown_provider"
    assert unknown["provider_key"] == "provider_typo"


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"disabled": True}, "disabled"),
        ({"provider_config_present": False}, "provider_config_missing"),
        ({"webhook_configured": False}, "webhook_config_missing"),
        ({"permission_denied": True}, "permission_denied"),
        ({"detail": "401 — token rejected; reconnect."}, "credentials_rejected"),
        ({"ok": True}, "healthy"),
    ],
)
def test_nango_health_reason_is_actionable(kwargs, expected):
    assert classify_nango_health(**kwargs) == expected
