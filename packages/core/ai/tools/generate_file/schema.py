from __future__ import annotations

from typing import Any

from packages.core.contracts.audio_generation import (
    AudioGenerationFormat,
    AudioGenerationPurpose,
    GenerateFileKind,
)

_CAPABILITIES = {
    "diagram": "Create an editable .diagram.json canvas from a prompt and save it to Knowledge.",
    "code": "Create a multi-file website/code bundle from params.files; use real extensions like .html/.css/.js, not .txt.",
    "document": "Create a simple user-visible file from supplied content (.md, .txt, .csv, .json, .diagram.json, .html, .docx, .pptx, .pdf). Use .diagram.json for editable AI-drawn diagrams.",
    "word_document": "Compatibility fallback for document specialist skills when no matching Available Skills entry is selected.",
    "pdf": "Compatibility fallback for document specialist skills when no matching Available Skills entry is selected.",
    "presentation": "Compatibility fallback for presentation specialist skills when no matching Available Skills entry is selected.",
    "spreadsheet": "Compatibility fallback for spreadsheet specialist skills when no matching Available Skills entry is selected.",
    "image": "Generate an image with the Account-selected image model and BYOK/platform billing rules.",
    "video": "Generate one short video clip with the Account-selected video model and BYOK/platform billing rules. For >15s total runtime, segment, wait, then merge.",
    "audio": (
        "Generate an audio file with the Account-selected OpenRouter voice/music/SFX "
        "model and platform billing rules. MP3/WAV/etc. is the final artifact format; "
        "the tool handles provider-format conversion internally, so changing the "
        "extension does not recover an upstream provider failure."
    ),
}

VIDEO_DURATION_CHOICES = [4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
VIDEO_RESOLUTION_CHOICES = ["480p", "720p", "1080p"]
VIDEO_SEEDANCE_FAST_RESOLUTION_CHOICES = ["480p", "720p"]
VIDEO_ASPECT_RATIO_CHOICES = ["adaptive", "21:9", "16:9", "4:3", "3:4", "1:1", "9:16"]


GENERATE_FILE_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "generate_file",
        "description": "Create docs, code, diagrams, images, videos/mp4, audio.",
        "parameters": {
            "type": "object",
            "properties": {
                "kind": {
                    "type": "string",
                    "enum": GenerateFileKind.values(),
                    "description": "Type",
                },
                "prompt": {
                    "type": "string",
                    "description": "Prompt",
                },
                "name": {
                    "type": "string",
                    "description": "Path",
                },
                "content": {
                    "type": "string",
                    "description": "Content",
                },
                "file_type": {
                    "type": "string",
                    "description": "Extension",
                },
                "approval_token": {
                    "type": "string",
                    "description": "OK",
                },
                "expected_sha256": {
                    "type": "string",
                    "description": "sha",
                },
                "params": {
                    "type": "object",
                    "properties": {
                        "duration": {
                            "type": "integer",
                            "enum": VIDEO_DURATION_CHOICES,
                            "default": 5,
                            # Kept terse here: the canonical, tested wording
                            # ("Single clip seconds.") lives on the top-level
                            # "duration" property below; this nested alias
                            # only needs its enum to match.
                            "description": "Seconds.",
                        },
                        "resolution": {
                            "type": "string",
                            "enum": VIDEO_RESOLUTION_CHOICES,
                            "default": "720p",
                            "description": "Res.",
                        },
                        "aspect_ratio": {
                            "type": "string",
                            "enum": VIDEO_ASPECT_RATIO_CHOICES,
                            "default": "16:9",
                            "description": "Aspect.",
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
                            "description": "Ref URL.",
                        },
                        "reference_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Image refs.",
                        },
                        "reference_video_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Video refs.",
                        },
                        "audio_reference_url": {
                            "type": "string",
                            "description": "Audio ref.",
                        },
                        "audio_reference_urls": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Audio refs.",
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
                    "enum": ["random_per_task", "fixed_per_workspace"],
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
                    "description": "Single clip seconds.",
                },
                "resolution": {
                    "type": "string",
                    "enum": VIDEO_RESOLUTION_CHOICES,
                    "default": "720p",
                    "description": "Res.",
                },
                "aspect_ratio": {
                    "type": "string",
                    "enum": VIDEO_ASPECT_RATIO_CHOICES,
                    "default": "16:9",
                    "description": "Aspect.",
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
                    "enum": ["random_per_task", "fixed_per_workspace"],
                    "description": "Voice scope.",
                },
                "language": {
                    "type": "string",
                    "description": "BCP-47; defaults to Workspace settings.audio_defaults.language.",
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
