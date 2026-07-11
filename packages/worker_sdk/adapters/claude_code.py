"""Claude Code subprocess adapter.

Wraps the ``claude`` CLI in non-interactive (``-p``) mode behind a
ManorWorker handler. Use as:

    from manor_worker_sdk import ManorWorker
    from manor_worker_sdk.adapters.claude_code import register_claude_code

    worker = ManorWorker(endpoint=..., worker_id=..., secret=...)
    register_claude_code(worker)
    await worker.run_forever()

The handler covers two lease kinds:
  * ``llm``      — single non-interactive prompt → Claude → text result
  * ``code``     — same, framed as "write code to do X" with the
                    workspace mounted (the user's repo is the cwd)

Cost reporting tries to parse Claude Code's stderr usage line; if not
present, returns zero so Manor's per-lease cost defaults to zero
(operator can wire a per-token rate later).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from typing import Any, Optional

from packages.worker_sdk.types import Lease, LeaseResult
from packages.worker_sdk.worker import LeaseContext, ManorWorker

logger = logging.getLogger(__name__)


_USAGE_LINE = re.compile(
    r"input[_ ]tokens[:=]?\s*(?P<input>\d+).+?output[_ ]tokens[:=]?\s*(?P<output>\d+)",
    re.IGNORECASE | re.DOTALL,
)


class ClaudeCodeNotInstalled(RuntimeError):
    """The ``claude`` binary is not on PATH."""


def find_claude_binary() -> str:
    """Locate the claude CLI. Honours ``CLAUDE_BIN`` env override for
    non-standard installs."""
    env_path = os.environ.get("CLAUDE_BIN")
    if env_path and os.path.isfile(env_path):
        return env_path
    located = shutil.which("claude")
    if not located:
        raise ClaudeCodeNotInstalled(
            "could not find 'claude' on PATH — install Claude Code or "
            "set CLAUDE_BIN env var to the binary path"
        )
    return located


async def claude_code_handle(lease: Lease, ctx: LeaseContext) -> LeaseResult:
    """Generic handler for kind=llm and kind=code leases."""
    prompt = (lease.params.get("prompt") or lease.params.get("user_prompt") or "").strip()
    if not prompt:
        raise ValueError("Claude Code handler requires lease.params.prompt")

    binary = find_claude_binary()
    cwd = lease.params.get("cwd")  # optional working directory hint

    # Non-interactive print mode (-p) returns assistant output to stdout.
    args = [binary, "-p", prompt]
    if model := lease.params.get("model"):
        args.extend(["--model", str(model)])

    env = os.environ.copy()
    # Help non-interactive sessions skip any TTY prompts.
    env.setdefault("CLAUDE_NON_INTERACTIVE", "1")

    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
    )
    stdout_b, stderr_b = await proc.communicate()
    stdout = stdout_b.decode("utf-8", errors="replace").rstrip()
    stderr = stderr_b.decode("utf-8", errors="replace")

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude exited {proc.returncode}: {stderr.strip() or stdout.strip()}"
        )

    cost = _maybe_parse_cost(stderr)
    return LeaseResult(
        result={"text": stdout, "model": lease.params.get("model")},
        cost=cost,
    )


def register_claude_code(
    worker: ManorWorker,
    *,
    handler=claude_code_handle,
    kinds: tuple[str, ...] = ("llm", "code"),
) -> None:
    """Bind the Claude Code handler to a worker for the given kinds.

    Default kinds cover plain LLM steps and code-generation steps.
    For ``action`` kind you'd write a different handler — Claude Code
    isn't an integration adapter.
    """
    for kind in kinds:
        worker.handle(kind=kind, provider=None)(handler)


def _maybe_parse_cost(stderr: str) -> Optional[dict]:
    m = _USAGE_LINE.search(stderr)
    if not m:
        return None
    try:
        return {
            "llm_tokens_input": int(m.group("input")),
            "llm_tokens_output": int(m.group("output")),
            "usd": 0,
        }
    except (TypeError, ValueError):
        return None
