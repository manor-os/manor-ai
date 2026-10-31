"""Authored Workspace text in each viewer's language.

Any authored object (a Flow input, a step, an App page, an entry point) may
carry translations of its own display text next to it::

    {"label": "Topic", "i18n": {"zh": {"label": "主题"}}}

An overlay mirrors the object's shape and replaces only text the object
already has, with a value of the same type. It never adds or replaces keys,
types, ids or anything that decides what runs (see ``sanitize_i18n``), so a
translation changes what a viewer reads, never what a run does.

``localized`` resolves a value for one viewer on a copy: the viewer's own
overlay over the authored text, or the English one when their language has
none. Stored configuration is never rewritten. Callers that feed a model (intent routing, prefill, prompts) pass
no locale and read the authored base.
"""

from __future__ import annotations

from typing import Any

I18N_KEY = "i18n"
# The languages Workspaces are translated into (workspace_translation); a
# viewer of any other language reads English.
TRANSLATED_LANGUAGES = ("en", "zh")
# Machine-translated overlays carry this mark: the fingerprint of the text
# they translate, so a change to that text is translated again. A person's
# edit drops it, and an overlay without it is never machine-written over.
AUTO_KEY = "_auto"
_MAX_LOCALES = 8
# Text a person reads. A value is translatable only below one of these keys.
_TEXT_KEYS = frozenset({
    "name", "title", "label", "description", "placeholder", "default", "examples", "options",
    "eyebrow", "summary", "caption", "alt_text", "use_when", "outcome_summary", "message",
    "review_title", "labels", "descriptions", "entries", "prompts", "presets", "forms",
    "x-enum-labels", "x-enum-descriptions", "x-example-labels", "x-example-groups", "stage_labels", "empty_message",
})
# Keys that identify or configure something. A translation never touches them,
# even below a text key (a form field's ``key`` or ``type``).
_STRUCTURAL_KEYS = frozenset({
    "id", "key", "type", "enum", "const", "required", "hidden", "target", "targets", "hero", "asks",
    "next", "true_next", "false_next", "binding_id", "workflow_id", "workflow_slug", "tool", "skill",
    "source", "mode", "format", "pattern", "prefill", "path", "terminal_step_id", "version", "url",
})


def viewer_locale(locale: str | None) -> str | None:
    """``zh-CN`` → ``zh``; nothing for no locale."""
    text = str(locale or "").strip().lower().replace("_", "-")
    return text.split("-", 1)[0] or None


def _overlay(base: Any, overlay: Any) -> Any:
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = dict(base)
        for key, value in overlay.items():
            if key in merged and key != I18N_KEY:
                merged[key] = _overlay(merged[key], value)
        return merged
    if isinstance(base, list) and isinstance(overlay, list) and len(base) == len(overlay):
        return [_overlay(item, value) for item, value in zip(base, overlay)]
    return overlay if type(overlay) is type(base) else base


def _overlay_language(overlays: dict[str, Any], language: str) -> str | None:
    """Which overlay a viewer reads: their own; none; or English.

    Workspaces are translated into ``TRANSLATED_LANGUAGES``, and only text
    written in another language gets an overlay, so a viewer of one of those
    languages without their overlay reads the text as written: a Chinese
    viewer of a Chinese page reads the page, not its English translation.
    Viewers of any other language read English.
    """
    if language in overlays:
        return language
    return None if language in TRANSLATED_LANGUAGES else "en"


def localized(value: Any, locale: str | None) -> Any:
    """A copy of ``value`` in the viewer's language, without its overlays.

    With no locale the value comes back unchanged (overlays included), so
    model-facing callers keep reading what was authored.
    """
    language = viewer_locale(locale)
    if language is None:
        return value

    def resolve(item: Any) -> Any:
        if isinstance(item, list):
            return [resolve(child) for child in item]
        if not isinstance(item, dict):
            return item
        overlays = item.get(I18N_KEY)
        result = {key: resolve(child) for key, child in item.items() if key != I18N_KEY}
        if isinstance(overlays, dict):
            # Stored overlays are trusted no further than written ones.
            overlay = _sanitize_overlay(result, overlays.get(_overlay_language(overlays, language) or ""))
            if overlay:
                result = _overlay(result, overlay)
        return result

    return resolve(value)


def _sanitize_overlay(base: Any, overlay: Any, *, text: bool = False) -> Any:
    """Keep only same-typed replacements of text the base already has."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        result = {}
        for key, value in overlay.items():
            if key == AUTO_KEY and (value is True or (isinstance(value, str) and len(value) <= 64)):
                result[key] = value
            elif key in base and key not in _STRUCTURAL_KEYS and key != I18N_KEY:
                cleaned = _sanitize_overlay(base[key], value, text=text or key in _TEXT_KEYS)
                if cleaned is not None:
                    result[key] = cleaned
        return result or None
    if isinstance(base, list) and isinstance(overlay, list) and len(base) == len(overlay):
        cleaned = [_sanitize_overlay(item, value, text=text) for item, value in zip(base, overlay)]
        if any(isinstance(item, (dict, list)) for item in base):
            return [item if item is not None else {} for item in cleaned] if any(cleaned) else None
        return cleaned if all(item is not None for item in cleaned) else None
    if not text or isinstance(base, bool) or type(overlay) is not type(base):
        return None
    return overlay if isinstance(base, (str, int, float)) else None


def sanitize_i18n(value: Any) -> Any:
    """Drop every overlay key that is not a translation of existing text.

    Applied wherever authored configuration is written, at every depth, so
    stored overlays can only ever change display text.
    """
    if isinstance(value, list):
        return [sanitize_i18n(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: sanitize_i18n(child) for key, child in value.items() if key != I18N_KEY}
    overlays = value.get(I18N_KEY)
    if isinstance(overlays, dict):
        kept = {}
        for code, overlay in overlays.items():
            language = viewer_locale(code if isinstance(code, str) else None)
            cleaned = _sanitize_overlay(result, overlay) if language and len(language) <= 3 else None
            if cleaned and set(cleaned) != {AUTO_KEY}:
                kept[language] = cleaned
            if len(kept) >= _MAX_LOCALES:
                break
        if kept:
            result[I18N_KEY] = kept
    return result


def without_i18n(value: Any) -> Any:
    """The authored value with every overlay removed (identity and hashing)."""
    if isinstance(value, list):
        return [without_i18n(item) for item in value]
    if isinstance(value, dict):
        return {key: without_i18n(child) for key, child in value.items() if key != I18N_KEY}
    return value


# ── Run cards ──
# A run keeps the names of its Flow and steps as they were when it started
# (``_workflow_name`` / ``_workflow_node_names``). Their translations travel
# with them under this key, so lists and chat cards read a viewer's names
# without loading the Flow:
#   {"zh": {"name": "...", "node_names": {"step_id": "..."}, "texts": {"<as authored>": "..."}}}
# ``texts`` covers names stored without an id next to them (a chat card's
# Flow title, a review title), matched by the exact authored text.
RUN_DISPLAY_I18N_KEY = "_workflow_display_i18n"
_RUN_DATA_KEYS = frozenset({
    "variables", "step_results", "trigger_data", "execution_trace", "output", "outputs", "values", "result",
    "content", "input_schema", "editable_input_schema", "inputs", "prompt", "answer",
})
# Keys whose value is a Flow or step name a person reads.
_RUN_NAME_KEYS = frozenset({
    "name", "title", "review_title", "workflow_title", "workflow_name", "step_name", "current_step_name",
    "retry_from_step_name",
})
_MAX_RUN_TEXTS = 200


def _add_text(result: dict[str, dict[str, Any]], language: str, base: Any, translated: Any) -> None:
    if (isinstance(base, str) and isinstance(translated, str) and base.strip() and translated.strip()
            and base != translated and len(result.setdefault(language, {}).setdefault("texts", {})) < _MAX_RUN_TEXTS):
        result[language]["texts"][base] = translated.strip()[:255]


def run_display_translations(
    steps: Any, *, name: str | None = None, name_translations: Any = None, texts: Any = None,
) -> dict[str, Any]:
    """The per-language Flow and step names to store on a run at start.

    ``texts`` adds other names the run is shown by, ``{lang: {authored: translated}}``.
    """
    result: dict[str, dict[str, Any]] = {}
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict) or not str(step.get("id") or "").strip():
            continue
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        base = {"name": str(step.get("name") or step.get("id")),
                "config": {key: config[key] for key in ("review_title",) if isinstance(config.get(key), str)}}
        for code, overlay in (step.get(I18N_KEY) or {}).items() if isinstance(step.get(I18N_KEY), dict) else ():
            cleaned = _sanitize_overlay(base, overlay)
            language = viewer_locale(code if isinstance(code, str) else None)
            if not cleaned or not language:
                continue
            if cleaned.get("name", "").strip():
                result.setdefault(language, {}).setdefault("node_names", {})[str(step["id"])] = cleaned["name"].strip()
                _add_text(result, language, base["name"], cleaned["name"])
            _add_text(result, language, base["config"].get("review_title"), (cleaned.get("config") or {}).get("review_title"))
    if name and isinstance(name_translations, dict):
        for code, translated in name_translations.items():
            language = viewer_locale(code if isinstance(code, str) else None)
            if language and isinstance(translated, str) and translated.strip():
                result.setdefault(language, {})["name"] = translated.strip()[:255]
                _add_text(result, language, name, translated)
    for code, pairs in (texts or {}).items() if isinstance(texts, dict) else ():
        language = viewer_locale(code if isinstance(code, str) else None)
        for base_text, translated in (pairs or {}).items() if language and isinstance(pairs, dict) else ():
            _add_text(result, language, base_text, translated)
    return result


def run_names_for(translations: Any, locale: str | None) -> dict[str, Any]:
    """The viewer's Flow and step names for one run (their language, else English)."""
    language = viewer_locale(locale)
    names: dict[str, Any] = {"node_names": {}, "texts": {}}
    if language is None or not isinstance(translations, dict):
        return names
    for code in (_overlay_language(translations, language) or "",):
        entry = translations.get(code)
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("name"), str):
            names["name"] = entry["name"]
        for field in ("node_names", "texts"):
            if isinstance(entry.get(field), dict):
                names[field].update({
                    str(key): text for key, text in entry[field].items() if isinstance(text, str)
                })
    return names


def localize_run_names(value: Any, names: dict[str, Any]) -> Any:
    """Replace a run payload's Flow and step names with the viewer's.

    Names sit next to the ids they name (``id``/``name`` on steps and nodes,
    ``current_step_id``/``current_step_name``, ``retry_from_step_id``/
    ``retry_from_step_name``), so one walk covers cards, progress and
    interventions without each builder knowing about languages. A name kept
    without its id (a chat card's title) is matched by its authored text.
    """
    node_names = names.get("node_names") or {}
    texts = names.get("texts") or {}
    if not node_names and not texts and not names.get("name"):
        return value

    def walk(item: Any) -> Any:
        if isinstance(item, list):
            return [walk(child) for child in item]
        if not isinstance(item, dict):
            return item
        # What a run produced is its own text, never renamed.
        result = {key: child if key in _RUN_DATA_KEYS else walk(child) for key, child in item.items()}
        for key in _RUN_NAME_KEYS:
            if isinstance(result.get(key), str) and result[key] in texts:
                result[key] = texts[result[key]]
        step_id = result.get("id")
        if isinstance(step_id, str) and step_id in node_names and isinstance(result.get("name"), str):
            result["name"] = node_names[step_id]
        for id_key, name_key in (("current_step_id", "current_step_name"), ("retry_from_step_id", "retry_from_step_name"),
                                 ("step_id", "step_name")):
            if isinstance(result.get(id_key), str) and result[id_key] in node_names and isinstance(result.get(name_key), str):
                result[name_key] = node_names[result[id_key]]
        if names.get("name") and isinstance(result.get("workflow_name"), str):
            result["workflow_name"] = names["name"]
        return result

    return walk(value)


def binding_text_translations(field: str, text: Any, config: Any) -> dict[str, str]:
    """A Flow binding's text kept beside its config, in other languages: ``config.i18n.<lang>.<field>``.

    Its name is a binding column, and an entry without its own description
    shows its Flow's, so neither is part of the config and both are read here
    rather than through ``localized``.
    """
    overlays = config.get(I18N_KEY) if isinstance(config, dict) else None
    if not isinstance(text, str) or not text.strip() or not isinstance(overlays, dict):
        return {}
    result = {}
    for code, overlay in overlays.items():
        language = viewer_locale(code if isinstance(code, str) else None)
        translated = overlay.get(field) if isinstance(overlay, dict) else None
        if language and isinstance(translated, str) and translated.strip():
            result[language] = translated.strip()[:2000]
    return result


def binding_text_for(field: str, text: Any, config: Any, locale: str | None) -> Any:
    """That text for one viewer (their language, else English)."""
    language = viewer_locale(locale)
    if language is None:
        return text
    translations = binding_text_translations(field, text, config)
    return translations.get(_overlay_language(translations, language) or "") or text


def binding_name_translations(name: Any, config: Any) -> dict[str, str]:
    """A Flow binding's name in other languages: ``config.i18n.<lang>.name``."""
    return {code: value[:255] for code, value in binding_text_translations("name", name, config).items()}


def binding_name_for(name: Any, config: Any, locale: str | None) -> Any:
    """The binding's name for one viewer (their language, else English)."""
    return binding_text_for("name", name, config, locale)


def chat_entrypoint_title_translations(config: Any) -> dict[str, dict[str, str]]:
    """``{lang: {authored title: translated}}`` for a binding's chat entry point."""
    entry = config.get("chat_entrypoint") if isinstance(config, dict) else None
    if not isinstance(entry, dict) or not isinstance(entry.get("title"), str):
        return {}
    languages = {
        viewer_locale(code) for overlays in (config.get(I18N_KEY), entry.get(I18N_KEY)) if isinstance(overlays, dict)
        for code in overlays if isinstance(code, str)
    } - {None}
    result = {}
    for language in sorted(languages):
        title = (localized(config, language).get("chat_entrypoint") or {}).get("title")
        if isinstance(title, str) and title != entry["title"]:
            result[language] = {entry["title"]: title}
    return result
