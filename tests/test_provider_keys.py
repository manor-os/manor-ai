from packages.core.services.provider_keys import (
    canonical_provider_key,
    provider_key_aliases,
    provider_keys_match,
)


def test_x_provider_aliases_canonicalize_to_twitter_x():
    assert canonical_provider_key("twitter") == "twitter_x"
    assert canonical_provider_key("x") == "twitter_x"
    assert canonical_provider_key("x-twitter") == "twitter_x"
    assert canonical_provider_key("twitter_x") == "twitter_x"


def test_provider_aliases_include_raw_and_canonical_keys():
    aliases = provider_key_aliases("twitter_x")

    assert {"twitter_x", "twitter", "x", "x_twitter"}.issubset(aliases)
    assert provider_keys_match("twitter", "twitter_x")
    assert provider_keys_match("x", "twitter_x")
    assert not provider_keys_match("linkedin", "twitter_x")


def test_missing_integration_resolution_filters_unsupported_and_browser_coverage():
    from packages.core.services.integration_resolution import (
        resolve_missing_integration_provider_key,
    )

    assert (
        resolve_missing_integration_provider_key(
            "openai",
            supported_provider_keys={"chrome", "twitter_x"},
            connected_provider_keys=set(),
        )
        is None
    )

    chrome_resolution = resolve_missing_integration_provider_key(
        "instagram",
        supported_provider_keys={"chrome", "twitter_x"},
        connected_provider_keys=set(),
    )
    assert chrome_resolution is not None
    assert chrome_resolution.provider == "chrome"
    assert chrome_resolution.covered_provider == "instagram"

    assert (
        resolve_missing_integration_provider_key(
            "instagram",
            supported_provider_keys={"chrome", "twitter_x"},
            connected_provider_keys={"chrome"},
        )
        is None
    )

    twitter_resolution = resolve_missing_integration_provider_key(
        "x",
        supported_provider_keys={"twitter_x"},
        connected_provider_keys=set(),
    )
    assert twitter_resolution is not None
    assert twitter_resolution.provider == "twitter_x"
