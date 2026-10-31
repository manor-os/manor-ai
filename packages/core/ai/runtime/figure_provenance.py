"""Business figures handed to another Agent must come from somewhere.

Asked to "handle a test promotion" with no numbers, the Workspace Agent wrote
"原价38元、成本10元、每天50单" into a delegation brief; the child Agents
computed on them and the reply presented the result as a conclusion. A
figure in a brief is sourced when the user said it, a tool returned it, or
the Workspace context carries it, or when it is one arithmetic step from
sourced figures. Anything else goes back to the Agent before the delegation
runs: ask the user, or mark it as an assumption the user asked for.
"""
from __future__ import annotations

import itertools
import json
import re
from typing import Any

# A number the brief states as a business quantity: money, counts, rates, in
# either order ("38元", "售价38"). List markers, times and dates are not
# figures, nor small counts that carry no money or rate ("写3条文案").
_FIGURE = re.compile(
    r"[¥￥$]?\s*(?<![\d.:/-])(\d+(?:\.\d+)?)(?![\d:/])"
    r"(\s*(?:元|块|人次|人|位|杯|单|件|份|%|％|折|次|万|千))?"
)
_PRICED = re.compile(r"元|块|%|％|折")
_NOT_FIGURE = re.compile(
    r"^\s*\d+\s*[.、)）]\s"          # a list marker at the start of a line
    r"|\d{1,2}:\d{2}"                 # a time
    r"|\d{4}[-/年]\d{1,2}"             # a date
)
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_UNIT = re.compile(r"\s*(?:元|块|人次|人|位|杯|单|件|份|%|％|折|次|万|千)")
_ASSUMPTION = re.compile(r"假设|假定|示例|举例|估计|估算|大概|大致|\bassum|\bhypothetical|\bexample\b|\bestimate", re.I)
_SENTENCE = re.compile(r"[。！？!?\n；;]+")

DELEGATION_TOOLS = frozenset({"delegate_to_agent"})
UNSOURCED_FIGURES_CODE = "unsourced_figures"


def _value(text: str) -> float:
    return round(float(text), 6)


def _text_of(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text_of(item) for item in value)
    if isinstance(value, dict):
        if isinstance(value.get("text"), str):
            return value["text"]
        return "\n".join(_text_of(item) for item in value.values())
    return ""


class FigureSources:
    """Numbers put in front of the Agent, split by how far they can be trusted.

    - The user's own turns source figures exactly and through one arithmetic step.
    - Tool results source the numbers they state exactly: they are full of
      incidental numbers (ids, times), and sums of those would make any value
      look derived.
    - System text (the system prompt, the per-turn Workspace context) sources
      only figures it states with a unit ("目标 20 人"); its guidance prose is
      full of bare numbers that would source anything.
    - The Agent's own words never do: a figure it invented does not become
      sourced by repetition.
    """

    def __init__(self, messages: list[dict[str, Any]] | None = None):
        from packages.core.ai.runtime.agentic_loop_prompts import runtime_is_host_feedback_message

        self.exact: set[float] = set()
        self.user: set[float] = set()
        # Assumed figures are allowed only when the user asked for an estimate
        # or example; otherwise "假设" is a label on an invented number.
        self.assumptions_allowed = False
        for message in messages or []:
            role = message.get("role") if isinstance(message, dict) else None
            text = _text_of(message.get("content")) if role else ""
            if role == "user" and not runtime_is_host_feedback_message(message):
                values = {_value(m.group()) for m in _NUMBER.finditer(text)}
                self.user |= values
                self.assumptions_allowed |= bool(_ASSUMPTION.search(text))
            elif role == "tool":
                values = {_value(m.group()) for m in _NUMBER.finditer(text)}
            elif role == "system":
                values = {value for sentence in _SENTENCE.split(text)
                          for shown, value in _figures(sentence) if _UNIT.search(shown)}
            else:
                continue
            self.exact |= values

    def sourced(self, value: float) -> bool:
        """Stated exactly, or one arithmetic step (or a percent/折 reading) from the user's figures."""
        if value in self.exact:
            return True
        for number in self.user:
            if value in {round(number / 100, 6), round(number * 100, 6), round(number / 10, 6), round(number * 10, 6)}:
                return True
        for a, b in itertools.permutations(sorted(self.user)[:40], 2):
            candidates = {a + b, a - b, a * b}
            if b:
                candidates.add(a / b)
            if any(abs(value - c) < 0.005 for c in candidates):
                return True
        return False


def source_numbers(messages: list[dict[str, Any]]) -> FigureSources:
    return FigureSources(messages)


def _figures(sentence: str) -> list[tuple[str, float]]:
    found = []
    blanked = _NOT_FIGURE.sub(lambda m: " " * len(m.group()), sentence)
    for match in _FIGURE.finditer(blanked):
        value = _value(match.group(1))
        priced = bool(match.group(2) and _PRICED.search(match.group(2))) or match.group().lstrip().startswith(("¥", "￥", "$"))
        if value <= 10 and value == int(value) and not priced:
            continue
        found.append((match.group().strip(), value))
    return found


def unsourced_figures(text: str, sources: FigureSources) -> list[str]:
    """Figures in ``text`` with no source.

    Sentences that call their figures assumptions are skipped only when the
    user asked for an estimate or example.
    """
    missing: list[str] = []
    for sentence in _SENTENCE.split(text or ""):
        if sources.assumptions_allowed and _ASSUMPTION.search(sentence):
            continue
        for shown, value in _figures(sentence):
            if not sources.sourced(value) and shown not in missing:
                missing.append(shown)
    return missing


def delegation_figures_refusal(
    tool_name: str, arguments: Any, messages: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """The tool error for a delegation brief that states unsourced figures, if any."""
    if tool_name not in DELEGATION_TOOLS:
        return None
    raw = arguments if isinstance(arguments, (dict, list, str)) else {}
    brief = _text_of(raw if not isinstance(raw, dict) else {
        key: value for key, value in raw.items() if not str(key).startswith("_")
    })
    if isinstance(raw, dict) and isinstance(raw.get("tasks"), str):
        try:
            brief += "\n" + _text_of(json.loads(raw["tasks"]))
        except (TypeError, ValueError):
            pass
    missing = unsourced_figures(brief, source_numbers(messages))
    if not missing:
        return None
    from packages.core.ai.runtime.tool_error_contract import tool_error_payload

    return tool_error_payload(
        tool_name=tool_name, code=UNSOURCED_FIGURES_CODE,
        message=(
            "The brief states figures nobody gave you: " + ", ".join(missing[:12]) + ". The Agents "
            "would compute on them and the result would read as fact. Ask the user for the missing "
            "figures before delegating; labelling them assumptions does not source them unless the "
            "user asked for an estimate or example. Nothing was delegated."
        ),
        details={"unsourced": missing[:12], "action_executed": False},
    )
