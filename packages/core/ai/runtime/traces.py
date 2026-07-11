from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from packages.core.ai.runtime.envelope import RuntimeEnvelope

logger = logging.getLogger(__name__)


@dataclass
class RuntimeTrace:
    envelope: RuntimeEnvelope
    events: list[dict[str, Any]] = field(default_factory=list)

    def record(self, event_type: str, **data: Any) -> None:
        self.events.append({"type": event_type, **data})
        logger.debug(
            "Manor AI runtime event surface=%s profile=%s type=%s data=%s",
            self.envelope.surface.value,
            self.envelope.profile.value,
            event_type,
            data,
        )
