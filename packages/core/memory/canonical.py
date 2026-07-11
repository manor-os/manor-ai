"""Canonical Markdown memory docs for workspace and workspace-agent evolution.

The older workspace memory layout stores many small frontmatter notes under
``guidance/``, ``decisions/``, and similar folders. Those notes remain the
search/index layer. The files in this module are the human-readable operating
memory layer: fixed docs the runtime can load predictably and agents can update
without inventing new places to store durable behavior.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Iterable

from packages.core.memory.paths import workspace_memory_root


@dataclass(frozen=True)
class CanonicalMemoryFile:
    filename: str
    description: str
    default_body: str
    default_load: bool = True


WORKSPACE_MEMORY_FILES: dict[str, CanonicalMemoryFile] = {
    "WORKSPACE.md": CanonicalMemoryFile(
        "WORKSPACE.md",
        "Workspace charter, operating model, active scope, and current priorities.",
        """# Workspace Operating Memory

## Purpose
Describe what this workspace is responsible for and what success looks like.

## Current Operating Model
- Goals:
- Services / agents:
- Channels:

## Update Policy
Keep this file short. Move large source material to Knowledge and reference it
from KNOWLEDGE.md instead of pasting it here.

## Generated Runtime Caches
- STATE.md records current goals, agents, active work, recent tasks, and plans.
- FILES.md is the workspace file wiki: what exists, what it is, and where it lives.
""",
    ),
    "STATE.md": CanonicalMemoryFile(
        "STATE.md",
        "Generated workspace status cache: goals, agents, current work, recent tasks, and plans.",
        """# Workspace State Cache

<!-- manor-generated: workspace-state-cache -->

This file is generated from Manor runtime state.

## Cache Policy
- Do not store durable human-authored guidance here.
- Use WORKSPACE.md, RULES.md, MEMORY.md, or LEARNINGS.md for operating memory.
- Refresh happens before Strategist review and after task finalization.
""",
    ),
    "FILES.md": CanonicalMemoryFile(
        "FILES.md",
        "Generated workspace file wiki: what files exist, what they are, and where to find them.",
        """# Workspace Files Wiki

<!-- manor-generated: workspace-files-cache -->

This file is generated from workspace documents and task output artifacts.

## Cache Policy
- Use this as the first lookup for workspace files and generated artifacts.
- Keep large file contents in Knowledge or the referenced filesystem path.
- Database document rows and task outputs remain the source of truth.
""",
    ),
    "RULES.md": CanonicalMemoryFile(
        "RULES.md",
        "Durable workspace-wide rules, approvals, prohibitions, and guardrails.",
        """# Workspace Rules

## Approval Rules
- External publishing/sending must follow runtime guardrails and approval gates.

## Never-Allow Rules
- Do not weaken workspace guardrails without explicit operator confirmation.

## Task-Local vs Workspace-Wide
Only promote a user instruction here when it should affect future work in this
workspace. Task-only instructions belong in task runtime context.
""",
    ),
    "KNOWLEDGE.md": CanonicalMemoryFile(
        "KNOWLEDGE.md",
        "Map of Knowledge collections and when agents must search/cite them.",
        """# Workspace Knowledge Map

## Default Retrieval Policy
- Search Workspace Knowledge before answering or executing document-dependent work.
- Cite document names or source snippets when Knowledge changes the answer.

## Collections and Use Cases
- Add each important Knowledge collection here with the situations where it applies.

## Do Not Memorize Source Documents
Large policies, contracts, manuals, and wiki pages stay in Knowledge. This file
only records when and how to retrieve them.
""",
    ),
    "MEMORY.md": CanonicalMemoryFile(
        "MEMORY.md",
        "Stable workspace facts, user preferences, and durable decisions.",
        """# Workspace Memory

## Stable Facts
- Add durable facts that should shape future work.

## User Preferences
- Add workspace-specific preferences after they are confirmed or repeatedly observed.

## Decisions
- Summarize important decisions and link to the source task, chat, or evidence when possible.
""",
    ),
    "LEARNINGS.md": CanonicalMemoryFile(
        "LEARNINGS.md",
        "Execution outcomes, repeated failures, calibration notes, and improvement loops.",
        """# Workspace Learnings

## What Works
- Capture reusable execution patterns that improved outcomes.

## What Failed
- Capture repeated failures and what the workspace should do differently next time.

## Promotion Rule
Promote only durable patterns here. Keep one-off logs in runtime evidence.
""",
    ),
    "TOOLS.md": CanonicalMemoryFile(
        "TOOLS.md",
        "Workspace-specific tool, integration, and platform usage guidance.",
        """# Workspace Tool Memory

## Available Platforms
- List workspace-scoped platforms and when to use them.

## Tool Patterns
- Record reliable tool sequences and common failure handling.

## Safety
- Respect workspace rules and runtime tool policy before external actions.
""",
    ),
    "RUNBOOKS.md": CanonicalMemoryFile(
        "RUNBOOKS.md",
        "Repeatable playbooks for recurring workspace work.",
        """# Workspace Runbooks

## Recurring Processes
- Add short step-by-step playbooks for common work.

## Escalation
- Define when the workspace agent should ask the operator or route to a human.
""",
    ),
    "AGENTS.md": CanonicalMemoryFile(
        "AGENTS.md",
        "Workspace agent roster, role boundaries, and workspace-specific overrides.",
        """# Workspace Agents

## Roster
- List active agents/services and their responsibilities in this workspace.

## Override Policy
Workspace-agent overrides beat global agent defaults when operating inside this
workspace. Promote an override to global agent memory only after it proves useful
across multiple workspaces.
""",
        default_load=False,
    ),
}

WORKSPACE_AGENT_MEMORY_FILES: dict[str, CanonicalMemoryFile] = {
    "AGENT.md": CanonicalMemoryFile(
        "AGENT.md",
        "Workspace-specific role and responsibility override for this agent.",
        "# Workspace-Agent Role Override\n\nAdd workspace-specific role guidance here.\n",
    ),
    "RULES.md": CanonicalMemoryFile(
        "RULES.md",
        "Workspace-specific rules for this agent.",
        "# Workspace-Agent Rules\n\nAdd agent-specific workspace rules here.\n",
    ),
    "TOOLS.md": CanonicalMemoryFile(
        "TOOLS.md",
        "Workspace-specific tool guidance for this agent.",
        "# Workspace-Agent Tool Memory\n\nAdd workspace-specific tool patterns here.\n",
    ),
    "RUNBOOKS.md": CanonicalMemoryFile(
        "RUNBOOKS.md",
        "Workspace-specific runbooks this agent should follow.",
        "# Workspace-Agent Runbooks\n\nAdd repeatable procedures here.\n",
    ),
    "LEARNINGS.md": CanonicalMemoryFile(
        "LEARNINGS.md",
        "Workspace-specific lessons learned by this agent.",
        "# Workspace-Agent Learnings\n\nAdd durable execution lessons here.\n",
    ),
}

WORKSPACE_MEMORY_DEFAULT_LOAD_ORDER = (
    "WORKSPACE.md",
    "RULES.md",
    "MEMORY.md",
    "LEARNINGS.md",
    "KNOWLEDGE.md",
    "TOOLS.md",
    "RUNBOOKS.md",
    "STATE.md",
    "FILES.md",
)

_SAFE_RE = re.compile(r"[^A-Za-z0-9_\-]")
_FILE_PROMPT_MAX_CHARS = 1800
_TOTAL_PROMPT_MAX_CHARS = 9000
_CANONICAL_FILE_SOFT_LIMIT_CHARS = 16_000
_RUNTIME_LEARNING_BLOCK_RE = re.compile(
    r"\n?<!-- runtime-learning:[^>]+ -->\n.*?(?:\n<!-- /runtime-learning -->|(?=\n<!-- runtime-learning:|\Z))",
    re.DOTALL,
)
_RUNTIME_LEARNING_SUMMARY_RE = re.compile(
    r"\n?<!-- runtime-learning-summary -->\n.*?\n<!-- /runtime-learning-summary -->",
    re.DOTALL,
)


def _safe_segment(value: str) -> str:
    value = str(value or "").strip()
    value = _SAFE_RE.sub("-", value).strip("-")
    return value or "default"


def _truncate_middle(text: str, max_chars: int, *, label: str = "truncated") -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 12:
        return text[:max_chars]
    marker = f"\n... [{label}; approx {len(text) - max_chars} chars omitted] ...\n"
    if len(marker) + 24 >= max_chars:
        return text[: max_chars - 3].rstrip() + "..."
    remaining = max_chars - len(marker)
    head = max(1, remaining * 2 // 3)
    tail = max(1, remaining - head)
    return text[:head].rstrip() + marker + text[-tail:].lstrip()


def _write_if_missing(path: str, body: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            f.write(body.rstrip() + "\n")


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            content = f.read().strip()
        return content or None
    except FileNotFoundError:
        return None


def ensure_workspace_memory_docs(
    entity_id: str,
    workspace_id: str,
    *,
    workspace_name: str | None = None,
    workspace_kind: str | None = None,
) -> dict[str, str]:
    """Ensure fixed workspace memory docs exist and return filename -> path."""
    root = workspace_memory_root(entity_id, workspace_id)
    os.makedirs(root, exist_ok=True)
    paths: dict[str, str] = {}
    for filename, spec in WORKSPACE_MEMORY_FILES.items():
        path = os.path.join(root, filename)
        body = spec.default_body
        if filename == "WORKSPACE.md" and (workspace_name or workspace_kind):
            body = _workspace_doc_template(workspace_name, workspace_kind)
        if (
            filename == "WORKSPACE.md"
            and (workspace_name or workspace_kind)
            and _read(path) == spec.default_body.strip()
        ):
            with open(path, "w", encoding="utf-8") as f:
                f.write(body.rstrip() + "\n")
        else:
            _write_if_missing(path, body)
        paths[filename] = path
    return paths


def workspace_agent_memory_dir(entity_id: str, workspace_id: str, agent_key: str) -> str:
    return os.path.join(
        workspace_memory_root(entity_id, workspace_id),
        "agents",
        _safe_segment(agent_key),
    )


def ensure_workspace_agent_memory_docs(
    entity_id: str,
    workspace_id: str,
    agent_key: str,
) -> dict[str, str]:
    """Ensure workspace-specific memory docs for an agent/subscription."""
    root = workspace_agent_memory_dir(entity_id, workspace_id, agent_key)
    paths: dict[str, str] = {}
    for filename, spec in WORKSPACE_AGENT_MEMORY_FILES.items():
        path = os.path.join(root, filename)
        _write_if_missing(path, spec.default_body)
        paths[filename] = path
    return paths


def read_workspace_memory_file(entity_id: str, workspace_id: str, filename: str) -> str | None:
    if filename not in WORKSPACE_MEMORY_FILES:
        raise ValueError(f"Unknown workspace memory file: {filename}")
    return _read(os.path.join(workspace_memory_root(entity_id, workspace_id), filename))


def write_workspace_memory_file(entity_id: str, workspace_id: str, filename: str, content: str) -> str:
    if filename not in WORKSPACE_MEMORY_FILES:
        raise ValueError(f"Unknown workspace memory file: {filename}")
    path = os.path.join(workspace_memory_root(entity_id, workspace_id), filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write((content or "").rstrip() + "\n")
    return path


def read_workspace_agent_memory_file(
    entity_id: str,
    workspace_id: str,
    agent_key: str,
    filename: str,
) -> str | None:
    if filename not in WORKSPACE_AGENT_MEMORY_FILES:
        raise ValueError(f"Unknown workspace-agent memory file: {filename}")
    return _read(os.path.join(workspace_agent_memory_dir(entity_id, workspace_id, agent_key), filename))


def write_workspace_agent_memory_file(
    entity_id: str,
    workspace_id: str,
    agent_key: str,
    filename: str,
    content: str,
) -> str:
    if filename not in WORKSPACE_AGENT_MEMORY_FILES:
        raise ValueError(f"Unknown workspace-agent memory file: {filename}")
    path = os.path.join(workspace_agent_memory_dir(entity_id, workspace_id, agent_key), filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write((content or "").rstrip() + "\n")
    return path


def append_workspace_memory_block(
    entity_id: str,
    workspace_id: str,
    filename: str,
    block: str,
    *,
    marker: str,
) -> dict[str, object]:
    """Append a managed runtime-learning block to a fixed workspace doc.

    The block is idempotent by marker and only compacts prior Manor-managed
    runtime-learning blocks, never user-authored prose.
    """
    ensure_workspace_memory_docs(entity_id, workspace_id)
    existing = read_workspace_memory_file(entity_id, workspace_id, filename) or ""
    return _append_managed_block(
        existing,
        filename=filename,
        block=block,
        marker=marker,
        writer=lambda next_content: write_workspace_memory_file(entity_id, workspace_id, filename, next_content),
        kind="workspace_memory_file",
    )


def append_workspace_agent_memory_block(
    entity_id: str,
    workspace_id: str,
    agent_key: str,
    filename: str,
    block: str,
    *,
    marker: str,
) -> dict[str, object]:
    """Append a managed block to a workspace-agent override doc."""
    ensure_workspace_agent_memory_docs(entity_id, workspace_id, agent_key)
    existing = read_workspace_agent_memory_file(entity_id, workspace_id, agent_key, filename) or ""
    return _append_managed_block(
        existing,
        filename=filename,
        block=block,
        marker=marker,
        writer=lambda next_content: write_workspace_agent_memory_file(
            entity_id,
            workspace_id,
            agent_key,
            filename,
            next_content,
        ),
        kind="workspace_agent_file",
    )


def load_workspace_operating_memory(
    entity_id: str,
    workspace_id: str,
    *,
    filenames: Iterable[str] | None = None,
    max_chars: int = _TOTAL_PROMPT_MAX_CHARS,
) -> str:
    """Load bounded fixed workspace memory docs for prompt injection."""
    selected = list(filenames) if filenames else [
        name
        for name in WORKSPACE_MEMORY_DEFAULT_LOAD_ORDER
        if WORKSPACE_MEMORY_FILES[name].default_load
    ]
    parts: list[str] = []
    for filename in selected:
        if filename not in WORKSPACE_MEMORY_FILES:
            continue
        content = read_workspace_memory_file(entity_id, workspace_id, filename)
        if not content:
            continue
        used = sum(len(p) for p in parts) + (2 * len(parts))
        remaining = max_chars - used
        if parts:
            remaining -= 2
        header = f"### {filename}\n"
        content_budget = min(_FILE_PROMPT_MAX_CHARS, remaining - len(header))
        if content_budget <= 80:
            break
        block = f"{header}{_compact_memory_doc_for_prompt(content, filename=filename, max_chars=content_budget)}"
        if used + len(block) + (2 if parts else 0) > max_chars:
            block = f"{header}{_truncate_middle(content, content_budget, label='memory doc budget')}"
        parts.append(block)
    return "\n\n".join(parts)


def _compact_memory_doc_for_prompt(content: str, *, filename: str, max_chars: int) -> str:
    """Keep prompt memory bounded without losing runtime-learning provenance.

    A plain middle truncate can keep the rule text while dropping the
    ``runtime-learning:<id>`` marker that explains where the rule came from.
    For managed docs, summarize runtime-learning blocks first so the prompt
    keeps recent evidence ids and the durable guidance.
    """
    text = content or ""
    if len(text) <= max_chars:
        return text

    without_summary = _RUNTIME_LEARNING_SUMMARY_RE.sub("", text)
    blocks = list(_RUNTIME_LEARNING_BLOCK_RE.finditer(without_summary))
    if not blocks:
        return _truncate_middle(text, max_chars, label="memory doc budget")

    user_authored = _RUNTIME_LEARNING_BLOCK_RE.sub("", without_summary).strip()
    summaries = [_summarize_managed_block(match.group(0)) for match in blocks]
    summaries = [s for s in summaries if s]
    summary = _managed_summary_block(summaries, filename=filename, compacted_count=len(blocks))
    user_budget = max(240, max_chars // 3)
    compacted = (
        f"{_truncate_middle(user_authored, user_budget, label='user-authored memory budget')}\n\n{summary}"
        if user_authored
        else summary
    )
    return _truncate_middle(compacted, max_chars, label="memory doc budget")


def load_workspace_agent_memory(
    entity_id: str,
    workspace_id: str,
    agent_key: str | None,
    *,
    max_chars: int = 2600,
) -> str:
    """Load bounded workspace-agent override docs if they exist."""
    if not agent_key:
        return ""
    root = workspace_agent_memory_dir(entity_id, workspace_id, agent_key)
    parts: list[str] = []
    for filename in WORKSPACE_AGENT_MEMORY_FILES:
        content = _read(os.path.join(root, filename))
        if not content:
            continue
        block = f"### {filename}\n{_truncate_middle(content, 800, label='workspace-agent memory budget')}"
        if sum(len(p) for p in parts) + len(block) + 2 > max_chars:
            break
        parts.append(block)
    return "\n\n".join(parts)


def _workspace_doc_template(workspace_name: str | None, workspace_kind: str | None) -> str:
    name = workspace_name or "this workspace"
    kind = f"\n- Kind: {workspace_kind}" if workspace_kind else ""
    return f"""# Workspace Operating Memory

## Purpose
{name} exists to coordinate autonomous work for this workspace.{kind}

## Current Operating Model
- Goals:
- Services / agents:
- Channels:

## Update Policy
Keep this file short. Move large source material to Knowledge and reference it
from KNOWLEDGE.md instead of pasting it here.

## Generated Runtime Caches
- STATE.md records current goals, agents, active work, recent tasks, and plans.
- FILES.md is the workspace file wiki: what exists, what it is, and where it lives.
"""


def _append_managed_block(
    existing: str,
    *,
    filename: str,
    block: str,
    marker: str,
    writer,
    kind: str,
) -> dict[str, object]:
    if marker and marker in existing:
        return {
            "kind": kind,
            "filename": filename,
            "already_present": True,
            "file_size_chars": len(existing),
        }
    compacted_existing, compaction = _compact_managed_blocks(existing, filename=filename)
    next_content = (
        f"{compacted_existing.rstrip()}\n\n{block.rstrip()}\n"
        if compacted_existing.strip()
        else f"{block.rstrip()}\n"
    )
    path = writer(next_content)
    return {
        "kind": kind,
        "filename": filename,
        "path": path,
        "already_present": False,
        "compacted": compaction["compacted_blocks"] > 0 or len(next_content) > _CANONICAL_FILE_SOFT_LIMIT_CHARS,
        "compacted_blocks": compaction["compacted_blocks"],
        "file_size_chars": len(next_content),
        "over_soft_limit": len(next_content) > _CANONICAL_FILE_SOFT_LIMIT_CHARS,
    }


def _compact_managed_blocks(existing: str, *, filename: str) -> tuple[str, dict[str, int]]:
    if len(existing or "") <= _CANONICAL_FILE_SOFT_LIMIT_CHARS:
        return existing or "", {"compacted_blocks": 0}
    without_summary = _RUNTIME_LEARNING_SUMMARY_RE.sub("", existing or "")
    blocks = list(_RUNTIME_LEARNING_BLOCK_RE.finditer(without_summary))
    if not blocks:
        return existing or "", {"compacted_blocks": 0}
    user_authored = _RUNTIME_LEARNING_BLOCK_RE.sub("", without_summary).rstrip()
    summaries = [_summarize_managed_block(match.group(0)) for match in blocks]
    summaries = [s for s in summaries if s]
    summary = _managed_summary_block(summaries, filename=filename, compacted_count=len(blocks))
    next_text = f"{user_authored}\n\n{summary}\n" if user_authored else f"{summary}\n"
    return next_text, {"compacted_blocks": len(blocks)}


def _summarize_managed_block(block: str) -> str:
    marker_match = re.search(r"<!--\s*(runtime-learning:[^>]+?)\s*-->", block or "")
    marker = marker_match.group(1) if marker_match else ""
    fields: dict[str, str] = {}
    for raw in block.splitlines():
        line = raw.strip()
        if line.startswith("- Agent profile update:"):
            fields["agent"] = line.replace("- Agent profile update:", "", 1).strip()
        elif line.startswith("- Workspace memory:"):
            fields["workspace"] = line.replace("- Workspace memory:", "", 1).strip()
        elif line.startswith("- Rule/guidance:"):
            fields["rule"] = line.replace("- Rule/guidance:", "", 1).strip()
        elif line.startswith("- Tool pattern that worked:"):
            fields["tool"] = line.replace("- Tool pattern that worked:", "", 1).strip()
        elif line.startswith("- Learning:"):
            fields["learning"] = line.replace("- Learning:", "", 1).strip()
        elif line.startswith("- Summary:"):
            fields["summary"] = line.replace("- Summary:", "", 1).strip()
    for key in ("agent", "workspace", "rule", "tool", "learning", "summary"):
        summary = fields.get(key)
        if not summary:
            continue
        compact = _truncate_middle(summary, 220)
        return f"{marker}: {compact}" if marker else compact
    return marker


def _managed_summary_block(summaries: list[str], *, filename: str, compacted_count: int) -> str:
    lines = [
        "<!-- runtime-learning-summary -->",
        "## Runtime Learning Summary",
        (
            f"- Manor compacted {compacted_count} managed runtime-learning entries in `{filename}` "
            "to keep this operating memory bounded."
        ),
    ]
    for item in summaries[-12:]:
        lines.append(f"- {item}")
    lines.append("- Full original context remains available in runtime evidence.")
    lines.append("<!-- /runtime-learning-summary -->")
    return "\n".join(lines)
