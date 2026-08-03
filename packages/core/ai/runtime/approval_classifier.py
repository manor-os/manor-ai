from __future__ import annotations

import json
import os
import re
import shlex
from typing import Any

from packages.core.ai.runtime.approvals import RuntimeApprovalAction


__all__ = [
    "bash_write_targets",
    "classify_runtime_tool_action",
    "split_mcp_tool",
]


_SOCIAL_PUBLISH_ACTIONS = {
    "create_tweet",
    "comment_tweet",
    "create_thread",
    "post_tweet",
    "create_post",
    "create_multi_photo_post",
    "create_video_post",
    "publish_instagram_media",
}
_SOCIAL_MUTATION_ACTIONS = {
    "update_post",
    "delete_post",
    "delete_tweet",
    "create_comment",
    "comment_note",
    "like_note",
    "unlike_note",
    "like_tweet",
    "unlike_tweet",
    "retweet",
    "unretweet",
    "follow_user",
    "unfollow_user",
    "delete_comment",
    "react_to_post",
    "remove_reaction",
    "delete_instagram_comment",
}
_EMAIL_SEND_ACTIONS = {"send_message", "send_draft", "send_email"}
_MESSAGE_SEND_ACTIONS = {
    "send_text_message",
    "send_image_message",
    "send_template_message",
    "send_messenger",
    "send_messenger_image",
}
_DESTRUCTIVE_PREFIXES = ("delete_", "remove_", "revoke_", "refund_")
_MUTATION_PREFIXES = ("update_", "create_", "publish_", "send_", "upload_", "archive_")
_READ_ONLY_TOOLS = {
    "search_tools",
    "web_search",
    "web_event_search",
    "web_fetch",
    "read_file",
    "list_files",
    "glob_files",
    "grep_files",
    "workspace_search",
    "workspace_list_knowledge",
    "sandbox_read_file",
    "list_skills",
    "get_skill_details",
    "list_workflows",
    "list_workflow_definitions",
    "get_workflow",
    "validate_workflow",
    "list_workflow_runs",
    "get_workflow_run",
}
_FILE_CREATE_TOOLS = {"sandbox_save_result"}
_FILE_MUTATION_BASE_CMDS = {"mv", "chmod", "sed"}
_FILE_CREATE_BASE_CMDS = {"mkdir", "touch", "cp", "tee"}
_FILE_DELETE_BASE_CMDS = {"rm"}
# NOTE: a bare ">" used to live here. It produced false positives on any
# command that contained stderr/fd redirection like `cmd 2>&1` or `cmd 2>&3`,
# both of which only re-route file descriptors and do NOT write to a file.
# `_command_has_redirection` already detects real `>` / `>>` redirections to
# a file target (its negated character class rejects `>&`), so the bare hint
# is redundant. Keeping it caused `python pre_gate_file_check.py ... 2>&1`
# to be misclassified as workspace.file.modify and trigger an approval card
# on a read-only diagnostic script.
_CLI_WRITE_HINTS = ("tee ", "sed -i", " rm ", " mv ", " cp ", " mkdir ", " touch ", "chmod ")
_SHELL_SEGMENT_SEPARATOR_RE = re.compile(r"(?:^|\s)(?:&&|\|\||;|\|)(?:\s|$)")
_MANOR_ACTION_MAP = {
    "create_task": ("workspace.task.create", "medium", "create task", "workspace_task", "create"),
    "assign_task": ("workspace.task.update", "medium", "assign task", "workspace_task", "modify"),
    "delete_task": ("workspace.task.delete", "high", "delete task", "workspace_task", "delete"),
    "start_workspace_draft": ("workspace.draft.start", "low", "start workspace setup", "workspace", "create"),
    "create_workspace": ("workspace.draft.start", "low", "start workspace setup", "workspace", "create"),
    "create_client": ("workspace.client.create", "medium", "create client", "client", "create"),
    "delete_client": ("workspace.client.delete", "high", "delete client", "client", "delete"),
    "create_order": ("workspace.order.create", "medium", "create order", "order", "create"),
    "create_skill": ("workspace.skill.create", "medium", "create skill", "skill", "create"),
    "delete_skill": ("workspace.skill.delete", "high", "delete skill", "skill", "delete"),
    "create_scheduled_job": ("workspace.automation.create", "high", "create automation", "automation", "create"),
    "cancel_scheduled_job": ("workspace.automation.delete", "high", "cancel automation", "automation", "delete"),
    "toggle_scheduled_job": ("workspace.automation.update", "medium", "toggle automation", "automation", "modify"),
    "run_scheduled_job_now": ("workspace.automation.run", "medium", "run automation", "automation", "execute"),
    "delete_document": ("workspace.file.delete", "high", "delete workspace document", "file", "delete"),
    "sync_file_to_knowledge": ("workspace.knowledge.update", "medium", "update workspace knowledge", "knowledge", "modify"),
}
_SCHEDULED_JOB_ACTION_MAP = {
    "create_scheduled_job": ("workspace.automation.create", "high", "create automation", "automation", "create"),
    "cancel_scheduled_job": ("workspace.automation.delete", "high", "cancel automation", "automation", "delete"),
    "toggle_scheduled_job": ("workspace.automation.update", "medium", "toggle automation", "automation", "modify"),
    "run_scheduled_job_now": ("workspace.automation.run", "medium", "run automation", "automation", "execute"),
}
_WORKSPACE_AGENT_ACTION_MAP = {
    "create_task": ("workspace.task.create", "medium", "create workspace task", "workspace_task", "create"),
    "update_task_runtime": ("workspace.task.update", "medium", "update task runtime requirements", "workspace_task", "modify"),
    "create_knowledge_folder": ("workspace.knowledge.update", "medium", "create workspace Knowledge Net", "knowledge", "create"),
    "add_knowledge_documents": ("workspace.knowledge.update", "medium", "attach workspace knowledge documents", "knowledge", "modify"),
    "remove_knowledge_document": ("workspace.knowledge.update", "medium", "detach workspace knowledge document", "knowledge", "modify"),
    "update_knowledge_policy": ("workspace.knowledge.update", "medium", "update workspace knowledge policy", "knowledge", "modify"),
    "add_rule": ("workspace.rule.update", "medium", "update workspace rules", "workspace_rule", "modify"),
    "delegate_service": ("workspace.service.delegate", "medium", "delegate workspace service agent", "workspace_service", "execute"),
    "operation": ("workspace.operation.update", "medium", "update workspace operation draft", "workspace_operation", "modify"),
    "request_strategist_review": ("workspace.strategist.run", "medium", "request strategist review", "workspace", "execute"),
    "workspace_create_task": ("workspace.task.create", "medium", "create workspace task", "workspace_task", "create"),
    "workspace_update_task_runtime": ("workspace.task.update", "medium", "update task runtime requirements", "workspace_task", "modify"),
    "workspace_create_knowledge_folder": ("workspace.knowledge.update", "medium", "create workspace Knowledge Net", "knowledge", "create"),
    "workspace_add_knowledge_documents": ("workspace.knowledge.update", "medium", "attach workspace knowledge documents", "knowledge", "modify"),
    "workspace_remove_knowledge_document": ("workspace.knowledge.update", "medium", "detach workspace knowledge document", "knowledge", "modify"),
    "workspace_update_knowledge_policy": ("workspace.knowledge.update", "medium", "update workspace knowledge policy", "knowledge", "modify"),
    "workspace_operation": ("workspace.operation.update", "medium", "update workspace operation draft", "workspace_operation", "modify"),
    "workspace_add_rule": ("workspace.rule.update", "medium", "update workspace rules", "workspace_rule", "modify"),
    "workspace_request_strategist_review": ("workspace.strategist.run", "medium", "request strategist review", "workspace", "execute"),
}
_WORKSPACE_OPERATION_READ_ACTIONS = {
    "get_current",
    "validate_draft",
    "preview_diff",
}
_WORKSPACE_OPERATION_ACTION_MAP = {
    "create_draft": ("workspace.operation.draft", "medium", "create workspace operation draft", "workspace_operation", "create"),
    "patch_draft": ("workspace.operation.draft", "medium", "patch workspace operation draft", "workspace_operation", "modify"),
    "apply_draft": ("workspace.operation.apply", "high", "apply workspace operation draft", "workspace_operation", "modify"),
    "discard_draft": ("workspace.operation.discard", "medium", "discard workspace operation draft", "workspace_operation", "modify"),
}

def classify_runtime_tool_action(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    entity_id: str | None = None,
) -> RuntimeApprovalAction | None:
    """Map a concrete tool call to governance action-key language."""
    args = arguments or {}
    name = str(tool_name or "").strip()
    if not name or name in _READ_ONLY_TOOLS:
        return None

    if name.startswith("mcp__"):
        return _classify_mcp_tool_action(name, args)

    if name == "write_file":
        return _classify_file_write(
            path=str(args.get("path") or ""),
            entity_id=entity_id,
            title="write workspace file",
        )
    if name == "edit_file":
        return RuntimeApprovalAction("action", "workspace.file.modify", "medium", "edit workspace file", "file", "modify", str(args.get("path") or "") or None)
    if name == "delete_file":
        return RuntimeApprovalAction("action", "workspace.file.delete", "high", "delete workspace file", "file", "delete", str(args.get("path") or "") or None)

    if name == "bash":
        return _classify_bash_tool(args, entity_id=entity_id)

    if name in _SCHEDULED_JOB_ACTION_MAP:
        key, risk, title, resource_kind, operation = _SCHEDULED_JOB_ACTION_MAP[name]
        return RuntimeApprovalAction(
            "action",
            key,
            risk,
            title,
            resource_kind,
            operation,
            str(args.get("job_id") or "") or None,
        )

    if name == "generate_file":
        kind = str(args.get("kind") or "").strip().lower().replace("-", "_")
        if kind == "search":
            return None
        raw_params = args.get("params") or {}
        params = raw_params if isinstance(raw_params, dict) else {}
        output_name = str(
            args.get("name")
            or params.get("name")
            or params.get("output_name")
            or params.get("filename")
            or ""
        )
        return _classify_file_write(
            path=output_name,
            entity_id=entity_id,
            title=f"generate {kind or 'file'}",
            default_key="workspace.file.create",
        )

    if name in _FILE_CREATE_TOOLS:
        return RuntimeApprovalAction("action", "workspace.file.create", "medium", "create workspace file", "file", "create")

    if name == "sandbox_write_file":
        return RuntimeApprovalAction("action", "sandbox.file.modify", "medium", "write sandbox file", "sandbox_file", "modify", str(args.get("path") or "") or None)
    if name == "sandbox_exec":
        command = str(args.get("command") or "")
        if _command_has_write_hint(command):
            return RuntimeApprovalAction("action", "sandbox.exec.write_unknown", "medium", "run sandbox command with possible writes", "sandbox", "execute")
        return RuntimeApprovalAction("action", "sandbox.exec", "low", "run sandbox command", "sandbox", "execute")
    if name == "sandbox_create":
        return RuntimeApprovalAction("action", "sandbox.create", "low", "create sandbox", "sandbox", "create")
    if name == "sandbox_destroy":
        return None

    if name == "manor":
        action_name = str(args.get("action") or "").strip()
        if not action_name or action_name in {"search"} or action_name.startswith("list_") or action_name.startswith("get_") or action_name.startswith("search_"):
            return None
        mapped = _MANOR_ACTION_MAP.get(action_name)
        if mapped:
            key, risk, title, resource_kind, operation = mapped
            return RuntimeApprovalAction("action", key, risk, title, resource_kind, operation)
        if action_name.startswith("delete_"):
            return RuntimeApprovalAction("action", f"workspace.{action_name}", "high", "run destructive Manor action", "workspace", "delete")
        if action_name.startswith(("create_", "update_", "assign_", "sync_")):
            return RuntimeApprovalAction("action", f"workspace.{action_name}", "medium", "run Manor mutation", "workspace", "modify")
        return RuntimeApprovalAction("action", f"workspace.{action_name}", "medium", "run Manor action", "workspace", "modify")

    if name == "workspace_agent":
        raw_params = args.get("params") or {}
        params = raw_params if isinstance(raw_params, dict) else {}
        action_name = str(args.get("action") or "").strip()
        if action_name in {"search", "list_knowledge"}:
            return None
        if action_name == "operation":
            return _classify_workspace_operation_action(params)
        mapped = _WORKSPACE_AGENT_ACTION_MAP.get(action_name)
        if mapped:
            key, risk, title, resource_kind, operation = mapped
            resource_id = str(
                params.get("task_id")
                or params.get("rule_key")
                or params.get("group_id")
                or params.get("document_id")
                or ""
            ) or None
            return RuntimeApprovalAction("action", key, risk, title, resource_kind, operation, resource_id)
        return RuntimeApprovalAction("action", "workspace.agent.action", "medium", "run Workspace Agent action", "workspace", "modify")

    if name in _WORKSPACE_AGENT_ACTION_MAP:
        if name == "workspace_list_knowledge":
            return None
        if name == "workspace_operation":
            return _classify_workspace_operation_action(args)
        key, risk, title, resource_kind, operation = _WORKSPACE_AGENT_ACTION_MAP[name]
        resource_id = str(
            args.get("task_id")
            or args.get("rule_key")
            or args.get("group_id")
            or args.get("document_id")
            or ""
        ) or None
        return RuntimeApprovalAction("action", key, risk, title, resource_kind, operation, resource_id)

    if name in {"create_skill", "update_skill", "delete_skill"}:
        operation = "delete" if name.startswith("delete_") else "create" if name.startswith("create_") else "modify"
        risk = "high" if operation == "delete" else "medium"
        return RuntimeApprovalAction("action", f"workspace.skill.{operation}", risk, f"{operation} skill", "skill", operation)

    workflow_actions = {
        "create_workflow": ("workspace.workflow.create", "medium", "create workflow", "create"),
        "import_workflow": ("workspace.workflow.create", "medium", "import workflow", "create"),
        "ai_edit_workflow": ("workspace.workflow.modify", "medium", "AI edit workflow", "modify"),
        "update_workflow": ("workspace.workflow.modify", "medium", "update workflow", "modify"),
        "deploy_workflow": ("workspace.workflow.deploy", "high", "deploy workflow", "publish"),
        "delete_workflow": ("workspace.workflow.delete", "high", "delete workflow", "delete"),
        "run_workflow": ("workspace.workflow.run", "high", "run published workflow", "execute"),
        "test_workflow": ("workspace.workflow.run", "high", "test workflow", "execute"),
        "test_workflow_node": ("workspace.workflow.run", "high", "test workflow node", "execute"),
        "cancel_workflow_run": ("workspace.workflow.run", "medium", "cancel workflow run", "modify"),
        "resume_workflow_run": ("workspace.workflow.run", "high", "resume workflow run", "execute"),
    }
    if name in workflow_actions:
        key, risk, title, operation = workflow_actions[name]
        resource_id = str(args.get("workflow") or args.get("run_id") or "").strip() or None
        return RuntimeApprovalAction(
            "action", key, risk, title, "workflow", operation, resource_id,
        )

    if name == "write_agent_file":
        return RuntimeApprovalAction("action", "workspace.agent_file.modify", "medium", "write agent file", "agent_file", "modify")

    if name.startswith(("delete_", "remove_")):
        return RuntimeApprovalAction("action", f"tool.{name}", "high", "run destructive tool", "tool", "delete")
    if name.startswith(("write_", "edit_", "update_", "create_", "save_")):
        operation = "create" if name.startswith("create_") else "modify"
        return RuntimeApprovalAction("action", f"tool.{name}", "medium", "run mutating tool", "tool", operation)
    return None


def _classify_workspace_operation_action(args: dict[str, Any]) -> RuntimeApprovalAction | None:
    from packages.core.ai.runtime.workspace_operation_actions import _normalise_workspace_operation_action

    action_name = _normalise_workspace_operation_action(args.get("action"))
    if not action_name or action_name in _WORKSPACE_OPERATION_READ_ACTIONS:
        return None
    mapped = _WORKSPACE_OPERATION_ACTION_MAP.get(action_name)
    if mapped:
        key, risk, title, resource_kind, operation = mapped
        resource_id = str(args.get("draft_id") or "") or None
        return RuntimeApprovalAction("action", key, risk, title, resource_kind, operation, resource_id)
    return RuntimeApprovalAction(
        "action",
        "workspace.operation.update",
        "medium",
        "update workspace operation draft",
        "workspace_operation",
        "modify",
        str(args.get("draft_id") or "") or None,
    )


def _classify_mcp_tool_action(tool_name: str, args: dict[str, Any]) -> RuntimeApprovalAction | None:
    server, action = split_mcp_tool(tool_name)
    if not server or not action:
        return None

    if server in {"twitter_x", "linkedin", "facebook"}:
        if action in _SOCIAL_PUBLISH_ACTIONS:
            return RuntimeApprovalAction("action", "social_post.publish", "high", "publish social post", "external_account", "publish")
        if action in _SOCIAL_MUTATION_ACTIONS:
            risk = "high" if action.startswith("delete_") else "medium"
            suffix = "delete" if action.startswith("delete_") else "mutate"
            operation = "delete" if suffix == "delete" else "modify"
            return RuntimeApprovalAction("action", f"social_post.{suffix}", risk, f"{suffix} social content", "external_account", operation)

    if server in {"gmail", "outlook", "email"}:
        if action in _EMAIL_SEND_ACTIONS:
            return RuntimeApprovalAction("action", "email.send", "high", "send email", "external_account", "send")
        if action.startswith("delete_"):
            return RuntimeApprovalAction("action", "email.delete", "medium", "delete email item", "external_account", "delete")
        if action in {"create_draft", "update_draft"}:
            return RuntimeApprovalAction("action", "email.draft", "low", "modify email draft", "external_account", "modify")

    if server in {"wechat_official", "facebook"} and action in _MESSAGE_SEND_ACTIONS:
        return RuntimeApprovalAction("action", "external_message.send", "high", "send external message", "external_account", "send")


    if action.startswith(_DESTRUCTIVE_PREFIXES):
        return RuntimeApprovalAction("action", f"{server}.{action}", "high", "run destructive external action", "external_account", "delete")
    if action.startswith(_MUTATION_PREFIXES):
        risk = "medium"
        if _looks_public_or_paid(server, action, args):
            risk = "high"
        return RuntimeApprovalAction("action", f"{server}.{action}", risk, "run external action", "external_account", "modify")
    return None


def _classify_file_write(
    *,
    path: str,
    entity_id: str | None,
    title: str,
    default_key: str = "workspace.file.write",
) -> RuntimeApprovalAction:
    rel_path = str(path or "").strip()
    exists = _entity_file_exists(entity_id, rel_path)
    if exists is True:
        return RuntimeApprovalAction("action", "workspace.file.modify", "medium", title, "file", "modify", rel_path or None)
    if exists is False:
        return RuntimeApprovalAction("action", "workspace.file.create", "medium", title, "file", "create", rel_path or None)
    return RuntimeApprovalAction("action", default_key, "medium", title, "file", "modify", rel_path or None)


def _classify_bash_tool(args: dict[str, Any], *, entity_id: str | None) -> RuntimeApprovalAction | None:
    command = str(args.get("command") or "").strip()
    if not command:
        return None
    base_cmd = _base_command(command)
    if not base_cmd:
        return None

    if base_cmd in _FILE_DELETE_BASE_CMDS:
        return RuntimeApprovalAction("action", "workspace.file.delete", "high", "delete files from this workspace", "file", "delete")
    if _command_has_delete_hint(command):
        return RuntimeApprovalAction("action", "workspace.file.delete", "high", "delete files from this workspace", "file", "delete")
    if base_cmd in _FILE_MUTATION_BASE_CMDS:
        if base_cmd == "sed" and "-i" not in command:
            return RuntimeApprovalAction("action", "cli.exec", "low", "run a command", "cli", "execute")
        return RuntimeApprovalAction("action", "workspace.file.modify", "high", "modify files in this workspace", "file", "modify")
    if base_cmd in _FILE_CREATE_BASE_CMDS or _command_has_redirection(command):
        paths = bash_write_targets(command)
        if not paths:
            return RuntimeApprovalAction("action", "workspace.file.modify", "high", "modify files in this workspace", "file", "modify")
        existence = [_entity_file_exists(entity_id, path) for path in paths]
        if any(v is True for v in existence) or any(v is None for v in existence):
            return RuntimeApprovalAction("action", "workspace.file.modify", "high", "modify files in this workspace", "file", "modify")
        return RuntimeApprovalAction("action", "workspace.file.create", "medium", "create files in this workspace", "file", "create")
    if _command_has_write_hint(command):
        return RuntimeApprovalAction("action", "workspace.file.modify", "high", "modify files in this workspace", "file", "modify")
    return RuntimeApprovalAction("action", "cli.exec", "low", "run a command", "cli", "execute")


def _base_command(command: str) -> str:
    stripped = command.strip()
    if not stripped:
        return ""
    try:
        parts = shlex.split(stripped)
    except ValueError:
        parts = stripped.split()
    if not parts:
        return ""
    return parts[0].rsplit("/", 1)[-1]


def _command_has_write_hint(command: str) -> bool:
    stripped = f" {command.strip()} "
    return any(hint in stripped for hint in _CLI_WRITE_HINTS) or _command_has_redirection(command)


def _command_has_delete_hint(command: str) -> bool:
    stripped = command.strip()
    return bool(
        re.search(r"(?:^|[;&|]\s*)rm\s+", stripped)
        or re.search(r"\bxargs\s+rm\b", stripped)
        or re.search(r"\bfind\b.*(?:\s-delete\b|-exec\s+rm\b)", stripped)
    )


def _command_has_redirection(command: str) -> bool:
    return bool(re.search(r"(?<![<>=])(?:\d?>>|>>|>)\s*[^\s;&|]+", command))


def bash_write_targets(command: str) -> list[str]:
    """Best-effort write target extraction for simple CLI commands.

    Extracts the file paths a bash command will create, modify, or delete so
    the approval prompt can name them ("Modify paper.tex" / "Delete log.txt")
    instead of dumping the raw command at the user. Coverage focuses on the
    common shapes the runtime classifier already flags as workspace.file.*.
    Chained commands are split into simple shell segments; each segment is
    still parsed conservatively, so unknown segments simply add no paths.
    """
    stripped = command.strip()
    if not stripped:
        return []
    if _SHELL_SEGMENT_SEPARATOR_RE.search(stripped):
        segment_targets: list[str] = []
        for segment in re.split(r"\s+(?:&&|\|\||;|\|)\s+", stripped):
            for target in bash_write_targets(segment):
                if target not in segment_targets:
                    segment_targets.append(target)
        return segment_targets
    try:
        args = shlex.split(stripped)
    except ValueError:
        return []
    if not args:
        return []
    base_cmd = args[0].rsplit("/", 1)[-1]

    def non_flags(items: list[str]) -> list[str]:
        return [item for item in items if item and not item.startswith("-")]

    targets: list[str] = []
    if base_cmd in {"mkdir", "touch", "chmod"}:
        targets.extend(non_flags(args[1:]))
    elif base_cmd == "cp":
        clean = non_flags(args[1:])
        if len(clean) >= 2:
            targets.append(clean[-1])
    elif base_cmd == "mv":
        # `mv src dst` (2-arg form): dst is the file being created/overwritten.
        # `mv src1 src2 ... destdir/` (n-arg form): destdir is the directory
        # receiving the files. Either way the last non-flag arg is the target.
        clean = non_flags(args[1:])
        if len(clean) >= 2:
            targets.append(clean[-1])
    elif base_cmd == "rm":
        # `rm file1 file2 ...` deletes every non-flag arg.
        targets.extend(non_flags(args[1:]))
    elif base_cmd == "sed":
        # Only `sed -i ...` mutates files in place. Other sed invocations
        # stream to stdout and don't touch the filesystem; skip those.
        if any(part == "-i" or part.startswith("-i") for part in args[1:]):
            # After the script argument, remaining non-flag args are files
            # that sed will rewrite in place. Heuristic: collect everything
            # that doesn't look like a flag or the s/x/y/ script. We treat
            # the LAST run of non-flag args as the files (skipping the
            # script if it's the first non-flag).
            clean = non_flags(args[1:])
            if len(clean) >= 2:
                targets.extend(clean[1:])
    elif base_cmd == "tee":
        targets.extend(non_flags(args[1:]))

    for op in (">>", ">"):
        if op in args:
            idx = args.index(op)
            if idx + 1 < len(args):
                targets.append(args[idx + 1])
    for match in re.finditer(r"(?<![<>=])(?:\d?>>|>>|>)\s*([^\s;&|]+)", stripped):
        targets.append(match.group(1).strip("'\""))
    return list(dict.fromkeys(t for t in targets if t))


def _entity_file_exists(entity_id: str | None, rel_path: str) -> bool | None:
    abs_path = _entity_abs_path(entity_id, rel_path)
    if not abs_path:
        return None
    return os.path.exists(abs_path)


def _entity_abs_path(entity_id: str | None, rel_path: str) -> str | None:
    if not entity_id or not rel_path:
        return None
    try:
        from packages.core.config import get_settings

        settings = get_settings()
        if not settings.MANOR_FS_ENABLED:
            return None
        root = os.path.realpath(os.path.join(settings.MANOR_FS_ROOT, entity_id))
        candidate = os.path.realpath(os.path.join(root, rel_path.lstrip("/")))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate
    except Exception:
        return None


def split_mcp_tool(tool_name: str) -> tuple[str | None, str | None]:
    parts = str(tool_name or "").split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp":
        return None, None
    return parts[1], parts[2]


def _looks_public_or_paid(server: str, action: str, args: dict[str, Any]) -> bool:
    text = f"{server} {action} {json.dumps(args, ensure_ascii=False, default=str)[:500]}".lower()
    return any(word in text for word in ("publish", "post", "send", "payment", "charge", "refund", "invoice"))
