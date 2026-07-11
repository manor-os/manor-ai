from __future__ import annotations

import pytest

from packages.core.tasks.video_adapters import (
    KlingVideoAdapter,
    OpenRouterVideoAdapter,
    VolcengineSeedanceAdapter,
    native_video_model,
    select_video_generation_adapter,
    video_adapter_metadata,
)


@pytest.mark.parametrize(
    ("model", "provider", "native_adapter"),
    [
        ("bytedance/seedance-2.0", "bytedance", VolcengineSeedanceAdapter),
        ("bytedance/seedance-2.0-fast", "bytedance", VolcengineSeedanceAdapter),
        ("kwaivgi/kling-v3.0-std", "kwaivgi", KlingVideoAdapter),
        ("kwaivgi/kling-v3.0-pro", "kwaivgi", KlingVideoAdapter),
    ],
)
def test_every_catalog_video_model_has_native_and_openrouter_routes(model, provider, native_adapter):
    assert isinstance(
        select_video_generation_adapter(
            model=model,
            provider=provider,
            api_key="native-provider-key",
        ),
        native_adapter,
    )
    assert isinstance(
        select_video_generation_adapter(
            model=model,
            provider=provider,
            api_key="sk-or-v1-platform-key",
        ),
        OpenRouterVideoAdapter,
    )


def test_video_adapter_selection_prefers_openrouter_for_openrouter_keys():
    adapter = select_video_generation_adapter(
        model="bytedance/seedance-2.0",
        provider="bytedance",
        api_key="sk-or-v1-platform-key",
    )

    assert isinstance(adapter, OpenRouterVideoAdapter)
    assert video_adapter_metadata(
        "bytedance/seedance-2.0",
        "bytedance",
        "sk-or-v1-platform-key",
    ) == {
        "video_provider": "bytedance",
        "video_adapter": "openrouter",
        "video_route": "openrouter",
        "native_model": "bytedance/seedance-2.0",
    }


def test_video_adapter_selection_routes_native_seedance_and_kling():
    seedance = select_video_generation_adapter(
        model="bytedance/seedance-2.0",
        provider="bytedance",
        api_key="volc-native-key",
    )
    kling = select_video_generation_adapter(
        model="kwaivgi/kling-v3.0-pro",
        provider="kwaivgi",
        api_key="kling-native-key",
    )

    assert isinstance(seedance, VolcengineSeedanceAdapter)
    assert isinstance(kling, KlingVideoAdapter)
    assert native_video_model("bytedance/seedance-2.0") == "doubao-seedance-2-0-260128"
    assert native_video_model("kwaivgi/kling-v3.0-pro") == "kling-v3.0-pro"


def test_video_adapter_metadata_records_native_route():
    assert video_adapter_metadata(
        "bytedance/seedance-2.0-fast",
        "bytedance",
        "volc-native-key",
    ) == {
        "video_provider": "bytedance",
        "video_adapter": "volcengine_seedance",
        "video_route": "native",
        "native_model": "doubao-seedance-2-0-fast-260128",
    }
