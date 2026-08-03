"""Provider-native request adapters for OpenAI-compatible chat APIs.

These adapters are used only for direct provider routes (tenant BYOK or a
native official credential). OpenRouter requests keep their catalog model id
and payload untouched so OpenRouter can apply its own provider translation.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit


ChatPayloadPurpose = Literal["runtime", "probe"]

KIMI_GLOBAL_BASE_URL = "https://api.moonshot.ai/v1"
KIMI_CHINA_BASE_URL = "https://api.moonshot.cn/v1"


@dataclass(frozen=True)
class KimiNativeChatAdapter:
    """Moonshot/Kimi's native OpenAI-compatible Chat Completions adapter."""

    provider: str = "moonshotai"
    api_shape: str = "kimi_chat_completions"
    base_urls: tuple[str, ...] = (KIMI_GLOBAL_BASE_URL, KIMI_CHINA_BASE_URL)

    def matches_base_url(self, base_url: str) -> bool:
        host = (urlsplit(str(base_url or "").strip()).hostname or "").lower()
        return host.endswith("moonshot.ai") or host.endswith("moonshot.cn")

    def normalize_model(self, model_id: str) -> str:
        model = str(model_id or "").strip()
        return model.split("/", 1)[1] if "/" in model else model

    @staticmethod
    def _is_fixed_sampling_model(model_id: str) -> bool:
        model = str(model_id or "").strip().lower()
        return model.startswith(("kimi-k3", "kimi-k2.6", "kimi-k2.5"))

    def adapt_chat_completion_payload(
        self,
        payload: dict[str, Any],
        *,
        model_id: str,
        purpose: ChatPayloadPurpose = "runtime",
    ) -> None:
        """Mutate a direct Kimi request into the provider-native wire shape."""

        wire_model = self.normalize_model(model_id)
        payload["model"] = wire_model

        # Kimi's current Chat API deprecates max_tokens in favor of
        # max_completion_tokens. Preserve an explicitly supplied native field.
        if "max_tokens" in payload:
            payload.setdefault("max_completion_tokens", payload["max_tokens"])
            payload.pop("max_tokens", None)

        # K3/K2.6/K2.5 have fixed sampling settings and reject arbitrary
        # OpenAI defaults. Omitting these fields selects the documented native
        # values instead of leaking Manor's generic temperature=0.7.
        if self._is_fixed_sampling_model(wire_model):
            for field in (
                "temperature",
                "top_p",
                "n",
                "presence_penalty",
                "frequency_penalty",
            ):
                payload.pop(field, None)

        # K3 always reasons and defaults to max. Keep that production default
        # intact; only make the low-cost connectivity probe intentionally fast.
        if wire_model.startswith("kimi-k3") and purpose == "probe":
            payload.setdefault("reasoning_effort", "low")


KIMI_NATIVE_CHAT_ADAPTER = KimiNativeChatAdapter()
NATIVE_CHAT_ADAPTERS = (KIMI_NATIVE_CHAT_ADAPTER,)


def native_chat_adapter_for_base_url(base_url: str) -> KimiNativeChatAdapter | None:
    for adapter in NATIVE_CHAT_ADAPTERS:
        if adapter.matches_base_url(base_url):
            return adapter
    return None


def native_chat_adapter_for_provider(provider: str | None) -> KimiNativeChatAdapter | None:
    normalized = str(provider or "").strip().lower()
    for adapter in NATIVE_CHAT_ADAPTERS:
        if adapter.provider == normalized:
            return adapter
    return None


def adapt_native_chat_completion_payload(
    payload: dict[str, Any],
    *,
    model_id: str,
    base_url: str,
    purpose: ChatPayloadPurpose = "runtime",
) -> None:
    """Apply the native adapter for a direct provider endpoint, if one exists."""

    adapter = native_chat_adapter_for_base_url(base_url)
    if adapter:
        adapter.adapt_chat_completion_payload(
            payload,
            model_id=model_id,
            purpose=purpose,
        )
