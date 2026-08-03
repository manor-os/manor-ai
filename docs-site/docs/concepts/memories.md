---
sidebar_position: 8
title: Memories
---

# Memories

Memories are durable facts that survive across conversations. They come in
two scopes backed by one store:

- **Agent memories** — things an agent learned about a specific user:
  facts, preferences, context, and standing instructions. Scoped to
  (agent, user), so each agent remembers its own relationship with each
  person.
- **Workspace memories** — the workspace's operating brain: guidance,
  preferences, decisions, learnings, and facts that the planning and
  strategist layers consult when deciding what the workspace should do next.

Open **Memories** in the sidebar to browse, add, edit, archive, or delete
them, filtered per agent.

## Where Memories Come From

1. **Manual** — you write them yourself. Manual entries are treated as the
   most authoritative source.
2. **Conversation extraction** — an LLM pass over a conversation pulls out
   memory-worthy statements (`POST /api/v1/memories/extract`).
3. **Periodic chat insight extraction** — a scheduled job sweeps workspace
   chat for operator preferences, guidance, and decisions. Automatically
   extracted memories are confidence-capped below manual ones so they never
   outrank what you stated explicitly.

## How Memories Are Used

Each memory carries an importance (1–10) and a confidence (0–1). When an
agent runs, a budgeted context block is assembled from the memories most
relevant to the current query — ranked by embedding similarity with
importance as the tiebreaker — and injected into the prompt. Workspace
memories above a confidence threshold are similarly rendered into the
workspace runtime prompt.

Memories can expire (`expires_at`) and can be archived instead of deleted
when you want them out of prompts but kept for the record.

## API Summary

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/memories` | List, filterable by agent, user, and type |
| `POST /api/v1/memories`, `PUT/DELETE /api/v1/memories/{id}` | Manage entries |
| `POST /api/v1/memories/{id}/archive` | Archive without deleting |
| `POST /api/v1/memories/extract` | Extract memories from a conversation |
| `GET /api/v1/memories/context` | Debug: see the rendered context block agents receive |
