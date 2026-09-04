"""Email MCP module — IMAP (read) + SMTP (send), stdlib-only.

Consolidates all email read/write/send actions behind a single
``email`` provider. One credential bundle configures both protocols:

    {
      "imap_host": "imap.gmail.com",
      "imap_port": 993,
      "smtp_host": "smtp.gmail.com",
      "smtp_port": 587,
      "username": "alice@example.com",
      "password": "app-password-or-plain",
      "from_address": "Alice <alice@example.com>",
      "use_ssl_imap": true,     # optional, default port==993
      "use_tls_smtp": true,     # optional, default port==587
      "use_ssl_smtp": false     # optional, default port==465
    }

Dispatch contract: the MCP runtime JSON-encodes the integration's
``credentials`` dict and hands it to every ``call_tool`` as
``bearer_token``. This module decodes it.

All blocking I/O (``imaplib``, ``smtplib``) is wrapped in
``asyncio.to_thread`` so callers stay non-blocking.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import imaplib
import json
import logging
import re
import smtplib
from collections import defaultdict
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from typing import Any, Dict, List

from packages.core.services import smtp_transport

logger = logging.getLogger(__name__)

_MAX_CHARS = 12_000
_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_ATTACHMENT_BYTES = 20 * 1024 * 1024
_MAX_THREAD_SCAN = 500


_ATTACHMENTS_SCHEMA: Dict[str, Any] = {
    "type": "array",
    "description": "Optional MIME attachments encoded as base64 (max 10 files / 20 MiB total).",
    "maxItems": 10,
    "items": {
        "type": "object",
        "required": ["filename", "data_base64"],
        "properties": {
            "filename": {"type": "string"},
            "content_type": {
                "type": "string",
                "description": "MIME type; defaults to application/octet-stream.",
            },
            "data_base64": {"type": "string"},
        },
    },
}


# ── Tool schemas ────────────────────────────────────────────────────────────

_TOOLS: Dict[str, Dict[str, Any]] = {
    "send_email": {
        "description": "Send an email through the configured SMTP relay.",
        "required": ["to", "subject", "body"],
        "properties": {
            "to": {"type": "string", "description": "Address or comma-separated list."},
            "subject": {"type": "string"},
            "body": {"type": "string", "description": "Plain-text body."},
            "html": {"type": "string", "description": "Optional HTML alternative body."},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
            "from_address": {"type": "string"},
            "reply_to": {"type": "string"},
            "attachments": _ATTACHMENTS_SCHEMA,
        },
    },
    "list_messages": {
        "description": "List messages in a mailbox folder with optional filters.",
        "required": [],
        "properties": {
            "folder": {"type": "string", "description": "IMAP folder (default 'INBOX')."},
            "unseen_only": {"type": "boolean"},
            "from_address": {"type": "string", "description": "Filter by sender."},
            "subject_contains": {"type": "string"},
            "body_contains": {"type": "string"},
            "since": {"type": "string", "description": "YYYY-MM-DD — messages on/after this date."},
            "before": {"type": "string", "description": "YYYY-MM-DD — messages before this date."},
            "max_results": {"type": "integer", "description": "Default 20, max 100."},
        },
    },
    "get_message": {
        "description": "Fetch one message by UID with headers + body.",
        "required": ["uid"],
        "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Default 'INBOX'."},
            "format": {"type": "string", "enum": ["full", "text", "headers"],
                       "description": "Default 'full'."},
        },
    },
    "list_attachments": {
        "description": "List attachment metadata for one message by UID.",
        "required": ["uid"],
        "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Default 'INBOX'."},
        },
    },
    "download_attachment": {
        "description": "Download one message attachment as base64-encoded bytes.",
        "required": ["uid", "attachment_id"],
        "properties": {
            "uid": {"type": "string"},
            "attachment_id": {
                "type": "string",
                "description": "ID returned by list_attachments/get_message.",
            },
            "folder": {"type": "string", "description": "Default 'INBOX'."},
        },
    },
    "reply_to_message": {
        "description": "Reply to one message and preserve RFC email thread headers.",
        "required": ["uid", "body"],
        "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Default 'INBOX'."},
            "body": {"type": "string"},
            "html": {"type": "string"},
            "attachments": _ATTACHMENTS_SCHEMA,
        },
    },
    "reply_all": {
        "description": "Reply to the sender and all original recipients, excluding the connected account.",
        "required": ["uid", "body"],
        "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Default 'INBOX'."},
            "body": {"type": "string"},
            "html": {"type": "string"},
            "attachments": _ATTACHMENTS_SCHEMA,
        },
    },
    "list_threads": {
        "description": "Group matching messages into RFC-header-based conversation threads.",
        "required": [],
        "properties": {
            "folder": {"type": "string", "description": "Default 'INBOX'."},
            "unseen_only": {"type": "boolean"},
            "from_address": {"type": "string"},
            "subject_contains": {"type": "string"},
            "body_contains": {"type": "string"},
            "since": {"type": "string", "description": "YYYY-MM-DD."},
            "before": {"type": "string", "description": "YYYY-MM-DD."},
            "max_results": {"type": "integer", "description": "Default 20, max 100 threads."},
            "max_messages_scan": {
                "type": "integer",
                "description": "Maximum matching messages to group; default 200, max 500.",
            },
        },
    },
    "get_thread": {
        "description": "Fetch every discoverable message in the conversation containing an anchor UID.",
        "required": ["uid"],
        "properties": {
            "uid": {"type": "string", "description": "Anchor message UID."},
            "folder": {"type": "string", "description": "Anchor folder; default 'INBOX'."},
            "search_folders": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Folders to scan. Defaults to the anchor folder only; include Sent for two-sided history.",
            },
            "format": {"type": "string", "enum": ["full", "text", "headers"]},
            "max_messages_scan": {"type": "integer", "description": "Default 500, max 500 per folder."},
        },
    },
    "list_drafts": {
        "description": "List drafts stored in the IMAP Drafts folder.",
        "required": [],
        "properties": {
            "folder": {"type": "string", "description": "Optional Drafts folder override."},
            "max_results": {"type": "integer", "description": "Default 20, max 100."},
        },
    },
    "get_draft": {
        "description": "Fetch one draft by IMAP UID.",
        "required": ["uid"],
        "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Optional Drafts folder override."},
        },
    },
    "create_draft": {
        "description": "Create an unsent MIME draft in the IMAP Drafts folder.",
        "required": ["to", "subject", "body"],
        "properties": {
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "html": {"type": "string"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
            "reply_to": {"type": "string"},
            "folder": {"type": "string", "description": "Optional Drafts folder override."},
            "reply_to_uid": {"type": "string", "description": "Optional message UID to make this an in-thread reply draft."},
            "reply_to_folder": {"type": "string", "description": "Folder containing reply_to_uid; default INBOX."},
            "reply_all": {"type": "boolean", "description": "With reply_to_uid, include all original recipients."},
            "attachments": _ATTACHMENTS_SCHEMA,
        },
    },
    "update_draft": {
        "description": "Replace an existing IMAP draft; returns the replacement UID.",
        "required": ["uid", "to", "subject", "body"],
        "properties": {
            "uid": {"type": "string"},
            "to": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "html": {"type": "string"},
            "cc": {"type": "string"},
            "bcc": {"type": "string"},
            "reply_to": {"type": "string"},
            "folder": {"type": "string", "description": "Optional Drafts folder override."},
            "attachments": _ATTACHMENTS_SCHEMA,
        },
    },
    "send_draft": {
        "description": "Send an IMAP draft through SMTP, then remove the draft after a successful send.",
        "required": ["uid"],
        "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Optional Drafts folder override."},
        },
    },
    "delete_draft": {
        "description": "Delete an IMAP draft by UID.",
        "required": ["uid"],
        "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string", "description": "Optional Drafts folder override."},
        },
    },
    "mark_read": {
        "description": "Mark a message as read (\\Seen).",
        "required": ["uid"],
        "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string"},
        },
    },
    "mark_unread": {
        "description": "Mark a message as unread (remove \\Seen).",
        "required": ["uid"],
        "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string"},
        },
    },
    "move_message": {
        "description": "Move a message to another folder.",
        "required": ["uid", "to_folder"],
        "properties": {
            "uid": {"type": "string"},
            "from_folder": {"type": "string"},
            "to_folder": {"type": "string"},
        },
    },
    "delete_message": {
        "description": "Delete a message (flag \\Deleted + expunge).",
        "required": ["uid"],
        "properties": {
            "uid": {"type": "string"},
            "folder": {"type": "string"},
        },
    },
    "list_folders": {
        "description": "List all IMAP folders (mailboxes) available.",
        "required": [],
        "properties": {},
    },
}


def list_tools() -> List[Dict[str, Any]]:
    return [
        {
            "name": name,
            "description": spec["description"],
            "inputSchema": {
                "type": "object",
                "required": spec.get("required", []),
                "properties": spec.get("properties", {}),
            },
        }
        for name, spec in _TOOLS.items()
    ]


# ── Entry point ─────────────────────────────────────────────────────────────

async def call_tool(
    name: str,
    arguments: Dict[str, Any],
    bearer_token: str,
) -> Dict[str, Any]:
    spec = _TOOLS.get(name)
    if not spec:
        return _error(f"Unknown tool: {name}")

    missing = [p for p in spec.get("required", []) if arguments.get(p) in (None, "")]
    if missing:
        return _error(f"Missing required params: {', '.join(missing)}")

    try:
        cfg = json.loads(bearer_token) if bearer_token else {}
    except Exception:
        return _error("Email credentials malformed.")

    try:
        if name == "send_email":
            text = await asyncio.to_thread(_send_email, cfg, arguments)
        elif name == "list_messages":
            text = await asyncio.to_thread(_list_messages, cfg, arguments)
        elif name == "get_message":
            text = await asyncio.to_thread(_get_message, cfg, arguments)
        elif name == "list_attachments":
            text = await asyncio.to_thread(_list_attachments, cfg, arguments)
        elif name == "download_attachment":
            text = await asyncio.to_thread(_download_attachment, cfg, arguments)
        elif name == "reply_to_message":
            text = await asyncio.to_thread(_reply_to_message, cfg, arguments, False)
        elif name == "reply_all":
            text = await asyncio.to_thread(_reply_to_message, cfg, arguments, True)
        elif name == "list_threads":
            text = await asyncio.to_thread(_list_threads, cfg, arguments)
        elif name == "get_thread":
            text = await asyncio.to_thread(_get_thread, cfg, arguments)
        elif name == "list_drafts":
            text = await asyncio.to_thread(_list_drafts, cfg, arguments)
        elif name == "get_draft":
            text = await asyncio.to_thread(_get_draft, cfg, arguments)
        elif name == "create_draft":
            text = await asyncio.to_thread(_create_draft, cfg, arguments)
        elif name == "update_draft":
            text = await asyncio.to_thread(_update_draft, cfg, arguments)
        elif name == "send_draft":
            text = await asyncio.to_thread(_send_draft, cfg, arguments)
        elif name == "delete_draft":
            text = await asyncio.to_thread(_delete_draft, cfg, arguments)
        elif name == "mark_read":
            text = await asyncio.to_thread(_flag, cfg, arguments, "+FLAGS", r"\Seen")
        elif name == "mark_unread":
            text = await asyncio.to_thread(_flag, cfg, arguments, "-FLAGS", r"\Seen")
        elif name == "move_message":
            text = await asyncio.to_thread(_move_message, cfg, arguments)
        elif name == "delete_message":
            text = await asyncio.to_thread(_delete_message, cfg, arguments)
        elif name == "list_folders":
            text = await asyncio.to_thread(_list_folders, cfg)
        else:
            return _error(f"Unhandled tool: {name}")
    except _EmailError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("Email MCP tool %s failed", name)
        return _error(f"{name} failed: {e}")

    # Attachment bytes are intentionally returned losslessly; truncating a
    # base64 payload would silently corrupt the downloaded file.
    lossless_tools = {"download_attachment", "get_message", "get_thread", "get_draft"}
    payload = text if name in lossless_tools else _truncate(text)
    return {"content": [{"type": "text", "text": payload}], "isError": False}


# ── SMTP send (blocking helper) ─────────────────────────────────────────────

def _send_email(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    from_addr = args.get("from_address") or cfg.get("from_address") or cfg.get("username")
    msg = _compose_message(args, from_addr=from_addr, include_bcc=False)
    _smtp_send_message(cfg, msg, bcc=args.get("bcc") or "")

    return json.dumps({
        "success": True, "to": args["to"],
        "from": from_addr, "subject": args["subject"],
        "attachment_count": len(args.get("attachments") or []),
    })


def _compose_message(
    args: Dict[str, Any],
    *,
    from_addr: str | None,
    thread_headers: Dict[str, str] | None = None,
    include_bcc: bool = False,
) -> EmailMessage:
    if not from_addr:
        raise _EmailError("No From address configured.")

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = args["to"]
    msg["Subject"] = args["subject"]
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if args.get("cc"):
        msg["Cc"] = args["cc"]
    if include_bcc and args.get("bcc"):
        msg["Bcc"] = args["bcc"]
    if args.get("reply_to"):
        msg["Reply-To"] = args["reply_to"]
    for key, value in (thread_headers or {}).items():
        if value:
            msg[key] = value

    msg.set_content(args["body"])
    if args.get("html"):
        msg.add_alternative(args["html"], subtype="html")
    _add_attachments(msg, args.get("attachments") or [])
    return msg


def _add_attachments(msg: EmailMessage, attachments: Any) -> None:
    if not isinstance(attachments, list):
        raise _EmailError("attachments must be an array.")
    if len(attachments) > 10:
        raise _EmailError("At most 10 attachments are allowed.")

    total = 0
    for index, item in enumerate(attachments):
        if not isinstance(item, dict):
            raise _EmailError(f"Attachment {index + 1} must be an object.")
        filename = str(item.get("filename") or "").strip()
        encoded = item.get("data_base64")
        if not filename or not isinstance(encoded, str):
            raise _EmailError(f"Attachment {index + 1} needs filename and data_base64.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise _EmailError(f"Attachment '{filename}' has invalid base64 data.")
        if len(data) > _MAX_ATTACHMENT_BYTES:
            raise _EmailError(f"Attachment '{filename}' exceeds the 10 MiB limit.")
        total += len(data)
        if total > _MAX_TOTAL_ATTACHMENT_BYTES:
            raise _EmailError("Attachments exceed the 20 MiB total limit.")

        content_type = str(item.get("content_type") or "application/octet-stream")
        maintype, sep, subtype = content_type.partition("/")
        if not sep or not maintype or not subtype:
            raise _EmailError(f"Attachment '{filename}' has an invalid content_type.")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)


def _smtp_send_message(cfg: Dict[str, Any], msg: EmailMessage, *, bcc: str = "") -> None:
    host = cfg.get("smtp_host") or cfg.get("host")
    port = int(cfg.get("smtp_port") or cfg.get("port") or 587)
    username = cfg.get("username")
    password = cfg.get("password")
    use_tls = bool(cfg.get("use_tls_smtp", port == 587))
    use_ssl = bool(cfg.get("use_ssl_smtp", port == 465))
    if not host or not username or not password:
        raise _EmailError("SMTP not configured — set smtp_host, username, and password.")

    from_addr = parseaddr(str(msg.get("From") or ""))[1]
    if not from_addr:
        raise _EmailError("No valid From address configured.")
    recipient_headers = [str(msg.get("To") or ""), str(msg.get("Cc") or ""), bcc]
    rcpts = _unique_addresses(recipient_headers)
    if not rcpts:
        raise _EmailError("No valid recipients provided.")

    try:
        smtp_transport.send_message(
            message=msg,
            host=host,
            port=port,
            username=username,
            password=password,
            use_starttls=use_tls,
            use_ssl=use_ssl,
            timeout=20,
            from_addr=from_addr,
            to_addrs=rcpts,
        )
    except smtplib.SMTPAuthenticationError as e:
        raise _EmailError(f"SMTP auth failed: {e}")
    except smtplib.SMTPException as e:
        raise _EmailError(f"SMTP error: {e}")
    except OSError as e:
        raise _EmailError(f"Network error to {host}:{port}: {e}")


# ── IMAP helpers (blocking) ─────────────────────────────────────────────────

class _EmailError(RuntimeError):
    """Raised inside blocking helpers to surface friendly errors."""


def _imap_connect(cfg: Dict[str, Any]) -> imaplib.IMAP4:
    host = cfg.get("imap_host")
    port = int(cfg.get("imap_port") or 993)
    username = cfg.get("username")
    password = cfg.get("password")
    use_ssl = bool(cfg.get("use_ssl_imap", port == 993))

    if not host or not username or not password:
        raise _EmailError("IMAP not configured — set imap_host, username, and password.")

    try:
        client = smtp_transport.open_imap_client(
            host, port, use_ssl=use_ssl, timeout=20,
        )
    except OSError as e:
        raise _EmailError(f"Cannot reach IMAP {host}:{port}: {e}")

    try:
        client.login(username, password)
    except imaplib.IMAP4.error as e:
        raise _EmailError(f"IMAP auth failed: {e}")

    return client


def _select(client: imaplib.IMAP4, folder: str, readonly: bool = False) -> None:
    typ, _ = client.select(_mailbox_arg(folder), readonly=readonly)
    if typ != "OK":
        raise _EmailError(f"Cannot open folder '{folder}'.")


def _build_search_criteria(args: Dict[str, Any]) -> List[str]:
    crit: List[str] = []
    if args.get("unseen_only"):
        crit.append("UNSEEN")
    if args.get("from_address"):
        crit += ["FROM", f'"{args["from_address"]}"']
    if args.get("subject_contains"):
        crit += ["SUBJECT", f'"{args["subject_contains"]}"']
    if args.get("body_contains"):
        crit += ["BODY", f'"{args["body_contains"]}"']
    if args.get("since"):
        crit += ["SINCE", _imap_date(args["since"])]
    if args.get("before"):
        crit += ["BEFORE", _imap_date(args["before"])]
    if not crit:
        crit = ["ALL"]
    return crit


def _imap_date(s: str) -> str:
    # IMAP wants DD-MMM-YYYY (e.g. 01-Jan-2026)
    try:
        d = datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise _EmailError(f"Invalid date '{s}' — expected YYYY-MM-DD.")
    return d.strftime("%d-%b-%Y")


def _list_messages(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    folder = args.get("folder") or "INBOX"
    limit = min(int(args.get("max_results") or 20), 100)
    client = _imap_connect(cfg)
    try:
        _select(client, folder, readonly=True)
        crit = _build_search_criteria(args)
        typ, data = client.uid("SEARCH", None, *crit)
        if typ != "OK" or not data or not data[0]:
            return json.dumps({"folder": folder, "messages": []})
        uids = data[0].split()
        uids = list(reversed(uids))[:limit]   # newest first

        messages = []
        for uid in uids:
            typ, msg_data = client.uid(
                "FETCH", uid.decode(),
                "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])",
            )
            if typ != "OK" or not msg_data:
                continue
            flags: List[str] = []
            headers_bytes = b""
            for part in msg_data:
                if isinstance(part, tuple) and len(part) >= 2:
                    headers_bytes = part[1]
                    flag_match = re.search(rb"FLAGS \(([^)]*)\)", part[0] or b"")
                    if flag_match:
                        flags = flag_match.group(1).decode().split()
            if not headers_bytes:
                continue
            parsed = message_from_bytes(headers_bytes)
            messages.append({
                "uid": uid.decode(),
                "from": _decode(parsed.get("From", "")),
                "to": _decode(parsed.get("To", "")),
                "subject": _decode(parsed.get("Subject", "(no subject)")),
                "date": parsed.get("Date", ""),
                "unread": "\\Seen" not in flags,
                "flags": flags,
            })
        return json.dumps({"folder": folder, "messages": messages}, ensure_ascii=False)
    finally:
        _safe_logout(client)


def _get_message(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    folder = args.get("folder") or "INBOX"
    uid = args["uid"]
    fmt = args.get("format") or "full"
    client = _imap_connect(cfg)
    try:
        msg = _fetch_parsed_message(client, folder, uid)
        return json.dumps(_message_dict(msg, uid, folder, fmt), ensure_ascii=False)
    finally:
        _safe_logout(client)


def _list_attachments(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    folder = args.get("folder") or "INBOX"
    uid = args["uid"]
    client = _imap_connect(cfg)
    try:
        msg = _fetch_parsed_message(client, folder, uid)
        return json.dumps({
            "uid": uid,
            "folder": folder,
            "attachments": [meta for _, _, meta in _iter_attachments(msg)],
        }, ensure_ascii=False)
    finally:
        _safe_logout(client)


def _download_attachment(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    folder = args.get("folder") or "INBOX"
    uid = args["uid"]
    attachment_id = str(args["attachment_id"])
    client = _imap_connect(cfg)
    try:
        msg = _fetch_parsed_message(client, folder, uid)
        for part_id, data, meta in _iter_attachments(msg):
            if part_id != attachment_id:
                continue
            if len(data) > _MAX_ATTACHMENT_BYTES:
                raise _EmailError(
                    f"Attachment '{meta['filename']}' exceeds the 10 MiB download limit."
                )
            return json.dumps({
                "uid": uid,
                "folder": folder,
                **meta,
                "data_base64": base64.b64encode(data).decode("ascii"),
            }, ensure_ascii=False)
        raise _EmailError(
            f"Attachment id={attachment_id} not found on message uid={uid}."
        )
    finally:
        _safe_logout(client)


def _reply_to_message(
    cfg: Dict[str, Any], args: Dict[str, Any], reply_all: bool,
) -> str:
    folder = args.get("folder") or "INBOX"
    uid = args["uid"]
    client = _imap_connect(cfg)
    try:
        original = _fetch_parsed_message(client, folder, uid)
    finally:
        _safe_logout(client)

    recipients = _reply_recipients(original, cfg, reply_all=reply_all)
    message_id = str(original.get("Message-ID") or "").strip()
    references = _thread_references(original)
    subject = _reply_subject(_decode(original.get("Subject", "")))
    send_args = {
        "to": recipients["to"],
        "cc": recipients["cc"],
        "subject": subject,
        "body": args["body"],
        "html": args.get("html"),
        "attachments": args.get("attachments") or [],
    }
    msg = _compose_message(
        send_args,
        from_addr=cfg.get("from_address") or cfg.get("username"),
        thread_headers={
            "In-Reply-To": message_id,
            "References": references,
        },
    )
    _smtp_send_message(cfg, msg)
    return json.dumps({
        "success": True,
        "reply_all": reply_all,
        "original_uid": uid,
        "folder": folder,
        "to": recipients["to"],
        "cc": recipients["cc"],
        "subject": subject,
        "in_reply_to": message_id,
        "attachment_count": len(send_args["attachments"]),
    }, ensure_ascii=False)


def _list_threads(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    folder = args.get("folder") or "INBOX"
    max_results = min(max(int(args.get("max_results") or 20), 1), 100)
    scan_limit = min(
        max(int(args.get("max_messages_scan") or 200), max_results),
        _MAX_THREAD_SCAN,
    )
    client = _imap_connect(cfg)
    try:
        records = _scan_message_headers(
            client, folder, _build_search_criteria(args), scan_limit,
        )
    finally:
        _safe_logout(client)

    groups = _group_thread_records(records)
    summaries = []
    for group in groups:
        newest = max(group, key=_record_sort_key)
        identifiers = sorted(_record_identifiers(group[0]))
        summaries.append({
            "thread_id": identifiers[0] if identifiers else f"{folder}:{group[0]['uid']}",
            "anchor_uid": newest["uid"],
            "folder": folder,
            "message_count": len(group),
            "subject": newest["subject"],
            "from": newest["from"],
            "date": newest["date"],
            "unread": any(record.get("unread") for record in group),
        })
    summaries.sort(key=lambda item: _date_sort_key(item.get("date")), reverse=True)
    return json.dumps({
        "folder": folder,
        "threads": summaries[:max_results],
        "messages_scanned": len(records),
        "threading": "Message-ID/In-Reply-To/References",
    }, ensure_ascii=False)


def _get_thread(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    anchor_folder = args.get("folder") or "INBOX"
    anchor_uid = str(args["uid"])
    requested_folders = args.get("search_folders") or [anchor_folder]
    if not isinstance(requested_folders, list):
        raise _EmailError("search_folders must be an array.")
    folders = []
    for value in [anchor_folder, *requested_folders]:
        folder = str(value or "").strip()
        if folder and folder not in folders:
            folders.append(folder)
    scan_limit = min(
        max(int(args.get("max_messages_scan") or _MAX_THREAD_SCAN), 1),
        _MAX_THREAD_SCAN,
    )
    fmt = args.get("format") or "full"

    client = _imap_connect(cfg)
    try:
        anchor = _fetch_header_record(client, anchor_folder, anchor_uid)
        records = []
        for folder in folders:
            try:
                scanned = _scan_message_headers(client, folder, ["ALL"], scan_limit)
            except _EmailError:
                if folder == anchor_folder:
                    raise
                continue
            records.extend(scanned)
        anchor_key = (anchor_folder, anchor_uid)
        if not any((r["folder"], r["uid"]) == anchor_key for r in records):
            records.append(anchor)

        matching = []
        for group in _group_thread_records(records):
            if any((r["folder"], r["uid"]) == anchor_key for r in group):
                matching = group
                break
        if not matching:
            matching = [anchor]

        messages = []
        for record in sorted(matching, key=_record_sort_key):
            parsed = _fetch_parsed_message(client, record["folder"], record["uid"])
            messages.append(_message_dict(
                parsed, record["uid"], record["folder"], fmt,
            ))
    finally:
        _safe_logout(client)

    return json.dumps({
        "anchor_uid": anchor_uid,
        "anchor_folder": anchor_folder,
        "folders_scanned": folders,
        "message_count": len(messages),
        "messages": messages,
        "threading": "Message-ID/In-Reply-To/References",
    }, ensure_ascii=False)


def _list_drafts(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    client = _imap_connect(cfg)
    try:
        folder = _resolve_special_folder(client, cfg, "drafts", args.get("folder"))
        records = _scan_message_headers(
            client, folder, ["ALL"], min(max(int(args.get("max_results") or 20), 1), 100),
        )
        return json.dumps({
            "folder": folder,
            "drafts": [{
                "uid": r["uid"],
                "to": r["to"],
                "cc": r["cc"],
                "subject": r["subject"],
                "date": r["date"],
            } for r in records],
        }, ensure_ascii=False)
    finally:
        _safe_logout(client)


def _get_draft(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    client = _imap_connect(cfg)
    try:
        folder = _resolve_special_folder(client, cfg, "drafts", args.get("folder"))
        msg = _fetch_parsed_message(client, folder, args["uid"])
        return json.dumps(
            _message_dict(msg, str(args["uid"]), folder, "full"),
            ensure_ascii=False,
        )
    finally:
        _safe_logout(client)


def _create_draft(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    draft_args = dict(args)
    thread_headers: Dict[str, str] = {}
    reply_to_uid = args.get("reply_to_uid")
    if reply_to_uid:
        reply_folder = args.get("reply_to_folder") or "INBOX"
        reader = _imap_connect(cfg)
        try:
            original = _fetch_parsed_message(reader, reply_folder, str(reply_to_uid))
        finally:
            _safe_logout(reader)
        recipients = _reply_recipients(
            original, cfg, reply_all=bool(args.get("reply_all")),
        )
        draft_args["to"] = recipients["to"]
        draft_args["cc"] = recipients["cc"]
        draft_args["subject"] = _reply_subject(_decode(original.get("Subject", "")))
        thread_headers = {
            "In-Reply-To": str(original.get("Message-ID") or "").strip(),
            "References": _thread_references(original),
        }

    msg = _compose_message(
        draft_args,
        from_addr=cfg.get("from_address") or cfg.get("username"),
        thread_headers=thread_headers,
        include_bcc=True,
    )
    client = _imap_connect(cfg)
    try:
        folder = _resolve_special_folder(client, cfg, "drafts", args.get("folder"))
        uid = _append_draft(client, folder, msg)
    finally:
        _safe_logout(client)
    return json.dumps({
        "success": True,
        "draft_uid": uid,
        "folder": folder,
        "to": draft_args["to"],
        "cc": draft_args.get("cc") or "",
        "subject": draft_args["subject"],
    }, ensure_ascii=False)


def _update_draft(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    client = _imap_connect(cfg)
    try:
        folder = _resolve_special_folder(client, cfg, "drafts", args.get("folder"))
        old = _fetch_parsed_message(client, folder, str(args["uid"]))
        msg = _compose_message(
            args,
            from_addr=cfg.get("from_address") or cfg.get("username"),
            thread_headers={
                "In-Reply-To": str(old.get("In-Reply-To") or ""),
                "References": str(old.get("References") or ""),
            },
            include_bcc=True,
        )
        new_uid = _append_draft(client, folder, msg)
        _delete_uid(client, folder, str(args["uid"]))
    finally:
        _safe_logout(client)
    return json.dumps({
        "success": True,
        "replaced_uid": str(args["uid"]),
        "draft_uid": new_uid,
        "folder": folder,
    })


def _send_draft(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    client = _imap_connect(cfg)
    try:
        folder = _resolve_special_folder(client, cfg, "drafts", args.get("folder"))
        uid = str(args["uid"])
        msg = _fetch_parsed_message(client, folder, uid)
        bcc = str(msg.get("Bcc") or "")
        _smtp_send_message(cfg, msg, bcc=bcc)
        _delete_uid(client, folder, uid)
    finally:
        _safe_logout(client)
    return json.dumps({
        "success": True,
        "sent_draft_uid": uid,
        "folder": folder,
        "to": str(msg.get("To") or ""),
        "subject": _decode(msg.get("Subject", "")),
    }, ensure_ascii=False)


def _delete_draft(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    client = _imap_connect(cfg)
    try:
        folder = _resolve_special_folder(client, cfg, "drafts", args.get("folder"))
        _delete_uid(client, folder, str(args["uid"]))
    finally:
        _safe_logout(client)
    return json.dumps({
        "success": True,
        "draft_uid": str(args["uid"]),
        "folder": folder,
        "deleted": True,
    })


def _fetch_parsed_message(client: imaplib.IMAP4, folder: str, uid: str):
    _select(client, folder, readonly=True)
    typ, data = client.uid("FETCH", uid, "(RFC822)")
    if typ != "OK" or not data or not data[0]:
        raise _EmailError(f"Message uid={uid} not found in '{folder}'.")
    raw = data[0][1] if isinstance(data[0], tuple) else b""
    if not raw:
        raise _EmailError(f"Message uid={uid} in '{folder}' has no content.")
    return message_from_bytes(raw)


def _message_dict(msg, uid: str, folder: str, fmt: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "uid": uid,
        "folder": folder,
        "from": _decode(msg.get("From", "")),
        "to": _decode(msg.get("To", "")),
        "cc": _decode(msg.get("Cc", "")),
        "bcc": _decode(msg.get("Bcc", "")),
        "reply_to": _decode(msg.get("Reply-To", "")),
        "subject": _decode(msg.get("Subject", "(no subject)")),
        "date": msg.get("Date", ""),
        "message_id": msg.get("Message-ID", ""),
        "in_reply_to": msg.get("In-Reply-To", ""),
        "references": msg.get("References", ""),
        "attachments": [meta for _, _, meta in _iter_attachments(msg)],
    }
    if fmt == "headers":
        return out
    body, html = _extract_body(msg)
    out["body"] = body
    if fmt == "full" and html:
        out["html"] = html
    return out


def _iter_attachments(msg):
    for index, part in enumerate(msg.walk()):
        if part.is_multipart():
            continue
        disposition = (part.get_content_disposition() or "").lower()
        filename = _decode(part.get_filename() or "")
        if disposition != "attachment" and not filename:
            continue
        data = part.get_payload(decode=True) or b""
        part_id = str(index)
        meta = {
            "attachment_id": part_id,
            "filename": filename or f"attachment-{part_id}",
            "content_type": part.get_content_type(),
            "size": len(data),
            "content_id": str(part.get("Content-ID") or "").strip("<>"),
            "disposition": disposition or "inline",
        }
        yield part_id, data, meta


def _reply_recipients(msg, cfg: Dict[str, Any], *, reply_all: bool) -> Dict[str, str]:
    reply_source = str(msg.get("Reply-To") or msg.get("From") or "")
    primary = _address_pairs([reply_source])
    if not primary:
        raise _EmailError("Original message has no valid reply address.")

    self_addresses = {
        address.lower()
        for _, address in _address_pairs([
            str(cfg.get("username") or ""),
            str(cfg.get("from_address") or ""),
        ])
    }
    to_pairs = []
    cc_pairs = []
    seen = set(self_addresses)

    def add(target: List[tuple[str, str]], pairs: List[tuple[str, str]]) -> None:
        for name, address in pairs:
            lowered = address.lower()
            if not lowered or lowered in seen:
                continue
            seen.add(lowered)
            target.append((name, address))

    add(to_pairs, primary)
    if reply_all:
        add(to_pairs, _address_pairs([str(msg.get("To") or "")]))
        add(cc_pairs, _address_pairs([str(msg.get("Cc") or "")]))

    return {
        "to": _format_address_pairs(to_pairs),
        "cc": _format_address_pairs(cc_pairs),
    }


def _thread_references(msg) -> str:
    values = _extract_message_ids(str(msg.get("References") or ""))
    parent = _extract_message_ids(str(msg.get("In-Reply-To") or ""))
    current = _extract_message_ids(str(msg.get("Message-ID") or ""))
    ordered = []
    for value in [*values, *parent, *current]:
        if value not in ordered:
            ordered.append(value)
    return " ".join(ordered)


def _reply_subject(subject: str) -> str:
    clean = subject.strip() or "(no subject)"
    return clean if re.match(r"^\s*re\s*:", clean, re.IGNORECASE) else f"Re: {clean}"


def _extract_message_ids(value: str) -> List[str]:
    return re.findall(r"<[^<>\s]+>", value or "")


def _address_pairs(values: List[str]) -> List[tuple[str, str]]:
    nonempty = [str(value).strip() for value in values if str(value or "").strip()]
    return [
        (_decode(name), address.strip())
        for name, address in getaddresses(nonempty)
        if address and "@" in address
    ]


def _format_address_pairs(pairs: List[tuple[str, str]]) -> str:
    return ", ".join(formataddr((name, address)) if name else address for name, address in pairs)


def _unique_addresses(values: List[str]) -> List[str]:
    seen = set()
    result = []
    for _, address in _address_pairs(values):
        lowered = address.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        result.append(address)
    return result


def _scan_message_headers(
    client: imaplib.IMAP4,
    folder: str,
    criteria: List[str],
    limit: int,
) -> List[Dict[str, Any]]:
    _select(client, folder, readonly=True)
    typ, data = client.uid("SEARCH", None, *criteria)
    if typ != "OK" or not data or not data[0]:
        return []
    uids = list(reversed(data[0].split()))[:limit]
    return [
        _fetch_header_record(client, folder, uid.decode())
        for uid in uids
    ]


def _fetch_header_record(
    client: imaplib.IMAP4, folder: str, uid: str,
) -> Dict[str, Any]:
    _select(client, folder, readonly=True)
    typ, data = client.uid(
        "FETCH",
        uid,
        "(FLAGS BODY.PEEK[HEADER.FIELDS (FROM TO CC SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES)])",
    )
    if typ != "OK" or not data:
        raise _EmailError(f"Message uid={uid} not found in '{folder}'.")
    flags: List[str] = []
    raw = b""
    for part in data:
        if isinstance(part, tuple) and len(part) >= 2:
            raw = part[1]
            match = re.search(rb"FLAGS \(([^)]*)\)", part[0] or b"")
            if match:
                flags = match.group(1).decode(errors="replace").split()
    if not raw:
        raise _EmailError(f"Message uid={uid} in '{folder}' has no headers.")
    msg = message_from_bytes(raw)
    return {
        "uid": uid,
        "folder": folder,
        "from": _decode(msg.get("From", "")),
        "to": _decode(msg.get("To", "")),
        "cc": _decode(msg.get("Cc", "")),
        "subject": _decode(msg.get("Subject", "(no subject)")),
        "date": str(msg.get("Date") or ""),
        "message_id": str(msg.get("Message-ID") or ""),
        "in_reply_to": str(msg.get("In-Reply-To") or ""),
        "references": str(msg.get("References") or ""),
        "unread": r"\Seen" not in flags,
        "flags": flags,
    }


def _record_identifiers(record: Dict[str, Any]) -> set[str]:
    return set(_extract_message_ids(" ".join([
        str(record.get("message_id") or ""),
        str(record.get("in_reply_to") or ""),
        str(record.get("references") or ""),
    ])))


def _group_thread_records(records: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    if not records:
        return []
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        lroot, rroot = find(left), find(right)
        if lroot != rroot:
            parent[rroot] = lroot

    owner: Dict[str, int] = {}
    for index, record in enumerate(records):
        for identifier in _record_identifiers(record):
            if identifier in owner:
                union(index, owner[identifier])
            else:
                owner[identifier] = index

    grouped: Dict[int, List[Dict[str, Any]]] = defaultdict(list)
    for index, record in enumerate(records):
        grouped[find(index)].append(record)
    return list(grouped.values())


def _date_sort_key(value: Any) -> float:
    try:
        parsed = parsedate_to_datetime(str(value or ""))
        return parsed.timestamp()
    except Exception:
        return 0.0


def _record_sort_key(record: Dict[str, Any]) -> tuple[float, int]:
    try:
        uid_number = int(record.get("uid") or 0)
    except (TypeError, ValueError):
        uid_number = 0
    return _date_sort_key(record.get("date")), uid_number


def _folder_rows(client: imaplib.IMAP4) -> List[Dict[str, Any]]:
    typ, data = client.list()
    if typ != "OK" or not data:
        return []
    rows = []
    for line in data:
        if not line:
            continue
        decoded = line.decode(errors="replace")
        match = re.match(r'^\(([^)]*)\)\s+"([^"]*)"\s+(.*)$', decoded)
        if not match:
            continue
        raw_name = match.group(3).strip()
        name = raw_name[1:-1].replace(r'\"', '"') if raw_name.startswith('"') and raw_name.endswith('"') else raw_name
        rows.append({"name": name, "flags": match.group(1).split()})
    return rows


def _resolve_special_folder(
    client: imaplib.IMAP4,
    cfg: Dict[str, Any],
    kind: str,
    override: Any = None,
) -> str:
    configured = str(override or cfg.get(f"{kind}_folder") or "").strip()
    rows = _folder_rows(client)
    names = {row["name"] for row in rows}
    if configured:
        if configured not in names:
            raise _EmailError(f"Configured {kind} folder '{configured}' was not found.")
        return configured

    flag = rf"\{kind.capitalize()}".lower()
    for row in rows:
        if any(value.lower() == flag for value in row["flags"]):
            return row["name"]
    common = {
        "drafts": ["Drafts", "INBOX.Drafts", "[Gmail]/Drafts"],
        "sent": ["Sent", "Sent Items", "Sent Messages", "[Gmail]/Sent Mail"],
    }.get(kind, [kind.capitalize()])
    for candidate in common:
        if candidate in names:
            return candidate
    raise _EmailError(
        f"No {kind} folder found. Pass folder explicitly or set {kind}_folder."
    )


def _append_draft(client: imaplib.IMAP4, folder: str, msg: EmailMessage) -> str:
    if not msg.get("Date"):
        msg["Date"] = formatdate(localtime=True)
    if not msg.get("Message-ID"):
        msg["Message-ID"] = make_msgid()
    typ, data = client.append(
        _mailbox_arg(folder), r"(\Draft \Seen)", None, msg.as_bytes(),
    )
    if typ != "OK":
        raise _EmailError(f"Could not append draft to '{folder}'.")

    response = client.response("APPENDUID")
    if response and response[1]:
        values = re.findall(rb"\d+", b" ".join(response[1]))
        if values:
            return values[-1].decode()
    _select(client, folder, readonly=True)
    search_type, search_data = client.uid("SEARCH", None, "ALL")
    if search_type == "OK" and search_data and search_data[0]:
        return search_data[0].split()[-1].decode()
    return "unknown"


def _delete_uid(client: imaplib.IMAP4, folder: str, uid: str) -> None:
    _select(client, folder)
    typ, _ = client.uid("STORE", uid, "+FLAGS", r"\Deleted")
    if typ != "OK":
        raise _EmailError(f"Could not delete uid={uid} from '{folder}'.")
    client.expunge()


def _mailbox_arg(folder: str) -> str:
    value = str(folder or "")
    if value.startswith('"') and value.endswith('"'):
        return value
    if re.search(r'[\s"\\]', value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return value


def _flag(
    cfg: Dict[str, Any], args: Dict[str, Any], op: str, flag: str,
) -> str:
    folder = args.get("folder") or "INBOX"
    uid = args["uid"]
    client = _imap_connect(cfg)
    try:
        _select(client, folder)
        typ, _ = client.uid("STORE", uid, op, flag)
        if typ != "OK":
            raise _EmailError(f"Could not update flag for uid={uid}.")
        return json.dumps({"success": True, "uid": uid, "op": op, "flag": flag})
    finally:
        _safe_logout(client)


def _move_message(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    from_folder = args.get("from_folder") or "INBOX"
    to_folder = args["to_folder"]
    uid = args["uid"]
    client = _imap_connect(cfg)
    try:
        _select(client, from_folder)
        # Try IMAP MOVE (RFC 6851) — most modern servers support it.
        quoted_to = f'"{to_folder}"'
        typ, _ = client.uid("MOVE", uid, quoted_to)
        if typ != "OK":
            # Fallback: COPY + STORE \Deleted + EXPUNGE
            typ2, _ = client.uid("COPY", uid, quoted_to)
            if typ2 != "OK":
                raise _EmailError(f"COPY failed to '{to_folder}'.")
            client.uid("STORE", uid, "+FLAGS", r"\Deleted")
            client.expunge()
        return json.dumps({"success": True, "uid": uid, "moved_to": to_folder})
    finally:
        _safe_logout(client)


def _delete_message(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    folder = args.get("folder") or "INBOX"
    uid = args["uid"]
    client = _imap_connect(cfg)
    try:
        _select(client, folder)
        typ, _ = client.uid("STORE", uid, "+FLAGS", r"\Deleted")
        if typ != "OK":
            raise _EmailError(f"Could not flag uid={uid} for deletion.")
        client.expunge()
        return json.dumps({"success": True, "uid": uid, "deleted": True})
    finally:
        _safe_logout(client)


def _list_folders(cfg: Dict[str, Any]) -> str:
    client = _imap_connect(cfg)
    try:
        typ, data = client.list()
        if typ != "OK" or not data:
            return json.dumps({"folders": []})
        folders = []
        for line in data:
            if not line:
                continue
            # Format: b'(\\HasNoChildren) "/" "INBOX"'
            m = re.match(rb'\(([^)]*)\) "([^"]*)" "?([^"]*)"?$', line)
            if m:
                flags = m.group(1).decode().split()
                name = m.group(3).decode()
                folders.append({"name": name, "flags": flags})
        return json.dumps({"folders": folders}, ensure_ascii=False)
    finally:
        _safe_logout(client)


# ── Utilities ───────────────────────────────────────────────────────────────

def _decode(h: str) -> str:
    if not h:
        return ""
    try:
        return str(make_header(decode_header(h)))
    except Exception:
        return h


def _extract_body(msg) -> tuple[str, str]:
    """Return (plain_text, html) — prefer text/plain, fall back to HTML."""
    plain = ""
    html = ""
    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp or part.get_filename():
                continue
            if ctype == "text/plain" and not plain:
                plain = _decode_payload(part)
            elif ctype == "text/html" and not html:
                html = _decode_payload(part)
    else:
        ctype = msg.get_content_type()
        if ctype == "text/html":
            html = _decode_payload(msg)
        else:
            plain = _decode_payload(msg)
    return plain, html


def _decode_payload(part) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _safe_logout(client: imaplib.IMAP4) -> None:
    try:
        client.logout()
    except Exception:
        pass


def _split(s: str) -> List[str]:
    return [a.strip() for a in (s or "").split(",") if a.strip()]


def _truncate(s: str) -> str:
    return s if len(s) <= _MAX_CHARS else s[:_MAX_CHARS] + "\n… (truncated)"


def _error(msg: str) -> Dict[str, Any]:
    return {"content": [{"type": "text", "text": msg}], "isError": True}


# Retain parseaddr import for future use (e.g. display-name extraction)
_ = parseaddr
