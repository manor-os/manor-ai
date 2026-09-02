"""Typed contracts shared by the Voice foreground control plane."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class VoiceWorkAction(str, Enum):
    STATUS = "status"
    QUEUE = "queue"
    CANCEL = "cancel"
    REPLACE = "replace"
    CLARIFY = "clarify"


class VoiceWorkState(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INTERRUPTED = "interrupted"

    @classmethod
    def from_value(cls, value: object) -> VoiceWorkState:
        if isinstance(value, cls):
            return value
        return cls(str(value or cls.RUNNING.value))


class VoiceControlReplyKind(str, Enum):
    QUEUED = "queued"
    PROGRESS = "progress"
    COMPLETED = "completed"
    ERROR = "error"
    SILENCE = "silence"
    LANGUAGE = "language"
    CONFUSED = "confused"
    IDLE = "idle"


class VoiceWorkUiStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    FAILED = "failed"


class VoiceLanguage(str, Enum):
    ZH = "zh"
    KO = "ko"
    JA = "ja"
    EN = "en"

    @classmethod
    def detect(cls, text: str) -> VoiceLanguage:
        if re.search(r"[\u4e00-\u9fff]", text):
            return cls.ZH
        if re.search(r"[\uac00-\ud7af]", text):
            return cls.KO
        if re.search(r"[\u3040-\u30ff]", text):
            return cls.JA
        return cls.EN


@dataclass(frozen=True)
class VoiceWorkContext:
    request: str
    state: VoiceWorkState = VoiceWorkState.RUNNING
    assistant_output: str = ""
    superseded_by: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", VoiceWorkState.from_value(self.state))


@dataclass(frozen=True)
class VoiceWorkDecision:
    action: VoiceWorkAction
    reply: str = ""
    superseded_by: str = ""
