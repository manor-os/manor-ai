"""Outbound-only X adapter for Workspace simulations.

Live X actions run through the MCP integration layer.  This channel adapter
exists solely so the social Workspace simulation can exercise the durable
approval and delivery pipeline without performing an external side effect.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from packages.core.models.channel import ChannelConfig
from packages.core.services.channels.base import (
    ChannelAdapter,
    ChannelTextSendError,
    ChannelTextSendResultStatus,
    ChannelTextSendRetryMode,
    NormalizedInbound,
    channel_text_send_result,
    register_adapter,
)


class TwitterXChannelAdapter(ChannelAdapter):
    channel_type = "twitter_x"
    text_send_retry_mode = ChannelTextSendRetryMode.PROVIDER_IDEMPOTENT

    @staticmethod
    def _is_simulation(cc: ChannelConfig) -> bool:
        return cc.provider == "sandbox_social" and bool((cc.config or {}).get("sandbox"))

    async def send_text(
        self,
        cc: ChannelConfig,
        to: str,
        text: str,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        if not self._is_simulation(cc):
            raise ChannelTextSendError.determinate("Live X messages must use the X integration")
        return channel_text_send_result(
            ChannelTextSendResultStatus.QUEUED,
            simulated=True,
            recipient=to,
            idempotency_key=kwargs.get("idempotency_key"),
        )

    async def verify_inbound(self, cc: ChannelConfig, *, headers, query, body) -> bool:
        return False

    async def parse_inbound(
        self,
        cc: ChannelConfig,
        *,
        headers: Dict[str, str],
        query: Dict[str, str],
        body: bytes,
    ) -> Optional[NormalizedInbound]:
        return None


register_adapter(TwitterXChannelAdapter())
