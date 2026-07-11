"""Email service — send transactional emails via SMTP with HTML templates.

Templates are ported from the original Manor AI Java backend
(manor-system/src/main/resources/messageTemplate/) and use the same
design system: Plus Jakarta Sans, teal #0D9488 accent, dark #0F172A.

Configuration (env vars):
  SMTP_HOST        — SMTP server hostname (default: smtp.gmail.com)
  SMTP_PORT        — SMTP port (default: 587)
  SMTP_USER        — SMTP username (e.g. support@manorai.xyz)
  SMTP_PASSWORD    — SMTP password / Gmail app password
  SMTP_FROM_EMAIL  — Sender email (default: support@manorai.xyz)
  SMTP_FROM_NAME   — Sender name (default: Manor AI)
  SMTP_STARTTLS    — Use STARTTLS for port 587 (default: true)
  SMTP_SSL         — Use implicit SSL for port 465 (default: false)
  EMAIL_ENABLED    — Master switch (default: false)
  APP_URL          — Base URL for links in emails (default: http://localhost:3010)
"""
import html
import logging
import os
import re
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from html import escape
from pathlib import Path
from typing import Optional

try:
    import aiosmtplib
except ImportError:
    aiosmtplib = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates" / "email"
_template_cache: dict[str, str] = {}


def _get_smtp_config() -> dict:
    return {
        "hostname": os.getenv("SMTP_HOST", "smtp.gmail.com"),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "username": os.getenv("SMTP_USER", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "use_starttls": os.getenv("SMTP_STARTTLS", "true").lower() == "true",
        "use_ssl": os.getenv("SMTP_SSL", "false").lower() == "true",
        "from_email": os.getenv("SMTP_FROM_EMAIL", "support@manorai.xyz"),
        "from_name": os.getenv("SMTP_FROM_NAME", "Manor AI"),
        "enabled": os.getenv("EMAIL_ENABLED", "false").lower() == "true",
    }


def _load_template(name: str) -> str:
    """Load an HTML template file from disk (cached after first read)."""
    if name not in _template_cache:
        path = _TEMPLATE_DIR / f"{name}.html"
        _template_cache[name] = path.read_text(encoding="utf-8")
    return _template_cache[name]


def _render(template_name: str, **kwargs) -> str:
    """Load a standalone template and replace {{ var }} placeholders."""
    html = _load_template(template_name)

    # Inject common variables
    kwargs.setdefault("app_url", os.getenv("APP_URL", "http://localhost:3010").rstrip("/"))
    kwargs.setdefault("year", str(datetime.now(timezone.utc).year))

    for key, value in kwargs.items():
        html = html.replace("{{ " + key + " }}", str(value))
    return html


# Test/throwaway domains — never send real emails to these
_BLOCKED_DOMAINS = {"example.com", "example.org", "example.net", "test.com", "localhost"}


async def send_email(to: str, subject: str, html_body: str, text_body: str = None) -> bool:
    """Send an email. Returns True on success, False on failure (never raises)."""
    config = _get_smtp_config()
    if not config["enabled"]:
        logger.info("Email disabled — would send to %s: %s", to, subject)
        return True  # pretend success when disabled

    # Block sending to test/throwaway domains
    domain = to.rsplit("@", 1)[-1].lower() if "@" in to else ""
    if domain in _BLOCKED_DOMAINS and not os.getenv("PYTEST_CURRENT_TEST"):
        logger.info("Blocked email to test domain %s: %s", to, subject)
        return True  # pretend success

    if aiosmtplib is None:
        logger.warning("aiosmtplib not installed — cannot send email to %s", to)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config['from_name']} <{config['from_email']}>"
    msg["To"] = to

    if text_body:
        msg.attach(MIMEText(text_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        await aiosmtplib.send(
            msg,
            hostname=config["hostname"],
            port=config["port"],
            username=config["username"] or None,
            password=config["password"] or None,
            start_tls=config["use_starttls"],
            use_tls=config["use_ssl"],
        )
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as e:
        logger.error("Failed to send email to %s: %s", to, e)
        return False


# ── Template-based senders ──

async def send_verification_email(to: str, code: str) -> bool:
    """Send a 6-digit email verification code (cloud mode only)."""
    subject = "Verify your Manor AI account"
    html = _render("verification", code=code)
    return await send_email(to, subject, html)


async def send_welcome_email(to: str, display_name: str) -> bool:
    """Send a welcome email after successful registration/verification."""
    subject = "Welcome to Manor AI!"
    html = _render("welcome", display_name=display_name)
    return await send_email(to, subject, html)


async def send_waitlist_confirmation_email(to: str, display_name: str) -> bool:
    """Send a confirmation to the submitter right after they join the waitlist."""
    subject = "You're on the Manor AI waitlist"
    body = _render("waitlist_confirmation", display_name=display_name or "there")
    return await send_email(to, subject, body)


# ── Announcement email ───────────────────────────────────────────────

_SEVERITY_STYLES = {
    "info":     {"label": "Announcement", "color": "#0D9488", "bg": "#F0FDFA"},
    "warning":  {"label": "Heads up",      "color": "#B45309", "bg": "#FEF3C7"},
    "critical": {"label": "Action required","color": "#B91C1C", "bg": "#FEE2E2"},
}


def _render_announcement_body_html(body_md: str) -> str:
    """Render the markdown subset admins use in announcements to HTML.

    Intentionally tiny — no external dep, no raw-HTML passthrough. The
    admin is trusted; this layer exists for output quality and to make
    sure stray ``<`` / ``>`` from a paste don't break the email.

    Supported: paragraphs, blank-line breaks, ``# / ## / ###`` headings,
    ``- `` / ``* `` bullet lists, ``1.`` numbered lists, ``**bold**``,
    ``*italic*``, ``` `code` ``` , ``[label](url)``, blockquotes,
    ``![alt](https://url)`` images (https only), pipe tables, ``---``
    horizontal rules.
    """
    # Escape first so any literal HTML in the markdown becomes text.
    safe = html.escape(body_md or "").replace("\r\n", "\n").replace("\r", "\n")

    lines = safe.split("\n")
    out: list[str] = []
    para: list[str] = []
    in_ul = False
    in_ol = False
    in_quote = False
    table_rows: list[str] = []

    def flush_para() -> None:
        if para:
            out.append("<p>" + " ".join(para).strip() + "</p>")
            para.clear()

    def close_lists() -> None:
        nonlocal in_ul, in_ol, in_quote
        if in_ul:
            out.append("</ul>"); in_ul = False
        if in_ol:
            out.append("</ol>"); in_ol = False
        if in_quote:
            out.append("</blockquote>"); in_quote = False

    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            flush_para(); close_lists()
            if table_rows:
                out.append(_render_md_table(table_rows))
                table_rows.clear()
            continue

        # Horizontal rule — check before unordered list so --- isn't parsed as a list item.
        if re.match(r"^\s*-{3,}\s*$", line):
            flush_para(); close_lists()
            out.append('<hr style="border:none;border-top:1px solid #E2E8F0;margin:20px 0;">')
            continue

        # Table row (pipe syntax) — accumulate until a non-table line.
        if re.match(r"^\s*\|.*\|\s*$", line):
            flush_para(); close_lists()
            table_rows.append(line)
            continue
        if table_rows:
            out.append(_render_md_table(table_rows))
            table_rows.clear()

        # Headings
        m = re.match(r"^(#{1,3})\s+(.*)$", line)
        if m:
            flush_para(); close_lists()
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline_md(m.group(2))}</h{level}>")
            continue

        # Blockquote
        if line.lstrip().startswith("&gt; "):
            flush_para()
            if in_ul: out.append("</ul>"); in_ul = False
            if in_ol: out.append("</ol>"); in_ol = False
            if not in_quote:
                out.append("<blockquote>"); in_quote = True
            out.append(f"<p>{_inline_md(line.lstrip()[len('&gt; '):])}</p>")
            continue

        # Unordered list
        m = re.match(r"^\s*[-*]\s+(.*)$", line)
        if m:
            flush_para()
            if in_ol: out.append("</ol>"); in_ol = False
            if in_quote: out.append("</blockquote>"); in_quote = False
            if not in_ul:
                out.append("<ul>"); in_ul = True
            out.append(f"<li>{_inline_md(m.group(1))}</li>")
            continue

        # Ordered list
        m = re.match(r"^\s*\d+\.\s+(.*)$", line)
        if m:
            flush_para()
            if in_ul: out.append("</ul>"); in_ul = False
            if in_quote: out.append("</blockquote>"); in_quote = False
            if not in_ol:
                out.append("<ol>"); in_ol = True
            out.append(f"<li>{_inline_md(m.group(1))}</li>")
            continue

        # Plain paragraph line — accumulate, flush on blank line.
        if in_ul or in_ol or in_quote:
            close_lists()
        para.append(_inline_md(line))

    flush_para(); close_lists()
    if table_rows:
        out.append(_render_md_table(table_rows))
    return "\n".join(out) if out else "<p></p>"


def _inline_md(s: str) -> str:
    """Inline markdown for one line of already-escaped text."""
    # Code (do first so * and _ inside aren't interpreted)
    s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
    # Images ![alt](https://url) — https only, block display for email.
    # Must come before link rule so ![] prefix doesn't pass through as broken link.
    s = re.sub(
        r"!\[([^\]]*)\]\((https://[^\s)]+)\)",
        r'<img src="\2" alt="\1" style="max-width:100%;height:auto;'
        r'border-radius:8px;display:block;margin:12px 0;">',
        s,
    )
    # Links [label](url) — url is escaped already; quotes are safe.
    s = re.sub(
        r"\[([^\]]+)\]\((https?://[^\s)]+)\)",
        r'<a href="\2">\1</a>',
        s,
    )
    # Bold (must come before italic so **x** doesn't match as *x* *x*)
    s = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", s)
    # Italic
    s = re.sub(r"(?<![*\w])\*([^*\n]+)\*(?!\w)", r"<em>\1</em>", s)
    return s


_TABLE_SEP_CELL = re.compile(r"^:?-{3,}:?$")


def _render_md_table(rows: list[str]) -> str:
    """Render accumulated pipe-table lines (already escaped) to HTML.

    Row 2 must be a separator (---) for row 1 to become a header;
    otherwise every row is a plain body row.
    """
    def cells(row: str) -> list[str]:
        return [c.strip() for c in row.strip().strip("|").split("|")]

    parsed = [cells(r) for r in rows]
    header: list[str] | None = None
    body = parsed
    if len(parsed) >= 2 and all(_TABLE_SEP_CELL.match(c) for c in parsed[1] if c):
        header = parsed[0]
        body = parsed[2:]

    parts = ["<table>"]
    if header:
        parts.append(
            "<tr>" + "".join(f"<th>{_inline_md(c)}</th>" for c in header) + "</tr>"
        )
    for row in body:
        parts.append(
            "<tr>" + "".join(f"<td>{_inline_md(c)}</td>" for c in row) + "</tr>"
        )
    parts.append("</table>")
    return "".join(parts)


def render_announcement_email_html(
    *,
    title: str,
    body_md: str,
    severity: str,
    display_name: Optional[str] = None,
    cta_url: Optional[str] = None,
    cta_label: Optional[str] = None,
) -> str:
    """Render the full announcement email HTML (for preview + send)."""
    style = _SEVERITY_STYLES.get(severity, _SEVERITY_STYLES["info"])
    body_html = _render_announcement_body_html(body_md)
    cta_block = ""
    if cta_url:
        label = (cta_label or "View details").strip() or "View details"
        cta_block = (
            f'<section class="cta-section"><div class="section-content">'
            f'<a href="{html.escape(cta_url, quote=True)}" class="btn-primary" '
            f'style="color: #ffffff !important;">{html.escape(label)}</a>'
            f"</div></section>"
        )
    return _render(
        "announcement",
        title=html.escape(title or ""),
        body_html=body_html,
        severity_label=style["label"],
        severity_color=style["color"],
        severity_bg=style["bg"],
        display_name=html.escape(display_name or "there"),
        cta_block=cta_block,
    )


async def send_announcement_email(
    to: str,
    *,
    title: str,
    body_md: str,
    severity: str,
    display_name: Optional[str] = None,
    cta_url: Optional[str] = None,
    cta_label: Optional[str] = None,
) -> bool:
    """Send a platform announcement to one recipient."""
    subject = f"[Manor AI] {title}".strip()
    html_body = render_announcement_email_html(
        title=title, body_md=body_md, severity=severity,
        display_name=display_name, cta_url=cta_url, cta_label=cta_label,
    )
    return await send_email(to, subject, html_body)


async def send_password_reset_email(to: str, reset_token: str, reset_url: str = None) -> bool:
    """Send a password reset link."""
    app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
    url = reset_url or f"{app_url}/reset-password?token={reset_token}"
    subject = "Reset your Manor AI password"
    html = _render("password_reset", reset_url=url)
    return await send_email(to, subject, html)


async def send_invite_email(to: str, entity_name: str, inviter_name: str, temp_password: str) -> bool:
    """Send an invitation email to a new user."""
    app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
    subject = f"You've been invited to {entity_name} on Manor AI"
    html = _render(
        "invite",
        inviter_name=inviter_name,
        entity_name=entity_name,
        email=to,
        temp_password=temp_password,
        login_url=f"{app_url}/login",
    )
    return await send_email(to, subject, html)


async def send_staff_invite_email(
    to: str, *, entity_name: str, inviter_name: str, invite_url: str,
) -> bool:
    """Send a team invitation email with a single-use accept link."""
    subject_entity = entity_name or "your team"
    safe_entity = escape(entity_name or "your team")
    safe_inviter = escape(inviter_name or "A Manor AI admin")
    safe_invite_url = escape(invite_url, quote=True)
    content = (
        f"<p>{safe_inviter} invited you to join <strong>{safe_entity}</strong> on Manor AI.</p>"
        "<p>Use the secure link below to create your account and join the team.</p>"
        f"<p style='margin-top:20px;'>"
        f"<a href='{safe_invite_url}' style='background:#0d9488;color:#fff;text-decoration:none;"
        f"padding:10px 18px;border-radius:8px;display:inline-block;font-weight:600;'>"
        f"Accept invitation</a></p>"
        f"<p style='color:#94a3b8;font-size:12px;margin-top:24px;'>"
        f"If the button does not work, paste this link into your browser:<br>"
        f"<span style='word-break:break-all;'>{safe_invite_url}</span></p>"
    )
    return await send_common_email(
        to,
        f"You've been invited to {subject_entity} on Manor AI",
        content,
    )


async def send_task_review_email(to: str, task_result: str, feedback_url: str) -> bool:
    """Send task completion review email with rating buttons."""
    subject = "Task completed — please rate your experience"
    html = _render("task_review", task_result=task_result, feedback_url=feedback_url)
    return await send_email(to, subject, html)


async def send_task_checkin_email(to: str, checkin_url: str) -> bool:
    """Send task check-in notification."""
    subject = "Task check-in — view your task details"
    html = _render("task_checkin", checkin_url=checkin_url)
    return await send_email(to, subject, html)


async def send_common_email(to: str, subject: str, content: str) -> bool:
    """Send a generic email with custom HTML content in the branded wrapper."""
    html = _render("common", content=content)
    return await send_email(to, subject, html)


# ── Payment receipts (Stripe webhook → user) ────────────────────────

def _fmt_dollars(cents: Optional[int]) -> str:
    if not cents:
        return "$0.00"
    return f"${cents / 100:.2f}"


def _receipt_row(label: str, value: str, *, muted: bool = False, is_html: bool = False) -> str:
    safe_value = value if is_html else html.escape(str(value))
    cls = "value muted" if muted else "value"
    return f"<tr><td class='label'>{html.escape(label)}</td><td class='{cls}'>{safe_value}</td></tr>"


def _payment_cta_block(url: Optional[str], label: str) -> str:
    if not url:
        return ""
    return (
        f'<section class="cta-section"><div class="section-content">'
        f'<a href="{html.escape(url, quote=True)}" class="btn-primary" '
        f'style="color: #ffffff !important;">{html.escape(label)}</a>'
        f"</div></section>"
    )


def _render_payment_email(
    *,
    subject: str,
    badge_label: str,
    badge_color: str,
    badge_bg: str,
    hero_text: str,
    subhead: str,
    display_name: Optional[str],
    body_html: str,
    receipt_title: str,
    receipt_rows_html: str,
    cta_url: Optional[str],
    cta_label: Optional[str],
    extra_block_html: str = "",
    extra_block_style: str = "",
) -> str:
    return _render(
        "payment_receipt",
        subject=html.escape(subject),
        badge_label=html.escape(badge_label),
        badge_color=badge_color,
        badge_bg=badge_bg,
        hero_text=html.escape(hero_text),
        subhead=html.escape(subhead),
        display_name=html.escape(display_name or "there"),
        body_html=body_html,
        receipt_title=html.escape(receipt_title),
        receipt_rows_html=receipt_rows_html,
        cta_block=_payment_cta_block(cta_url, cta_label or "Open dashboard"),
        extra_block_html=extra_block_html,
        extra_block_style=extra_block_style,
    )


async def send_subscription_started_email(
    to: str, *,
    display_name: Optional[str],
    plan_name: str,
    plan_perks: list[str],
    amount_cents: int,
    payment_method_label: Optional[str] = None,
    next_renewal_label: Optional[str] = None,
    invoice_url: Optional[str] = None,
    invoice_id: Optional[str] = None,
    dashboard_url: Optional[str] = None,
) -> bool:
    """Fire when a user just upgraded to a paid plan for the first time
    or switched between paid plans. ``plan_perks`` is a list of HTML-
    escape-safe strings rendered as a checkmarked feature list."""
    perks_html = "".join(
        f"<li>{html.escape(p)}</li>" for p in plan_perks
    )
    body = (
        f"<p>Your {html.escape(plan_name)} subscription started just now. "
        f"Here's what's unlocked on your account:</p>"
        f"<ul class='features-list'>{perks_html}</ul>"
    )
    receipt = (
        _receipt_row("Plan", f"{plan_name} · monthly")
        + _receipt_row("Amount", _fmt_dollars(amount_cents))
        + (_receipt_row("Payment method", payment_method_label) if payment_method_label else "")
        + (_receipt_row("Next renewal", next_renewal_label, muted=True) if next_renewal_label else "")
        + (_receipt_row(
            "Invoice",
            f'<a href="{html.escape(invoice_url, quote=True)}">{html.escape(invoice_id or "Download")}</a>',
            is_html=True,
        ) if invoice_url else "")
    )
    subject = f"You're now on Manor AI {plan_name}"
    html_body = _render_payment_email(
        subject=subject,
        badge_label="Subscription Activated", badge_color="#0D9488", badge_bg="#F0FDFA",
        hero_text=f"You're now on {plan_name}",
        subhead="Thanks for upgrading — your new quotas are live immediately.",
        display_name=display_name,
        body_html=body,
        receipt_title="Receipt", receipt_rows_html=receipt,
        cta_url=dashboard_url, cta_label="Open your dashboard",
    )
    return await send_email(to, subject, html_body)


async def send_subscription_renewed_email(
    to: str, *,
    display_name: Optional[str],
    plan_name: str,
    amount_cents: int,
    payment_method_label: Optional[str] = None,
    next_renewal_label: Optional[str] = None,
    invoice_url: Optional[str] = None,
    invoice_id: Optional[str] = None,
    dashboard_url: Optional[str] = None,
) -> bool:
    """Fire on every successful monthly Stripe renewal so the user has
    a paper trail and isn't surprised by the recurring charge."""
    body = (
        f"<p>Your <strong>{html.escape(plan_name)}</strong> subscription "
        f"just renewed for another billing cycle. Your monthly credit "
        f"allowance has been reset and any unused plan credits from "
        f"last cycle have expired — top-up credits you purchased "
        f"separately keep rolling over.</p>"
    )
    receipt = (
        _receipt_row("Plan", f"{plan_name} · monthly")
        + _receipt_row("Amount", _fmt_dollars(amount_cents))
        + (_receipt_row("Payment method", payment_method_label) if payment_method_label else "")
        + (_receipt_row("Next renewal", next_renewal_label, muted=True) if next_renewal_label else "")
        + (_receipt_row(
            "Invoice",
            f'<a href="{html.escape(invoice_url, quote=True)}">{html.escape(invoice_id or "Download")}</a>',
            is_html=True,
        ) if invoice_url else "")
    )
    subject = f"Your Manor AI {plan_name} renewed"
    html_body = _render_payment_email(
        subject=subject,
        badge_label="Subscription Renewed", badge_color="#0D9488", badge_bg="#F0FDFA",
        hero_text="You're set for another month",
        subhead="Your subscription renewed and credits are refilled.",
        display_name=display_name,
        body_html=body,
        receipt_title="Receipt", receipt_rows_html=receipt,
        cta_url=dashboard_url, cta_label="Open your dashboard",
    )
    return await send_email(to, subject, html_body)


async def send_credit_topup_email(
    to: str, *,
    display_name: Optional[str],
    credits_added: int,
    new_balance: Optional[int],
    amount_cents: int,
    payment_method_label: Optional[str] = None,
    invoice_url: Optional[str] = None,
    invoice_id: Optional[str] = None,
    usage_url: Optional[str] = None,
) -> bool:
    """Fire when a one-time credit purchase clears."""
    bump_html = (
        f'<div style="margin:20px 48px 4px;padding:16px 20px;'
        f'background:linear-gradient(135deg, rgba(13,148,136,0.06) 0%, rgba(20,184,166,0.06) 100%);'
        f'border:1px solid rgba(13,148,136,0.18);border-radius:14px;'
        f'display:flex;align-items:center;justify-content:space-between;gap:16px;">'
        f'<div>'
        f'<div style="font-size:11px;color:#475569;font-weight:600;'
        f'text-transform:uppercase;letter-spacing:0.05em;">Added</div>'
        f'<div style="font-size:22px;font-weight:800;color:#0F766E;">'
        f'+{credits_added:,}</div>'
        f'</div>'
    )
    if new_balance is not None:
        bump_html += (
            f'<div style="text-align:right;">'
            f'<div style="font-size:11px;color:#475569;font-weight:600;'
            f'text-transform:uppercase;letter-spacing:0.05em;">New balance</div>'
            f'<div style="font-size:18px;font-weight:700;color:#0F172A;">'
            f'{new_balance:,} credits</div>'
            f'</div>'
        )
    bump_html += "</div>"

    body = (
        f"<p>Your top-up purchase went through. Top-up credits "
        f"<strong>never expire</strong> and stack on top of your plan's "
        f"monthly allowance.</p>"
        f"<p>If you're regularly running low, turning on auto-recharge "
        f"or upgrading to the next plan is usually cheaper per credit.</p>"
    )
    receipt = (
        _receipt_row("Item", f"Credit top-up · {credits_added:,} credits")
        + _receipt_row("Amount", _fmt_dollars(amount_cents))
        + (_receipt_row("Payment method", payment_method_label) if payment_method_label else "")
        + (_receipt_row(
            "Invoice",
            f'<a href="{html.escape(invoice_url, quote=True)}">{html.escape(invoice_id or "Download")}</a>',
            is_html=True,
        ) if invoice_url else "")
    )
    subject = f"Credit top-up confirmed — +{credits_added:,} credits"
    html_body = _render_payment_email(
        subject=subject,
        badge_label="Credits Topped Up", badge_color="#0D9488", badge_bg="#F0FDFA",
        hero_text=f"+{credits_added:,} credits added",
        subhead="Your balance is ready to go.",
        display_name=display_name,
        body_html=body,
        receipt_title="Receipt", receipt_rows_html=receipt,
        cta_url=usage_url, cta_label="View usage",
        extra_block_html=bump_html,
    )
    return await send_email(to, subject, html_body)


async def send_subscription_canceled_email(
    to: str, *,
    display_name: Optional[str],
    plan_name: str,
    access_ends_label: Optional[str] = None,
    reactivate_url: Optional[str] = None,
) -> bool:
    """Fire when a user schedules cancellation (cancel_at_period_end).
    Their access continues until the end of the paid period."""
    if access_ends_label:
        body = (
            f"<p>We've scheduled your <strong>{html.escape(plan_name)}</strong> "
            f"subscription to end. You'll keep full access until "
            f"<strong>{html.escape(access_ends_label)}</strong> — after that, your "
            f"workspace will move to the Free plan. Your data, conversations, "
            f"automations, and knowledge base all stay intact; only the quotas "
            f"and model key access shrink to Free-tier limits.</p>"
            f"<p>If you change your mind before then, reactivating is one click "
            f"and there's no penalty.</p>"
        )
    else:
        body = (
            f"<p>Your <strong>{html.escape(plan_name)}</strong> subscription has "
            f"been canceled. Your workspace will move to the Free plan; data, "
            f"conversations, automations, and knowledge base all stay intact.</p>"
            f"<p>Reactivating is one click whenever you're ready.</p>"
        )

    receipt = (
        _receipt_row("Plan", plan_name)
        + (_receipt_row("Access ends", access_ends_label, muted=True) if access_ends_label else "")
        + _receipt_row("Future charges", "None", muted=True)
    )
    subject = f"Your Manor AI {plan_name} subscription is canceled"
    html_body = _render_payment_email(
        subject=subject,
        badge_label="Subscription Canceled", badge_color="#B45309", badge_bg="#FEF3C7",
        hero_text="We're sorry to see you go",
        subhead="Your access continues through the end of the paid period.",
        display_name=display_name,
        body_html=body,
        receipt_title="Cancellation summary", receipt_rows_html=receipt,
        cta_url=reactivate_url, cta_label="Reactivate subscription",
    )
    return await send_email(to, subject, html_body)


async def send_waitlist_invite_email(
    to: str, *, name: str, code: str, expires_at_label: Optional[str] = None,
) -> bool:
    """Email a waitlist applicant their invitation code + signup link.

    No dedicated template — composes inside ``common`` so the visual
    style stays consistent without another file to maintain.
    """
    app_url = os.getenv("APP_URL", "http://localhost:3010").rstrip("/")
    signup_url = f"{app_url}/login?invite={code}"
    greeting = f"Hi {name}," if name else "Hi,"
    expiry_line = (
        f"<p style='color:#64748b;font-size:13px;'>The code is valid until {expires_at_label}.</p>"
        if expires_at_label else ""
    )
    content = (
        f"<p>{greeting}</p>"
        f"<p>You're off the waitlist — welcome to Manor AI.</p>"
        f"<p>Your invitation code:</p>"
        f"<p style='font-family:ui-monospace,monospace;font-size:18px;font-weight:700;"
        f"letter-spacing:0.06em;background:#f1f5f9;padding:12px 16px;border-radius:8px;"
        f"display:inline-block;'>{code}</p>"
        f"<p style='margin-top:20px;'>"
        f"<a href='{signup_url}' style='background:#0d9488;color:#fff;text-decoration:none;"
        f"padding:10px 18px;border-radius:8px;display:inline-block;font-weight:600;'>"
        f"Create your account →</a></p>"
        f"{expiry_line}"
        f"<p style='color:#94a3b8;font-size:12px;margin-top:24px;'>"
        f"Or paste the code on the signup page if the button doesn't work.</p>"
    )
    return await send_common_email(to, "Your Manor AI invitation", content)


# Backward-compatible aliases
async def send_notification_email(to: str, notification_title: str, notification_body: str) -> bool:
    return await send_common_email(to, f"Manor AI: {notification_title}", notification_body)


async def send_task_assigned_email(to: str, task_title: str, assigner_name: str) -> bool:
    content = f"<p><strong>{assigner_name}</strong> assigned you a task: <strong>{task_title}</strong></p>"
    return await send_common_email(to, f"Task assigned: {task_title}", content)
