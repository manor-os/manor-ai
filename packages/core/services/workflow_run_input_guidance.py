"""Which control each Workflow run input gets, so a person never types what they could pick.

Every prompt that writes Workflows reads this one text -- the Architect's own
workflow step and the Workflows designer's skill -- and ``run_input_issues``
checks the same rules on what they write. The run form already draws each
control from the input's ``schema`` (``workflowSchema.ts``); this is about
asking for it.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

RUN_INPUT_TYPES = """\
Give each run input the control that fits what it holds, in its
``schema``; a plain text box is only for words the person writes (a
topic, a brief, a note):
  - a picture, video, recording or file: an upload, ``type:'json'``,
    schema ``{type:'array',items:{type:'object',required:['document_id'],
    properties:{document_id:{type:'string'}}},maxItems:1,
    'x-ui':{control:'documents',accept:['image']}}`` (accept image,
    video, audio or document; drop maxItems for several). Never ask for a
    document id or a link to the file as text. An upload is optional
    unless the work cannot start without it (the footage to edit, the
    contract to review): only then mark it required with ``minItems:1``.
    A dish, product or listing photo can come later. A project keeps an
    upload as ``<name>_document_id:'{{<key>.0.document_id}}'``; when
    nothing was uploaded the key is left out.
  - one of a few fixed values (a status, a level, a size): ``enum`` with
    ``x-enum-labels`` in the person's language, and
    ``x-ui:{control:'segmented'}`` for up to five; several of them:
    ``{type:'array',items:{type:'string',enum:[...]}}``; yes/no is
    ``type:'boolean'``.
  - a day or a moment: ``format:'date'`` or ``format:'date-time'``; a
    period is one ``type:'json'`` input, schema ``{type:'object',
    properties:{start:{type:'string',format:'date'},end:{type:'string',
    format:'date'}},required:['start','end'],'x-ui':{control:'date_range'}}``
    read as ``{{<key>.start}}`` / ``{{<key>.end}}`` (or two inputs
    ``start_date`` and ``end_date``), never one text.
  - other values with their own control: an email ``format:'email'``, a
    phone number ``format:'phone'``, a brand colour
    ``'x-ui':{control:'color'}`` (``#RRGGBB``), a score
    ``{type:'integer',minimum:1,maximum:5,'x-ui':{control:'rating'}}``,
    free tags ``{type:'array',items:{type:'string'},'x-ui':{control:
    'tags'}}``, formatted notes ``'x-ui':{control:'rich_text'}``
    (Markdown), a street address ``'x-ui':{control:'address'}``, a
    signature ``type:'json'``, the upload schema with ``maxItems:1`` and
    ``'x-ui':{control:'signature'}``, rows of an order or quote
    ``type:'json'``, ``{type:'array',items:{type:'object',properties:
    {<column>:{type:'string'|'number'|'integer',title}}},'x-ui':{control:
    'line_items'}}``. A photo or recording taken on the phone is the
    upload with ``'x-ui':{control:'documents',accept:['image'],capture:
    'environment'}`` (``accept:['audio'],capture:'user'`` to record).
  - a record of this Workspace (a project one of its Workflows creates):
    a picker, ``{type:'string','x-ui':{control:'workflow_project_select',
    project_type:'<type>'}}``, read with a ``workflow_project`` get.
    Several records of one type (the dishes of a promotion, the products
    of a bundle): ``type:'json'``, schema ``{type:'array',items:{type:
    'string'},minItems:1,'x-ui':{control:'workflow_project_select',
    project_type:'<type>'}}`` (``maxItems`` caps it); the value is the
    list of picked project ids. Never ask for records as text.
  - changing a record: the Flow takes only its picker, gets the project,
    then asks with a ``wait`` ``{wait_type:'input',message,
    response_variable:'changes',input_schema:{type:'object',properties}}``
    whose properties use the same controls with
    ``default:'{{<get step>.state.<field>}}'`` (an upload:
    ``default:[{document_id:'{{<get step>.state.<name>_document_id}}'}]``;
    a picker of several opens with the id list the record keeps),
    so the form opens with the current values; the patch writes
    ``{{changes.<field>}}``. Never ask for every field again as run inputs."""

# The last word of a key, or a label word, naming the media kind it holds.
_MEDIA_WORDS = {
    "image": {"photo", "photos", "image", "images", "picture", "pictures", "pic", "pics", "logo", "logos", "poster",
              "posters", "cover", "thumbnail", "thumbnails", "screenshot", "screenshots", "headshot", "avatar"},
    "video": {"video", "videos", "clip", "clips", "footage"},
    "audio": {"audio"},
    "document": {"file", "files", "attachment", "attachments", "upload", "uploads", "pdf"},
}
# A label asking for files in a text box ("Photo document ID or URL", "Uploaded media references").
_MEDIA_LABEL = re.compile(r"document id|file id|\bupload|\battach|上传|附件", re.IGNORECASE)
_LINK_KEY = re.compile(r"(?:^|_)(?:url|urls|link|links|uri|href)$")
# Words of keys that hold one of a few values.
_CHOICE_WORDS = {"status", "level", "priority", "tier", "mode", "stage", "visibility", "severity"}
# "Status: active or unlisted", "(mild/medium/hot)", "开/关".
_CHOICE_LABEL = re.compile(
    r"(?:[:：(（]\s*)[\w\- ]{1,20}?(?:\s*(?:/|,|，|、|\bor\b|或)\s*[\w\- ]{1,20}?)+\s*[)）]?\s*$", re.IGNORECASE)
_DATE_WORDS = {"date", "dates", "deadline", "datetime", "start", "end", "starts", "ends", "period", "when"}
_DATE_ACTIONS = _DATE_WORDS | {"created", "updated", "published", "scheduled", "submitted", "due", "expires"}
_DATE_LABEL = re.compile(r"\bdates?\b|\bdeadline\b|日期|截止|起止|开始时间|结束时间", re.IGNORECASE)
# Controls that already pick something rather than take free text.
_PICKERS = {"documents", "knowledge_file", "workflow_project_select", "record_select", "timezone", "audio_clip",
            "range", "segmented", "aspect", "date_range", "tags", "color", "rating", "signature", "line_items"}


def _schema(row: dict[str, Any]) -> dict[str, Any]:
    schema = row.get("schema")
    return schema if isinstance(schema, dict) else {}


def _control(schema: dict[str, Any]) -> str:
    ui = schema.get("x-ui")
    return str(ui.get("control") or "") if isinstance(ui, dict) else ""


def _words(key: str) -> list[str]:
    return [word for word in key.lower().split("_") if word]


# The row types the run form knows (``workspace_workflow_router.workflow_run_inputs``); any other is a text box.
_ROW_TYPES = {"string", "number", "integer", "boolean", "json"}
# Row keys that look like a control but that the form never reads: it reads ``schema``.
_IGNORED_ROW_KEYS = ("options", "enum", "choices", "accept", "project_type", "format", "control", "x-ui", "items")


def _row_type(row: dict[str, Any]) -> str:
    kind = str(row.get("type") or "string").strip().lower()
    return kind if kind in _ROW_TYPES else "string"


def _free_text(row: dict[str, Any], *, kinds: frozenset[str] = frozenset({"string"})) -> bool:
    """A visible input the form shows as a box the person types into."""
    if row.get("hidden"):
        return False
    schema = _schema(row)
    kind = str(schema.get("type") or _row_type(row)).lower()
    items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
    return (kind in kinds and not schema.get("enum") and not schema.get("format")
            and "const" not in schema and not items.get("enum") and _control(schema) not in _PICKERS)


def _media_kind(key: str) -> str | None:
    """The media kind a key's last word names: ``photo``, ``cover_image``, ``source_document_ids``."""
    if _LINK_KEY.search(key.lower()):
        return None
    words = _words(key)
    last = words[-1] if words else ""
    if last in {"id", "ids"} and len(words) > 1 and words[-2] in {"document", "file"}:
        return "document"
    return next((kind for kind, names in _MEDIA_WORDS.items() if last in names), None)


def _project_types(workflows: Iterable[dict[str, Any]]) -> dict[str, dict[str, str]]:
    """Each project type a design creates -> state field -> the run input it was set from."""
    created: dict[str, dict[str, str]] = {}
    for workflow in workflows:
        inputs = {str(row.get("key")) for row in workflow.get("run_inputs") or [] if isinstance(row, dict)}
        for step in workflow.get("steps") or []:
            config = step.get("config") if isinstance(step, dict) and isinstance(step.get("config"), dict) else {}
            if step.get("type") != "workflow_project" or config.get("operation") not in {"create", "claim"}:
                continue
            project_type = str(config.get("project_type") or "").strip()
            if not project_type or "{{" in project_type:
                continue
            fields = created.setdefault(project_type, {})
            state = config.get("state") if isinstance(config.get("state"), dict) else {}
            for name, value in state.items():
                match = re.fullmatch(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}", value) if isinstance(value, str) else None
                if match and match.group(1) in inputs:
                    fields[str(name)] = match.group(1)
    return created


_RECORD_SUFFIXES = ("id", "ids", "key", "keys", "ref", "refs", "reference", "references")


def _names_record(key: str, project_type: str) -> bool:
    """``dish``, ``dishes``, ``dish_id`` or ``eligible_dish_references`` name records of type ``dish``."""
    key, kind = key.lower(), project_type.lower()
    plurals = {f"{kind}s", f"{kind}es", f"{kind[:-1]}ies" if kind.endswith("y") else f"{kind}s"}
    if key in {kind, *plurals}:
        return True
    return any(key == f"{kind}_{suffix}" or key.endswith(f"_{kind}_{suffix}") for suffix in _RECORD_SUFFIXES)


def _picked_projects(workflow: dict[str, Any]) -> dict[str, str]:
    """Run input key -> the project type its picker chooses."""
    picked = {}
    for row in workflow.get("run_inputs") or []:
        schema = _schema(row) if isinstance(row, dict) else {}
        ui = schema.get("x-ui") if isinstance(schema.get("x-ui"), dict) else {}
        if ui.get("control") == "workflow_project_select" and ui.get("project_type"):
            picked[str(row.get("key"))] = str(ui["project_type"])
    return picked


def _edit_issues(workflow: dict[str, Any], created: dict[str, dict[str, str]]) -> list[str]:
    """A Flow that changes a picked record by asking for its fields again as run inputs."""
    picked = _picked_projects(workflow)
    visible = {str(row.get("key")) for row in workflow.get("run_inputs") or []
               if isinstance(row, dict) and not row.get("hidden")}
    gets = {}
    for step in workflow.get("steps") or []:
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        if step.get("type") == "workflow_project" and config.get("operation") == "get":
            source = re.fullmatch(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}", str(config.get("project_id") or ""))
            if source and source.group(1) in picked:
                gets[str(step.get("id"))] = picked[source.group(1)]
    issues = []
    for step in workflow.get("steps") or []:
        config = step.get("config") if isinstance(step.get("config"), dict) else {}
        if step.get("type") != "workflow_project" or config.get("operation") != "patch":
            continue
        project_type = str(config.get("project_type") or "")
        if project_type not in gets.values():
            continue
        patch = config.get("patch") if isinstance(config.get("patch"), dict) else {}
        asked = sorted(name for name, value in patch.items()
                       if name in created.get(project_type, {}) and isinstance(value, str)
                       and (match := re.fullmatch(r"\{\{\s*([A-Za-z_]\w*)\s*\}\}", value))
                       and match.group(1) in visible)
        if asked:
            get_step = next(step_id for step_id, kind in gets.items() if kind == project_type)
            issues.append(
                f"workflows.{workflow.get('slug')}.{step.get('id')}: this Flow changes a {project_type} it picks but asks "
                f"for {', '.join(asked)} again as run inputs, so the person retypes what the record already holds: keep "
                f"only the picker, then add a wait {{wait_type:'input', response_variable:'changes', input_schema:"
                f"{{type:'object', properties:{{<field>:{{..., default:'{{{{{get_step}.state.<field>}}}}'}}}}}}}} after "
                f"{get_step!r} so the form opens with the current values, and patch {{{{changes.<field>}}}}")
    return issues


def run_input_issues(workflows: list[dict[str, Any]], *, checked: set[str] | None = None) -> list[str]:
    """Run inputs asked as free text that ``RUN_INPUT_TYPES`` gives a control.

    ``checked`` limits the Workflows checked; all of them say which project
    types the design creates.
    """
    created = _project_types(workflows)
    issues: list[str] = []
    for workflow in workflows:
        # A Flow only other Flows run is filled by them, never by a person.
        if (checked is not None and workflow.get("slug") not in checked) or workflow.get("internal"):
            continue
        for row in workflow.get("run_inputs") or []:
            if isinstance(row, dict) and str(row.get("key") or "").strip():
                issue = _input_issue(row, created)
                if issue:
                    issues.append(f"workflows.{workflow.get('slug')}.run_inputs.{str(row['key']).strip()} {issue}")
        issues.extend(_edit_issues(workflow, created))
    return issues


def _a(kind: str) -> str:
    return f"an {kind}" if kind[0] in "aeiou" else f"a {kind}"


def _input_issue(row: dict[str, Any], created: dict[str, dict[str, str]]) -> str | None:
    """What a person asked this input would have to type that they could pick, if anything."""
    kind = str(row.get("type") or "string").strip().lower()
    ignored = [name for name in _IGNORED_ROW_KEYS if name in row]
    if kind not in _ROW_TYPES or ignored:
        said = ([f"type {kind!r}"] if kind not in _ROW_TYPES else []) + [f"{name!r}" for name in ignored]
        return (f"declares {', '.join(said)} on the input itself, which the run form ignores, so it shows a plain text "
                "box: type is only string, number, integer, boolean or json, and the control goes in schema "
                "(enum, format, 'x-ui':{control, accept, project_type})")
    if row.get("hidden"):
        return None
    key = str(row["key"]).strip()
    label = str(row.get("label") or "")
    schema = _schema(row)
    control = _control(schema)
    words = _words(key)
    last = words[-1] if words else ""
    record = next((kind for kind in created if _names_record(key, kind)), None)
    if record is not None:
        several = key.lower() != record.lower() and (last in {"ids", "keys", "refs", "references"}
                                                     or not key.lower().endswith(f"{record.lower()}_{last}"))
        if control == "workflow_project_select":
            if schema.get("type") == "array" and kind != "json":
                return (f"picks several {record} records but is type {kind!r}, so the run reads the list as one "
                        "text: make the input type:'json'")
            return None
        if several:
            return (f"names {record} records as text, so the person types ids they cannot see: make it a picker of "
                    f"several, type:'json', schema {{type:'array',items:{{type:'string'}},minItems:1,"
                    f"'x-ui':{{control:'workflow_project_select',project_type:'{record}'}}}}")
        return (f"names {record} records as text, so the person types ids they cannot see: make it a picker, schema "
                f"{{type:'string','x-ui':{{control:'workflow_project_select',project_type:'{record}'}}}}")
    media = _media_kind(key)
    if media is not None and control == "documents":
        if media != "document" and not (schema.get("x-ui") or {}).get("accept"):
            return f"uploads {_a(media)}: add 'x-ui':{{accept:['{media}']}} so the picker lists only those files"
        return None
    upload = ("type:'json', schema {type:'array',items:{type:'object',required:['document_id'],properties:"
              "{document_id:{type:'string'}}},maxItems:1,'x-ui':{control:'documents',accept:['%s']}}")
    if media is not None and _free_text(row, kinds=frozenset({"string", "json"})):
        return f"asks for {_a(media)} as text: make it an upload, " + upload % media
    if not _free_text(row):
        return None
    if _MEDIA_LABEL.search(label):
        return ("asks for files inside a text box: keep this input for the words and add an upload input for the "
                "files, " + upload % "image" + " (accept the kinds it takes; drop maxItems for several)")
    dated_action = last in {"at", "on"} and len(words) > 1 and words[-2] in _DATE_ACTIONS
    if last in _DATE_WORDS or dated_action or _DATE_LABEL.search(label):
        if last in {"dates", "period"} or key.lower() == "date_range":
            return ("asks for a period as one text: make it type:'json', schema {type:'object',properties:"
                    "{start:{type:'string',format:'date'},end:{type:'string',format:'date'}},required:['start','end'],"
                    "'x-ui':{control:'date_range'}} (or ask start_date and end_date as two inputs, each schema "
                    "{type:'string',format:'date'})")
        return "asks for a date as text: give it schema {type:'string',format:'date'} (format:'date-time' for a moment)"
    if last in _CHOICE_WORDS or _CHOICE_LABEL.search(label):
        return ("takes one of a few values as text: give it schema {type:'string',enum:[...],'x-enum-labels':[...],"
                "'x-ui':{control:'segmented'}} (several of them: {type:'array',items:{type:'string',enum:[...]}})")
    return None
