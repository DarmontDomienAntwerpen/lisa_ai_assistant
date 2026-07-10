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
from app.config import (
    DASHBOARD_PASSWORD,
    DASHBOARD_USERNAME,
    FIXED_MONTHLY_INFRA_COST_EUR,
    PRICING_BASE_EUR,
    PRICING_INCLUDED_CALLS,
    PRICING_OVERAGE_EUR,
    TWILIO_MONTHLY_NUMBER_USD,
    USD_TO_EUR_RATE,
    get_pool,
)

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


def _estimated_invoice_eur(call_count: int) -> float:
    """Aanbevolen factuurbedrag: basisprijs + overage boven het inbegrepen
    aantal gesprekken. Schatting/hulpmiddel voor Domien, geen echte
    facturatie-integratie — zie CLAUDE.md/PRICING_* env vars."""
    overage_calls = max(0, call_count - PRICING_INCLUDED_CALLS)
    return PRICING_BASE_EUR + overage_calls * PRICING_OVERAGE_EUR


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
  table.breakdown {{ width: auto; margin: 0.4rem 0 0 0; }}
  table.breakdown td {{ padding: 0.2rem 0.6rem; border-bottom: none; font-size: 0.85rem; }}
  details summary {{ cursor: pointer; }}
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
    total_invoice_eur = 0.0
    total_variable_cost_eur = 0.0
    for tenant in all_tenants:
        summary = await usage_log.get_tenant_usage_summary(pool, tenant.client_id, since=since)
        last_call = summary["last_call_at"].strftime("%Y-%m-%d %H:%M") if summary["last_call_at"] else "—"
        invoice = _estimated_invoice_eur(summary["call_count"])

        # asyncpg geeft SUM(numeric)/EXTRACT(EPOCH ...) terug als Decimal —
        # expliciet naar float, anders crasht de optelling hieronder.
        ai_cost_usd = float(summary["total_cost_usd"])
        twilio_cost_usd = float(summary["total_twilio_cost_usd"]) + TWILIO_MONTHLY_NUMBER_USD
        variable_cost_eur = (ai_cost_usd + twilio_cost_usd) * USD_TO_EUR_RATE

        total_invoice_eur += invoice
        total_variable_cost_eur += variable_cost_eur

        rows.append(f"""<tr>
            <td><a href="/dashboard/{html.escape(tenant.client_id)}">{html.escape(tenant.business_name)}</a></td>
            <td>{html.escape(tenant.niche)}</td>
            <td>{summary['call_count']}</td>
            <td>${ai_cost_usd:.2f}</td>
            <td>${twilio_cost_usd:.2f}</td>
            <td>€{variable_cost_eur:.2f}</td>
            <td>€{invoice:.2f}</td>
            <td>{summary['escalations']}</td>
            <td class="muted">{last_call}</td>
        </tr>""")

    total_cost_eur = total_variable_cost_eur + FIXED_MONTHLY_INFRA_COST_EUR
    margin_eur = total_invoice_eur - total_cost_eur

    body = f"""<h1>Lisa — dashboard</h1>
    <p class="muted">Laatste 30 dagen, per tenant. "AI-kost" en "Twilio-kost" zijn
    gemeten (OpenAI/Twilio-tarieven, incl. nummerhuur), "Totale kost" rekent dat
    om naar EUR (koers {USD_TO_EUR_RATE:.2f}). "Aanbevolen factuur" is een
    schatting (basis €{PRICING_BASE_EUR:.0f}, {PRICING_INCLUDED_CALLS} calls inbegrepen,
    €{PRICING_OVERAGE_EUR:.2f}/call daarboven) — geen echte facturatie-integratie.</p>
    <table>
      <tr><th>Zaak</th><th>Niche</th><th>Calls</th><th>AI-kost</th><th>Twilio-kost</th><th>Totale kost</th><th>Aanbevolen factuur</th><th>Escalaties</th><th>Laatste call</th></tr>
      {''.join(rows) or '<tr><td colspan="9" class="muted">Nog geen tenants.</td></tr>'}
    </table>
    <p class="muted" style="margin-top:1rem;">
      Vaste, gedeelde kost (Railway + Postgres, niet per klant op te splitsen):
      €{FIXED_MONTHLY_INFRA_COST_EUR:.2f}/maand (zie FIXED_MONTHLY_INFRA_COST_EUR).<br>
      Totaal deze 30 dagen: €{total_variable_cost_eur:.2f} variabel (AI + Twilio, alle klanten)
      + €{FIXED_MONTHLY_INFRA_COST_EUR:.2f} vast = <b>€{total_cost_eur:.2f}</b> kost,
      tegenover <b>€{total_invoice_eur:.2f}</b> aanbevolen facturatie
      → marge <b>€{margin_eur:.2f}</b>.
    </p>"""
    return _page("Lisa — dashboard", body)


@router.get("/{client_id}", response_class=HTMLResponse)
async def tenant_detail(client_id: str, _: None = Depends(_require_auth)) -> str:
    pool = await get_pool()
    all_tenants = await tenants.list_tenants(pool)
    tenant = next((t for t in all_tenants if t.client_id == client_id), None)
    if tenant is None:
        raise HTTPException(status_code=404, detail="Onbekende tenant")

    calls = await usage_log.get_calls_with_cost_breakdown(pool, client_id, limit=50)

    rows = []
    for c in calls:
        b = c["cost_breakdown"]
        escalated_badge = " 🚨" if c["escalated"] else ""
        duration = c["duration_seconds"]
        duration_text = f"{int(duration // 60)}m{int(duration % 60):02d}s" if duration else "—"
        rows.append(f"""<tr>
            <td><a href="/dashboard/{html.escape(client_id)}/{html.escape(c['phone_number'])}">{html.escape(c['phone_number'])}</a></td>
            <td>{html.escape(c['channel'])}</td>
            <td class="muted">{c['started_at'].strftime('%Y-%m-%d %H:%M')}</td>
            <td>${c['combined_total_cost_usd']:.4f}{escalated_badge}</td>
            <td>
              <details>
                <summary class="muted">waar komt dit vandaan?</summary>
                <table class="breakdown">
                  <tr><td>Vers input (OpenAI)</td><td>${b['fresh_input_cost_usd']:.4f}</td></tr>
                  <tr><td>Cached input (OpenAI)</td><td>${b['cached_input_cost_usd']:.4f}</td></tr>
                  <tr><td>Output (OpenAI)</td><td>${b['output_cost_usd']:.4f}</td></tr>
                  <tr><td>Telefonie (Twilio, {duration_text})</td><td>${c['twilio_cost_usd']:.4f}</td></tr>
                  <tr><td><b>Totaal</b></td><td><b>${c['combined_total_cost_usd']:.4f}</b></td></tr>
                  <tr><td class="muted">Beurten</td><td class="muted">{c['turns']}</td></tr>
                </table>
              </details>
            </td>
        </tr>""")

    body = f"""<p><a href="/dashboard">&larr; Overzicht</a></p>
    <h1>{html.escape(tenant.business_name)}</h1>
    <p class="muted">{html.escape(tenant.niche)} — {html.escape(tenant.twilio_number)}</p>
    <table>
      <tr><th>Nummer</th><th>Kanaal</th><th>Gestart</th><th>Kost</th><th>Herkomst</th></tr>
      {''.join(rows) or '<tr><td colspan="5" class="muted">Nog geen calls.</td></tr>'}
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
