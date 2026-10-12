---
name: mcp_suno
description: Generate original music, covers, and mashups through the Suno MCP, then check generation status and account usage.
version: 1.0.0
---

# Suno Runtime Skill

Use this skill for music generation through the Suno MCP (`mcp__suno__*`).

## When To Use

Use Suno when the user asks for an original song, an instrumental track, a cover of existing Suno audio, or a mashup of two Suno audio items. Use ElevenLabs for voiceover, dialogue, Foley, or ambience rather than songs.

## Connection And Cost

Suno authenticates with the user's own API key. Creation consumes that Suno account's credits, so `generate_music`, `create_cover`, and `create_mashup` are approval-gated paid actions. `get_generation` and `get_usage` are read-only.

## Core Tools

- `generate_music` — submit one song job using either a simple `description` or custom `lyrics` + `style`. For an instrumental custom job, set `instrumental: true` and provide `style`.
- `get_generation` — read the status and provider output URL for one job id.
- `create_cover` — submit one cover job from `source_audio_id`.
- `create_mashup` — submit one mashup job from `source_audio_id` plus `additional_audio_id`.
- `get_usage` — read current API usage without starting work.

## Workflow

1. Confirm the creative brief, lyrics, style, and instrumental/vocal intent before spending credits.
2. Submit exactly one creation call.
3. Preserve the returned job id and use `get_generation` to check it.
4. Report the provider status and output URL. A Suno URL is an upstream reference, not a durable Manor Knowledge artifact.

## Guardrails

- Never loop or automatically resubmit a creation call. A timeout can occur after Suno accepted and charged the request; check Suno Platform and ask the user before retrying.
- Do not claim completion while the job is only submitted, queued, or streaming.
- Use only lyrics and source audio the user is entitled to use. Do not imitate a real artist or person's voice deceptively, and do not promise ownership or licensing terms.
- Auth, quota, moderation, or provider errors stay visible. Do not transform them into a success result.
