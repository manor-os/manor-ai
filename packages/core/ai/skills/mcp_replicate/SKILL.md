---
name: mcp_replicate
description: Generate images and short videos from text prompts through the Replicate MCP. Use when the user asks to create/generate an image or video with AI, or to run a specific Replicate model.
version: 1.0.0
---

# Replicate Runtime Skill

Use this skill to generate media via **Replicate** through the Replicate MCP (`mcp__replicate__*`).

## When To Use

Use Replicate when the user asks to generate an image or a short video from a text prompt, or to run a specific Replicate model. For voice/audio use `mcp_elevenlabs`; for Chinese-first image/video use `mcp_jimeng`.

## Connection

Authenticates with a Replicate API token. On an auth error, stop and ask the user to fix the token. **Generation costs money and time** per run — treat each call as billable.

## Core Tools

- `generate_image` — text prompt → image.
- `generate_video` — text prompt → short video.
- `run_model` — escape hatch: run any Replicate model by id with arbitrary inputs (use only when the two helpers above don't fit).

## Common Recipes

**Generate an image**
1. Confirm the prompt (and any size/style) with the user. 2. `generate_image`. 3. Return the result reference/URL.

**Run a specific model**
1. Identify the model id + its required inputs. 2. `run_model` with those inputs.

## Guardrails

- **Each generation is billable and not instant** — confirm the prompt before generating; don't speculatively fan out many variations unless the user asked for several.
- Don't silently retry a failed/long generation in a loop — surface status and ask before re-running (re-runs cost again).
- Respect content policy: refuse prompts for disallowed content (e.g. real-person deepfakes, sexual content involving minors, etc.).
- `run_model` with a wrong input schema wastes a paid run — verify the model's expected inputs first.

## Edge Cases & Errors

- Output is async/URL-based — return the produced asset reference; don't fabricate a result if generation hasn't completed.
- Video generation is slower/pricier than image — set expectations and confirm before kicking it off.
- Auth/quota errors → stop and tell the user (token invalid or credits exhausted).
