"""Native file capabilities shared by discovery, validation and execution."""

from __future__ import annotations

from pathlib import PurePosixPath

from packages.core.contracts.audio_generation import (
    AudioGenerationFormat,
    GenerateFileKind,
    StringValueEnum,
)

EXCEL_MAX_ROW = 1048576
EXCEL_MAX_COLUMN = 16384
EXCEL_MAX_CELL_CHARS = 32767
EXCEL_MAX_SHEET_TITLE_CHARS = 31


class FilePatchOperation(StringValueEnum):
    TEXT_REPLACE = "replace_text"
    TEXT_SET = "text.set"
    JSON_ADD = "json.add"
    JSON_REPLACE = "json.replace"
    JSON_REMOVE = "json.remove"
    CELL_SET = "set_cell"
    CELL_FORMAT = "cell.format"
    ROW_UPDATE = "update_row"
    ROW_APPEND = "append_row"
    SHEET_ADD = "add_sheet"
    SHEET_DELETE = "sheet.delete"
    SHEET_REORDER = "sheet.reorder"
    SHEET_RENAME = "sheet.rename"
    SHEET_FORMAT = "sheet.format"
    MERGE_SET = "merge.set"
    MERGE_CLEAR = "merge.clear"
    VALIDATION_INSERT = "validation.insert"
    VALIDATION_FORMAT = "validation.format"
    VALIDATION_DELETE = "validation.delete"
    CONDITIONAL_FORMAT_INSERT = "conditional_format.insert"
    CONDITIONAL_FORMAT_FORMAT = "conditional_format.format"
    CONDITIONAL_FORMAT_DELETE = "conditional_format.delete"
    PARAGRAPH_INSERT = "paragraph.insert"
    PARAGRAPH_DELETE = "paragraph.delete"
    PARAGRAPH_FORMAT = "paragraph.format"
    PARAGRAPH_STYLE_INSERT = "paragraph_style.insert"
    PARAGRAPH_STYLE_FORMAT = "paragraph_style.format"
    PARAGRAPH_STYLE_DELETE = "paragraph_style.delete"
    SECTION_INSERT = "section.insert"
    PAGE_SETUP = "page.setup"
    TABLE_INSERT = "table.insert"
    TABLE_FORMAT = "table.format"
    TABLE_DELETE = "table.delete"
    SLIDE_INSERT = "slide.insert"
    SLIDE_DUPLICATE = "slide.duplicate"
    SLIDE_DELETE = "slide.delete"
    SLIDE_FORMAT = "slide.format"
    SLIDE_REORDER = "slide.reorder"
    TEXTBOX_INSERT = "textbox.insert"
    SHAPE_INSERT = "shape.insert"
    SHAPE_DELETE = "shape.delete"
    SHAPE_FORMAT = "shape.format"
    SHAPE_TRANSFORM = "shape.transform"
    SHAPE_REORDER = "shape.reorder"
    SHAPE_GROUP = "shape.group"
    SHAPE_UNGROUP = "shape.ungroup"
    PICTURE_INSERT = "picture.insert"
    PICTURE_DELETE = "picture.delete"
    PICTURE_REPLACE = "picture.replace"
    PICTURE_FORMAT = "picture.format"
    CHART_INSERT = "chart.insert"
    CHART_DELETE = "chart.delete"
    CHART_DATA = "chart.data"
    CHART_FORMAT = "chart.format"
    PAGE_ROTATE = "page.rotate"


class PresentationShapePreset(StringValueEnum):
    RECTANGLE = "rect"
    ROUNDED_RECTANGLE = "roundRect"
    ELLIPSE = "ellipse"
    TRIANGLE = "triangle"
    RIGHT_TRIANGLE = "rtTriangle"
    DIAMOND = "diamond"
    PENTAGON = "pentagon"
    HEXAGON = "hexagon"
    OCTAGON = "octagon"
    PARALLELOGRAM = "parallelogram"
    TRAPEZOID = "trapezoid"
    CHEVRON = "chevron"
    RIGHT_ARROW = "rightArrow"
    LEFT_ARROW = "leftArrow"
    UP_ARROW = "upArrow"
    DOWN_ARROW = "downArrow"
    LEFT_RIGHT_ARROW = "leftRightArrow"
    STAR_4 = "star4"
    STAR_5 = "star5"
    STAR_6 = "star6"
    CROSS = "plus"
    FLOWCHART_PROCESS = "flowChartProcess"
    FLOWCHART_DECISION = "flowChartDecision"
    FLOWCHART_TERMINATOR = "flowChartTerminator"
    FLOWCHART_DATA = "flowChartInputOutput"
    RECTANGULAR_CALLOUT = "wedgeRectCallout"
    ROUNDED_RECTANGULAR_CALLOUT = "wedgeRoundRectCallout"
    OVAL_CALLOUT = "wedgeEllipseCallout"
    LINE = "line"


class OfficeChartType(StringValueEnum):
    COLUMN = "column"
    COLUMN_STACKED = "column_stacked"
    COLUMN_STACKED_100 = "column_stacked_100"
    BAR = "bar"
    BAR_STACKED = "bar_stacked"
    BAR_STACKED_100 = "bar_stacked_100"
    LINE = "line"
    LINE_MARKERS = "line_markers"
    AREA = "area"
    AREA_STACKED = "area_stacked"
    AREA_STACKED_100 = "area_stacked_100"
    PIE = "pie"
    DOUGHNUT = "doughnut"
    SCATTER = "scatter"
    SCATTER_LINES = "scatter_lines"
    SCATTER_LINES_MARKERS = "scatter_lines_markers"
    SCATTER_SMOOTH = "scatter_smooth"
    SCATTER_SMOOTH_MARKERS = "scatter_smooth_markers"
    COMBO_COLUMN_LINE = "combo_column_line"
    STOCK_HLC = "stock_hlc"
    STOCK_OHLC = "stock_ohlc"
    STOCK_VHLC = "stock_vhlc"
    STOCK_VOHLC = "stock_vohlc"


PATCH_OPERATION_ALIASES = {
    "text.replace": FilePatchOperation.TEXT_REPLACE.value,
    "cell.set": FilePatchOperation.CELL_SET.value,
    "row.update": FilePatchOperation.ROW_UPDATE.value,
    "row.append": FilePatchOperation.ROW_APPEND.value,
    "sheet.add": FilePatchOperation.SHEET_ADD.value,
}

TEXT_FILE_TYPES = frozenset(
    {
        "txt",
        "md",
        "markdown",
        "log",
        "html",
        "htm",
        "xml",
        "svg",
        "drawio",
        "mmd",
        "mermaid",
        "css",
        "scss",
        "sass",
        "less",
        "js",
        "mjs",
        "cjs",
        "ts",
        "tsx",
        "jsx",
        "vue",
        "svelte",
        "py",
        "sh",
        "bash",
        "zsh",
        "sql",
        "yaml",
        "yml",
        "toml",
        "ini",
        "cfg",
        "env",
        "jsonl",
        "ndjson",
        "srt",
        "vtt",
        "c",
        "h",
        "cpp",
        "hpp",
        "cc",
        "cs",
        "java",
        "go",
        "rs",
        "rb",
        "php",
        "swift",
        "kt",
        "kts",
        "r",
        "lua",
        "pl",
        "ps1",
        "bat",
        "tex",
        "dockerfile",
        "makefile",
    }
)
JSON_FILE_TYPES = frozenset({"json", "diagram", "diagram.json"})
DELIMITED_FILE_TYPES = frozenset({"csv", "tsv"})
TEXT_CONTENT_TYPES = TEXT_FILE_TYPES | JSON_FILE_TYPES | DELIMITED_FILE_TYPES
OFFICE_FILE_TYPES = frozenset({"docx", "pptx", "xlsx", "xlsm"})
OPERATION_GENERATION_TYPES = frozenset({"docx", "pptx", "xlsx"})
TEMPLATE_OPERATION_GENERATION_TYPES = OPERATION_GENERATION_TYPES | {"xlsm"}
LEGACY_OFFICE_TYPES = frozenset({"doc", "wps", "xls", "et", "ppt", "dps"})
IMAGE_FILE_TYPES = frozenset({"png", "jpg", "jpeg", "gif", "webp", "bmp", "ico", "tiff", "tif", "avif"})
AUDIO_FILE_TYPES = frozenset({"mp3", "wav", "aac", "flac", "m4a", "ogg", "opus", "pcm", "pcm16"})
VIDEO_FILE_TYPES = frozenset({"mp4", "webm", "mov", "avi", "mkv", "m4v"})
ARCHIVE_FILE_TYPES = frozenset({"zip", "tar", "gz", "7z", "rar"})
KNOWN_FILE_TYPES = (
    TEXT_CONTENT_TYPES
    | OFFICE_FILE_TYPES
    | LEGACY_OFFICE_TYPES
    | IMAGE_FILE_TYPES
    | AUDIO_FILE_TYPES
    | VIDEO_FILE_TYPES
    | ARCHIVE_FILE_TYPES
    | {"pdf"}
)


def file_type_from_path(path: str) -> str:
    name = PurePosixPath(str(path).replace("\\", "/")).name.lower()
    if name.endswith(".diagram.json"):
        return "diagram.json"
    return PurePosixPath(name).suffix.lstrip(".") or name


def file_patch_operations(file_type: str) -> tuple[str, ...]:
    op = FilePatchOperation
    if file_type in JSON_FILE_TYPES:
        return (op.TEXT_REPLACE.value, op.JSON_ADD.value, op.JSON_REPLACE.value, op.JSON_REMOVE.value)
    if file_type in DELIMITED_FILE_TYPES:
        return (op.TEXT_REPLACE.value, op.CELL_SET.value, op.ROW_APPEND.value)
    if file_type in TEXT_FILE_TYPES:
        return (op.TEXT_REPLACE.value,)
    return {
        "docx": (op.TEXT_REPLACE.value, op.TEXT_SET.value, op.PARAGRAPH_INSERT.value, op.PARAGRAPH_DELETE.value, op.PARAGRAPH_FORMAT.value, op.PARAGRAPH_STYLE_INSERT.value, op.PARAGRAPH_STYLE_FORMAT.value, op.PARAGRAPH_STYLE_DELETE.value, op.SECTION_INSERT.value, op.PAGE_SETUP.value, op.TEXTBOX_INSERT.value, op.SHAPE_TRANSFORM.value, op.SHAPE_FORMAT.value, op.SHAPE_DELETE.value, op.TABLE_INSERT.value, op.TABLE_FORMAT.value, op.TABLE_DELETE.value, op.CELL_SET.value, op.CELL_FORMAT.value, op.PICTURE_INSERT.value, op.PICTURE_DELETE.value, op.PICTURE_REPLACE.value, op.PICTURE_FORMAT.value),
        "pptx": (op.TEXT_REPLACE.value, op.TEXT_SET.value, op.PARAGRAPH_INSERT.value, op.PARAGRAPH_DELETE.value, op.PARAGRAPH_FORMAT.value, op.PAGE_SETUP.value, op.SHAPE_TRANSFORM.value, op.SHAPE_REORDER.value, op.SHAPE_INSERT.value, op.SHAPE_DELETE.value, op.SHAPE_FORMAT.value, op.SHAPE_GROUP.value, op.SHAPE_UNGROUP.value, op.PICTURE_INSERT.value, op.PICTURE_DELETE.value, op.PICTURE_REPLACE.value, op.PICTURE_FORMAT.value, op.CHART_INSERT.value, op.CHART_DELETE.value, op.CHART_DATA.value, op.CHART_FORMAT.value, op.SLIDE_INSERT.value, op.SLIDE_DUPLICATE.value, op.SLIDE_DELETE.value, op.SLIDE_FORMAT.value, op.SLIDE_REORDER.value, op.TEXTBOX_INSERT.value, op.TABLE_INSERT.value, op.TABLE_FORMAT.value, op.TABLE_DELETE.value, op.CELL_SET.value, op.CELL_FORMAT.value),
        "xlsx": (op.CELL_SET.value, op.ROW_UPDATE.value, op.ROW_APPEND.value, op.SHEET_ADD.value, op.SHEET_DELETE.value, op.SHEET_REORDER.value, op.SHEET_RENAME.value, op.CELL_FORMAT.value, op.SHEET_FORMAT.value, op.MERGE_SET.value, op.MERGE_CLEAR.value, op.VALIDATION_INSERT.value, op.VALIDATION_FORMAT.value, op.VALIDATION_DELETE.value, op.CONDITIONAL_FORMAT_INSERT.value, op.CONDITIONAL_FORMAT_FORMAT.value, op.CONDITIONAL_FORMAT_DELETE.value, op.PAGE_SETUP.value, op.TABLE_INSERT.value, op.TABLE_FORMAT.value, op.TABLE_DELETE.value, op.PICTURE_INSERT.value, op.PICTURE_DELETE.value, op.PICTURE_REPLACE.value, op.PICTURE_FORMAT.value, op.CHART_INSERT.value, op.CHART_DELETE.value, op.CHART_DATA.value, op.CHART_FORMAT.value),
        "xlsm": (op.CELL_SET.value, op.ROW_UPDATE.value, op.ROW_APPEND.value, op.SHEET_ADD.value, op.SHEET_DELETE.value, op.SHEET_REORDER.value, op.SHEET_RENAME.value, op.CELL_FORMAT.value, op.SHEET_FORMAT.value, op.MERGE_SET.value, op.MERGE_CLEAR.value, op.VALIDATION_INSERT.value, op.VALIDATION_FORMAT.value, op.VALIDATION_DELETE.value, op.CONDITIONAL_FORMAT_INSERT.value, op.CONDITIONAL_FORMAT_FORMAT.value, op.CONDITIONAL_FORMAT_DELETE.value, op.PAGE_SETUP.value, op.TABLE_INSERT.value, op.TABLE_FORMAT.value, op.TABLE_DELETE.value, op.PICTURE_INSERT.value, op.PICTURE_DELETE.value, op.PICTURE_REPLACE.value, op.PICTURE_FORMAT.value, op.CHART_INSERT.value, op.CHART_DELETE.value, op.CHART_DATA.value, op.CHART_FORMAT.value),
        "pdf": (op.PAGE_ROTATE.value,),
    }.get(file_type, ())


def file_type_capability(file_type: str) -> dict:
    ext = str(file_type or "").strip().lower().lstrip(".")
    operations = file_patch_operations(ext)
    kind = None
    if ext in TEXT_CONTENT_TYPES or ext == "docx":
        kind = GenerateFileKind.DOCUMENT.value
    elif ext in {"pptx", "xlsx", "xlsm", "pdf"}:
        kind = {
            "pptx": "presentation",
            "xlsx": "spreadsheet",
            "xlsm": "spreadsheet",
            "pdf": "pdf",
        }[ext]
    elif ext in {"png", "jpg", "jpeg", "webp"}:
        kind = GenerateFileKind.IMAGE.value
    elif ext == "mp4":
        kind = GenerateFileKind.VIDEO.value
    elif ext in AudioGenerationFormat.values():
        kind = GenerateFileKind.AUDIO.value
    public_ops = list(operations) + [
        alias for alias, canonical in PATCH_OPERATION_ALIASES.items() if canonical in operations
    ]
    return {
        "file_type": ext,
        "known_type": ext in KNOWN_FILE_TYPES,
        "can_generate": kind is not None,
        "generate_kind": kind,
        "can_patch": bool(operations),
        "can_generate_from_operations": ext in OPERATION_GENERATION_TYPES,
        "can_generate_from_template": ext in TEMPLATE_OPERATION_GENERATION_TYPES,
        "operations": public_ops,
        "requires_conversion": ext in LEGACY_OFFICE_TYPES,
        "generation_note": (
            "Media model availability and returned output format depend on the configured provider."
            if ext in IMAGE_FILE_TYPES | AUDIO_FILE_TYPES | VIDEO_FILE_TYPES
            else None
        ),
    }


def normalize_file_patch_operation(raw: object) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("each patch operation must be an object")
    op = str(raw.get("op") or raw.get("operation") or "").strip().lower().replace("-", "_")
    operation = PATCH_OPERATION_ALIASES.get(op, op)
    if operation not in FilePatchOperation.values():
        raise ValueError(f"Unsupported patch op: {raw.get('op') or raw.get('operation') or '(empty)'}")
    normalized = dict(raw)
    normalized["operation"] = operation
    normalized.pop("op", None)
    return normalized


def normalize_file_operation_resources(
    operations: list[dict], *, normalize_path, visible_path,
) -> tuple[list[dict], list[dict[str, str]]]:
    """Canonicalize approval-bound Knowledge resources used by operations."""
    normalized, resources = [], []
    seen: set[tuple[str, str]] = set()
    for operation in operations:
        patch = dict(operation)
        if patch.get("operation") in {
            FilePatchOperation.PICTURE_INSERT.value,
            FilePatchOperation.PICTURE_REPLACE.value,
        }:
            source = patch.get("source")
            if not isinstance(source, dict) or set(source) != {"path", "expected_sha256"}:
                raise ValueError("picture source requires path and expected_sha256 from read_file")
            raw_path, digest = source["path"], source["expected_sha256"]
            if (
                not isinstance(raw_path, str)
                or raw_path.strip().startswith(("/", "\\"))
                or any(char in raw_path for char in (":", "\x00"))
                or ".." in raw_path.replace("\\", "/").split("/")
            ):
                raise ValueError("picture source.path must be an entity-relative Knowledge path")
            path = normalize_path(raw_path)
            if not path or not visible_path(path):
                raise ValueError("picture source.path must be a user-visible Knowledge path")
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(char not in "0123456789abcdefABCDEF" for char in digest)
            ):
                raise ValueError("picture source.expected_sha256 must be the SHA-256 from read_file")
            source = {"path": path, "expected_sha256": digest.lower()}
            patch["source"] = source
            key = (source["path"], source["expected_sha256"])
            if key not in seen:
                resources.append(source)
                seen.add(key)
        normalized.append(patch)
    return normalized, resources
