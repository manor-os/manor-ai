---
name: mcp_jimeng
description: Generate and edit images and generate short videos through the Jimeng (即梦) MCP. Use when the user asks to create or edit an image, or generate a short video, especially from a Chinese-language prompt.
version: 1.0.0
---

# Jimeng (即梦) Runtime Skill

Use this skill to generate/edit media via **Jimeng (即梦)** through the Jimeng MCP (`mcp__jimeng__*`). Jimeng handles Chinese and English prompts and is a strong default for Chinese-language creative requests.

## When To Use

Use Jimeng when the user asks to generate an image, edit/transform an existing image, or generate a short video — particularly with Chinese prompts or for a China-market aesthetic. For English-first or specific Replicate models use `mcp_replicate`.

## Connection

Authenticates with Jimeng credentials. On an auth error, stop and ask the user to reconnect. **Generation is billable / not instant** — treat each call as a paid run.

## Core Tools

- `generate_image` — prompt (中文 or English) → one or more images.
- `edit_image` — transform an existing image with a text instruction.
- `generate_video` — prompt → short video.

## Common Recipes

**Generate an image**
1. Confirm the prompt (and count/aspect if relevant). 2. `generate_image`. 3. Return the asset reference.

**Edit an image**
1. Take the source image + the edit instruction. 2. `edit_image`. 3. Return the result.

## Guardrails

- **Each generation is billable and not instant** — confirm the prompt before generating; don't fan out many variations unless asked.
- For `edit_image`, confirm which source image is being transformed so you don't edit the wrong asset.
- Respect content policy and platform rules (no disallowed or infringing content).
- Don't loop-retry a slow/failed generation without asking — re-runs cost again.

## Edge Cases & Errors

- Output is an async asset reference/URL — return it; don't fabricate a result before completion.
- Video is slower/pricier than image — set expectations and confirm first.
- Auth/quota errors → stop and tell the user.
