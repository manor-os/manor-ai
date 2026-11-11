"""Machine translations of a Workspace's authored text, stored next to it.

People and agents write a Workspace in one language. Every viewer reads it in
their own (see ``viewer_language``), so after an edit this fills in the other
languages a viewer may use: the App page, the names and entry points of its
Flows, and each Flow's step names and input labels.

* A translation is an ``i18n`` overlay marked ``_auto`` with the fingerprint
  of the text it translates. When that text changes it is translated again;
  an overlay a person wrote (no ``_auto``) is never written over.
* Only display text is translated: names, titles, labels, descriptions and
  placeholders. Values a run receives (defaults, options, presets, prompts)
  stay as authored.
* Writes never bump a Flow's revision and never touch its graph, so a
  translation is not a local edit and never blocks a Blueprint upgrade or a
  paused run.
* It runs in the background after an edit, billed to the Workspace's account
  like any model call. When it cannot run, viewers read the authored text.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from packages.core.i18n import text_locale
from packages.core.services.viewer_language import AUTO_KEY, DATA_PATH_KEYS, I18N_KEY, TRANSLATED_LANGUAGES

logger = logging.getLogger(__name__)

TRANSLATION_LANGUAGES = TRANSLATED_LANGUAGES
_LANGUAGE_NAMES = {"en": "English", "zh": "Simplified Chinese"}
TRANSLATION_MODEL = os.getenv("WORKSPACE_TRANSLATION_MODEL", "google/gemini-2.5-flash-lite")
# Text a person reads. Values a run receives are never machine-translated.
_TEXT_KEYS = frozenset({
    "name", "title", "label", "description", "placeholder", "eyebrow", "review_title",
    "labels", "descriptions", "entries", "forms", "x-enum-labels", "x-example-labels", "x-example-groups",
    "x-enum-descriptions",
    "stage_labels", "empty_message", "collection_title",
})
_NEVER_KEYS = frozenset({
    I18N_KEY, "id", "key", "type", "enum", "const", "required", "hidden", "target", "targets", "hero", "asks",
    "default", "examples", "options", "prompts", "presets", "prompt", "message", "url", "source", "path",
}) | DATA_PATH_KEYS
_MAX_TEXT = 1200
_BATCH = 120
# Edits come in bursts (a draft applies many patches); wait for the last one.
DISPATCH_DELAY_SECONDS = 20
LOCK_SECONDS = 300


def _language(text: str) -> str:
    return text_locale(text) or "en"


def _translatable(text: Any) -> bool:
    return (isinstance(text, str) and 0 < len(text.strip()) <= _MAX_TEXT and "{{" not in text
            and any(char.isalpha() for char in text))


@dataclass
class Anchor:
    """One authored object whose overlays sit on it, and the parts of it to translate."""
    kind: str
    ref: str
    value: dict[str, Any]
    regions: tuple[tuple[str, ...], ...] | None = None
    # Text that belongs to the object but is stored beside it (a binding's name).
    beside: dict[str, str] = field(default_factory=dict)

    def leaves(self) -> list[tuple[tuple, str]]:
        found: list[tuple[tuple, str]] = []

        def walk(item: Any, path: tuple, text: bool) -> None:
            if isinstance(item, dict):
                for key, child in item.items():
                    if key not in _NEVER_KEYS:
                        walk(child, path + (key,), text or key in _TEXT_KEYS)
            elif isinstance(item, list):
                for index, child in enumerate(item):
                    walk(child, path + (index,), text)
            elif text and _translatable(item):
                found.append((path, item))

        for key, text in self.beside.items():
            if _translatable(text):
                found.append(((key,), text))
        for region in self.regions or ((),):
            node: Any = self.value
            for part in region:
                node = node.get(part) if isinstance(node, dict) else None
            if node is not None:
                walk(node, region, bool(region) and region[-1] in _TEXT_KEYS)
        return found

    def fingerprint(self) -> str:
        encoded = json.dumps(sorted([list(map(str, path)), text] for path, text in self.leaves()), ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]

    def wanted(self) -> dict[str, list[tuple[tuple, str]]]:
        """Per language, the text still to translate: none where a person wrote the overlay."""
        overlays = self.value.get(I18N_KEY) if isinstance(self.value.get(I18N_KEY), dict) else {}
        fingerprint = self.fingerprint()
        result = {}
        for language in TRANSLATION_LANGUAGES:
            existing = overlays.get(language)
            if isinstance(existing, dict) and (AUTO_KEY not in existing or existing[AUTO_KEY] == fingerprint):
                continue
            texts = [(path, text) for path, text in self.leaves() if _language(text) != language]
            if texts:
                result[language] = texts
        return result

    def with_translations(self, language: str, translated: dict[tuple, str]) -> dict[str, Any]:
        """A copy of the object whose ``language`` overlay holds these translations."""
        overlay: dict[str, Any] = {AUTO_KEY: self.fingerprint()}
        base = {**self.beside, **self.value}
        for path, text in translated.items():
            _place(overlay, base, path, text)
        value = dict(self.value)
        value[I18N_KEY] = {**(value.get(I18N_KEY) or {}), language: overlay}
        return value


def _place(overlay: Any, base: Any, path: tuple, text: str) -> None:
    """Set ``text`` at ``path`` in an overlay shaped like ``base``."""
    for index, part in enumerate(path):
        last = index == len(path) - 1
        if isinstance(base, list):
            if not overlay:
                overlay.extend([{} if isinstance(item, dict) else item for item in base])
            if last:
                overlay[part] = text
                return
            if not isinstance(overlay[part], (dict, list)) or overlay[part] == {} and isinstance(base[part], list):
                overlay[part] = [] if isinstance(base[part], list) else {}
        else:
            if last:
                overlay[part] = text
                return
            overlay.setdefault(part, [] if isinstance(base[part], list) else {})
        overlay, base = overlay[part], base[part]


def workspace_anchors(workspace: Any, bindings: list[Any], workflows: list[Any]) -> list[Anchor]:
    """Everything a Workspace's viewers read that its authors wrote."""
    anchors = []
    model = workspace.operating_model if isinstance(workspace.operating_model, dict) else {}
    if isinstance(model.get("app_view"), dict):
        anchors.append(Anchor("app_view", workspace.id, model["app_view"],
                              regions=(("eyebrow",), ("title",), ("description",), ("labels",), ("descriptions",),
                                       ("entries",), ("forms",), ("components",))))
    descriptions = {workflow.id: workflow.description for workflow in workflows}
    for binding in bindings:
        config = binding.config if isinstance(binding.config, dict) else {}
        beside = {"name": binding.name} if isinstance(binding.name, str) else {}
        entry = config.get("chat_entrypoint") if isinstance(config.get("chat_entrypoint"), dict) else {}
        # An entry without its own description shows its Flow's (normalize_chat_entrypoint).
        if not entry.get("description") and isinstance(descriptions.get(binding.workflow_id), str):
            beside["description"] = descriptions[binding.workflow_id]
        anchors.append(Anchor("binding", binding.id, config, regions=(("chat_entrypoint", "title"),
                              ("chat_entrypoint", "description"), ("chat_entrypoint", "placeholder")),
                              beside=beside))
    for workflow in workflows:
        for index, step in enumerate(workflow.steps if isinstance(workflow.steps, list) else []):
            if isinstance(step, dict) and step.get("id"):
                anchors.append(Anchor("step", f"{workflow.id}:{index}", step, regions=(
                    ("name",), ("config", "run_inputs"), ("config", "inputs"), ("config", "input_schema"),
                    ("config", "review_title"),
                )))
    return anchors


def _parse(text: str) -> dict[str, str]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    data = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    return {str(key): value for key, value in data.items() if isinstance(value, str) and value.strip()}


async def _translate(texts: list[str], language: str, *, entity_id: str, workspace_id: str) -> dict[str, str]:
    """``{authored: translated}`` for UI text, one model call per batch."""
    from packages.core.ai.runtime import RUNTIME_WORKSPACE_TRANSLATION_SOURCE
    from packages.core.ai.runtime.completions import runtime_execute_text_completion

    result: dict[str, str] = {}
    for start in range(0, len(texts), _BATCH):
        batch = texts[start:start + _BATCH]
        source = {str(index): text for index, text in enumerate(batch)}
        completion = await runtime_execute_text_completion(
            [
                {"role": "system", "content": (
                    f"Translate the user interface text of a business Workspace into {_LANGUAGE_NAMES[language]}. "
                    "Each value is a label, title, name or short description a person reads in an app. "
                    "Keep it as short and natural as an app would say it, keep product and brand names, "
                    "numbers, URLs and anything in quotes or brackets as they are. Return only a JSON object "
                    "with the same keys and the translated values."
                )},
                {"role": "user", "content": json.dumps(source, ensure_ascii=False)},
            ],
            entity_id=entity_id,
            workspace_id=workspace_id,
            source=RUNTIME_WORKSPACE_TRANSLATION_SOURCE,
            model=TRANSLATION_MODEL,
            temperature=0.1,
            response_format={"type": "json_object"},
            max_tokens=8000,
        )
        if not completion.content:
            raise RuntimeError("workspace translation returned nothing")
        translated = _parse(completion.content)
        result.update({source[key]: value.strip() for key, value in translated.items() if key in source})
    return result


async def _load(db: Any, workspace_id: str, *, lock: bool = False) -> tuple[Any, list[Any], list[Any]] | None:
    from sqlalchemy import or_, select

    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.models.workspace import Workspace

    def locked(statement):
        return statement.with_for_update() if lock else statement

    workspace = (await db.execute(locked(select(Workspace).where(Workspace.id == workspace_id)))).scalar_one_or_none()
    if workspace is None or getattr(workspace, "deleted_at", None) is not None:
        return None
    bindings = list((await db.execute(locked(select(WorkflowBinding).where(
        WorkflowBinding.entity_id == workspace.entity_id, WorkflowBinding.workspace_id == workspace_id,
    ).order_by(WorkflowBinding.id)))).scalars())
    workflow_ids = {binding.workflow_id for binding in bindings}
    workflows = list((await db.execute(locked(select(WorkflowDefinition).where(
        WorkflowDefinition.entity_id == workspace.entity_id,
        or_(WorkflowDefinition.workspace_id == workspace_id, WorkflowDefinition.id.in_(workflow_ids)),
    ).order_by(WorkflowDefinition.id)))).scalars())
    return workspace, bindings, workflows


async def translate_workspace_text(workspace_id: str) -> dict[str, Any]:
    """Translate what changed since the last pass; returns counts for logs."""
    from sqlalchemy.orm.attributes import flag_modified

    from packages.core.ai.llm_client import CreditCheckUnavailableError, CreditExhaustedError
    from packages.core.ai.runtime import RUNTIME_WORKSPACE_TRANSLATION_SOURCE, runtime_assert_credit_available
    from packages.core.database import create_worker_session

    async with create_worker_session()() as db:
        loaded = await _load(db, workspace_id)
        if loaded is None:
            return {"status": "missing"}
        workspace, bindings, workflows = loaded
        entity_id = workspace.entity_id
        plans = {(anchor.kind, anchor.ref): (anchor, anchor.wanted())
                 for anchor in workspace_anchors(workspace, bindings, workflows)}
    plans = {key: plan for key, plan in plans.items() if plan[1]}
    if not plans:
        return {"status": "current", "anchors": 0}

    texts = {language: sorted({text for _, wanted in plans.values() for path, text in wanted.get(language, [])})
             for language in TRANSLATION_LANGUAGES}
    try:
        await runtime_assert_credit_available(
            entity_id, source=RUNTIME_WORKSPACE_TRANSLATION_SOURCE, workspace_id=workspace_id,
        )
    except (CreditExhaustedError, CreditCheckUnavailableError):
        return {"status": "waiting_credit"}
    translations = {
        language: await _translate(items, language, entity_id=entity_id, workspace_id=workspace_id)
        for language, items in texts.items() if items
    }

    written = 0
    async with create_worker_session()() as db:
        loaded = await _load(db, workspace_id, lock=True)
        if loaded is None:
            return {"status": "missing"}
        workspace, bindings, workflows = loaded
        by_binding = {binding.id: binding for binding in bindings}
        by_workflow = {workflow.id: workflow for workflow in workflows}
        steps_by_workflow: dict[str, list] = {}
        app_view = None
        for anchor in workspace_anchors(workspace, bindings, workflows):
            planned = plans.get((anchor.kind, anchor.ref))
            # Text edited since it was read is translated on the next pass.
            if planned is None or anchor.fingerprint() != planned[0].fingerprint():
                continue
            value = anchor.value
            for language, wanted in planned[1].items():
                done = {path: translations.get(language, {}).get(text) for path, text in wanted}
                if all(done.values()):
                    value = Anchor(anchor.kind, anchor.ref, value, anchor.regions, anchor.beside).with_translations(
                        language, done)
            if value is anchor.value:
                continue
            written += 1
            if anchor.kind == "app_view":
                app_view = value
            elif anchor.kind == "binding":
                by_binding[anchor.ref].config = value
                flag_modified(by_binding[anchor.ref], "config")
            else:
                workflow_id, index = anchor.ref.rsplit(":", 1)
                steps = steps_by_workflow.setdefault(workflow_id, list(by_workflow[workflow_id].steps))
                steps[int(index)] = value
        for workflow_id, steps in steps_by_workflow.items():
            # Display text only: the graph and its revision stay as they are.
            by_workflow[workflow_id].steps = steps
            flag_modified(by_workflow[workflow_id], "steps")
        if app_view is not None:
            from packages.core.services.workspace_app_view import normalize_app_view_config

            model = dict(workspace.operating_model or {})
            model["app_view"] = normalize_app_view_config(app_view)
            workspace.operating_model = model
            flag_modified(workspace, "operating_model")
        await db.commit()
    return {"status": "translated", "anchors": written}


def schedule_workspace_translation(workspace_id: str | None) -> None:
    """Best-effort dispatch after an edit; viewers read the authored text meanwhile.

    A burst of edits dispatches several passes; each translates only what is
    still missing, so the later ones find nothing to do.
    """
    if not workspace_id:
        return
    try:
        from packages.core.tasks.workspace_translation_tasks import translate_workspace_text_task

        translate_workspace_text_task.apply_async((workspace_id,), countdown=DISPATCH_DELAY_SECONDS)
    except Exception:
        logger.debug("workspace translation dispatch failed for %s", workspace_id, exc_info=True)


async def translate_workspace_text_once(workspace_id: str) -> dict[str, Any] | None:
    """One pass at a time per Workspace; ``None`` when another pass holds it."""
    from packages.core.cache import _get_redis

    key = f"workspace_translation:{workspace_id}"
    try:
        redis = await _get_redis()
    except Exception:
        redis = None
    if redis is not None and not await redis.set(key, "1", nx=True, ex=LOCK_SECONDS):
        return None
    try:
        return await translate_workspace_text(workspace_id)
    finally:
        if redis is not None:
            await redis.delete(key)
