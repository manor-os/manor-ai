---
name: mcp_elevenlabs
description: Generate audio through the ElevenLabs MCP. Use when the user asks to create a voiceover / text-to-speech, multi-speaker dialogue, a sound effect or ambience, or music.
version: 1.0.0
---

# ElevenLabs Runtime Skill

Use this skill to generate audio via **ElevenLabs** through the ElevenLabs MCP (`mcp__elevenlabs__*`).

## When To Use

Use ElevenLabs when the user asks for a spoken voiceover, a multi-speaker dialogue, a sound effect/ambience, or generated music. For images/video use `mcp_replicate` or `mcp_jimeng`.

## Connection

Authenticates with an ElevenLabs API key. On an auth error, stop and ask the user to fix the key. **Generation consumes credits** — treat each call as billable.

## Core Tools

- `list_voices` — the user's prebuilt + cloned voices (pick a `voice` before TTS).
- `text_to_speech` — text → MP3 voiceover.
- `text_to_dialogue` — a list of speaker lines → one natural multi-speaker track.
- `generate_sound_effect` — a dedicated SFX/ambience bed.
- `compose_music` — generate music/score audio.

## Common Recipes

**Voiceover**
1. `list_voices` → choose a `voice` (confirm with the user if they have a preference). 2. Confirm the script. 3. `text_to_speech`.

**Two-person dialogue**
1. `list_voices` for the speakers. 2. Build the ordered speaker lines. 3. `text_to_dialogue`.

## Guardrails

- **Each generation consumes credits** — confirm the script/voice before generating; don't regenerate repeatedly without asking.
- **Voice cloning / likeness**: only use voices the user is entitled to; don't synthesize a real person's voice without their consent.
- Keep within reasonable length per call; very long scripts burn credits fast — chunk deliberately.
- Refuse disallowed content (impersonation, fraud, etc.).

## Edge Cases & Errors

- Pick a valid `voice` id from `list_voices` — a wrong/empty voice fails or uses an unintended default.
- Output is an audio asset reference — return it; don't claim success before it's produced.
- Auth/quota errors → stop and tell the user (key invalid or credits exhausted).
