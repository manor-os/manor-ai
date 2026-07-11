"""Gmail MCP server — in-process MCP for Gmail API v1.

Scopes used:
  - https://www.googleapis.com/auth/gmail.send
  - https://www.googleapis.com/auth/gmail.readonly
  - https://www.googleapis.com/auth/gmail.modify

Auth: Google OAuth access_token (from oauth_accounts or Integration credentials,
auto-refreshed via _google_auth._refresh if needed).
"""
from __future__ import annotations

import base64
import json
import logging
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

_API = "https://gmail.googleapis.com/gmail/v1"
_MAX_CHARS = 12_000


# ── MCP Protocol ─────────────────────────────────────────────────────────────

_TOOLS: Dict[str, Dict[str, Any]] = {
    # ── Messages: read ─────────────────────────────────────────────────────
    "list_messages": {
        "description": "List Gmail messages matching a query (Gmail search syntax).",
        "required": ["query"],
        "properties": {
            "query": {"type": "string", "description": "Gmail search query, e.g. 'from:tenant@example.com newer_than:7d'"},
            "max_results": {"type": "integer", "description": "Max messages to return (default 20, max 100)."},
            "page_token": {"type": "string", "description": "Pagination cursor returned by a prior list_messages call."},
        },
    },
    "get_message": {
        "description": "Fetch a single Gmail message by ID (headers + body).",
        "required": ["message_id"],
        "properties": {
            "message_id": {"type": "string"},
            "format": {"type": "string", "enum": ["full", "metadata", "minimal"], "description": "Default 'full'."},
        },
    },
    # ── Messages: write ────────────────────────────────────────────────────
    "send_message": {
        "description": "Send an email from the authenticated user.",
        "required": ["to", "subject", "body"],
        "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
            "reply_to_message_id": {"type": "string", "description": "Optional — include to reply in thread."},
        },
    },
    "reply_to_message": {
        "description": "Reply in an existing Gmail thread.",
        "required": ["message_id", "body"],
        "properties": {
            "message_id": {"type": "string"},
            "body": {"type": "string"},
        },
    },
    # ── Messages: status ───────────────────────────────────────────────────
    "mark_read": {
        "description": "Mark a message as read (removes the UNREAD label).",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    },
    "mark_unread": {
        "description": "Mark a message as unread (adds the UNREAD label).",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    },
    "archive_message": {
        "description": "Archive a message — removes it from INBOX without deleting.",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    },
    "mark_spam": {
        "description": "Mark a message as spam (adds the SPAM label, removes INBOX).",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    },
    "trash_message": {
        "description": "Move a message to Trash. Reversible via untrash_message within ~30 days.",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    },
    "untrash_message": {
        "description": "Restore a message from Trash to its prior labels.",
        "required": ["message_id"],
        "properties": {"message_id": {"type": "string"}},
    },
    "batch_modify": {
        "description": (
            "Apply the same label changes to up to 1000 messages in one request. "
            "Pass either add_labels, remove_labels, or both as comma-separated names "
            "(custom names auto-resolve; system labels like INBOX/UNREAD/STARRED work too)."
        ),
        "required": ["message_ids"],
        "properties": {
            "message_ids": {"type": "array", "items": {"type": "string"}, "description": "Up to 1000 IDs"},
            "add_labels": {"type": "string", "description": "Comma-separated labels to add"},
            "remove_labels": {"type": "string", "description": "Comma-separated labels to remove"},
        },
    },
    # ── Drafts ─────────────────────────────────────────────────────────────
    "list_drafts": {
        "description": "List the authenticated user's drafts.",
        "required": [],
        "properties": {
            "query": {"type": "string", "description": "Optional Gmail search syntax filter."},
            "max_results": {"type": "integer", "description": "Default 20, max 100."},
        },
    },
    "get_draft": {
        "description": "Fetch a draft (including the embedded message body).",
        "required": ["draft_id"],
        "properties": {
            "draft_id": {"type": "string"},
            "format": {"type": "string", "enum": ["full", "metadata", "minimal"]},
        },
    },
    "create_draft": {
        "description": (
            "Create an unsent draft for human review. Use update_draft / "
            "send_draft afterwards to refine or send."
        ),
        "required": ["to", "subject", "body"],
        "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
            "thread_message_id": {"type": "string", "description": "If set, creates a reply draft in that thread."},
        },
    },
    "update_draft": {
        "description": "Replace the contents of an existing draft.",
        "required": ["draft_id", "to", "subject", "body"],
        "properties": {
            "draft_id": {"type": "string"},
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
        },
    },
    "send_draft": {
        "description": "Send a previously-prepared draft. Returns the resulting message id.",
        "required": ["draft_id"],
        "properties": {"draft_id": {"type": "string"}},
    },
    "delete_draft": {
        "description": "Permanently delete a draft.",
        "required": ["draft_id"],
        "properties": {"draft_id": {"type": "string"}},
    },
    # ── Threads ────────────────────────────────────────────────────────────
    "list_threads": {
        "description": "List threads matching a Gmail query (returns thread ids + snippets).",
        "required": ["query"],
        "properties": {
            "query": {"type": "string"},
            "max_results": {"type": "integer", "description": "Default 20, max 100."},
        },
    },
    "get_thread": {
        "description": "Fetch a thread including every message in it.",
        "required": ["thread_id"],
        "properties": {
            "thread_id": {"type": "string"},
            "format": {"type": "string", "enum": ["full", "metadata", "minimal"]},
        },
    },
    "trash_thread": {
        "description": "Move an entire thread to Trash.",
        "required": ["thread_id"],
        "properties": {"thread_id": {"type": "string"}},
    },
    # ── Attachments ────────────────────────────────────────────────────────
    "download_attachment": {
        "description": (
            "Fetch the body bytes of a single attachment (base64url-encoded). "
            "Get attachment_id from get_message → payload.parts[].body.attachmentId."
        ),
        "required": ["message_id", "attachment_id"],
        "properties": {
            "message_id": {"type": "string"},
            "attachment_id": {"type": "string"},
        },
    },
    # ── Labels ─────────────────────────────────────────────────────────────
    "list_labels": {
        "description": "List all labels in the user's mailbox (system + custom).",
        "required": [],
        "properties": {},
    },
    "create_label": {
        "description": "Create a new custom label.",
        "required": ["name"],
        "properties": {
            "name": {"type": "string"},
            "label_list_visibility": {"type": "string", "enum": ["labelShow", "labelHide", "labelShowIfUnread"]},
            "message_list_visibility": {"type": "string", "enum": ["show", "hide"]},
        },
    },
    "delete_label": {
        "description": "Delete a custom label (cannot delete system labels).",
        "required": ["label_id"],
        "properties": {"label_id": {"type": "string"}},
    },
    "add_label": {
        "description": "Apply a label (e.g. INBOX, STARRED, or a custom label name) to a message.",
        "required": ["message_id", "label"],
        "properties": {
            "message_id": {"type": "string"},
            "label": {"type": "string"},
        },
    },
    "remove_label": {
        "description": "Remove a label from a message.",
        "required": ["message_id", "label"],
        "properties": {
            "message_id": {"type": "string"},
            "label": {"type": "string"},
        },
    },
    # ── Profile ────────────────────────────────────────────────────────────
    "get_profile": {
        "description": "Get the authenticated user's Gmail profile (email address, message totals).",
        "required": [],
        "properties": {},
    },
}


def list_tools() -> List[Dict[str, Any]]:
    return [_tool_def(name, spec) for name, spec in _TOOLS.items()]


async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    handler = _HANDLERS.get(name)
    if not handler:
        return _error(f"Unknown tool: {name}")

    spec = _TOOLS.get(name, {})
    missing = [p for p in spec.get("required", []) if arguments.get(p) in (None, "")]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")

    try:
        text = await handler(bearer_token, arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("Gmail MCP tool %s failed", name)
        return _error(str(e))


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# ── Simulation (dry_run / sandbox plans) ────────────────────────────────────

async def simulate_tool(
    name: str,
    arguments: Dict[str, Any],
) -> Dict[str, Any]:
    """Schema-realistic fake response for sandbox / dry_run plans.

    Mirrors the same envelope shape ``call_tool`` returns. Used by the
    InternalWorker's ``_simulate`` path when the plan's execution_mode
    is ``dry_run`` or ``sandbox`` — operator can preview the briefing
    pipeline end-to-end without burning real Gmail quota.
    """
    handler = _SIMULATORS.get(name)
    if handler is None:
        text = json.dumps({"_simulated": True, "tool": name, "input": arguments})
        return {"content": [{"type": "text", "text": text}], "isError": False}
    try:
        text = handler(arguments)
        return {"content": [{"type": "text", "text": text}], "isError": False}
    except Exception as e:
        logger.exception("Gmail simulator %s failed", name)
        return _error(str(e))


def _sim_list_messages(args: Dict) -> str:
    """Return a believable handful of unread messages so a sandbox
    briefing has signal to triage."""
    n = min(int(args.get("max_results") or 10), 10)
    fixtures = _DEMO_INBOX[:n]
    return json.dumps({
        "messages": [{"id": m["id"], "threadId": m["thread_id"]} for m in fixtures],
        "resultSizeEstimate": len(fixtures),
        "_simulated": True,
    })


def _sim_get_message(args: Dict) -> str:
    msg_id = args.get("message_id") or args.get("id") or _DEMO_INBOX[0]["id"]
    msg = next(
        (m for m in _DEMO_INBOX if m["id"] == msg_id),
        _DEMO_INBOX[0],
    )
    # Return Gmail-style envelope (subset — what the briefing prompt reads)
    return json.dumps({
        "id": msg["id"],
        "threadId": msg["thread_id"],
        "snippet": msg["snippet"],
        "labelIds": ["INBOX", "UNREAD"],
        "payload": {
            "headers": [
                {"name": "From", "value": msg["from"]},
                {"name": "Subject", "value": msg["subject"]},
                {"name": "Date", "value": msg["date"]},
            ],
            "body": {"data": msg["body_b64"]},
        },
        "_simulated": True,
    })


def _sim_send_message(args: Dict) -> str:
    return json.dumps({
        "id": "msg_" + (args.get("subject", "x")[:8]).replace(" ", "_"),
        "threadId": "thr_simulated",
        "labelIds": ["SENT"],
        "_simulated": True,
    })


def _sim_reply_to_message(args: Dict) -> str:
    return json.dumps({
        "id": "msg_reply_simulated",
        "threadId": args.get("thread_id", "thr_simulated"),
        "labelIds": ["SENT"],
        "_simulated": True,
    })


# Realistic-looking mixed inbox: one urgent, one routine question, one
# transactional, one promotional. Briefing should triage these into
# different action buckets.
import base64 as _b64
def _b64body(text: str) -> str:
    return _b64.urlsafe_b64encode(text.encode("utf-8")).decode("ascii")


_DEMO_INBOX: List[Dict[str, Any]] = [
    {
        "id": "sim_msg_001", "thread_id": "sim_thr_001",
        "from": "Acme Inc <billing@acme.com>",
        "subject": "Invoice #4218 — payment due tomorrow",
        "date": "Mon, 14 Apr 2026 09:14:22 -0700",
        "snippet": "Hi — your monthly subscription invoice for $1,200 is due 2026-04-15. Pay now to avoid suspension.",
        "body_b64": _b64body(
            "Hi,\n\nYour monthly Acme subscription invoice for $1,200 is due "
            "tomorrow (2026-04-15). Pay before midnight PT to avoid service "
            "suspension.\n\nView invoice: https://acme.com/inv/4218\n\nThanks!"
        ),
    },
    {
        "id": "sim_msg_002", "thread_id": "sim_thr_002",
        "from": "Sarah Chen <sarah.chen@example.com>",
        "subject": "Quick question on the proposal",
        "date": "Mon, 14 Apr 2026 11:02:05 -0700",
        "snippet": "Hey — re: the proposal you sent Friday, can we move the kickoff to the 21st instead?",
        "body_b64": _b64body(
            "Hi there,\n\nThanks again for the proposal on Friday — it looks "
            "great. Quick scheduling question: can we move the project "
            "kickoff to Tuesday April 21st instead of the 17th? I'm flying "
            "back from a conference and the 17th is tight.\n\nLet me know "
            "what works.\n\nSarah"
        ),
    },
    {
        "id": "sim_msg_003", "thread_id": "sim_thr_003",
        "from": "Stripe <receipts@stripe.com>",
        "subject": "Payment received — $89.00",
        "date": "Sun, 13 Apr 2026 22:11:00 -0700",
        "snippet": "Customer paid $89.00 — receipt #ch_3OXYZ.",
        "body_b64": _b64body(
            "You received a payment of $89.00 USD from a customer.\n"
            "Receipt: ch_3OXYZ\nView in Stripe: https://stripe.com/payments/3OXYZ"
        ),
    },
    {
        "id": "sim_msg_004", "thread_id": "sim_thr_004",
        "from": "TechCrunch Daily <newsletter@techcrunch.com>",
        "subject": "🚀 Today's top 5 AI startups",
        "date": "Mon, 14 Apr 2026 06:00:01 -0700",
        "snippet": "Your daily digest of AI funding news + product launches.",
        "body_b64": _b64body("Daily AI digest content..."),
    },
    {
        "id": "sim_msg_005", "thread_id": "sim_thr_005",
        "from": "Jordan Lee <jordan@vendorco.com>",
        "subject": "Following up on our call",
        "date": "Fri, 11 Apr 2026 16:48:33 -0700",
        "snippet": "Hi — just bumping this back up. Ready to start whenever you give the green light.",
        "body_b64": _b64body(
            "Hey,\n\nJust bumping our chat from last Thursday back up. "
            "Quoting from notes: you mentioned wanting to kick off in mid-April. "
            "We're ready whenever you give the green light. Should I send "
            "the standard contract draft over today?\n\nJordan"
        ),
    },
]


_SIMULATORS = {
    "list_messages": _sim_list_messages,
    "get_message": _sim_get_message,
    "send_message": _sim_send_message,
    "reply_to_message": _sim_reply_to_message,
}


# ── HTTP client ──────────────────────────────────────────────────────────────

async def _api(
    token: str,
    method: str,
    path: str,
    body: Optional[Dict] = None,
    params: Optional[Dict] = None,
) -> str:
    url = f"{_API}/{path.lstrip('/')}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.request(
            method, url, headers=headers, json=body, params=params or {},
        )

    if resp.status_code == 401:
        raise RuntimeError("Gmail auth failed. Reconnect Google under Settings → Integrations.")
    if resp.status_code == 403:
        raise RuntimeError(f"Gmail forbidden (scope or permissions): {resp.text[:300]}")
    if resp.status_code == 404:
        raise RuntimeError("Not found.")
    if resp.status_code == 204:
        return json.dumps({"success": True})
    if not resp.is_success:
        raise RuntimeError(f"Gmail API error ({resp.status_code}): {resp.text[:300]}")

    try:
        data = resp.json()
    except Exception:
        return resp.text[:_MAX_CHARS]
    out = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    if len(out) > _MAX_CHARS:
        return out[:_MAX_CHARS] + "\n… (truncated)"
    return out


# ── Tool handlers ────────────────────────────────────────────────────────────

async def _list_messages(token: str, args: Dict) -> str:
    params = {
        "q": args["query"],
        "maxResults": min(int(args.get("max_results") or 20), 100),
    }
    if args.get("page_token"):
        params["pageToken"] = args["page_token"]
    return await _api(token, "GET", "users/me/messages", params=params)


async def _get_message(token: str, args: Dict) -> str:
    fmt = args.get("format") or "full"
    return await _api(
        token, "GET",
        f"users/me/messages/{args['message_id']}",
        params={"format": fmt},
    )


async def _send_message(token: str, args: Dict) -> str:
    msg = MIMEText(args["body"])
    msg["to"] = args["to"]
    msg["subject"] = args["subject"]
    if args.get("cc"):
        msg["cc"] = args["cc"]
    if args.get("bcc"):
        msg["bcc"] = args["bcc"]

    body: Dict[str, Any] = {
        "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode(),
    }
    if args.get("reply_to_message_id"):
        thread_resp = await _api(
            token, "GET", f"users/me/messages/{args['reply_to_message_id']}",
            params={"format": "metadata"},
        )
        try:
            thread_data = json.loads(thread_resp)
            body["threadId"] = thread_data.get("threadId")
        except Exception:
            pass
    return await _api(token, "POST", "users/me/messages/send", body=body)


async def _reply_to_message(token: str, args: Dict) -> str:
    # Look up thread id + original headers so the reply threads properly
    meta = await _api(
        token, "GET", f"users/me/messages/{args['message_id']}",
        params={"format": "metadata",
                "metadataHeaders": ["Subject", "From", "Message-ID", "References"]},
    )
    try:
        data = json.loads(meta)
    except Exception:
        return "Failed to look up original message headers."

    thread_id = data.get("threadId")
    headers_list = (data.get("payload") or {}).get("headers") or []
    hmap = {h.get("name", "").lower(): h.get("value", "") for h in headers_list}
    subject = hmap.get("subject", "")
    if subject and not subject.lower().startswith("re:"):
        subject = f"Re: {subject}"

    reply_to = hmap.get("from", "")
    orig_msgid = hmap.get("message-id", "")

    msg = MIMEText(args["body"])
    msg["to"] = reply_to
    msg["subject"] = subject
    if orig_msgid:
        msg["In-Reply-To"] = orig_msgid
        refs = hmap.get("references", "")
        msg["References"] = (refs + " " + orig_msgid).strip() if refs else orig_msgid

    body = {
        "raw": base64.urlsafe_b64encode(msg.as_bytes()).decode(),
        "threadId": thread_id,
    }
    return await _api(token, "POST", "users/me/messages/send", body=body)


async def _resolve_label_ids(
    token: str, names_or_ids: List[str],
) -> List[str]:
    """Map a list of label names or IDs into Gmail label IDs.
    System labels (INBOX, UNREAD, STARRED, …) are accepted in any case.
    Custom labels are looked up by name; unknown ones are silently
    dropped — caller should pre-validate when correctness matters."""
    if not names_or_ids:
        return []
    labels_resp = await _api(token, "GET", "users/me/labels")
    try:
        labels = json.loads(labels_resp).get("labels", [])
    except Exception:
        labels = []
    by_id = {l.get("id"): l for l in labels}
    by_name = {(l.get("name") or "").lower(): l for l in labels}
    out: List[str] = []
    for tok in names_or_ids:
        if not tok:
            continue
        if tok in by_id:
            out.append(tok)
        elif tok.lower() in by_name:
            out.append(by_name[tok.lower()].get("id"))
        elif tok.upper() in by_id:
            out.append(tok.upper())
        # else: silently drop — Gmail returns 400 if any id is invalid,
        # which is worse for batch ops than a quiet skip.
    return [x for x in out if x]


def _csv_list(v: Any) -> List[str]:
    if not v:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if x]
    return [s.strip() for s in str(v).split(",") if s.strip()]


async def _add_label(token: str, args: Dict) -> str:
    ids = await _resolve_label_ids(token, [args["label"]])
    if not ids:
        return f"Label not found: {args['label']}"
    return await _api(
        token, "POST",
        f"users/me/messages/{args['message_id']}/modify",
        body={"addLabelIds": ids},
    )


async def _remove_label(token: str, args: Dict) -> str:
    ids = await _resolve_label_ids(token, [args["label"]])
    if not ids:
        return f"Label not found: {args['label']}"
    return await _api(
        token, "POST",
        f"users/me/messages/{args['message_id']}/modify",
        body={"removeLabelIds": ids},
    )


# ── Status changes (label-tweak shortcuts) ──────────────────────────────────

async def _modify_message(
    token: str, message_id: str,
    *, add: Optional[List[str]] = None, remove: Optional[List[str]] = None,
) -> str:
    body: Dict[str, Any] = {}
    if add:
        body["addLabelIds"] = add
    if remove:
        body["removeLabelIds"] = remove
    return await _api(token, "POST", f"users/me/messages/{message_id}/modify", body=body)


async def _mark_read(token: str, args: Dict) -> str:
    return await _modify_message(token, args["message_id"], remove=["UNREAD"])


async def _mark_unread(token: str, args: Dict) -> str:
    return await _modify_message(token, args["message_id"], add=["UNREAD"])


async def _archive_message(token: str, args: Dict) -> str:
    return await _modify_message(token, args["message_id"], remove=["INBOX"])


async def _mark_spam(token: str, args: Dict) -> str:
    return await _modify_message(
        token, args["message_id"], add=["SPAM"], remove=["INBOX"],
    )


async def _trash_message(token: str, args: Dict) -> str:
    return await _api(token, "POST", f"users/me/messages/{args['message_id']}/trash")


async def _untrash_message(token: str, args: Dict) -> str:
    return await _api(token, "POST", f"users/me/messages/{args['message_id']}/untrash")


async def _batch_modify(token: str, args: Dict) -> str:
    ids = list(args["message_ids"]) if isinstance(args["message_ids"], list) else _csv_list(args["message_ids"])
    if not ids:
        return "message_ids cannot be empty"
    if len(ids) > 1000:
        return "batch_modify accepts at most 1000 message_ids per call"
    add_label_ids = await _resolve_label_ids(token, _csv_list(args.get("add_labels")))
    remove_label_ids = await _resolve_label_ids(token, _csv_list(args.get("remove_labels")))
    body: Dict[str, Any] = {"ids": ids}
    if add_label_ids:
        body["addLabelIds"] = add_label_ids
    if remove_label_ids:
        body["removeLabelIds"] = remove_label_ids
    return await _api(token, "POST", "users/me/messages/batchModify", body=body)


# ── Drafts ──────────────────────────────────────────────────────────────────

def _build_raw_message(args: Dict) -> str:
    msg = MIMEText(args["body"])
    msg["to"] = args["to"]
    msg["subject"] = args["subject"]
    if args.get("cc"):
        msg["cc"] = args["cc"]
    if args.get("bcc"):
        msg["bcc"] = args["bcc"]
    return base64.urlsafe_b64encode(msg.as_bytes()).decode()


async def _list_drafts(token: str, args: Dict) -> str:
    params: Dict[str, Any] = {
        "maxResults": min(int(args.get("max_results") or 20), 100),
    }
    if args.get("query"):
        params["q"] = args["query"]
    return await _api(token, "GET", "users/me/drafts", params=params)


async def _get_draft(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", f"users/me/drafts/{args['draft_id']}",
        params={"format": args.get("format") or "full"},
    )


async def _create_draft(token: str, args: Dict) -> str:
    raw = _build_raw_message(args)
    body: Dict[str, Any] = {"message": {"raw": raw}}
    if args.get("thread_message_id"):
        meta = await _api(
            token, "GET", f"users/me/messages/{args['thread_message_id']}",
            params={"format": "metadata"},
        )
        try:
            tid = json.loads(meta).get("threadId")
            if tid:
                body["message"]["threadId"] = tid
        except Exception:
            pass
    return await _api(token, "POST", "users/me/drafts", body=body)


async def _update_draft(token: str, args: Dict) -> str:
    raw = _build_raw_message(args)
    body: Dict[str, Any] = {"message": {"raw": raw}}
    return await _api(
        token, "PUT", f"users/me/drafts/{args['draft_id']}", body=body,
    )


async def _send_draft(token: str, args: Dict) -> str:
    return await _api(
        token, "POST", "users/me/drafts/send", body={"id": args["draft_id"]},
    )


async def _delete_draft(token: str, args: Dict) -> str:
    return await _api(token, "DELETE", f"users/me/drafts/{args['draft_id']}")


# ── Threads ─────────────────────────────────────────────────────────────────

async def _list_threads(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", "users/me/threads",
        params={
            "q": args["query"],
            "maxResults": min(int(args.get("max_results") or 20), 100),
        },
    )


async def _get_thread(token: str, args: Dict) -> str:
    return await _api(
        token, "GET", f"users/me/threads/{args['thread_id']}",
        params={"format": args.get("format") or "full"},
    )


async def _trash_thread(token: str, args: Dict) -> str:
    return await _api(
        token, "POST", f"users/me/threads/{args['thread_id']}/trash",
    )


# ── Attachments ─────────────────────────────────────────────────────────────

async def _download_attachment(token: str, args: Dict) -> str:
    return await _api(
        token, "GET",
        f"users/me/messages/{args['message_id']}/attachments/{args['attachment_id']}",
    )


# ── Labels CRUD ─────────────────────────────────────────────────────────────

async def _list_labels(token: str, args: Dict) -> str:
    return await _api(token, "GET", "users/me/labels")


async def _create_label(token: str, args: Dict) -> str:
    body: Dict[str, Any] = {"name": args["name"]}
    if args.get("label_list_visibility"):
        body["labelListVisibility"] = args["label_list_visibility"]
    if args.get("message_list_visibility"):
        body["messageListVisibility"] = args["message_list_visibility"]
    return await _api(token, "POST", "users/me/labels", body=body)


async def _delete_label(token: str, args: Dict) -> str:
    return await _api(token, "DELETE", f"users/me/labels/{args['label_id']}")


# ── Profile ─────────────────────────────────────────────────────────────────

async def _get_profile(token: str, args: Dict) -> str:
    return await _api(token, "GET", "users/me/profile")


_HANDLERS = {
    # Messages: read
    "list_messages": _list_messages,
    "get_message": _get_message,
    # Messages: write
    "send_message": _send_message,
    "reply_to_message": _reply_to_message,
    # Messages: status
    "mark_read": _mark_read,
    "mark_unread": _mark_unread,
    "archive_message": _archive_message,
    "mark_spam": _mark_spam,
    "trash_message": _trash_message,
    "untrash_message": _untrash_message,
    "batch_modify": _batch_modify,
    # Drafts
    "list_drafts": _list_drafts,
    "get_draft": _get_draft,
    "create_draft": _create_draft,
    "update_draft": _update_draft,
    "send_draft": _send_draft,
    "delete_draft": _delete_draft,
    # Threads
    "list_threads": _list_threads,
    "get_thread": _get_thread,
    "trash_thread": _trash_thread,
    # Attachments
    "download_attachment": _download_attachment,
    # Labels
    "list_labels": _list_labels,
    "create_label": _create_label,
    "delete_label": _delete_label,
    "add_label": _add_label,
    "remove_label": _remove_label,
    # Profile
    "get_profile": _get_profile,
}


# ── Schema helpers ───────────────────────────────────────────────────────────

def _tool_def(name: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": name,
        "description": spec["description"],
        "inputSchema": {
            "type": "object",
            "required": spec.get("required", []),
            "properties": spec.get("properties", {}),
        },
    }
