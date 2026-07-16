from __future__ import annotations

import hashlib
import json
from typing import Any

from packages.core.ai.runtime.approval_classifier import bash_write_targets
from packages.core.ai.runtime.approvals import RuntimeApprovalAction


__all__ = [
    "approval_args_hash",
    "approval_content_preview",
    "approval_paths",
    "approval_preview_arguments",
    "approval_public_content",
    "describe_runtime_approval_action",
    "runtime_approval_prompt",
    "runtime_approval_rejected_message",
    "runtime_approval_retry_args",
    "runtime_approval_resume_guidance",
    "runtime_file_approval_guidance",
    "runtime_approval_retry_message",
]


_CONTEXT_KEYS = {
    "approval_token",
    "active_user_message",
    "workspace_id",
    "conversation_id",
    "_agent_id_from_context",
    "_user_id_from_context",
    "_active_user_message_from_context",
    "_manual_skill_selected_from_context",
}
_BOOLEAN_CONTROL_KEYS = {"confirm"}
_FILE_MUTATION_TOOL_NAMES = {
    "write_file",
    "edit_file",
    "delete_file",
    "generate_document_file",
    "generate_file",
    "sandbox_save_result",
    "bash",
}


def approval_args_hash(arguments: dict[str, Any]) -> str:
    clean = _strip_context(arguments)
    encoded = json.dumps(clean, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _strip_context(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            if key in _CONTEXT_KEYS:
                continue
            clean_value = _strip_context(v)
            out[key] = _coerce_control_bool(clean_value) if key in _BOOLEAN_CONTROL_KEYS else clean_value
        return out
    if isinstance(value, list):
        return [_strip_context(v) for v in value]
    return value


def _coerce_control_bool(value: Any) -> Any:
    if isinstance(value, bool):
        return value
    if value is None:
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"true", "1", "yes", "y", "on"}:
            return True
        if text in {"false", "0", "no", "n", "off"}:
            return False
    return value


def approval_preview_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    clean = _strip_context(arguments)
    encoded = json.dumps(clean, ensure_ascii=False, default=str)
    if len(encoded) <= 3000:
        return clean if isinstance(clean, dict) else {"payload": clean}
    return {"preview": encoded[:3000], "truncated": True}


def approval_content_preview(value: Any, *, limit: int = 1800) -> str:
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str, indent=2)
        except Exception:
            text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > limit:
        return text[:limit] + "\n..."
    return text


def runtime_approval_prompt(
    action: RuntimeApprovalAction,
    tool_name: str,
    arguments: dict[str, Any],
) -> str:
    """Build a human-readable approval prompt without JSON dumps."""
    described = describe_runtime_approval_action(tool_name, arguments)
    if described:
        return described
    payload = approval_preview_arguments(arguments)
    content = approval_public_content(payload)
    if content:
        return f"{action.title.capitalize()}: {content[:300]}"
    return action.title.capitalize()


def runtime_approval_resume_guidance(text: str | None) -> str | None:
    from packages.core.ai.runtime.skill_routing import runtime_approval_resume_intent

    if not runtime_approval_resume_intent(text):
        return None
    return (
        "## Runtime Approval Resume\n"
        "- The latest user message is an internal approval-resume instruction, "
        "not a new user request.\n"
        "- Your first tool call must retry the exact approved tool named in "
        "that message, with the exact approved arguments plus "
        "`approval_token`.\n"
        "- If the approved tool name starts with `mcp__`, call that MCP tool "
        "directly. Do not call `manor` with the MCP tool name as its "
        "`action`.\n"
        "- Do not call `search_tools`, `list_files`, `read_file`, "
        "`invoke_skill`, or recreate drafts before this retry.\n"
        "- `approval_token` is a Manor runtime control argument accepted by "
        "approved tool calls, even if it is not listed in the tool's public "
        "schema."
    )


def runtime_file_approval_guidance(tool_names: set[str] | list[str] | tuple[str, ...]) -> str | None:
    names = {str(name or "").strip() for name in (tool_names or ()) if str(name or "").strip()}
    if not names.intersection(_FILE_MUTATION_TOOL_NAMES):
        return None
    return (
        "## File Change Approvals\n"
        "- User-visible file writes, edits, deletes, and generated deliverables "
        "are protected by the tool layer.\n"
        "- Do NOT ask for approval in plain chat before changing a user-visible "
        "file. Instead, call the intended file mutation tool once without "
        "`approval_token`; if approval is required, the tool will return a "
        "HITL approval payload and the UI will show Approve / Always approve / Reject.\n"
        "- After approval, retry the exact blocked tool call with the returned "
        "`approval_token`. Do not change any other files.\n"
        "- If `read_file` returned `source_sha256`, pass it as "
        "`expected_sha256` when editing, overwriting, deleting, or continuing "
        "that file."
    )


def _bash_intent_verb(command: str) -> str:
    """Map the first significant token of a bash command to a user-facing verb.

    Returns a single capitalised English verb suitable as a prefix in the
    approval prompt (e.g. "Delete", "Move", "Modify", "Create"). Keeps the
    classification in sync with `_classify_bash_tool` in approval_classifier.
    """
    import shlex as _shlex

    try:
        parts = _shlex.split((command or "").strip())
    except ValueError:
        parts = (command or "").strip().split()
    if not parts:
        return "Modify"
    base = parts[0].rsplit("/", 1)[-1]
    if base == "rm":
        return "Delete"
    if base == "mv":
        return "Move"
    if base == "mkdir":
        return "Create directory"
    if base == "touch":
        return "Create"
    if base == "cp":
        return "Copy to"
    if base == "tee":
        return "Write"
    return "Modify"


def describe_runtime_approval_action(tool_name: str, arguments: dict[str, Any]) -> str:
    """Tool-specific one-line description of what is about to happen."""
    args = arguments or {}

    def _str(key: str, default: str = "") -> str:
        v = args.get(key)
        return str(v).strip() if v is not None else default

    def _trunc(text: str, n: int = 120) -> str:
        text = (text or "").strip().replace("\n", " ")
        return text if len(text) <= n else text[: n - 1] + "…"

    if tool_name == "bash":
        # The runtime classifier groups bash commands by destruction-risk
        # (workspace.file.create / .modify / .delete / cli.exec). We surface
        # the same intent here: a verb on what's being authorized, not the
        # raw command. The verb is inferred from the keyword present in the
        # command — `rm`/`delete` → Delete, `mv` (which both reads from and
        # writes to a target) → Move, everything else (touch/cp/sed -i/>
        # redirects/etc.) → Modify or Create.
        command = _str("command")
        targets = bash_write_targets(command)
        if targets:
            shown = ", ".join(targets[:3])
            suffix = f" (+{len(targets) - 3} more)" if len(targets) > 3 else ""
            verb = _bash_intent_verb(command)
            return f"{verb}: {shown}{suffix}"
        # Return empty so `runtime_approval_prompt` falls back to
        # `action.title.capitalize()` — e.g. "Modify files in this
        # workspace" / "Delete files from this workspace" / "Run a command".
        # We deliberately do NOT dump the raw command anymore: it scared
        # users with shell jargon (`2>&1`, redirection, full python flag
        # lists) when all they needed was "Manor is about to <do thing>".
        return ""

    if tool_name == "mcp__linkedin__create_post":
        text = _trunc(_str("text"))
        return f'Publish LinkedIn post: "{text}"'

    if tool_name in ("mcp__twitter_x__create_tweet", "mcp__twitter_x__post_tweet"):
        text = _trunc(_str("text"))
        return f'Post tweet on X: "{text}"'
    if tool_name == "mcp__twitter_x__comment_tweet":
        text = _trunc(_str("text"))
        target = _str("tweet_id") or "(unspecified)"
        return f'Comment on tweet {target}: "{text}"'
    if tool_name == "mcp__twitter_x__create_thread":
        texts = args.get("texts") if isinstance(args.get("texts"), list) else []
        count = len(texts)
        hook = _trunc(str(texts[0])) if texts else ""
        suffix = f': "{hook}"' if hook else ""
        return f"Publish X thread ({count} tweet{'s' if count != 1 else ''}){suffix}"
    if tool_name == "mcp__twitter_x__delete_tweet":
        return f'Delete tweet on X: {_str("tweet_id") or "(unspecified)"}'
    if tool_name == "mcp__twitter_x__like_tweet":
        return f'Like tweet on X: {_str("tweet_id") or "(unspecified)"}'
    if tool_name == "mcp__twitter_x__unlike_tweet":
        return f'Unlike tweet on X: {_str("tweet_id") or "(unspecified)"}'
    if tool_name == "mcp__twitter_x__retweet":
        return f'Retweet on X: {_str("tweet_id") or "(unspecified)"}'
    if tool_name == "mcp__twitter_x__unretweet":
        return f'Undo retweet on X: {_str("tweet_id") or "(unspecified)"}'
    if tool_name == "mcp__twitter_x__follow_user":
        target = _str("target_user_id") or _str("user_id") or _str("username") or "(unspecified)"
        return f"Follow X user: {target}"
    if tool_name == "mcp__twitter_x__unfollow_user":
        target = _str("target_user_id") or _str("user_id") or _str("username") or "(unspecified)"
        return f"Unfollow X user: {target}"

    if tool_name == "mcp__facebook__publish_post":
        msg = _trunc(_str("message"))
        return f'Publish Facebook post: "{msg}"'

    if tool_name in ("mcp__gmail__send_draft", "mcp__email__send_draft"):
        draft_id = _str("draft_id") or _str("id") or "?"
        to = _str("to") or _str("recipient")
        subject = _trunc(_str("subject"), 80)
        if to and subject:
            return f'Send prepared Gmail draft to {to}: "{subject}"'
        if to:
            return f"Send prepared Gmail draft to {to}"
        return f"Send prepared Gmail draft ({draft_id})"
    if tool_name in (
        "mcp__gmail__send_email",
        "mcp__gmail__send_message",
        "mcp__outlook__send_email",
        "mcp__outlook__send_message",
        "mcp__email__send",
        "mcp__email__send_message",
    ):
        to = _str("to") or _str("recipient") or "(unspecified)"
        subject = _trunc(_str("subject"), 80) or "(no subject)"
        return f'Send email to {to}: "{subject}"'
    if tool_name in (
        "mcp__gmail__delete_message",
        "mcp__outlook__delete_message",
        "mcp__email__delete",
    ):
        return f'Delete email message ({_str("message_id") or _str("id") or "?"})'

    if tool_name in (
        "mcp__telegram__send_message",
        "mcp__slack__send_message",
        "mcp__discord__send_message",
        "mcp__whatsapp__send_message",
        "mcp__wechat_official__send_message",
        "mcp__wechat_personal__send_message",
    ):
        chat = _str("chat") or _str("channel") or _str("recipient") or "(unspecified)"
        text = _trunc(_str("text") or _str("message"))
        platform = tool_name.split("__")[1].replace("_", " ").title()
        return f'Send {platform} message to {chat}: "{text}"'

    # ── Sandbox file writes: show destination path. The `content` arg is
    # typically a generated code blob and never belongs in the prompt. ────
    if tool_name == "sandbox_write_file":
        path = _str("path") or _str("name")
        if path:
            return f"Save sandbox file: {path}"

    # ── Direct workspace file tools: classifier already routes to
    # workspace.file.* so the frontend renders "Modify foo.tex in this
    # workspace" via the paths channel. We add a backend label too so
    # non-frontend consumers (logs, audit, CLI) still see something
    # readable. ──────────────────────────────────────────────────────────
    if tool_name in ("write_file", "edit_file", "delete_file"):
        path = _str("path") or _str("name") or _str("filename")
        verb = "Delete" if tool_name == "delete_file" else "Modify"
        if path:
            return f"{verb} workspace file: {path}"
    if tool_name == "generate_file":
        kind = _str("kind") or "file"
        params_raw = args.get("params")
        params = params_raw if isinstance(params_raw, dict) else {}
        name = (
            _str("name")
            or _str("output_name")
            or _str("filename")
            or str(params.get("name") or "").strip()
            or str(params.get("output_name") or "").strip()
            or str(params.get("filename") or "").strip()
        )
        return f"Generate {kind}: {name}" if name else f"Generate {kind}"
    if tool_name == "sandbox_save_result":
        name = _str("filename") or _str("name") or _str("path")
        if name:
            return f"Save to Knowledge: {name}"
        return "Save to Knowledge"

    # ── Workspace mutations via `manor` action / dedicated tool names.
    # Each one has a primary "label field" (title / name / id) we surface
    # directly so reviewers see "Create task: Review lease options" not
    # the bare "Create task". ────────────────────────────────────────────
    if tool_name == "manor":
        manor_payload_raw = args.get("params")
        manor_payload = manor_payload_raw if isinstance(manor_payload_raw, dict) else {}
        manor_action = _str("action")
    else:
        manor_payload = args
        manor_action = tool_name

    def _payload_str(*keys: str) -> str:
        for key in keys:
            val = manor_payload.get(key)
            text = str(val or "").strip()
            if text:
                return text
        return ""

    if manor_action in ("create_task", "workspace_create_task"):
        title = _trunc(_payload_str("title", "name", "subject"), 90)
        return f"Create task: {title}" if title else "Create task"
    if manor_action in ("assign_task",):
        target = _payload_str("assignee", "assignee_id", "user_id")
        tid = _payload_str("task_id", "id") or "(task)"
        return f"Assign task {tid} to {target}" if target else f"Assign task: {tid}"
    if manor_action in ("delete_task",):
        tid = _payload_str("task_id", "id") or "(task)"
        return f"Delete task: {tid}"
    if manor_action in ("update_task_runtime", "workspace_update_task_runtime"):
        tid = _payload_str("task_id", "id") or "(task)"
        return f"Update task settings: {tid}"
    if manor_action in ("create_client",):
        name = _trunc(_payload_str("name", "title"), 80) or "(unnamed client)"
        return f"Create client: {name}"
    if manor_action in ("delete_client",):
        cid = _payload_str("client_id", "id") or "(client)"
        return f"Delete client: {cid}"
    if manor_action in ("create_order",):
        label = _trunc(_payload_str("name", "title", "description"), 80)
        return f"Create order: {label}" if label else "Create order"
    if manor_action in ("create_skill", "workspace_create_skill"):
        name = _trunc(_payload_str("name", "title"), 80) or "(unnamed skill)"
        return f"Create skill: {name}"
    if manor_action in ("delete_skill",):
        sid = _payload_str("skill_id", "name", "id") or "(skill)"
        return f"Delete skill: {sid}"
    if manor_action in ("update_skill",):
        sid = _payload_str("skill_id", "name", "id") or "(skill)"
        return f"Update skill: {sid}"
    if manor_action in ("create_scheduled_job",):
        name = _trunc(_payload_str("name", "description", "title"), 90) or "(automation)"
        return f"Create automation: {name}"
    if manor_action in ("cancel_scheduled_job",):
        job = _payload_str("job_id", "id") or "(automation)"
        return f"Cancel automation: {job}"
    if manor_action in ("toggle_scheduled_job",):
        job = _payload_str("job_id", "id") or "(automation)"
        return f"Toggle automation: {job}"
    if manor_action in ("run_scheduled_job_now",):
        job = _payload_str("job_id", "id") or "(automation)"
        return f"Run automation now: {job}"
    if manor_action in ("sync_file_to_knowledge",):
        target = _payload_str("name", "path", "document_id") or "(document)"
        return f"Add to Knowledge: {target}"
    if manor_action in ("delete_document",):
        target = _payload_str("document_id", "name", "path") or "(document)"
        return f"Delete document: {target}"
    if manor_action in ("create_knowledge_folder", "workspace_create_knowledge_folder"):
        name = _trunc(_payload_str("name", "title"), 80) or "(folder)"
        return f"Create Knowledge folder: {name}"
    if manor_action in ("add_knowledge_documents", "workspace_add_knowledge_documents"):
        docs = manor_payload.get("document_ids")
        if isinstance(docs, list):
            n = len([d for d in docs if str(d or "").strip()])
            if n:
                noun = "document" if n == 1 else "documents"
                return f"Attach {n} {noun} to Knowledge"
        return "Attach documents to Knowledge"
    if manor_action in ("remove_knowledge_document", "workspace_remove_knowledge_document"):
        doc = _payload_str("document_id", "name", "path") or "(document)"
        return f"Remove from Knowledge: {doc}"

    return ""


def approval_paths(
    action: RuntimeApprovalAction,
    tool_name: str,
    arguments: dict[str, Any],
) -> list[str]:
    paths: list[str] = []

    def add(value: Any) -> None:
        text = str(value or "").strip()
        if text and text not in paths:
            paths.append(text)

    if action.resource_kind == "file":
        add(action.resource_id)

    args = arguments or {}
    if tool_name == "bash":
        for path in bash_write_targets(str(args.get("command") or "")):
            add(path)

    if action.action_key.startswith("workspace.file."):
        for key in (
            "path",
            "file_path",
            "output_path",
            "output_name",
            "destination",
            "target_path",
            "source_path",
        ):
            val = args.get(key)
            if isinstance(val, list):
                for item in val:
                    add(item)
            else:
                add(val)
    return paths[:12]


def runtime_approval_retry_args(item: dict[str, Any], hitl_id: str) -> dict[str, Any]:
    args = item.get("retry_args")
    if not isinstance(args, dict):
        args = item.get("args_preview")
    retry_args = dict(args) if isinstance(args, dict) else {}
    retry_args["approval_token"] = hitl_id
    return retry_args


def runtime_approval_retry_message(item: dict[str, Any], hitl_id: str) -> str:
    tool_name = str(item.get("tool") or "").strip()
    action_key = str(item.get("action_key") or "").strip()
    retry_args = runtime_approval_retry_args(item, hitl_id)
    args_json = json.dumps(retry_args, ensure_ascii=False, indent=2, sort_keys=True)
    truncated_note = (
        "\nThe stored argument preview was truncated. Use the exact previous "
        "approved payload from the conversation and add the approval token."
        if retry_args.get("truncated") else ""
    )
    return (
        "[Runtime approval approved]\n"
        f"The user approved `{tool_name}` for `{action_key}`.\n"
        "Retry the exact same tool call with the approval token.\n"
        "First tool call now:\n"
        f"tool: `{tool_name}`\n"
        "arguments:\n"
        f"```json\n{args_json}\n```\n"
        "Do not call search_tools, list_files, read_file, invoke_skill, or "
        "create/rewrite drafts before this retry. Do not change the approved "
        "content or payload."
        f"{truncated_note}"
    )


def runtime_approval_rejected_message(item: dict[str, Any]) -> str:
    return (
        f"[Runtime approval rejected] The user rejected `{item.get('tool')}` for "
        f"`{item.get('action_key')}`. Do not retry the blocked tool call; offer a draft or revision instead."
    )


def approval_public_content(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("text", "message", "content", "body", "caption", "subject", "title", "name", "job_id", "path", "id"):
            val = payload.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()[:1800]
        for val in payload.values():
            nested = approval_public_content(val)
            if nested:
                return nested
    if isinstance(payload, list):
        for val in payload:
            nested = approval_public_content(val)
            if nested:
                return nested
    return ""
