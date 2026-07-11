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
import imaplib
import json
import logging
import re
import smtplib
from datetime import datetime
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import parseaddr
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_MAX_CHARS = 12_000


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

    return {"content": [{"type": "text", "text": _truncate(text)}], "isError": False}


# ── SMTP send (blocking helper) ─────────────────────────────────────────────

def _send_email(cfg: Dict[str, Any], args: Dict[str, Any]) -> str:
    host = cfg.get("smtp_host") or cfg.get("host")
    port = int(cfg.get("smtp_port") or cfg.get("port") or 587)
    username = cfg.get("username")
    password = cfg.get("password")
    default_from = cfg.get("from_address") or username
    use_tls = bool(cfg.get("use_tls_smtp", port == 587))
    use_ssl = bool(cfg.get("use_ssl_smtp", port == 465))

    if not host or not username or not password:
        raise _EmailError("SMTP not configured — set smtp_host, username, and password.")

    from_addr = args.get("from_address") or default_from
    if not from_addr:
        raise _EmailError("No From address configured.")

    msg = EmailMessage()
    msg["From"] = from_addr
    msg["To"] = args["to"]
    msg["Subject"] = args["subject"]
    if args.get("cc"):
        msg["Cc"] = args["cc"]
    if args.get("reply_to"):
        msg["Reply-To"] = args["reply_to"]
    msg.set_content(args["body"])
    if args.get("html"):
        msg.add_alternative(args["html"], subtype="html")

    rcpts = _split(args["to"]) + _split(args.get("cc") or "") + _split(args.get("bcc") or "")

    try:
        if use_ssl:
            with smtplib.SMTP_SSL(host, port, timeout=20) as s:
                s.login(username, password)
                s.send_message(msg, from_addr=from_addr, to_addrs=rcpts)
        else:
            with smtplib.SMTP(host, port, timeout=20) as s:
                s.ehlo()
                if use_tls:
                    s.starttls()
                    s.ehlo()
                s.login(username, password)
                s.send_message(msg, from_addr=from_addr, to_addrs=rcpts)
    except smtplib.SMTPAuthenticationError as e:
        raise _EmailError(f"SMTP auth failed: {e}")
    except smtplib.SMTPException as e:
        raise _EmailError(f"SMTP error: {e}")
    except OSError as e:
        raise _EmailError(f"Network error to {host}:{port}: {e}")

    return json.dumps({
        "success": True, "to": args["to"],
        "from": from_addr, "subject": args["subject"],
    })


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
        client: imaplib.IMAP4
        if use_ssl:
            client = imaplib.IMAP4_SSL(host, port, timeout=20)
        else:
            client = imaplib.IMAP4(host, port, timeout=20)
    except OSError as e:
        raise _EmailError(f"Cannot reach IMAP {host}:{port}: {e}")

    try:
        client.login(username, password)
    except imaplib.IMAP4.error as e:
        raise _EmailError(f"IMAP auth failed: {e}")

    return client


def _select(client: imaplib.IMAP4, folder: str, readonly: bool = False) -> None:
    # IMAP folder names with spaces must be quoted
    quoted = f'"{folder}"' if " " in folder and not folder.startswith('"') else folder
    typ, _ = client.select(quoted, readonly=readonly)
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
        _select(client, folder, readonly=True)
        typ, data = client.uid("FETCH", uid, "(RFC822)")
        if typ != "OK" or not data or not data[0]:
            raise _EmailError(f"Message uid={uid} not found in '{folder}'.")
        raw = data[0][1] if isinstance(data[0], tuple) else b""
        msg = message_from_bytes(raw)

        out: Dict[str, Any] = {
            "uid": uid,
            "folder": folder,
            "from": _decode(msg.get("From", "")),
            "to": _decode(msg.get("To", "")),
            "cc": _decode(msg.get("Cc", "")),
            "subject": _decode(msg.get("Subject", "(no subject)")),
            "date": msg.get("Date", ""),
            "message_id": msg.get("Message-ID", ""),
        }
        if fmt == "headers":
            return json.dumps(out, ensure_ascii=False)
        if fmt == "text" or fmt == "full":
            body, html = _extract_body(msg)
            out["body"] = body
            if fmt == "full" and html:
                out["html"] = html
        return json.dumps(out, ensure_ascii=False)
    finally:
        _safe_logout(client)


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
            if "attachment" in disp:
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
