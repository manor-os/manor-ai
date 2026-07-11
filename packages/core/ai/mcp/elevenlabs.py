"""ElevenLabs MCP server — speech, dialogue, music, and sound effects.

Auth: bearer_token = the user's ElevenLabs API key, stored as an
entity Integration with provider="elevenlabs" and credentials
``{"api_key": "..."}``. ElevenLabs uses the ``xi-api-key`` header,
not Authorization Bearer — we adapt internally.

Output: audio responses come back as bytes. To keep the MCP wire
format text-friendly, we save the audio to Manor's filesystem under
``/manor-fs/audio/elevenlabs/<id>.mp3`` and return a short JSON blob
with the path + duration. Callers that need the actual bytes should
read the file via the existing filesystem MCP.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List

import httpx

logger = logging.getLogger(__name__)

_API = "https://api.elevenlabs.io/v1"
_TIMEOUT = 60.0

# Sensible defaults — all selectable from ElevenLabs's prebuilt voices.
# `21m00Tcm4TlvDq8ikWAM` is "Rachel", a stock English female voice.
_DEFAULT_VOICE_ID = "21m00Tcm4TlvDq8ikWAM"
_DEFAULT_MODEL = "eleven_multilingual_v2"
_DEFAULT_DIALOGUE_MODEL = "eleven_v3"
_DEFAULT_SOUND_EFFECT_MODEL = "eleven_text_to_sound_v2"
_DEFAULT_MUSIC_MODEL = "music_v1"

# Where to drop generated MP3s when there is no entity context. Entity-scoped
# saves go through the shared entity filesystem helpers instead.
_FALLBACK_AUDIO_DIR = Path("/tmp/manor-audio/elevenlabs")


def _get_audio_dir() -> Path:
    """Resolve the fallback audio output directory."""
    _FALLBACK_AUDIO_DIR.mkdir(parents=True, exist_ok=True)
    return _FALLBACK_AUDIO_DIR

# Call context — set by the MCP dispatcher before call_tool()
_call_context: Dict[str, str] = {}


def set_call_context(ctx: Dict[str, str]) -> None:
    global _call_context
    _call_context = ctx


def clear_call_context() -> None:
    global _call_context
    _call_context = {}


# ── MCP protocol ────────────────────────────────────────────────────────────

def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "text_to_speech",
            "description": (
                "Convert text into an MP3 voiceover via ElevenLabs. "
                "Returns the path to the saved audio file plus a "
                "play-ready URL. Good for demo voiceovers, voice-agent "
                "responses, and narration. ~$0.30 / 1000 characters."
            ),
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "voice_id": {
                        "type": "string",
                        "description": "Voice id from list_voices. Defaults to a stock English female voice ('Rachel').",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "ElevenLabs model. Default 'eleven_multilingual_v2' (works for English + 28 other langs). Cheaper/faster: 'eleven_flash_v2_5'.",
                    },
                    "stability": {
                        "type": "number",
                        "description": "0.0-1.0; lower = more variability. Default 0.5.",
                    },
                    "similarity_boost": {
                        "type": "number",
                        "description": "0.0-1.0; higher = closer to source voice. Default 0.75.",
                    },
                    "filename_hint": {
                        "type": "string",
                        "description": "Optional human-readable label folded into the saved filename.",
                    },
                },
            },
        },
        {
            "name": "text_to_dialogue",
            "description": (
                "Convert a list of speaker lines into one natural "
                "multi-speaker dialogue audio file via ElevenLabs Text "
                "to Dialogue. Use for character conversations, not music "
                "or ambience."
            ),
            "parameters": {
                "type": "object",
                "required": ["inputs"],
                "properties": {
                    "inputs": {
                        "type": "array",
                        "description": (
                            "Dialogue turns. Each item needs text and a "
                            "voice_id from list_voices."
                        ),
                        "items": {
                            "type": "object",
                            "required": ["text", "voice_id"],
                            "properties": {
                                "text": {"type": "string"},
                                "voice_id": {"type": "string"},
                            },
                        },
                    },
                    "model_id": {
                        "type": "string",
                        "description": "ElevenLabs dialogue model. Default 'eleven_v3'.",
                    },
                    "language_code": {
                        "type": "string",
                        "description": "Optional ISO 639-1 language code.",
                    },
                    "filename_hint": {"type": "string"},
                },
            },
        },
        {
            "name": "generate_sound_effect",
            "description": (
                "Generate a dedicated sound-effect or ambience audio bed "
                "from text via ElevenLabs Sound Effects. Use for SFX, "
                "Foley, room tone, rain, streets, crowds, machinery, and "
                "other non-speech/non-music audio."
            ),
            "parameters": {
                "type": "object",
                "required": ["text"],
                "properties": {
                    "text": {"type": "string"},
                    "duration_seconds": {
                        "type": "number",
                        "description": "Optional duration from 0.5 to 30 seconds.",
                    },
                    "loop": {
                        "type": "boolean",
                        "description": "Ask for a smoothly looping bed when supported.",
                    },
                    "prompt_influence": {
                        "type": "number",
                        "description": "0.0-1.0; higher follows the prompt more closely.",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "Default 'eleven_text_to_sound_v2'.",
                    },
                    "filename_hint": {"type": "string"},
                },
            },
        },
        {
            "name": "compose_music",
            "description": (
                "Generate music or score audio via ElevenLabs Music. "
                "Use for BGM, themes, stingers, and instrumental score; "
                "not for ambience or dialogue. Requires a paid ElevenLabs "
                "plan according to the API docs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Text prompt for the music. Required unless composition_plan is provided.",
                    },
                    "composition_plan": {
                        "type": "object",
                        "description": "Optional detailed composition plan accepted by ElevenLabs Music.",
                    },
                    "music_length_ms": {
                        "type": "integer",
                        "description": "Optional length in milliseconds, 3000-600000 when using prompt.",
                    },
                    "force_instrumental": {
                        "type": "boolean",
                        "description": "Force instrumental output when using prompt.",
                    },
                    "model_id": {
                        "type": "string",
                        "description": "Default 'music_v1'.",
                    },
                    "filename_hint": {"type": "string"},
                },
            },
        },
        {
            "name": "list_voices",
            "description": (
                "List the user's prebuilt + cloned ElevenLabs voices. "
                "Returns each voice's id, name, and labels (gender, "
                "accent, age) so the agent can pick a fitting one."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    if not bearer_token:
        return _error(
            "ElevenLabs API key is missing. Get one at "
            "https://elevenlabs.io/app/settings/api-keys and add it "
            "under Integrations → ElevenLabs."
        )

    handler = _HANDLERS.get(name)
    if handler is None:
        return _error(f"Unknown elevenlabs tool: {name}")

    try:
        return _content(await handler(arguments, bearer_token))
    except httpx.HTTPStatusError as exc:
        body = exc.response.text[:500] if exc.response is not None else ""
        return _error(f"ElevenLabs HTTP {exc.response.status_code}: {body}")
    except Exception as exc:  # noqa: BLE001
        logger.exception("ElevenLabs tool %s crashed", name)
        return _error(f"ElevenLabs call failed: {exc}")


# ── Handlers ────────────────────────────────────────────────────────────────

async def _text_to_speech(args: Dict[str, Any], api_key: str) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")

    voice_id = (args.get("voice_id") or _DEFAULT_VOICE_ID).strip()
    model_id = (args.get("model_id") or _DEFAULT_MODEL).strip()

    voice_settings: Dict[str, Any] = {}
    if args.get("stability") is not None:
        voice_settings["stability"] = float(args["stability"])
    if args.get("similarity_boost") is not None:
        voice_settings["similarity_boost"] = float(args["similarity_boost"])

    body: Dict[str, Any] = {"text": text, "model_id": model_id}
    if voice_settings:
        body["voice_settings"] = voice_settings

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(
            f"{_API}/text-to-speech/{voice_id}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=body,
        )
        r.raise_for_status()
        audio_bytes = r.content

    fpath, fname = await _save_audio_bytes(audio_bytes, args.get("filename_hint") or "tts")

    return json.dumps({
        "voice_id": voice_id,
        "model_id": model_id,
        "characters": len(text),
        "bytes": len(audio_bytes),
        "path": str(fpath),
        "filename": fname,
    }, ensure_ascii=False, indent=2)


async def _text_to_dialogue(args: Dict[str, Any], api_key: str) -> str:
    inputs = args.get("inputs") or []
    if not isinstance(inputs, list) or not inputs:
        raise ValueError("inputs is required and must be a non-empty list")

    normalized_inputs: list[dict[str, str]] = []
    total_chars = 0
    for index, item in enumerate(inputs, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"inputs[{index}] must be an object")
        text = str(item.get("text") or "").strip()
        voice_id = str(item.get("voice_id") or "").strip()
        if not text or not voice_id:
            raise ValueError(f"inputs[{index}] requires text and voice_id")
        normalized_inputs.append({"text": text, "voice_id": voice_id})
        total_chars += len(text)

    if total_chars > 2000:
        raise ValueError(
            "ElevenLabs Text to Dialogue is most reliable at <= 2000 characters per request. Split this dialogue into smaller chunks."
        )

    body: Dict[str, Any] = {
        "inputs": normalized_inputs,
        "model_id": (args.get("model_id") or _DEFAULT_DIALOGUE_MODEL).strip(),
    }
    if args.get("language_code"):
        body["language_code"] = str(args["language_code"]).strip()

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(
            f"{_API}/text-to-dialogue",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=body,
        )
        r.raise_for_status()
        audio_bytes = r.content

    fpath, fname = await _save_audio_bytes(
        audio_bytes,
        args.get("filename_hint") or "dialogue",
    )
    return json.dumps({
        "model_id": body["model_id"],
        "turns": len(normalized_inputs),
        "characters": total_chars,
        "bytes": len(audio_bytes),
        "path": str(fpath),
        "filename": fname,
    }, ensure_ascii=False, indent=2)


async def _generate_sound_effect(args: Dict[str, Any], api_key: str) -> str:
    text = (args.get("text") or "").strip()
    if not text:
        raise ValueError("text is required")

    body: Dict[str, Any] = {
        "text": text,
        "model_id": (args.get("model_id") or _DEFAULT_SOUND_EFFECT_MODEL).strip(),
    }
    if args.get("duration_seconds") is not None:
        body["duration_seconds"] = float(args["duration_seconds"])
    if args.get("loop") is not None:
        body["loop"] = bool(args["loop"])
    if args.get("prompt_influence") is not None:
        body["prompt_influence"] = float(args["prompt_influence"])

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(
            f"{_API}/sound-generation",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=body,
        )
        r.raise_for_status()
        audio_bytes = r.content

    fpath, fname = await _save_audio_bytes(audio_bytes, args.get("filename_hint") or "sfx")
    return json.dumps({
        "model_id": body["model_id"],
        "characters": len(text),
        "bytes": len(audio_bytes),
        "path": str(fpath),
        "filename": fname,
    }, ensure_ascii=False, indent=2)


async def _compose_music(args: Dict[str, Any], api_key: str) -> str:
    prompt = (args.get("prompt") or "").strip()
    composition_plan = args.get("composition_plan")
    if not prompt and not isinstance(composition_plan, dict):
        raise ValueError("prompt or composition_plan is required")
    if prompt and isinstance(composition_plan, dict):
        raise ValueError("Use either prompt or composition_plan, not both")

    body: Dict[str, Any] = {
        "model_id": (args.get("model_id") or _DEFAULT_MUSIC_MODEL).strip(),
    }
    if prompt:
        body["prompt"] = prompt
        if args.get("music_length_ms") is not None:
            body["music_length_ms"] = int(args["music_length_ms"])
        if args.get("force_instrumental") is not None:
            body["force_instrumental"] = bool(args["force_instrumental"])
    else:
        body["composition_plan"] = composition_plan

    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.post(
            f"{_API}/music",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json=body,
        )
        r.raise_for_status()
        audio_bytes = r.content

    fpath, fname = await _save_audio_bytes(audio_bytes, args.get("filename_hint") or "music")
    return json.dumps({
        "model_id": body["model_id"],
        "characters": len(prompt),
        "bytes": len(audio_bytes),
        "path": str(fpath),
        "filename": fname,
    }, ensure_ascii=False, indent=2)


async def _save_audio_bytes(audio_bytes: bytes, filename_hint: str) -> tuple[Path, str]:
    safe_hint = "".join(
        c if c.isalnum() or c in ("-", "_") else "_"
        for c in (filename_hint or "audio")
    )[:32]
    fname = f"{int(time.time())}_{safe_hint}_{uuid.uuid4().hex[:8]}.mp3"
    entity_id = _call_context.get("entity_id")
    if entity_id:
        from packages.core.services.entity_fs import write_entity_file_atomic

        rel_path = f"audio/elevenlabs/{fname}"
        fpath = Path(
            write_entity_file_atomic(
                entity_id,
                rel_path,
                audio_bytes,
                expected_size=len(audio_bytes),
            )
        )
    else:
        audio_dir = _get_audio_dir()
        fpath = audio_dir / fname
        fpath.write_bytes(audio_bytes)

    # Register in knowledge base so the file is searchable/visible.
    await _register_document(fname, str(fpath), len(audio_bytes))
    return fpath, fname


async def _register_document(name: str, fs_path: str, file_size: int) -> None:
    """Register a generated audio file in the documents table."""
    entity_id = _call_context.get("entity_id")
    if not entity_id:
        logger.warning("No entity_id in call context — skipping document registration for %s", name)
        return
    try:
        from packages.core.database import async_session
        from packages.core.services.document_metadata import merge_document_metadata
        from packages.core.services.document_service import upsert_document_by_fs_path
        from packages.core.services.knowledge_sync import bind_document_to_workspace, ensure_folder_path

        from packages.core.services.entity_fs import get_entity_root

        try:
            rel_path = Path(fs_path).resolve().relative_to(
                Path(get_entity_root(entity_id)).resolve()
            ).as_posix()
        except ValueError:
            rel_path = fs_path

        rel_dir = str(Path(rel_path).parent).replace("\\", "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        folder_id = await ensure_folder_path(entity_id, rel_dir)
        workspace_id = _call_context.get("workspace_id")
        task_id = _call_context.get("task_id")
        agent_id = _call_context.get("agent_id")
        conversation_id = _call_context.get("conversation_id")
        user_id = _call_context.get("user_id")
        document_id = None
        async with async_session() as db:
            doc = await upsert_document_by_fs_path(
                db,
                entity_id,
                name=name,
                fs_path=rel_path,
                file_size=file_size,
                file_type="mp3",
                mime_type="audio/mpeg",
                source="elevenlabs",
                created_by=user_id,
                folder_id=folder_id,
            )
            doc.source = "elevenlabs"
            doc.created_by = user_id
            doc.vector_status = "skipped"
            doc.metadata_ = merge_document_metadata(
                doc.metadata_,
                artifact={"role": "final", "storage_scope": "artifact"},
                origin={
                    "workspace_id": workspace_id,
                    "task_id": task_id,
                    "agent_id": agent_id,
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "tool_name": "elevenlabs",
                },
            )
            document_id = doc.id
            await db.commit()
            logger.info("Registered ElevenLabs audio as document %s", document_id)
        if workspace_id and document_id:
            await bind_document_to_workspace(
                entity_id=entity_id,
                document_id=document_id,
                workspace_id=workspace_id,
                task_id=task_id,
                agent_id=agent_id,
                conversation_id=conversation_id,
                user_id=user_id,
                tool_name="elevenlabs",
            )
    except Exception:
        logger.exception("Failed to register audio document %s", name)


async def _list_voices(args: Dict[str, Any], api_key: str) -> str:
    async with httpx.AsyncClient(timeout=_TIMEOUT) as cx:
        r = await cx.get(
            f"{_API}/voices",
            headers={"xi-api-key": api_key, "Accept": "application/json"},
        )
        r.raise_for_status()
        body = r.json()

    voices = []
    for v in (body.get("voices") or []):
        voices.append({
            "voice_id": v.get("voice_id"),
            "name": v.get("name"),
            "labels": v.get("labels") or {},
            "category": v.get("category"),
        })
    return json.dumps({"count": len(voices), "voices": voices}, ensure_ascii=False, indent=2)


def _content(text: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "isError": False}


from packages.core.ai.mcp._http import mcp_err as _error  # noqa: E402, F401


_HANDLERS = {
    "text_to_speech": _text_to_speech,
    "text_to_dialogue": _text_to_dialogue,
    "generate_sound_effect": _generate_sound_effect,
    "compose_music": _compose_music,
    "list_voices": _list_voices,
}
