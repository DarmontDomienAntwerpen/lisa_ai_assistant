"""Intern, read-only dashboard: per tenant calls/kosten/escalaties, en een
transcript-drill-down per klant. HTTP Basic Auth — enkel voor Darmont Digital
zelf (geen multi-user login), zie CLAUDE.md.

Bevat gevoelige data (gespreksinhoud, telefoonnummers) — daarom altijd achter
auth, nooit publiek bereikbaar, en DASHBOARD_PASSWORD moet expliciet gezet
zijn (leeg = toegang geweigerd, nooit "geen wachtwoord vereist").
"""
from __future__ import annotations

import html
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app import conversation_store, tenants, usage_log
from app.config import DASHBOARD_PASSWORD, DASHBOARD_USERNAME, get_pool

router = APIRouter(prefix="/dashboard")
_security = HTTPBasic()


def _require_auth(credentials: Annotated[HTTPBasicCredentials, Depends(_security)]) -> None:
    if not DASHBOARD_PASSWORD:
        # Geen stille faalstand: leeg wachtwoord betekent geweigerd, nooit
        # "geen auth nodig" — dit dashboard toont gespreksinhoud en nummers.
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Dashboard niet geconfigureerd (DASHBOARD_PASSWORD ontbreekt)")
    correct_username = secrets.compare_digest(credentials.username, DASHBOARD_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, DASHBOARD_PASSWORD)
    if not (correct_username and correct_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Ongeldige login",
            headers={"WWW-Authenticate": "Basic"},
        )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; margin: 2rem; color: #1f2937; }}
  h1 {{ font-size: 1.3rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
  th, td {{ text-align: left; padding: 0.5rem 0.8rem; border-bottom: 1px solid #e5e7eb; font-size: 0.9rem; }}
  th {{ background: #f9fafb; }}
  a {{ color: #2563eb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
  .muted {{ color: #6b7280; font-size: 0.85rem; }}
  .bubble {{ padding: 0.5rem 0.8rem; border-radius: 8px; margin: 0.3rem 0; max-width: 70%; }}
  .bubble.user {{ background: #eff6ff; }}
  .bubble.assistant {{ background: #f3f4f6; margin-left: auto; }}
</style>
</head>
<body>
{body}
</body>
</html>"""


@router.get("", response_class=HTMLResponse)
async def overview(_: None = Depends(_require_auth)) -> str:
    pool = await get_pool()
    all_tenants = await tenants.list_tenants(pool)
    since = datetime.now(timezone.utc) - timedelta(days=30)

    rows = []
    for tenant in all_tenants:
        summary = await usage_log.get_tenant_usage_summary(pool, tenant.client_id, since=since)
        last_call = summary["last_call_at"].strftime("%Y-%m-%d %H:%M") if summary["last_call_at"] else "—"
        rows.append(f"""<tr>
            <td><a href="/dashboard/{html.escape(tenant.client_id)}">{html.escape(tenant.business_name)}</a></td>
            <td>{html.escape(tenant.niche)}</td>
            <td>{summary['call_count']}</td>
            <td>${summary['total_cost_usd']:.2f}</td>
            <td>{summary['escalations']}</td>
            <td class="muted">{last_call}</td>
        </tr>""")

    body = f"""<h1>Lisa — dashboard</h1>
    <p class="muted">Laatste 30 dagen, per tenant.</p>
    <table>
      <tr><th>Zaak</th><th>Niche</th><th>Calls</th><th>Kosten</th><th>Escalaties</th><th>Laatste call</th></tr>
      {''.join(rows) or '<tr><td colspan="6" class="muted">Nog geen tenants.</td></tr>'}
    </table>"""
    return _page("Lisa — dashboard", body)


@router.get("/{client_id}", response_class=HTMLResponse)
async def tenant_detail(client_id: str, _: None = Depends(_require_auth)) -> str:
    pool = await get_pool()
    all_tenants = await tenants.list_tenants(pool)
    tenant = next((t for t in all_tenants if t.client_id == client_id), None)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Onbekende tenant")

    async with pool.acquire() as conn:
        calls = await conn.fetch(
            "SELECT phone_number, channel, started_at FROM calls WHERE tenant_id = $1 ORDER BY started_at DESC LIMIT 50",
            client_id,
        )

    rows = [
        f"""<tr>
            <td><a href="/dashboard/{html.escape(client_id)}/{html.escape(c['phone_number'])}">{html.escape(c['phone_number'])}</a></td>
            <td>{html.escape(c['channel'])}</td>
            <td class="muted">{c['started_at'].strftime('%Y-%m-%d %H:%M')}</td>
        </tr>"""
        for c in calls
    ]

    body = f"""<p><a href="/dashboard">&larr; Overzicht</a></p>
    <h1>{html.escape(tenant.business_name)}</h1>
    <p class="muted">{html.escape(tenant.niche)} — {html.escape(tenant.twilio_number)}</p>
    <table>
      <tr><th>Nummer</th><th>Kanaal</th><th>Gestart</th></tr>
      {''.join(rows) or '<tr><td colspan="3" class="muted">Nog geen calls.</td></tr>'}
    </table>"""
    return _page(f"Lisa — {tenant.business_name}", body)


@router.get("/{client_id}/{phone_number}", response_class=HTMLResponse)
async def call_transcript(client_id: str, phone_number: str, _: None = Depends(_require_auth)) -> str:
    pool = await get_pool()
    history = await conversation_store.get_history(pool, client_id, phone_number, limit=200)

    bubbles = [
        f'<div class="bubble {html.escape(m["role"])}"><b>{html.escape(m["role"])}:</b> {html.escape(m["content"])}</div>'
        for m in history
    ]

    body = f"""<p><a href="/dashboard/{html.escape(client_id)}">&larr; Terug</a></p>
    <h1>Gesprek met {html.escape(phone_number)}</h1>
    {''.join(bubbles) or '<p class="muted">Geen gesprekshistorie.</p>'}"""
    return _page(f"Lisa — gesprek {phone_number}", body)
