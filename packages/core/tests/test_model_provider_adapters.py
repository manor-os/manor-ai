from packages.core.services.model_provider_adapters import (
    KIMI_NATIVE_CHAT_ADAPTER,
    adapt_native_chat_completion_payload,
    native_chat_adapter_for_base_url,
)


def test_kimi_adapter_matches_both_official_regions():
    assert native_chat_adapter_for_base_url("https://api.moonshot.ai/v1") is KIMI_NATIVE_CHAT_ADAPTER
    assert native_chat_adapter_for_base_url("https://api.moonshot.cn/v1") is KIMI_NATIVE_CHAT_ADAPTER
    assert native_chat_adapter_for_base_url("https://openrouter.ai/api/v1") is None


def test_kimi_k3_adapter_uses_native_runtime_request_shape():
    payload = {
        "model": "moonshotai/kimi-k3",
        "messages": [{"role": "user", "content": "hello"}],
        "temperature": 0.7,
        "top_p": 0.8,
        "max_tokens": 8192,
    }

    adapt_native_chat_completion_payload(
        payload,
        model_id="moonshotai/kimi-k3",
        base_url="https://api.moonshot.ai/v1",
    )

    assert payload["model"] == "kimi-k3"
    assert payload["max_completion_tokens"] == 8192
    assert "max_tokens" not in payload
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "reasoning_effort" not in payload


def test_kimi_k3_probe_uses_low_reasoning_effort():
    payload = {
        "model": "moonshotai/kimi-k3",
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
    }

    adapt_native_chat_completion_payload(
        payload,
        model_id="moonshotai/kimi-k3",
        base_url="https://api.moonshot.ai/v1",
        purpose="probe",
    )

    assert payload["reasoning_effort"] == "low"
    assert payload["max_completion_tokens"] == 1


def test_kimi_k26_adapter_omits_fixed_sampling_parameters():
    payload = {
        "model": "moonshotai/kimi-k2.6",
        "temperature": 0.7,
        "max_tokens": 4096,
    }

    adapt_native_chat_completion_payload(
        payload,
        model_id="moonshotai/kimi-k2.6",
        base_url="https://api.moonshot.cn/v1",
    )

    assert payload == {
        "model": "kimi-k2.6",
        "max_completion_tokens": 4096,
    }


def test_openrouter_kimi_payload_is_not_mutated():
    payload = {
        "model": "moonshotai/kimi-k3",
        "temperature": 0.7,
        "max_tokens": 4096,
    }

    adapt_native_chat_completion_payload(
        payload,
        model_id="moonshotai/kimi-k3",
        base_url="https://openrouter.ai/api/v1",
    )

    assert payload == {
        "model": "moonshotai/kimi-k3",
        "temperature": 0.7,
        "max_tokens": 4096,
    }
