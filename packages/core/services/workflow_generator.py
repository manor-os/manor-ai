"""Workflow generator — LLM-powered AI-edit for the visual workflow canvas.

Mirrors the agent generator (``agent_generator.py``): a natural-language
request is turned into a canonical workflow graph — the same
``WorkflowDefinition.steps`` shape the importer produces. Powers the n8n-style
"AI edit" panel: describe a workflow (or a change to the current one) and the
model returns the full updated step list, which the UI applies to the canvas.

Streaming: yields ``("step", label)`` progress frames then a terminal
``("workflow", {name, steps, variables})`` — emitting a step before the slow
completion keeps the HTTP connection alive past Cloudflare's 100s 524 timeout
and lets the UI narrate the build.
"""
from __future__ import annotations

import json
import logging
from typing import Any, AsyncIterator, Tuple

from packages.core.ai.runtime.completions import runtime_execute_text_completion
from packages.core.ai.workflow_import.model import CANONICAL_NODE_TYPES
from packages.core.services.skill_bundle import extract_json_object

logger = logging.getLogger(__name__)

_SOURCE = "workflow_generator"

# Loose synonyms → canonical type, so a slightly-off model output still lands.
_TYPE_SYNONYMS = {
    "if": "condition",
    "ifelse": "condition",
    "branch": "condition",
    "set": "transform",
    "edit": "transform",
    "map": "transform",
    "function": "code",
    "script": "code",
    "httprequest": "http",
    "request": "http",
    "api": "http",
    "knowledge": "rag",
    "retrieval": "rag",
    "vectorstore": "rag",
    "classify": "classifier",
    "router": "switch",
    "webhook": "webhook",
    "schedule": "trigger",
    "cron": "trigger",
    "start": "trigger",
    "manual": "trigger",
    "delay": "wait",
    "sleep": "wait",
    "email": "notify",
    "slack": "notify",
    "message": "notify",
    "aggregate": "merge",
    "join": "merge",
    "fanout": "parallel",
    "split": "parallel",
    "foreach": "loop",
    "iterate": "loop",
    "imagegen": "image",
    "image_generation": "image",
    "text_to_image": "image",
    "videogen": "video",
    "tts": "audio",
    "mcp": "connector",
    "integration": "connector",
}

# webhook is a first-class trigger variant in the canvas vocabulary.
_ALLOWED_TYPES = CANONICAL_NODE_TYPES | {"webhook"}

WORKFLOW_GEN_SYSTEM_PROMPT = """\
You design automation workflows for a visual node-graph builder (like n8n / \
Dify). Given a natural-language request — and, when present, the workflow the \
user already has — output a single JSON object describing the COMPLETE updated \
workflow.

The JSON object must contain exactly these keys:
  name      - short human-friendly workflow name
  steps     - ordered list of node objects (see below)
  variables - object of workflow-level variables (use {} if none)

Each node in "steps" is an object:
  id         - short unique string id (e.g. "n1", "score_lead")
  type       - one of the CANONICAL TYPES below — never invent a type
  name       - short human label shown on the node
  config     - object of node settings (see per-type hints); {} if none
  next       - list of node ids this node flows into (control flow)
  true_next  - (condition only) ids taken when the condition is TRUE
  false_next - (condition only) ids taken when the condition is FALSE

CANONICAL TYPES (pick the closest fit; do not use any other value). The config
keys below are EXACTLY what the runner reads — use these names, not synonyms:
  trigger    - entry point (manual / schedule / event). Every workflow starts with one. config: {}
  webhook    - entry point fired by an inbound HTTP call. config: {}
  llm        - call a language model. config: {model?, system_prompt?, prompt, temperature?, max_rounds?}
               (temperature 0–2; max_rounds = tool-call rounds, default 1)
  agent      - run an existing manor Agent. config: {agent_id?, input, model?}
  rag        - retrieve from a knowledge base. config: {query, limit?, workspace_id?, collection?}
  tool       - call a single tool. config: {tool, args}   (args is an object)
  connector  - call an external integration / MCP server. config: {tool, args}
               (tool = resolved name e.g. "mcp__slack__post_message")
  code       - run Python / JavaScript / Bash in an ephemeral restricted sandbox.
               `inputs` contains workflow variables; print the result to stdout.
               config: {language, code, requirements?, code_timeout?, output_format?, allow_network?}
  http       - make an HTTP request. config: {method, url, headers?, body?, timeout?, output_var?}
  condition  - if/else branch. Put the test in config.expression and wire the
               branches with true_next / false_next. config: {expression}
  switch     - multi-way branch. config: {cases: [{expression, next: [ids]}], default_next: [ids]}
  loop       - repeat over items. config: {items, item_var?, max_iterations?, steps?}
               (steps = inline sub-step objects run per item)
  parallel   - fan out to several branches at once. config: {steps: [sub-step objects]}
  merge      - join multiple branches back together. config: {sources: [ids], mode?}
  subworkflow- run another workflow inline and return its result. config: {workflow_id}
               (its result — the child's variables — is this step's output)
  transform  - set workflow variables. config: {set: {var_name: "value or {{expr}}"}}
  extract    - pull structured fields from text with an LLM → JSON object.
               config: {input, schema}  (schema = "name, email, amount" or a JSON shape)
  filter     - keep list items matching a condition. config: {items, item_var?, condition}
               (condition uses bare names like the IF node, e.g. item.score >= 70)
  aggregate  - reduce a list. config: {items, operation, field?, separator?}
               (operation: count|sum|avg|min|max|join|first|last|collect)
  datetime   - produce / format / shift a date. config: {operation, value?, format?, amount?, unit?}
               (operation: now|format|add|subtract)
  split      - explode a list field (or delimited string) into a flat list.
               config: {items, field?, separator?}
  limit      - keep the first/last N items of a list. config: {items, max, keep?}
  respond    - return an HTTP response to a webhook caller (webhook flows only).
               config: {body, status_code?}
  sort       - sort a list. config: {items, field?, order?}  (order: asc|desc)
  dedupe     - remove duplicate list items. config: {items, field?}
  stop       - deliberately fail/stop the run. config: {message}
  extractfromfile - parse text content as JSON or CSV. config: {input, format?}
  classifier - classify input. Runs as a tool-less LLM — describe the categories
               INSIDE the prompt. config: {model?, prompt}
  wait       - pause or delay the run. A "timer" wait of ≤90s runs inline and
               auto-continues; longer timers schedule their own resume;
               "approval"/"event" pause for an external resume.
               config: {wait_type: "approval"|"timer"|"event", duration_seconds?, message}
  notify     - send a notification (email / slack / message). config: {channel, message}
  end        - terminal node. config: {}
  image      - generate an image. config: {prompt, model?, size?, quality?, reference_url?}
               (size: 1024x1024|1536x1024|1024x1536; quality: low|medium|high;
                reference_url = Knowledge path / URL to edit or style-match)
  video      - generate a video. config: {prompt, model?, duration?, resolution?, aspect_ratio?}
               (duration seconds: 4|5|6|8|10|12|15; resolution: 480p|720p|1080p;
                aspect_ratio: 16:9|9:16|1:1|4:3|3:4|21:9)
  audio      - generate audio / speech. config: {prompt, model?}

DATA FLOW — how a step uses an earlier step's result:
- Every step's output is auto-available as ``{{<that step's id>}}``. Reference
  upstream results with the step's OWN id — never invent variable names.
- Output shapes (use the right path):
    llm / agent / classifier → text          → ``{{id}}``
    rag                      → {context, sources} → ``{{id.context}}``
    http                     → {status_code, body} → ``{{id.body}}``
    image / video / audio    → {image_url, ...}    → ``{{id.image_url}}``
    transform                → the {set} object     → ``{{id.var_name}}``
  Example: a rag step ``r`` feeding an llm step → the llm prompt is
  ``"Summarise these notes:\n{{r.context}}"``. An llm ``a`` feeding a translate
  llm ``b`` → b's prompt is ``"Translate to Spanish:\n{{a}}"``.
- For ``condition`` / ``switch`` expressions, reference variables by BARE name
  (no braces): ``a == "high"``, ``r.source_count > 0`` — operators
  == != > < >= <= combined with and / or.

Rules:
- Always begin with exactly one trigger (or webhook) node.
- Wire data flow: when a step needs an earlier step's result, put ``{{<id>}}``
  (or ``{{<id>.field}}``) into its prompt / input / body — using the real
  upstream step id, NOT a made-up name like ``{{results}}``.
- Wire the graph with "next" (and true_next/false_next for conditions). Every
  non-terminal node should point to at least one other node; the last node(s)
  should be "end" or have an empty "next".
- Keep ids unique and referenced ids valid (they must exist in "steps").
- When the user already has a workflow, return the FULL updated steps (the edit
  applied), preserving existing ids where the node is unchanged.
- Fill in the config keys that matter for the request (prompt, size, duration,
  temperature, limit, …) — be concrete, don't leave a node bare when a setting
  is implied (e.g. "a 9:16 short video" → set aspect_ratio: "9:16").
- ``model?`` keys are optional catalog model ids. OMIT them unless the user
  named a specific model — blank means "use the account's default for this
  kind", which is almost always what you want. Never invent a model id.
- Prefer the smallest graph that satisfies the request; be concrete in config.

Output ONLY the JSON object — no markdown fences, no commentary."""


def _coerce_type(raw: object) -> str:
    t = str(raw or "").strip().lower().replace("-", "").replace(" ", "")
    if t in _ALLOWED_TYPES:
        return t
    if t in _TYPE_SYNONYMS:
        return _TYPE_SYNONYMS[t]
    return "unsupported"


def _as_id_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and str(v).strip()]
    return []


def normalize_workflow(raw: dict[str, Any]) -> dict[str, Any]:
    """Validate + coerce the model's JSON into a safe WorkflowDefinition shape.

    Tolerant by design: unknown node types degrade to ``unsupported`` (kept
    visible rather than crashing), ids are de-duplicated/synthesised, and any
    ``next`` reference to a non-existent node is dropped.
    """
    name = str(raw.get("name") or "Untitled workflow").strip()[:120]
    variables = raw.get("variables")
    if not isinstance(variables, dict):
        variables = {}

    raw_steps = raw.get("steps")
    if not isinstance(raw_steps, list):
        raw_steps = []

    steps: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for i, node in enumerate(raw_steps):
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "").strip() or f"n{i + 1}"
        while node_id in seen_ids:
            node_id = f"{node_id}_{i}"
        seen_ids.add(node_id)

        node_type = _coerce_type(node.get("type"))
        label = str(node.get("name") or node_type.title()).strip()[:80]
        config = node.get("config")
        if not isinstance(config, dict):
            config = {}

        step: dict[str, Any] = {
            "id": node_id,
            "type": node_type,
            "name": label,
            "config": config,
            "next": _as_id_list(node.get("next")),
        }
        if node_type == "condition":
            step["true_next"] = _as_id_list(node.get("true_next"))
            step["false_next"] = _as_id_list(node.get("false_next"))
        steps.append(step)

    # Drop edges that point at non-existent nodes.
    valid = {s["id"] for s in steps}
    for s in steps:
        for key in ("next", "true_next", "false_next"):
            if key in s:
                s[key] = [ref for ref in s[key] if ref in valid]

    return {"name": name, "steps": steps, "variables": variables}


async def generate_workflow_streaming(
    prompt: str,
    current_steps: list[dict] | None,
    entity_id: str,
) -> AsyncIterator[Tuple[str, object]]:
    """Generate / edit a workflow graph, yielding progress then the result.

    Yields ``("step", label)`` tuples, then a final
    ``("workflow", {name, steps, variables})`` tuple.
    """
    has_current = bool(current_steps)
    yield ("step", "Refining the workflow" if has_current else "Designing the workflow")

    user_parts = [f"Request:\n{prompt}"]
    if has_current:
        user_parts.append(
            "Current workflow steps (apply the change and return the full "
            "updated list, preserving unchanged ids):\n"
            + json.dumps(current_steps, ensure_ascii=False)
        )
    user_content = "\n\n".join(user_parts)

    completion = await runtime_execute_text_completion(
        [
            {"role": "system", "content": WORKFLOW_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        entity_id=entity_id,
        source=_SOURCE,
        temperature=0.3,
        max_tokens=8000,
    )
    raw = completion.content
    if not raw or not raw.strip():
        raise ValueError("LLM returned an empty response for workflow generation")

    yield ("step", "Validating nodes")
    result = normalize_workflow(extract_json_object(raw))
    if not result["steps"]:
        raise ValueError("Workflow generation did not produce any nodes")

    logger.info(
        "Generated workflow '%s' (%d nodes) for entity %s",
        result["name"], len(result["steps"]), entity_id,
    )
    yield ("workflow", result)


async def generate_workflow(
    prompt: str,
    current_steps: list[dict] | None,
    entity_id: str,
) -> dict[str, Any]:
    """Generate a workflow graph without exposing the streaming transport.

    The canvas uses :func:`generate_workflow_streaming` so it can display
    progress over SSE. Agent tools need the same generator as a regular async
    service call. Keeping this adapter here makes AI Edit and conversational
    workflow authoring share one prompt, normalizer, and node vocabulary.
    """
    result: dict[str, Any] | None = None
    async for kind, payload in generate_workflow_streaming(
        prompt,
        current_steps,
        entity_id,
    ):
        if kind == "workflow" and isinstance(payload, dict):
            result = payload
    if result is None:
        raise ValueError("Workflow generation did not produce a result")
    return result
