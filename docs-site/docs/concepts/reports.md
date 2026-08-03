---
sidebar_position: 9
title: Reports
---

# Reports

Reports are on-demand, server-rendered summaries of what your deployment has
been doing. They are generated fresh from live data on every request — no
report storage, no stale snapshots.

Open **Reports** in the sidebar to preview, download, or email them.

## Report Types

| Type | Contents | Default window |
| --- | --- | --- |
| **Tasks** | Task volume, completion trends, and status breakdowns | 30 days |
| **Usage** | Token and cost usage across models and agents | 30 days |
| **Activity** | A digest of recent activity across workspaces | 7 days |

Each report is produced as styled HTML (self-contained, inline CSS — ready to
email or archive), plus a plain-text summary and the raw data used to build
it.

## Emailing Reports

`POST /api/v1/reports/email` sends any report type to a list of recipients
through your configured SMTP server — combine it with a
[scheduled job](automations.md) to get a recurring emailed report. Email
delivery requires `EMAIL_ENABLED=true` and SMTP settings
([Configuration](../configuration#email-smtp)).

## API Summary

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/reports/tasks?days=30` | Task report as JSON (`title`, `html`, `text_summary`, `data`) |
| `GET /api/v1/reports/usage?days=30` | Usage report as JSON |
| `GET /api/v1/reports/activity?days=7` | Activity digest as JSON |
| `GET /api/v1/reports/tasks/html`, `/usage/html` | Raw HTML responses |
| `POST /api/v1/reports/email` | Send a report to recipients |

`days` accepts 1–365.
