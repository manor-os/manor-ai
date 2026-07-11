from __future__ import annotations

import contextvars
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from packages.core.ai.runtime.output_policy import PREVIOUS_TOOL_ACTIVITY_MARKER


RuntimeToolEventRecorder = Callable[[str, dict[str, Any]], None]
RuntimeEventFormatter = Callable[[str, dict[str, Any]], str]
RuntimeToolArgumentFormatter = Callable[[str, dict[str, Any] | None], Any]
RuntimeToolResultFormatter = Callable[[str, str], Any]
RuntimeToolStatusResolver = Callable[[str], str]
RuntimeToolStartCallback = Callable[[str, dict[str, Any]], None]
RuntimeToolEndCallback = Callable[[str, str, float, dict[str, Any] | None], None]

_TOOL_RESULT_PREVIEW_CHARS = 500
_MEDIA_TOOL_RESULT_MAX_CHARS = 4000
_STREAM_TEXT_FLUSH_CHARS = 120
_STREAM_TEXT_FLUSH_SUFFIXES = (
    "\n",
    "。",
    "！",
    "？",
    "!",
    "?",
    "；",
    ";",
    "：",
    ":",
)
_TOOL_HISTORY_PREVIEW_CHARS = 320
_TOOL_HISTORY_MAX_ITEMS = 12


@dataclass
class RuntimeToolStreamSink:
    """Runtime-owned bridge for nested tool events.

    The sink is intentionally formatter-driven so runtime code does not import
    chat-service SSE helpers. A UI stream, non-stream trace recorder, channel
    adapter, or worker can each install its own formatter/recorder.
    """

    event_queue: Any | None = None
    record_tool_event: RuntimeToolEventRecorder | None = None
    format_event: RuntimeEventFormatter | None = None
    format_tool_arguments: RuntimeToolArgumentFormatter | None = None
    format_tool_result: RuntimeToolResultFormatter | None = None
    resolve_tool_status: RuntimeToolStatusResolver | None = None

    @property
    def active(self) -> bool:
        return self.record_tool_event is not None or (
            self.event_queue is not None and self.format_event is not None
        )

    def _arguments(self, tool_name: str, args: dict[str, Any] | None) -> Any:
        if self.format_tool_arguments is None:
            return args
        return self.format_tool_arguments(tool_name, args)

    def _result(self, tool_name: str, result: str) -> Any:
        if self.format_tool_result is None:
            return result
        return self.format_tool_result(tool_name, result)

    def _status(self, result: str) -> str:
        if self.resolve_tool_status is None:
            return "success"
        return self.resolve_tool_status(result)

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self.event_queue is None or self.format_event is None:
            return
        self.event_queue.put_nowait(self.format_event(event_type, payload))

    def emit_tool_start(self, tool_name: str, args: dict[str, Any] | None) -> None:
        payload = {
            "tool_call": {
                "name": tool_name,
                "arguments": self._arguments(tool_name, args),
                "status": "pending",
            },
        }
        if self.record_tool_event is not None:
            self.record_tool_event("tool_start", payload)
        self._emit("tool_start", payload)

    def emit_tool_end(
        self,
        tool_name: str,
        result: str,
        *,
        duration_ms: float = 0,
        args: dict[str, Any] | None = None,
    ) -> None:
        payload = {
            "tool_call": {
                "name": tool_name,
                "arguments": self._arguments(tool_name, args),
                "result": self._result(tool_name, result),
                "status": self._status(result),
                "duration_ms": int(duration_ms),
            },
        }
        if self.record_tool_event is not None:
            self.record_tool_event("tool_end", payload)
        self._emit("tool_end", payload)

    def emit_delegated_invoke_skill_end(
        self,
        *,
        skill_name: str,
        invoke_skill_args: dict[str, Any],
        active_child: str | None = None,
    ) -> None:
        result = runtime_delegated_invoke_skill_result(
            skill_name=skill_name,
            active_child=active_child,
        )
        payload = {
            "tool_call": {
                "name": "invoke_skill",
                "arguments": self._arguments("invoke_skill", invoke_skill_args),
                "result": self._result("invoke_skill", result),
                "status": self._status(result),
                "duration_ms": 0,
            },
        }
        if self.record_tool_event is not None:
            self.record_tool_event("tool_end", payload)
        self._emit("tool_end", payload)


runtime_tool_stream_sink_var: contextvars.ContextVar[RuntimeToolStreamSink | None] = (
    contextvars.ContextVar("runtime_tool_stream_sink", default=None)
)


def runtime_delegated_invoke_skill_result(
    *,
    skill_name: str,
    active_child: str | None = None,
) -> str:
    return json.dumps({
        "status": "delegated",
        "skill": skill_name,
        "message": (
            "Skill entered its nested tool workflow; child tool "
            "events will stream separately."
        ),
        "active_child": active_child,
    }, ensure_ascii=False)


def runtime_is_media_tool_name(tool_name: str) -> bool:
    name = (tool_name or "").lower()
    return (
        name in {
            "generate_file", "generate_image", "generate_video",
            "wait_media_jobs", "merge_videos", "align_subtitles",
            "normalize_audio_loudness", "compose_video_timeline",
        }
        or name.endswith("generate_image")
        or name.endswith("generate_video")
    )


def runtime_is_local_coding_tool_name(tool_name: str) -> bool:
    name = (tool_name or "").lower()
    return "codex_cli" in name or "claude_code" in name


def runtime_should_flush_stream_text(text: str) -> bool:
    if not text:
        return False
    if len(text) >= _STREAM_TEXT_FLUSH_CHARS:
        return True
    stripped = text.rstrip()
    return bool(stripped) and stripped.endswith(_STREAM_TEXT_FLUSH_SUFFIXES)


def _tool_event_arguments_key(args) -> str:
    if args is None:
        return ""
    try:
        return json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        return str(args)


def runtime_find_pending_tool_event(
    tool_events: list[dict],
    name: str,
    args,
) -> int:
    args_key = _tool_event_arguments_key(args)
    if args is not None:
        for idx in range(len(tool_events) - 1, -1, -1):
            event = tool_events[idx]
            if (
                event.get("name") == name
                and event.get("status") == "pending"
                and _tool_event_arguments_key(event.get("arguments")) == args_key
            ):
                return idx
    for idx in range(len(tool_events) - 1, -1, -1):
        event = tool_events[idx]
        if event.get("name") == name and event.get("status") == "pending":
            return idx
    return -1


def runtime_record_tool_start_for_chat(
    tool_events: list[dict],
    name: str,
    args,
) -> None:
    entry = {"name": name or "tool", "status": "pending"}
    if args is not None:
        entry["arguments"] = args
    tool_events.append(entry)


def runtime_record_tool_end_for_chat(
    tool_events: list[dict],
    name: str,
    *,
    args=None,
    result: str | None = None,
    status: str | None = None,
    duration_ms: float | int | None = None,
) -> None:
    entry = {
        "name": name or "tool",
        "result": result,
        "status": status or runtime_tool_status_for_chat(result or ""),
    }
    if args is not None:
        entry["arguments"] = args
    if duration_ms is not None:
        try:
            entry["duration_ms"] = int(duration_ms)
        except Exception:
            pass
    idx = runtime_find_pending_tool_event(tool_events, entry["name"], args)
    if idx >= 0:
        tool_events[idx] = {**tool_events[idx], **entry}
    else:
        tool_events.append(entry)


def _persisted_tool_call_items(raw) -> list[dict]:
    if not raw:
        return []
    calls = raw if isinstance(raw, list) else (raw.get("calls") or [])
    return [c for c in calls if isinstance(c, dict)]


def _tool_call_name_for_history(call: dict) -> str:
    fn = call.get("function") or {}
    if isinstance(fn, dict) and fn.get("name"):
        return str(fn.get("name"))
    return str(call.get("name") or "tool")


def _tool_call_preview_for_history(call: dict) -> str:
    for key in ("result", "content", "output", "message"):
        value = call.get(key)
        if value:
            break
    else:
        fn = call.get("function") or {}
        value = fn.get("arguments") if isinstance(fn, dict) else call.get("arguments")
        if value:
            value = f"called with {value}"
    if not value:
        return "completed"
    if not isinstance(value, str):
        try:
            value = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            value = str(value)
    name = _tool_call_name_for_history(call).lower()
    if runtime_is_local_coding_tool_name(name):
        try:
            parsed = json.loads(value)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            parts = []
            status = parsed.get("status")
            tool = parsed.get("tool")
            mode = parsed.get("mode")
            cwd = parsed.get("cwd")
            session_id = parsed.get("session_id") or parsed.get("input_session_id")
            step_id = parsed.get("step_id")
            final = parsed.get("final_message")
            changed = parsed.get("changed_files")
            if status:
                parts.append(f"status={status}")
            if tool:
                parts.append(f"tool={tool}")
            if mode:
                parts.append(f"mode={mode}")
            if cwd:
                parts.append(f"cwd={cwd}")
            if session_id:
                parts.append(f"session_id={session_id}")
            if step_id:
                parts.append(f"step_id={step_id}")
            if isinstance(changed, list) and changed:
                parts.append(
                    "changed_files="
                    + ", ".join(str(item) for item in changed[:8])
                )
            if final:
                parts.append(f"final_message={final}")
            if parts:
                return " | ".join(parts)[:_TOOL_HISTORY_PREVIEW_CHARS]
    compact = " ".join(str(value).split())
    if len(compact) > _TOOL_HISTORY_PREVIEW_CHARS:
        compact = compact[:_TOOL_HISTORY_PREVIEW_CHARS] + "..."
    return compact


def runtime_persisted_tool_calls_history_summary(raw) -> str | None:
    """Render persisted tool-call previews as plain assistant history text."""

    calls = _persisted_tool_call_items(raw)
    if not calls:
        return None

    out: list = []
    for call in calls[:_TOOL_HISTORY_MAX_ITEMS]:
        out.append(
            f"- {_tool_call_name_for_history(call)}: {_tool_call_preview_for_history(call)}"
        )
    omitted = len(calls) - len(out)
    if omitted > 0:
        out.append(f"- ... {omitted} more tool result(s) omitted")
    return f"{PREVIOUS_TOOL_ACTIVITY_MARKER}\n" + "\n".join(out)


def _short_text(value, *, max_chars: int) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n... [truncated]"


def _compact_code_event(event: dict) -> dict:
    keep = {
        key: event[key]
        for key in (
            "seq", "source", "type", "raw_type", "raw_subtype", "status",
            "tool_name", "session_id", "metrics",
        )
        if key in event
    }
    message = _short_text(event.get("message"), max_chars=900)
    if message:
        keep["message"] = message
    return keep


def _compact_local_coding_tool_result(result: str) -> str | None:
    try:
        parsed = json.loads(result)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    tool = str(parsed.get("tool") or "").lower()
    has_coding_shape = (
        tool in {"codex_cli", "claude_code"}
        or parsed.get("session_id")
        or parsed.get("input_session_id")
        or parsed.get("diff")
        or parsed.get("diff_stat")
        or parsed.get("changed_files")
        or parsed.get("events")
    )
    if not has_coding_shape:
        return None

    compact: dict = {}
    for key in (
        "status", "error", "fix", "tool", "mode", "argv", "exit_code",
        "events_format", "session_id", "input_session_id", "resume",
        "new_session", "cwd", "root_id", "step_id",
        "session_binding_key", "auto_resumed", "write_blocked",
        "cancel_requested", "last_status", "target_policy",
        "scratch_workspace",
    ):
        if key in parsed and parsed.get(key) is not None:
            compact[key] = parsed.get(key)
    for key, limit in (
        ("final_message", 4000),
        ("stdout", 8000),
        ("stderr", 8000),
        ("diff", 50_000),
        ("diff_stat", 12_000),
    ):
        text = _short_text(parsed.get(key), max_chars=limit)
        if text:
            compact[key] = text
    changed = parsed.get("changed_files")
    if isinstance(changed, list):
        compact["changed_files"] = changed[:80]
        if len(changed) > 80:
            compact["omitted_changed_files"] = len(changed) - 80
    vcs = parsed.get("vcs")
    if isinstance(vcs, dict):
        compact["vcs"] = {
            key: vcs[key]
            for key in (
                "type", "available", "repo_root", "dirty_before",
                "dirty_after", "changed_files_before", "error",
            )
            if key in vcs
        }
    events = parsed.get("events")
    if isinstance(events, list):
        compact_events = [
            _compact_code_event(event)
            for event in events[-80:]
            if isinstance(event, dict)
        ]
        compact["events"] = compact_events
        if len(events) > len(compact_events):
            compact["omitted_events"] = len(events) - len(compact_events)
    return json.dumps(compact, ensure_ascii=False)


def _looks_like_media_result(value: dict) -> bool:
    if value.get("kind") in {"image", "video", "audio", "subtitle", "media_jobs"}:
        return True
    return any(
        key in value
        for key in ("image_url", "image_urls", "job_id", "video_url", "audio_url", "result_url")
    )


def _compact_media_jobs_tool_result(parsed: dict) -> str:
    jobs = parsed.get("jobs") if isinstance(parsed.get("jobs"), list) else []
    compact_jobs = []
    for job in jobs[:20]:
        if not isinstance(job, dict):
            continue
        compact_jobs.append({
            key: job[key]
            for key in (
                "job_id", "kind", "status", "document_id", "result_url",
                "fs_path", "duration_seconds", "file_size", "model", "error",
            )
            if key in job
        })
    compact = {
        "kind": parsed.get("kind"),
        "status": parsed.get("status"),
        "jobs": compact_jobs,
    }
    if len(jobs) > len(compact_jobs):
        compact["omitted_jobs"] = len(jobs) - len(compact_jobs)
    for key in (
        "missing_job_ids", "pending_job_ids", "failed_job_ids",
        "completed_count", "total_count", "timed_out",
    ):
        if key in parsed:
            compact[key] = parsed.get(key)
    return json.dumps(compact, ensure_ascii=False)


def _compact_media_tool_result(result: str) -> str | None:
    try:
        parsed = json.loads(result)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    if parsed.get("kind") == "media_jobs":
        return _compact_media_jobs_tool_result(parsed)
    if not _looks_like_media_result(parsed):
        return None
    if len(result) <= _MEDIA_TOOL_RESULT_MAX_CHARS:
        return result

    keep_keys = (
        "kind", "status", "job_id", "id", "document_id", "image_url", "image_urls",
        "result_url", "video_url", "url", "prompt", "name", "size", "model",
        "duration", "duration_seconds", "resolution", "aspect_ratio", "fps",
        "purpose", "voice", "format", "audio_url",
        "file_size", "fs_path", "saved_to_knowledge", "credits",
        "credits_estimate", "error", "message",
    )
    compact = {key: parsed[key] for key in keep_keys if key in parsed}
    if isinstance(compact.get("prompt"), str) and len(compact["prompt"]) > 280:
        compact["prompt"] = compact["prompt"][:280] + "..."
    return json.dumps(compact, ensure_ascii=False)


def runtime_tool_result_for_chat(tool_name: str, result: str) -> str:
    """Return a UI-safe tool result without breaking media preview JSON."""

    result = result if isinstance(result, str) else str(result)
    if runtime_is_local_coding_tool_name(tool_name):
        coding = _compact_local_coding_tool_result(result)
        if coding is not None:
            return coding
    if runtime_is_media_tool_name(tool_name):
        media = _compact_media_tool_result(result)
        if media is not None:
            return media
    return result[:_TOOL_RESULT_PREVIEW_CHARS] if len(result) > _TOOL_RESULT_PREVIEW_CHARS else result


def _short_arg_value(value, *, max_chars: int = 240):
    if isinstance(value, str):
        text = " ".join(value.split())
        return text if len(text) <= max_chars else text[: max_chars - 1] + "\u2026"
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_short_arg_value(item, max_chars=max_chars) for item in value[:10]]
    if isinstance(value, dict):
        return {
            str(key): _short_arg_value(item, max_chars=max_chars)
            for key, item in value.items()
            if str(key) not in {"prompt", "content", "input", "instructions", "messages"}
        }
    return str(value)[:max_chars]


def runtime_tool_arguments_for_chat(tool_name: str, args: dict | None) -> dict | None:
    """Return compact tool args for SSE/UI display."""

    if not isinstance(args, dict):
        return None

    name = (tool_name or "").lower()
    if name == "generate_file":
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        keep_keys = (
            "kind", "name", "path", "output_name", "filename", "file_type",
            "aspect_ratio", "duration", "resolution", "purpose", "voice",
            "response_format", "save_to_knowledge",
        )
        compact = {key: args[key] for key in keep_keys if args.get(key) is not None}
        params_keep = {
            key: params[key]
            for key in (
                "name", "path", "output_name", "filename", "file_type",
                "aspect_ratio", "duration", "resolution", "generate_audio",
                "purpose", "voice", "response_format", "save_to_knowledge",
            )
            if params.get(key) is not None
        }
        if params_keep:
            compact["params"] = params_keep
        return compact or None

    if name == "invoke_skill":
        skill = args.get("skill") or args.get("slug") or args.get("name")
        return {"skill": skill} if skill else None

    if name == "manor":
        params = args.get("params") if isinstance(args.get("params"), dict) else {}
        compact_params = {
            key: _short_arg_value(params[key], max_chars=160)
            for key in (
                "name", "folder_name", "folder_path", "path", "query",
                "document_id", "folder_id", "parent_id",
            )
            if params.get(key) is not None
        }
        compact = {"action": args.get("action")}
        if compact_params:
            compact["params"] = compact_params
        return {key: value for key, value in compact.items() if value} or None

    redacted_keys = {"prompt", "content", "input", "instructions", "messages"}
    compact = {
        str(key): _short_arg_value(value)
        for key, value in args.items()
        if str(key) not in redacted_keys
    }
    return compact or None


def runtime_tool_status_for_chat(result: str) -> str:
    """Infer UI status from a structured tool result."""

    try:
        parsed = json.loads(result if isinstance(result, str) else str(result))
    except Exception:
        return "error" if str(result).startswith("Tool error") else "success"
    if not isinstance(parsed, dict):
        return "success"
    status = str(parsed.get("status") or "").lower()
    if status in {
        "error", "failed", "timeout", "waiting_human",
        "rejected", "blocked", "cancelled", "canceled",
    } or parsed.get("error"):
        return "error"
    return "success"


def runtime_skill_nested_tool_callbacks(
    *,
    skill_name: str,
    invoke_skill_args: dict[str, Any],
) -> tuple[RuntimeToolStartCallback | None, RuntimeToolEndCallback | None]:
    """Return callbacks that stream child tool events for nested skill runs."""

    sink = runtime_tool_stream_sink_var.get(None)
    if sink is None or not sink.active:
        return None, None

    outer_invoke_skill_closed = False

    def close_outer_invoke_skill_once(first_child: str | None = None) -> None:
        nonlocal outer_invoke_skill_closed
        if outer_invoke_skill_closed:
            return
        outer_invoke_skill_closed = True
        sink.emit_delegated_invoke_skill_end(
            skill_name=skill_name,
            invoke_skill_args=invoke_skill_args,
            active_child=first_child,
        )

    def on_sub_tool_start(name: str, args: dict[str, Any]) -> None:
        close_outer_invoke_skill_once(name)
        sink.emit_tool_start(name, args)

    def on_sub_tool_end(
        name: str,
        result: str,
        duration_ms: float = 0,
        args: dict[str, Any] | None = None,
    ) -> None:
        sink.emit_tool_end(
            name,
            result,
            duration_ms=duration_ms,
            args=args,
        )

    return on_sub_tool_start, on_sub_tool_end
