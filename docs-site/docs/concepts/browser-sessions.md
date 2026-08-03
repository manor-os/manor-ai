---
sidebar_position: 11
title: Browser Sessions
---

# Browser Sessions

Browser sessions give agents (and operators) a server-side Chromium browser
for web automation: navigate, click, fill forms, run JavaScript, take
screenshots, extract page content, and render PDFs.

Open **Browser Sessions** in the sidebar to create a session, drive it, and
watch screenshots of what it sees.

## Capabilities

Per session:

- `navigate` — load a URL (30-second timeout).
- `screenshot` — PNG, optionally full-page.
- Actions — `click`, `fill`, `evaluate` (JavaScript), `get_content`
  (page HTML), `pdf`.

Sessions are held in memory on the API process: they are working tools, not
durable records. Anything worth keeping (screenshots, extracted content)
should be saved to documents or task attachments.

## Requirements

Server-side browsing uses Playwright with Chromium, which is an optional
dependency. If it isn't installed the API returns a clear error; install
with:

```bash
pip install playwright && playwright install chromium
```

(In Docker deployments, add this to your API image if you use the feature.)

## API Summary

| Endpoint | Purpose |
| --- | --- |
| `POST /api/v1/browser/sessions` | Create (`{"headless": true}`) |
| `GET /api/v1/browser/sessions`, `GET .../{id}` | List / inspect |
| `POST /api/v1/browser/sessions/{id}/navigate` | Go to a URL |
| `POST /api/v1/browser/sessions/{id}/screenshot` | Capture |
| `POST /api/v1/browser/sessions/{id}/action` | click / fill / evaluate / get_content / pdf |
| `DELETE /api/v1/browser/sessions/{id}` | Close |

Treat browser automation as a sensitive capability: it can reach anything
your server can reach. Scope it to agents that need it and pair outward
actions with [HITL governance](hitl-governance.md).
