from types import SimpleNamespace


from packages.core.services.agent_runtime_config import (
    AgentModelMode,
    AgentRuntimeConfig,
    agent_runtime_config,
    agent_runtime_config_for,
    normalize_agent_runtime_config,
)


def test_agent_runtime_config_returns_fixed_overrides():
    assert agent_runtime_config(
        {
            "model_mode": AgentModelMode.FIXED.value,
            "model": "openai/gpt-5.6-terra",
            "temperature": 0.2,
            "max_tokens": 8192,
        }
    ) == AgentRuntimeConfig(
        model_mode=AgentModelMode.FIXED,
        model="openai/gpt-5.6-terra",
        temperature=0.2,
        max_tokens=8192,
    )


def test_agent_runtime_config_uses_inherit_enum_without_model_keyword():
    assert agent_runtime_config(
        {
            "model_mode": AgentModelMode.INHERIT.value,
            "model": "openai/gpt-5.6-terra",
            "temperature": "0.4",
            "max_tokens": "4096",
        }
    ) == AgentRuntimeConfig(
        model=None,
        temperature=0.4,
        max_tokens=4096,
    )


def test_agent_runtime_config_ignores_invalid_sampling_values():
    assert agent_runtime_config(
        {
            "model": " ",
            "temperature": 3,
            "max_tokens": 0,
        }
    ) == AgentRuntimeConfig()


def test_agent_runtime_config_reads_agent_objects_and_mappings():
    config = {
        "model_mode": AgentModelMode.FIXED.value,
        "model": "anthropic/claude-sonnet-4.6",
        "temperature": 0,
        "max_tokens": 2048,
    }
    expected = AgentRuntimeConfig(
        model_mode=AgentModelMode.FIXED,
        model="anthropic/claude-sonnet-4.6",
        temperature=0,
        max_tokens=2048,
    )

    assert agent_runtime_config_for(SimpleNamespace(config=config)) == expected
    assert agent_runtime_config_for({"config": config}) == expected


def test_agent_runtime_config_normalizes_legacy_selection_to_enum():
    assert normalize_agent_runtime_config(
        {
            "model": "default",
            "temperature": 0.3,
        }
    ) == {
        "model_mode": AgentModelMode.INHERIT.value,
        "temperature": 0.3,
    }


def test_agent_runtime_config_infers_fixed_mode_for_pre_enum_model_ids():
    assert normalize_agent_runtime_config(
        {
            "model": "openai/gpt-5.6-sol",
            "max_tokens": 4096,
        }
    ) == {
        "model_mode": AgentModelMode.FIXED.value,
        "model": "openai/gpt-5.6-sol",
        "max_tokens": 4096,
    }


