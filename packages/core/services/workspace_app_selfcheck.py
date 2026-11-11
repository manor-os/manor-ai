"""A Workspace App page's self-check: a test plan from its config, and the reads that prove it.

The plan is generated from the page and what the Workspace runs, never written
by whoever designed the page, so it cannot test only what came out right. It
lists everything the page offers and what each must satisfy.

Two levels run today. L0 reports the designer's own ``check()`` and
``coverage()`` through the plan. L1 reads every view with the code the App read
routes use, as the real person, on the page under test (``AppViewPreview``),
inside a read-only savepoint that is always rolled back. What the first records
of each view read back is checked as the page shows it: a card a person would
see with an unfilled template, an id, an internal key or raw data fails.

L1 proves that reads work, not that clicking works: nothing is clicked, started
or approved here.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.exc import InterfaceError, OperationalError, SQLAlchemyError

from packages.core.database_runtime import DatabaseFailureKind
from packages.core.services.workspace_app_view import block_card_actions

logger = logging.getLogger(__name__)

READS_ONLY = ("Every view was read back on the real routes as this person, and what the first records of each "
              "show was checked. Nothing was clicked, started or approved, so this proves reads work, not that "
              "clicking works.")
# Checked between reads, so a read is never cancelled halfway; what is left is reported as not read.
_READ_BUDGET_SECONDS = 30.0
_FAILED, _NOT_RUN, _NO_DATA, _PASSED = "failed", "not_run", "no_data", "passed"
_WORST_FIRST = (_FAILED, _NOT_RUN, _NO_DATA, _PASSED)
# What to change for each way the App read routes refuse a view.
_FIXES = {
    "invalid_context": "read a source that depends on the data view's context source, or move it to a data view over that source",
    "invalid_component": "make the data view's context name a declared source, and its sources what this Workspace still has",
    "component_not_found": "declare the data view in components",
    "context_not_found": "make the data view's context list projects this person can read",
    "block_not_found": "declare the block in its data view",
    "source_not_found": "declare the source in data_sources",
    "invalid_source": "give the related source a project source as its parent",
    "unsupported_source": "use a source type from the module catalog",
    "source_too_large": "limit the source to the stages it shows, so it stays within the App read limit",
}
# Records differ where a view does not, so values are checked on this many of each kind of context.
_VALUE_CONTEXTS = 5
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_OPAQUE_ID = re.compile(r"(?=[0-9A-HJKMNP-TV-Z]*\d)(?=[0-9A-HJKMNP-TV-Z]*[A-Z])[0-9A-HJKMNP-TV-Z]{26}"
                        r"|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# Underscores only: hyphenated words and dates are ordinary text.
_INTERNAL_KEY = re.compile(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)+")
# What to change for each kind of value a person should never see.
_VALUE_FIXES = {
    "template": "the step that writes it left a template unfilled: make that Workflow step write the value, "
                "or stop showing the field",
    "id": "show a name instead: point the field at one, give the source label_fields, use the document "
          "format for a Document id, or the record format for the ids of Workspace records (projects) a "
          "Workflow picked",
    "key": "give the field labels for its values, or the status format",
    "data": "point the field at one value inside it",
    "untitled": "give the block the fields that say what each card is, or its source label_fields",
    "blank_post": "point preview.fields at the paths its Workflow saves the caption, title and pictures or video in",
}
_VALUE_WHAT = {"template": "an unfilled template", "id": "an id", "key": "an internal key",
               "data": "raw structured data", "untitled": "a card with no name",
               "blank_post": "a post with no words or pictures"}


# ── Plan ────────────────────────────────────────────────────────────────────

def _card_actions(config: dict[str, Any]) -> list[tuple[str, str, str]]:
    """(where, option, entry key) of each card action on the page."""
    return [(f"{component['key']}.{block['key']}", option, action["action"])
            for component in config["components"] for block in component["blocks"]
            for option, action in block_card_actions(block)]


def _named_entries(config: dict[str, Any]) -> list[str]:
    """The entries a config names. Remapping keeps this order, so two key spaces line up."""
    return [*([config["hero"]] if config["hero"] else []), *config["order"], *config["entries"], *config["targets"],
            *(key for _, _, key in _card_actions(config))]


def _sources_named_by(value: Any, sources: dict[str, Any]) -> set[str]:
    """Every source a block's options read: its own, its metrics, its plan, its display's."""
    if isinstance(value, list):
        return {name for item in value for name in _sources_named_by(item, sources)}
    if not isinstance(value, dict):
        return set()
    names = {item for key, item in value.items()
             if key in {"source", "metrics_source", "plan"} and isinstance(item, str) and item in sources}
    return names | _sources_named_by(list(value.values()), sources)


def _with_dependencies(names: set[str], sources: dict[str, Any]) -> set[str]:
    seen: set[str] = set()
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in seen or name not in sources:
            continue
        seen.add(name)
        pending += [sources[name][key] for key in ("parent", "subject") if sources[name].get(key)]
    return seen


def _entry_rules(key: str, config: dict[str, Any], context: Any) -> list[str]:
    must = ["its target exists and is available to this person"]
    target = config["targets"].get(key) or key
    slug = target.removeprefix("workflow:") if target.startswith("workflow:") else None
    filled = [*(config["asks"].get(key) or []), *(config["presets"].get(key) or {})]
    if slug and filled:
        must.append(f"the inputs it fills ({', '.join(filled)}) exist on {slug}")
    waits = [name for name, project in (context.projects() if context else {}).items() if slug in project["waits_in"]]
    if waits:
        must.append(f"each {' and '.join(waits)} waiting on a person shows on the front page")
    return must


def app_test_plan(config: dict[str, Any], context: Any = None) -> list[dict[str, Any]]:
    """Everything ``config`` offers and what each must satisfy, as plan rows.

    ``config`` is a normalized page; ``context`` is its ``AppDesignContext``,
    which adds what the Workspace runs: entries its page does not name, and
    what its projects keep.
    """
    from packages.core.services.workspace_app_view import app_view_sections

    def row(item_id: str, kind: str, key: str, must: list[str], **extra: Any) -> dict[str, Any]:
        return {"id": item_id, "kind": kind, "key": key, "must": must, **extra}

    sources = config["data_sources"]
    projects = context.projects() if context else {}
    project_of = {name: projects.get(source.get("project_type")) for name, source in sources.items()}
    actions = _card_actions(config)
    opened = {key: [f"{where}.{option}" for where, option, action in actions if action == key] for _, _, key in actions}
    plan = [row("page", "page", "page", ["opens for this person", "its data views hold against what this Workspace has"])]
    named = list(dict.fromkeys(_named_entries(config)))
    for key in named:
        if key in config["hidden"] and key not in opened:
            continue
        plan.append(row(f"entry:{key}", "entry", key, [
            *_entry_rules(key, config, context),
            *(f"{where} opens it" for where in opened.get(key, [])),
        ], named=True))
    unnamed = [*(f"workflow:{slug}" for slug in (context.runnable_workflows() if context else {})),
               *(f"service:{service['key']}" for service in (context.services if context else [])),
               *(f"automation:{job['id']}" for job in (context.automations if context else []))]
    plan += [row(f"entry:{key}", "entry", key, ["shows unless the page hides it"], named=False)
             for key in unnamed if key not in named and key not in config["hidden"]]
    for section in app_view_sections(config):
        must = {"queue": ["lists what waits on people"], "actions": ["shows the entries above"],
                "metrics": [f"reads {', '.join(section['items'])}" if section.get("items") else "reads the tracked numbers"],
                "outputs": [f"reads what {section['source'] or 'this Workspace'} produced"],
                "connections": ["lists what is still to connect"]}.get(section["block"], ["reads back"])
        plan.append(row(f"section:{section['id']}", "section", section["id"], must))
    readers: dict[str, list[str]] = {}
    for component in config["components"]:
        key, context_spec = component["key"], component["context"]
        children = [f"context:{key}", *(f"block:{key}.{block['key']}" for block in component["blocks"])]
        plan.append(row(f"view:{key}", "data_view", key, ["its context and every block read back"], children=children))
        source_name = context_spec.get("source")
        labels = (sources.get(source_name) or {}).get("label_fields") if source_name else None
        plan.append(row(f"context:{key}", "context", key, [
            f"lists its {context_spec['kind']} contexts for this person",
            *([f"each reads by its own {', '.join('.'.join(path) for path in labels)}"] if labels else []),
        ]))
        if source_name:
            for name in _with_dependencies({source_name}, sources):
                readers.setdefault(name, []).append(f"context:{key}")
        for block in component["blocks"]:
            where = f"{key}.{block['key']}"
            project = project_of.get(block["source"])
            must = ["reads back on the real route for this person"]
            if project is not None and project["document_fields"]:
                must.append("its documents open as documents, never as ids")
            if project is not None and project["waits_in"] and block["type"] == "progress":
                must.append(f"lists the {project['project_type']} waiting on a person")
            must += [f"{option} opens {action['action']}" for option, action in block_card_actions(block)]
            plan.append(row(f"block:{where}", "block", where, must))
            for name in _with_dependencies(_sources_named_by(block, sources), sources):
                readers.setdefault(name, []).append(f"block:{where}")
    plan += [row(f"source:{name}", "source", name, ["read by " + ", ".join(readers[name])] if name in readers
                 else ["nothing on the page reads it"], children=readers.get(name, []))
             for name in sources]
    return plan


# ── Reads (L1) ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def reads_only(db: Any) -> AsyncIterator[None]:
    """Postgres refuses every write inside this, and rolling the savepoint back ends the mode."""
    savepoint = await db.begin_nested()
    try:
        await db.execute(text("SET TRANSACTION READ ONLY"))
        yield
    finally:
        if savepoint.is_active:
            await savepoint.rollback()


def _technical(exc: Exception) -> bool:
    """A fault of the platform at this moment, which says nothing about the page."""
    driver = getattr(exc, "orig", exc)
    code = getattr(driver, "sqlstate", None) or getattr(driver, "pgcode", None)
    return (DatabaseFailureKind.from_exception(exc) is not None
            # Query cancellation and lock timeouts do not invalidate the page.
            or code in {"57014", "55P03"}
            or isinstance(exc, (OperationalError, InterfaceError, TimeoutError, ConnectionError, OSError))
            or "read-only transaction" in str(exc))


def _read_fault(where: str, exc: Exception) -> dict[str, Any]:
    from packages.core.services.workspace_app_data import AppDataError
    from packages.core.services.workspace_app_sections import AppSectionNotFound

    if _technical(exc):
        # Operators see it; neither the designer nor the person is told to fix a platform fault.
        logger.warning("App self-check could not read %s: %s", where, exc)
        return {"status": _NOT_RUN, "message": f"{where} could not be read right now"}
    if isinstance(exc, AppDataError):
        return _data_failure(where, exc.code, exc.message)
    if isinstance(exc, AppSectionNotFound):
        return {"status": _FAILED, "message": f"{where} could not be read: the page does not declare this section: "
                                              "declare it in sections"}
    logger.warning("App self-check read of %s crashed", where, exc_info=True)
    detail = "" if isinstance(exc, SQLAlchemyError) else f": {str(exc)[:200]}"
    return {"status": _FAILED, "message": f"{where} could not be read: the read failed with "
                                          f"{type(exc).__name__}{detail}: change or remove it"}


def _data_failure(where: str, code: Any, reason: Any) -> dict[str, Any]:
    fix = _FIXES.get(str(code), "change or remove it")
    return {"status": _FAILED, "message": f"{where} could not be read ({code}): {reason}: {fix}"}


# ── Values ──────────────────────────────────────────────────────────────────

def _unfit(value: Any, *, raw: bool) -> str | None:
    """Why ``value`` must not reach a person, if it must not. ``raw``: the page shows it as it is."""
    if isinstance(value, dict):
        return "data"
    if isinstance(value, list):
        # The page joins a list; one of its parts can still be a template or an id.
        return "data" if any(isinstance(item, (dict, list)) for item in value) else next(
            (kind for item in value if (kind := _unfit(item, raw=raw)) in {"template", "id"}), None)
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _TEMPLATE.search(text):
        return "template"
    if _OPAQUE_ID.fullmatch(text):
        return "id"
    return "key" if raw and _INTERNAL_KEY.fullmatch(text) else None


def _shown_fields(fields: Any, *, raw: bool) -> list[tuple[str, Any, bool]]:
    shown = []
    for field in fields if isinstance(fields, list) else []:
        # A document, a list of links, a record, a person or a file shows only their own names.
        if not isinstance(field, dict) or field.get("format") in {"document", "link_list", "record", "member",
                                                                    "image", "images", "video", "audio", "clips",
                                                                    "timeline", "sparkline"}:
            continue
        label = f"its {field.get('label') or 'field'} field"
        if field.get("format") == "tags":
            # Each tag shows as it is stored, unless the field's labels name it.
            shown.append((label, field.get("tags") or [], True))
            continue
        # Only plain text is shown as it is; a status, provider, link or date reads through a label or a format.
        shown.append((label, field.get("value"), raw and field.get("format") in {None, "text"}))
        if field.get("secondary"):
            shown.append((label, field["secondary"], False))
    return shown


def _shown_values(block_type: str, item: dict[str, Any]) -> list[tuple[str, Any, bool]] | None:
    """(where on the card, value, shown as it is) for what a card of ``block_type``, or a context, shows as text.

    Mirrors the App renderer: a name, detail or record field is humanized
    there, so only a summary's and a progress card's own fields read as they
    are. None means the card has no name, which the page shows as "Untitled".
    """
    if block_type == "context":
        return [("its name", item.get("label"), False)]
    preview = item.get("preview")
    if isinstance(preview, dict) and block_type in {"progress", "records", "gallery"}:
        # A card shown as its post reads by the post's own words, tags and replies.
        return [*((f"its post {part}", preview.get(part), False) for part in ("title", "caption", "account", "summary")),
                ("its post hashtags", preview.get("hashtags") or [], False),
                *(("its post thread", post, False) for post in preview.get("thread") or [])]
    if block_type == "chart":
        # A bar, slice or step reads by its category; one named by a stored value (a stage key) shows as it is.
        return [("its category", item.get("title"), not item.get("labelled", True))]
    if block_type == "summary":
        return _shown_fields(item.get("fields"), raw=True)
    if block_type == "progress":
        if not (item.get("label") or item.get("title")):
            return None
        return [("its name", item.get("label") or item.get("title"), False), ("its detail", item.get("detail"), False),
                *_shown_fields(item.get("fields"), raw=True)]
    if block_type == "documents":
        title = item.get("title")
        # A file named by an id reads as the block's title and its date there.
        return [*([] if isinstance(title, str) and _OPAQUE_ID.search(title) else [("its name", title, False)]),
                *_shown_fields(item.get("fields"), raw=False)]
    if block_type in {"records", "gallery"}:
        # An entry of a list with fields of its own reads by them and needs no name.
        if not item.get("title") and not item.get("label") and (block_type == "gallery" or not item.get("fields")):
            return None
        return [("its name", item.get("title") or item.get("label"), False), ("its subtitle", item.get("subtitle"), False),
                *_shown_fields(item.get("fields"), raw=False)]
    return []


def _value_finding(where: str, block_type: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A failed row when cards of ``where`` would show a person what they should never see."""
    found: list[tuple[str, str, Any]] = []
    for item in items:
        preview = item.get("preview")
        if isinstance(preview, dict) and not item.get("media") and not any(
                preview.get(part) for part in ("title", "caption", "images", "video", "cover")):
            found.append(("blank_post", "its post", None))
            continue
        shown = _shown_values(block_type, item)
        if shown is None:
            found.append(("untitled", "its name", None))
            continue
        found += [(kind, part, value) for part, value, raw in shown if (kind := _unfit(value, raw=raw))][:1]
    if not found:
        return None
    kind, part, value = found[0]
    what = _VALUE_WHAT[kind] if kind in {"untitled", "blank_post"} else f"{_VALUE_WHAT[kind]} in {part}"
    example = "" if value is None else f" ({json.dumps(value, ensure_ascii=False, default=str)[:80]})"
    share = f" on {len(found)} of {len(items)} cards" if len(items) > 1 else ""
    fix = ("name its values with chart.labels, or its stages with the block's stage_labels"
           if block_type == "chart" and kind == "key" else _VALUE_FIXES[kind])
    return {"status": _FAILED, "message": f"{where} would show people {what}{share}{example}: {fix}"}


async def _read_page(db: Any, shown: Any, *, user: Any, plan: list[dict[str, Any]], portable: dict[str, Any],
                     runtime: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read every view of ``shown`` as ``user``; returns each plan row's result by its id.

    ``portable`` names the page as the plan does and ``runtime`` as the reads do.
    """
    from packages.core.i18n import get_locale
    from packages.core.services.workspace_app_data import component_contexts, component_data, load_workspace_app_data_catalog
    from packages.core.services.workspace_app_sections import load_workspace_app_section
    from packages.core.services.workspace_app_view import (
        load_workspace_app_view,
        normalize_app_view_config,
        validate_app_view_data_contract,
    )

    locale = get_locale()
    deadline = time.monotonic() + _READ_BUDGET_SECONDS
    reads: dict[str, dict[str, Any]] = {}

    async def attempt(item_id: str, where: str, read: Callable[[], Awaitable[Any]]) -> Any:
        if time.monotonic() > deadline:
            reads[item_id] = {"status": _NOT_RUN, "message": f"{where} was not read: the check ran out of time"}
            return None
        try:
            async with db.begin_nested():
                return await read()
        except Exception as exc:  # noqa: BLE001 - a read that fails is the finding
            reads[item_id] = _read_fault(where, exc)
            return None

    view = await attempt("page", "the page", lambda: load_workspace_app_view(
        db, shown, user=user, include_catalog=False, locale=locale))
    if view is None:
        return reads
    reads["page"] = {"status": _PASSED}

    runtime_key = dict(zip(_named_entries(portable), _named_entries(runtime)))
    offered = {action["key"]: action for action in view["available_actions"]}
    for row in plan:
        if row["kind"] != "entry":
            continue
        reads[row["id"]] = {"status": _PASSED}
        if not row["named"]:
            continue
        action = offered.get(runtime_key.get(row["key"], row["key"]))
        if action is None or action.get("available") is False:
            reads[row["id"]] = {"status": _FAILED, "message": (
                f"{row['key']} is on the page but opens nothing for this person: "
                + ("this Workspace no longer offers it" if action is None else "its target is not available")
                + ": point it at a Workflow, Agent or Automation this Workspace has, or remove it")}

    for section in view["sections"]:
        item_id, where = f"section:{section['id']}", f"section {section['id']}"
        if section["block"] == "actions":
            reads[item_id] = {"status": _PASSED}
        elif not section["available"]:
            reads[item_id] = {"status": _FAILED, "message": (
                f"{where} reads a Flow this Workspace no longer has: point its source at one it has, or remove it")}
        else:
            result = await attempt(item_id, where, lambda section_id=section["id"]: load_workspace_app_section(
                db, shown, user=user, section_id=section_id, locale=locale))
            if result is not None:
                reads[item_id] = {"status": _PASSED, "rows": len(result.get("items") or [])}

    stored = normalize_app_view_config((shown.operating_model or {}).get("app_view"))
    catalog = await attempt("page", "the page", lambda: load_workspace_app_data_catalog(db, shown))
    if catalog is None:
        return reads
    try:
        validate_app_view_data_contract(stored["data_sources"], stored["components"], catalog=catalog)
    except ValueError as exc:
        # The data routes would refuse every view for this one reason; it is reported once.
        reads["page"] = {"status": _FAILED, "message": f"the page's data views do not hold on this Workspace: {exc}"}
        return reads

    for component in runtime["components"]:
        key = component["key"]
        blocks = [f"{key}.{block['key']}" for block in component["blocks"]]
        contexts = await attempt(f"context:{key}", f"{key} contexts", lambda component_key=key: component_contexts(
            db, shown, user=user, component_key=component_key, locale=locale))
        if contexts is None:
            continue
        # The page opens every context the same way, but records differ: the first few of each kind are read.
        picked: dict[str, list[str]] = {}
        for item in contexts.get("items") or []:
            ids = picked.setdefault(item["kind"], [])
            if len(ids) < _VALUE_CONTEXTS:
                ids.append(item["id"])
        if not picked:
            kind = component["context"]["kind"]
            nothing = {"status": _NO_DATA, "message": f"{key} has no {kind} to read yet"}
            reads[f"context:{key}"] = nothing
            for where in blocks:
                reads[f"block:{where}"] = nothing
            continue
        # A view or block the page hides shows nothing, so only its reads are checked; a calendar shows the
        # blocks it is made of in place of them.
        display = component.get("display") or {}
        on_page = not display.get("hidden")
        calendar = display.get("calendar") or {}
        off_page = set(display.get("hidden_blocks") or []) - {calendar.get(part) for part in (
            "block", "rollup_block", "context_block")}
        reads[f"context:{key}"] = (on_page and _value_finding(f"{key} contexts", "context", contexts["items"])
                                   or {"status": _PASSED, "rows": len(contexts["items"])})
        for block, where in zip(component["blocks"], blocks):
            item_id = f"block:{where}"
            outcome: dict[str, Any] | None = None
            items: list[dict[str, Any]] = []
            hidden = 0
            for kind, context_id in ((kind, context_id) for kind, ids in picked.items() for context_id in ids):
                result = await attempt(item_id, where, lambda component_key=key, block_key=block["key"], kind=kind,
                                       context_id=context_id: component_data(
                    db, shown, user=user, component_key=component_key, context_kind=kind, context_id=context_id,
                    block_key=block_key, locale=locale))
                if result is None:
                    outcome = reads[item_id]
                    break
                if result.get("visible") is False:
                    hidden += 1
                    continue
                read = next(iter(result.get("blocks") or []), {})
                if read.get("status") == "error":
                    error = read.get("error") or {}
                    outcome = _data_failure(where, error.get("code"), error.get("message"))
                    break
                items += read.get("items") or []
            if outcome is None and hidden and not items:
                outcome = {"status": _NO_DATA, "message": f"{where} stays hidden until it has data"}
            if outcome is None and on_page and block["key"] not in off_page:
                outcome = _value_finding(where, block["type"], items)
            reads[item_id] = outcome or {"status": _PASSED, "rows": len(items)}
    return reads


# ── Report ──────────────────────────────────────────────────────────────────

def _worst(statuses: list[str]) -> str:
    return next((status for status in _WORST_FIRST if status in statuses), _NOT_RUN)


def _static_home(plan: list[dict[str, Any]], message: str) -> str:
    """The plan row a static finding is about: the most specific one it names, else the page."""
    named = [row for row in plan if row["kind"] in {"entry", "block", "data_view", "section"}
             and re.search(rf"(?<![\w.:-]){re.escape(row['key'])}(?![\w-])", message)]
    return max(named, key=lambda row: len(row["key"]))["id"] if named else "page"


def app_self_check_report(plan: list[dict[str, Any]], *, errors: list[str], warnings: list[str],
                          reads: dict[str, dict[str, Any]] | None, static_ran: bool = True,
                          pre_existing: list[str] | None = None) -> dict[str, Any]:
    """L0 findings and L1 reads, through the plan. ``reads`` is None where there is nothing live to read."""
    items = [dict(row) for row in plan]
    by_id = {row["id"]: row for row in items}
    for row in items:
        row.update(reads.get(row["id"], {"status": _NOT_RUN}) if reads is not None else {"status": _NOT_RUN})
    for row in items:
        if row["kind"] in {"data_view", "source"}:
            row.pop("message", None)
            statuses = [by_id[child]["status"] for child in row.get("children", [])]
            # A source nothing reads is reported as such; the page works without it.
            row["status"] = _worst(statuses) if statuses else _PASSED
    for name, messages in (("errors", errors), ("warnings", warnings)):
        for message in messages:
            by_id[_static_home(plan, message)].setdefault(f"static_{name}", []).append(message)
    leaves = [row for row in items if row["kind"] not in {"data_view", "source"}]
    read_failures = [row["message"] for row in leaves if row["status"] == _FAILED]
    counts = {status: sum(row["status"] == status for row in leaves) for status in _WORST_FIRST}
    return {
        "status": _FAILED if errors or read_failures else _PASSED,
        "summary": _summary(counts, errors=errors, reads_ran=reads is not None, static_ran=static_ran),
        "limits": READS_ONLY if reads is not None else "Reads did not run, so this says nothing about them.",
        "counts": counts,
        "errors": errors,
        "warnings": warnings,
        "pre_existing_issues": pre_existing or [],
        "read_failures": read_failures,
        "items": items,
    }


def _summary(counts: dict[str, int], *, errors: list[str], reads_ran: bool, static_ran: bool) -> str:
    static = (f"{len(errors)} static check{'s' if len(errors) != 1 else ''} failed" if errors
              else "static checks passed" if static_ran else "static checks could not run")
    if not reads_ran:
        return f"Self-check: {static}; reads have not run on a live Workspace."
    parts = [static, f"{counts[_PASSED]} reads passed"]
    if counts[_FAILED]:
        parts.append(f"{counts[_FAILED]} failed")
    if counts[_NO_DATA]:
        parts.append(f"{counts[_NO_DATA]} had no data to read yet")
    if counts[_NOT_RUN]:
        parts.append(f"{counts[_NOT_RUN]} not read")
    return f"Self-check: {', '.join(parts)}. Reads only: nothing was clicked or started."


# ── Entry points ────────────────────────────────────────────────────────────

async def self_check_user(db: Any, workspace: Any, user_id: str | None) -> Any | None:
    """The current manager, presented in this Workspace's entity without persisting a switch."""
    from sqlalchemy.orm.attributes import set_committed_value

    from packages.core.models.user import User
    from packages.core.services.actor_authorization import AuthenticatedUserCredential
    from packages.core.services.collaborator_scope import actor_workspace_scope
    from packages.core.services.permission_gate import ResourcePermissionGate
    from packages.core.services.workspace_access import user_can_manage_workspace

    user = await db.get(User, user_id) if user_id and workspace is not None else None
    if user is None:
        return None
    authorized = await ResourcePermissionGate.authorize_workspace_read(
        db, credential=AuthenticatedUserCredential(
            user_id=str(user.id), entity_id=str(workspace.entity_id), token_version=user.token_version,
        ), workspace_id=workspace.id,
    )
    if authorized is None:
        return None
    scope = await actor_workspace_scope(db, user_id=user_id, entity_id=workspace.entity_id)
    if scope is not None and workspace.id not in scope:
        return None
    if not await user_can_manage_workspace(
        db, workspace_id=workspace.id, user_id=user_id, entity_role=authorized.actor.role,
    ):
        return None
    set_committed_value(user, "entity_id", authorized.actor.entity_id)
    set_committed_value(user, "role", authorized.actor.display_role)
    return user


async def self_check_app_page(db: Any, shown: Any, *, user: Any) -> dict[str, Any]:
    """L0 and L1 of the page ``shown`` carries: a saved page, or an ``AppViewPreview`` of a draft's.

    A Workspace with no page of its own shows the default one, which nobody
    designed, so only its reads are checked.
    """
    from packages.core.blueprints.exporter import ExportError
    from packages.core.services.workspace_app_designer import workspace_app_design_context
    from packages.core.services.workspace_app_view import AppViewPreview, normalize_app_view_config

    page = (shown.operating_model or {}).get("app_view")
    try:
        runtime = normalize_app_view_config(page)
    except ValueError as exc:
        message = f"the page is not a valid App config: {exc}"
        plan = [{"id": "page", "kind": "page", "key": "page", "must": ["is a valid App config"]}]
        return app_self_check_report(plan, errors=[message], warnings=[], reads=None)
    async with reads_only(db):
        try:
            context = await workspace_app_design_context(db, shown)
        except ExportError as exc:
            # A Workflow that cannot leave this Workspace says nothing about the page, whose reads still run.
            logger.info("App self-check of Workspace %s has no design context: %s", shown.id, exc)
            context = None
        portable, errors, warnings = await _static_checks(db, shown, runtime, context)
        pre_existing: list[str] = []
        if isinstance(shown, AppViewPreview) and errors:
            # Like a draft's validation: only what this draft introduces counts against it.
            before = await _saved_page_errors(db, shown, context)
            pre_existing = [message for message in errors if message in before]
            errors = [message for message in errors if message not in before]
        plan = app_test_plan(portable, context)
        reads = await _read_page(db, shown, user=user, plan=plan, portable=portable, runtime=runtime)
    return app_self_check_report(plan, errors=errors, warnings=warnings, reads=reads, static_ran=context is not None,
                                 pre_existing=pre_existing)


async def _static_checks(db: Any, workspace: Any, runtime: dict[str, Any], context: Any
                         ) -> tuple[dict[str, Any], list[str], list[str]]:
    """The page in portable keys, and L0's (errors, warnings) on it where it can run."""
    from packages.core.blueprints.app_view import remap_workspace_app_view, without_local_pictures

    if not isinstance((workspace.operating_model or {}).get("app_view"), dict):
        return runtime, [], []
    try:
        # An uploaded picture does not move to another Workspace, which says nothing about this page.
        portable = await remap_workspace_app_view(db, workspace, without_local_pictures(runtime), to_runtime=False)
    except ValueError as exc:
        return runtime, [f"the page names something this Workspace no longer has: {exc}: point it at what "
                         "the Workspace runs now, or remove it"], []
    if context is None:
        return portable, [], []
    _, errors, warnings = context.check(portable)
    return portable, errors, warnings


async def _saved_page_errors(db: Any, shown: Any, context: Any) -> list[str]:
    from packages.core.services.workspace_app_view import normalize_app_view_config

    saved = shown._workspace
    try:
        runtime = normalize_app_view_config((saved.operating_model or {}).get("app_view"))
    except ValueError:
        return []
    return (await _static_checks(db, saved, runtime, context))[1]


def candidate_read_probe(*, workspace_id: str, entity_id: str, user_id: str | None
                         ) -> Callable[[dict[str, Any], list[dict[str, Any]]], Awaitable[dict[str, dict[str, Any]] | None]]:
    """The reads for a designer's candidates on a live Workspace, as the person who asked."""

    async def probe(config: dict[str, Any], plan: list[dict[str, Any]]) -> dict[str, dict[str, Any]] | None:
        from packages.core.blueprints.app_view import remap_workspace_app_view
        from packages.core.database import async_session
        from packages.core.models.workspace import Workspace
        from packages.core.services.workspace_app_view import AppViewPreview

        # The designer holds no transaction while it works, so each candidate reads in its own.
        async with async_session() as db:
            workspace = await db.get(Workspace, workspace_id)
            if workspace is None or workspace.entity_id != entity_id:
                return None
            user = await self_check_user(db, workspace, user_id)
            if user is None:
                return None
            async with reads_only(db):
                runtime = await remap_workspace_app_view(db, workspace, config, to_runtime=True)
                return await _read_page(db, AppViewPreview(workspace, runtime), user=user, plan=plan,
                                        portable=config, runtime=runtime)

    return probe


async def self_check_candidate(context: Any, config: dict[str, Any], warnings: list[str]) -> dict[str, Any]:
    """The report of a candidate that passed ``context.check``, read where it can be."""
    plan = app_test_plan(config, context)
    reads = None
    if context.read_probe is not None:
        try:
            reads = await context.read_probe(config, plan)
        except Exception as exc:  # noqa: BLE001 - a platform fault must not stop a design
            logger.warning("App self-check could not read the candidate: %s", exc, exc_info=True)
    return app_self_check_report(plan, errors=[], warnings=warnings, reads=reads)


# ── On the draft ────────────────────────────────────────────────────────────

def _page_digest(state: Any) -> str:
    page = ((state or {}).get("operating_model") or {}).get("app_view")
    return hashlib.sha256(json.dumps(page, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()


def compact_self_check(report: dict[str, Any]) -> dict[str, Any]:
    """What a draft and a design note keep: the findings, without the plan's rows."""
    return {key: report[key] for key in ("status", "summary", "limits", "counts", "errors", "warnings",
                                         "pre_existing_issues", "read_failures")}


def _self_check_warnings(kept: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"path": "app_view.self_check", "code": "app_self_check", "message": message}
            for message in [*kept["errors"], *kept["read_failures"]]]


def attach_app_self_check(draft: Any, report: dict[str, Any]) -> None:
    """Put the report where the review card reads a draft's findings; it never blocks the card."""
    kept = {**compact_self_check(report), "page_digest": _page_digest(draft.current_state)}
    validation = dict(draft.validation or {})
    draft.validation = {**validation, "app_self_check": kept,
                        "warnings": [*_self_check_warnings(kept), *(validation.get("warnings") or [])]}


def carry_app_self_check(previous: Any, state: Any, validation: dict[str, Any]) -> None:
    """Keep a report over a draft's revalidation, for as long as its page is the one it read."""
    kept = previous.get("app_self_check") if isinstance(previous, dict) else None
    if isinstance(kept, dict) and kept.get("page_digest") == _page_digest(state):
        validation["app_self_check"] = kept
        validation["warnings"] = [*_self_check_warnings(kept), *validation["warnings"]]
