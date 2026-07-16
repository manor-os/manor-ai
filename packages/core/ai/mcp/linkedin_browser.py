"""LinkedIn (browser) — MCP wrapper.

The capabilities here cover what LinkedIn's public REST API does NOT:
people / company search, third-party profile reads, jobs, feed,
messaging. All of these need an authenticated browser session, so we
drive a headless Chromium through the browser-runner sidecar using the
user's exported session cookies — same pattern as ``notebooklm`` /
``chatgpt_web``.

Risk note
─────────
Using these tools violates LinkedIn's ToS (8.2 — "automated means").
LinkedIn's anti-automation is stricter than most platforms (device
fingerprinting + Cloudflare + behavioural analysis). To reduce
exposure:

  * keep call rate low (single-digit per minute, not per second)
  * use a dedicated LinkedIn account, not the user's primary one
  * rotate IPs / use a residential proxy if available

Surface (rolled out across 3 PRs, all live in this file now):

  PR2: search_people / view_profile / search_companies / view_company
  PR3: search_jobs / view_job / easy_apply / list_my_applications
  PR4: list_conversations / view_conversation / send_message
       browse_feed / search_posts / view_post

Auth
────
``bearer_token`` = exported LinkedIn session cookies (Playwright
``storage_state`` JSON or Cookie-Editor list export). The user pastes
this into Integrations → "LinkedIn (Search & Messaging)". The runner injects it
on every call; we never persist it server-side beyond CredentialService.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import _browser_runner
from ..pending_action import PendingAction


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": "search_people",
            "description": (
                "Search LinkedIn members by free-text keywords. Returns "
                "name, headline, profile URL, location and (when "
                "visible) current position. Pure search — does not open "
                "any profile. Combine with view_profile for details."
            ),
            "parameters": {
                "type": "object",
                "required": ["keywords"],
                "properties": {
                    "keywords": {
                        "type": "string",
                        "description": (
                            "Free-text query — e.g. 'series A founder "
                            "fintech', 'recruiter Stripe'."
                        ),
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results (1-25, default 10)",
                    },
                },
            },
        },
        {
            "name": "view_profile",
            "description": (
                "Fetch a LinkedIn profile — name, headline, location, "
                "about, current company, top experience entries. "
                "Accepts a full URL "
                "(https://www.linkedin.com/in/{handle}/) or just the "
                "vanity handle ('{handle}')."
            ),
            "parameters": {
                "type": "object",
                "required": ["profile"],
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": "Profile URL or vanity handle",
                    },
                },
            },
        },
        {
            "name": "search_companies",
            "description": (
                "Search LinkedIn companies by keywords. Returns name, "
                "vanity handle, page URL, industry and (when visible) "
                "headcount range. Combine with view_company for details."
            ),
            "parameters": {
                "type": "object",
                "required": ["keywords"],
                "properties": {
                    "keywords": {"type": "string"},
                    "count": {
                        "type": "integer",
                        "description": "Max results (1-25, default 10)",
                    },
                },
            },
        },
        {
            "name": "view_company",
            "description": (
                "Fetch a LinkedIn company page — name, tagline, about, "
                "industry, headcount, headquarters, website, follower "
                "count. Accepts the company URL "
                "(https://www.linkedin.com/company/{handle}/) or just "
                "the vanity handle ('{handle}')."
            ),
            "parameters": {
                "type": "object",
                "required": ["company"],
                "properties": {
                    "company": {
                        "type": "string",
                        "description": "Company URL or vanity handle",
                    },
                },
            },
        },
        # ── Jobs (PR3) ──────────────────────────────────────────────
        {
            "name": "search_jobs",
            "description": (
                "Search LinkedIn job postings. Returns title, company, "
                "location, posted age (e.g. '3d ago'), whether Easy "
                "Apply is available, the job_id (numeric) and URL. "
                "Combine with view_job for full details."
            ),
            "parameters": {
                "type": "object",
                "required": ["keywords"],
                "properties": {
                    "keywords": {"type": "string"},
                    "location": {
                        "type": "string",
                        "description": "City or country, e.g. 'San Francisco' or 'Remote'",
                    },
                    "remote": {
                        "type": "boolean",
                        "description": "Filter to remote-only roles (default false)",
                    },
                    "posted_within_days": {
                        "type": "integer",
                        "description": "1, 7, or 30 — LinkedIn rounds to its filter buckets",
                    },
                    "easy_apply_only": {
                        "type": "boolean",
                        "description": "Only return jobs with Easy Apply (default false)",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Max results (1-25, default 10)",
                    },
                },
            },
        },
        {
            "name": "view_job",
            "description": (
                "Fetch a LinkedIn job posting — title, company, "
                "location, posted_age, applicant_count, "
                "employment_type, seniority, full description, "
                "criteria. Accepts a full URL "
                "(https://www.linkedin.com/jobs/view/{id}/) or just "
                "the numeric job id."
            ),
            "parameters": {
                "type": "object",
                "required": ["job"],
                "properties": {
                    "job": {
                        "type": "string",
                        "description": "Job URL or numeric job_id",
                    },
                },
            },
        },
        {
            "name": "easy_apply",
            "description": (
                "Submit an Easy Apply application to a LinkedIn job. "
                "**Default is dry-run** — opens the form, walks the "
                "pages (filling whatever ``answers`` provides), and "
                "returns either status='preview' (clear-to-submit), "
                "status='blocked' (still missing answers — the "
                "blocking_questions list says which), status='submitted' "
                "(sent), or status='error'. Pass confirm=true alongside "
                "an ``answers`` dict that covers every blocking_question "
                "from a prior dry-run to actually submit."
            ),
            "parameters": {
                "type": "object",
                "required": ["job"],
                "properties": {
                    "job": {
                        "type": "string",
                        "description": "Job URL or numeric job_id",
                    },
                    "answers": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": (
                            "Map of question label → answer for any "
                            "blocking_questions returned by a prior "
                            "dry-run. Labels must match verbatim (the "
                            "wrapper passes them through unchanged). "
                            "For select/radio fields the answer must "
                            "match one of the visible options. Filled "
                            "best-effort — wrong-format values are "
                            "skipped and re-surface as still-blocking "
                            "on the next pass."
                        ),
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "REQUIRED for actual submission. Defaults "
                            "to false (dry-run). When true, clicks "
                            "Submit on the final page if no blocking "
                            "questions remain after filling."
                        ),
                    },
                },
            },
        },
        {
            "name": "list_my_applications",
            "description": (
                "List the user's recent LinkedIn job applications "
                "(jobs they've applied to via Easy Apply). Returns "
                "title, company, applied_date, status."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max results (1-50, default 20)",
                    },
                },
            },
        },
        # ── Messaging (PR4) ─────────────────────────────────────────
        {
            "name": "list_conversations",
            "description": (
                "List the user's recent LinkedIn message threads. "
                "Returns thread_id, participants, last_message "
                "preview, last_updated, and unread flag for each "
                "conversation in the messaging inbox."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max threads (1-50, default 20)",
                    },
                },
            },
        },
        {
            "name": "view_conversation",
            "description": (
                "Read messages in a LinkedIn conversation. Returns the "
                "participant list and the last N messages, each with "
                "sender, body, and timestamp. Accepts the thread_id "
                "from list_conversations or a "
                "https://www.linkedin.com/messaging/thread/{id}/ URL."
            ),
            "parameters": {
                "type": "object",
                "required": ["thread"],
                "properties": {
                    "thread": {
                        "type": "string",
                        "description": "thread_id or full /messaging/thread/{id}/ URL",
                    },
                    "message_count": {
                        "type": "integer",
                        "description": "Last N messages to return (1-50, default 20)",
                    },
                },
            },
        },
        {
            "name": "send_message",
            "description": (
                "Send a LinkedIn DM. **Default is dry-run** — types "
                "the message into the composer but does NOT click "
                "Send. Pass confirm=true to actually deliver. "
                "Recipient can be a thread_id (reply to existing "
                "conversation) or a profile URL/handle (open a new "
                "conversation). Returns status='preview' (dry-run, "
                "ready to send), status='sent' (delivered), or "
                "status='error'."
            ),
            "parameters": {
                "type": "object",
                "required": ["recipient", "text"],
                "properties": {
                    "recipient": {
                        "type": "string",
                        "description": (
                            "thread_id (existing conversation) OR "
                            "profile URL / vanity handle (new "
                            "conversation)"
                        ),
                    },
                    "text": {"type": "string", "description": "Message body"},
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "REQUIRED for actual delivery. Defaults "
                            "to false (dry-run). When true, clicks "
                            "Send after composing."
                        ),
                    },
                },
            },
        },
        # ── Networking ──────────────────────────────────────────────
        {
            "name": "send_invitation",
            "description": (
                "Send a LinkedIn connection request to a profile, with "
                "an optional personal note (max 300 chars; LinkedIn "
                "Free caps these notes at ~5/month). **Default is "
                "dry-run** — opens the profile, primes the Connect "
                "dialog, captures what would be sent, then dismisses "
                "the dialog WITHOUT clicking Send. Pass confirm=true "
                "to actually deliver. Returns status='preview' "
                "(dry-run, ready to send), status='already_connected' "
                "(no-op), status='pending' (a previous invite is "
                "already outstanding), status='blocked' (Connect "
                "button hidden behind 'More' or unavailable for this "
                "profile), status='sent' (delivered), or "
                "status='error'."
            ),
            "parameters": {
                "type": "object",
                "required": ["profile"],
                "properties": {
                    "profile": {
                        "type": "string",
                        "description": (
                            "Profile URL "
                            "(https://www.linkedin.com/in/{handle}/) "
                            "or vanity handle"
                        ),
                    },
                    "note": {
                        "type": "string",
                        "description": (
                            "Optional personalized note (max 300 "
                            "chars). Omit or pass empty to send a "
                            "noteless invite."
                        ),
                    },
                    "confirm": {
                        "type": "boolean",
                        "description": (
                            "REQUIRED for actual delivery. Defaults "
                            "to false (dry-run). When true, clicks "
                            "Send after composing."
                        ),
                    },
                },
            },
        },
        # ── Feed + posts (PR4) ──────────────────────────────────────
        {
            "name": "browse_feed",
            "description": (
                "Read the user's LinkedIn home feed. Returns recent "
                "posts with author, body excerpt, post URN, post URL, "
                "reactions count, comments count. Useful for "
                "monitoring what the user's network is sharing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "count": {
                        "type": "integer",
                        "description": "Max posts (1-25, default 10)",
                    },
                },
            },
        },
        {
            "name": "search_posts",
            "description": (
                "Search LinkedIn posts by keywords (content search). "
                "Returns post author, body excerpt, URN, URL, "
                "engagement counts."
            ),
            "parameters": {
                "type": "object",
                "required": ["keywords"],
                "properties": {
                    "keywords": {"type": "string"},
                    "count": {
                        "type": "integer",
                        "description": "Max results (1-25, default 10)",
                    },
                },
            },
        },
        {
            "name": "view_post",
            "description": (
                "Fetch a single LinkedIn post — full body, author, "
                "reactions/comments counts, posted age. Accepts a "
                "/feed/update/{urn}/ URL, /posts/{slug}-{id} permalink, "
                "or a raw urn:li:activity:{id} / urn:li:share:{id} URN."
            ),
            "parameters": {
                "type": "object",
                "required": ["post"],
                "properties": {
                    "post": {
                        "type": "string",
                        "description": "post URL or URN",
                    },
                },
            },
        },
    ]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    return await _browser_runner.call_provider(
        provider="linkedin_browser",
        name=name,
        arguments=arguments,
        bearer_token=bearer_token,
        # LinkedIn pages are heavy; profile loads with all sections
        # often need 30-60s. Leave headroom for slow connections.
        timeout_ms=180_000,
        result_to_pending_action=_to_pending_action,
    )


def _to_pending_action(result: Dict[str, Any]) -> Optional[PendingAction]:
    """Translate per-tool LinkedIn statuses to PendingAction kinds.

    Only fires for statuses where the user can productively act:
      easy_apply blocked + blocking_questions  → needs_input
      send_invitation blocked                   → no PendingAction
        (the user can't fix Connect-button-hidden by signing in or
        answering questions; LLM should report and move on)
      send_message error 'compose box'          → no PendingAction
        (transient, retry handles it)
    """
    status = result.get("status")
    if status != "blocked":
        return None

    # easy_apply: blocking_questions present → needs_input form.
    blocking_questions = result.get("blocking_questions") or []
    if not blocking_questions:
        return None

    # Two shapes are accepted (provider was upgraded to return rich
    # field metadata in commit 7; pre-upgrade provider sends label
    # strings). Normalize to {label, type, options?, required: True}.
    questions: List[Dict[str, Any]] = []
    for q in blocking_questions:
        if isinstance(q, dict):
            label = (q.get("label") or "").strip()
            if not label:
                continue
            entry: Dict[str, Any] = {
                "label": label,
                "type": (q.get("type") or "text"),
                "required": True,
            }
            opts = q.get("options")
            if isinstance(opts, list) and opts:
                entry["options"] = list(opts)
            questions.append(entry)
        elif isinstance(q, str) and q.strip():
            questions.append({"label": q.strip(), "type": "text", "required": True})

    if not questions:
        return None

    job_id = result.get("job_id") or "this LinkedIn job"
    return PendingAction.needs_input(
        questions=questions,
        title="LinkedIn Easy Apply needs your answers",
        context_summary=f"Applying to {job_id}",
    )
