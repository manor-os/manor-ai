"""Report generation — build HTML reports from entity data, optionally convert to PDF.

Supports: task summary, usage report, activity digest, custom.
Uses inline HTML with CSS for styling (no external template engine needed).
"""
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


async def generate_task_report(db: AsyncSession, entity_id: str, *, days: int = 30) -> dict:
    """Generate a task summary report.
    Returns: {title, html, text_summary, data}
    """
    from packages.core.services.analytics_service import get_dashboard_stats, get_task_trends

    stats = await get_dashboard_stats(db, entity_id)
    trends = await get_task_trends(db, entity_id, days=days)

    task_stats = stats.get("tasks", {})
    by_status = task_stats.get("by_status", {})

    # Build HTML report
    html = _report_header(f"Task Report — Last {days} Days")
    html += _stats_row([
        ("Total Tasks", task_stats.get("total", 0)),
        ("Completed", by_status.get("completed", 0)),
        ("In Progress", by_status.get("in_progress", 0)),
        ("Pending", by_status.get("pending", 0)),
        ("Overdue", task_stats.get("overdue", 0)),
    ])

    # Trend table
    if trends:
        html += "<h3 style='margin-top:24px;'>Daily Trends</h3>"
        html += "<table style='width:100%;border-collapse:collapse;'>"
        html += "<tr style='background:#f1f5f9;'><th style='padding:8px;text-align:left;'>Date</th><th>Created</th><th>Completed</th></tr>"
        for t in trends[-14:]:  # last 14 days
            html += f"<tr><td style='padding:8px;border-bottom:1px solid #e2e8f0;'>{t.get('date','')}</td>"
            html += f"<td style='text-align:center;border-bottom:1px solid #e2e8f0;'>{t.get('created',0)}</td>"
            html += f"<td style='text-align:center;border-bottom:1px solid #e2e8f0;'>{t.get('completed',0)}</td></tr>"
        html += "</table>"

    html += _report_footer()

    text_summary = f"Tasks: {task_stats.get('total',0)} total, {by_status.get('completed',0)} completed, {task_stats.get('overdue',0)} overdue"

    return {
        "title": f"Task Report — Last {days} Days",
        "html": html,
        "text_summary": text_summary,
        "data": {"stats": stats, "trends": trends},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def generate_usage_report(db: AsyncSession, entity_id: str, *, days: int = 30) -> dict:
    """Generate a token usage and cost report."""
    from packages.core.services.usage_service import get_usage_summary

    summary = await get_usage_summary(db, entity_id, days=days)

    html = _report_header(f"Usage Report — Last {days} Days")
    html += _stats_row([
        ("Total Tokens", f"{summary.get('total_tokens', 0):,}"),
        ("Total Cost", f"${summary.get('total_cost', 0):.4f}"),
    ])

    by_model = summary.get("by_model", [])
    if by_model:
        html += "<h3 style='margin-top:24px;'>By Model</h3>"
        html += "<table style='width:100%;border-collapse:collapse;'>"
        html += "<tr style='background:#f1f5f9;'><th style='padding:8px;text-align:left;'>Model</th><th>Tokens</th><th>Cost</th></tr>"
        for m in by_model:
            html += f"<tr><td style='padding:8px;border-bottom:1px solid #e2e8f0;'>{m.get('model','')}</td>"
            html += f"<td style='text-align:center;border-bottom:1px solid #e2e8f0;'>{m.get('tokens',0):,}</td>"
            html += f"<td style='text-align:center;border-bottom:1px solid #e2e8f0;'>${m.get('cost',0):.4f}</td></tr>"
        html += "</table>"

    html += _report_footer()

    return {
        "title": f"Usage Report — Last {days} Days",
        "html": html,
        "text_summary": f"Tokens: {summary.get('total_tokens',0):,}, Cost: ${summary.get('total_cost',0):.4f}",
        "data": summary,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


async def generate_activity_report(db: AsyncSession, entity_id: str, *, days: int = 7) -> dict:
    """Generate an activity digest report."""
    from packages.core.services.analytics_service import get_recent_activity

    activities = await get_recent_activity(db, entity_id, limit=50)

    html = _report_header(f"Activity Digest — Last {days} Days")

    if activities:
        html += "<ul style='list-style:none;padding:0;'>"
        for a in activities[:30]:
            html += f"<li style='padding:8px 0;border-bottom:1px solid #f1f5f9;'>"
            html += f"<strong>{a.get('type','')}</strong> — {a.get('name','')}"
            if a.get('timestamp'):
                html += f" <span style='color:#94a3b8;font-size:12px;'>({a['timestamp'][:10]})</span>"
            html += "</li>"
        html += "</ul>"
    else:
        html += "<p style='color:#94a3b8;'>No activity in this period.</p>"

    html += _report_footer()

    return {
        "title": f"Activity Digest — Last {days} Days",
        "html": html,
        "text_summary": f"{len(activities)} activities",
        "data": {"activities": activities},
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── HTML helpers ──

def _report_header(title: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:700px;margin:0 auto;padding:24px;color:#1e293b;">
<div style="border-bottom:3px solid #2563eb;padding-bottom:16px;margin-bottom:24px;">
    <h1 style="margin:0;font-size:24px;color:#0f172a;">Manor AI</h1>
    <h2 style="margin:4px 0 0;font-size:18px;font-weight:normal;color:#475569;">{title}</h2>
    <p style="margin:4px 0 0;font-size:12px;color:#94a3b8;">Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}</p>
</div>
"""


def _stats_row(items: list[tuple[str, any]]) -> str:
    html = "<div style='display:flex;gap:16px;flex-wrap:wrap;'>"
    for label, value in items:
        html += f"""<div style="flex:1;min-width:120px;background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:16px;text-align:center;">
            <div style="font-size:24px;font-weight:bold;color:#0f172a;">{value}</div>
            <div style="font-size:12px;color:#64748b;margin-top:4px;">{label}</div>
        </div>"""
    html += "</div>"
    return html


def _report_footer() -> str:
    return """<div style="margin-top:32px;padding-top:16px;border-top:1px solid #e2e8f0;font-size:12px;color:#94a3b8;">
    <p>This report was automatically generated by Manor AI.</p>
</div></body></html>"""
