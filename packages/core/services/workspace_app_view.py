"""Project live Workspace capabilities into its app panel."""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from packages.core.contracts.project_display import PROJECT_DISPLAY_PATHS
from packages.core.services.viewer_language import localized, sanitize_i18n

_ACTION_KEY = re.compile(r"^(workflow|service|automation|action):[A-Za-z0-9_.:-]{1,128}$")
_ACTION_KEY_FORMS = "workflow:<id>, service:<key>, automation:<id> or action:<id>"
_FIELD_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_FORM_FIELD_TYPES = ("text", "long_text", "file", "number", "select", "picture_choice")
# A picture choice shows a picture the platform ships with the page (its web asset), never a remote URL.
_PICTURE_PATH = re.compile(r"^/blueprints/[A-Za-z0-9_./-]{1,200}\.(webp|png|jpg|jpeg)$")
_DATA_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_DATA_SOURCE_TYPES = {
    "workflow_projects", "related_projects", "project_records", "project_documents",
    "publication_stats", "workspace_stats", "owned_automations",
    # A project's own folder (``state.project_root``) and the references it
    # holds or inherits (e.g. the voices a series pins for its episodes).
    "project_files", "project_references",
    # What a Flow's runs made (a song, a music video), read from each run's own
    # results: the latest version of each, never a failed or cancelled attempt.
    "workflow_results",
}
_DATA_BLOCK_TYPES = {"progress", "records", "documents", "metrics", "summary", "project_rollup", "references", "packs",
                     "gallery", "chart"}
PUBLICATION_STAT_COLLECTOR_TYPES = ("public_post", "manual")
DEFAULT_PUBLICATION_STAT_COLLECTOR_TYPES = ["public_post"]
_DATA_CONTEXT_KINDS = {"project", "launch_run", "workspace"}
# The options a data_view's context and each of its blocks accept.
_DATA_CONTEXT_OPTIONS = ("kind", "source", "label", "display", "cover", "progress")
_DATA_CONTEXT_DISPLAYS = ("select", "cards")
# ``grid`` is a dashboard: charts and numbers two to a row, a list or table across the row; one per row on a phone.
_DATA_DISPLAY_LAYOUTS = ("stack", "tabs", "grid")
_DATA_BLOCK_OPTIONS = (
    "key", "type", "source", "title", "fields", "metric_keys", "limit", "metrics_source", "metrics_display",
    "record_display", "count_by_stage", "first", "empty_message", "completion", "stage_labels", "plan",
    "item_action", "item_actions", "result_action", "plan_action", "add_action", "filters", "search", "view",
    "media", "preview", "group_by", "select", "export", "totals", "timeline", "places", "tree", "chart",
)
# How a gallery block presents its files (``media``). ``layout``: tiles in a grid (default), a masonry wall of
# pictures in their own shapes, a carousel swiped one by one, a strip of small thumbnails, a board (a mood board
# collage) or a list (a row per file with its player: tracks, episodes, voices to compare).
_MEDIA_LAYOUTS = ("grid", "masonry", "carousel", "strip", "board", "list")
# The frame a picture or clip shows in; ``auto`` is each file's own shape, never cropped (a 9:16 short stays 9:16).
MEDIA_ASPECTS = ("auto", "9:16", "4:5", "3:4", "1:1", "4:3", "16:9")
# A gallery's yes/no presentation options: the pixel size on each picture, a checkerboard behind cut-outs, a clip
# playing on hover, the file's details (size, model, prompt), its download, and comments pinned to a spot or a moment.
_MEDIA_FLAGS = ("size_label", "transparent", "hover_play", "details", "download", "comments")
_MEDIA_OPTIONS = ("layout", "aspect", "group", *_MEDIA_FLAGS)
# ``group: "record"`` shows one card per record or result with each of its files as a version (9:16, 1:1, 16:9).
_MEDIA_GROUPS = ("record",)
# A block's records shown the way each post will look where it goes (``preview``): one presentation, a neutral
# frame per platform (layout and proportions, never a platform's branding). instagram is a feed post (several
# pictures swipe as a carousel), the vertical ones (reels, story, tiktok, douyin, youtube_shorts) a 9:16 screen
# with the caption's safe area marked, wechat_article an Official Account article, wechat_moments a 1–9 grid.
PREVIEW_PLATFORMS = ("instagram", "instagram_reels", "instagram_story", "xiaohongshu", "tiktok", "douyin",
                     "youtube_shorts", "x", "linkedin", "facebook", "youtube", "wechat_article", "wechat_moments",
                     "pinterest")
# The parts of a post a preview reads, each from a path its source declares: ``platform`` a record's own
# platform (its frame, over ``preview.platform``), ``images`` and ``video`` the post's files (Document ids),
# ``cover`` a video's cover, ``hashtags`` a list of words (copyable), ``account`` and ``avatar`` who posts it,
# ``link`` the address it carries, ``published_at`` when it goes or went out, ``summary`` an article's digest,
# ``thread`` the posts after the first (a list of text), ``comments`` the replies a Workflow collected
# ``[{author, text, at, likes}]``.
PREVIEW_FIELDS = ("platform", "images", "video", "cover", "title", "caption", "hashtags", "account", "avatar",
                  "link", "published_at", "summary", "thread", "comments")
# What a post is made of: a preview reads at least one of them (a gallery also shows its own files).
_PREVIEW_CONTENT = ("images", "video", "cover", "title", "caption")
# ``feed`` lists the posts one after another; ``side_by_side`` puts them in one row (a post's version for each
# platform), tabs on a phone, a row per ``group_field`` value when one is given.
_PREVIEW_LAYOUTS = ("feed", "side_by_side")
_PREVIEW_OPTIONS = ("platform", "fields", "aspect", "layout", "group_field")
_PREVIEW_BLOCK_TYPES = ("progress", "records", "gallery")
# How a progress block lays out its items: a list of cards, a table with a column per field, a board with a
# column per stage, a timeline (its ``timeline.date_field`` in time order, by day), a gantt (bars from
# ``timeline.date_field`` to ``end_field``, arrows from its ``depends_field``), places (its ``places`` grouped by
# where they are, each with a maps link; no map tiles) or a tree (each record and its ``tree.children`` projects).
_DATA_BLOCK_VIEWS = ("list", "table", "board", "timeline", "gantt", "places", "tree")
# The views a person can group by a field (``group_by``); on a board each group is a row of the stage columns.
_GROUP_VIEWS = ("list", "table", "board")
# The formats a table can total (``totals``) and draw as a bar in its cell (a field's ``bar``).
NUMERIC_FIELD_FORMATS = ("number", "money", "percent", "duration", "file_size", "rating")
_TOTAL_AGGREGATES = ("sum", "avg", "min", "max")
# A ``chart`` block draws its source: ``kind`` the picture; ``x`` the categories or time buckets ({field, bucket}
# or {by: stage}); ``value`` what each bar or point measures ({agg, field}); ``series`` splits it (stacked, several
# lines, a heatmap's rows); ``y`` a scatter's second number. A ``workspace_stats`` source draws each Stat's
# readings over time, one series per Stat.
CHART_KINDS = ("line", "area", "bar", "stacked_bar", "stacked_area", "donut", "funnel", "leaderboard", "heatmap",
               "scatter")
CHART_BUCKETS = ("day", "week", "month", "weekday", "hour")
CHART_TIME_BUCKETS = ("day", "week", "month")
CHART_AGGREGATES = ("count", "sum", "avg", "min", "max")
_CHART_STAT_AGGREGATES = ("last", "sum", "avg", "min", "max")
CHART_FORMATS = ("number", "money", "percent", "duration")
_CHART_OPTIONS = ("kind", "x", "y", "value", "series", "format", "currency", "unit", "scale", "labels", "order",
                  "limit", "target")
_CHART_SOURCES = ("workflow_projects", "related_projects", "project_records", "workspace_stats")
_CHART_TARGET_KINDS = ("line", "area", "bar", "stacked_bar", "stacked_area")
# How much of the calendar shows at once; the person can switch to the other.
_DATA_CALENDAR_SPANS = ("month", "week")
_SECTION_OPTIONS = ("id", "block", "source", "view", "limit", "items", "title")
_DATA_COMPONENT_OPTIONS = ("type", "schema_version", "key", "title", "description", "context", "blocks", "display", "visibility")
_DATA_DISPLAY_OPTIONS = ("layout", "groups", "hidden", "hidden_blocks", "calendar")
_DATA_FIELD_OPTIONS = ("field", "label", "format", "hide_when_empty", "labels", "secondary", "project_type", "currency",
                       "scale", "end_field", "relative", "max", "max_field", "unit", "copy",
                       "aspect", "layout", "transparent", "compare_field", "compare", "captions_field",
                       "transcript_field", "chapters_field", "bar")
# ``image`` shows a Document id (or ``{document_id}``) as its picture; ``record`` shows the
# Workspace records (project ids, one or a list) a field holds by their names, of ``project_type`` when set;
# ``money`` reads in its ``currency``. ``percent`` reads a share on its ``scale`` (1: 0–1, 100: 0–100);
# ``duration`` a number of seconds; ``file_size`` a number of bytes; ``date_range`` its ``field`` to its
# ``end_field``; ``progress`` a number out of ``max`` (or the number at ``max_field``, else 100);
# ``member`` the Workspace people or AI staff (user or agent ids) by name and picture; ``tags`` a list of words;
# ``color`` a #rrggbb swatch; ``audio`` a Document id to play; ``images`` several Document ids as a strip;
# ``clips`` several video or audio Document ids (a video's shots, one line in several voices), each played in
# place; ``timeline`` a list of segments ``{start, end|duration, label, track}`` in seconds, laid out in time.
_DATA_FIELD_FORMATS = ("text", "status", "date", "link", "link_list", "document", "provider", "image", "record",
                       "number", "money", "boolean", "rating", "video", "percent", "duration", "date_range",
                       "phone", "email", "address", "tags", "progress", "member", "file_size", "color", "audio",
                       "images", "code", "clips", "timeline", "sparkline")
# ``sparkline`` draws a list of numbers (or of {value}) as a small trend line in the card.
# The formats whose value is a Document id (or a list of them): never shown as text.
DOCUMENT_FIELD_FORMATS = frozenset({"document", "image", "video", "audio", "images", "clips"})
# A media field's own presentation. ``aspect`` frames a picture or clip (MEDIA_ASPECTS); ``layout`` lays an
# ``images`` field out as a strip, a carousel or a grid; ``transparent`` puts a checkerboard behind a cut-out;
# ``compare_field`` names another picture or clip (the reference, the version before) shown against it, as a
# ``compare`` slider (pictures) or side by side; ``captions_field`` a captions file (.vtt/.srt) over a video;
# ``transcript_field`` its words (a captions file, or segments ``{start, end, text, speaker}``) highlighted as
# it plays; ``chapters_field`` its chapters (``{at, title}``) to jump to.
_IMAGES_LAYOUTS = ("strip", "carousel", "grid")
_COMPARE_MODES = ("slider", "side_by_side")
_MEDIA_FIELD_PATHS = ("compare_field", "captions_field", "transcript_field", "chapters_field")
# The formats a person copies from (a caption, hashtags, a link, a phone number): ``copy: true`` adds the button.
_COPYABLE_FIELD_FORMATS = ("text", "link", "code", "phone", "email", "address", "tags")
# ``percent``'s ``scale``: the stored number that means 100 %.
_PERCENT_SCALES = (1, 100)
# The fields each source type takes besides ``type``. A check for the others names them.
_DATA_SOURCE_FIELDS: dict[str, dict[str, tuple[str, ...]]] = {
    "workflow_projects": {"required": ("project_type", "producer_workflows"), "optional": (
        "fields", "label_fields", "stages", "exclude_stages", "order", "schema_versions", "launch_workflows", "execution_run_id_path")},
    "related_projects": {"required": ("project_type", "producer_workflows", "parent"), "optional": (
        "fields", "label_fields", "stages", "exclude_stages", "order", "schema_versions", "parent_state_path", "execution_run_id_path")},
    "project_records": {"required": ("parent", "record_path"), "optional": ("record_schema", "fields", "producer_workflows")},
    "project_documents": {"required": ("parent", "document_path"), "optional": ("document_role", "fields", "producer_workflows")},
    "publication_stats": {"required": ("parent", "subject", "metric_keys"), "optional": ("collector_types", "fields", "producer_workflows")},
    "workspace_stats": {"required": ("stat_keys",), "optional": ("fields", "producer_workflows")},
    "owned_automations": {"required": ("parent",), "optional": ("fields", "producer_workflows")},
    "project_files": {"required": ("parent",), "optional": ("results_only", "fields", "producer_workflows")},
    "project_references": {"required": ("parent",), "optional": ("reference_kind", "fields", "producer_workflows")},
    "workflow_results": {"required": ("producer_workflows",), "optional": ("fields", "label_fields", "media", "versions")},
}


class AppViewContractError(ValueError):
    """Every problem one App config has, so its author fixes them in one pass.

    Its text joins them, so a caller that reports a ValueError's text keeps working.
    """

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("; ".join(self.problems))


def app_view_problems(error: ValueError) -> list[str]:
    """The problems a rejected App config has, one per item."""
    return error.problems if isinstance(error, AppViewContractError) else [str(error)]


class _CheckedSources:
    """A page's data sources while it is checked.

    A source with a reported problem stays declared, and what depends on its
    content is not judged: its own error already names what to fix.
    """

    def __init__(self) -> None:
        self.items: dict[str, dict[str, Any]] = {}
        self.broken: set[Any] = set()

    def names(self) -> list[str]:
        return [*self.items, *map(str, self.broken)]

    def declared(self, name: Any) -> bool:
        return isinstance(name, str) and (name in self.items or name in self.broken)

    def is_type(self, name: str, *types: str) -> bool:
        return name in self.broken or self.items[name]["type"] in types


def _shown(value: Any) -> str:
    """A rejected value, short enough to repeat in an error."""
    text = repr(value)
    return text if len(text) <= 80 else f"{text[:77]}..."


def _names(names: Any) -> str:
    return ", ".join(sorted(str(name) for name in names)) or "none"


def _dotted(paths: Any) -> str:
    return ", ".join(".".join(path) for path in paths)


async def load_workspace_app_data_catalog(db: Any, workspace: Any, *, user: Any = None) -> dict[str, Any]:
    """Return the native catalog used to validate App reads.

    With ``user`` (an author saving a config) only the definitions they can
    read count. Without one it is the Workspace's own catalog: a viewer the
    route already admitted reads the saved config whether or not they could
    open each producer definition (a trial collaborator, a teammate next to a
    private Flow), so their view never turns into a broken configuration.

    Internal producer definitions are retained here because they describe a
    read contract; callers must not use this catalog as an executable target
    catalog.  The normal App runner projection remains separate.
    """
    from sqlalchemy import select
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition
    from packages.core.models.workspace_stat import WorkspaceStat
    from packages.core.models.permission import ResourceType
    from packages.core.services.resource_access import ResourceDescriptor, readable_resource_ids

    entity_id = workspace.entity_id
    definitions = list((await db.execute(select(WorkflowDefinition).where(
        WorkflowDefinition.entity_id == entity_id,
        WorkflowDefinition.workspace_id == workspace.id,
    ))).scalars())
    if user is not None:
        readable = await readable_resource_ids(
            db, descriptors=[ResourceDescriptor.from_row(row, ResourceType.WORKFLOW) for row in definitions],
            entity_id=entity_id, user_id=user.id, role=user.role,
        )
        definitions = [row for row in definitions if row.id in readable]
    bindings = set((await db.execute(select(WorkflowBinding.id).where(
        WorkflowBinding.entity_id == entity_id, WorkflowBinding.workspace_id == workspace.id,
    ))).scalars())
    stats = set((await db.execute(select(WorkspaceStat.key).where(
        WorkspaceStat.entity_id == entity_id, WorkspaceStat.workspace_id == workspace.id,
    ))).scalars())
    return {"workflow_definitions": {row.id for row in definitions}, "workflow_bindings": bindings,
            "stat_keys": stats,
            "workflows": [{"id": row.id, "slug": row.id, "steps": row.steps or []} for row in definitions]}


def _short_text(value: Any, *, field: str, limit: int, required: bool = False) -> str | None:
    if value is None and not required:
        return None
    if value is None:
        raise ValueError(f"{field} is required: text of 1–{limit} characters")
    if not isinstance(value, str) or not (1 <= len(value.strip()) <= limit):
        raise ValueError(f"{field} must be {'1–' if required else 'at most '}{limit} characters, not {_shown(value)}")
    return value.strip()


def _path(value: Any, *, field: str) -> list[str]:
    if (not isinstance(value, list) or not 1 <= len(value) <= 8
            or any(not isinstance(part, str) or not _DATA_KEY.fullmatch(part) for part in value)):
        raise ValueError(f'{field} must be a bounded field path, a list of 1–8 identifiers such as ["title"], not {_shown(value)}')
    return list(value)


def _result_path(value: Any, *, field: str) -> list[str]:
    """A run-variable path; it may start at the run's final result (``__result``)."""
    if isinstance(value, list) and value[:1] == ["__result"]:
        return ["__result", *(_path(value[1:], field=field) if len(value) > 1 else [])]
    return _path(value, field=field)


def _secondary_paths(item: dict[str, Any], *, field: str) -> dict[str, list[list[str]]]:
    """Optional fallback paths shown beside a field value; the first non-empty one wins."""
    if "secondary" not in item:
        return {}
    paths = item["secondary"]
    if not isinstance(paths, list) or not 1 <= len(paths) <= 4:
        raise ValueError(f"{field} must be 1–4 field paths, not {_shown(paths)}")
    return {"secondary": [_path(path, field=field) for path in paths]}


def _format_options(item: dict[str, Any], field_format: str, *, where: str,
                    path: Callable[..., list[str]]) -> dict[str, Any]:
    """The options one field format takes beside its value, each only on the formats it belongs to."""
    def only(option: str, formats: tuple[str, ...]) -> bool:
        if option not in item:
            return False
        if field_format not in formats:
            raise ValueError(f"{where} {option} needs format {' or '.join(formats)}, not {_shown(field_format)}")
        return True

    options: dict[str, Any] = {}
    # A share is stored as 0–1 or as 0–100; the page says which, never guessed from the value.
    scale = item.get("scale")
    if field_format == "percent" and (isinstance(scale, bool) or scale not in _PERCENT_SCALES):
        raise ValueError(f"{where} format percent takes scale 1 (stored as 0–1) or 100 (stored as 0–100), "
                         f"not {_shown(item.get('scale'))}")
    if only("scale", ("percent",)):
        options["scale"] = scale
    # A range reads from its field (the start) to its end_field.
    if field_format == "date_range" and "end_field" not in item:
        raise ValueError(f"{where} format date_range takes end_field, the path of the date it ends on")
    if only("end_field", ("date_range",)):
        options["end_field"] = path(item["end_field"], field=f"{where} end_field")
    if only("relative", ("date",)):
        if type(item["relative"]) is not bool:
            raise ValueError(f"{where} relative must be boolean, not {_shown(item['relative'])}")
        if item["relative"]:
            options["relative"] = True
    if only("max", ("progress",)):
        bound = item["max"]
        if isinstance(bound, bool) or not isinstance(bound, (int, float)) or not 0 < bound <= 1_000_000_000:
            raise ValueError(f"{where} max must be a positive number, not {_shown(bound)}")
        options["max"] = bound
    if only("max_field", ("progress",)):
        if "max" in options:
            raise ValueError(f"{where} takes max or max_field, not both")
        options["max_field"] = path(item["max_field"], field=f"{where} max_field")
    if only("unit", ("number",)):
        options["unit"] = _short_text(item["unit"], field=f"{where} unit", limit=12, required=True)
    if only("copy", _COPYABLE_FIELD_FORMATS):
        if type(item["copy"]) is not bool:
            raise ValueError(f"{where} copy must be boolean, not {_shown(item['copy'])}")
        if item["copy"]:
            options["copy"] = True
    # A number drawn as a bar in its cell, as long as its share of the column's largest.
    if only("bar", NUMERIC_FIELD_FORMATS):
        if type(item["bar"]) is not bool:
            raise ValueError(f"{where} bar must be boolean, not {_shown(item['bar'])}")
        if item["bar"]:
            options["bar"] = True
    if only("aspect", ("image", "images", "video", "clips")):
        if item["aspect"] not in MEDIA_ASPECTS:
            raise ValueError(f"{where} aspect must be one of {', '.join(MEDIA_ASPECTS)}, not {_shown(item['aspect'])}")
        options["aspect"] = item["aspect"]
    if only("layout", ("images",)):
        if item["layout"] not in _IMAGES_LAYOUTS:
            raise ValueError(f"{where} layout must be one of {', '.join(_IMAGES_LAYOUTS)}, not {_shown(item['layout'])}")
        if item["layout"] != "strip":
            options["layout"] = item["layout"]
    if only("transparent", ("image", "images")):
        if type(item["transparent"]) is not bool:
            raise ValueError(f"{where} transparent must be boolean, not {_shown(item['transparent'])}")
        if item["transparent"]:
            options["transparent"] = True
    # What a picture or clip is shown against or with: the reference, captions, its words, its chapters.
    for option, formats in (("compare_field", ("image", "video")), ("captions_field", ("video",)),
                            ("transcript_field", ("audio", "video")), ("chapters_field", ("audio", "video"))):
        if only(option, formats):
            options[option] = path(item[option], field=f"{where} {option}")
    if "compare" in item:
        if "compare_field" not in item:
            raise ValueError(f"{where} compare needs compare_field, the picture or clip it is compared with")
        mode = item["compare"]
        if mode not in _COMPARE_MODES or (mode == "slider" and field_format != "image"):
            raise ValueError(f"{where} compare is slider (pictures only) or side_by_side, not {_shown(mode)} on {field_format}")
        options["compare"] = mode
    elif "compare_field" in options:
        options["compare"] = "slider" if field_format == "image" else "side_by_side"
    return options


def _normalize_block_media(value: Any) -> dict[str, Any]:
    """A gallery's presentation of its files: layout, frame, version groups and what each file offers."""
    if not isinstance(value, dict) or not value or set(value) - set(_MEDIA_OPTIONS):
        raise ValueError(f"media takes {', '.join(_MEDIA_OPTIONS)}, not {_shown(value)}")
    media: dict[str, Any] = {}
    for option, allowed in (("layout", _MEDIA_LAYOUTS), ("aspect", MEDIA_ASPECTS), ("group", _MEDIA_GROUPS)):
        if option in value:
            if value[option] not in allowed:
                raise ValueError(f"media.{option} must be one of {', '.join(allowed)}, not {_shown(value[option])}")
            media[option] = value[option]
    for flag in _MEDIA_FLAGS:
        if flag in value:
            if type(value[flag]) is not bool:
                raise ValueError(f"media.{flag} must be boolean, not {_shown(value[flag])}")
            if value[flag]:
                media[flag] = True
    return media


# The names Workflows and people give the platforms, each read as its frame.
_PREVIEW_PLATFORM_NAMES = {
    "ig": "instagram", "instagram_feed": "instagram", "instagram_post": "instagram", "instagram_carousel": "instagram",
    "reel": "instagram_reels", "reels": "instagram_reels", "instagram_reel": "instagram_reels",
    "story": "instagram_story", "stories": "instagram_story", "instagram_stories": "instagram_story",
    "xhs": "xiaohongshu", "rednote": "xiaohongshu", "red_note": "xiaohongshu", "小红书": "xiaohongshu",
    "tik_tok": "tiktok", "抖音": "douyin", "twitter": "x", "x_twitter": "x", "fb": "facebook",
    "facebook_page": "facebook", "linked_in": "linkedin", "领英": "linkedin", "yt": "youtube",
    "shorts": "youtube_shorts", "youtube_short": "youtube_shorts", "wechat": "wechat_article",
    "weixin": "wechat_article", "wechat_official_account": "wechat_article", "wechat_oa": "wechat_article",
    "公众号": "wechat_article", "微信公众号": "wechat_article", "moments": "wechat_moments",
    "朋友圈": "wechat_moments", "微信朋友圈": "wechat_moments", "pin": "pinterest",
}


def preview_platform(value: Any) -> str | None:
    """The preview frame a stored platform name stands for (``Twitter`` is x, ``小红书`` xiaohongshu), else None."""
    if not isinstance(value, str) or not value.strip():
        return None
    key = re.sub(r"[\s./-]+", "_", value.strip().lower()).strip("_")
    return key if key in PREVIEW_PLATFORMS else _PREVIEW_PLATFORM_NAMES.get(key)


def _normalize_block_preview(value: Any, btype: str) -> dict[str, Any]:
    """How a block's records show as posts: the platform frame, the paths each part reads, and the layout."""
    if btype not in _PREVIEW_BLOCK_TYPES:
        raise ValueError(f"preview needs a {', '.join(_PREVIEW_BLOCK_TYPES)} block, not {btype}")
    if not isinstance(value, dict) or set(value) - set(_PREVIEW_OPTIONS):
        raise ValueError(f"preview takes {', '.join(_PREVIEW_OPTIONS)}, not {_shown(value)}")
    raw_fields = value.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields or set(raw_fields) - set(PREVIEW_FIELDS):
        raise ValueError(f"preview.fields maps parts of the post ({', '.join(PREVIEW_FIELDS)}) to the paths that hold "
                         f"them, not {_shown(raw_fields)}")
    path_of = _result_path if btype == "gallery" else _path
    fields = {role: path_of(raw_fields[role], field=f"preview.fields.{role}") for role in PREVIEW_FIELDS if role in raw_fields}
    preview: dict[str, Any] = {}
    platform = value.get("platform")
    if platform is not None:
        if platform not in PREVIEW_PLATFORMS:
            raise ValueError(f"preview.platform must be one of {', '.join(PREVIEW_PLATFORMS)}, not {_shown(platform)}")
        preview["platform"] = platform
    elif "platform" not in fields:
        raise ValueError("preview needs platform (the frame every post shows in) or fields.platform (each record's own "
                         "platform, one row per platform version)")
    if btype != "gallery" and not set(fields) & set(_PREVIEW_CONTENT):
        raise ValueError(f"preview.fields needs what the post shows: at least one of {', '.join(_PREVIEW_CONTENT)}")
    preview["fields"] = fields
    if "aspect" in value:
        if value["aspect"] not in MEDIA_ASPECTS:
            raise ValueError(f"preview.aspect must be one of {', '.join(MEDIA_ASPECTS)}, not {_shown(value['aspect'])}")
        preview["aspect"] = value["aspect"]
    layout = value.get("layout", "feed")
    if layout not in _PREVIEW_LAYOUTS:
        raise ValueError(f"preview.layout must be one of {', '.join(_PREVIEW_LAYOUTS)}, not {_shown(layout)}")
    if layout != "feed":
        preview["layout"] = layout
    if "group_field" in value:
        if layout != "side_by_side":
            raise ValueError("preview.group_field needs layout side_by_side: it puts each group's posts in one row")
        preview["group_field"] = path_of(value["group_field"], field="preview.group_field")
    return preview


def preview_paths(block: dict[str, Any]) -> list[list[str]]:
    """Every path a normalized block's preview reads."""
    preview = block.get("preview") or {}
    return [*preview.get("fields", {}).values(), *([preview["group_field"]] if "group_field" in preview else [])]


def _readable(sources: "_CheckedSources", name: Any) -> Callable[[Any, str], list[str]]:
    """A path option of a block: a field path its source reads (unchecked while the source itself is broken)."""
    fields = _record_fields(sources, name)

    def read(value: Any, where: str) -> list[str]:
        path = _path(value, field=where)
        if name not in sources.broken and path not in fields:
            raise ValueError(f"{where} reads {'.'.join(path)}, which its source does not read: add it to the source's fields")
        return path
    return read


def _normalize_collection(block: dict[str, Any], btype: str, view: Any, sources: "_CheckedSources", name: Any,
                          fields: list[dict[str, Any]] | None) -> dict[str, Any]:
    """How a progress block's records are grouped, picked, exported, totalled and laid out in time, place or a tree."""
    own = [option for option in ("group_by", "select", "export", "totals", "timeline", "places", "tree") if option in block]
    if not own:
        if view in {"timeline", "gantt", "places", "tree"}:
            needed = "timeline" if view in {"timeline", "gantt"} else view
            raise ValueError(f"view {view} needs {needed}: " + {
                "timeline": "{date_field, end_field, depends_field}", "places": "{address_field, name_field, lat_field, lng_field}",
                "tree": "{children: [the related_projects sources under it]}"}[needed])
        return {}
    if btype != "progress":
        raise ValueError(f"{', '.join(own)} {'needs' if len(own) == 1 else 'need'} a progress block, not {btype}")
    read = _readable(sources, name)
    shown = view or "list"
    out: dict[str, Any] = {}
    group_by = block.get("group_by")
    if group_by is not None:
        if shown not in _GROUP_VIEWS:
            raise ValueError(f"group_by needs view {', '.join(_GROUP_VIEWS)}, not {shown}")
        if not isinstance(group_by, list) or not 1 <= len(group_by) <= 3:
            raise ValueError(f"group_by is a list of 1–3 groupings a person picks from, each {{field, label}} or "
                             f"{{by: stage, label}}, not {_shown(group_by)}")
        out["group_by"] = []
        for index, item in enumerate(group_by):
            if not isinstance(item, dict) or set(item) - {"field", "by", "label"} or ("field" in item) == ("by" in item):
                raise ValueError(f"group_by[{index}] takes a field (a path the source reads) or by: stage, and a label, "
                                 f"not {_shown(item)}")
            if "by" in item and item["by"] != "stage":
                raise ValueError(f"group_by[{index}].by can only be stage, not {_shown(item['by'])}")
            out["group_by"].append({**({"by": "stage"} if "by" in item else {"field": read(item["field"], f"group_by[{index}].field")}),
                                    "label": _short_text(item.get("label"), field=f"group_by[{index}] label", limit=40, required=True)})
    for flag in ("select", "export"):
        if flag in block:
            if type(block[flag]) is not bool:
                raise ValueError(f"{flag} must be boolean, not {_shown(block[flag])}")
            if block[flag]:
                out[flag] = True
    totals = block.get("totals")
    if totals is not None:
        if shown not in {"list", "table"}:
            raise ValueError(f"totals needs view list or table, not {shown}")
        if name not in sources.broken and not sources.is_type(name, "workflow_projects"):
            raise ValueError("totals need a progress block over workflow_projects: they add up every record, not one page")
        if not isinstance(totals, list) or not 1 <= len(totals) <= 6:
            raise ValueError(f"totals is a list of 1–6 {{field, agg}}, not {_shown(totals)}")
        numeric = {tuple(field["field"]): field["format"] for field in fields or []}
        out["totals"] = []
        for index, item in enumerate(totals):
            if not isinstance(item, dict) or set(item) != {"field", "agg"} or item.get("agg") not in _TOTAL_AGGREGATES:
                raise ValueError(f"totals[{index}] takes exactly field and agg ({', '.join(_TOTAL_AGGREGATES)}), not {_shown(item)}")
            path = _path(item["field"], field=f"totals[{index}].field")
            if numeric.get(tuple(path)) not in NUMERIC_FIELD_FORMATS:
                raise ValueError(f"totals[{index}] totals {'.'.join(path)}, which must be one of the block's fields with "
                                 f"format {', '.join(NUMERIC_FIELD_FORMATS)}")
            out["totals"].append({"field": path, "agg": item["agg"]})
    timeline = block.get("timeline")
    if (timeline is not None) != (shown in {"timeline", "gantt"}):
        raise ValueError(f"timeline belongs with view timeline or gantt, and each needs it; this block has view {shown}")
    if timeline is not None:
        if not isinstance(timeline, dict) or set(timeline) - {"date_field", "end_field", "depends_field"} or "date_field" not in timeline:
            raise ValueError(f"timeline takes date_field (when each starts or happens), end_field and depends_field, "
                             f"not {_shown(timeline)}")
        if shown == "gantt" and "end_field" not in timeline:
            raise ValueError("view gantt needs timeline.end_field, the date each bar ends on")
        if "depends_field" in timeline and shown != "gantt":
            raise ValueError("timeline.depends_field (the records each waits for) needs view gantt")
        out["timeline"] = {key: read(timeline[key], f"timeline.{key}") for key in ("date_field", "end_field", "depends_field")
                           if key in timeline}
    places = block.get("places")
    if (places is not None) != (shown == "places"):
        raise ValueError(f"places belongs with view places, which needs it; this block has view {shown}")
    if places is not None:
        if (not isinstance(places, dict) or set(places) - {"address_field", "name_field", "lat_field", "lng_field"}
                or "address_field" not in places or ("lat_field" in places) != ("lng_field" in places)):
            raise ValueError(f"places takes address_field (where each is), name_field, and lat_field with lng_field, "
                             f"not {_shown(places)}")
        out["places"] = {key: read(places[key], f"places.{key}") for key in ("address_field", "name_field", "lat_field", "lng_field")
                         if key in places}
    tree = block.get("tree")
    if (tree is not None) != (shown == "tree"):
        raise ValueError(f"tree belongs with view tree, which needs it; this block has view {shown}")
    if tree is not None:
        children = tree.get("children") if isinstance(tree, dict) and set(tree) == {"children"} else None
        if not isinstance(children, list) or not 1 <= len(children) <= 2 or any(not isinstance(child, str) for child in children):
            raise ValueError(f"tree takes children: the 1–2 related_projects sources under each record (episodes, then "
                             f"their shots), not {_shown(tree)}")
        parent = name
        for child in children:
            if child in sources.broken:
                continue
            if not sources.declared(child) or not sources.is_type(child, "related_projects") or sources.items[child]["parent"] != parent:
                raise ValueError(f"tree child {child!r} must be a related_projects source whose parent is {parent!r}")
            parent = child
        if name not in sources.broken and not sources.is_type(name, "workflow_projects", "related_projects"):
            raise ValueError("view tree needs a project source")
        out["tree"] = {"children": list(children)}
    return out


def _chart_dimension(value: Any, where: str, read: Callable[[Any, str], list[str]], *, stats: bool,
                     bucket: bool = True) -> dict[str, Any]:
    if stats:
        if not isinstance(value, dict) or set(value) != {"bucket"} or value["bucket"] not in CHART_TIME_BUCKETS:
            raise ValueError(f"{where} of a Stat chart is {{bucket: {'|'.join(CHART_TIME_BUCKETS)}}}: the time each value "
                             f"was read, not {_shown(value)}")
        return {"bucket": value["bucket"]}
    if not isinstance(value, dict) or set(value) - {"field", "bucket", "by"} or ("field" in value) == ("by" in value):
        raise ValueError(f"{where} takes a field (a path the source reads) with an optional bucket "
                         f"({', '.join(CHART_BUCKETS)}), or by: stage, not {_shown(value)}")
    if "by" in value:
        if value["by"] != "stage" or "bucket" in value:
            raise ValueError(f"{where}.by can only be stage, with no bucket, not {_shown(value)}")
        return {"by": "stage"}
    dimension: dict[str, Any] = {"field": read(value["field"], f"{where}.field")}
    if "bucket" in value:
        if not bucket or value["bucket"] not in CHART_BUCKETS:
            raise ValueError(f"{where}.bucket must be one of {', '.join(CHART_BUCKETS)}" if bucket
                             else f"{where} is a number, so it takes no bucket")
        dimension["bucket"] = value["bucket"]
    return dimension


def _normalize_chart(value: Any, sources: "_CheckedSources", name: Any) -> dict[str, Any]:
    """A chart block's picture: what it counts or adds up, along what, split by what, and how it reads."""
    if not isinstance(value, dict) or set(value) - set(_CHART_OPTIONS):
        raise ValueError(f"chart takes {', '.join(_CHART_OPTIONS)}, not {_shown(value)}")
    kind = value.get("kind")
    if kind not in CHART_KINDS:
        raise ValueError(f"chart.kind must be one of {', '.join(CHART_KINDS)}, not {_shown(kind)}")
    if name not in sources.broken and not sources.is_type(name, *_CHART_SOURCES):
        raise ValueError(f"a chart reads a {', '.join(_CHART_SOURCES)} source, not {sources.items[name]['type']}")
    stats = name not in sources.broken and sources.is_type(name, "workspace_stats")
    projects = name not in sources.broken and sources.is_type(name, "workflow_projects", "related_projects")
    fields_declared = bool(_record_fields(sources, name)) or projects
    plain = _readable(sources, name)
    read = plain if fields_declared else (lambda raw, where: _path(raw, field=where))
    chart: dict[str, Any] = {"kind": kind}
    if stats and kind not in {"line", "area", "bar", "stacked_bar", "stacked_area"}:
        raise ValueError(f"a Stat chart is a line, area, bar, stacked_bar or stacked_area over time, not {kind}")
    for part in ("x", "series", "y"):
        if part in value and value[part] is not None:
            if stats and part != "x":
                raise ValueError(f"a Stat chart has a series per Stat already, so it takes no chart.{part}")
            dimension = _chart_dimension(value[part], f"chart.{part}", read, stats=stats, bucket=part != "y" and kind != "scatter")
            if "by" in dimension and not projects:
                raise ValueError(f"chart.{part} by stage needs a project source")
            chart[part] = dimension
    if stats:
        chart.setdefault("x", {"bucket": "day"})
    x, series = chart.get("x"), chart.get("series")
    time_x = bool(x) and x.get("bucket") in CHART_TIME_BUCKETS
    if kind in {"line", "area", "stacked_area"} and not time_x:
        raise ValueError(f"a {kind} chart runs over time: give chart.x a date field with bucket "
                         f"{', '.join(CHART_TIME_BUCKETS)}")
    if kind in {"bar", "stacked_bar", "donut", "funnel", "heatmap", "scatter"} and not x:
        raise ValueError(f"a {kind} chart needs chart.x: what its {'points' if kind == 'scatter' else 'parts'} are")
    if kind in {"stacked_bar", "stacked_area", "heatmap"} and not series and not stats:
        raise ValueError(f"a {kind} chart needs chart.series: what each {'row' if kind == 'heatmap' else 'layer'} is")
    if kind in {"donut", "funnel", "leaderboard"} and series:
        raise ValueError(f"a {kind} chart shows one series, so it takes no chart.series")
    if ("y" in chart) != (kind == "scatter"):
        raise ValueError("chart.y is a scatter's second number, and a scatter needs it")
    raw_value = value.get("value")
    if kind == "scatter":
        if raw_value is not None:
            raise ValueError("a scatter plots each record at its x and y, so it takes no chart.value")
    else:
        aggregates = _CHART_STAT_AGGREGATES if stats else CHART_AGGREGATES
        default = {"agg": "last"} if stats else {"agg": "count"}
        raw_value = default if raw_value is None else raw_value
        if kind == "leaderboard" and not x:
            # Each record by its own number: the best-selling dish, the best post.
            if not isinstance(raw_value, dict) or set(raw_value) != {"field"}:
                raise ValueError("a leaderboard without chart.x ranks each record by its own number: chart.value is "
                                 f"{{field}}, not {_shown(raw_value)}")
            chart["value"] = {"field": read(raw_value["field"], "chart.value.field")}
        else:
            if (not isinstance(raw_value, dict) or set(raw_value) - {"agg", "field"} or raw_value.get("agg", default["agg"]) not in aggregates
                    or (stats and "field" in raw_value)):
                raise ValueError(f"chart.value takes agg ({', '.join(aggregates)})" + ("" if stats else " and the field it adds up")
                                 + f", not {_shown(raw_value)}")
            agg = raw_value.get("agg", default["agg"])
            if not stats and (agg == "count") == ("field" in raw_value):
                raise ValueError("chart.value count counts records and takes no field; sum, avg, min and max need the field")
            chart["value"] = {"agg": agg, **({"field": read(raw_value["field"], "chart.value.field")} if "field" in raw_value else {})}
    if kind == "funnel" and "order" not in value and not (x.get("by") == "stage" and name not in sources.broken
                                                          and sources.items[name].get("stages")):
        raise ValueError("a funnel needs its steps in order: chart.order, or x by stage over a source that lists its stages")
    chart_format = value.get("format", "number")
    if chart_format not in CHART_FORMATS:
        raise ValueError(f"chart.format must be one of {', '.join(CHART_FORMATS)}, not {_shown(chart_format)}")
    chart["format"] = chart_format
    currency = value.get("currency")
    if (currency is None) != (chart_format != "money") or (
            currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency))):
        raise ValueError(f"chart.format money takes a currency (a 3-letter code such as CNY), and only money does, not {_shown(currency)}")
    if currency:
        chart["currency"] = currency
    if chart_format == "percent":
        scale = value.get("scale")
        if isinstance(scale, bool) or scale not in _PERCENT_SCALES:
            raise ValueError(f"chart.format percent takes scale 1 (stored as 0–1) or 100 (stored as 0–100), not {_shown(scale)}")
        chart["scale"] = scale
    elif "scale" in value:
        raise ValueError("chart.scale needs format percent")
    if "unit" in value:
        if chart_format != "number":
            raise ValueError("chart.unit needs format number")
        chart["unit"] = _short_text(value["unit"], field="chart.unit", limit=12, required=True)
    labels = value.get("labels")
    if labels is not None:
        if (not isinstance(labels, dict) or not labels or len(labels) > 48
                or any(not isinstance(key, str) or not isinstance(text, str) or not key.strip() or not text.strip()
                       or len(text.strip()) > 80 for key, text in labels.items())):
            raise ValueError(f"chart.labels maps at most 48 stored values to their names (1–80 characters), not {_shown(labels)}")
        chart["labels"] = {key.strip(): text.strip() for key, text in labels.items()}
    order = value.get("order")
    if order is not None:
        if (not isinstance(order, list) or not 1 <= len(order) <= 48 or len(set(map(str, order))) != len(order)
                or any(not isinstance(item, str) or not item.strip() or len(item) > 80 for item in order)):
            raise ValueError(f"chart.order lists 1–48 distinct stored values in the order they show, not {_shown(order)}")
        chart["order"] = [item.strip() for item in order]
    limit = value.get("limit")
    if limit is not None:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError(f"chart.limit is how many parts show, 1–50, not {_shown(limit)}")
        chart["limit"] = limit
    target = value.get("target")
    if target is not None:
        if kind not in _CHART_TARGET_KINDS:
            raise ValueError(f"chart.target is a line across a {', '.join(_CHART_TARGET_KINDS)} chart, not a {kind}")
        if (not isinstance(target, dict) or set(target) - {"value", "goal", "label"} or ("value" in target) == ("goal" in target)
                or ("value" in target and (isinstance(target["value"], bool) or not isinstance(target["value"], (int, float))))
                or ("goal" in target and (not isinstance(target["goal"], str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", target["goal"])))):
            raise ValueError(f"chart.target is {{value: a number}} or {{goal: a Goal's key}} (its target), with an optional "
                             f"label, not {_shown(target)}")
        chart["target"] = {key: target[key] for key in ("value", "goal") if key in target}
        if target.get("label") is not None:
            chart["target"]["label"] = _short_text(target["label"], field="chart.target label", limit=60)
    return chart


def chart_paths(chart: dict[str, Any]) -> list[list[str]]:
    """Every record path a normalized chart reads."""
    return [part["field"] for part in (chart.get("x"), chart.get("series"), chart.get("y"), chart.get("value"))
            if isinstance(part, dict) and "field" in part]


def app_data_project_sources(sources: dict[str, Any], name: str) -> list[str]:
    """Declared project owner and ancestors of a record, including project rows."""
    source = sources[name]
    current = source["parent"] if source["type"] == "project_records" else name
    result: list[str] = []
    while current not in result:
        source = sources[current]
        if source["type"] not in {"workflow_projects", "related_projects"}:
            return []
        result.append(current)
        if source["type"] == "workflow_projects":
            return result
        current = source["parent"]
    return []


def _normalize_data_source(raw: dict[str, Any], sources: dict[str, Any], found: list[str]) -> dict[str, Any]:
    """One source. An unsupported field goes on ``found`` and the rest is still read, so what else is wrong shows too."""
    source_type = raw.get("type")
    if not isinstance(source_type, str) or source_type not in _DATA_SOURCE_TYPES:
        raise ValueError(f"contains an unsupported source type {_shown(source_type)} in type: "
                         f"it must be one of {', '.join(sorted(_DATA_SOURCE_TYPES))}")
    takes = _DATA_SOURCE_FIELDS[source_type]
    unknown = set(raw) - {"type", *takes["required"], *takes["optional"]}
    if unknown:
        found.append(f"contains unsupported fields {_names(unknown)}: a {source_type} source requires "
                     f"{', '.join(takes['required'])} and also takes {', '.join(takes['optional'])}")
    item: dict[str, Any] = {"type": source_type}
    if source_type in {"workflow_projects", "related_projects"}:
        item["project_type"] = _short_text(raw.get("project_type"), field="project_type", limit=80, required=True)
        versions = raw.get("schema_versions", [1])
        if not isinstance(versions, list) or not versions or len(versions) > 16 or any(type(v) is not int or not 1 <= v <= 999 for v in versions):
            raise ValueError(f"schema_versions must be 1–16 integers from 1 to 999, not {_shown(versions)}")
        item["schema_versions"] = sorted(set(versions))
        if source_type == "related_projects":
            parent = raw.get("parent")
            if not isinstance(parent, str) or parent not in sources:
                raise ValueError(f"related_projects requires a declared parent source: parent is {_shown(parent)}, "
                                 f"the page declares {_names(sources)}")
            item["parent"] = parent
            item["parent_state_path"] = _path(raw.get("parent_state_path", ["parent_project_id"]), field="parent_state_path")
        else:
            launch = raw.get("launch_workflows", [])
            if not isinstance(launch, list) or len(launch) > 16:
                raise ValueError("launch_workflows must be a list of at most 16 entries")
            item["launch_workflows"] = []
            for entry in launch:
                if not isinstance(entry, dict) or set(entry) - {"workflow", "project_steps"}:
                    raise ValueError(f"launch_workflows entry {_shown(entry)} takes only workflow and project_steps")
                workflow = entry.get("workflow")
                steps = entry.get("project_steps")
                if not isinstance(workflow, str) or not workflow.startswith("workflow:") or not isinstance(steps, list) or not steps or len(steps) > 32 or any(not isinstance(step, str) or not _DATA_KEY.fullmatch(step) for step in steps):
                    raise ValueError("launch_workflows requires a workflow reference (workflow:<id>) and project_steps, "
                                     f"1–32 step ids, not {_shown(entry)}")
                item["launch_workflows"].append({"workflow": workflow, "project_steps": list(steps)})
        if "execution_run_id_path" in raw:
            item["execution_run_id_path"] = _path(raw["execution_run_id_path"], field="execution_run_id_path")
    if source_type in {"project_records", "project_documents", "publication_stats", "owned_automations",
                       "project_files", "project_references"}:
        parent = raw.get("parent")
        if not isinstance(parent, str) or parent not in sources:
            raise ValueError(f"{source_type} requires a declared parent source: parent is {_shown(parent)}, "
                             f"the page declares {_names(sources)}")
        item["parent"] = parent
    if source_type == "project_records":
        item["record_path"] = _path(raw.get("record_path"), field="record_path")
        # A versioned record (a publication receipt) names its schema; a plain
        # list kept in the project (a series' episode plan) names none.
        record_schema = _short_text(raw.get("record_schema"), field="record_schema", limit=120)
        if record_schema is not None:
            item["record_schema"] = record_schema
    if source_type == "project_files" and "results_only" in raw:
        # Left out, a project's finished files show: the ones its state names
        # (``display.deliverables``, else every Document id it holds), or with
        # none named, every file but those whose artifact role marks how it was
        # made. true keeps only the latter rule; false lists the whole folder.
        if type(raw["results_only"]) is not bool:
            raise ValueError(f"results_only must be boolean, not {_shown(raw['results_only'])}")
        item["results_only"] = raw["results_only"]
    if source_type == "project_references":
        kind = raw.get("reference_kind", "voices")
        if not isinstance(kind, str) or not _DATA_KEY.fullmatch(kind):
            raise ValueError(f"reference_kind must be an identifier, not {_shown(kind)}")
        item["reference_kind"] = kind
    if source_type == "project_documents":
        item["document_path"] = _path(raw.get("document_path"), field="document_path")
        item["document_role"] = _short_text(raw.get("document_role"), field="document_role", limit=80)
    if source_type == "publication_stats":
        subject = raw.get("subject")
        if not isinstance(subject, str) or subject not in sources:
            raise ValueError(f"publication_stats requires a declared subject source: subject is {_shown(subject)}, "
                             f"the page declares {_names(sources)}")
        metric_keys = raw.get("metric_keys", [])
        if not isinstance(metric_keys, list) or not metric_keys or len(metric_keys) > 20 or any(not isinstance(metric, str) or not _DATA_KEY.fullmatch(metric) for metric in metric_keys):
            raise ValueError(f"publication_stats requires metric_keys, 1–20 identifiers, not {_shown(metric_keys)}")
        item.update(subject=subject, metric_keys=list(dict.fromkeys(metric_keys)))
        # Publication metrics may come from the automatic public-post
        # collector or from evidence-backed manual assessments bound to the
        # same receipt identity. Omitted keeps the automatic collector only.
        if "collector_types" in raw:
            collector_types = raw["collector_types"]
            if (not isinstance(collector_types, list) or not collector_types
                    or any(value not in PUBLICATION_STAT_COLLECTOR_TYPES for value in collector_types)):
                raise ValueError(f"collector_types must list public_post and/or manual, not {_shown(collector_types)}")
            item["collector_types"] = list(dict.fromkeys(collector_types))
    if source_type == "workspace_stats":
        stat_keys = raw.get("stat_keys", [])
        if not isinstance(stat_keys, list) or not stat_keys or len(stat_keys) > 50 or any(not isinstance(stat, str) or not 1 <= len(stat) <= 120 for stat in stat_keys):
            raise ValueError(f"workspace_stats requires stat_keys, 1–50 Stat keys, not {_shown(stat_keys)}")
        item["stat_keys"] = list(dict.fromkeys(stat_keys))
    fields = raw.get("fields", [])
    if not isinstance(fields, list) or len(fields) > 24:
        raise ValueError(f"fields must be a list of at most 24 field paths, not {_shown(fields)}")
    path_of = _result_path if source_type == "workflow_results" else _path
    item["fields"] = [path_of(path, field="fields entry") for path in fields]
    if source_type in {"workflow_projects", "related_projects"}:
        # ``stages`` lists only those stages; ``exclude_stages`` leaves some out (an ended
        # or cancelled project kept for its history but hidden from the list).
        for stage_key in ("stages", "exclude_stages"):
            stages = raw.get(stage_key)
            if stages is None:
                continue
            if (not isinstance(stages, list) or not stages or len(stages) > 32
                    or any(not isinstance(stage, str) or not stage.strip() or len(stage) > 80 for stage in stages)):
                raise ValueError(f"{stage_key} must be a list of 1–32 stage names, not {_shown(stages)}")
            item[stage_key] = list(dict.fromkeys(stage.strip() for stage in stages))
        order = raw.get("order")
        if order is not None:
            if not isinstance(order, dict) or set(order) != {"field", "type", "direction"}:
                raise ValueError(f"order must have exactly field, type and direction, not {_shown(order)}")
            field = _path(order.get("field"), field="order.field")
            if field not in item["fields"] or order.get("type") != "datetime" or order.get("direction") not in ("asc", "desc"):
                raise ValueError("order requires a field listed in fields, type datetime and direction asc or desc, "
                                 f"not {_shown(order)}")
            item["order"] = {"field": field, "type": "datetime", "direction": order["direction"]}
    if "label_fields" in raw:
        label_fields = raw["label_fields"]
        if not isinstance(label_fields, list) or not 1 <= len(label_fields) <= 3:
            raise ValueError(f"label_fields must contain 1–3 field paths, not {_shown(label_fields)}")
        item["label_fields"] = [(_result_path if source_type == "workflow_results" else _path)(
            path, field="label_fields entry") for path in label_fields]
        undeclared = [path for path in item["label_fields"] if path not in item["fields"]]
        if undeclared:
            raise ValueError(f"label_fields must be declared in fields: add {_dotted(undeclared)} to fields")
    producers = raw.get("producer_workflows", [])
    if not isinstance(producers, list) or len(producers) > 32 or any(not isinstance(ref, str) or not ref.startswith("workflow_definition:") for ref in producers):
        raise ValueError("producer_workflows must contain workflow definition references (workflow_definition:<id>), "
                         f"not {_shown(producers)}")
    if source_type in {"workflow_projects", "related_projects", "workflow_results"} and not producers:
        raise ValueError(f"{source_type} data sources require producer_workflows: "
                         "a list of workflow_definition:<id> references")
    if source_type == "workflow_results":
        # The files a result is: each a run-variable path to a Knowledge document id.
        media = raw.get("media", [])
        if not isinstance(media, list) or len(media) > 2:
            raise ValueError(f"media must be at most 2 entries, not {_shown(media)}")
        item["media"] = []
        for entry in media:
            if not isinstance(entry, dict) or set(entry) - {"field", "label"}:
                raise ValueError(f"media entries have a field and an optional label, not {_shown(entry)}")
            label = _short_text(entry.get("label"), field="media label", limit=80)
            item["media"].append({"field": _result_path(entry.get("field"), field="media field"),
                                  **({"label": label} if label else {})})
        # true keeps each result's earlier finished versions beside its latest (v1, v2, … the one shown).
        if "versions" in raw:
            if type(raw["versions"]) is not bool:
                raise ValueError(f"versions must be boolean, not {_shown(raw['versions'])}")
            if raw["versions"]:
                item["versions"] = True
    item["producer_workflows"] = list(dict.fromkeys(producers))
    return item


# A gallery's file action also gives the player's moment (a cover frame) or the span picked on it (a ringtone cut).
_MEDIA_ACTION_INPUTS = ("time_input", "start_input", "end_input")


def _card_action(field: str, value: Any, *, record_fields: list[list[str]] | None = None,
                 media: bool = False) -> dict[str, Any]:
    """One button on a card. On one record's card (``record_fields`` given) it may open its form already
    filled with the record's values (``prefill``: Flow input -> a field the source reads). On a gallery's
    file (``media``) its ``input`` gets that file, and ``time_input`` (or ``start_input`` with
    ``end_input``) the seconds the person picked on its player."""
    allowed = {"action", "label", "input", "stages", "input_from", "confirm", *(("prefill",) if record_fields is not None else ()),
               *(_MEDIA_ACTION_INPUTS if media else ())}
    if (not isinstance(value, dict) or set(value) - allowed):
        raise ValueError(f"{field} takes {', '.join(sorted(allowed))}, not {_shown(value)}")
    action_key, action_input = value.get("action"), value.get("input")
    stages = value.get("stages", [])
    # "parent" fills the input with the item's parent project (a scheduled item's campaign), not the item itself.
    input_from = value.get("input_from", "item")
    if (not isinstance(action_key, str) or not _ACTION_KEY.fullmatch(action_key)
            or not isinstance(action_input, str) or not _DATA_KEY.fullmatch(action_input)
            or not isinstance(stages, list) or len(stages) > 32
            or input_from not in {"item", "parent"}
            or any(not isinstance(stage, str) or not stage.strip() or len(stage) > 80 for stage in stages)):
        raise ValueError(f"{field} needs an action (an entry key), an input (the Flow input it fills), "
                         f"input_from (item or parent), and at most 32 stage names in stages, not {_shown(value)}")
    # A confirm text makes the button ask first; the action then runs in place (no input form) once confirmed.
    confirm = value.get("confirm")
    if confirm is not None and (not isinstance(confirm, str) or not confirm.strip() or len(confirm) > 300):
        raise ValueError(f"{field} confirm is a sentence of at most 300 characters, not {_shown(confirm)}")
    prefill = value.get("prefill") or {}
    if not isinstance(prefill, dict) or len(prefill) > 12 or action_input in prefill or any(
            not isinstance(name, str) or not _DATA_KEY.fullmatch(name) for name in prefill):
        raise ValueError(f"{field}.prefill maps at most 12 Flow inputs, other than input, to fields, not {_shown(prefill)}")
    prefill = {name: _path(path, field=f"{field}.prefill.{name}") for name, path in prefill.items()}
    unread = sorted(name for name, path in prefill.items() if path not in (record_fields or []))
    if unread:
        raise ValueError(f"{field}.prefill fills {', '.join(unread)} from fields its source does not read: "
                         "add them to the source's fields")
    moments = {option: value[option] for option in _MEDIA_ACTION_INPUTS if option in value}
    if (any(not isinstance(name, str) or not _DATA_KEY.fullmatch(name) or name == action_input for name in moments.values())
            or len(set(moments.values())) != len(moments) or ("start_input" in moments) != ("end_input" in moments)):
        raise ValueError(f"{field} time_input, or start_input with end_input, each name another Flow input "
                         f"(the seconds picked on the player), not {_shown(moments)}")
    if moments and confirm:
        raise ValueError(f"{field} picks a moment on the player, so it opens its form: drop confirm")
    return {"action": action_key, "input": action_input, **moments,
            "label": _short_text(value.get("label"), field=f"{field} label", limit=40, required=True),
            **({"stages": [stage.strip() for stage in stages]} if stages else {}),
            **({"input_from": input_from} if input_from != "item" else {}),
            **({"confirm": confirm.strip()} if confirm else {}),
            **({"prefill": prefill} if prefill else {})}


def block_card_actions(block: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Every button a normalized block puts on its cards or its header, with the option that holds it.

    Only ``add_action`` (the header's Add button) takes no input: it opens its entry as the entry's tile does."""
    return [*((option, block[option]) for option in ("item_action", "result_action", "plan_action", "add_action")
              if block.get(option)),
            *(("item_actions", action) for action in block.get("item_actions") or [])]


def _block_narrowing(block: dict[str, Any], btype: Any, sources: "_CheckedSources", name: Any
                     ) -> tuple[list[dict[str, Any]] | None, dict[str, Any] | None]:
    """A list people can narrow: up to three filters (a field the source reads, or the stage) and
    a search over up to four text fields. The values come from the records, never a query."""
    filters, search = block.get("filters"), block.get("search")
    if filters is None and search is None:
        return None, None
    source = sources.items.get(name) if isinstance(name, str) else None
    if btype != "progress" or (source or {}).get("type") != "workflow_projects":
        raise ValueError(f"filters and search need a progress block over workflow_projects, not {btype} over {_shown(name)}")
    readable = _record_fields(sources, name)
    narrowed: list[dict[str, Any]] | None = None
    if filters is not None:
        if not isinstance(filters, list) or not 1 <= len(filters) <= 3:
            raise ValueError(f"filters must be a list of 1–3 filters, not {_shown(filters)}")
        narrowed = []
        for index, item in enumerate(filters):
            if not isinstance(item, dict) or set(item) - {"field", "by", "label"} or ("field" in item) == ("by" in item):
                raise ValueError(f"filters[{index}] takes a field (a path the source reads) or by: stage, and a label, not {_shown(item)}")
            label = _short_text(item.get("label"), field=f"filters[{index}] label", limit=40, required=True)
            if "by" in item:
                if item["by"] != "stage":
                    raise ValueError(f"filters[{index}].by can only be stage, not {_shown(item['by'])}")
                narrowed.append({"by": "stage", "label": label})
                continue
            path = _path(item.get("field"), field=f"filters[{index}].field")
            if path not in readable:
                raise ValueError(f"filters[{index}] filters by {'.'.join(path)}, which its source does not read: add it to the source's fields")
            narrowed.append({"field": path, "label": label})
    searched: dict[str, Any] | None = None
    if search is not None:
        if not isinstance(search, dict) or set(search) - {"fields", "label"} or not isinstance(search.get("fields"), list) \
                or not 1 <= len(search["fields"]) <= 4:
            raise ValueError(f"search takes fields (1–4 paths the source reads) and an optional label, not {_shown(search)}")
        paths = [_path(path, field="search.fields") for path in search["fields"]]
        unread = [".".join(path) for path in paths if path not in readable]
        if unread:
            raise ValueError(f"search reads {', '.join(unread)}, which its source does not read: add them to the source's fields")
        searched = {"fields": paths, **({"label": _short_text(search["label"], field="search label", limit=60)}
                                        if search.get("label") else {})}
    return narrowed, searched


def _record_fields(sources: "_CheckedSources", name: Any) -> list[list[str]]:
    """The fields a block's source reads, which its card actions may fill a form from."""
    source = sources.items.get(name) if isinstance(name, str) else None
    return [path for path in (source or {}).get("fields") or [] if isinstance(path, list)]


def _normalize_block(block: Any, sources: _CheckedSources, block_keys: set[str], found: list[str]) -> dict[str, Any]:
    """One block. An unsupported field goes on ``found`` and the rest is still read."""
    if not isinstance(block, dict):
        raise ValueError(f"a block is an object, not {_shown(block)}")
    unknown = set(block) - set(_DATA_BLOCK_OPTIONS)
    if unknown:
        found.append(f"contains unsupported fields {_names(unknown)}: a block takes {', '.join(_DATA_BLOCK_OPTIONS)}")
    bkey, btype, bsource = block.get("key"), block.get("type"), block.get("source")
    known_type = isinstance(btype, str) and btype in _DATA_BLOCK_TYPES
    if not isinstance(bkey, str) or not _DATA_KEY.fullmatch(bkey) or bkey in block_keys or not known_type or not sources.declared(bsource):
        problems = []
        if not isinstance(bkey, str) or not _DATA_KEY.fullmatch(bkey) or bkey in block_keys:
            problems.append(f"key {bkey!r} must be a unique snake_case identifier")
        if not known_type:
            problems.append(f"type {btype!r} is not one of {', '.join(sorted(_DATA_BLOCK_TYPES))}")
        if not sources.declared(bsource):
            problems.append(f"source {bsource!r} is not a declared data_source (the page declares {_names(sources.names())})")
        raise ValueError("block references are invalid: " + "; ".join(problems))
    limit = block.get("limit", 20)
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError(f"limit must be 1–100, not {_shown(limit)}")
    metrics_source = block.get("metrics_source")
    if metrics_source is not None and (btype != "records" or not sources.declared(metrics_source)):
        raise ValueError(f"metrics_source must name a declared source on a records block; this {btype} block has {_shown(metrics_source)}")
    metrics_display = block.get("metrics_display")
    if metrics_display is not None and (metrics_source is None or not isinstance(metrics_display, str) or metrics_display not in {"declared", "configured", "zero"}):
        raise ValueError(f"metrics_display requires a metrics_source and one of declared, configured, zero, not {_shown(metrics_display)}")
    block_fields = block.get("fields", [])
    count_by_stage = block.get("count_by_stage", False)
    first = block.get("first")
    empty_message = _short_text(block.get("empty_message"), field="empty_message", limit=300)
    stage_labels = block.get("stage_labels")
    if stage_labels is not None:
        if btype not in {"project_rollup", "progress", "packs", "records", "chart"}:
            raise ValueError(f"stage_labels require a project_rollup, progress, packs, records or chart block, not {btype}")
        if not isinstance(stage_labels, dict) or len(stage_labels) > 32:
            raise ValueError(f"stage_labels must map at most 32 stages to their labels, not {_shown(stage_labels)}")
        normalized_stage_labels: dict[str, str] = {}
        for stage, label in stage_labels.items():
            if (not isinstance(stage, str) or not stage.strip() or len(stage.strip()) > 80
                    or not isinstance(label, str) or not label.strip() or len(label.strip()) > 80):
                raise ValueError(f"stage_labels entries must be strings of 1–80 characters, not {_shown(stage)}: {_shown(label)}")
            normalized_stage_labels[stage.strip()] = label.strip()
        if len(normalized_stage_labels) != len(stage_labels):
            raise ValueError("stage_labels must have unique stage names")
        stage_labels = normalized_stage_labels
    # A packs block can list a plan (project records) in its order,
    # each planned child a card before its run starts, and offer one
    # App action per card that opens with the card's project chosen.
    # A project_rollup counts the same plan as its total.
    plan = block.get("plan")
    if plan is not None and (btype not in {"packs", "project_rollup"} or not sources.declared(plan)
                             or not sources.is_type(plan, "project_records")):
        raise ValueError(f"plan of a packs or project_rollup block must name a project_records source; this {btype} block has {_shown(plan)}")

    item_action = block.get("item_action")
    if item_action is not None:
        # A pack, a scheduled item or a summarised project (a calendar's
        # open campaign) opens an App action with itself as the input.
        if btype not in {"packs", "progress", "summary", "gallery"} or not isinstance(item_action, dict):
            raise ValueError(f"item_action needs a packs, progress, summary or gallery block and an object, not {btype} and {_shown(item_action)}")
        # A pack card is made of files, so only one record's list or summary fills a form or asks first.
        # A gallery's opens with the file chosen (the picture voted for, the take kept).
        item_action = _card_action("item_action", item_action,
                                   record_fields=_record_fields(sources, bsource) if btype not in {"packs", "gallery"} else None,
                                   media=btype == "gallery")
    filters, search = _block_narrowing(block, btype, sources, bsource)
    block_media = block.get("media")
    if block_media is not None:
        if btype != "gallery":
            raise ValueError(f"media needs a gallery block, not {btype}")
        block_media = _normalize_block_media(block_media)
    block_preview = block.get("preview")
    if block_preview is not None:
        block_preview = _normalize_block_preview(block_preview, btype)
    view = block.get("view")
    if view is not None and (btype != "progress" or view not in _DATA_BLOCK_VIEWS):
        raise ValueError(f"view needs a progress block and one of {', '.join(_DATA_BLOCK_VIEWS)}, "
                         f"not {_shown(view)} on {btype}")
    # More buttons on the same card (edit this dish, take it off the menu), after item_action.
    item_actions = block.get("item_actions")
    if item_actions is not None:
        if btype not in {"progress", "summary"} or not isinstance(item_actions, list) or not 1 <= len(item_actions) <= 4:
            raise ValueError(f"item_actions needs a progress or summary block and a list of 1–4 actions, not {btype} and {_shown(item_actions)}")
        item_actions = [_card_action(f"item_actions[{index}]", action, record_fields=_record_fields(sources, bsource))
                        for index, action in enumerate(item_actions)]
        opened = [action["action"] for action in ([item_action] if item_action else []) + item_actions]
        if len(set(opened)) != len(opened):
            raise ValueError(f"item_action and item_actions each open a different entry, not {_shown(opened)}")
    # An action about a scheduled item's results (refresh this post's counts)
    # sits beside them as an icon and runs in place.
    result_action = block.get("result_action")
    if result_action is not None:
        if btype != "progress":
            raise ValueError(f"result_action needs a progress block, not {btype}")
        result_action = _card_action("result_action", result_action)
    # A planned card (no project yet) opens its own App action, its
    # inputs filled from the card: its parent project or its plan code.
    plan_action = block.get("plan_action")
    if plan_action is not None:
        if (btype != "packs" or plan is None or not isinstance(plan_action, dict)
                or set(plan_action) - {"action", "label", "inputs"}):
            raise ValueError(f"plan_action needs a packs block with a plan and takes action, label and inputs, not {_shown(plan_action)}")
        action_key, action_inputs = plan_action.get("action"), plan_action.get("inputs")
        if (not isinstance(action_key, str) or not _ACTION_KEY.fullmatch(action_key)
                or not isinstance(action_inputs, dict) or not 1 <= len(action_inputs) <= 4
                or any(not isinstance(key, str) or not _DATA_KEY.fullmatch(key) or value not in ("parent", "code")
                       for key, value in action_inputs.items())):
            raise ValueError(f"plan_action needs an action (an entry key) and 1–4 inputs, each from parent or code, not {_shown(plan_action)}")
        plan_action = {"action": action_key, "inputs": dict(action_inputs),
                       "label": _short_text(plan_action.get("label"), field="plan_action label", limit=40, required=True)}
    # A list of records people add by hand (a menu, a catalogue) offers its Add entry in its own
    # header: the button opens that entry exactly as its tile does, with an empty form.
    add_action = block.get("add_action")
    if add_action is not None:
        if (btype != "progress" or not sources.is_type(bsource, "workflow_projects")
                or not isinstance(add_action, dict) or set(add_action) - {"action", "label"}):
            raise ValueError(f"add_action needs a progress block over workflow_projects and takes action and label, "
                             f"not {_shown(add_action)} on a {btype} block")
        action_key = add_action.get("action")
        if not isinstance(action_key, str) or not _ACTION_KEY.fullmatch(action_key):
            raise ValueError(f"add_action needs an action (an entry key), not {_shown(action_key)}")
        add_action = {"action": action_key,
                      "label": _short_text(add_action.get("label"), field="add_action label", limit=40, required=True)}
    completion = block.get("completion")
    if completion is not None:
        if btype != "project_rollup":
            raise ValueError(f"completion requires a project_rollup block, not {btype}")
        if not isinstance(completion, dict) or set(completion) != {"stages", "label"}:
            raise ValueError(f"completion must have exactly stages and label, not {_shown(completion)}")
        completion_stages = completion.get("stages")
        if (not isinstance(completion_stages, list) or not completion_stages or len(completion_stages) > 32
                or any(not isinstance(stage, str) or not stage.strip() or len(stage.strip()) > 80
                       for stage in completion_stages)):
            raise ValueError(f"completion.stages must be 1–32 stage names, not {_shown(completion_stages)}")
        completion_stages = [stage.strip() for stage in completion_stages]
        if len(set(completion_stages)) != len(completion_stages):
            raise ValueError(f"completion stages must be unique, not {_shown(completion_stages)}")
        completion = {
            "stages": completion_stages,
            "label": _short_text(completion.get("label"), field="completion.label", limit=100, required=True),
        }
    if btype == "project_rollup":
        if not sources.is_type(bsource, "workflow_projects", "related_projects"):
            raise ValueError(f"project_rollup requires a project source: {bsource!r} is {sources.items[bsource]['type']}")
        if type(count_by_stage) is not bool:
            raise ValueError(f"project_rollup count_by_stage must be boolean, not {_shown(count_by_stage)}")
        if completion is not None and not count_by_stage:
            raise ValueError("completion requires count_by_stage: true")
        if first is not None:
            if (not isinstance(first, dict) or set(first) != {"stages"} or not isinstance(first.get("stages"), list)
                    or not first["stages"] or len(first["stages"]) > 32
                    or any(not isinstance(stage, str) or not stage.strip() or len(stage) > 80 for stage in first["stages"])):
                raise ValueError(f"project_rollup first must be {{stages: [1–32 stage names]}}, not {_shown(first)}")
            if bsource not in sources.broken and "order" not in sources.items[bsource]:
                raise ValueError(f"project_rollup first requires an ordered project source: give {bsource!r} an order")
            first = {"stages": list(dict.fromkeys(stage.strip() for stage in first["stages"]))}
        if not count_by_stage and first is None:
            raise ValueError("project_rollup requires count_by_stage: true or first")
    # Only a gallery shows what a Flow's runs made; a gallery also shows the
    # pictures projects and their records name.
    gallery_sources = ("workflow_results", "workflow_projects", "related_projects", "project_records")
    if bsource not in sources.broken and (
            (btype == "gallery" and sources.items[bsource]["type"] not in gallery_sources)
            or (btype != "gallery" and sources.items[bsource]["type"] == "workflow_results")):
        raise ValueError(f"gallery blocks read {', '.join(gallery_sources)} sources, and only a gallery reads workflow_results: "
                         f"this {btype} block reads {bsource!r}, a {sources.items[bsource]['type']} source")
    structured_fields = btype == "summary" or (
        btype in {"progress", "gallery", "records"} and isinstance(block_fields, list)
        and any(isinstance(field, dict) for field in block_fields)
    )
    if structured_fields:
        if (not isinstance(block_fields, list) or not block_fields or len(block_fields) > 12
                or any(not isinstance(item, dict) for item in block_fields)):
            raise ValueError(f"{btype} fields must be 1–12 field definitions, each "
                             f"{{field, label, format, hide_when_empty, ...}} (options: {', '.join(_DATA_FIELD_OPTIONS)}), "
                             f"not {_shown(block_fields)}")
        summary_fields = []
        for item in block_fields:
            unknown = set(item) - set(_DATA_FIELD_OPTIONS)
            if unknown:
                raise ValueError(f"{btype} field contains unsupported fields {_names(unknown)}: it takes {', '.join(_DATA_FIELD_OPTIONS)}")
            field_format = item.get("format", "text")
            if field_format not in _DATA_FIELD_FORMATS:
                raise ValueError(f"{btype} field format {_shown(field_format)} must be one of {', '.join(_DATA_FIELD_FORMATS)}")
            labels = item.get("labels", {})
            if (not isinstance(labels, dict) or len(labels) > 24
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           or not key.strip() or not value.strip() or len(value.strip()) > 80
                           for key, value in labels.items())):
                raise ValueError(f"{btype} field labels must map at most 24 values to text of 1–80 characters, not {_shown(labels)}")
            hide_when_empty = item.get("hide_when_empty", False)
            if type(hide_when_empty) is not bool:
                raise ValueError(f"{btype} field hide_when_empty must be boolean, not {_shown(hide_when_empty)}")
            # A price reads in its currency (CNY, USD), named once by the page, never guessed from the value.
            currency = item.get("currency")
            if (currency is None) != (field_format != "money") or (
                    currency is not None and (not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency))):
                raise ValueError(f"{btype} field format money takes a currency (a 3-letter code such as CNY), "
                                 f"and only money does, not {_shown(currency)} on {field_format}")
            if "project_type" in item and field_format != "record":
                raise ValueError(f"{btype} field project_type needs format record, not {_shown(field_format)}")
            record_type = _short_text(item.get("project_type"), field=f"{btype} field project_type", limit=80)
            field_path = _result_path if btype == "gallery" else _path
            summary_fields.append({
                "label": _short_text(item.get("label"), field=f"{btype} field label", limit=80, required=True),
                "field": field_path(item.get("field"), field=f"{btype} field path"),
                "format": field_format,
                "hide_when_empty": hide_when_empty,
                **({"labels": {key.strip(): value.strip() for key, value in labels.items()}} if labels else {}),
                **({"currency": currency} if currency else {}),
                **_secondary_paths(item, field=f"{btype} field secondary"),
                **({"project_type": record_type} if record_type else {}),
                **_format_options(item, field_format, where=f"{btype} field", path=field_path),
            })
    else:
        if not isinstance(block_fields, list):
            raise ValueError(f"fields must be a list, not {_shown(block_fields)}")
        summary_fields = None
    collection = _normalize_collection(block, btype, view, sources, bsource, summary_fields)
    chart = block.get("chart")
    if (chart is None) != (btype != "chart"):
        raise ValueError("a chart block needs chart: {kind, x, value, series, ...}, and only a chart block takes it")
    if chart is not None:
        chart = _normalize_chart(chart, sources, bsource)
    raw_display = block.get("record_display")
    if raw_display is not None and (btype != "records" or not isinstance(raw_display, dict)
                                    or set(raw_display) - {"title", "fields", "media"}):
        raise ValueError(f"record_display needs a records block and takes title, fields and media; this {btype} block has {_shown(raw_display)}")
    display: dict[str, Any] | None = None
    if raw_display is not None:
        display = {}
        title = raw_display.get("title")
        if title is not None:
            if not isinstance(title, dict) or set(title) != {"source", "field"} or not sources.declared(title.get("source")):
                raise ValueError(f"record_display.title must have exactly source (a declared source) and field, not {_shown(title)}")
            display["title"] = {"source": title["source"], "field": _path(title.get("field"), field="record_display.title.field")}
        raw_fields = raw_display.get("fields", [])
        if not isinstance(raw_fields, list) or len(raw_fields) > 12:
            raise ValueError(f"record_display.fields must be a list of at most 12 fields, not {_shown(raw_fields)}")
        display_fields = []
        for item in raw_fields:
            if not isinstance(item, dict) or set(item) - {"label", "source", "field", "labels", "format", "secondary"} or not sources.declared(item.get("source")):
                raise ValueError("record_display.fields entry takes label, source (a declared source), field, labels, "
                                 f"format and secondary, not {_shown(item)}")
            display_format = item.get("format", "text")
            if display_format not in {"text", "provider"}:
                raise ValueError(f"record_display.fields format must be text or provider, not {_shown(display_format)}")
            labels = item.get("labels", {})
            if (not isinstance(labels, dict) or len(labels) > 24
                    or any(not isinstance(key, str) or not isinstance(value, str)
                           or not key.strip() or not value.strip() or len(key) > 80 or len(value.strip()) > 80
                           for key, value in labels.items())):
                raise ValueError(f"record_display.fields labels must map at most 24 values to text of 1–80 characters, not {_shown(labels)}")
            display_fields.append({"label": _short_text(item.get("label"), field="record_display.fields label", limit=80, required=True),
                                   "source": item["source"], "field": _path(item.get("field"), field="record_display.fields field"),
                                   **({"labels": {key: value.strip() for key, value in labels.items()}} if labels else {}),
                                   **({"format": display_format} if display_format != "text" else {}),
                                   **_secondary_paths(item, field="record_display.fields secondary")})
        if display_fields:
            display["fields"] = display_fields
        raw_media = raw_display.get("media", [])
        if not isinstance(raw_media, list) or len(raw_media) > 4:
            raise ValueError(f"record_display.media must be a list of at most 4 entries, not {_shown(raw_media)}")
        media = []
        for entry in raw_media:
            if (not isinstance(entry, dict) or set(entry) - {"source", "label"}
                    or not sources.declared(entry.get("source"))
                    or not sources.is_type(entry["source"], "project_documents")
                    or any(item["source"] == entry["source"] for item in media)):
                raise ValueError(f"record_display.media requires unique document sources (project_documents), each {{source, label}}, not {_shown(entry)}")
            label = _short_text(entry.get("label"), field="record_display.media label", limit=80)
            media.append({"source": entry["source"], **({"label": label} if label else {})})
        if media:
            display["media"] = media
    normalized = {"key": bkey, "type": btype, "source": bsource,
                  "title": _short_text(block.get("title"), field="title", limit=100),
                  "fields": summary_fields if btype in {"summary", "progress", "gallery", "records"} and summary_fields is not None else [_path(path, field="fields entry") for path in block_fields],
                  "metric_keys": list(block.get("metric_keys", [])), "limit": limit,
                  **({"metrics_source": metrics_source} if metrics_source is not None else {}),
                  **({"metrics_display": metrics_display} if metrics_display is not None else {}),
                  **({"record_display": display} if display is not None else {}),
                  **({"empty_message": empty_message} if empty_message is not None else {}),
                  **({"stage_labels": stage_labels} if stage_labels is not None else {}),
                  **({"plan": plan} if plan is not None else {}),
                  **({"item_action": item_action} if item_action is not None else {}),
                  **({"item_actions": item_actions} if item_actions is not None else {}),
                  **({"filters": filters} if filters is not None else {}),
                  **({"search": search} if search is not None else {}),
                  **({"view": view} if view not in (None, "list") else {}),
                  **({"result_action": result_action} if result_action is not None else {}),
                  **({"plan_action": plan_action} if plan_action is not None else {}),
                  **({"add_action": add_action} if add_action is not None else {}),
                  **({"completion": completion} if completion is not None else {}),
                  **({"media": block_media} if block_media else {}),
                  **({"preview": block_preview} if block_preview else {}),
                  **collection,
                  **({"chart": chart} if chart is not None else {})}
    if btype == "project_rollup":
        normalized.update(count_by_stage=count_by_stage, **({"first": first} if first is not None else {}))
    return normalized


def _normalize_component_context(raw: dict[str, Any], sources: _CheckedSources) -> dict[str, Any]:
    context = raw.get("context")
    if not isinstance(context, dict):
        raise ValueError(f"context must be an object with a kind ({', '.join(sorted(_DATA_CONTEXT_KINDS))}), not {_shown(context)}")
    unknown = set(context) - set(_DATA_CONTEXT_OPTIONS)
    if unknown:
        raise ValueError(f"context contains unsupported fields {_names(unknown)}: it takes {', '.join(_DATA_CONTEXT_OPTIONS)}")
    if not isinstance(context.get("kind"), str) or context["kind"] not in _DATA_CONTEXT_KINDS:
        raise ValueError(f"context.kind must be one of {', '.join(sorted(_DATA_CONTEXT_KINDS))}, not {_shown(context.get('kind'))}")
    source = context.get("source")
    project_source_types = ("workflow_projects",) if context["kind"] == "launch_run" else ("workflow_projects", "related_projects")
    if context["kind"] in {"project", "launch_run"} and (not sources.declared(source) or not sources.is_type(source, *project_source_types)):
        raise ValueError(f"project and launch-run component contexts require a project source: context.source is "
                         f"{_shown(source)}, it must name a {' or '.join(project_source_types)} source")
    if source is not None and not sources.declared(source):
        raise ValueError(f"context references an unknown source {_shown(source)} (the page declares {_names(sources.names())})")
    context_display = context.get("display", "select")
    if context_display not in _DATA_CONTEXT_DISPLAYS or (context_display == "cards" and context["kind"] != "project"):
        raise ValueError(f"context.display must be select, or cards for projects, not {_shown(context_display)} on a {context['kind']} context")
    return context


def _normalize_component_display(raw: dict[str, Any], block_keys: set[str]) -> dict[str, Any]:
    display = raw.get("display", {})
    if not isinstance(display, dict):
        raise ValueError(f"display must be an object, not {_shown(display)}")
    unknown = set(display) - set(_DATA_DISPLAY_OPTIONS)
    if unknown:
        raise ValueError(f"display contains unsupported fields {_names(unknown)}: it takes {', '.join(_DATA_DISPLAY_OPTIONS)} "
                         "(tabs are layout: tabs with groups of {key, title, blocks})")
    display_layout = display.get("layout", "stack")
    if display_layout not in _DATA_DISPLAY_LAYOUTS:
        raise ValueError(f"display.layout must be one of {', '.join(_DATA_DISPLAY_LAYOUTS)}, not {_shown(display_layout)}")
    component_hidden = display.get("hidden", False)
    if type(component_hidden) is not bool:
        raise ValueError(f"display.hidden must be boolean, not {_shown(component_hidden)}")
    hidden_blocks = display.get("hidden_blocks", [])
    if (not isinstance(hidden_blocks, list) or len(hidden_blocks) > len(block_keys)
            or any(not isinstance(item, str) or not _DATA_KEY.fullmatch(item) for item in hidden_blocks)
            or len(set(hidden_blocks)) != len(hidden_blocks)
            or not set(hidden_blocks).issubset(block_keys)):
        raise ValueError(f"display.hidden_blocks must name distinct blocks of this component ({_names(block_keys)}), not {_shown(hidden_blocks)}")
    groups = display.get("groups", [])
    if not isinstance(groups, list) or len(groups) > 12:
        raise ValueError(f"display.groups must be a list of at most 12 groups, each {{key, title, blocks}}, not {_shown(groups)}")
    normalized_groups = []
    group_keys: set[str] = set()
    grouped_blocks: set[str] = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) != {"key", "title", "blocks"}:
            raise ValueError(f"display.groups entry must have exactly key, title and blocks, not {_shown(group)}")
        group_key = group.get("key")
        group_blocks = group.get("blocks")
        if (not isinstance(group_key, str) or not _DATA_KEY.fullmatch(group_key) or group_key in group_keys
                or not isinstance(group_blocks, list) or not group_blocks or len(group_blocks) > len(block_keys)
                or any(not isinstance(item, str) or not _DATA_KEY.fullmatch(item) for item in group_blocks)
                or len(set(group_blocks)) != len(group_blocks)
                or not set(group_blocks).issubset(block_keys)
                or grouped_blocks.intersection(group_blocks)):
            raise ValueError(f"display.groups entry {_shown(group_key)} needs a unique identifier as key and blocks "
                             f"naming this component's blocks ({_names(block_keys)}), each in one group, not {_shown(group_blocks)}")
        group_keys.add(group_key)
        grouped_blocks.update(group_blocks)
        normalized_groups.append({
            "key": group_key,
            "title": _short_text(group.get("title"), field="display.groups title", limit=100, required=True),
            "blocks": list(group_blocks),
        })
    if groups and grouped_blocks != block_keys:
        raise ValueError(f"display groups must cover every block exactly once: {_names(block_keys - grouped_blocks)} in no group")
    if display_layout == "tabs" and not groups:
        raise ValueError("display tabs require groups: layout tabs needs groups of {key, title, blocks}")
    return {"layout": display_layout, "groups": normalized_groups, "hidden": component_hidden, "hidden_blocks": list(hidden_blocks)}


def _normalize_component_links(
    raw: dict[str, Any], context: dict[str, Any], normalized_blocks: list[dict[str, Any]], sources: _CheckedSources,
) -> tuple[dict[str, Any] | None, dict[str, str], dict[str, Any] | None]:
    """What refers to the blocks' content: visibility, the cards' options and the calendar."""
    narrowed = [block["key"] for block in normalized_blocks if block.get("filters") or block.get("search")]
    if narrowed and context["kind"] != "workspace":
        raise ValueError(f"{', '.join(narrowed)} filters or searches a list, which only a workspace view lists: "
                         f"move it to a workspace data view, not a {context['kind']} one")
    visibility = raw.get("visibility")
    if visibility is not None:
        if (not isinstance(visibility, dict) or set(visibility) != {"block", "when"}
                or visibility.get("when") not in ("has_records", "has_observations") or context["kind"] != "project"):
            raise ValueError("visibility requires a project context and exactly block and when (has_records or "
                             f"has_observations), not {_shown(visibility)} on a {context['kind']} context")
        gate = next((block for block in normalized_blocks if block["key"] == visibility.get("block")), None)
        if (gate is None or gate["type"] != "records"
                or not app_data_project_sources(sources.items, gate["source"])):
            raise ValueError(f"visibility requires a project records block: visibility.block is {_shown(visibility.get('block'))}")
        if (visibility["when"] == "has_observations" and (not gate.get("metrics_source")
                or sources.items[gate["source"]].get("record_schema") != "publication-receipt/v1")):
            raise ValueError(f"visibility requires a publication records block with metrics_source: {gate['key']!r} is not one")
        visibility = {"block": gate["key"], "when": visibility["when"]}
    # Projects can be picked from cards instead of a list: each card shows
    # the project's first image reference (``cover``, a references block)
    # and how many of its children are done (``progress``, a rollup block
    # with completion); one opens the component for that project.
    context_display = context.get("display", "select")
    card_options: dict[str, str] = {}
    for option, block_type in (("cover", "references"), ("progress", "project_rollup")):
        value = context.get(option)
        if value is None:
            continue
        target = next((item for item in normalized_blocks if item["key"] == value), None)
        if context_display != "cards" or target is None or target["type"] != block_type or (
                option == "progress" and not target.get("completion")):
            raise ValueError(f"context.{option} requires cards and a {block_type} block"
                             + (" with completion" if option == "progress" else "") + f", not {_shown(value)}")
        card_options[option] = value
    calendar = raw.get("display", {}).get("calendar")
    if calendar is not None:
        # A project context is one period's calendar (a campaign, a week), opened from its card;
        # a Workspace context is one calendar of every dated project, with no period to pick.
        calendar_fields = {"block", "date_field", "rollup_block", "context_block", "attention_stages", "collection_title"}
        whole_workspace = context["kind"] == "workspace"
        required = calendar_fields - ({"rollup_block", "context_block"} if whole_workspace else set())
        # extra_blocks add their items to the calendar by date; hide_stages drops the calendar block's items at those stages
        # once their extra items exist (a publish time's card gives way to its platform posts).
        # span opens it on a month (the default) or a week; the person can switch to the other.
        # lane_field lays the week out as a schedule, a row per value (a platform) and a column per day.
        calendar_optional = {"extra_blocks", "hide_stages", "span", "lane_field"} | (calendar_fields - required)
        if (context["kind"] not in {"project", "workspace"} or not isinstance(calendar, dict)
                or not required <= set(calendar) <= required | calendar_optional):
            raise ValueError(f"display.calendar needs a project context and {_names(calendar_fields)}, or a workspace "
                             f"context and {_names(calendar_fields - {'rollup_block', 'context_block'})} with an optional "
                             f"rollup_block; optionally {_names({'extra_blocks', 'hide_stages', 'span', 'lane_field'})}; "
                             f"not {_shown(calendar)}")
        calendar_block = next((block for block in normalized_blocks if block["key"] == calendar.get("block")), None)
        rollup_block = next((block for block in normalized_blocks if block["key"] == calendar.get("rollup_block")), None)
        context_block = next((block for block in normalized_blocks if block["key"] == calendar.get("context_block")), None)
        date_field = _path(calendar.get("date_field"), field="display.calendar.date_field")
        calendar_source = sources.items[calendar_block["source"]] if calendar_block is not None else {}
        if (
            calendar_block is None or calendar_block["type"] != "progress"
            or calendar_source["type"] not in ({"workflow_projects"} if whole_workspace else {"workflow_projects", "related_projects"})
            or bool(calendar_source.get("stages"))
            or calendar_source.get("order", {}).get("field") != date_field
            or (rollup_block is None and (not whole_workspace or calendar.get("rollup_block") is not None))
            or (rollup_block is not None and (rollup_block["type"] != "project_rollup" or not rollup_block.get("count_by_stage")
                                              or rollup_block["source"] != calendar_block["source"]))
            or (not whole_workspace and (
                context_block is None or context_block["type"] != "summary"
                or context_block["source"] != context.get("source")
                or app_data_project_sources(sources.items, calendar_block["source"])[-1:] != [context.get("source")]))
            or (whole_workspace and calendar.get("context_block") is not None)
        ):
            raise ValueError("display.calendar: calendar display references are invalid: block must be a progress block over a project "
                             "source with no stages, ordered by date_field; rollup_block a count_by_stage project_rollup "
                             "over the same source; context_block a summary over the context's source, its ancestor. "
                             "In a workspace context the block reads workflow_projects, rollup_block is optional and "
                             "there is no context_block")
        attention_stages = calendar.get("attention_stages")
        if (not isinstance(attention_stages, list) or len(attention_stages) > 32
                or any(not isinstance(stage, str) or not stage.strip() or len(stage.strip()) > 80
                       for stage in attention_stages)):
            raise ValueError(f"display.calendar.attention_stages must be at most 32 stage names, not {_shown(attention_stages)}")
        collection_title = _short_text(calendar.get("collection_title"), field="display.calendar.collection_title", limit=100, required=True)
        extra_keys = calendar.get("extra_blocks", [])
        if not isinstance(extra_keys, list) or len(extra_keys) > 8 or any(not isinstance(key, str) for key in extra_keys):
            raise ValueError(f"display.calendar.extra_blocks must be at most 8 block keys, not {_shown(extra_keys)}")
        extra_blocks = []
        for key in extra_keys:
            block = next((item for item in normalized_blocks if item["key"] == key), None)
            if (block is None or block["key"] == calendar_block["key"] or block["type"] != "progress"
                    or sources.items[block["source"]]["type"] not in {"workflow_projects", "related_projects"}
                    or sources.items[block["source"]].get("order", {}).get("field") != date_field
                    or app_data_project_sources(sources.items, block["source"])[-1:] != [context.get("source")]):
                raise ValueError(f"display.calendar.extra_blocks: {key} must be a progress block over a project source "
                                 "in this context, ordered by date_field")
            extra_blocks.append(key)
        hide_stages = calendar.get("hide_stages", [])
        if (not isinstance(hide_stages, list) or len(hide_stages) > 32
                or any(not isinstance(stage, str) or not stage.strip() or len(stage.strip()) > 80 for stage in hide_stages)):
            raise ValueError(f"display.calendar.hide_stages must be at most 32 stage names, not {_shown(hide_stages)}")
        # Preserve omitted optional keys so older Blueprints round-trip intact.
        optional_calendar = {}
        if "extra_blocks" in calendar:
            optional_calendar["extra_blocks"] = list(dict.fromkeys(extra_blocks))
        if "hide_stages" in calendar:
            optional_calendar["hide_stages"] = list(dict.fromkeys(stage.strip() for stage in hide_stages))
        span = calendar.get("span", "month")
        if span not in _DATA_CALENDAR_SPANS:
            raise ValueError(f"display.calendar.span must be one of {', '.join(_DATA_CALENDAR_SPANS)}, not {_shown(span)}")
        if "lane_field" in calendar:
            lane_field = _path(calendar["lane_field"], field="display.calendar.lane_field")
            unread = [key for key in [calendar_block["key"], *extra_blocks]
                      if lane_field not in sources.items[next(block["source"] for block in normalized_blocks
                                                              if block["key"] == key)].get("fields", [])]
            if unread:
                raise ValueError(f"display.calendar.lane_field {'.'.join(lane_field)} must be read by every calendar "
                                 f"block's source: add it to the fields of the source of {', '.join(unread)}")
            optional_calendar["lane_field"] = lane_field
        calendar = {
            "block": calendar_block["key"], "date_field": date_field,
            "rollup_block": rollup_block["key"] if rollup_block else None,
            "context_block": context_block["key"] if context_block else None,
            "attention_stages": list(dict.fromkeys(stage.strip() for stage in attention_stages)),
            "collection_title": collection_title,
            **optional_calendar,
            **({"span": span} if span != "month" else {}),
        }
    return visibility, card_options, calendar


def _normalize_component(
    raw: Any, index: int, sources: _CheckedSources, seen: set[str], problems: list[str],
) -> dict[str, Any] | None:
    """One component, each of its blocks on its own. What cannot be read yet is left to the next pass."""
    where = f"app_view.components[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: a component is an object, not {_shown(raw)}")
        return None
    key = raw.get("key")
    if isinstance(key, str) and _DATA_KEY.fullmatch(key):
        where = f"app_view.components.{key}"
    unknown = set(raw) - set(_DATA_COMPONENT_OPTIONS)
    if unknown:
        problems.append(f"{where}: contains unsupported fields {_names(unknown)}: a component takes "
                        f"{', '.join(_DATA_COMPONENT_OPTIONS)} (record_display and fields belong on a block)")
    if not isinstance(key, str) or not _DATA_KEY.fullmatch(key) or key in seen:
        problems.append(f"{where}: component keys must be unique identifiers, not {_shown(key)}")
    else:
        seen.add(key)
    if raw.get("type") != "data_view" or raw.get("schema_version", 1) != 1:
        problems.append(f"{where}: app_view supports data_view components at schema version 1: set type to data_view, "
                        f"not type {_shown(raw.get('type'))}, schema_version {_shown(raw.get('schema_version', 1))}")
    context: dict[str, Any] | None = None
    try:
        context = _normalize_component_context(raw, sources)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
    blocks = raw.get("blocks")
    normalized_blocks: list[dict[str, Any]] = []
    block_keys: set[str] = set()
    blocks_read = isinstance(blocks, list) and bool(blocks) and len(blocks) <= 12
    if not blocks_read:
        problems.append(f"{where}: components require blocks, a list of 1–12 blocks, not {_shown(blocks)}")
    for position, block in enumerate(blocks if blocks_read else []):
        bkey = block.get("key") if isinstance(block, dict) else None
        named = isinstance(bkey, str) and bool(_DATA_KEY.fullmatch(bkey))
        block_where = f"{where}.blocks.{bkey}" if named else f"{where}.blocks[{position}]"
        found: list[str] = []
        try:
            normalized = _normalize_block(block, sources, block_keys, found)
        except ValueError as exc:
            found.append(str(exc))
        if found:
            problems.extend(f"{block_where}: {message}" for message in found)
            blocks_read = False
        else:
            normalized_blocks.append(normalized)
        if named:
            block_keys.add(bkey)
    display: dict[str, Any] | None = None
    try:
        display = _normalize_component_display(raw, block_keys)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
    visibility, card_options, calendar = None, {}, None
    # These read the blocks and their sources, so they wait for both.
    if context is not None and display is not None and blocks_read and not sources.broken:
        try:
            visibility, card_options, calendar = _normalize_component_links(raw, context, normalized_blocks, sources)
        except ValueError as exc:
            problems.append(f"{where}: {exc}")
    title = description = label = None
    try:
        title = _short_text(raw.get("title"), field="title", limit=100)
        description = _short_text(raw.get("description"), field="description", limit=300)
        if context is not None and context.get("label"):
            label = _short_text(context.get("label"), field="context.label", limit=80)
    except ValueError as exc:
        problems.append(f"{where}: {exc}")
    if context is None or display is None:
        return None
    source = context.get("source")
    return {"type": "data_view", "schema_version": 1, "key": key,
            "title": title,
            "description": description,
            "context": {"kind": context["kind"], **({"source": source} if source else {}),
                        **({"label": label} if context.get("label") else {}),
                        **({"display": "cards", **card_options} if context.get("display", "select") == "cards" else {})},
            "blocks": normalized_blocks,
            **({"visibility": visibility} if visibility is not None else {}),
            "display": {**display, **({"calendar": calendar} if calendar is not None else {})}}


def field_paths(field: dict[str, Any]) -> list[list[str]]:
    """Every path one block field reads: its value, its fallbacks, a range's end and a progress's max."""
    return [field["field"], *field.get("secondary", []),
            *(field[option] for option in ("end_field", "max_field", *_MEDIA_FIELD_PATHS) if option in field)]


def _check_block_declared_fields(component: dict[str, Any], block: dict[str, Any], sources: dict[str, Any]) -> None:
    """A block reads only what its source declares, and only the sources its context can reach."""
    for option, what in (("tree", "view tree lists every record of the Workspace with what is under it"),
                         ("totals", "totals add up every record of the Workspace")):
        if block.get(option) and component["context"]["kind"] != "workspace":
            raise ValueError(f"{what}: it needs a workspace context, not {component['context']['kind']}")
    if block.get("preview"):
        # A receipt is the post that went out: its parts read the post's own project, the receipt's parent.
        record_source = sources[block["source"]]
        receipt = record_source.get("record_schema") == "publication-receipt/v1"
        owner = record_source["parent"] if receipt else block["source"]
        declared = sources[owner].get("fields", [])
        absent = [path for path in preview_paths(block) if path not in declared]
        if absent:
            raise ValueError(f"preview reads {_dotted(absent)}, which source {owner!r} does not declare"
                             + (" (a receipt's post is its parent project)" if receipt else "")
                             + f": add them to its fields, which list {_dotted(declared) or 'none'}")
    if block["type"] in {"summary", "progress", "records"} and block["fields"] and isinstance(block["fields"][0], dict):
        source_is_project = sources[block["source"]]["type"] in {"workflow_projects", "related_projects"}
        if block["type"] == "summary" and (component["context"]["kind"] != "project"
                                              or component["context"].get("source") != block["source"]
                                              or not source_is_project):
            raise ValueError(f"summary blocks require the selected project source: this one reads {block['source']!r}, "
                             f"the component's context is {component['context']['kind']} over "
                             f"{_shown(component['context'].get('source'))}")
        # Progress fields are presented per project row, so any context
        # may list projects with them; summary describes one selection.
        if block["type"] == "progress" and not source_is_project:
            raise ValueError(f"progress structured fields require a project source: {block['source']!r} is {sources[block['source']]['type']}")
        # Labeled record fields read the record itself: a plain list's items or
        # projects listed as records. A versioned receipt keeps its own presentation.
        record_source = sources[block["source"]]
        if block["type"] == "records" and not source_is_project and (
                record_source["type"] != "project_records" or record_source.get("record_schema") is not None):
            raise ValueError("records structured fields require a plain project_records or project source: "
                             f"{block['source']!r} is {record_source['type']}")
        declared = sources[block["source"]].get("fields", [])
        absent = [path for field in block["fields"] for path in field_paths(field) if path not in declared]
        if absent:
            raise ValueError(f"{block['type']} fields must be declared by their project source: add {_dotted(absent)} to "
                             f"the fields of source {block['source']!r}, which lists {_dotted(declared) or 'none'}")
        return
    if block["type"] != "records":
        return
    ancestors = set(app_data_project_sources(sources, block["source"]))
    metrics_source = block.get("metrics_source")
    if metrics_source is not None:
        metrics = sources[metrics_source]
        records = sources[block["source"]]
        if (records["type"] != "project_records" or records.get("record_schema") != "publication-receipt/v1"
                or metrics["type"] != "publication_stats" or metrics.get("parent") != block["source"]
                or metrics.get("subject") not in ancestors):
            raise ValueError(f"records block {block['key']!r} has an incompatible metrics_source: {metrics_source!r} must be a "
                             f"publication_stats source whose parent is {block['source']!r}, a publication-receipt/v1 "
                             "project_records source, and whose subject is its project")
    display = block.get("record_display") or {}
    for media in display.get("media", []):
        if sources[media["source"]]["parent"] not in ancestors:
            raise ValueError(f"record_display.media source {media['source']!r} is not owned by a record ancestor ({_names(ancestors)})")
    entries = ([display["title"]] if isinstance(display.get("title"), dict) else []) + list(display.get("fields", []))
    for entry in entries:
        if entry["source"] not in ancestors:
            raise ValueError(f"records block {block['key']!r} display source {entry['source']!r} is not a record ancestor ({_names(ancestors)})")
        declared = sources[entry["source"]].get("fields", [])
        absent = [path for path in [entry["field"], *entry.get("secondary", [])] if path not in declared]
        if absent:
            raise ValueError(f"records block {block['key']!r} display field {_dotted(absent)} is not declared by its source "
                             f"{entry['source']!r}, which lists {_dotted(declared) or 'none'}")


def normalize_app_view_data_contract(
    data_sources: Any, components: Any, *, catalog: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Normalize the declarative, non-executable App result-view contract.

    ``catalog`` is intentionally optional: Blueprint and Draft callers provide
    their portable/live resource catalog to validate producer references, while
    persisted legacy App configs still normalize without a catalog.

    Every source, component and block is read on its own, so one pass raises an
    ``AppViewContractError`` naming all their problems.
    """
    raw_sources = {} if data_sources is None else data_sources
    if not isinstance(raw_sources, dict) or len(raw_sources) > 32:
        raise ValueError(f"app_view.data_sources must be an object with at most 32 entries, not {_shown(raw_sources)}")
    sources = _CheckedSources()
    problems: list[str] = []
    for key, raw in raw_sources.items():
        found: list[str] = []
        try:
            if not isinstance(key, str) or not _DATA_KEY.fullmatch(key):
                raise ValueError("a source's key is an identifier: a letter, then letters, digits or _")
            if not isinstance(raw, dict):
                raise ValueError(f"a source is an object, not {_shown(raw)}")
            item = _normalize_data_source(raw, raw_sources, found)
        except ValueError as exc:
            found.append(str(exc))
        if found:
            problems.extend(f"app_view.data_sources.{key}: {message}" for message in found)
            sources.broken.add(key)
        else:
            sources.items[key] = item

    def source_dependencies(name: str, trail: set[str]) -> None:
        if name in trail:
            raise ValueError(f"app_view.data_sources must not contain dependency cycles: {name!r} depends on itself")
        source = sources.items[name]
        for dependency in (source.get("parent"), source.get("subject")):
            if dependency:
                source_dependencies(dependency, trail | {name})
    # These follow parents across sources, so they wait until every source can be read.
    if not sources.broken:
        for source_name in sources.items:
            try:
                source_dependencies(source_name, set())
            except ValueError as exc:
                problems.append(str(exc))
                break
        for source_name, source in sources.items.items():
            if (source["type"] in {"related_projects", "project_files", "project_references"}
                    and sources.items[source["parent"]]["type"] not in {"workflow_projects", "related_projects"}):
                problems.append(f"app_view.data_sources.{source_name}: {source['type']} requires a project source parent: "
                                f"{source['parent']!r} is {sources.items[source['parent']]['type']}")

    raw_components = [] if components is None else components
    if not isinstance(raw_components, list) or len(raw_components) > 16:
        problems.append(f"app_view.components must be an array of at most 16 components, not {_shown(raw_components)}")
        raise AppViewContractError(problems)
    normalized_components: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_components):
        component = _normalize_component(raw, index, sources, seen, problems)
        if component is not None:
            normalized_components.append(component)
    if not sources.broken:
        for component in normalized_components:
            if component.get("visibility"):
                gate = next(block for block in component["blocks"] if block["key"] == component["visibility"]["block"])
                if component["context"].get("source") not in app_data_project_sources(sources.items, gate["source"]):
                    problems.append(f"app_view.components.{component['key']}: visibility must use the selected project's "
                                    f"records: {gate['key']!r} reads {gate['source']!r}, the context selects {_shown(component['context'].get('source'))}")
            for block in component["blocks"]:
                try:
                    _check_block_declared_fields(component, block, sources.items)
                except ValueError as exc:
                    problems.append(f"app_view.components.{component['key']}.blocks.{block['key']}: {exc}")
    if problems:
        raise AppViewContractError(problems)
    if catalog is not None:
        validate_app_view_data_contract(sources.items, normalized_components, catalog=catalog)
    return sources.items, normalized_components


def workflow_result_paths(workflows: list[dict[str, Any]]) -> dict[str, dict[str, Any] | None]:
    """What a Flow's runs record: each step's output name, with its output schema when it declares one.

    A run input (``start``'s) and the final result (``__result``) are readable too.
    """
    roots: dict[str, dict[str, Any] | None] = {"__result": None}
    for workflow in workflows:
        for step in workflow.get("steps", []) if isinstance(workflow.get("steps"), list) else []:
            config = step.get("config") if isinstance(step, dict) else None
            if not isinstance(config, dict):
                continue
            for run_input in config.get("run_inputs") or []:
                if isinstance(run_input, dict) and isinstance(run_input.get("key"), str):
                    roots.setdefault(run_input["key"], None)
            name = config.get("output_var")
            if isinstance(name, str) and name:
                schema = config.get("output_schema")
                roots[name] = schema if isinstance(schema, dict) else roots.get(name)
    return roots


def _result_path_declared(roots: dict[str, dict[str, Any] | None], path: list[str]) -> bool:
    """A path starts at an output a step records; under a declared schema it must be declared there."""
    if not path or path[0] not in roots:
        return False
    node = roots[path[0]]
    for part in path[1:]:
        if not isinstance(node, dict) or not isinstance(node.get("properties"), dict):
            return True  # An output without a schema is read as it comes.
        node = node["properties"].get(part)
        if node is None:
            return False
    return True


def validate_app_view_data_contract(data_sources: dict[str, Any], components: list[dict[str, Any]], *, catalog: dict[str, Any]) -> None:
    """Validate portable/live producer references without making them runners."""
    definitions_present = "workflow_definitions" in catalog
    bindings_present = "workflow_bindings" in catalog
    stats_present = "stat_keys" in catalog
    definitions = set(catalog.get("workflow_definitions", ()))
    bindings = set(catalog.get("workflow_bindings", ()))
    stats = set(catalog.get("stat_keys", ()))
    workflows = {
        str(row.get("slug") or row.get("id") or ""): row
        for row in catalog.get("workflows", ()) if isinstance(row, dict)
    }

    def declared_paths(source_name: str, seen: set[str] | None = None) -> set[tuple[str, ...]]:
        seen = set() if seen is None else seen
        if source_name in seen:
            raise ValueError("app_view data sources must not contain dependency cycles")
        seen.add(source_name)
        source = data_sources[source_name]
        refs = list(source.get("producer_workflows", []))
        parent = source.get("parent")
        if parent:
            refs += list(data_sources[parent].get("producer_workflows", []))
        # Every project may carry the platform's display, declared or not.
        paths: set[tuple[str, ...]] = set(PROJECT_DISPLAY_PATHS)
        for ref in refs:
            workflow = workflows.get(ref.removeprefix("workflow_definition:"))
            if workflow is None:
                continue
            for step in workflow.get("steps", []) if isinstance(workflow.get("steps"), list) else []:
                config = step.get("config") if isinstance(step, dict) else None
                if not isinstance(config, dict) or config.get("project_type") != source.get("project_type"):
                    continue
                schema = config.get("state_schema")
                if not isinstance(schema, dict):
                    continue
                properties = schema.get("properties")
                if not isinstance(properties, dict):
                    continue
                def walk(node: Any, prefix: tuple[str, ...] = ()) -> None:
                    if not isinstance(node, dict):
                        return
                    props = node.get("properties")
                    if not isinstance(props, dict):
                        paths.add(prefix)
                        return
                    # Object-valued receipt fields are declared values too;
                    # derived record sources may select the object itself.
                    if prefix:
                        paths.add(prefix)
                    for name, child in props.items():
                        if isinstance(name, str):
                            walk(child, prefix + (name,))
                for key, child in properties.items():
                    walk(child, (key,))
        return paths

    def declared_types(source_name: str) -> dict[tuple[str, ...], set[str]]:
        source = data_sources[source_name]
        refs = list(source.get("producer_workflows", []))
        if source.get("parent"):
            refs += list(data_sources[source["parent"]].get("producer_workflows", []))
        result: dict[tuple[str, ...], set[str]] = {path: {kind} for path, kind in PROJECT_DISPLAY_PATHS.items()}
        for ref in refs:
            workflow = workflows.get(ref.removeprefix("workflow_definition:"))
            if workflow is None:
                continue
            for step in workflow.get("steps", []) if isinstance(workflow.get("steps"), list) else []:
                config = step.get("config") if isinstance(step, dict) else None
                if not isinstance(config, dict) or config.get("project_type") != source.get("project_type"):
                    continue
                schema = config.get("state_schema")
                if not isinstance(schema, dict):
                    continue
                def walk(node: Any, prefix: tuple[str, ...] = ()) -> None:
                    if not isinstance(node, dict):
                        return
                    raw_type = node.get("type")
                    kinds = set(raw_type if isinstance(raw_type, list) else [raw_type]) - {None, "null"}
                    if prefix and kinds:
                        result.setdefault(prefix, set()).update(str(kind) for kind in kinds)
                    for name, child in (node.get("properties") or {}).items():
                        if isinstance(name, str):
                            walk(child, prefix + (name,))
                walk(schema)
        return result

    def schema_lists(paths: set[tuple[str, ...]]) -> str:
        """What a producer's state schema offers, for an error that sends the author back to it."""
        return _dotted(sorted(paths)[:40]) or "nothing for this project_type"

    def check_source(source_name: str, source: dict[str, Any]) -> None:
        for ref in source.get("producer_workflows", []):
            if definitions_present and ref.removeprefix("workflow_definition:") not in definitions:
                raise ValueError(f"app_view source {source_name!r} references an unavailable workflow definition: {ref} "
                                 f"(the Workspace has {_names(definitions)})")
        for launch in source.get("launch_workflows", []):
            ref = launch["workflow"].removeprefix("workflow:")
            if bindings_present and ref not in bindings:
                raise ValueError(f"app_view source {source_name!r} references an unavailable workflow binding: "
                                 f"{launch['workflow']} (the Workspace has {_names(bindings)})")
        if source["type"] == "workspace_stats" and stats_present:
            missing = set(source["stat_keys"]) - stats
            if missing:
                raise ValueError(f"app_view source {source_name!r} references unavailable stats: {', '.join(sorted(missing))} "
                                 f"(the Workspace has {_names(stats)})")
        # Configured state paths are only safe when a producer schema declares
        # them. A schema's additionalProperties never authorizes an App read.
        if workflows and source["type"] in {"workflow_projects", "related_projects"}:
            allowed = declared_paths(source_name)
            missing_paths = [path for path in source.get("fields", []) if tuple(path) not in allowed]
            if missing_paths:
                raise ValueError(f"app_view source {source_name!r} reads fields absent from its producer schema: "
                                 f"{_dotted(missing_paths)}; it declares {schema_lists(allowed)}")
            field_types = declared_types(source_name)
            strings = {path for path, kinds in field_types.items() if kinds == {"string"}}
            execution_run_id_path = source.get("execution_run_id_path")
            if execution_run_id_path and field_types.get(tuple(execution_run_id_path)) != {"string"}:
                raise ValueError(f"app_view source {source_name!r} execution_run_id_path {_dotted([execution_run_id_path])} "
                                 f"requires a string-typed producer field; those are {schema_lists(strings)}")
            not_strings = [path for path in source.get("label_fields", []) if field_types.get(tuple(path)) != {"string"}]
            if not_strings:
                raise ValueError(f"app_view source {source_name!r} label_fields require string-typed producer fields, "
                                 f"not {_dotted(not_strings)}; those are {schema_lists(strings)}")
            order = source.get("order")
            if order and field_types.get(tuple(order["field"])) != {"string"}:
                raise ValueError(f"app_view source {source_name!r} order field {_dotted([order['field']])} requires a "
                                 f"string-typed producer field; those are {schema_lists(strings)}")
        if source["type"] == "workflow_results" and workflows:
            allowed_roots = workflow_result_paths(
                [workflows[ref.removeprefix("workflow_definition:")] for ref in source.get("producer_workflows", [])
                 if ref.removeprefix("workflow_definition:") in workflows])
            requested = [*source.get("fields", []), *(entry["field"] for entry in source.get("media", []))]
            absent = [path for path in requested if not _result_path_declared(allowed_roots, path)]
            if absent:
                raise ValueError(f"app_view source {source_name!r} reads results its Flow does not produce: {_dotted(absent)}; "
                                 f"its steps record {_names(allowed_roots)}")
        if source["type"] in {"project_records", "project_documents"} and workflows:
            allowed = declared_paths(source["parent"])
            requested = source.get("record_path") or source.get("document_path")
            if tuple(requested) not in allowed:
                raise ValueError(f"app_view source {source_name!r} reads a field absent from its producer schema: "
                                 f"{_dotted([requested])}; source {source['parent']!r} declares {schema_lists(allowed)}")
            actual = declared_types(source["parent"]).get(tuple(requested), set())
            expected = ("string" if source["type"] == "project_documents"
                        else "object" if source.get("record_schema") else "array")
            if expected not in actual:
                raise ValueError(f"app_view source {source_name!r} requires a {expected}-typed producer field: "
                                 f"{_dotted([requested])} is {_names(actual)}")
        if source["type"] == "project_files" and workflows:
            # Only a folder the producer declares (and the runtime names) is listed.
            if "string" not in declared_types(source["parent"]).get(("project_root",), set()):
                raise ValueError(f"app_view source {source_name!r} requires a string project_root in its producer schema: "
                                 f"the state of source {source['parent']!r} declares none")
        if source["type"] == "related_projects" and workflows:
            relation_types = declared_types(source_name).get(tuple(source["parent_state_path"]), set())
            if "string" not in relation_types:
                raise ValueError(f"app_view related source {source_name!r} requires a string parent relation: "
                                 f"parent_state_path {_dotted([source['parent_state_path']])} is {_names(relation_types)}")
    # Blocks can only present their configured source category. This prevents a
    # valid-looking block from silently turning a document source into metrics.
    compatible = {
        "progress": {"workflow_projects", "related_projects", "owned_automations"},
        "records": {"project_records", "workflow_projects", "related_projects", "owned_automations"},
        "documents": {"project_documents", "project_files"},
        # One card per child project: its latest version's results together.
        "packs": {"project_files"},
        "references": {"project_references"},
        "metrics": {"publication_stats", "workspace_stats"},
        "summary": {"workflow_projects", "related_projects"},
        "project_rollup": {"workflow_projects", "related_projects"},
        # One tile per image or video: each a Flow's results hold, or a
        # project's ``display.media`` and a record's image fields name.
        "gallery": {"workflow_results", "workflow_projects", "related_projects", "project_records"},
        # Counts and sums over records, or Stats' readings over time.
        "chart": set(_CHART_SOURCES),
    }

    def record_project_ancestors(record_source: str) -> set[str]:
        return set(app_data_project_sources(data_sources, record_source))

    def check_block(component: dict[str, Any], block: dict[str, Any]) -> None:
        source = data_sources[block["source"]]
        if source["type"] not in compatible[block["type"]]:
            raise ValueError(f"app_view block {block['key']!r} has an incompatible source type: {block['source']!r} is "
                             f"{source['type']}, a {block['type']} block reads {_names(compatible[block['type']])}")
        if block["type"] == "summary":
            if component["context"].get("kind") != "project" or component["context"].get("source") != block["source"]:
                raise ValueError(f"app_view summary blocks require the selected project source: this one reads "
                                 f"{block['source']!r}, the component's context is {component['context'].get('kind')} over "
                                 f"{_shown(component['context'].get('source'))}")
            requested_fields = [path for field in block.get("fields", []) for path in field_paths(field)]
        elif block["type"] == "project_rollup":
            if component["context"].get("kind") != "project":
                raise ValueError("project_rollup blocks require a selected project context, not "
                                 f"{component['context'].get('kind')}")
            requested_fields = []
        else:
            requested_fields = [path for field in block.get("fields", [])
                                for path in (field_paths(field) if isinstance(field, dict) else [field])]
        allowed_fields = {tuple(path) for path in source.get("fields", [])}
        absent = [path for path in requested_fields if tuple(path) not in allowed_fields]
        if absent:
            raise ValueError(f"app_view block {block['key']!r} reads fields not declared by its source: add {_dotted(absent)} "
                             f"to the fields of source {block['source']!r}, which lists {_dotted(sorted(allowed_fields)) or 'none'}")
        if block["type"] != "records":
            return
        ancestors = record_project_ancestors(block["source"])
        metrics_source = block.get("metrics_source")
        if metrics_source is not None:
            metrics = data_sources[metrics_source]
            if (source["type"] != "project_records" or source.get("record_schema") != "publication-receipt/v1"
                    or metrics["type"] != "publication_stats" or metrics.get("parent") != block["source"]
                    or metrics.get("subject") not in ancestors):
                raise ValueError(f"app_view records block {block['key']!r} has an incompatible metrics_source: {metrics_source!r} "
                                 f"must be a publication_stats source whose parent is {block['source']!r}, a "
                                 "publication-receipt/v1 project_records source, and whose subject is its project")
        display = block.get("record_display") or {}
        for media in display.get("media", []):
            media_source = data_sources.get(media["source"], {})
            if media_source.get("type") != "project_documents" or media_source.get("parent") not in ancestors:
                raise ValueError(f"app_view records media source {media['source']!r} is not owned by a record ancestor "
                                 f"({_names(ancestors)})")
        entries = ([display["title"]] if isinstance(display.get("title"), dict) else []) + list(display.get("fields", []))
        for entry in entries:
            display_source = entry["source"]
            field = tuple(entry["field"])
            if display_source not in ancestors:
                raise ValueError(f"app_view records block {block['key']!r} display source {display_source!r} is not a record "
                                 f"ancestor ({_names(ancestors)})")
            declared = {tuple(path) for path in data_sources[display_source].get("fields", [])}
            absent = [path for path in [entry["field"], *entry.get("secondary", [])] if tuple(path) not in declared]
            if absent:
                raise ValueError(f"app_view records block {block['key']!r} display field {_dotted(absent)} is not declared by its "
                                 f"source {display_source!r}, which lists {_dotted(sorted(declared)) or 'none'}")
            if entry is display.get("title") and workflows and "string" not in declared_types(display_source).get(field, set()):
                raise ValueError(f"app_view records block {block['key']!r} title display field {_dotted([field])} must be string-typed")

    problems: list[str] = []
    for source_name, source in data_sources.items():
        try:
            check_source(source_name, source)
        except ValueError as exc:
            problems.append(str(exc))
    for component in components:
        visibility = component.get("visibility")
        if visibility:
            gate = next(block for block in component["blocks"] if block["key"] == visibility["block"])
            if component["context"].get("source") not in record_project_ancestors(gate["source"]):
                problems.append(f"app_view component {component.get('key')!r} visibility must use the selected project's records: "
                                f"{gate['key']!r} reads {gate['source']!r}, the context selects {_shown(component['context'].get('source'))}")
        for block in component.get("blocks", []):
            try:
                check_block(component, block)
            except ValueError as exc:
                problems.append(f"app_view.components.{component.get('key')}.blocks.{block['key']}: {exc}")
    if problems:
        raise AppViewContractError(problems)


_SECTION_ID =re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
# A tracked number a metrics section names: a Stat by its key, or a Goal by its key.
_METRIC_REF = re.compile(r"^(stat|goal):[A-Za-z0-9_.:-]{1,120}$")
# The glyphs an App entry may name; the web maps each to one icon. An entry
# without one gets the glyph its name suggests.
APP_ENTRY_ICONS: tuple[str, ...] = (
    "flow", "chat", "clock", "play", "music", "audio", "microphone", "image", "palette", "sparkles",
    "document", "report", "presentation", "checklist", "clipboard", "search", "megaphone", "send",
    "comment", "mail", "globe", "calendar", "trending", "rocket", "academic", "box", "cart",
    "refresh", "bolt", "bookmark", "scale", "code", "users", "dollar", "heart", "star",
)

# The App page is a short list of blocks: the one contract shared by Blueprints,
# settings, the AI and the web renderer (which registers exactly these blocks
# and reads this catalog for views, sources and limits). Each block reads one
# typed source; the first view is the default; ``limit`` is how many items the
# block shows at once (default, max).
# The page is four zones in a fixed order; a block belongs to one zone and only
# moves within it: what needs the person, what they can start, how things
# stand, and what was made.
APP_ZONES: tuple[str, ...] = ("notices", "functions", "data", "outputs")

APP_SECTION_BLOCKS: dict[str, dict[str, Any]] = {
    # Connections the Workspace's Blueprint needs that are not set up yet.
    "connections": {"zone": "notices", "sources": None, "default_source": None, "source_kinds": (), "views": ("default",),
                    "limit": None, "multiple": False, "removable": True},
    # The page's entries; every page keeps them.
    "actions": {"zone": "functions", "sources": None, "default_source": None, "source_kinds": (), "views": ("default",),
                "limit": None, "multiple": False, "removable": False},
    "queue": {"zone": "notices", "sources": re.compile(r"^approvals:pending$"), "default_source": "approvals:pending",
              "source_kinds": ("approvals",), "views": ("list",), "limit": None, "multiple": False, "removable": True},
    # Tracked Workspace stats and goals: the ones its ``items`` name, in that
    # order, or else all of them in the viewer's own stat order.
    "metrics": {"zone": "data", "sources": re.compile(r"^workspace$"), "default_source": "workspace", "source_kinds": ("workspace",),
                "views": ("tiles",), "limit": (4, 12), "multiple": False, "removable": True},
    # 'auto' shows a gallery when most recent files are images or video.
    "outputs": {"zone": "outputs", "sources": re.compile(r"^(workspace|workflow:[A-Za-z0-9_.:-]{1,128})$"), "default_source": "workspace",
                "source_kinds": ("workspace", "workflow"), "views": ("auto", "list", "gallery"),
                "limit": (8, 24), "multiple": True, "removable": True},
}
DEFAULT_APP_SECTIONS: tuple[dict[str, Any], ...] = (
    {"id": "connections", "block": "connections", "source": None, "view": "default", "limit": None},
    {"id": "queue", "block": "queue", "source": "approvals:pending", "view": "list", "limit": None},
    {"id": "actions", "block": "actions", "source": None, "view": "default", "limit": None},
    {"id": "outputs", "block": "outputs", "source": "workspace", "view": "auto", "limit": 8},
)


def app_block_catalog() -> list[dict[str, Any]]:
    """The block contract as data, so clients never hard-code it."""
    return [{
        "block": block, "zone": spec["zone"], "views": list(spec["views"]), "default_source": spec["default_source"],
        "source_kinds": list(spec["source_kinds"]), "multiple": spec["multiple"], "removable": spec["removable"],
        "limit": {"default": spec["limit"][0], "max": spec["limit"][1]} if spec["limit"] else None,
    } for block, spec in APP_SECTION_BLOCKS.items()]


# Every page-level field an App config accepts (normalize_app_view_config).
APP_VIEW_FIELDS: tuple[str, ...] = (
    "version", "eyebrow", "title", "description", "hero", "order", "hidden", "labels", "descriptions", "forms",
    "targets", "entries", "prompts", "presets", "asks", "choices", "icons", "visual", "layout", "cover_image_url",
    "motion_cover_url", "motion_cover_source_url", "data_sources", "components", "sections", "open_as", "i18n",
    "theme",
)

# What a Workspace shows when someone opens it: the App beside the chat, the App alone, or the chat.
APP_OPEN_AS: tuple[str, ...] = ("split", "app", "chat")

# How an App looks within the platform's one layout (see normalize_app_theme).
APP_THEME_SURFACES: tuple[str, ...] = ("neutral", "tinted", "deep")
APP_THEME_RADII: tuple[str, ...] = ("sharp", "soft", "round")
_APP_THEME_ACCENT = re.compile(r"#[0-9a-fA-F]{6}")


def normalize_app_theme(value: Any) -> dict[str, str] | None:
    """An App's own look: a seed accent colour, a surface and a corner style.

    The App renders these as tokens; the platform derives hover and soft
    shades from the accent and keeps text on it readable, so no setting can
    make the page unreadable. None or empty means the platform's look.
    """
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"accent", "surface", "radius"}:
        raise ValueError("app_view.theme has only accent, surface and radius")
    theme: dict[str, str] = {}
    accent = value.get("accent")
    if accent is not None:
        if not isinstance(accent, str) or not _APP_THEME_ACCENT.fullmatch(accent):
            raise ValueError("app_view.theme.accent is a #rrggbb colour")
        theme["accent"] = accent.lower()
    for field, allowed in (("surface", APP_THEME_SURFACES), ("radius", APP_THEME_RADII)):
        if value.get(field) is not None:
            if value[field] not in allowed:
                raise ValueError(f"app_view.theme.{field} must be one of: {', '.join(allowed)}")
            theme[field] = value[field]
    return theme or None


# The shape of a page, for its author to copy: sections, data sources, and a data
# view in tabs and one per project. A test keeps it passing normalize_app_view_config.
APP_PAGE_EXAMPLE: dict[str, Any] = {
    "sections": [{"id": "queue", "block": "queue"}, {"id": "actions", "block": "actions"},
                 {"id": "drafts", "block": "outputs", "source": "workflow:your_flow", "title": "Drafts"},
                 {"id": "reports", "block": "outputs", "source": "workflow:other_flow", "title": "Reports"}],
    "data_sources": {
        "jobs": {"type": "workflow_projects", "project_type": "your_project_type",
                 "producer_workflows": ["workflow_definition:your_flow"],
                 "fields": [["title"], ["due_date"]], "label_fields": [["title"]]},
        "waiting": {"type": "workflow_projects", "project_type": "your_project_type",
                    "producer_workflows": ["workflow_definition:your_flow"],
                    "fields": [["title"]], "stages": ["awaiting_approval"]},
        "report": {"type": "project_documents", "parent": "jobs", "document_path": ["report_document_id"],
                   "document_role": "report"},
    },
    "components": [
        {"type": "data_view", "key": "overview", "title": "Overview", "context": {"kind": "workspace"},
         "display": {"layout": "tabs", "groups": [{"key": "now", "title": "Waiting for you", "blocks": ["waiting_jobs"]},
                                                  {"key": "all", "title": "All", "blocks": ["all_jobs"]}]},
         "blocks": [{"key": "waiting_jobs", "type": "progress", "source": "waiting",
                     "stage_labels": {"awaiting_approval": "Waiting for you"}, "empty_message": "Nothing waits on you."},
                    {"key": "all_jobs", "type": "progress", "source": "jobs",
                     "fields": [{"field": ["due_date"], "label": "Due", "format": "date"}]}]},
        {"type": "data_view", "key": "job", "title": "Job",
         "context": {"kind": "project", "source": "jobs", "label": "Job", "display": "cards"},
         "blocks": [{"key": "details", "type": "summary", "source": "jobs",
                     "fields": [{"field": ["title"], "label": "Title"}]},
                    {"key": "report_file", "type": "documents", "source": "report", "title": "Report"}]},
    ],
}


def app_module_catalog() -> dict[str, Any]:
    """Every module an App page can be composed of, as data.

    The App design guide explains when to use each; this is the part read from
    the normalizer itself, so the guide never offers a module the page rejects.
    """
    return {
        "zones": list(APP_ZONES),
        "sections": app_block_catalog(),
        "page_fields": list(APP_VIEW_FIELDS),
        "layouts": ["feature", "cover", "list"],
        "visuals": ["auto", "image", "motion", "plain"],
        "form_field_types": list(_FORM_FIELD_TYPES),
        "data_sources": sorted(_DATA_SOURCE_TYPES),
        "data_blocks": sorted(_DATA_BLOCK_TYPES),
        "data_block_options": list(_DATA_BLOCK_OPTIONS),
        # A progress block's ``view``, and a ``display.calendar``'s ``span``; the first of each is the default.
        "data_block_views": list(_DATA_BLOCK_VIEWS),
        "data_calendar_spans": list(_DATA_CALENDAR_SPANS),
        "entry_icons": list(APP_ENTRY_ICONS),
        "data_contexts": sorted(_DATA_CONTEXT_KINDS),
        "data_context_options": list(_DATA_CONTEXT_OPTIONS),
        "data_context_displays": list(_DATA_CONTEXT_DISPLAYS),
        "data_display_layouts": list(_DATA_DISPLAY_LAYOUTS),
        "data_source_fields": {name: {part: list(fields) for part, fields in takes.items()}
                               for name, takes in _DATA_SOURCE_FIELDS.items()},
        "section_options": list(_SECTION_OPTIONS),
        "data_component_options": list(_DATA_COMPONENT_OPTIONS),
        "data_display_options": list(_DATA_DISPLAY_OPTIONS),
        "data_field_options": list(_DATA_FIELD_OPTIONS),
        "data_field_formats": list(_DATA_FIELD_FORMATS),
        # A gallery block's ``media`` presentation, and the frames, layouts and companions a media field takes.
        "data_block_media": {"options": list(_MEDIA_OPTIONS), "layouts": list(_MEDIA_LAYOUTS),
                             "aspects": list(MEDIA_ASPECTS), "groups": list(_MEDIA_GROUPS),
                             "item_action_inputs": ["input", *_MEDIA_ACTION_INPUTS]},
        "data_field_media": {"aspects": list(MEDIA_ASPECTS), "images_layouts": list(_IMAGES_LAYOUTS),
                             "compare": list(_COMPARE_MODES), "paths": list(_MEDIA_FIELD_PATHS)},
        # A block's ``preview``: each record as the post it will be on a platform (its receipt and counts once out),
        # and a calendar's ``lane_field`` (a week as platform × day).
        "data_block_preview": {"options": list(_PREVIEW_OPTIONS), "blocks": list(_PREVIEW_BLOCK_TYPES),
                               "platforms": list(PREVIEW_PLATFORMS), "fields": list(PREVIEW_FIELDS),
                               "needs_one_of": list(_PREVIEW_CONTENT), "layouts": list(_PREVIEW_LAYOUTS),
                               "aspects": list(MEDIA_ASPECTS)},
        "data_calendar_options": ["extra_blocks", "hide_stages", "span", "lane_field"],
        # A progress block's collection options: groupings a person picks, row selection, CSV export, totals,
        # and what the timeline, gantt, places and tree views read. A table always lets a person hide columns and
        # save a filtered, sorted, grouped view as a named tab of their own.
        "data_block_collection": {"group_views": list(_GROUP_VIEWS), "totals": list(_TOTAL_AGGREGATES),
                                  "numeric_formats": list(NUMERIC_FIELD_FORMATS),
                                  "timeline": ["date_field", "end_field", "depends_field"],
                                  "places": ["address_field", "name_field", "lat_field", "lng_field"],
                                  "tree": ["children"]},
        # A ``chart`` block's picture, and what each part of it takes.
        "data_block_chart": {"options": list(_CHART_OPTIONS), "kinds": list(CHART_KINDS), "buckets": list(CHART_BUCKETS),
                             "aggregates": list(CHART_AGGREGATES), "stat_aggregates": list(_CHART_STAT_AGGREGATES),
                             "formats": list(CHART_FORMATS), "sources": list(_CHART_SOURCES),
                             "target_kinds": list(_CHART_TARGET_KINDS)},
        # What a producing Workflow writes in ``state.display`` for every page to show.
        "project_display_fields": [".".join(path) for path in PROJECT_DISPLAY_PATHS if len(path) > 1],
        "page_example": APP_PAGE_EXAMPLE,
    }


def _normalize_section(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"a section is an object, not {_shown(item)}")
    unknown = set(item) - set(_SECTION_OPTIONS)
    if unknown:
        raise ValueError(f"contains unsupported fields {_names(unknown)}: a section takes {', '.join(_SECTION_OPTIONS)}")
    block = item.get("block")
    spec = APP_SECTION_BLOCKS.get(block) if isinstance(block, str) else None
    if spec is None:
        raise ValueError(f"app_view section block must be one of: {', '.join(APP_SECTION_BLOCKS)}, not {_shown(block)}")
    section_id = item.get("id", block)
    if not isinstance(section_id, str) or not _SECTION_ID.fullmatch(section_id):
        raise ValueError(f"app_view section ids must be short lowercase identifiers, not {_shown(section_id)}")
    source = item.get("source", spec["default_source"])
    if spec["sources"] is None:
        if source is not None:
            raise ValueError(f"app_view {block} sections take no source, not {_shown(source)}")
    elif not isinstance(source, str) or not spec["sources"].fullmatch(source):
        raise ValueError(f"app_view {block} section source {_shown(source)} must match {spec['sources'].pattern}")
    view = item.get("view", spec["views"][0])
    if view not in spec["views"]:
        raise ValueError(f"app_view {block} section view must be one of: {', '.join(spec['views'])}, not {_shown(view)}")
    limit = item.get("limit")
    if spec["limit"]:
        default_limit, max_limit = spec["limit"]
        limit = default_limit if limit is None else limit
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= max_limit:
            raise ValueError(f"app_view {block} limit must be 1–{max_limit}, not {_shown(limit)}")
    elif limit is not None:
        raise ValueError(f"app_view {block} sections take no limit, not {_shown(limit)}")
    normalized = {"id": section_id, "block": block, "source": source, "view": view, "limit": limit}
    if item.get("items") is not None:
        items = item["items"]
        if block != "metrics":
            raise ValueError(f"app_view {block} sections take no items: only metrics sections do")
        if (not isinstance(items, list) or len(items) > APP_SECTION_BLOCKS["metrics"]["limit"][1]
                or any(not isinstance(ref, str) or not _METRIC_REF.fullmatch(ref) for ref in items)
                or len(set(items)) != len(items)):
            raise ValueError(f"app_view metrics items must be up to 12 distinct stat:<key> or goal:<key> references, not {_shown(items)}")
        # Only a section that names its numbers carries the list, so older
        # configs keep their normalized form.
        normalized["items"] = list(items)
    if item.get("title") is not None:
        title = item["title"].strip() if isinstance(item["title"], str) else ""
        if not 1 <= len(title) <= 60:
            raise ValueError(f"app_view section titles must be 1–60 characters, not {_shown(item['title'])}")
        # Present only when set, like items: the heading over this section
        # instead of its source's name (a Flow's outputs as "Decks").
        normalized["title"] = title
    return normalized


def _normalize_sections(value: Any) -> list[dict[str, Any]] | None:
    """``None`` keeps the default page; a list replaces it in order."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError(f"app_view.sections must be an array of at most 8 sections, not {_shown(value)}")
    sections: list[dict[str, Any]] = []
    problems: list[str] = []
    for position, item in enumerate(value):
        try:
            sections.append(_normalize_section(item))
        except ValueError as exc:
            problems.append(f"app_view.sections[{position}]: {exc}")
    ids = [section["id"] for section in sections]
    if len(set(ids)) != len(ids):
        repeated = {section_id for section_id in ids if ids.count(section_id) > 1}
        problems.append(f"app_view section ids must be unique: {_names(repeated)} is used more than once; a section "
                        "without an id takes its block's name, so give each of them its own id")
    for block, spec in APP_SECTION_BLOCKS.items():
        if not spec["multiple"] and sum(section["block"] == block for section in sections) > 1:
            problems.append(f"app_view may contain at most one {block} section")
    if problems:
        raise AppViewContractError(problems)
    # Zones keep their order whatever order the blocks were written in.
    return sorted(sections, key=lambda section: APP_ZONES.index(APP_SECTION_BLOCKS[section["block"]]["zone"]))


# workflow_service_keys as a JSON path: every non-empty string ``service_key``
# at any depth of a Flow's steps.
_SERVICE_KEYS_PATH = '$.**.service_key ? (@.type() == "string" && @ != "")'


def workflow_service_keys(steps_by_workflow: list[Any]) -> set[str]:
    """Services some Workflow already runs as one of its steps."""
    keys: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key == "service_key" and isinstance(item, str) and item:
                    keys.add(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(steps_by_workflow)
    return keys


def suggested_hidden_actions(
    actions: list[dict[str, Any]], *, flow_service_keys: set[str], plumbing_keys: set[str],
    named: set[str] | None = None, hidden: list[str] | None = None,
) -> list[str]:
    """Entries the page leaves out unless its order or hidden list names them.

    A service a Workflow already runs is reached through that Workflow; an
    Automation that only schedules a listed Workflow repeats it; plumbing such
    as the metric assessment shows up on the tracking tiles instead. Standalone
    entries and everything else stay.
    """
    named = named or set()
    explicit = set(hidden or [])
    suggested = [
        action["key"] for action in actions
        if action["key"] not in named and (
            action["key"] in plumbing_keys
            or (action["key"].startswith("service:") and action["key"].removeprefix("service:") in flow_service_keys)
            or (action["key"].startswith("automation:") and action.get("target_workflow_id")))
    ]
    # Never hide the whole page.
    if len(explicit | set(suggested)) >= len(actions):
        return []
    return suggested


def app_view_sections(config: dict[str, Any]) -> list[dict[str, Any]]:
    """The page's sections, falling back to the default page."""
    sections = config.get("sections")
    return [dict(section) for section in (DEFAULT_APP_SECTIONS if sections is None else sections)]


class AppViewPreview:
    """A Workspace read with an open operation draft's App page in place of its own.

    Every App read (the page, its sections and data views) takes it where it
    takes the Workspace, so a candidate page renders with the Workspace's real
    data before anyone approves it. It is never written.
    """

    def __init__(self, workspace: Any, app_view: dict[str, Any]) -> None:
        self._workspace = workspace
        self.operating_model = {**(workspace.operating_model or {}), "app_view": app_view}

    def __getattr__(self, name: str) -> Any:
        return getattr(self._workspace, name)


async def app_view_draft_preview(db: Any, workspace: Any, draft_id: str) -> AppViewPreview | None:
    """The Workspace as an open draft of it would show its App, or None."""
    from packages.core.constants.workspace_operations import WorkspaceOperationDraftStatus
    from packages.core.models.workspace import WorkspaceOperationDraft

    draft = await db.get(WorkspaceOperationDraft, draft_id)
    if (draft is None or draft.workspace_id != workspace.id or draft.entity_id != workspace.entity_id
            or draft.status != WorkspaceOperationDraftStatus.OPEN.value):
        return None
    page = ((draft.current_state or {}).get("operating_model") or {}).get("app_view")
    return AppViewPreview(workspace, page) if isinstance(page, dict) else None


async def load_workspace_app_view(
    db: Any, workspace: Any, *, user: Any, include_catalog: bool = True, locale: str | None = None,
) -> dict[str, Any]:
    """Use the same actor-visible target catalog for Chat and Config.

    ``locale`` is the viewer's language for the page text (see viewer_language).
    """
    from sqlalchemy import cast, exists, func, or_, select
    from sqlalchemy.dialects.postgresql import JSONB, JSONPATH
    from packages.core.models.scheduler import ScheduledJob
    from packages.core.models.workspace import Agent, AgentSubscription, Workspace
    from packages.core.models.workflow import WorkflowBinding, WorkflowDefinition, WorkflowTemplateInstallation
    from packages.core.stats.review_setup import REVIEW_WORKFLOW_SLUG
    from packages.core.models.permission import ResourceType
    from packages.core.services.resource_access import ResourceDescriptor, readable_resource_ids
    from packages.core.services.workspace_workflow_router import list_workspace_chat_entrypoints
    from packages.core.services.workspace_workflow_router import workflow_run_inputs

    entrypoints = await list_workspace_chat_entrypoints(
        db, entity_id=user.entity_id, workspace_id=workspace.id,
        user=user, require_control=True, include_unpublished=True, locale=locale,
    )
    agent_rows = (await db.execute(select(AgentSubscription, Agent).join(
        Agent, Agent.id == AgentSubscription.agent_id,
    ).where(
        AgentSubscription.entity_id == user.entity_id,
        AgentSubscription.workspace_id == workspace.id,
        AgentSubscription.status == "active", Agent.deleted_at.is_(None),
    ))).all()
    agents = [{"id": agent.id, "name": getattr(agent, "display_name", None) or agent.name,
               "service_key": subscription.service_key or ""} for subscription, agent in agent_rows]
    jobs = (await db.execute(select(ScheduledJob).where(
        ScheduledJob.entity_id == user.entity_id, ScheduledJob.workspace_id == workspace.id,
        ScheduledJob.agent_initiative_id.is_(None),
    ))).scalars().all()
    jobs = [job for job in jobs if app_lists_scheduled_job(job)]
    # workflow_service_keys, run by Postgres: the keys, not every Flow's step graph.
    flow_service_keys = set((await db.execute(select(func.jsonb_path_query(
        WorkflowDefinition.steps, cast(_SERVICE_KEYS_PATH, JSONPATH), type_=JSONB,
    )).where(
        WorkflowDefinition.entity_id == user.entity_id,
        WorkflowDefinition.id.in_({entry.workflow_id for entry in entrypoints}),
    ))).scalars().all()) if entrypoints else set()
    # The metric assessment feeds the tracking tiles; its schedule is not an entry.
    assessments = (await db.execute(select(WorkflowBinding.id, WorkflowBinding.workflow_id).where(
        WorkflowBinding.entity_id == user.entity_id, WorkflowBinding.workspace_id == workspace.id,
        WorkflowBinding.config["workspace_blueprint_workflow_slug"].as_string() == REVIEW_WORKFLOW_SLUG,
    ))).all()
    assessment_refs = {ref for row in assessments for ref in row}

    def assesses(job: Any) -> bool:
        target = job.execution_target or {}
        return target.get("workflow_slug") == REVIEW_WORKFLOW_SLUG or bool(
            {target.get("binding_id"), target.get("workflow_id")} & assessment_refs)

    view = project_workspace_app_view(
        workspace, entrypoints, jobs, agents,
        flow_service_keys=flow_service_keys,
        plumbing_keys={f"automation:{job.job_id}" for job in jobs if assesses(job)}
        | {f"workflow:{binding_id}" for binding_id, _ in assessments},
        locale=locale,
    )
    if not include_catalog:
        return view
    workflows = (await db.execute(select(WorkflowDefinition).where(
        WorkflowDefinition.entity_id == user.entity_id, WorkflowDefinition.status == "active",
        WorkflowDefinition.is_active.is_(True), WorkflowDefinition.trigger_type != "internal",
        or_(WorkflowDefinition.workspace_id.is_(None), exists(select(Workspace.id).where(
            Workspace.id == WorkflowDefinition.workspace_id, Workspace.entity_id == user.entity_id,
            Workspace.deleted_at.is_(None),
        ))),
        ~exists(select(WorkflowTemplateInstallation.id).where(
            WorkflowTemplateInstallation.entity_id == user.entity_id,
            WorkflowTemplateInstallation.workflow_id == WorkflowDefinition.id,
            WorkflowTemplateInstallation.source_type == "workspace_blueprint",
            WorkflowTemplateInstallation.installation_metadata["internal"].as_boolean().is_(True),
        )),
    ).order_by(WorkflowDefinition.name, WorkflowDefinition.id))).scalars().all()
    readable = await readable_resource_ids(
        db, descriptors=[ResourceDescriptor.from_row(row, ResourceType.WORKFLOW) for row in workflows],
        entity_id=user.entity_id, user_id=user.id, role=user.role,
    )
    attached_ids = {entry.workflow_id for entry in entrypoints}
    from packages.core.services.workflow_service import validate_workflow_definition
    view["attachable_workflow_options"] = [
        {"workflow_id": row.id, "title": row.name, "description": row.description or "",
         "inputs": [item for item in workflow_run_inputs(row, locale=locale) if not item.get("hidden")]}
        for row in workflows if row.id in readable and row.id not in attached_ids
        and validate_workflow_definition(row)["valid"]
    ]
    return view


_CHOICE_FIELDS = {"value", "label", "description", "group", "image", "sample", "presets"}
# A catalog of templates (song styles, deck types) is browsed by group and search.
MAX_CHOICES_PER_INPUT = 60


def _normalize_app_view_choices(value: Any) -> dict[str, dict[str, list[dict[str, Any]]]]:
    """A Workspace's own templates for an entry's free-text inputs (a house style,
    a recurring character): offered first among the input's suggestions, each able
    to set other inputs of the entry the way ``presets`` do. A template may name
    its ``group`` (browsed by group) and a ``sample`` to hear (an audio URL)."""
    if not isinstance(value, dict) or len(value) > 50:
        raise ValueError("app_view.choices must be an object with at most 50 entries")
    normalized: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for key, fields in value.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError("app_view.choices contains an invalid action key")
        if not isinstance(fields, dict) or len(fields) > 10:
            raise ValueError("app_view.choices maps an entry to at most 10 inputs")
        for field, options in fields.items():
            if not isinstance(field, str) or not _FIELD_KEY.fullmatch(field):
                raise ValueError("app_view.choices contains an invalid input key")
            if not isinstance(options, list) or not 1 <= len(options) <= MAX_CHOICES_PER_INPUT:
                raise ValueError(f"app_view.choices offers 1 to {MAX_CHOICES_PER_INPUT} choices per input")
            for option in options:
                if not isinstance(option, dict) or set(option) - _CHOICE_FIELDS:
                    raise ValueError("an app_view choice has only value, label, description, group, image, sample "
                                     "and presets")
                text = option.get("value")
                if not isinstance(text, str) or not text.strip() or len(text) > 4000:
                    raise ValueError("an app_view choice needs a value of at most 4000 characters")
                for name, limit in (("label", 80), ("description", 300), ("group", 40), ("image", 2000),
                                    ("sample", 2000)):
                    if option.get(name) is not None and (not isinstance(option[name], str) or len(option[name]) > limit):
                        raise ValueError(f"an app_view choice {name} is text of at most {limit} characters")
                for name in ("image", "sample"):
                    url = (option.get(name) or "").strip()
                    if url and not (re.match(r"^/(?!/)", url) or url.lower().startswith("https://")):
                        raise ValueError(f"an app_view choice {name} is a site path or an https URL")
                presets = option.get("presets") or {}
                if not isinstance(presets, dict) or len(presets) > 10 or any(
                    not isinstance(name, str) or not _FIELD_KEY.fullmatch(name)
                    or not isinstance(text, str) or len(text) > 4000
                    for name, text in presets.items()
                ):
                    raise ValueError("app_view choice presets map input keys to text of at most 4000 characters")
            normalized.setdefault(key, {})[field] = [
                {name: option[name] for name in ("value", "label", "description", "group", "image", "sample", "presets")
                 if option.get(name)}
                for option in options
            ]
    return normalized


def _input_with_choices(item: dict[str, Any], options: list[dict[str, Any]]) -> dict[str, Any]:
    """Put a Workspace's own choices first among a free-text input's suggestions.

    A fixed list (``enum``) is checked against the Flow's definition when the run
    starts, so only free text, where any value is valid, takes them."""
    schema = dict(item.get("schema") or {"type": item.get("type") or "string"})
    if schema.get("enum") is not None or schema.get("type", "string") != "string":
        return item
    examples = list(schema.get("examples") or [])

    def column(name: str, fill: Any) -> list[Any]:
        values = list(schema.get(name) or [])
        return (values + [fill] * len(examples))[:len(examples)]

    schema["examples"] = [option["value"] for option in options] + examples
    schema["x-example-labels"] = [option.get("label", "") for option in options] + column("x-example-labels", "")
    for field, name in (("image", "x-example-images"), ("group", "x-example-groups"), ("sample", "x-example-samples")):
        if any(option.get(field) for option in options) or schema.get(name):
            schema[name] = [option.get(field, "") for option in options] + column(name, "")
    if any(option.get("presets") for option in options) or schema.get("x-example-presets"):
        schema["x-example-presets"] = [option.get("presets") for option in options] + column("x-example-presets", None)
    return {**item, "schema": schema}


def _normalize_app_view_page(value: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """The page's own fields: those every config has, and those present only when set."""
    unknown = set(value) - set(APP_VIEW_FIELDS)
    if unknown:
        raise ValueError(f"unsupported app_view fields: {', '.join(sorted(unknown))}; the page takes {', '.join(APP_VIEW_FIELDS)}")
    if value.get("version", 1) != 1:
        raise ValueError("app_view.version must be 1")
    visual = value.get("visual", "auto")
    if visual not in ("auto", "image", "motion", "plain"):
        raise ValueError("app_view.visual must be auto, image, motion, or plain")
    layout = value.get("layout", "feature")
    if layout not in ("feature", "cover", "list"):
        raise ValueError("app_view.layout must be feature, cover, or list")
    open_as = value.get("open_as")
    if open_as is not None and open_as not in APP_OPEN_AS:
        raise ValueError(f"app_view.open_as must be one of: {', '.join(APP_OPEN_AS)}")
    cover_image_url = value.get("cover_image_url") or None
    theme = normalize_app_theme(value.get("theme"))
    motion_cover_url = value.get("motion_cover_url")
    motion_cover_source_url = value.get("motion_cover_source_url")
    if motion_cover_url == "":
        motion_cover_url = None
    if motion_cover_source_url == "":
        motion_cover_source_url = None
    for field, url in (("cover_image_url", cover_image_url), ("motion_cover_url", motion_cover_url),
                       ("motion_cover_source_url", motion_cover_source_url)):
        if url is not None and (not isinstance(url, str) or len(url) > 2048 or not ((url.startswith("/") and not url.startswith("//")) or url.startswith("https://"))):
            raise ValueError(f"app_view.{field} must be a relative or HTTPS URL")
    if motion_cover_url and not motion_cover_source_url:
        raise ValueError("app_view.motion_cover_source_url is required with a motion cover")

    eyebrow = value.get("eyebrow")
    if eyebrow is not None and (not isinstance(eyebrow, str) or len(eyebrow.strip()) > 60):
        raise ValueError("app_view.eyebrow must be at most 60 characters")
    title = value.get("title")
    if title is not None and (not isinstance(title, str) or not 1 <= len(title.strip()) <= 100):
        raise ValueError("app_view.title must be 1–100 characters")
    description = value.get("description")
    if description is not None and (not isinstance(description, str) or len(description.strip()) > 600):
        raise ValueError("app_view.description must be at most 600 characters")

    hero = value.get("hero")
    if hero is not None and (not isinstance(hero, str) or not _ACTION_KEY.fullmatch(hero)):
        raise ValueError(f"app_view.hero must be a valid action key, not {_shown(hero)}: use {_ACTION_KEY_FORMS}")

    def keys(field: str) -> list[str]:
        items = value.get(field, [])
        if not isinstance(items, list) or len(items) > 50:
            raise ValueError(f"app_view.{field} must be an array of at most 50 action keys")
        if any(not isinstance(item, str) or not _ACTION_KEY.fullmatch(item) for item in items):
            raise ValueError(f"app_view.{field} contains an invalid action key: use {_ACTION_KEY_FORMS}, not {_shown(items)}")
        if len(set(items)) != len(items):
            raise ValueError(f"app_view.{field} contains duplicate action keys: {_shown(items)}")
        return items

    labels = value.get("labels", {})
    if not isinstance(labels, dict) or len(labels) > 50:
        raise ValueError("app_view.labels must be an object with at most 50 entries")
    for key, label in labels.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError(f"app_view.labels contains an invalid action key {_shown(key)}: use {_ACTION_KEY_FORMS}")
        if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
            raise ValueError("app_view labels must be 1–80 characters")
    descriptions = value.get("descriptions", {})
    if not isinstance(descriptions, dict) or len(descriptions) > 50:
        raise ValueError("app_view.descriptions must be an object with at most 50 entries")
    for key, text in descriptions.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError(f"app_view.descriptions contains an invalid action key {_shown(key)}: use {_ACTION_KEY_FORMS}")
        if not isinstance(text, str) or len(text.strip()) > 240:
            raise ValueError("app_view action descriptions must be at most 240 characters")

    forms = value.get("forms", {})
    if not isinstance(forms, dict) or len(forms) > 50:
        raise ValueError("app_view.forms must be an object with at most 50 entries")
    normalized_forms: dict[str, list[dict[str, Any]]] = {}
    for key, fields in forms.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError(f"app_view.forms must reference a valid action, not {_shown(key)}: use {_ACTION_KEY_FORMS}")
        if not isinstance(fields, list) or len(fields) > 10:
            raise ValueError("app_view form must contain at most 10 fields")
        normalized_fields = []
        seen: set[str] = set()
        for field in fields:
            if not isinstance(field, dict) or set(field) - {
                "key", "label", "type", "required", "placeholder", "options", "option_labels", "option_images",
            }:
                raise ValueError(
                    f"app_view.forms[{key}] field takes key, label, type, required, placeholder, options, "
                    f"option_labels and option_images, not {_shown(field)}"
                )
            field_key = field.get("key")
            label = field.get("label")
            field_type = field.get("type", "text")
            placeholder = field.get("placeholder", "")
            options = field.get("options", [])
            if not isinstance(field_key, str) or not _FIELD_KEY.fullmatch(field_key) or field_key in seen:
                raise ValueError(f"app_view.forms[{key}] field keys must be unique identifiers, not {_shown(field_key)}")
            if not isinstance(label, str) or not 1 <= len(label.strip()) <= 80:
                raise ValueError("app_view form labels must be 1–80 characters")
            if field_type not in _FORM_FIELD_TYPES:
                raise ValueError(
                    f"app_view.forms[{key}] field type must be one of {', '.join(_FORM_FIELD_TYPES)}, "
                    f"not {_shown(field_type)}"
                )
            if not isinstance(field.get("required", False), bool):
                raise ValueError("app_view form required must be boolean")
            if not isinstance(placeholder, str) or len(placeholder) > 160:
                raise ValueError("app_view form placeholder must be at most 160 characters")
            if not isinstance(options, list) or len(options) > 20 or any(
                not isinstance(option, str) or not 1 <= len(option.strip()) <= 80 for option in options
            ):
                raise ValueError("app_view form options must be short labels")
            if field_type in ("select", "picture_choice") and not options:
                raise ValueError(f"app_view {field_type} fields need options")
            if field_type != "picture_choice" and (field.get("option_labels") or field.get("option_images")):
                raise ValueError("app_view option_labels and option_images belong to picture_choice fields only")
            normalized = {
                "key": field_key, "label": label.strip(), "type": field_type,
                "required": field.get("required", False), "placeholder": placeholder.strip(),
                "options": [option.strip() for option in options] if field_type in ("select", "picture_choice") else [],
            }
            if field_type == "picture_choice":
                normalized.update(_picture_choice_options(key, field_key, normalized["options"], field))
            seen.add(field_key)
            normalized_fields.append(normalized)
        normalized_forms[key] = normalized_fields

    targets = value.get("targets", {})
    if not isinstance(targets, dict) or len(targets) > 50:
        raise ValueError("app_view.targets must be an object with at most 50 entries")
    for key, target in targets.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError(f"app_view.targets contains an invalid action key {_shown(key)}: use {_ACTION_KEY_FORMS}")
        if not isinstance(target, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", target):
            raise ValueError(f"app_view.targets[{key}] is an invalid target {_shown(target)}: use workflow:<id>, agent:<id>, agent:default or automation:<id>")

    entries = value.get("entries", {})
    if not isinstance(entries, dict) or len(entries) > 50:
        raise ValueError(f"app_view.entries must be an object with at most 50 entries, each action:<id>: {{\"title\": ...}} with its target in targets, not {_shown(entries)}")
    for key, entry in entries.items():
        if not isinstance(key, str) or not key.startswith("action:") or not _ACTION_KEY.fullmatch(key):
            raise ValueError(f"app_view.entries keys must be action:<entry id>, not {_shown(key)}: entries are new entries of your own; name other entries in labels and order")
        if (not isinstance(entry, dict) or set(entry) != {"title"}
                or not isinstance(entry["title"], str) or not 1 <= len(entry["title"].strip()) <= 80):
            raise ValueError(f"app_view.entries[{key}] must be {{\"title\": \"<1–80 characters>\"}} with its target in targets, not {_shown(entry)}")
        if key in targets and not targets[key].startswith(("agent:", "workflow:", "automation:")):
            raise ValueError(f"app_view.targets[{key}]: custom App entries require a typed Agent, Workflow, or Automation target (agent:, workflow: or automation:), not {_shown(targets[key])}")
    prompts = value.get("prompts", {})
    if not isinstance(prompts, dict) or len(prompts) > 50:
        raise ValueError("app_view.prompts must be an object with at most 50 commands")
    for key, prompt in prompts.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError(f"app_view.prompts contains an invalid action key {_shown(key)}: use {_ACTION_KEY_FORMS}")
        if not isinstance(prompt, str) or len(prompt) > 8000:
            raise ValueError("App Agent commands must be strings of at most 8000 characters")
    # Presets fill an entry's Flow inputs before the person edits them, so one
    # Flow can appear as several entries (a pitch deck, a training deck, ...).
    presets = value.get("presets", {})
    if not isinstance(presets, dict) or len(presets) > 50:
        raise ValueError("app_view.presets must be an object with at most 50 entries")
    for key, values in presets.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError(f"app_view.presets contains an invalid action key {_shown(key)}: use {_ACTION_KEY_FORMS}")
        if not isinstance(values, dict) or len(values) > 10 or any(
            not isinstance(field, str) or not _FIELD_KEY.fullmatch(field)
            or not isinstance(text, str) or len(text) > 4000
            for field, text in values.items()
        ):
            raise ValueError(f"app_view.presets[{key}]: presets map up to 10 input keys to text of at most 4000 characters, not {_shown(values)}")
    # An entry can ask for a Flow input the Flow keeps hidden elsewhere (the
    # series a "next episode" entry continues), so one Flow serves both.
    asks = value.get("asks", {})
    if not isinstance(asks, dict) or len(asks) > 50:
        raise ValueError("app_view.asks must be an object with at most 50 entries")
    for key, fields in asks.items():
        if (not isinstance(key, str) or not _ACTION_KEY.fullmatch(key) or not isinstance(fields, list)
                or not 1 <= len(fields) <= 10
                or any(not isinstance(field, str) or not _FIELD_KEY.fullmatch(field) for field in fields)):
            raise ValueError(f"app_view.asks maps an entry to the 1–10 hidden Flow inputs it asks for, not {_shown(key)}: {_shown(fields)}")

    icons = value.get("icons", {})
    if not isinstance(icons, dict) or len(icons) > 50:
        raise ValueError("app_view.icons must be an object with at most 50 entries")
    for key, icon in icons.items():
        if not isinstance(key, str) or not _ACTION_KEY.fullmatch(key):
            raise ValueError(f"app_view.icons contains an invalid action key {_shown(key)}: use {_ACTION_KEY_FORMS}")
        if icon not in APP_ENTRY_ICONS:
            raise ValueError(f"app_view icons must be one of: {', '.join(APP_ENTRY_ICONS)}")
    choices = _normalize_app_view_choices(value.get("choices", {}))

    page = {
        "version": 1,
        "eyebrow": eyebrow.strip() if eyebrow is not None else None,
        "title": title.strip() if title is not None else None,
        "description": description.strip() if description is not None else None,
        "hero": hero,
        "order": keys("order"),
        "hidden": keys("hidden"),
        "labels": {key: label.strip() for key, label in labels.items()},
        "descriptions": {key: text.strip() for key, text in descriptions.items()},
        "forms": normalized_forms,
        "targets": targets,
        "entries": {key: {"title": entry["title"].strip()} for key, entry in entries.items()},
        "prompts": prompts,
        "presets": {key: dict(values) for key, values in presets.items() if values},
        "asks": {key: list(dict.fromkeys(fields)) for key, fields in asks.items()},
        "choices": choices,
        "visual": visual,
        "layout": layout,
        "motion_cover_url": motion_cover_url,
        "motion_cover_source_url": motion_cover_source_url,
    }
    # Like i18n, present only when set, so configs without icons keep their form.
    optional: dict[str, Any] = {}
    if icons:
        optional["icons"] = dict(icons)
    if open_as is not None:
        optional["open_as"] = open_as
    if cover_image_url:
        optional["cover_image_url"] = cover_image_url
    if theme:
        optional["theme"] = theme
    return page, optional


def _picture_choice_options(form_key: str, field_key: str, options: list[str], field: dict[str, Any]) -> dict[str, Any]:
    """Labels and pictures for a picture choice: one picture per option, in the same order."""
    if len(set(options)) != len(options):
        raise ValueError(f"app_view.forms[{form_key}].{field_key} options must be unique")
    labels = field.get("option_labels") or options
    images = field.get("option_images")
    if not isinstance(labels, list) or len(labels) != len(options) or any(
        not isinstance(label, str) or not 1 <= len(label.strip()) <= 80 for label in labels
    ):
        raise ValueError(f"app_view.forms[{form_key}].{field_key} option_labels must match options, as short labels")
    if not isinstance(images, list) or len(images) != len(options) or any(
        not isinstance(image, str) or not _PICTURE_PATH.fullmatch(image)
        or any(part in {"", ".", ".."} for part in image.split("/")[2:])
        for image in images
    ):
        raise ValueError(
            f"app_view.forms[{form_key}].{field_key} option_images must give one /blueprints/… picture per option"
        )
    return {"option_labels": [label.strip() for label in labels], "option_images": list(images)}


def normalize_app_view_config(value: Any) -> dict[str, Any]:
    """Validate the small page config before resolving its live targets.

    The page's fields, its sections and its data views are read apart, so one
    pass raises an ``AppViewContractError`` naming the problems of all three.
    """
    if value is None:
        return {"version": 1, "eyebrow": None, "title": None, "description": None, "hero": None,
                "order": [], "hidden": [], "labels": {}, "descriptions": {}, "forms": {}, "targets": {},
                "entries": {}, "prompts": {}, "presets": {}, "asks": {}, "choices": {},
                "visual": "auto", "layout": "feature", "motion_cover_url": None,
                "motion_cover_source_url": None, "data_sources": {}, "components": [], "sections": None}
    if not isinstance(value, dict):
        raise ValueError(f"app_view must be an object, not {_shown(value)}")
    problems: list[str] = []

    def read(normalize: Any, *args: Any) -> Any:
        try:
            return normalize(*args)
        except ValueError as exc:
            problems.extend(app_view_problems(exc))
            return None

    page = read(_normalize_app_view_page, value)
    sections = read(_normalize_sections, value.get("sections"))
    data_views = read(normalize_app_view_data_contract, value.get("data_sources"), value.get("components"))
    if problems:
        raise AppViewContractError(problems)
    fields, optional = page
    data_sources, components = data_views
    normalized = {**fields, "data_sources": data_sources, "components": components, "sections": sections, **optional}
    # The page's text in other languages: same-typed replacements of text it
    # already has, never of keys, targets or data (see viewer_language).
    if value.get("i18n") is not None:
        if not isinstance(value["i18n"], dict):
            raise ValueError("app_view.i18n must map a language to translated page text")
        overlays = sanitize_i18n({**normalized, "i18n": value["i18n"]}).get("i18n")
        if overlays:
            if len(json.dumps(overlays, ensure_ascii=False)) > 100_000:
                raise ValueError("app_view.i18n is too large")
            normalized["i18n"] = overlays
    return normalized


def app_lists_scheduled_job(job: Any) -> bool:
    """Whether a Workspace schedule is something its people run from the App.

    Agent initiatives and the schedules the platform derives at runtime
    (Strategist review, outcome evaluation, chat insight extraction, goal
    measurement) are how the Workspace runs itself, not entries of its page.
    """
    from packages.core.constants.blueprints import is_runtime_derived_scheduled_job

    return ((getattr(job, "execution_target", None) or {}).get("managed_by") != "agent_initiative"
            and not is_runtime_derived_scheduled_job(job_id=job.job_id, execution_type=getattr(job, "execution_type", None)))


def app_target_kind_and_id(key: str, target: str) -> tuple[str, str]:
    """Typed targets work for every entry; retain the original bare-ID config."""
    for prefix, kind in (("agent:", "chat"), ("workflow:", "workflow"), ("automation:", "automation")):
        if target.startswith(prefix):
            return kind, target[len(prefix):]
    source, source_id = key.split(":", 1)
    return {"service": "chat", "action": "chat", "workflow": "workflow", "automation": "automation"}[source], target or source_id


def app_cover(workspace_cover_url: str | None, config: dict[str, Any],
              starter_cover_url: str | None) -> tuple[str | None, str]:
    """The picture an App opens under, and whose it is.

    The Workspace's own cover wins; then the one its App config names, which
    travels with the Blueprint (a motion cover is made for it); then the
    Blueprint's Marketplace artwork.
    """
    if workspace_cover_url:
        return workspace_cover_url, "workspace"
    if config.get("cover_image_url"):
        return config["cover_image_url"], "app"
    if starter_cover_url:
        return starter_cover_url, "starter"
    return None, "none"


def project_workspace_app_view(
    workspace: Any, entrypoints: list[Any], automations: list[Any] | None = None,
    agents: list[dict[str, str]] | None = None,
    starter_cover_url: str | None = None,
    *,
    flow_service_keys: set[str] | None = None,
    plumbing_keys: set[str] | None = None,
    locale: str | None = None,
) -> dict[str, Any]:
    """Derive the visible actions afresh, so removed bindings cannot linger.

    Entries that ``order`` and ``hidden`` do not name follow the suggested
    default (see ``suggested_hidden_actions``). The effective list is reported
    as ``hidden`` so the settings page shows it and saving keeps it.

    The page reads in the viewer's ``locale``; the returned ``config`` stays
    as authored, translations included, because the settings page saves it.
    """
    model = workspace.operating_model if isinstance(workspace.operating_model, dict) else {}
    try:
        stored = normalize_app_view_config(model.get("app_view"))
    except ValueError:
        stored = normalize_app_view_config(None)
    config = localized(stored, locale)

    actions: list[dict[str, Any]] = []
    flows_by_id = {entry.binding_id: entry for entry in entrypoints}
    agents = agents or []
    agents_by_id = {agent["id"]: agent for agent in agents}
    agents_by_service = {agent.get("service_key"): agent for agent in agents if agent.get("service_key")}
    automations = automations or []
    def flow_inputs(entry: Any, *, with_hidden: bool = False) -> list[dict[str, Any]]:
        # Hidden inputs are never asked, but an entry's preset can still set them
        # (e.g. which kind of deck an entry makes), so entries carry them flagged.
        return [
            {field: item[field] for field in (
                "key", "label", "type", "required", "placeholder", "description", "default", "schema", "hidden",
                "step", "step_label",
            ) if field in item}
            for item in entry.run_inputs if with_hidden or not item.get("hidden")
        ]
    def automation_target(job: Any) -> dict[str, Any]:
        target = getattr(job, "execution_target", None) or {}
        if not isinstance(target, dict):
            target = {}
        binding_id = target.get("binding_id")
        target_flow = flows_by_id.get(binding_id) if isinstance(binding_id, str) else None
        if target_flow is None and isinstance(target.get("workflow_id"), str):
            target_flow = next((entry for entry in entrypoints if entry.workflow_id == target["workflow_id"]), None)
        return {
            "job_id": job.job_id, "title": job.name or job.job_id,
            "description": target_flow.description if target_flow else "",
            "enabled": bool(job.enabled), "last_status": job.last_status,
            **{field: getattr(job, field, None) for field in (
                "job_type", "schedule_kind", "every_seconds", "run_at", "cron_expr", "timezone",
            )},
            "instructions": getattr(job, "payload_message", None),
            "target_title": target_flow.title if target_flow else None,
            "target_workflow_id": target_flow.workflow_id if target_flow else None,
        }

    automation_targets = [(job, automation_target(job)) for job in automations]
    automation_options = [target for _, target in automation_targets]
    jobs_by_id = {job["job_id"]: job for job in automation_options}
    has_owned_automation_component = any(
        config["data_sources"].get(block["source"], {}).get("type") == "owned_automations"
        for component in config["components"]
        for block in component["blocks"]
    )
    automatic_automation_options = [
        target for job, target in automation_targets
        if not has_owned_automation_component or getattr(job, "owner_project_id", None) is None
    ]
    for entry in entrypoints:
        key = f"workflow:{entry.binding_id}"
        actions.append({
            "key": key, "kind": "workflow", "binding_id": entry.binding_id, "workflow_id": entry.workflow_id,
            "title": config["labels"].get(key) or entry.title, "source_title": entry.title,
            "source_description": entry.description,
            "description": config["descriptions"].get(key, entry.description),
            "placeholder": entry.placeholder, "inputs": flow_inputs(entry, with_hidden=True),
            # Present only when the Flow declares rules, so other actions keep their form.
            **({"input_rules": [dict(rule) for rule in entry.input_rules]} if getattr(entry, "input_rules", ()) else {}),
        })

    for service in model.get("services") or []:
        if not isinstance(service, dict):
            continue
        service_key = str(service.get("key") or service.get("service_key") or "").strip()
        name = str(service.get("name") or "").strip()
        if not service_key or not name or not _ACTION_KEY.fullmatch(f"service:{service_key}"):
            continue
        key = f"service:{service_key}"
        default_agent = agents_by_service.get(service_key)
        action = {
            "key": key, "kind": "chat", "title": config["labels"].get(key) or name, "source_title": name,
            "source_description": str(service.get("description") or "").strip(),
            "description": config["descriptions"].get(key, str(service.get("description") or "").strip()),
            "prompt": config["prompts"].get(key) or name, "inputs": config["forms"].get(key, []),
        }
        if default_agent:
            action.update(agent_id=default_agent["id"], agent_name=default_agent["name"],
                          default_agent_id=default_agent["id"], default_agent_name=default_agent["name"])
        actions.append(action)

    for job in automatic_automation_options:
        key = f"automation:{job['job_id']}"
        actions.append({**job, "key": key, "kind": "automation", "source_title": job["title"],
                        "source_description": job["description"],
                        "title": config["labels"].get(key) or job["title"],
                        "description": config["descriptions"].get(key, job["description"])})

    # Standalone App entries own only presentation and a runner reference.
    # They never create a Service, Agent, scheduled job, or Workflow definition.
    for key, entry in config["entries"].items():
        actions.append({
            "key": key, "kind": "chat", "source_title": entry["title"], "source_description": "",
            "title": config["labels"].get(key) or entry["title"],
            "description": config["descriptions"].get(key, ""),
            "prompt": config["prompts"].get(key) or entry["title"],
            "inputs": config["forms"].get(key, []), "available": False,
        })

    for action in actions:
        key = action["key"]
        target = config["targets"].get(key)
        if not target:
            continue
        kind, target_id = app_target_kind_and_id(key, target)
        # Remove the previous runner's fields so previews and execution cannot
        # accidentally use a stale Agent, binding, or scheduled job.
        for field in ("agent_id", "agent_name", "binding_id", "workflow_id", "job_id", "prompt", "placeholder",
                      "enabled", "last_status", "job_type", "schedule_kind", "every_seconds", "run_at",
                      "cron_expr", "timezone", "instructions", "target_title", "target_workflow_id", "input_rules"):
            action.pop(field, None)
        action["kind"] = kind
        action["inputs"] = []
        if kind == "chat":
            agent = agents_by_id.get(action.get("default_agent_id") if target_id == "default" else target_id)
            action.update(prompt=config["prompts"].get(key) or action["source_title"], inputs=config["forms"].get(key, []),
                          description=config["descriptions"].get(key, action["source_description"]),
                          available=target_id == "default" or agent is not None)
            if agent:
                action.update(agent_id=agent["id"], agent_name=agent["name"])
        elif kind == "workflow":
            selected = flows_by_id.get(target_id)
            action.update(binding_id=selected.binding_id if selected else None,
                          workflow_id=selected.workflow_id if selected else None,
                          description=config["descriptions"].get(key, selected.description if selected else ""),
                          placeholder=selected.placeholder if selected else "",
                          inputs=flow_inputs(selected, with_hidden=True) if selected else [],
                          available=selected is not None)
            if getattr(selected, "input_rules", ()):
                action["input_rules"] = [dict(rule) for rule in selected.input_rules]
        else:
            job = jobs_by_id.get(target_id)
            action.update({field: value for field, value in (job or {}).items() if field not in ("title", "description")})
            action.update(available=job is not None,
                          description=config["descriptions"].get(key, job["description"] if job else ""))

    config["hidden"] = (config["hidden"] + suggested_hidden_actions(
        actions, flow_service_keys=flow_service_keys or set(), plumbing_keys=plumbing_keys or set(),
        named=set(config["order"]) | set(config["hidden"]), hidden=config["hidden"],
    ))[:50]
    for action in actions:
        preset = config["presets"].get(action["key"]) or {}
        asked = set(config["asks"].get(action["key"]) or [])
        if action.get("kind") == "workflow":
            # The preset wins over the Flow's own default wherever the form reads one.
            # A hidden input travels only where a preset sets it, and is asked
            # only where the entry asks for it.
            action["inputs"] = [{
                **item, "default": preset[item["key"]],
                **({"schema": {**item["schema"], "default": preset[item["key"]]}} if isinstance(item.get("schema"), dict) else {}),
            } if item.get("key") in preset else {**item, "hidden": False, "required": True} if item.get("key") in asked else item
                for item in action.get("inputs") or []
                if not item.get("hidden") or item.get("key") in preset or item.get("key") in asked]
            choices = config["choices"].get(action["key"]) or {}
            action["inputs"] = [_input_with_choices(item, choices[item["key"]]) if item.get("key") in choices else item
                                for item in action["inputs"]]
    # An entry carries an icon only when the page chose one; the web guesses the rest.
    for action in actions:
        if action["key"] in (config.get("icons") or {}):
            action["icon"] = config["icons"][action["key"]]
    visible = [action for action in actions if action["key"] not in config["hidden"]]
    positions = {key: index for index, key in enumerate(config["order"])}
    visible.sort(key=lambda action: positions.get(action["key"], len(positions) + actions.index(action)))
    hero = next((action["key"] for action in visible if action["key"] == config["hero"]), None)
    default_hero = next((action["key"] for action in visible
                         if action["kind"] == "workflow" and action.get("available") is not False), None)
    sections = []
    for section in app_view_sections(config):
        section["implicit"] = config["sections"] is None
        source = section["source"] or ""
        if source.startswith("workflow:"):
            # Like action targets, a removed binding leaves an unavailable
            # section instead of silently widening to every Workspace run.
            flow = flows_by_id.get(source.removeprefix("workflow:"))
            section.update(available=flow is not None, source_title=flow.title if flow else None)
        else:
            section.update(available=True, source_title=None)
        sections.append(section)
    cover_image_url, cover_source = app_cover(getattr(workspace, "cover_image_url", None), config, starter_cover_url)
    return {
        "workspace_id": workspace.id,
        "eyebrow": config["eyebrow"],
        "title": config["title"] if config["title"] is not None else workspace.name,
        "description": config["description"] if config["description"] is not None else (workspace.primary_work or workspace.operating_context or ""),
        "status": workspace.status,
        "cover_image_url": cover_image_url,
        "cover_source": cover_source,
        "hero": hero if config["hero"] else default_hero,
        "actions": visible,
        "available_actions": actions,
        "sections": sections,
        "blocks": app_block_catalog(),
        "workflow_options": [
            {"binding_id": entry.binding_id, "workflow_id": entry.workflow_id,
             "title": entry.title, "description": entry.description,
             "placeholder": entry.placeholder, "inputs": flow_inputs(entry)}
            for entry in entrypoints
        ],
        "automation_options": automation_options,
        "agent_options": [{"id": agent["id"], "name": agent["name"]} for agent in agents],
        # Components intentionally retain only their declaration here. Their
        # workspace-scoped rows are fetched through the bounded read endpoints.
        "components": config["components"],
        "config": {**stored, "hidden": config["hidden"]},
    }
