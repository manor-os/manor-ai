"""How a Workflow's production shows while it runs: its stages and its units.

A Workflow that makes something (a video scene by scene, a song, a deck
slide by slide) declares ``production`` on its trigger step,
beside ``run_inputs``. It only describes what the person sees; what runs
stays in the steps and Skills.

    "production": {
      "media": "video",
      "unit": {"name": "Scene"},
      "units_from": "script_packet.blocks",
      "unit_fields": {"id": "block_id", "title": "prop", "duration": "duration_seconds"},
      "lanes": [{"key": "narration", "label": "Narration", "from": "narration"}],
      "stages": [
        {"label": "Script", "steps": ["write_script_storyboard"]},
        {"label": "Scenes", "steps": ["generate_stickman_video"], "counts_units": true, "weight": 3}
      ]
    }

``media`` (video, audio or image; video by default) is what the run makes:
its units' media jobs are of that kind and its finished whole plays as one.
Units are optional; a song made in one go shows its stages and the track.
``units_from`` is a dotted path into the run's variables (usually a planning
step's output) to a list of units; ``final_from`` is the path to the finished
whole (an object with its Knowledge ``document_id``). ``revise`` lets a
finished run be changed: ``{from_step, notes_input, versions_input}`` start
its next version from that step, with the person's notes and the unit
versions they picked passed in as those run inputs. Each unit's media (``MediaJob`` with the
run's ``run_id``) is matched to it by ``unit_id``, or else by order.
"""
from __future__ import annotations

import re
from typing import Any

_PATH = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}(\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,7}$")
_FIELD = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_UNITS = 200
PRODUCTION_MEDIA = ("video", "audio", "image")
MAX_TEXT = 400


def _text(value: Any, limit: int, what: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"production {what} is text of 1 to {limit} characters")
    return value.strip()


def normalize_workflow_production(value: Any) -> dict[str, Any]:
    """Check a trigger step's ``production`` and return it in canonical form."""
    if not isinstance(value, dict):
        raise ValueError("production must be an object")
    unknown = set(value) - {
        "media", "unit", "units_from", "unit_fields", "lanes", "stages", "final_from", "revise", "i18n",
    }
    if unknown:
        raise ValueError(f"unsupported production fields: {', '.join(sorted(unknown))}")
    unit = value.get("unit") or {}
    if not isinstance(unit, dict) or set(unit) - {"name", "i18n"}:
        raise ValueError("production.unit has only a name")
    media = value.get("media", "video")
    if media not in PRODUCTION_MEDIA:
        raise ValueError(f"production.media is one of: {', '.join(PRODUCTION_MEDIA)}")
    result: dict[str, Any] = {"media": media, "unit": {"name": _text(unit.get("name") or "Unit", 40, "unit name")}}
    units_from = value.get("units_from")
    if units_from is not None:
        if not isinstance(units_from, str) or not _PATH.fullmatch(units_from):
            raise ValueError("production.units_from is a dotted path into the run's variables")
        result["units_from"] = units_from
    final_from = value.get("final_from")
    if final_from is not None:
        if not isinstance(final_from, str) or not _PATH.fullmatch(final_from):
            raise ValueError("production.final_from is a dotted path into the run's variables")
        result["final_from"] = final_from
    revise = value.get("revise")
    if revise is not None:
        # One step, or several in order of preference: a run starts again from
        # the first it reached (a step added later was never reached by older runs).
        from_steps = revise.get("from_step") if isinstance(revise, dict) else None
        from_steps = [from_steps] if isinstance(from_steps, str) else from_steps
        if not isinstance(revise, dict) or set(revise) - {"from_step", "notes_input", "versions_input"} \
                or not isinstance(from_steps, list) or not 1 <= len(from_steps) <= 5 \
                or any(not isinstance(step, str) or not step.strip() for step in from_steps) \
                or any(not isinstance(revise.get(key), str) or not _FIELD.fullmatch(revise[key])
                       for key in ("notes_input", "versions_input") if key in revise) \
                or "notes_input" not in revise:
            raise ValueError("production.revise names the step (or steps, first reached wins) a revision "
                             "starts from and the run inputs that take the notes (notes_input) and the chosen "
                             "unit versions (versions_input)")
        result["revise"] = {"from_step": [step.strip() for step in from_steps],
                            **{key: revise[key] for key in ("notes_input", "versions_input") if key in revise}}
    fields = value.get("unit_fields") or {}
    if not isinstance(fields, dict) or set(fields) - {"id", "title", "duration"} or any(
        not isinstance(field, str) or not _FIELD.fullmatch(field) for field in fields.values()
    ):
        raise ValueError("production.unit_fields maps id, title and duration to field names")
    result["unit_fields"] = {"id": "id", "title": "title", "duration": "duration_s", **fields}
    lanes = value.get("lanes") or []
    if not isinstance(lanes, list) or len(lanes) > 4:
        raise ValueError("production.lanes is a list of at most 4 lanes")
    result["lanes"] = []
    for lane in lanes:
        if not isinstance(lane, dict) or set(lane) - {"key", "label", "from", "i18n"}:
            raise ValueError("a production lane has a key, a label and the unit field it reads")
        if not isinstance(lane.get("key"), str) or not _FIELD.fullmatch(lane["key"]) \
                or not isinstance(lane.get("from"), str) or not _FIELD.fullmatch(lane["from"]):
            raise ValueError("a production lane's key and from are field names")
        result["lanes"].append({"key": lane["key"], "label": _text(lane.get("label"), 40, "lane label"),
                                "from": lane["from"]})
    stages = value.get("stages") or []
    if not isinstance(stages, list) or not 1 <= len(stages) <= 8:
        raise ValueError("production.stages lists 1 to 8 stages")
    result["stages"] = []
    for stage in stages:
        if not isinstance(stage, dict) or set(stage) - {"label", "steps", "counts_units", "weight", "i18n"}:
            raise ValueError("a production stage has a label, its steps, counts_units and weight")
        steps = stage.get("steps")
        if not isinstance(steps, list) or not 1 <= len(steps) <= 50 or any(
            not isinstance(step, str) or not step.strip() for step in steps
        ):
            raise ValueError("a production stage lists the ids of 1 to 50 steps")
        weight = stage.get("weight", 1)
        if isinstance(weight, bool) or not isinstance(weight, (int, float)) or not 0 < weight <= 100:
            raise ValueError("a production stage weight is a number above 0 and at most 100")
        result["stages"].append({"label": _text(stage.get("label"), 40, "stage label"), "steps": list(steps),
                                 "counts_units": bool(stage.get("counts_units")), "weight": weight})
    return result


def workflow_production_config(steps: Any) -> dict[str, Any] | None:
    """The ``production`` a Workflow's trigger step declares, if any and valid."""
    for step in steps if isinstance(steps, list) else []:
        if not isinstance(step, dict) or str(step.get("type") or "").lower() not in {"trigger", "webhook"}:
            continue
        config = step.get("config")
        production = config.get("production") if isinstance(config, dict) else None
        if production is None:
            continue
        try:
            return normalize_workflow_production(production)
        except ValueError:
            return None
    return None


def _at_path(value: Any, path: str) -> Any:
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def production_units(config: dict[str, Any], variables: Any) -> list[dict[str, Any]]:
    """The run's units as far as its plan exists yet: id, title, duration and lane text."""
    items = _at_path(variables, config["units_from"]) if config.get("units_from") else None
    if not isinstance(items, list):
        return []
    fields = config["unit_fields"]
    units = []
    for index, item in enumerate(items[:MAX_UNITS]):
        if not isinstance(item, dict):
            continue
        duration = item.get(fields["duration"])
        title = item.get(fields["title"])
        units.append({
            "id": str(item.get(fields["id"]) or index + 1)[:128],
            "title": title.strip()[:MAX_TEXT] if isinstance(title, str) else "",
            "duration_s": duration if isinstance(duration, (int, float)) and not isinstance(duration, bool)
            and 0 < duration <= 3600 else None,
            "lanes": {lane["key"]: str(item.get(lane["from"]) or "")[:MAX_TEXT] for lane in config["lanes"]},
        })
    return units


def workflow_run_production(steps: Any, variables: Any, *, locale: str | None = None) -> dict[str, Any] | None:
    """What a run's production view shows: its Workflow's ``production`` and its units so far.

    ``steps`` are the Workflow's (the run snapshot keeps no step config); the
    view only describes the run, so the current definition is the one shown.
    """
    from packages.core.services.viewer_language import localized, without_i18n

    steps = localized(steps, locale) if locale else without_i18n(steps)
    config = workflow_production_config(steps)
    if config is None:
        return None
    view = {**config, "units": production_units(config, variables)}
    # The finished whole: the Knowledge file the run made, which the view leads with.
    final = _at_path(variables, config["final_from"]) if config.get("final_from") else None
    if isinstance(final, dict) and isinstance(final.get("document_id"), str) and final["document_id"].strip():
        view["final"] = {"document_id": final["document_id"].strip(),
                         "name": str(final.get("filename") or final.get("name") or "")[:255]}
    return view
