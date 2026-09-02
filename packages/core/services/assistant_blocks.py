from __future__ import annotations

import json
from collections import Counter
from collections.abc import Mapping
from pathlib import PurePath
from typing import Any

from packages.core.ai.runtime.composite_tools import RuntimeCompositeToolCallFactory


ASSISTANT_BLOCKS_SCHEMA = "v1"
ASSISTANT_PROCESS_TOOL_KEY_PREFIX = "component.assistant_process.tool"
ASSISTANT_STRUCTURED_RESULT_TOOLS = frozenset({
    "query_ledger",
    "visualize_workspace_ledgers",
    "render_response_surface",
})
_LEDGER_COUNT_METRICS = {"count"}
_LEDGER_FINANCE_METRICS = _LEDGER_COUNT_METRICS | {
    "sum_amount_minor",
    "sum_tax_minor",
    "sum_fee_minor",
    "sum_inflow_minor",
    "sum_outflow_minor",
}


def assistant_tool_uses_structured_result(name: str, arguments: Any = None) -> bool:
    """Return whether a tool result must bypass the lossy preview path."""

    args = arguments if isinstance(arguments, dict) else {}
    canonical_call = RuntimeCompositeToolCallFactory.create(name, args)
    if canonical_call.tool_name in ASSISTANT_STRUCTURED_RESULT_TOOLS:
        return True
    return (
        canonical_call.tool_name == "workspace_agent"
        and str(canonical_call.arguments.get("action") or "").strip().lower()
        == "visualize_ledgers"
    )


def _ledger_metric_types(contract_id: Any) -> set[str]:
    return (
        _LEDGER_FINANCE_METRICS
        if contract_id == "manor.finance_ledger/v1"
        else _LEDGER_COUNT_METRICS
    )


def _compact_text(value: Any, *, max_chars: int = 240) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else str(value)
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1] + "…"


def _preview_json(value: Any, *, max_chars: int = 240) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except Exception:
            text = str(value)
    return _compact_text(text, max_chars=max_chars)


def _parse_preview_json(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return None


def _valid_ledger_presentation(
    value: Any,
    *,
    contract_id: Any,
    group_by: Any,
    date_granularity: Any,
) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or value.get("version") != 1:
        return False
    title = value.get("title")
    subtitle = value.get("subtitle")
    if (
        (title is not None and (not isinstance(title, str) or len(title) > 160))
        or (subtitle is not None and (not isinstance(subtitle, str) or len(subtitle) > 240))
    ):
        return False
    sections = value.get("sections")
    if not isinstance(sections, list) or not 1 <= len(sections) <= 4:
        return False
    section_types = {"metrics", "chart", "records", "profile", "projection"}
    chart_types = {"bar", "line", "area", "pie", "donut"}
    metric_types = _ledger_metric_types(contract_id)
    for section in sections:
        if not isinstance(section, dict):
            return False
        section_type = section.get("type")
        if not isinstance(section_type, str) or section_type not in section_types:
            return False
        if section_type == "projection" and (
            not isinstance(group_by, list)
            or len(group_by) != 1
            or not isinstance(group_by[0], str)
            or group_by[0] not in {"recorded_at", "occurred_at"}
            or not isinstance(date_granularity, str)
            or date_granularity not in {"day", "week", "month"}
        ):
            return False
        if (
            section.get("title") is not None
            and (
                not isinstance(section["title"], str)
                or len(section["title"]) > 120
            )
        ):
            return False
        chart = section.get("chart")
        if chart is not None and (
            not isinstance(chart, str) or chart not in chart_types
        ):
            return False
        if (
            section.get("metric") is not None
            and (
                not isinstance(section["metric"], str)
                or section["metric"] not in metric_types
            )
        ):
            return False
        for field, limit in (("metrics", 4), ("columns", 10)):
            items = section.get(field)
            if items is not None and (
                not isinstance(items, list)
                or len(items) > limit
                or any(not isinstance(item, str) or len(item) > 160 for item in items)
                or (field == "metrics" and any(item not in metric_types for item in items))
            ):
                return False
        periods = section.get("forecast_periods")
        if periods is not None and (
            not isinstance(periods, int)
            or isinstance(periods, bool)
            or not 1 <= periods <= 12
        ):
            return False
    return True


def _workspace_ledger_visualization(
    value: Any,
    *,
    expected_kind: str,
) -> dict[str, Any] | None:
    """Extract a bounded server-owned Ledger visualization envelope."""

    parsed = _parse_preview_json(value) if isinstance(value, str) else value
    if not isinstance(parsed, dict) or parsed.get("ok") is not True:
        return None
    visualization = parsed.get("visualization")
    if not isinstance(visualization, dict):
        return None
    if visualization.get("kind") != expected_kind:
        return None
    data = visualization.get("data")
    if not isinstance(data, dict):
        return None
    if expected_kind == "workspace_ledger_overview":
        if not isinstance(data.get("ledgers"), list) or len(data["ledgers"]) > 16:
            return None
    elif expected_kind == "ledger_query_result":
        contract_id = data.get("contract_id")
        metric_types = _ledger_metric_types(contract_id)
        query_fingerprint = data.get("query_fingerprint")
        coalesce_previous = data.get("coalesce_previous_visualization")
        group_count = data.get("group_count")
        groups_truncated = data.get("groups_truncated")
        layout = data.get("layout")
        date_granularity = data.get("date_granularity")
        if (
            not isinstance(contract_id, str)
            or not isinstance(layout, str)
            or layout not in {"metrics", "bar", "timeline", "table", "empty"}
            or not (
                date_granularity is None
                or (
                    isinstance(date_granularity, str)
                    and date_granularity in {"day", "week", "month"}
                )
            )
            or not isinstance(data.get("metrics"), list)
            or any(
                not isinstance(metric, str) or metric not in metric_types
                for metric in data["metrics"]
            )
            or not isinstance(data.get("aggregates"), dict)
            or any(metric not in metric_types for metric in data["aggregates"])
            or not isinstance(data.get("groups"), list)
            or len(data["groups"]) > 24
            or (
                group_count is not None
                and (
                    not isinstance(group_count, int)
                    or isinstance(group_count, bool)
                    or group_count < len(data["groups"])
                )
            )
            or (
                groups_truncated is not None
                and not isinstance(groups_truncated, bool)
            )
            or not isinstance(data.get("rows"), list)
            or len(data["rows"]) > 20
            or not isinstance(data.get("columns"), list)
            or len(data["columns"]) > 10
            or any(
                not isinstance(group, dict)
                or not isinstance(group.get("aggregates"), dict)
                or any(metric not in metric_types for metric in group["aggregates"])
                for group in data["groups"]
            )
            or not _valid_ledger_presentation(
                data.get("presentation"),
                contract_id=contract_id,
                group_by=data.get("group_by"),
                date_granularity=date_granularity,
            )
            or (
                query_fingerprint is not None
                and (
                    not isinstance(query_fingerprint, str)
                    or len(query_fingerprint) != 64
                    or any(character not in "0123456789abcdef" for character in query_fingerprint)
                )
            )
            or (
                coalesce_previous is not None
                and not isinstance(coalesce_previous, bool)
            )
        ):
            return None
    else:
        return None
    try:
        if len(json.dumps(data, ensure_ascii=False, default=str)) > 100_000:
            return None
    except Exception:
        return None
    return {
        "kind": expected_kind,
        "data": data,
    }


def _ledger_table_row_relation(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> str | None:
    """Compare Ledger table rows without trusting display order."""

    if existing.get("layout") != "table" or incoming.get("layout") != "table":
        return None
    existing_columns = existing.get("columns")
    incoming_columns = incoming.get("columns")
    existing_rows = existing.get("rows")
    incoming_rows = incoming.get("rows")
    if (
        not isinstance(existing_columns, list)
        or not isinstance(incoming_columns, list)
        or not isinstance(existing_rows, list)
        or not isinstance(incoming_rows, list)
        or not existing_rows
        or not incoming_rows
        or not all(isinstance(row, dict) for row in [*existing_rows, *incoming_rows])
    ):
        return None
    common_columns = sorted(
        set(column for column in existing_columns if isinstance(column, str))
        & set(column for column in incoming_columns if isinstance(column, str))
    )
    if not common_columns:
        return None

    def row_counts(rows: list[dict[str, Any]]) -> Counter[str]:
        return Counter(
            json.dumps(
                [row.get(column) for column in common_columns],
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            )
            for row in rows
        )

    existing_counts = row_counts(existing_rows)
    incoming_counts = row_counts(incoming_rows)
    if all(count <= existing_counts[token] for token, count in incoming_counts.items()):
        return "incoming_subset"
    if all(count <= incoming_counts[token] for token, count in existing_counts.items()):
        return "incoming_superset"
    return None


def _ledger_query_relation(
    existing: dict[str, Any],
    incoming: dict[str, Any],
) -> str | None:
    """Compare query identity before considering a row-level replacement."""

    if any(
        existing.get(field) != incoming.get(field)
        for field in ("contract_id", "view")
    ):
        return None
    existing_fingerprint = existing.get("query_fingerprint")
    incoming_fingerprint = incoming.get("query_fingerprint")
    if isinstance(existing_fingerprint, str) and isinstance(incoming_fingerprint, str):
        if existing_fingerprint == incoming_fingerprint:
            return "incoming_replacement"
        if incoming.get("coalesce_previous_visualization") is not True:
            return None
        if existing.get("layout") != incoming.get("layout"):
            return "incoming_replacement"
        if existing.get("layout") != "table":
            return "incoming_replacement"
        return _ledger_table_row_relation(existing, incoming) or "incoming_replacement"
    if existing_fingerprint is not None or incoming_fingerprint is not None:
        return None

    # Historical blocks predate query identity. Keep their narrowly bounded
    # table-only behavior so already-persisted duplicate cards still collapse.
    return _ledger_table_row_relation(existing, incoming)


def _path_basename(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return PurePath(text.replace("\\", "/")).name or text


def _display_name(name: str) -> str:
    return (name or "tool").replace("_", " ").strip().capitalize()


def _param_text(value: Any, *, max_chars: int = 80) -> str:
    return _compact_text(value, max_chars=max_chars)


def _pretty_identifier(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text.replace("_", " ").replace("-", " ")


def _first_argument(arguments: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = arguments.get(key)
        if value not in ("", None):
            return value
    return None


def _result_count(result: Any) -> int | None:
    if isinstance(result, dict):
        count = result.get("count") or result.get("total") or result.get("total_count")
        if isinstance(count, int):
            return count
        items = result.get("items") or result.get("results") or result.get("documents") or result.get("tasks")
        if isinstance(items, list):
            return len(items)
    if isinstance(result, list):
        return len(result)
    return None


def _mcp_parts(name: str) -> tuple[str, str] | None:
    if not name.startswith("mcp__"):
        return None
    parts = [part for part in name.split("__") if part]
    if len(parts) >= 3:
        return parts[1], parts[-1]
    return None


def _tool_target(arguments: dict[str, Any], *, fallback: str = "") -> str:
    value = _first_argument(
        arguments,
        "query",
        "q",
        "keyword",
        "pattern",
        "prompt",
        "question",
        "url",
        "uri",
        "href",
        "path",
        "file",
        "filename",
        "output_name",
        "name",
        "title",
        "to",
        "recipient",
        "channel",
        "chat",
        "customer",
        "repo",
        "repository",
        "worksheet",
        "sheet",
        "cmd",
        "command",
        "username",
        "user",
        "cwd",
        "selector",
        "ref",
        "action",
        "state",
        "textContains",
        "text_contains",
        "urlContains",
        "url_contains",
    )
    if value is None:
        return fallback
    text_value = str(value).strip()
    if "://" in text_value:
        return _param_text(text_value)
    if text_value.replace("\\", "/").startswith("/") or "/" in text_value:
        base = _path_basename(value)
        return _param_text(base or value)
    return _param_text(value)


def _mcp_display_metadata(
    server: str,
    tool: str,
    arguments: dict[str, Any],
    *,
    summary: str = "",
) -> dict[str, Any]:
    server_pretty = _pretty_identifier(server)
    tool_pretty = _pretty_identifier(tool)
    lower_server = server.lower()
    lower_tool = tool.lower()
    target = _tool_target(arguments, fallback=summary)
    social_servers = {
        "twitter",
        "twitter_x",
        "x",
        "xiaohongshu",
        "linkedin",
        "facebook",
        "producthunt",
        "instagram",
        "tiktok",
        "tiktok_shop",
        "youtube",
        "wechat",
        "wechat_personal",
    }
    is_social_server = lower_server in social_servers or any(
        token in lower_server
        for token in ("twitter", "xiaohongshu", "linkedin", "facebook", "instagram", "tiktok", "youtube", "wechat")
    )

    if is_social_server:
        if any(token in lower_tool for token in ("search", "list", "get", "observe")):
            key = "social.search"
            display_target = target or server_pretty
        elif any(token in lower_tool for token in ("publish", "post", "create", "tweet", "draft", "upload")):
            key = "social.publish"
            display_target = server_pretty if target == tool_pretty else (target or server_pretty)
        elif any(token in lower_tool for token in ("comment", "reply", "like", "follow", "retweet", "share")):
            key = "social.interact"
            display_target = server_pretty if target == tool_pretty else (target or server_pretty)
        else:
            key = "social.use"
            display_target = target or server_pretty
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": display_target},
        }

    if lower_server == "chrome" or "browser" in lower_server:
        if any(token in lower_tool for token in ("open", "goto", "navigate")):
            key = "browser.open"
        elif lower_tool in {"read_page", "get_interactive_elements"}:
            key = "browser.read_page"
        elif any(token in lower_tool for token in ("observe", "screenshot", "status")):
            key = "browser.observe"
        elif lower_tool in {"get_web_content", "get_content"}:
            key = "browser.extract"
        elif lower_tool == "wait":
            key = "browser.wait"
        elif lower_tool in {"inject_script", "send_cdp"}:
            key = "browser.script"
        elif any(token in lower_tool for token in ("click", "fill", "type", "scroll", "press")):
            key = "browser.interact"
        elif lower_tool in {"computer", "hover", "upload", "set_cursor", "hide_cursor", "set_badge"}:
            key = "browser.interact"
        else:
            key = "browser.use"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if any(token in lower_server for token in ("codex", "claude_code", "aider", "continue_cli", "cursor_cli", "gemini_cli")):
        if "check_path" in lower_tool:
            key = "coding.check"
        elif any(token in lower_tool for token in ("review", "diff")):
            key = "coding.review"
        else:
            key = "coding.run"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if any(token in lower_server for token in ("calendar",)):
        if any(token in lower_tool for token in ("create", "quick_add", "accept", "respond")):
            key = "calendar.create"
        elif any(token in lower_tool for token in ("update", "move", "tentatively")):
            key = "calendar.update"
        elif any(token in lower_tool for token in ("delete", "cancel", "decline")):
            key = "calendar.delete"
        else:
            key = "calendar.search"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if any(token in lower_server for token in ("teams", "telegram")):
        if any(token in lower_tool for token in ("send", "reply", "answer", "create_chat", "meeting")):
            key = "message.send"
        elif any(token in lower_tool for token in ("list", "get", "channel", "chat", "presence")):
            key = "message.read"
        else:
            key = "message.use"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if lower_server in {"gmail", "outlook", "email", "imap", "smtp"} or any(
        token in lower_server for token in ("gmail", "outlook", "mail", "email")
    ):
        if any(token in lower_tool for token in ("send", "draft", "compose", "reply")):
            key = "email.send"
        elif any(token in lower_tool for token in ("search", "list", "get", "read")):
            key = "email.read"
        else:
            key = "email.use"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if any(token in lower_server for token in ("excel", "sheet")):
        if any(token in lower_tool for token in ("read", "get", "list")):
            key = "spreadsheet.read"
        elif any(token in lower_tool for token in ("write", "update", "add", "create", "rename", "clear", "delete")):
            key = "spreadsheet.write"
        else:
            key = "spreadsheet.analyze"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if any(token in lower_server for token in ("knowledge", "drive", "notion", "docs")):
        if any(token in lower_tool for token in ("save", "write", "create", "upload")):
            key = "knowledge.save"
        elif any(token in lower_tool for token in ("search", "list", "find")):
            key = "knowledge.search"
        else:
            key = "knowledge.read"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if any(token in lower_server for token in ("tavily", "perplexity")):
        key = "web.fetch" if any(token in lower_tool for token in ("fetch", "extract")) else "web.search"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if "nango" in lower_server:
        key = "integration.list" if any(token in lower_tool for token in ("list", "providers", "connections")) else "integration.manage"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if "github" in lower_server:
        if any(token in lower_tool for token in ("search", "list", "get", "read", "repo_info", "compare")):
            key = "repo.search"
        elif any(token in lower_tool for token in ("create", "push", "merge", "update", "add", "remove", "rerun", "run", "request", "fork")):
            key = "repo.create"
        elif any(token in lower_tool for token in ("delete", "close", "cancel")):
            key = "repo.modify"
        else:
            key = "repo.use"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    commerce_servers = ("shopify", "woocommerce", "tiktok_shop", "amazon", "square")
    if any(token in lower_server for token in commerce_servers):
        if any(token in lower_tool for token in ("list", "get", "search", "query")):
            key = "commerce.search"
        elif any(token in lower_tool for token in ("create", "put", "add")):
            key = "commerce.create"
        else:
            key = "commerce.update"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if any(token in lower_server for token in ("stripe", "quickbooks")):
        if any(token in lower_tool for token in ("list", "get", "search", "query", "report")):
            key = "finance.search"
        elif any(token in lower_tool for token in ("create", "send")):
            key = "finance.create"
        else:
            key = "finance.update"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    if any(token in lower_server for token in ("replicate", "jimeng", "elevenlabs")):
        if any(token in lower_tool for token in ("list", "voice", "model")):
            key = "media.search"
        else:
            key = "media.generate"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": target or server_pretty},
        }

    return {
        "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.mcp",
        "display_params": {
            "server": server_pretty,
            "tool": tool_pretty,
            "target": target or tool_pretty,
        },
    }


def _tool_display_metadata(
    name: str,
    arguments: Any,
    *,
    result: Any = None,
    summary: str = "",
    skill_display_names: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    normalized_name = (name or "tool").strip()
    args = arguments if isinstance(arguments, dict) else {}
    canonical_call = RuntimeCompositeToolCallFactory.create(normalized_name, args)
    normalized_name = canonical_call.tool_name
    lower_name = normalized_name.lower()
    args = canonical_call.arguments
    target = summary

    if lower_name == "manor":
        manor_params = args.get("params") if isinstance(args.get("params"), dict) else {}
        action = str(
            _first_argument(args, "action", "operation", "tool", "name") or ""
        ).strip().lower()
        query = _param_text(
            _first_argument(args, "query", "q", "search", "keyword")
            or _first_argument(manor_params, "query", "q", "search", "keyword")
        )
        doc_target = _param_text(
            _path_basename(
                _first_argument(args, "path", "file", "filename", "name")
                or _first_argument(manor_params, "path", "file", "filename", "name")
            )
        )
        if action in {"list_tasks", "tasks"}:
            return {
                "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.manor.list_tasks",
                "display_params": {"target": "tasks"},
            }
        if action in {"create_task", "add_task"}:
            return {
                "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.manor.create_task",
                "display_params": {"target": target or "task"},
            }
        if action in {"update_task", "complete_task", "delete_task"}:
            return {
                "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.manor.update_task",
                "display_params": {"target": target or "task"},
            }
        if action in {"list_documents", "list_files", "documents", "files"}:
            count = _result_count(result)
            params: dict[str, str | int] = {"target": "documents"}
            if count is not None:
                params["count"] = count
            return {
                "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.manor.list_documents",
                "display_params": params,
            }
        if action in {"get_document", "read_document", "read_file", "get_file"}:
            return {
                "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.manor.read_document",
                "display_params": {"target": doc_target or target or "document"},
            }
        if action == "search_documents":
            return {
                "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.manor.search_documents",
                "display_params": {"target": query or target or "documents"},
            }
        if "search" in action:
            return {
                "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.manor.search",
                "display_params": {"target": query or target or "knowledge"},
            }
        if action:
            return {
                "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.manor.action",
                "display_params": {"action": _pretty_identifier(action)},
            }
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.manor.action",
            "display_params": {"action": "Manor"},
        }

    mcp = _mcp_parts(lower_name)
    if mcp:
        server, tool = mcp
        return _mcp_display_metadata(server, tool, args, summary=target)

    if lower_name == "invoke_skill":
        skill = _param_text(_first_argument(args, "slug", "skill", "name") or target)
        display_skill = (skill_display_names or {}).get(skill.casefold(), skill)
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.skill",
            "display_params": {"target": display_skill or "skill"},
        }
    if lower_name == "search_tools":
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.discovery.search_tools",
            "display_params": {"target": _tool_target(args, fallback=target or "tools")},
        }
    if lower_name == "list_skills":
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.discovery.list_skills",
            "display_params": {"target": "skills"},
        }
    if lower_name == "get_skill_details":
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.discovery.skill_details",
            "display_params": {"target": _tool_target(args, fallback=target or "skill")},
        }
    if lower_name == "rag":
        rag_has_content = (
            result.get("content_evidence_available")
            if isinstance(result, dict)
            else None
        )
        return {
            "display_key": (
                f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.knowledge.rag_no_content"
                if rag_has_content is False
                else f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.knowledge.rag"
            ),
            "display_params": {"target": _tool_target(args, fallback=target or "knowledge")},
        }
    if lower_name == "search_documents":
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.knowledge.document_search",
            "display_params": {"target": _tool_target(args, fallback=target or "documents")},
        }
    if lower_name == "search_tasks":
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.workspace.search",
            "display_params": {"target": _tool_target(args, fallback=target or "workspace")},
        }
    if lower_name.startswith("workspace_"):
        workspace_action = (
            str(args.get("action") or "").strip().lower()
            if lower_name == "workspace_agent"
            else ""
        )
        workspace_args = (
            args.get("params")
            if workspace_action and isinstance(args.get("params"), dict)
            else args
        )
        if lower_name in {"workspace_search", "workspace_list_knowledge"} or workspace_action in {
            "search",
            "list_knowledge",
            "get_goal_status",
            "visualize_ledgers",
        }:
            key = "workspace.search"
        elif lower_name == "workspace_create_task" or workspace_action == "create_task":
            key = "workspace.create_task"
        elif lower_name == "workspace_update_task_runtime" or workspace_action == "update_task_runtime":
            key = "workspace.update_task"
        elif "knowledge" in lower_name or "knowledge" in workspace_action:
            key = "workspace.knowledge"
        elif "rule" in lower_name or "rule" in workspace_action:
            key = "workspace.rule"
        elif "review" in lower_name or "review" in workspace_action:
            key = "workspace.review"
        else:
            key = "workspace.operate"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {
                "target": _tool_target(workspace_args, fallback=target or "workspace")
            },
        }
    if lower_name.startswith("ws_"):
        if "search" in lower_name:
            key = "workspace.search"
        elif "lint" in lower_name or "get_draft" in lower_name:
            key = "workspace.review"
        elif "rule" in lower_name:
            key = "workspace.rule"
        else:
            key = "workspace.operate"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": _tool_target(args, fallback=target or "workspace")},
        }
    if lower_name in {"web_search", "browse_web"}:
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.web.search",
            "display_params": {"target": _tool_target(args, fallback=target or "web")},
        }
    if lower_name == "web_fetch":
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.web.fetch",
            "display_params": {"target": _tool_target(args, fallback=target or "web page")},
        }
    if lower_name.startswith("sandbox_") or lower_name in {"list_sandbox_files", "save_sandbox_file"}:
        if lower_name == "sandbox_exec":
            key = "sandbox.exec"
        elif lower_name in {"sandbox_read_file", "list_sandbox_files"}:
            key = "sandbox.read"
        elif lower_name in {"sandbox_write_file", "sandbox_save_result", "save_sandbox_file"}:
            key = "sandbox.write"
        elif lower_name == "sandbox_create":
            key = "sandbox.create"
        elif lower_name == "sandbox_destroy":
            key = "sandbox.destroy"
        else:
            key = "sandbox.use"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": _tool_target(args, fallback=target or "sandbox")},
        }
    if lower_name in {"read_file", "list_files", "glob_files", "grep_files", "delete_file", "write_file", "edit_file"}:
        if lower_name in {"grep_files", "glob_files"}:
            key = "file.search"
        elif lower_name in {"read_file", "list_files"}:
            key = "file.read"
        elif lower_name == "delete_file":
            key = "file.delete"
        elif lower_name == "edit_file":
            key = "file.edit"
        else:
            key = "file.write"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": _tool_target(args, fallback=target or "file")},
        }
    if lower_name in {"browse_web", "take_screenshot", "interact_with_page"}:
        if "screenshot" in lower_name or "extract" in lower_name:
            key = "browser.observe"
        elif "interact" in lower_name or "perform" in lower_name or "login" in lower_name:
            key = "browser.interact"
        else:
            key = "browser.open"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": _tool_target(args, fallback=target or "browser")},
        }
    if lower_name in {"send_email", "delete_message", "move_message", "mark_read", "mark_unread"}:
        key = "email.send" if lower_name == "send_email" else "email.use"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": _tool_target(args, fallback=target or "email")},
        }
    if any(token in lower_name for token in ("send_message", "direct_message", "group_message", "send_document", "send_photo", "send_image_message", "send_template_message", "send_text_message")):
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.message.send",
            "display_params": {"target": _tool_target(args, fallback=target or "message")},
        }
    if lower_name == "me":
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.social.search",
            "display_params": {"target": _tool_target(args, fallback=target or "profile")},
        }
    if any(token in lower_name for token in ("post_tweet", "delete_tweet", "comment_on_post", "post_comment", "add_reaction", "daily_posts")):
        if any(token in lower_name for token in ("delete", "comment", "reaction")):
            key = "social.interact"
        elif "daily_posts" in lower_name:
            key = "social.search"
        else:
            key = "social.publish"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": _tool_target(args, fallback=target or "social")},
        }
    if lower_name in {"new_chat", "continue_chat", "ask", "follow_up", "code", "review", "check_path"}:
        if lower_name == "check_path":
            key = "coding.check"
        elif lower_name in {"code", "review"}:
            key = "coding.review" if lower_name == "review" else "coding.run"
        else:
            key = "ai_web.ask"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": _tool_target(args, fallback=target or _display_name(normalized_name))},
        }
    if lower_name in {"extract", "extract_data"}:
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.web.fetch",
            "display_params": {"target": _tool_target(args, fallback=target or "content")},
        }
    if lower_name in {"provision_agent", "find_team_members", "notify_user"}:
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.workspace.operate",
            "display_params": {"target": _tool_target(args, fallback=target or "workspace")},
        }
    if lower_name in {"start_workspace_draft"}:
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.workspace.operate",
            "display_params": {"target": _tool_target(args, fallback=target or "workspace draft")},
        }
    if lower_name in {"nango_proxy", "nango_list_connections", "nango_list_providers"}:
        key = "integration.list" if "list" in lower_name else "integration.manage"
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.{key}",
            "display_params": {"target": _tool_target(args, fallback=target or "integration")},
        }
    if lower_name in {"create_skill", "update_skill", "delete_skill"}:
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.skill",
            "display_params": {"target": _tool_target(args, fallback=target or "skill")},
        }
    if lower_name in {
        "compose_music",
        "text_to_dialogue",
        "text_to_speech",
        "generate_sound_effect",
        "normalize_audio_loudness",
        "upload_media",
    }:
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.media.generate",
            "display_params": {"target": _tool_target(args, fallback=target or "media")},
        }
    if lower_name in {
        "create_scheduled_job",
        "list_scheduled_jobs",
        "cancel_scheduled_job",
        "toggle_scheduled_job",
        "run_scheduled_job_now",
    }:
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.automation.manage",
            "display_params": {"target": _tool_target(args, fallback=target or "automation")},
        }
    if lower_name in {
        "generate_file",
        "generate_image",
        "generate_video",
        "wait_media_jobs",
        "merge_videos",
        "align_subtitles",
        "compose_video_timeline",
    }:
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.media.generate",
            "display_params": {"target": _tool_target(args, fallback=target or _display_name(normalized_name))},
        }
    if lower_name in {"bash", "shell", "run_command", "exec_command"} or any(
        token in lower_name for token in ("run", "exec", "command")
    ):
        command = _param_text(_first_argument(args, "cmd", "command") or target)
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.run",
            "display_params": {"target": command or _display_name(normalized_name)},
        }
    if any(token in lower_name for token in ("search", "grep", "rg")):
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.search",
            "display_params": {"target": target or _display_name(normalized_name)},
        }
    if any(token in lower_name for token in ("list", "browse")):
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.browse",
            "display_params": {"target": target or _display_name(normalized_name)},
        }
    if "read" in lower_name or lower_name.startswith("get_"):
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.read",
            "display_params": {"target": target or _display_name(normalized_name)},
        }
    if any(token in lower_name for token in ("write", "generate", "create")):
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.generate",
            "display_params": {"target": target or _display_name(normalized_name)},
        }
    if any(token in lower_name for token in ("edit", "patch", "update", "modify")):
        return {
            "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.modify",
            "display_params": {"target": target or _display_name(normalized_name)},
        }
    return {
        "display_key": f"{ASSISTANT_PROCESS_TOOL_KEY_PREFIX}.generic",
        "display_params": {"target": _display_name(normalized_name)},
    }


def _tool_summary(name: str, arguments: Any) -> str:
    if not isinstance(arguments, dict):
        return _preview_json(arguments, max_chars=96)
    if name in {"read_file", "write_file", "edit_file", "generate_file"}:
        target = arguments.get("path") or arguments.get("file") or arguments.get("filename") or arguments.get("name")
        return _path_basename(target)
    if name in {"search_code", "search_files", "grep", "rg"}:
        return _compact_text(arguments.get("query") or arguments.get("pattern"), max_chars=96)
    target = (
        arguments.get("name")
        or arguments.get("path")
        or arguments.get("output_name")
        or arguments.get("filename")
        or arguments.get("query")
        or arguments.get("action")
    )
    return _compact_text(target, max_chars=96)


class AssistantBlocksBuilder:
    """Build UI-oriented assistant content blocks from stream text and tool events."""

    def __init__(self, *, skill_display_names: Mapping[str, str] | None = None) -> None:
        self._blocks: list[dict[str, Any]] = []
        self._skill_display_names = {
            str(key).strip().casefold(): str(value).strip()
            for key, value in (skill_display_names or {}).items()
            if str(key or "").strip() and str(value or "").strip()
        }
        self._text_seq = 0
        self._process_seq = 0
        self._step_seq = 0
        self._visualization_seq = 0
        self._surface_seq = 0
        self._active_text: dict[str, Any] | None = None
        self._process: dict[str, Any] | None = None
        self._bound_opening_text_chars = 0
        self._has_final_text = False

    def append_text(self, text: str, *, phase: str = "opening", after_step_seq: int | None = None) -> None:
        if not text:
            return
        if phase == "opening" and self._process is not None:
            phase = "final"
        if self._has_final_text and phase not in {"final", "progress"}:
            phase = "final"
        if phase == "progress" and after_step_seq is None and self._process is not None:
            after_step_seq = len(self._process.get("steps") or [])
        if self._active_text and self._active_text.get("phase") == phase:
            if phase == "progress" and self._active_text.get("after_step_seq") != after_step_seq:
                self._active_text = None
            else:
                self._active_text["text"] = f"{self._active_text.get('text', '')}{text}"
                return
        self._text_seq += 1
        block = {
            "id": f"blk_text_{self._text_seq}",
            "type": "text",
            "phase": phase,
            "text": text,
        }
        if phase == "progress" and after_step_seq is not None:
            block["after_step_seq"] = int(after_step_seq)
        self._blocks.append(block)
        self._active_text = block

    def reset_text(self) -> None:
        self._blocks = [block for block in self._blocks if block.get("type") != "text"]
        self._active_text = None
        self._text_seq = 0
        self._bound_opening_text_chars = 0

    def start_tool(
        self,
        name: str,
        arguments: Any = None,
        *,
        now_ms: int | None = None,
        assistant_text: str | None = None,
    ) -> None:
        process = self._ensure_process()
        self._active_text = None
        self._step_seq += 1
        step = {
            "id": f"step_{self._step_seq}",
            "seq": self._step_seq,
            "kind": "tool",
            "name": name or "tool",
            "display_name": _display_name(name or "tool"),
            "status": "running",
            "arguments_preview": _preview_json(arguments),
        }
        summary = _tool_summary(name or "tool", arguments)
        if summary:
            step["summary"] = summary
        bound_assistant_text = assistant_text.strip() if assistant_text else ""
        if bound_assistant_text:
            step["assistant_text"] = bound_assistant_text
        step.update(
            _tool_display_metadata(
                name or "tool",
                arguments,
                summary=summary,
                skill_display_names=self._skill_display_names,
            )
        )
        if now_ms is not None:
            step["started_at_ms"] = int(now_ms)
        process["steps"].append({key: value for key, value in step.items() if value not in ("", None)})
        self._refresh_process_status()

    def end_tool(
        self,
        name: str,
        *,
        result: Any = None,
        structured_result: Any = None,
        status: str | None = None,
        duration_ms: int | float | None = None,
        arguments: Any = None,
        now_ms: int | None = None,
    ) -> None:
        process = self._ensure_process()
        wanted = name or "tool"
        step = None
        for candidate in reversed(process["steps"]):
            if candidate.get("name") == wanted and candidate.get("status") == "running":
                step = candidate
                break
        if step is None:
            self.start_tool(wanted, arguments)
            step = process["steps"][-1]
        step["status"] = status or "success"
        preview = _preview_json(result, max_chars=500)
        if preview:
            step["result_preview"] = preview
        display_arguments = arguments
        if display_arguments is None:
            display_arguments = _parse_preview_json(step.get("arguments_preview"))
        summary = step.get("summary") or _tool_summary(wanted, display_arguments)
        step.update(
            _tool_display_metadata(
                wanted,
                display_arguments,
                result=result,
                summary=str(summary or ""),
                skill_display_names=self._skill_display_names,
            )
        )
        if duration_ms is not None:
            step["duration_ms"] = int(duration_ms)
        if now_ms is not None:
            step["ended_at_ms"] = int(now_ms)
        canonical_call = RuntimeCompositeToolCallFactory.create(
            wanted,
            display_arguments if isinstance(display_arguments, dict) else {},
        )
        expected_visualization_kind = {
            "visualize_workspace_ledgers": "workspace_ledger_overview",
            "query_ledger": "ledger_query_result",
        }.get(canonical_call.tool_name)
        if (
            canonical_call.tool_name == "workspace_agent"
            and str(canonical_call.arguments.get("action") or "").strip().lower()
            == "visualize_ledgers"
        ):
            expected_visualization_kind = "workspace_ledger_overview"
        if expected_visualization_kind:
            visualization = _workspace_ledger_visualization(
                structured_result if structured_result is not None else result,
                expected_kind=expected_visualization_kind,
            )
            if visualization is not None:
                coalesced = (
                    expected_visualization_kind == "ledger_query_result"
                    and self._coalesce_ledger_query_visualization(visualization)
                )
                if not coalesced:
                    self._visualization_seq += 1
                    self._blocks.append(
                        {
                            "id": f"blk_visualization_{self._visualization_seq}",
                            "type": "visualization",
                            **visualization,
                        }
                    )
                self._active_text = None
        if wanted == "render_response_surface":
            from packages.core.services.response_surfaces import (
                response_surface_from_tool_result,
            )

            surface = response_surface_from_tool_result(
                structured_result if structured_result is not None else result
            )
            if surface is not None:
                self._surface_seq += 1
                self._blocks.append({
                    "id": f"blk_surface_{self._surface_seq}",
                    "type": "surface",
                    **surface,
                })
                self._active_text = None
        self._refresh_process_status()

    def _coalesce_ledger_query_visualization(
        self,
        visualization: dict[str, Any],
    ) -> bool:
        incoming_data = visualization.get("data")
        if not isinstance(incoming_data, dict):
            return False
        if incoming_data.get("coalesce_previous_visualization") is True:
            for index in range(len(self._blocks) - 1, -1, -1):
                block = self._blocks[index]
                existing_data = block.get("data")
                if (
                    block.get("type") != "visualization"
                    or block.get("kind") != "ledger_query_result"
                    or not isinstance(existing_data, dict)
                    or any(
                        existing_data.get(field) != incoming_data.get(field)
                        for field in ("contract_id", "view")
                    )
                ):
                    continue
                relation = _ledger_query_relation(existing_data, incoming_data)
                if relation == "incoming_subset":
                    return True
                if relation in {"incoming_replacement", "incoming_superset"}:
                    self._blocks[index] = {
                        "id": block["id"],
                        "type": "visualization",
                        **visualization,
                    }
                    return True
                return False
            return False

        superseded_indices: list[int] = []
        for index, block in enumerate(self._blocks):
            if (
                block.get("type") != "visualization"
                or block.get("kind") != "ledger_query_result"
                or not isinstance(block.get("data"), dict)
            ):
                continue
            relation = _ledger_query_relation(block["data"], incoming_data)
            if relation == "incoming_subset":
                return True
            if relation in {"incoming_replacement", "incoming_superset"}:
                superseded_indices.append(index)
        if not superseded_indices:
            return False

        target_index = superseded_indices[0]
        replacement = {
            "id": self._blocks[target_index]["id"],
            "type": "visualization",
            **visualization,
        }
        superseded = set(superseded_indices)
        self._blocks = [
            replacement if index == target_index else block
            for index, block in enumerate(self._blocks)
            if index == target_index or index not in superseded
        ]
        return True

    def set_final_text(self, text: str) -> None:
        if not text:
            return
        final_text = text.strip()
        self._remove_unbound_opening_text_matching_final(final_text)
        existing_opening = self._opening_text().strip()
        if existing_opening and final_text.startswith(existing_opening):
            final_text = final_text[len(existing_opening):].lstrip()
        if not final_text:
            return
        existing_final_blocks = [
            block
            for block in self._blocks
            if block.get("type") == "text" and block.get("phase") == "final"
        ]
        existing_final_text = "".join(str(block.get("text") or "") for block in existing_final_blocks)
        if existing_final_text.strip() == final_text:
            self._active_text = existing_final_blocks[-1] if existing_final_blocks else None
            self._has_final_text = True
            self._refresh_process_status(force_completed=True)
            return
        if existing_final_blocks and final_text.startswith(existing_final_text.strip()):
            first = existing_final_blocks[0]
            first["text"] = final_text
            for block in existing_final_blocks[1:]:
                block["text"] = ""
            self._blocks = [
                block
                for block in self._blocks
                if not (
                    block.get("type") == "text"
                    and block.get("phase") == "final"
                    and not str(block.get("text") or "")
                )
            ]
            self._active_text = first
            self._has_final_text = True
            self._refresh_process_status(force_completed=True)
            return
        self._active_text = None
        self.append_text(final_text, phase="final")
        self._has_final_text = True
        self._refresh_process_status(force_completed=True)

    def start_final_summary(self) -> None:
        self._active_text = None
        self._has_final_text = True
        self._refresh_process_status(force_completed=True)

    def set_process_note(self, text: str | None) -> None:
        note = str(text or "").strip()
        if not note:
            return
        process = self._ensure_process()
        process["note"] = note

    def blocks(self) -> list[dict[str, Any]]:
        self._refresh_process_status()
        return self._blocks

    def meta(self) -> dict[str, Any]:
        blocks = self.blocks()
        if not blocks:
            return {}
        return {
            "assistant_blocks_schema": ASSISTANT_BLOCKS_SCHEMA,
            "assistant_blocks": blocks,
        }

    def _ensure_process(self) -> dict[str, Any]:
        if self._process is None:
            self._process_seq += 1
            self._process = {
                "id": f"blk_process_{self._process_seq}",
                "type": "process",
                "title": "Processing",
                "status": "running",
                "default_collapsed": False,
                "steps": [],
            }
            self._blocks.append(self._process)
        return self._process

    def _opening_text(self) -> str:
        return "".join(
            str(block.get("text") or "")
            for block in self._blocks
            if block.get("type") == "text" and block.get("phase") == "opening"
        )

    def _remove_unbound_opening_text_matching_final(self, final_text: str) -> None:
        opening_blocks = [
            block
            for block in self._blocks
            if block.get("type") == "text" and block.get("phase") == "opening"
        ]
        if not opening_blocks:
            return
        opening_text = "".join(str(block.get("text") or "") for block in opening_blocks)
        if len(opening_text) <= self._bound_opening_text_chars:
            return
        bound_text = opening_text[: self._bound_opening_text_chars]
        unbound_text = opening_text[self._bound_opening_text_chars :]
        if not unbound_text.strip() or not final_text.startswith(unbound_text.strip()):
            return

        remaining = len(bound_text)
        for block in opening_blocks:
            block_text = str(block.get("text") or "")
            if remaining >= len(block_text):
                remaining -= len(block_text)
                continue
            if remaining > 0:
                block["text"] = block_text[:remaining]
                remaining = 0
            else:
                block["text"] = ""
        self._blocks = [
            block
            for block in self._blocks
            if not (
                block.get("type") == "text"
                and block.get("phase") == "opening"
                and not str(block.get("text") or "")
            )
        ]
        if self._active_text and self._active_text not in self._blocks:
            self._active_text = None

    def _refresh_process_status(self, *, force_completed: bool = False) -> None:
        process = self._process
        if not process:
            return
        steps = process.get("steps") or []
        has_running = any(step.get("status") == "running" for step in steps)
        has_error = any(step.get("status") == "error" for step in steps)
        if has_error:
            process["status"] = "error"
            process["default_collapsed"] = bool(force_completed or self._has_final_text)
        elif has_running and not force_completed:
            process["status"] = "running"
            process["default_collapsed"] = False
        else:
            process["status"] = "completed"
            process["default_collapsed"] = bool(force_completed or self._has_final_text)
        durations = [
            int(step.get("duration_ms"))
            for step in steps
            if isinstance(step.get("duration_ms"), int | float)
        ]
        timestamped_steps = [
            step
            for step in steps
            if isinstance(step.get("started_at_ms"), int | float)
            and isinstance(step.get("ended_at_ms"), int | float)
        ]
        if steps and len(timestamped_steps) == len(steps):
            started_at_ms = min(int(step["started_at_ms"]) for step in timestamped_steps)
            ended_at_ms = max(int(step["ended_at_ms"]) for step in timestamped_steps)
            process["duration_ms"] = max(0, ended_at_ms - started_at_ms)
        elif durations and not timestamped_steps:
            process["duration_ms"] = sum(durations)
        else:
            process.pop("duration_ms", None)


def assistant_blocks_stream_payload(builder: AssistantBlocksBuilder) -> dict[str, Any]:
    """Return the current assistant block state for SSE payloads."""
    return builder.meta()
