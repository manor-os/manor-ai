"""Producer-friendly helpers for posting chat messages.

PlanExecutor + measurement.py + Strategist call into these instead of
constructing Message rows directly. Each notifier encapsulates:
  * which conversation thread to use (main vs per-plan vs per-goal)
  * the message_kind enum value
  * the canonical body format
  * any pending_action card shape

All notifiers are best-effort: a chat write failure must NEVER block
the underlying operation (a step doesn't fail because we couldn't
post a receipt). They handle their own DB session + log on failure.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional

from packages.core.database import async_session
from packages.core.workspace_chat import service as chat

logger = logging.getLogger(__name__)


# ── Plan lifecycle ────────────────────────────────────────────────────

def _plan_refs(plan_id: str, task_id: Optional[str] = None) -> list[dict]:
    refs = [{"type": "plan", "id": plan_id}]
    if task_id:
        refs.append({"type": "task", "id": task_id})
    return refs


async def notify_plan_started(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    plan_id: str,
    task_id: Optional[str] = None,
    task_title: Optional[str],
    step_count: int,
    execution_mode: str,
    steps: Optional[list[dict]] = None,
) -> None:
    if not workspace_id:
        return
    headline = (
        f"▶ **Plan started** — {step_count} step(s)"
        + (f" for task: *{task_title}*" if task_title else "")
    )
    if execution_mode != "live":
        headline += f"  `[{execution_mode}]`"

    body = headline
    if steps:
        body += "\n\n" + _render_dag(steps)

    # Plan thread (full DAG)
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=body, message_kind="agent_update", author_kind="agent",
        thread_ref_kind="plan", thread_ref_id=plan_id,
        refs=_plan_refs(plan_id, task_id),
    )

    # Main workspace chat (headline only)
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=headline, message_kind="agent_update", author_kind="agent",
        refs=_plan_refs(plan_id, task_id),
    )


async def notify_plan_completed(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    plan_id: str,
    task_id: Optional[str] = None,
    duration_seconds: Optional[float],
    cost_usd: Optional[float],
    task_title: Optional[str] = None,
    steps: Optional[list[dict]] = None,
) -> None:
    if not workspace_id:
        return
    body = _render_plan_completion_summary(
        plan_id=plan_id,
        task_title=task_title,
        duration_seconds=duration_seconds,
        cost_usd=cost_usd,
        steps=steps or [],
    )
    headline = body
    if steps:
        body += "\n\n" + _render_dag(steps)

    # Plan thread (full DAG)
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=body, message_kind="agent_update", author_kind="agent",
        thread_ref_kind="plan", thread_ref_id=plan_id,
        refs=_plan_refs(plan_id, task_id),
    )

    # Main workspace chat
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=headline, message_kind="agent_update", author_kind="agent",
        refs=_plan_refs(plan_id, task_id),
    )


async def notify_plan_failed(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    plan_id: str,
    task_id: Optional[str] = None,
    error: Optional[dict],
    task_title: Optional[str] = None,
    steps: Optional[list[dict]] = None,
) -> None:
    if not workspace_id:
        return
    msg = (error or {}).get("message") or "unknown error"
    subject = task_title or f"Plan {plan_id[:8]}"
    headline = f"✗ **Task failed — {subject}**\n\nReason: {msg}"
    body = headline
    if steps:
        body += "\n\n" + _render_dag(steps)

    # Plan thread (full DAG)
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=body, message_kind="agent_update", author_kind="agent",
        thread_ref_kind="plan", thread_ref_id=plan_id,
        refs=_plan_refs(plan_id, task_id),
    )

    # Main workspace chat
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=headline, message_kind="agent_update", author_kind="agent",
        refs=_plan_refs(plan_id, task_id),
    )


# ── Step lifecycle ────────────────────────────────────────────────────

async def notify_step_done(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    plan_id: str,
    step_id: str,
    step_key: str,
    kind: str,
    description: Optional[str],
    duration_seconds: Optional[float],
    cost_usd: Optional[float],
    subscription_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    result_summary: Optional[str] = None,
    result: Optional[dict] = None,
) -> None:
    if not workspace_id:
        return
    # Human-readable step name
    label = description or step_key.replace("_", " ").title()
    # Agent attribution
    agent_part = f" by **{agent_name}**" if agent_name else ""
    # Duration
    time_part = f" in {_fmt_duration(duration_seconds)}" if duration_seconds and duration_seconds >= 1 else ""

    summary = result_summary or summarize_result_for_chat(result, max_chars=1600)
    artifacts = extract_artifacts_for_chat(result)
    body = _render_step_completion_summary(
        label=label,
        agent_part=agent_part,
        time_part=time_part,
        summary=summary,
        artifacts=artifacts,
        max_summary_chars=1600,
    )

    # 1. Detailed message in the plan thread (for drill-down)
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=body, message_kind="step_event",
        author_kind="agent", author_subscription_id=subscription_id,
        thread_ref_kind="plan", thread_ref_id=plan_id,
        refs=[
            {"type": "plan", "id": plan_id},
            {"type": "step", "id": step_id},
        ],
    )

    # 2. Human-readable receipt in the main workspace chat. Keep enough
    # detail here so the user does not have to open a hidden plan thread
    # just to understand what changed.
    main_body = _render_step_completion_summary(
        label=label,
        agent_part=agent_part,
        time_part=time_part,
        summary=summary,
        artifacts=artifacts,
        max_summary_chars=1000,
    )
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=main_body, message_kind="step_event",
        author_kind="agent", author_subscription_id=subscription_id,
        refs=[
            {"type": "plan", "id": plan_id},
            {"type": "step", "id": step_id},
        ],
    )


async def notify_step_failed(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    plan_id: str,
    step_id: str,
    step_key: str,
    error: Optional[dict],
    will_retry: bool,
    subscription_id: Optional[str] = None,
) -> None:
    if not workspace_id:
        return
    msg = (error or {}).get("message") or "unknown error"
    suffix = " — will retry" if will_retry else ""
    body = f"✗ Step `{step_key}` failed: {msg}{suffix}"

    # Plan thread (detailed)
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=body, message_kind="step_event",
        author_kind="agent", author_subscription_id=subscription_id,
        thread_ref_kind="plan", thread_ref_id=plan_id,
        refs=[
            {"type": "plan", "id": plan_id},
            {"type": "step", "id": step_id},
        ],
    )

    # Main workspace chat (visible)
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=body, message_kind="step_event",
        author_kind="agent", author_subscription_id=subscription_id,
        refs=[
            {"type": "plan", "id": plan_id},
            {"type": "step", "id": step_id},
        ],
    )


async def notify_step_needs_human(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    plan_id: str,
    step_id: str,
    step_key: str,
    prompt: str,
    subscription_id: Optional[str] = None,
    pending_action: Optional[dict] = None,
) -> None:
    """Lease-level HITL prompt — interactive message with a
    pending_action so the user can resolve it from the chat.

    ``pending_action`` is the optional structured payload from
    packages/core/ai/pending_action.py (kinds: ``needs_login``,
    ``needs_input``, ``needs_confirmation``). When provided, it's
    used verbatim and step_id / plan_id are merged in so the resolve
    endpoint can look up the originating step.

    When omitted, falls back to the historical free-form text-input
    card (``kind="human_input"``) that asks the user to type a reply.
    """
    if not workspace_id:
        return
    if pending_action and pending_action.get("kind"):
        # Caller provided a structured payload — merge step/plan refs
        # so the resolve endpoint can find the originating step.
        merged_action = {**pending_action, "step_id": step_id, "plan_id": plan_id}
        # Use the structured title as the message body when present;
        # otherwise fall back to the prompt text.
        title = merged_action.get("title") or prompt or f"Need your input on `{step_key}`"
        body = title
    else:
        merged_action = {
            "kind": "human_input",
            "step_id": step_id,
            "plan_id": plan_id,
            "input_schema": {
                "type": "object",
                "properties": {"response": {"type": "string"}},
                "required": ["response"],
            },
        }
        body = f"⚠ Need your input on step `{step_key}`:\n\n{prompt}"

    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=body,
        message_kind="hitl_request",
        author_kind="agent", author_subscription_id=subscription_id,
        thread_ref_kind="plan", thread_ref_id=plan_id,
        refs=[
            {"type": "plan", "id": plan_id},
            {"type": "step", "id": step_id},
        ],
        pending_action=merged_action,
    )


# ── Goal lifecycle ────────────────────────────────────────────────────

async def notify_goal_measured(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    goal_id: str,
    metric_key: str,
    value: float,
    pace: Optional[str],
) -> None:
    """Quiet tick — only posted on non-trivial pace context. Frequent
    routine measurements (e.g. hourly) would spam the chat."""
    if not workspace_id:
        return
    if pace not in {"at_risk", "behind", "ahead"}:
        return
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=f"📊 {metric_key}: {value:g} ({pace})",
        message_kind="goal_alert", author_kind="agent",
        refs=[{"type": "goal", "id": goal_id}],
    )


async def notify_goal_pace_changed(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    goal_id: str,
    metric_key: str,
    value: float,
    prev_pace: Optional[str],
    new_pace: Optional[str],
) -> None:
    if not workspace_id:
        return
    icon = {"at_risk": "🔴", "behind": "🟡", "ahead": "🟢", "on_track": "🟢"}.get(new_pace or "", "ℹ")
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=f"{icon} Goal pace changed: {prev_pace or '?'} → {new_pace or '?'} (current {metric_key}={value:g})",
        message_kind="goal_alert", author_kind="agent",
        refs=[{"type": "goal", "id": goal_id}],
    )


async def notify_goal_achieved(
    *,
    entity_id: str,
    workspace_id: Optional[str],
    goal_id: str,
    metric_key: str,
    value: float,
) -> None:
    if not workspace_id:
        return
    await _safe_post(
        entity_id=entity_id, workspace_id=workspace_id,
        body=f"🎉 Goal achieved! {metric_key} reached {value:g}",
        message_kind="goal_alert", author_kind="agent",
        refs=[{"type": "goal", "id": goal_id}],
    )


# ── Internals ─────────────────────────────────────────────────────────

# ── Agent greetings ──────────────────────────────────────────────────

async def notify_agent_greeting(
    *,
    entity_id: str,
    workspace_id: str,
    subscription_id: str,
    greeting: str,
) -> None:
    """Post a single agent greeting to the workspace main chat."""
    await _safe_post(
        entity_id=entity_id,
        workspace_id=workspace_id,
        body=greeting,
        message_kind="agent_update",
        author_kind="agent",
        author_subscription_id=subscription_id,
    )


def summarize_result_for_chat(result: Any, *, max_chars: int = 1200) -> Optional[str]:
    """Best-effort user-facing summary from worker/LLM step output."""
    text = _extract_result_text(result)
    if not text:
        return None
    return _clip_text(_strip_boilerplate_completion_headings(text), max_chars)


def extract_artifacts_for_chat(result: Any) -> list[dict]:
    """Return user-visible files/URLs produced by a step result."""
    artifacts: list[dict] = []

    def add(kind: str, value: Any, *, name: Optional[str] = None) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value or value.startswith("data:"):
            return
        key = (kind, value)
        if any((a.get("kind"), a.get("value")) == key for a in artifacts):
            return
        artifacts.append({"kind": kind, "value": value, "name": name})

    def walk(obj: Any) -> None:
        if not isinstance(obj, dict):
            return

        doc = obj.get("document")
        if isinstance(doc, dict):
            doc_name = _as_text(doc.get("name"))
            add("file", doc.get("fs_path") or doc.get("path") or doc_name, name=doc_name)
            add("document", doc.get("id"), name=doc_name)
            add("url", doc.get("file_url") or doc.get("url"), name=doc_name)

        for key, kind in (
            ("fs_path", "file"),
            ("path", "file"),
            ("file_path", "file"),
            ("output_path", "file"),
            ("name", "file"),
            ("file_url", "url"),
            ("document_url", "url"),
            ("image_url", "url"),
            ("video_url", "url"),
            ("result_url", "url"),
            ("url", "url"),
            ("document_id", "document"),
            ("job_id", "job"),
        ):
            add(kind, obj.get(key), name=_as_text(obj.get("name")))

        for key in ("files", "artifacts", "outputs", "documents"):
            values = obj.get(key)
            if isinstance(values, list):
                for item in values:
                    walk(item)
            elif isinstance(values, dict):
                walk(values)

        for key in ("summary", "message", "text", "content", "output", "value"):
            nested = _parse_json_if_string(obj.get(key))
            if isinstance(nested, dict):
                walk(nested)

    parsed = _parse_json_if_string(result)
    walk(parsed)
    return artifacts[:8]


def _render_plan_completion_summary(
    *,
    plan_id: str,
    task_title: Optional[str],
    duration_seconds: Optional[float],
    cost_usd: Optional[float],
    steps: list[dict],
) -> str:
    done_steps = [s for s in steps if s.get("status") == "done"]
    failed_steps = [s for s in steps if s.get("status") in {"failed", "skipped", "cancelled"}]
    total = len(steps)
    subject = task_title or f"Plan {plan_id[:8]}"

    meta: list[str] = []
    if total:
        meta.append(f"{len(done_steps)}/{total} steps")
    if duration_seconds is not None:
        meta.append(f"finished in {_fmt_duration(duration_seconds)}")
    if cost_usd is not None:
        meta.append(f"cost ${cost_usd:.3f}")

    lines = [f"✅ **Task complete — {subject}**"]
    if meta:
        lines.append("")
        lines.append("Completed " + ", ".join(meta) + ".")

    if done_steps:
        lines.append("")
        lines.append("**What was completed**")
        for step in done_steps[:8]:
            label = _step_label(step)
            summary = _clip_text(_as_text(step.get("result_summary")), 360)
            if summary:
                lines.append(f"- **{label}:** {summary}")
            else:
                lines.append(f"- **{label}**")

    artifacts = _unique_artifacts(
        artifact
        for step in done_steps
        for artifact in (step.get("artifacts") or [])
        if isinstance(artifact, dict)
    )
    if artifacts:
        lines.append("")
        lines.append("**Files and outputs saved**")
        lines.extend(f"- {_format_artifact(artifact)}" for artifact in artifacts[:8])

    if failed_steps:
        lines.append("")
        lines.append("**Needs attention**")
        for step in failed_steps[:5]:
            err = step.get("error") if isinstance(step.get("error"), dict) else {}
            msg = _as_text((err or {}).get("message") or (err or {}).get("type")) or str(step.get("status"))
            lines.append(f"- **{_step_label(step)}:** {msg}")

    return "\n".join(lines)


def _render_step_completion_summary(
    *,
    label: str,
    agent_part: str,
    time_part: str,
    summary: Optional[str],
    artifacts: list[dict],
    max_summary_chars: int,
) -> str:
    lines = [f"✅ **Completed: {label}**{agent_part}{time_part}"]

    clean_summary = _clip_text(_as_text(summary), max_summary_chars)
    if clean_summary:
        lines.append("")
        lines.append("**What was produced**")
        for item in _summary_bullets(clean_summary):
            lines.append(f"- {item}")

    if artifacts:
        lines.append("")
        lines.append("**Files and outputs saved**")
        lines.extend(f"- {_format_artifact(artifact)}" for artifact in artifacts[:6])

    if not clean_summary and not artifacts:
        lines.append("")
        lines.append("This step finished successfully.")

    return "\n".join(lines)


def _extract_result_text(result: Any) -> Optional[str]:
    parsed = _parse_json_if_string(result)
    if isinstance(parsed, dict):
        for key in ("summary", "message", "text", "content", "output", "value"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                nested = _parse_json_if_string(value)
                if isinstance(nested, dict) and nested is not value:
                    nested_text = _extract_result_text(nested)
                    if nested_text:
                        return nested_text
                return value.strip()
        if isinstance(parsed.get("document"), dict):
            doc = parsed["document"]
            name = doc.get("name") or doc.get("fs_path") or doc.get("id")
            if name:
                return f"Saved document: {name}"
        return None
    if isinstance(parsed, str) and parsed.strip():
        return parsed.strip()
    return None


def _parse_json_if_string(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    text = value.strip()
    if not text or text[0] not in "[{":
        return value
    try:
        return json.loads(text)
    except Exception:
        return value


def _summary_bullets(summary: str) -> list[str]:
    lines = [line.strip(" -•\t") for line in summary.splitlines()]
    lines = [line for line in lines if line and not _is_markdown_rule(line)]
    if len(lines) > 1:
        return lines[:8]
    return [summary]


def _strip_boilerplate_completion_headings(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        normalized = line.lower().strip("# -*✅✓")
        if normalized.startswith("task complete") or normalized.startswith("step complete"):
            continue
        if normalized.startswith("step ") and (
            "complete" in normalized[:80] or "delivered" in normalized[:80]
        ):
            continue
        if normalized.startswith("completed:") and len(normalized) < 80:
            continue
        lines.append(raw)
    return "\n".join(lines).strip() or text.strip()


def _format_artifact(artifact: dict) -> str:
    kind = artifact.get("kind") or "output"
    value = _as_text(artifact.get("value")) or "output"
    name = _as_text(artifact.get("name"))
    if kind == "document" and name:
        return f"{kind.title()}: `{name}`"
    if name and name != value:
        return f"{kind.title()}: `{name}` ({value})"
    return f"{kind.title()}: `{value}`"


def _unique_artifacts(artifacts: Any) -> list[dict]:
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for artifact in artifacts:
        kind = _as_text(artifact.get("kind")) or "output"
        value = _as_text(artifact.get("value"))
        if not value:
            continue
        key = (kind, value)
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": kind, "value": value, "name": _as_text(artifact.get("name"))})
    return out


def _step_label(step: dict) -> str:
    return _as_text(step.get("description")) or _as_text(step.get("key")).replace("_", " ").title() or "Completed step"


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _clip_text(text: Optional[str], max_chars: int) -> str:
    value = (text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 1)].rstrip() + "…"


def _is_markdown_rule(line: str) -> bool:
    return set(line) <= {"-", "_", "*"} and len(line) >= 3


def _render_dag(steps: list[dict]) -> str:
    """Render a plan DAG as markdown for chat / task comments.

    Input: list of step dicts with keys:
      key, kind, service_key, description, depends_on, status, error
    """
    STATUS_ICONS = {
        "done": "✅", "failed": "❌", "running": "🔄",
        "pending": "⬜", "waiting_human": "⏸️",
        "skipped": "⏭️", "cancelled": "🚫",
    }

    lines: list[str] = []
    keys_seen: set[str] = set()

    # Group steps into layers (parallel steps at same depth)
    layers: list[list[dict]] = []
    remaining = list(steps)
    while remaining:
        layer = [s for s in remaining if all(d in keys_seen for d in (s.get("depends_on") or []))]
        if not layer:
            layer = remaining[:]  # break infinite loop
        for s in layer:
            remaining.remove(s)
            keys_seen.add(s["key"])
        layers.append(layer)

    for i, layer in enumerate(layers):
        if len(layer) > 1:
            # Parallel steps
            lines.append(f"**Layer {i + 1}** (parallel):")
            for s in layer:
                lines.append(_render_step(s, STATUS_ICONS, indent="  "))
        else:
            s = layer[0]
            lines.append(_render_step(s, STATUS_ICONS, indent=""))

        # Arrow to next layer
        if i < len(layers) - 1:
            lines.append("  ↓")

    return "\n".join(lines)


def _render_step(s: dict, icons: dict, indent: str = "") -> str:
    status = s.get("status") or "pending"
    icon = icons.get(status, "⬜")
    key = s.get("key", "?")
    desc = s.get("description") or key.replace("_", " ").title()
    service = s.get("service_key")
    agent = s.get("agent_name")

    # Step name + agent
    line = f"{indent}{icon} **{desc}**"
    if agent:
        line += f" — _{agent}_"
    elif service:
        line += f" — _{service.replace('_', ' ')}_"

    # Show error for failed steps
    if status == "failed" and s.get("error"):
        err = s["error"]
        err_msg = err.get("message", err.get("type", ""))[:150]
        line += f"\n{indent}  ↳ _{err_msg}_"

    # Show result preview for completed steps
    if status == "done" and s.get("result_summary"):
        preview = _clip_text(s["result_summary"], 600)
        line += f"\n{indent}  ↳ {preview}"

    artifacts = s.get("artifacts") or []
    if status == "done" and artifacts:
        for artifact in artifacts[:3]:
            if isinstance(artifact, dict):
                line += f"\n{indent}  ↳ {_format_artifact(artifact)}"

    return line


async def _safe_post(**kwargs) -> None:
    """Open own session + commit — workspace_chat writes never share
    a transaction with the caller. A chat post failing must not roll
    back the underlying step / measurement / plan write."""
    try:
        async with async_session() as db:
            await chat.post_message(db, **kwargs)
            await db.commit()
    except Exception:
        logger.warning("workspace_chat post failed", exc_info=True)


def _fmt_duration(seconds: float) -> str:
    if seconds < 1:
        return f"{int(seconds * 1000)}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"
