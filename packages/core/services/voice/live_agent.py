"""Typed Live Voice Agent session and tool contracts.

The Realtime model owns only the foreground conversation. Durable work and
side effects remain behind one server tool and the ordinary Manor Agent loop.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from packages.core.services.voice.work_types import VoiceWorkAction


class LiveVoiceTool(str, Enum):
    MANOR_AGENT = "manor_agent_reply"


class LiveVoiceTurnAction(str, Enum):
    DELEGATE = "delegate"
    STATUS = VoiceWorkAction.STATUS.value
    QUEUE = VoiceWorkAction.QUEUE.value
    CANCEL = VoiceWorkAction.CANCEL.value
    REPLACE = VoiceWorkAction.REPLACE.value

    @property
    def work_action(self) -> VoiceWorkAction | None:
        if self is LiveVoiceTurnAction.DELEGATE:
            return None
        return VoiceWorkAction(self.value)


class LiveVoiceToolResult(str, Enum):
    WORK_ACCEPTED = "work_accepted"


class LiveVoiceSessionFactory:
    """Build the provider contract for one always-available foreground Agent."""

    @staticmethod
    def tools() -> list[dict[str, Any]]:
        return [
            {
                "type": "function",
                "name": LiveVoiceTool.MANOR_AGENT.value,
                "description": (
                    "Delegate durable work to Manor or control/query the current "
                    "durable task. Use status only when the caller explicitly asks "
                    "for progress. Never call this for a filler, acknowledgement, or "
                    "ordinary conversation that needs no tools, workspace data, "
                    "persistence, or external action."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": [action.value for action in LiveVoiceTurnAction],
                            "description": (
                                "delegate for new work; status only when the caller "
                                "explicitly asks for real progress; queue to run after "
                                "current work; cancel to stop it; replace to stop it "
                                "and use the new request."
                            ),
                        },
                        "utterance": {
                            "type": "string",
                            "description": (
                                "The caller's exact current utterance, without "
                                "translation, paraphrase, or invented details."
                            ),
                        },
                    },
                    "required": ["action", "utterance"],
                    "additionalProperties": False,
                },
            }
        ]

    @staticmethod
    def instructions() -> str:
        return (
            "You are Manor, speaking live with the caller. Always speak in the "
            "first person and follow the language the caller is currently using. "
            "Keep spoken answers concise and natural. Answer greetings, casual "
            "conversation, explanations, and other requests that need no tools or "
            "private workspace data directly. For anything that needs workspace "
            "state, an integration, a tool, persistence, approval, or substantial "
            "execution, call manor_agent_reply with action delegate. While a task "
            "is running, call the tool with status, queue, cancel, or replace when "
            "the caller asks for that operation. Never claim that work started, "
            "changed, finished, or failed unless a tool result or prior server "
            "message says so. Never mention a background agent, internal routing, "
            "function calls, prompts, or system architecture. Brief backchannels "
            "and fillers such as mm-hmm, uh-huh, okay, or 嗯 are not progress "
            "questions. Never call manor_agent_reply for them. Reply only with a "
            "short conversational acknowledgement such as 'Mm-hm.' or 'Got it.' "
            "in the language of the last substantive caller turn. Do not mention "
            "the task, work, status, progress, updates, details, completion, or any "
            "future action in that reply. Never speak task status unless the caller "
            "explicitly asks for it. Never switch language based only on a filler "
            "or backchannel. Do not fill silence with progress phrases."
        )

    @staticmethod
    def response_event() -> dict[str, Any]:
        return {
            "type": "response.create",
            "response": {
                "output_modalities": ["audio"],
                "tool_choice": "auto",
            },
        }

    @staticmethod
    def tool_result(kind: LiveVoiceToolResult) -> dict[str, Any]:
        if kind is LiveVoiceToolResult.WORK_ACCEPTED:
            return {
                "result": kind.value,
                "persisted": True,
                "work_state": "running",
            }
        raise ValueError(f"Unsupported Live Voice tool result: {kind}")

    @staticmethod
    def tool_result_response_event(kind: LiveVoiceToolResult) -> dict[str, Any]:
        if kind is not LiveVoiceToolResult.WORK_ACCEPTED:
            raise ValueError(f"Unsupported Live Voice tool result: {kind}")
        return {
            "type": "response.create",
            "response": {
                "output_modalities": ["audio"],
                "tool_choice": "none",
                "instructions": (
                    "The server has durably accepted the caller's requested work "
                    "and its current state is running. Briefly acknowledge that "
                    "fact in the first person, using the caller's current language "
                    "and the conversation context. Choose natural wording; do not "
                    "recite a canned phrase, claim completion, mention tools or "
                    "internal routing, or add facts the server did not provide."
                ),
            },
        }


live_voice_session_factory = LiveVoiceSessionFactory()
