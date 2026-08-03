"""Runtime boundary for L1 consolidation summarization (M15 L1 白名单).

Consolidators are the *observation-only* half of the decision layer: they
summarize facts, the Strategist decides (裁定 1). L0 is deterministic
SQL/Python; L1 is the narrow whitelist where a single LLM call per domain
per review turns unstructured text (execution errors, edit field
aggregates) into a short neutral *label* — never into advice.

That invariant is enforced twice, on purpose:

* schema side — ``FORBIDDEN_KEYS`` in
  ``packages/core/consolidators/contract.py`` rejects any
  recommendation/priority/next_step key reaching a persisted report;
* prompt side — :data:`RUNTIME_CONSOLIDATOR_L1_SYSTEM_PROMPT` below
  forbids the model from recommending, prioritizing, or proposing
  actions at all.

Callers live in ``packages/core/consolidators/l1.py``; they never import
``llm_client`` directly and never raise a runtime failure into a review.
"""
from __future__ import annotations

import json
from typing import Any

from packages.core.ai.runtime.completions import (
    RuntimeTextCompletionResult,
    runtime_execute_text_completion,
    runtime_one_shot_messages,
)
from packages.core.ai.runtime.sources import RUNTIME_CONSOLIDATOR_L1_SOURCE

# Small, cheap, deterministic: the L1 layer labels, it does not reason.
RUNTIME_CONSOLIDATOR_L1_TEMPERATURE = 0.0
RUNTIME_CONSOLIDATOR_L1_MAX_TOKENS = 400

RUNTIME_CONSOLIDATOR_L1_SYSTEM_PROMPT = (
    "You are a summarization function inside an observation-only analytics "
    "pipeline. You group observed records and give each group a short, "
    "neutral factual label.\n\n"
    "Hard rules:\n"
    "- You MUST NOT recommend, advise, prioritize, rank by importance, "
    "propose next steps, or suggest any action, fix, or change.\n"
    "- You MUST NOT speculate about causes you cannot see in the records, "
    "and MUST NOT judge people, teams, or services.\n"
    "- Labels are noun phrases describing what was observed (e.g. "
    "\"upstream API timeout\", \"missing output field\"), at most 8 words, "
    "no verbs in the imperative mood, no words like should/must/需要/建议.\n"
    "- Output JSON only: no prose, no explanation, no markdown fences.\n"
    "- If the records do not support any grouping, output []."
)


def runtime_consolidator_l1_user_prompt(
    *,
    instruction: str,
    payload: Any,
    output_schema_hint: str,
) -> str:
    """Build the L1 user prompt: instruction + compact JSON records + shape."""

    records = json.dumps(payload, ensure_ascii=False, default=str)
    return (
        f"{instruction.strip()}\n\n"
        f"Records (JSON array, index = position in the array):\n{records}\n\n"
        f"Respond with ONLY a JSON array of this shape:\n{output_schema_hint.strip()}"
    )


def runtime_consolidator_l1_messages(
    *,
    instruction: str,
    payload: Any,
    output_schema_hint: str,
) -> list[dict[str, str]]:
    """Runtime one-shot messages for an L1 consolidation summarization."""

    return runtime_one_shot_messages(
        system_prompt=RUNTIME_CONSOLIDATOR_L1_SYSTEM_PROMPT,
        user_prompt=runtime_consolidator_l1_user_prompt(
            instruction=instruction,
            payload=payload,
            output_schema_hint=output_schema_hint,
        ),
    )


async def runtime_execute_consolidator_l1_completion(
    *,
    instruction: str,
    payload: Any,
    output_schema_hint: str,
    entity_id: str | None = None,
    workspace_id: str | None = None,
) -> RuntimeTextCompletionResult:
    """Execute one L1 consolidation summarization with Runtime-owned defaults."""

    return await runtime_execute_text_completion(
        runtime_consolidator_l1_messages(
            instruction=instruction,
            payload=payload,
            output_schema_hint=output_schema_hint,
        ),
        entity_id=entity_id,
        workspace_id=workspace_id,
        source=RUNTIME_CONSOLIDATOR_L1_SOURCE,
        temperature=RUNTIME_CONSOLIDATOR_L1_TEMPERATURE,
        max_tokens=RUNTIME_CONSOLIDATOR_L1_MAX_TOKENS,
    )
