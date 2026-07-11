from packages.core.ai.runtime.envelope import RuntimeEnvelope
from packages.core.ai.runtime.principals import RuntimePrincipal, RuntimePrincipalKind
from packages.core.ai.runtime.profiles import RuntimeProfile
from packages.core.ai.runtime.surfaces import ChatSurface
from packages.core.services.sensitive_data import REDACTED, redact_sensitive_text, sanitize_sensitive_payload


def test_sanitize_sensitive_payload_redacts_nested_credentials() -> None:
    payload = {
        "_llm_model_role": "worker",
        "_llm_entity_id": "ent_123",
        "llm_api_key": "sk-or-v1-user-secret",
        "nested": {
            "credentials": {"api_key": "ark-native-secret", "safe": "ok"},
            "items": [{"access_token": "oauth-token-secret"}, "Authorization: Bearer abcdefghijklmnop"],
        },
    }

    redacted = sanitize_sensitive_payload(payload)

    assert redacted["_llm_model_role"] == "worker"
    assert redacted["_llm_entity_id"] == "ent_123"
    assert redacted["llm_api_key"] == REDACTED
    assert redacted["nested"]["credentials"] == REDACTED
    assert redacted["nested"]["items"][0]["access_token"] == REDACTED
    assert "abcdefghijklmnop" not in redacted["nested"]["items"][1]


def test_redact_sensitive_text_covers_common_inline_key_shapes() -> None:
    text = (
        "Authorization: Bearer sk-or-v1-secret-value "
        "url=https://example.test?api_key=ark-test-secret&workspace_id=ok "
        "'_resolved_api_key': 'sk-ant-secret-value'"
    )

    redacted = redact_sensitive_text(text)

    assert redacted is not None
    assert "sk-or-v1-secret-value" not in redacted
    assert "ark-test-secret" not in redacted
    assert "sk-ant-secret-value" not in redacted
    assert "workspace_id=ok" in redacted


def test_runtime_envelope_message_meta_redacts_runtime_metadata_secrets() -> None:
    envelope = RuntimeEnvelope(
        surface=ChatSurface.GLOBAL_OWNER_CHAT,
        profile=RuntimeProfile.OWNER_COPILOT,
        principal=RuntimePrincipal(kind=RuntimePrincipalKind.OWNER, actor_user_id="user_1"),
        entity_id="ent_1",
        metadata={
            "runtime_events": [
                {
                    "event": "llm_call",
                    "llm_api_key": "sk-or-v1-runtime-secret",
                    "metadata": {"_resolved_api_key": "ark-runtime-secret", "safe": "ok"},
                }
            ],
            "runtime_subagents": [{"name": "researcher", "token": "subagent-token-secret"}],
            "llm_api_key": "sk-or-v1-not-allowlisted",
        },
    )

    meta = envelope.to_message_meta()
    rendered = str(meta)

    assert "sk-or-v1-runtime-secret" not in rendered
    assert "ark-runtime-secret" not in rendered
    assert "subagent-token-secret" not in rendered
    assert "sk-or-v1-not-allowlisted" not in rendered
    assert meta["metadata"]["runtime_events"][0]["llm_api_key"] == REDACTED
    assert meta["metadata"]["runtime_events"][0]["metadata"]["safe"] == "ok"
