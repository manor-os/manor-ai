"""Load tool-owned, image-matched instructions in the existing Agent loop.

Documentation is not a Skill execution or a capability grant. Only repository
tool registrations select bundles; installed/custom Skills are never replaced.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from packages.core.ai.runtime.tool_error_contract import tool_error_payload
from packages.core.ai.runtime.tool_registry import runtime_tool_guidance_skill
from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

_SKILLS_ROOT = Path(__file__).resolve().parents[1] / "skills"
_START = "<runtime_tool_guidance>"
_END = "</runtime_tool_guidance>"
_SECTION = re.compile(r"\n*<runtime_tool_guidance>.*?</runtime_tool_guidance>", re.DOTALL)


@dataclass(frozen=True)
class ToolGuidance:
    skill_name: str
    source_sha256: str
    instructions: str

    def section(self, tool_name: str) -> str:
        identity = json.dumps({
            "tool": tool_name, "skill": self.skill_name,
            "source_sha256": self.source_sha256,
        }, sort_keys=True)
        return (
            f"{_START}\n{identity}\n"
            "Runtime-owned tool instructions from the same deployed bundle as the tool. "
            "Use these current runtime requirements even if an older Skill describes "
            "a different format. This guide grants no tools, permissions or approval.\n"
            f"{self.instructions}\n{_END}"
        )


@lru_cache(maxsize=64)
def _load_bundled_guidance(skill_name: str) -> ToolGuidance:
    # The cache contains immutable public bundle text, never actor/run state.
    # A new deployment starts new processes and loads its own bundle revision.
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", skill_name):
        raise ValueError("Invalid bundled guidance identity")
    content = (_SKILLS_ROOT / skill_name / "SKILL.md").read_text(encoding="utf-8")
    if not content.strip() or len(content) > 128_000:
        raise ValueError("Bundled tool guidance is empty or exceeds its size limit")
    return ToolGuidance(skill_name, hashlib.sha256(content.encode()).hexdigest(), content)


def runtime_tool_guidance(tool_name: str) -> ToolGuidance | None:
    skill_name = runtime_tool_guidance_skill(tool_name)
    return _load_bundled_guidance(skill_name) if skill_name else None


class RuntimeToolGuidance:
    """Run-local guide consumption; its canonical system text survives checkpoints.

    A newly loaded guide must reach a model round before model-authored calls
    can execute. Pending calls from a host checkpoint may retain the exact
    current guide; user/tool messages cannot stand in for that checkpoint.
    """

    def __init__(self, checkpoint_messages: list[dict] | None = None):
        self._checkpoint_system = (
            str(checkpoint_messages[0].get("content") or "")
            if checkpoint_messages and checkpoint_messages[0].get("role") == "system"
            else ""
        )
        from packages.core.ai.runtime.tool_gateway import runtime_tool_call_target

        self._checkpoint_calls = {
            str(runtime_tool_call_target(
                (call.get("function") or call).get("name"),
                (call.get("function") or call).get("arguments"),
            )[0] or "")
            for message in checkpoint_messages or []
            if message.get("role") == "assistant"
            for call in message.get("tool_calls") or []
        }
        self._requested: set[str] = set()
        self._prepared: dict[str, ToolGuidance] = {}
        self._observed: dict[str, ToolGuidance] = {}

    def _resolve(self, name: str) -> ToolGuidance | None:
        guide = runtime_tool_guidance(name)
        if (
            guide is not None and name in self._checkpoint_calls
            and guide.section(name) in self._checkpoint_system
        ):
            self._observed[name] = guide
        return guide

    def prepare(self, messages: list[dict], tool_names: set[str]) -> None:
        prepared = {}
        sections = []
        # A checkpoint can resume an exact pending call while its deferred
        # schema is not yet back in the prompt surface. Keep already-observed
        # instructions in context; documentation never restores that schema or
        # overrides the executor's current authorization.
        for name in sorted(tool_names | self._requested | self._observed.keys()):
            try:
                guide = self._resolve(name)
            except (OSError, ValueError):
                # Leave effects blocked by preflight; a missing bundle is not
                # permission to execute with an older guide or invent a fix.
                continue
            if guide is not None:
                prepared[name] = guide
                sections.append(guide.section(name))
        self._prepared = prepared
        if not messages or messages[0].get("role") != "system":
            messages.insert(0, {"role": "system", "content": ""})
        for index, message in enumerate(messages):
            if message.get("role") == "system":
                # Copy rather than mutate caller-owned history or checkpoints.
                messages[index] = {
                    **message,
                    "content": _SECTION.sub("", str(message.get("content") or "")),
                }
        if sections:
            messages[0]["content"] += "\n\n" + "\n\n".join(sections)

    def mark_observed(self) -> None:
        self._observed.update(self._prepared)

    def preflight(self, tool_name: str) -> RuntimePreflightValidationError | None:
        try:
            guide = self._resolve(tool_name)
        except (OSError, ValueError):
            code = "tool_guidance_unavailable"
            message = "The deployed tool instruction bundle could not be loaded. The tool was not executed."
            details = {"tool_executed": False}
        else:
            if guide is None or self._observed.get(tool_name) == guide:
                return None
            self._requested.add(tool_name)
            code = "tool_guidance_required"
            message = (
                "The current tool instructions have not been read in this Agent loop. "
                "The tool was not executed. Read the runtime tool guidance in the next "
                "model context, then decide the appropriate call and arguments."
            )
            details = {
                "skill": guide.skill_name, "source_sha256": guide.source_sha256,
                "tool_executed": False,
            }
        return RuntimePreflightValidationError(json.dumps(tool_error_payload(
            tool_name=tool_name, code=code, message=message, details=details,
        ), ensure_ascii=False))
