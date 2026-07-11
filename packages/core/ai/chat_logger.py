"""Structured logging for every LLM chat request lifecycle.

Logs a structured JSON line at each phase:
  - chat.request   — incoming request with user/entity/agent context
  - chat.context    — resolved prompt, tools, history
  - chat.llm_call   — each LLM API call (round N)
  - chat.tool_exec  — each tool execution
  - chat.complete   — final result with usage/timing
  - chat.error      — any failure

All log entries share a `trace_id` for correlation.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("manor.chat")


@dataclass
class ChatTrace:
    """Tracks a single chat request through its lifecycle."""

    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    entity_id: str | None = None
    user_id: str | None = None
    agent_id: str | None = None
    workspace_id: str | None = None
    conversation_id: str | None = None
    started_at: float = field(default_factory=time.time)

    # Accumulated during the request
    tool_names_loaded: list[str] = field(default_factory=list)
    tool_calls_made: list[str] = field(default_factory=list)
    llm_rounds: int = 0
    total_usage: dict[str, Any] = field(default_factory=dict)

    def _base(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "entity_id": self.entity_id,
            "user_id": self.user_id,
            "agent_id": self.agent_id,
            "workspace_id": self.workspace_id,
            "conversation_id": self.conversation_id,
        }

    # ── Lifecycle events ──

    def log_request(
        self, message_preview, has_files: bool = False,
    ) -> None:
        # ``message_preview`` may be a plain str or a multimodal content
        # array (list of {type, text, image_url}) when the user attached
        # images. Coerce to a short string for telemetry, and auto-set
        # has_files when image blocks are present.
        if isinstance(message_preview, list):
            text_bits: list[str] = []
            image_count = 0
            for part in message_preview:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "image_url":
                    image_count += 1
                else:
                    text_bits.append(str(part.get("text") or ""))
            preview = " ".join(text_bits)[:200]
            if image_count:
                preview = f"{preview} [+{image_count} image(s)]".strip()
                has_files = True
        else:
            preview = (message_preview or "")[:200]

        logger.info(
            "chat.request %s",
            {
                **self._base(),
                "phase": "request",
                "message_preview": preview,
                "has_files": has_files,
            },
        )

    def log_context(
        self,
        *,
        agent_name: str | None = None,
        user_name: str | None = None,
        entity_name: str | None = None,
        tool_count: int = 0,
        tool_names: list[str] | None = None,
        history_count: int = 0,
        prompt_length: int = 0,
        agent_files_loaded: dict[str, str] | None = None,
        prompt_source: str | None = None,
        model: str | None = None,
        user_timezone: str | None = None,
    ) -> None:
        self.tool_names_loaded = tool_names or []
        logger.info(
            "chat.context %s",
            {
                **self._base(),
                "phase": "context",
                "agent_name": agent_name,
                "user_name": user_name,
                "entity_name": entity_name,
                "model": model,
                "user_timezone": user_timezone,
                "tool_count": tool_count,
                "tool_names": self.tool_names_loaded,
                "history_messages": history_count,
                "system_prompt_chars": prompt_length,
                "prompt_source": prompt_source,
                "agent_files_loaded": agent_files_loaded,
            },
        )

    def log_llm_call(
        self,
        *,
        round_num: int,
        model: str | None = None,
        message_count: int = 0,
        tool_count: int = 0,
        duration_ms: float = 0,
        usage: dict[str, Any] | None = None,
        finish_reason: str | None = None,
        tool_calls: list[str] | None = None,
    ) -> None:
        self.llm_rounds = round_num
        logger.info(
            "chat.llm_call %s",
            {
                **self._base(),
                "phase": "llm_call",
                "round": round_num,
                "model": model,
                "message_count": message_count,
                "tool_count": tool_count,
                "duration_ms": round(duration_ms, 1),
                "usage": usage or {},
                "finish_reason": finish_reason,
                "tool_calls_requested": tool_calls or [],
            },
        )

    def log_tool_exec(
        self,
        *,
        round_num: int,
        tool_name: str,
        duration_ms: float = 0,
        result_length: int = 0,
        result_preview: str | None = None,
        success: bool = True,
        error: str | None = None,
        tool_args: dict[str, Any] | None = None,
    ) -> None:
        self.tool_calls_made.append(tool_name)
        level = logging.INFO if success else logging.WARNING
        extra: dict[str, Any] = {}
        # Capture skill name for invoke_skill calls
        if tool_name == "invoke_skill" and tool_args:
            extra["skill_name"] = tool_args.get("skill", "")
        # Include args (truncate large values for readability)
        safe_args: dict[str, Any] | None = None
        if tool_args:
            safe_args = {}
            for k, v in tool_args.items():
                if k.startswith("_"):
                    continue  # skip internal context keys
                sv = str(v)
                safe_args[k] = sv[:200] if len(sv) > 200 else v
            extra["tool_args"] = safe_args
        logger.log(
            level,
            "chat.tool_exec %s",
            {
                **self._base(),
                "phase": "tool_exec",
                "round": round_num,
                "tool_name": tool_name,
                "duration_ms": round(duration_ms, 1),
                "result_chars": result_length,
                "result_preview": (result_preview or "")[:300] if result_preview else None,
                "success": success,
                "error": error,
                **extra,
            },
        )

        # Fire-and-forget DB persist — same fields, append-only row in
        # ``tool_call_logs``. Best-effort; never blocks the chat loop.
        if self.entity_id:
            self._schedule_tool_call_record(
                tool_name=tool_name,
                round_num=round_num,
                duration_ms=int(duration_ms),
                result_chars=int(result_length or 0),
                success=success,
                error=error,
                tool_args=safe_args,
            )

    def _schedule_tool_call_record(
        self,
        *,
        tool_name: str,
        round_num: int,
        duration_ms: int,
        result_chars: int,
        success: bool,
        error: str | None,
        tool_args: dict[str, Any] | None,
    ) -> None:
        """Persist a tool-execution row to ``tool_call_logs``. Best-effort
        + fire-and-forget so it can't slow the chat loop."""
        snap = {
            "entity_id": self.entity_id,
            "workspace_id": self.workspace_id,
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "tool_name": tool_name,
            "round_num": round_num,
            "duration_ms": duration_ms,
            "result_chars": result_chars,
            "success": success,
            "error": error,
            "tool_args": tool_args,
            "source": "chat",
        }

        async def _write():
            from packages.core.database import async_session
            from packages.core.services.usage_service import record_tool_call
            async with async_session() as db:
                await record_tool_call(db, **snap)
                await db.commit()

        from packages.core.ai.llm_client import fire_and_forget
        fire_and_forget(_write, label="tool_call_log persist")

    def log_complete(self, *, result: Any = None) -> None:
        elapsed_ms = (time.time() - self.started_at) * 1000
        logger.info(
            "chat.complete %s",
            {
                **self._base(),
                "phase": "complete",
                "elapsed_ms": round(elapsed_ms, 1),
                "rounds": self.llm_rounds,
                "tools_loaded": len(self.tool_names_loaded),
                "tool_calls_made": self.tool_calls_made,
                "total_tool_calls": len(self.tool_calls_made),
                "usage": self.total_usage,
                "stop_reason": getattr(result, "stop_reason", None),
                "response_chars": len(getattr(result, "content", "") or ""),
            },
        )

    def log_error(self, error: str, *, phase: str = "unknown") -> None:
        elapsed_ms = (time.time() - self.started_at) * 1000
        logger.error(
            "chat.error %s",
            {
                **self._base(),
                "phase": phase,
                "elapsed_ms": round(elapsed_ms, 1),
                "error": error,
            },
        )
