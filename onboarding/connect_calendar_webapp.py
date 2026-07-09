"""Klant-installatiestap: branded lokaal schermpje voor de Google Agenda-
koppeling — draai dit tijdens de installatie bij de klant in plaats van het
kale CLI-script connect_google_calendar.py. Zelfde onderliggende OAuth-flow
(InstalledAppFlow, dezelfde google_oauth_client_secret.json), enkel met een
eenvoudig, professioneel schermpje ervoor en erna i.p.v. een kale terminal.

De klant ziet dit op jouw laptop, klikt op "Connecteer", logt in met zijn/
haar eigen Google-account in een nieuw tabblad (Google's eigen inlog- en
toestemmingsscherm) — geen wachtwoord gedeeld, niets geïnstalleerd.

Gebruik:
  python onboarding/connect_calendar_webapp.py <client_id>
  open http://localhost:8003

Vereist dat de tenant al bestaat (draai eerst onboarding/onboard_tenant.py)
en dat google_oauth_client_secret.json in de projectroot staat.
"""
from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import html  # noqa: E402

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402

from app import tenants  # noqa: E402
from app.config import close_pool, get_pool  # noqa: E402

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
]
CLIENT_SECRET_PATH = Path(__file__).resolve().parent.parent / "google_oauth_client_secret.json"

if len(sys.argv) != 2:
    raise SystemExit("Gebruik: python onboarding/connect_calendar_webapp.py <client_id>")
CLIENT_ID_ARG = sys.argv[1]

_state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    tenant = await tenants.get_tenant_by_client_id(pool, CLIENT_ID_ARG)
    if tenant is None:
        await close_pool()
        raise SystemExit(f"Geen tenant gevonden met client_id '{CLIENT_ID_ARG}' — draai eerst onboarding/onboard_tenant.py.")
    _state["pool"] = pool
    _state["tenant"] = tenant
    yield
    await close_pool()


app = FastAPI(title="Lisa — agenda koppelen", lifespan=lifespan)

_PAGE_STYLE = """
  body {
    margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: 2rem 1rem; background: #f9fafb; color: #1f2937;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif; text-align: center;
  }
  .card { max-width: 420px; }
  h1 { font-size: 1.3rem; margin-bottom: 0.4rem; }
  p { color: #6b7280; font-size: 0.95rem; line-height: 1.5; }
  button, a.button {
    display: inline-block; margin-top: 1.5rem; padding: 0.8rem 1.8rem; border-radius: 10px;
    border: none; font-size: 1rem; cursor: pointer; background: #2563eb; color: #fff;
    text-decoration: none; font-weight: 600;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  .status { margin-top: 1rem; font-size: 0.85rem; color: #9ca3af; }
  .error { color: #dc2626; }
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    tenant = _state["tenant"]
    return f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lisa — agenda koppelen</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card">
  <h1>Google Agenda koppelen</h1>
  <p>Voor <b>{html.escape(tenant.business_name)}</b> — log in met het Google-account
  waarvan de agenda door Lisa gebruikt moet worden. Er wordt niets geïnstalleerd
  en er wordt geen wachtwoord gedeeld.</p>
  <button id="connectBtn" onclick="startConnect()">Connecteer</button>
  <div class="status" id="status"></div>
</div>
<script>
async function startConnect() {{
  const btn = document.getElementById('connectBtn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.textContent = 'Bezig — een nieuw tabblad opent voor het Google-inlogscherm...';
  try {{
    const res = await fetch('/connect', {{ method: 'POST' }});
    const data = await res.json();
    if (res.ok) {{
      document.body.innerHTML = `<div class="card"><h1>Klaar!</h1><p>Agenda van ${{data.business_name}} is gekoppeld.<br>(calendar_id: ${{data.calendar_id}})</p><p>Test nu met een boeking of annulering voor de klant live gaat.</p></div>`;
    }} else {{
      status.textContent = 'Fout: ' + data.detail;
      status.className = 'status error';
      btn.disabled = false;
    }}
  }} catch (e) {{
    status.textContent = 'Fout: ' + e;
    status.className = 'status error';
    btn.disabled = false;
  }}
}}
</script>
</body></html>"""


@app.post("/connect")
async def connect() -> dict:
    tenant = _state["tenant"]
    pool = _state["pool"]

    if not CLIENT_SECRET_PATH.exists():
        raise HTTPException(status_code=500, detail="Ontbreekt: google_oauth_client_secret.json")

    def _run_oauth():
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
        return flow.run_local_server(port=0)

    credentials = await asyncio.to_thread(_run_oauth)

    calendar_id = "primary"
    tenant.calendar_type = "google_calendar"
    tenant.calendar_config = {
        "oauth_credentials": {
            "token": credentials.token,
            "refresh_token": credentials.refresh_token,
            "token_uri": credentials.token_uri,
            "client_id": credentials.client_id,
            "client_secret": credentials.client_secret,
            "scopes": credentials.scopes,
        },
        "calendar_id": calendar_id,
    }

    if not tenant.escalation_email:
        try:
            userinfo = await asyncio.to_thread(
                lambda: build("oauth2", "v2", credentials=credentials).userinfo().get().execute()
            )
            google_email = userinfo.get("email", "")
        except Exception:  # noqa: BLE001 — nooit de hele koppeling laten falen op deze extra stap
            google_email = ""
        if google_email:
            tenant.escalation_email = google_email

    await tenants.upsert_tenant(pool, tenant)

    return {"business_name": tenant.business_name, "calendar_id": calendar_id}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8003)
