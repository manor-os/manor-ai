from __future__ import annotations

import contextvars
import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from packages.core.ai.provider_retry import withhold_stream_events
from packages.core.ai.runtime.output_policy import (
    PREVIOUS_TOOL_ACTIVITY_MARKER,
    _runtime_is_file_read_content_payload,
    runtime_public_transport_payload,
    runtime_public_tool_result,
    runtime_is_internal_failure_detail,
    runtime_strip_leaked_tool_activity,
)
from packages.core.ai.runtime.control import runtime_sequence_child_owner
from packages.core.ai.runtime.provider_approvals import (
    normalize_provider_approval,
    normalize_provider_approval_resolution,
    provider_tool_result_for_persistence,
)
from packages.core.ai.runtime.tool_results import RuntimeAlternatePathToolResult
from packages.core.services.sensitive_data import sanitize_approval_credentials, sanitize_sensitive_payload
from packages.core.constants.hitl_envelope import parse_hitl_envelope


RuntimeToolEventRecorder = Callable[[str, dict[str, Any]], None]
RuntimeSubAgentEventRecorder = Callable[[dict[str, Any]], None]
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
    record_sub_agent_event: RuntimeSubAgentEventRecorder | None = None
    format_event: RuntimeEventFormatter | None = None
    format_tool_arguments: RuntimeToolArgumentFormatter | None = None
    format_tool_result: RuntimeToolResultFormatter | None = None
    resolve_tool_status: RuntimeToolStatusResolver | None = None
    record_progress: Callable[[dict[str, Any]], None] | None = None

    @property
    def active(self) -> bool:
        return (
            self.record_tool_event is not None
            or self.record_sub_agent_event is not None
            or self.record_progress is not None
            or (
                self.event_queue is not None and self.format_event is not None
            )
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
        self.event_queue.put_nowait(
            self.format_event(event_type, runtime_public_transport_payload(payload))
        )

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
        formatted_arguments = self._arguments(tool_name, args)
        tool_call = {
            "name": tool_name,
            "arguments": formatted_arguments,
            "result": self._result(tool_name, result),
            "status": self._status(result),
            "duration_ms": int(duration_ms),
        }
        payload = {
            "tool_call": tool_call,
        }
        if self.record_tool_event is not None:
            recorded_tool_call = dict(tool_call)
            # The chat recorder needs the unformatted result to detect and
            # persist nested HITL payloads.  Keep it off the SSE payload so
            # large or sensitive provider responses are never exposed to the
            # browser merely to support durable server-side bookkeeping.
            recorded_tool_call["raw_result"] = provider_tool_result_for_persistence(
                tool_name,
                result,
            )
            provider_approval = normalize_provider_approval(
                tool_name,
                args,
                result,
            )
            if provider_approval is not None:
                recorded_tool_call["provider_approval"] = provider_approval
            provider_approval_resolution = normalize_provider_approval_resolution(
                tool_name,
                args,
                result,
            )
            if provider_approval_resolution is not None:
                recorded_tool_call["provider_approval_resolution"] = provider_approval_resolution
            recorded_payload = {"tool_call": recorded_tool_call}
            self.record_tool_event("tool_end", recorded_payload)
            # Only host-owned chat projection fields may cross into SSE. The
            # recorder can retain arbitrary bookkeeping beside tool_call, but
            # those fields remain server-side.
            assistant_blocks = recorded_payload.get("assistant_blocks")
            if isinstance(assistant_blocks, list):
                payload["assistant_blocks"] = assistant_blocks
            assistant_blocks_schema = recorded_payload.get(
                "assistant_blocks_schema"
            )
            if isinstance(assistant_blocks_schema, str):
                payload["assistant_blocks_schema"] = assistant_blocks_schema
        self._emit("tool_end", payload)

    def emit_sub_agent_event(self, event: dict[str, Any]) -> None:
        """Emit one delegated Agent lifecycle update to chat consumers."""

        payload = {"sub_agent": dict(event)}
        if self.record_sub_agent_event is not None:
            self.record_sub_agent_event(dict(event))
        self._emit("sub_agent", payload)

    def emit_progress(self, text: str) -> None:
        from packages.core.ai.agentic_loop import FINAL_RESPONSE_SENTINELS

        note = runtime_strip_leaked_tool_activity(text).strip()
        if not note or any(marker in note for marker in FINAL_RESPONSE_SENTINELS) or runtime_is_internal_failure_detail(note):
            return
        payload = {"content": sanitize_sensitive_payload(sanitize_approval_credentials(note))}
        if self.record_progress is not None:
            recorded = dict(payload)
            self.record_progress(recorded)
            for key in ("assistant_blocks", "assistant_blocks_schema"):
                if key in recorded:
                    payload[key] = recorded[key]
        self._emit("process_note", payload)

runtime_tool_stream_sink_var: contextvars.ContextVar[RuntimeToolStreamSink | None] = (
    contextvars.ContextVar("runtime_tool_stream_sink", default=None)
)


def runtime_is_media_tool_name(tool_name: str) -> bool:
    name = (tool_name or "").lower()
    return (
        name in {
            "generate_file", "generate_image", "generate_video", "import_free_music",
            "wait_media_jobs", "merge_videos", "align_subtitles",
            "normalize_audio_loudness",
            "compose_video_timeline",
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


def runtime_record_sub_agent_event_for_chat(
    events: list[dict],
    event: dict[str, Any],
) -> None:
    """Merge delegated Agent updates into one durable run record."""

    if not isinstance(event, dict):
        return
    run_id = str(event.get("run_id") or "").strip()
    if not run_id:
        return

    idx = next(
        (
            index
            for index, existing in enumerate(events)
            if isinstance(existing, dict) and existing.get("run_id") == run_id
        ),
        -1,
    )
    current = dict(events[idx]) if idx >= 0 else {"run_id": run_id, "tools": []}
    incoming_tool = event.get("tool")
    for key, value in event.items():
        if key != "tool" and value is not None:
            current[key] = value

    tools = [
        dict(tool)
        for tool in current.get("tools", [])
        if isinstance(tool, dict)
    ]
    if isinstance(incoming_tool, dict):
        tool = dict(incoming_tool)
        tool_seq = tool.get("seq")
        tool_idx = next(
            (
                index
                for index, existing in enumerate(tools)
                if tool_seq is not None and existing.get("seq") == tool_seq
            ),
            -1,
        )
        if tool_idx >= 0:
            tools[tool_idx] = {**tools[tool_idx], **tool}
        else:
            tools.append(tool)
        current["tools"] = tools[-20:]

    if idx >= 0:
        events[idx] = current
    else:
        events.append(current)


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


def _compact_native_file_patch_result(result: str) -> str | None:
    """Keep the editor reload receipt when a patch result has large details."""

    try:
        parsed = json.loads(result)
    except Exception:
        return None
    if (
        not isinstance(parsed, dict)
        or parsed.get("error")
        or parsed.get("patched") is not True
        or not isinstance(parsed.get("path"), str)
        or not parsed["path"]
    ):
        return None
    keep_keys = (
        "patched",
        "path",
        "document_id",
        "source_sha256",
        "knowledge_synced",
    )
    return json.dumps(
        {key: parsed[key] for key in keep_keys if key in parsed},
        ensure_ascii=False,
    )


def runtime_tool_result_for_chat(tool_name: str, result: Any) -> str:
    """Return a UI-safe tool result without breaking media preview JSON."""

    if runtime_sequence_child_owner(tool_name, result) is not None:
        return str(result)
    result = (
        json.dumps(result, ensure_ascii=False, default=str)
        if isinstance(result, (dict, list))
        else result if isinstance(result, str) else str(result)
    )
    if tool_name.startswith("mcp__chrome__"):
        from packages.core.ai.runtime.chrome_action_outcomes import CONTRACT, pending_action_receipt

        try:
            chrome_result = json.loads(result)
        except (ValueError, TypeError):
            chrome_result = None
        if pending_action_receipt(chrome_result):
            return json.dumps({"status": "unverified", "message": (
                "The action ran. Its result still needs page verification."
                if chrome_result["action_receipt"]["execution"] in {"executed", "acknowledged"}
                else "Execution could not be confirmed. Inspect the page before trying again."
            )})
        proof = chrome_result.get("action_verification") if isinstance(chrome_result, dict) else None
        if isinstance(proof, dict) and proof.get("contract") == CONTRACT:
            return json.dumps({"status": proof.get("status"), "message": (
                "The expected page condition was confirmed." if proof.get("status") in {"verified", "already_verified"}
                else "The page confirms that the action did not achieve the requested result." if proof.get("status") == "failed"
                else "The page was read, but it does not yet confirm the action's result."
            )})
    provider_resolution = normalize_provider_approval_resolution(
        tool_name,
        None,
        result,
    )
    if provider_resolution is not None:
        return json.dumps(
            {
                "ok": True,
                "status": "approved",
                **provider_resolution,
            },
            ensure_ascii=False,
        )
    result = str(sanitize_approval_credentials(result))
    if tool_name == "generate_file":
        handoff = _runtime_tool_result_payload(result)
        if isinstance(handoff, dict) and handoff.get("status") == "sandbox_ready":
            # Preparing an Office Skill is not a generated-file receipt. Keep
            # that state parseable even when its instructions are very long.
            return json.dumps({key: handoff[key] for key in ("status", "skill") if key in handoff}, ensure_ascii=False)
    if _runtime_command_exit_code(result) not in {None, 0}:
        result = runtime_tool_call_error(result) or result
    public_result = (
        result
        if tool_name == "read_file" and _runtime_is_file_read_content_payload(result)
        else runtime_public_tool_result(result)
    )
    if public_result != result:
        envelope = parse_hitl_envelope(result)
        hitl = envelope.get("hitl") if envelope is not None else None
        if isinstance(hitl, dict) and hitl.get("type") == "approval":
            # Preserve the gate identity when the generic redactor hides the
            # private continuation. Dedicated public review payloads stay intact.
            return json.dumps({
                "__hitl__": True,
                "status": "approval_required",
                "hitl": {"id": hitl.get("id"), "type": "approval"},
                "message": "Waiting for your approval. The action has not run.",
            })
        return str(public_result)
    if runtime_is_local_coding_tool_name(tool_name):
        coding = _compact_local_coding_tool_result(result)
        if coding is not None:
            return coding
    if runtime_is_media_tool_name(tool_name):
        media = _compact_media_tool_result(result)
        if media is not None:
            return media
    if tool_name == "patch_file":
        native_patch = _compact_native_file_patch_result(result)
        if native_patch is not None:
            return native_patch
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
            if str(key).strip().lower().replace("-", "_")
            not in {"approval_token", "approvaltoken"}
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
            "response_format", "save_to_knowledge", "storage_scope",
        )
        compact = {key: args[key] for key in keep_keys if args.get(key) is not None}
        params_keep = {
            key: params[key]
            for key in (
                "name", "path", "output_name", "filename", "file_type",
                "aspect_ratio", "duration", "resolution", "generate_audio",
                "purpose", "voice", "response_format", "save_to_knowledge", "storage_scope",
            )
            if params.get(key) is not None
        }
        # Keep the bounded source identity needed to match conversion retries;
        # never include document content, arbitrary source fields or credentials.
        for source_args, destination in ((args, compact), (params, params_keep)):
            source = source_args.get("source")
            if not isinstance(source, dict):
                continue
            path, digest = source.get("path"), source.get("expected_sha256")
            if (
                isinstance(path, str) and 0 < len(path) <= 1000
                and isinstance(digest, str) and len(digest) == 64
                and all(char in "0123456789abcdefABCDEF" for char in digest)
            ):
                destination["source"] = {"path": path, "expected_sha256": digest.lower()}
        if params_keep:
            compact["params"] = params_keep
        return compact or None

    if name == "invoke_skill":
        # The registered tool's public schema uses ``skill`` (name/slug). Keep
        # accepting the internal ``skill_id`` spelling for older callers, but
        # render the stable public key in chat previews.
        skill = args.get("skill") or args.get("skill_id")
        return {"skill": skill} if skill else None

    from packages.core.ai.runtime.composite_tools import (
        RuntimeCompositeToolCallFactory,
    )

    canonical_call = RuntimeCompositeToolCallFactory.create(name, args)
    canonical_args = canonical_call.arguments
    if canonical_call.tool_name == "workspace_agent":
        workspace_action = str(canonical_args.get("action") or "").strip()
        params = (
            canonical_args.get("params")
            if isinstance(canonical_args.get("params"), dict)
            else {}
        )
        compact_params = {
            key: _short_arg_value(params[key], max_chars=160)
            for key in (
                "query",
                "category",
                "status",
                "name",
                "title",
                "task_id",
                "goal_id",
                "document_id",
                "document_ids",
                "folder_id",
                "folder_name",
                "folder_path",
                "path",
                "contract_id",
                "service_key",
                "agent_subscription_id",
                "agent_id",
                "max_rounds",
                "request_id",
                "hitl_id",
                "blocker_id",
            )
            if params.get(key) is not None
        }
        if workspace_action == "delegate_service":
            objective = (
                params.get("prompt")
                or params.get("instructions")
                or params.get("task")
                or params.get("message")
                or params.get("request")
            )
            if objective:
                compact_params["objective"] = _short_arg_value(objective, max_chars=240)
        workspace_args: dict[str, Any] = {"action": workspace_action}
        if compact_params:
            workspace_args["params"] = compact_params
        if name == "manor":
            return {"action": "workspace", "params": workspace_args}
        return workspace_args

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

    redacted_keys = {
        "prompt",
        "content",
        "input",
        "instructions",
        "messages",
        "approval_token",
        "approvaltoken",
    }
    compact = {
        str(key): _short_arg_value(value)
        for key, value in args.items()
        if str(key).strip().lower().replace("-", "_") not in redacted_keys
    }
    return compact or None


# ── the tool-result error contract ────────────────────────────────
#
# A tool result reaching an ``on_tool_end`` callback is always a string, so
# these two envelopes ARE the contract for "the call blew up". They are
# declared here, next to the classifiers that decode them, and imported by
# the code that produces them (``runtime_execute_prepared_tool_handler`` and
# ``agentic_loop``) so producer and decoder cannot drift apart — the
# handler wrapper's ``Error: ...`` form used to be invisible to every
# classifier precisely because the prefix was written out twice.
RUNTIME_TOOL_ERROR_PREFIX = "Error: "
"""Prefix ``runtime_execute_prepared_tool_handler`` puts on a raised handler."""
RUNTIME_TOOL_EXECUTOR_ERROR_PREFIX = "Tool error"
"""Prefix ``agentic_loop`` puts on a tool executor that raised."""

_RUNTIME_COMMAND_EXIT_CODE_RE = re.compile(
    r"(?:^|\n)\[exit_code:\s*(-?\d+)\]\s*$"
)


def _runtime_command_exit_code(result: str) -> int | None:
    """Decode the plain-text command trailer emitted by ``sandbox_exec``.

    The sandbox API predates the JSON tool-result envelope and intentionally
    keeps stdout readable for the model.  Treat its terminal exit-code marker
    as part of the shared result contract so a failed command cannot be
    persisted or displayed as a successful tool step.
    """

    match = _RUNTIME_COMMAND_EXIT_CODE_RE.search(result)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def runtime_tool_error_result(message: str) -> str:
    """Encode a handler failure in the tool-result error contract."""

    return f"{RUNTIME_TOOL_ERROR_PREFIX}{message}"


def runtime_tool_alternate_path_error_result(result: Any) -> RuntimeAlternatePathToolResult:
    """Carry host-only pre-I/O proof without discarding structured evidence."""

    payload = result if isinstance(result, dict) else None
    if payload is None and isinstance(result, str):
        try:
            parsed = json.loads(result)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            payload = parsed
    if payload is not None:
        return RuntimeAlternatePathToolResult(json.dumps(
            {**payload, "retry_policy": "read_only_alternate_path"},
            ensure_ascii=False,
            default=str,
        ))
    return RuntimeAlternatePathToolResult(json.dumps(
        {
            "status": "error",
            "error": str(result),
            "retry_policy": "read_only_alternate_path",
        },
        ensure_ascii=False,
    ))


def runtime_tool_allows_alternate_path(result: Any) -> bool:
    """Permit read-only alternate work only from host-held pre-I/O proof."""

    if not isinstance(result, RuntimeAlternatePathToolResult):
        return False
    try:
        parsed = json.loads(result)
    except Exception:
        return False
    return (
        isinstance(parsed, dict)
        and parsed.get("retry_policy") == "read_only_alternate_path"
        and runtime_tool_call_error(result) is not None
    )


# Approval outcomes that runtime_tool_status_for_chat collapses into its
# UI-facing "error" bucket, but that say nothing about whether the TOOL/PATH
# itself works — a human declined or the request was otherwise blocked.
_APPROVAL_GATE_STATUSES = frozenset({
    "waiting_human", "rejected", "blocked", "cancelled", "canceled",
    "approval_required", "handoff_required",
})
# True execution outcomes (vs. an approval gate) that DO belong in
# intent-path-memory failure recording.
_EXECUTION_FAILURE_STATUSES = frozenset({"error", "failed", "failure", "timeout"})
RUNTIME_UNFINISHED_TOOL_STATUSES = frozenset({
    "unknown", "pending", "queued", "running", "submitted", "partial", "interrupted",
})


def _runtime_tool_result_payload(result: Any) -> dict[str, Any] | None:
    from packages.core.ai.runtime.tool_results import RuntimeStructuredToolResult

    if isinstance(result, RuntimeStructuredToolResult):
        result = result.structured_content
    if isinstance(result, dict):
        return result
    if not isinstance(result, str):
        return None
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _runtime_tool_control_status(result: Any) -> str | None:
    """Recognize waits/decisions for classification, never for authorization."""
    from packages.core.ai.runtime.control import RuntimeToolSuspension

    if isinstance(result, RuntimeToolSuspension):
        return "hitl_required"
    payload = _runtime_tool_result_payload(result)
    if parse_hitl_envelope(payload if payload is not None else result) is not None:
        return "hitl_required"
    if payload is None:
        return None
    status = str(payload.get("status") or "").strip().lower()
    if status in {"hitl_required", "approval_required"}:
        return "hitl_required"
    if status in _APPROVAL_GATE_STATUSES:
        return status
    if payload.get("approval_required") is True:
        return "hitl_required"
    control = payload.get("control")
    if (
        isinstance(control, dict)
        and control.get("kind") == "approval_required"
        and "blocking_result" in control
    ):
        return "hitl_required"
    return None


def runtime_tool_status_for_chat(result: Any) -> str:
    """Project the shared execution classification into the UI vocabulary."""
    control_status = _runtime_tool_control_status(result)
    if control_status is not None:
        return "success" if control_status == "hitl_required" else "error"
    from packages.core.ai.runtime.chrome_action_outcomes import pending_action_receipt

    if pending_action_receipt(_runtime_tool_result_payload(result)):
        return "unverified"
    return "error" if runtime_tool_call_error(result) is not None else "success"


def runtime_tool_path_memory_outcome(result: Any) -> str | None:
    """Classify a raw tool result for intent-path-memory recording (spec
    §A3) — distinct from runtime_tool_status_for_chat's UI-facing
    success/error split. Spec §A3 explicitly excludes approval denials,
    cancellations, and availability blocks from failure recording ("those
    say nothing about whether the path fits the task"); a `__hitl__`
    interrupt is not a terminal outcome at all yet (recording it as a
    'success' would be premature and would double-count once the human
    actually resolves it). Returns "success", "failure", or None (skip
    recording entirely for this call).
    """
    if _runtime_tool_control_status(result) is not None:
        return None
    from packages.core.ai.runtime.chrome_action_outcomes import pending_action_receipt

    if pending_action_receipt(_runtime_tool_result_payload(result)):
        return None
    parsed = _runtime_tool_result_payload(result)
    if runtime_tool_call_error(result) is not None:
        return "failure"
    if isinstance(parsed, dict) and (parsed.get("success") is False or parsed.get("ok") is False):
        return "failure"
    if isinstance(parsed, dict) and str(parsed.get("status") or "").strip().lower() in RUNTIME_UNFINISHED_TOOL_STATUSES:
        return None
    return "success"


def runtime_tool_call_error(result: Any) -> str | None:
    """Return the error detail a tool result carries, or ``None``.

    The single decoder of the tool-result error contract for durable
    failure analysis (``tool_call_logs.success`` / ``.error``). It shares
    the status vocabulary with ``runtime_tool_path_memory_outcome`` — an
    approval gate or an un-resolved ``__hitl__`` interrupt is not a tool
    failure, only a real execution outcome is — and additionally recovers
    the human-readable detail, which the boolean-only classifiers throw
    away. Callers must not re-implement this check inline; one decoder is
    what keeps every consumer of the contract in agreement.
    """
    if _runtime_tool_control_status(result) is not None:
        return None
    parsed = _runtime_tool_result_payload(result)
    # Structured MCP data is authoritative; its display text can itself
    # contain examples of errors or HITL JSON without changing the outcome.
    text = result if isinstance(result, str) and parsed is None else ""
    if text.startswith(RUNTIME_TOOL_ERROR_PREFIX):
        return text[len(RUNTIME_TOOL_ERROR_PREFIX):].strip() or text
    if text.startswith(RUNTIME_TOOL_EXECUTOR_ERROR_PREFIX):
        return text
    if text.lstrip().startswith("Error invoking skill"):
        return text.strip()
    exit_match = _RUNTIME_COMMAND_EXIT_CODE_RE.search(text)
    exit_code = _runtime_command_exit_code(text)
    if exit_code not in {None, 0} and exit_match is not None:
        detail = text[: exit_match.start()].strip()
        # stdout can be a whole reference document. The executor appends
        # stderr separately; preserve the actual failure before that content.
        stderr_marker = "\n[stderr]\n"
        if stderr_marker in detail:
            stderr = detail.rsplit(stderr_marker, 1)[1].strip()
            if stderr:
                detail = stderr
        elif detail.startswith("[stderr]\n"):
            detail = detail[len("[stderr]\n"):].strip()
        if detail:
            detail = " ".join(detail.split())[-900:]
            return f"Command exited with status {exit_code}: {detail}"
        return f"Command exited with status {exit_code}"
    if parsed is None:
        return None
    from packages.core.ai.runtime.chrome_action_outcomes import pending_action_receipt

    if pending_action_receipt(parsed):
        return None
    status = str(parsed.get("status") or "").strip().lower()
    json_exit_code = parsed.get("exit_code")
    if isinstance(json_exit_code, str) and re.fullmatch(r"-?\d+", json_exit_code.strip()):
        try:
            json_exit_code = int(json_exit_code)
        except ValueError:
            return "Tool returned an invalid exit_code (integer is out of range)"
    command_failed = isinstance(json_exit_code, int) and not isinstance(json_exit_code, bool) and json_exit_code != 0
    if not (
        status in _EXECUTION_FAILURE_STATUSES
        or parsed.get("error")
        or parsed.get("isError") is True
        or parsed.get("ok") is False
        or parsed.get("success") is False
        or command_failed
    ):
        return None
    detail = parsed.get("error") or parsed.get("message") or parsed.get("detail") or parsed.get("reason")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if detail is not None and not isinstance(detail, str):
        return json.dumps(detail, ensure_ascii=False, default=str)
    if command_failed:
        command_detail = str(parsed.get("stderr") or parsed.get("stdout") or "").strip()
        return f"Command exited with status {json_exit_code}" + (f": {command_detail}" if command_detail else "")
    if parsed.get("isError") is True:
        content = parsed.get("content")
        if isinstance(content, list):
            messages = [item["text"] for item in content if isinstance(item, dict) and isinstance(item.get("text"), str)]
            if messages:
                return "\n".join(messages)
    return status if status in _EXECUTION_FAILURE_STATUSES else "Tool call failed"


def runtime_tool_call_outcome(tool_name: str, result: Any) -> str:
    """Classify a tool result into ``"success"`` | ``"error"`` | ``"empty_result"``.

    A real failure always wins — even for ``search_tools`` itself raising,
    which must still count as an error, not get relabeled just because the
    tool name matches. Only a *clean* zero-match ``search_tools`` result is
    ``"empty_result"``; every other tool's "found nothing" case isn't
    representable here and stays ``"success"`` (matches the caller-level
    ``success`` boolean, which was never about result content). Shares the
    single decoder (``runtime_tool_call_error``) so the two never drift on
    what counts as a failure.
    """
    if runtime_tool_call_error(result) is not None:
        return "error"
    if tool_name == "search_tools":
        parsed = _runtime_tool_result_payload(result)
        if isinstance(parsed, dict):
            matches = parsed.get("matches")
            if isinstance(matches, list) and not matches:
                return "empty_result"
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

    def on_sub_tool_start(name: str, args: dict[str, Any]) -> None:
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


def runtime_skill_progress_callbacks(
    on_tool_start: RuntimeToolStartCallback | None,
) -> tuple[RuntimeToolStartCallback | None, Callable[[str, dict[str, Any]], Awaitable[None]] | None]:
    """Release public child commentary only once its next tool is confirmed."""
    sink = runtime_tool_stream_sink_var.get(None)
    if sink is None or not (sink.record_progress or (sink.event_queue is not None and sink.format_event)):
        return on_tool_start, None
    pending_text = ""
    final_started = False

    async def on_stream_event(event_type: str, payload: dict[str, Any]) -> None:
        nonlocal pending_text, final_started
        if event_type in {"text_reset", "final_response_start"}:
            pending_text = ""
            final_started = event_type == "final_response_start"
        elif event_type == "text_delta" and not final_started:
            text = payload.get("content", payload.get("text_delta", payload.get("token", "")))
            if isinstance(text, str):
                pending_text = (pending_text + text)[:4000]
        elif event_type == "process_note" and not final_started:
            text = payload.get("content")
            if isinstance(text, str):
                sink.emit_progress(text[:4000])

    def start_tool(name: str, args: dict[str, Any]) -> None:
        nonlocal pending_text
        text, pending_text = pending_text, ""
        if text:
            sink.emit_progress(text)
        if on_tool_start is not None:
            on_tool_start(name, args)

    # Text is held until the next tool is confirmed (text_reset clears it) and
    # drafts are ignored, so a stream that drops mid-round showed no one.
    withhold_stream_events(on_stream_event, {"text_delta", "tool_call_draft"})
    return start_tool, on_stream_event


def runtime_partial_json_object(text: Any) -> dict[str, Any]:
    """Best-effort object from JSON that is still streaming in.

    Closes an open string and any open containers, dropping a trailing key
    that has no value yet. Display only: never used to execute a call.
    """
    if not isinstance(text, str) or not text.strip():
        return {}
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else {}
    except ValueError:
        pass
    stack: list[str] = []
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack:
            stack.pop()
    candidate = text
    if in_string:
        if escaped:
            candidate = candidate[:-1]
        candidate += '"'
    closers = "".join(reversed(stack))
    for attempt in (candidate, candidate.rstrip().rstrip(",")):
        for tail in ("", "null"):
            try:
                value = json.loads(attempt + tail + closers)
                return value if isinstance(value, dict) else {}
            except ValueError:
                continue
    # A key whose value has not started: drop it and parse what came before.
    cut = max(candidate.rfind(","), candidate.rfind("{"))
    if cut > 0:
        head = candidate[:cut] if candidate[cut] == "," else candidate[:cut + 1]
        return runtime_partial_json_object(head + closers[-len(stack):] if stack else head)
    return {}


def runtime_tool_draft_preview(arguments: Any, *, max_chars: int = 90) -> str:
    """The line the model is writing now: the tail of its longest text field."""
    longest = ""

    def walk(value: Any) -> None:
        nonlocal longest
        if isinstance(value, str):
            if len(value) > len(longest):
                longest = value
        elif isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(arguments)
    if len(longest) < 40:
        return ""
    lines = [
        cleaned for cleaned in (line.strip().lstrip("#>*-•0123456789.) ").strip() for line in longest.splitlines())
        if cleaned
    ]
    line = lines[-1] if lines else ""
    # A line just begun ("让", "Card") says nothing yet: keep the last whole one.
    if len(line) < 8 and len(lines) > 1:
        line = lines[-2]
    if len(line) > max_chars:
        line = "…" + line[-(max_chars - 1):]
    return line
