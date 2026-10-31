from __future__ import annotations

from typing import Any

from packages.core.contracts.audio_generation import (
    AudioGenerationFormat,
    AudioGenerationPurpose,
    GenerateFileKind,
    REQUESTABLE_NARRATION_VOICE_MODES,
)

_CAPABILITIES = {
    "diagram": "Create an editable .diagram.json canvas from a prompt and save it to Knowledge.",
    "code": "Create a multi-file website/code bundle from params.files; use real extensions like .html/.css/.js, not .txt.",
    "document": "Create from content (.md/.txt/.csv/.json/.diagram.json/.html/.docx/.pptx/.pdf), blank DOCX/PPTX/XLSX operations, template DOCX/PPTX/XLSX/XLSM operations, or convert an existing file using source={path,expected_sha256} plus name and target file_type. Native Office-to-PDF, legacy-to-OOXML and image-to-PNG/JPEG/WebP conversion preserves the original; inspect_file_engine lists supported pairs. Source/operations cannot mix with content/prompt/options. XLSM generation requires template={path,expected_sha256} of the same type.",
    "word_document": "Create DOCX from patch_file operations. For uploaded DOCX edits, use template={path,expected_sha256} from read_file plus operations to create a new Knowledge file; the upload stays unchanged. A PDF is not a DOCX template: read and verify the full PDF, then use the document specialist without template or operations. Convert a resulting DOCX to PDF using source.",
    "pdf": "Convert an existing Office file to PDF with source={path,expected_sha256} and a new name. To edit an uploaded PDF, use prompt referencing its current file path and the PDF specialist; omit source, template and operations. Read and verify the full source PDF.",
    "presentation": "Create PPTX from patch_file operations; without operations, use presentation specialist skills (designed templates by default; set params.mode='free_design' when the user asks, in any words, for no template, a different design on every page or a bespoke look).",
    "spreadsheet": "Create XLSX, or XLSM from a same-type template, using patch_file operations; without operations, use spreadsheet specialist skills.",
    "image": "Convert an existing image (including HEIC/HEIF) to PNG/JPEG/WebP using source={path,expected_sha256}, name and file_type; this preserves resolution and uses no image model. Without source, generate an image with the Account-selected image model and BYOK/platform billing rules.",
    "video": "Generate one clip with the Account-selected video model and BYOK/platform billing rules. Inspect the selected model before choosing clip duration and inputs; segment longer runtimes as needed.",
    "audio": (
        "Generate an audio file with the Account-selected voice/music/SFX "
        "model and platform billing rules. MP3/WAV/etc. is the final artifact format; "
        "the tool handles provider-format conversion internally, so changing the "
        "extension does not recover an upstream provider failure."
    ),
}

VIDEO_DURATION_CHOICES = list(range(4, 31))
VIDEO_RESOLUTION_CHOICES = ["480p", "720p", "1080p"]
VIDEO_SEEDANCE_FAST_RESOLUTION_CHOICES = ["480p", "720p"]
VIDEO_ASPECT_RATIO_CHOICES = ["adaptive", "21:9", "16:9", "4:3", "3:4", "1:1", "9:16"]


GENERATE_FILE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_file",
        "description": "Knowledge files: videos/mp4; Office copy: template+operations; Office/image format conversion: source (no model search needed). Before generating new image/video/audio, call kind=search with params.media_kind (plus params.purpose for audio); use selected_model.generation_schema and pass selected_model.expected_model when generating.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": GenerateFileKind.values(),
                },
                "prompt": {
                    "type": "string",
                    "description": "Media, specialist or diagram instructions; plain kind=document needs complete content.",
                },
                "name": {
                    "type": "string",
                    "description": "Output filename including extension (e.g. report.md); use name, not title.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete kind=document file text, not instructions; Office operations/conversion use other inputs.",
                },
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "items": {"type": "object"},
                    "description": "Office ops; no content/prompt/options. See inspect_file_engine.",
                },
                "template": {
                    "type": "object",
                    "description": "Unchanged read_file Office Knowledge/current-chat upload matching output type. PDF cannot be an Office template.",
                    "properties": {
                        "path": {"type": "string"},
                        "expected_sha256": {"type": "string"},
                    },
                    "required": ["path", "expected_sha256"],
                    "additionalProperties": False,
                },
                "source": {
                    "type": "object",
                    "description": "Unchanged conversion only; no content/prompt/files/options/operations/template/expected_sha256. For PDF edits use prompt with current file path, without source.",
                    "properties": {
                        "path": {"type": "string"},
                        "expected_sha256": {"type": "string"},
                    },
                    "required": ["path", "expected_sha256"],
                    "additionalProperties": False,
                },
                "file_type": {
                    "type": "string",
                },
                "storage_scope": {
                    "type": "string",
                    "enum": ["task", "workspace"],
                    "default": "task",
                    "description": "Task/workspace scope.",
                },
                "approval_token": {
                    "type": "string",
                },
                "expected_sha256": {
                    "type": "string",
                },
                "params": {
                    "type": "object",
                    "properties": {
                        "media_kind": {"type": "string", "enum": ["image", "video", "audio"], "description": "For search: selected model schema and limits."},
                        "expected_model": {"type": "string", "description": "Search result model; fail if changed."},
                        "duration": {
                            "type": "integer",
                            "enum": VIDEO_DURATION_CHOICES,
                            "default": 5,
                            # Kept terse here: the canonical, tested wording
                            # ("Single clip seconds.") lives on the top-level
                            # "duration" property below; this nested alias
                            # only needs its enum to match.
                            "description": "Clip seconds; inspect selected model.",
                        },
                        "resolution": {
                            "type": "string",
                            "enum": VIDEO_RESOLUTION_CHOICES,
                            "default": "720p",
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "enum": VIDEO_ASPECT_RATIO_CHOICES,
                            "default": "16:9",
                        },
                        "first_frame_url": {
                            "type": "string",
                            "description": "Start frame URL.",
                        },
                        "last_frame_url": {
                            "type": "string",
                            "description": "End frame URL.",
                        },
                        "reference_url": {
                            "type": "string",
                        },
                        "reference_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "reference_video_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "audio_reference_url": {
                            "type": "string",
                        },
                        "audio_reference_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "reference_audio_url": {
                            "type": "string",
                        },
                        "audio_url": {
                            "type": "string",
                        },
                        "generate_audio": {
                            "type": "boolean",
                            "default": True,
                            "description": "Set false for a silent clean picture.",
                        },
                        "requires_reference_media": {
                            "type": "boolean",
                            "default": False,
                            "description": "Refs required.",
                        },
                        "files": {
                            "type": "array",
                            "items": {"type": "object"},
                            "description": "Files [{path,content}].",
                        },
                        "profile": {
                            "type": "string",
                            "enum": ["release_package"],
                            "description": "For a music release bundle with supplied master audio and cover assets.",
                        },
                        "entry": {
                            "type": "string",
                            "description": "Code entry path.",
                        },
                        "image_url": {
                            "type": "string",
                            "description": "Image ref.",
                        },
                        "input_image_url": {
                            "type": "string",
                            "description": "Image ref.",
                        },
                        "input_image_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Image refs.",
                        },
                        "input_fidelity": {
                            "type": "string",
                            "enum": ["low", "high"],
                            "description": "Edit fidelity.",
                        },
                        "save_to_knowledge": {
                            "type": "boolean",
                            "description": "Sync to Knowledge.",
                        },
                        "workspace_asset_key": {
                            "type": "string",
                        },
                        "reuse_if_exists": {
                            "type": "boolean",
                        },
                        "purpose": {
                            "type": "string",
                            "enum": AudioGenerationPurpose.values(),
                            "description": "Purpose.",
                        },
                        "duration_seconds": {
                            "type": "number",
                            "minimum": 0.1,
                            "description": "Audio seconds.",
                        },
                "voice": {
                    "type": "string",
                    "description": "Voice name.",
                },
                "language": {
                    "type": "string",
                    "description": "BCP-47.",
                },
                "narration_voice_mode": {
                    "type": "string",
                    "enum": list(REQUESTABLE_NARRATION_VOICE_MODES),
                    "description": "Voice scope.",
                },
                "voice_instructions": {
                    "type": "string",
                    "description": "Tone, pace, emphasis.",
                },
                        "response_format": {
                            "type": "string",
                            "enum": AudioGenerationFormat.values(),
                            # Conversion is internal, so the only thing the model
                            # needs told is not to treat this as a retry knob.
                            "description": "Stored format. Not a retry knob.",
                        },
                    },
                    "additionalProperties": True,
                },
                "duration": {
                    "type": "integer",
                    "enum": VIDEO_DURATION_CHOICES,
                    "default": 5,
                    "description": "Single clip seconds; inspect selected model.",
                },
                "expected_model": {"type": "string", "description": "Model from search; fail if changed."},
                "resolution": {
                    "type": "string",
                    "enum": VIDEO_RESOLUTION_CHOICES,
                    "default": "720p",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": VIDEO_ASPECT_RATIO_CHOICES,
                    "default": "16:9",
                },
                "requires_reference_media": {
                    "type": "boolean",
                    "default": False,
                    "description": "Refs required.",
                },
                "first_frame_url": {
                    "type": "string",
                    "description": "Start frame URL.",
                },
                "last_frame_url": {
                    "type": "string",
                    "description": "End frame URL.",
                },
                "narration_voice_mode": {
                    "type": "string",
                    "enum": list(REQUESTABLE_NARRATION_VOICE_MODES),
                    "description": "Voice scope.",
                },
                "language": {
                    "type": "string",
                    "description": "BCP-47; defaults to Workspace audio language.",
                },
                "save_to_knowledge": {
                    "type": "boolean",
                    "description": "Sync to Knowledge.",
                },
            },
            "required": ["kind"],
        },
    },
}
