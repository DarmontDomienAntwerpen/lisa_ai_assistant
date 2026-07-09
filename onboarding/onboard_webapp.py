"""Eén doorlopend lokaal schermpje voor de volledige klant-onboarding: eerst
vul jij de bedrijfsgegevens in (tenant aanmaken), en zodra je op "Klaar" drukt
schakelt hetzelfde scherm automatisch door naar de agenda-koppeling — geef op
dat moment gewoon de laptop aan de klant, die klikt enkel nog "Connecteer",
logt in met hun eigen Google-account, en de koppeling is meteen opgeslagen.

Schrijft altijd naar de PRODUCTIE-database op Railway (haalt DATABASE_PUBLIC_URL
rechtstreeks op via de railway CLI, negeert .env) — nooit naar de lokale
testdatabase, dus geen risico dat een echte klant-koppeling in het niets
verdwijnt. Vereist dat `railway` geïnstalleerd en aan dit project gelinkt is.

Gebruik:
  python onboarding/onboard_webapp.py
  open http://localhost:8004
"""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import HTMLResponse  # noqa: E402
from google_auth_oauthlib.flow import InstalledAppFlow  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app import config, tenants  # noqa: E402
from app.config import close_pool, get_pool  # noqa: E402
from app.tenants import Tenant  # noqa: E402


def _use_production_database() -> None:
    """Onboarding hoort ALTIJD naar de productie-database te schrijven, nooit
    naar de lokale testdatabase in .env — anders komt een echte klant-
    koppeling nergens terecht waar de live Lisa op Railway ze kan vinden.
    Haalt de productie-DATABASE_PUBLIC_URL rechtstreeks van Railway op (niet
    uit .env) zodat er geen risico is dat iemand vergeet .env terug te
    zetten. Faalt hard (geen stille fallback naar lokaal) als dit niet lukt."""
    try:
        result = subprocess.run(
            ["railway", "variables", "--service", "Postgres", "--json"],
            capture_output=True, text=True, timeout=20, check=True,
        )
        production_url = json.loads(result.stdout)["DATABASE_PUBLIC_URL"]
    except Exception as exc:
        raise SystemExit(
            "Kon de productie-database-URL niet ophalen via 'railway variables' "
            f"(is de railway CLI geïnstalleerd en gelinkt aan dit project?): {exc}"
        ) from exc
    config.DATABASE_URL = production_url
    print("Verbonden met: PRODUCTIE-database (Railway) — niet de lokale testdatabase.")


_use_production_database()

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",  # Google voegt dit automatisch toe zodra userinfo.email gevraagd wordt;
    # expliciet vermelden voorkomt een scope-mismatch-fout in oauthlib
]
CLIENT_SECRET_PATH = Path(__file__).resolve().parent.parent / "google_oauth_client_secret.json"

_state: dict = {}


def _slugify(business_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", business_name.lower()).strip("_")
    return slug or "tenant"


class TenantForm(BaseModel):
    business_name: str
    niche: str
    twilio_number: str
    escalation_contact: str = ""
    escalation_email: str = ""
    system_prompt_extra: str = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    await close_pool()


app = FastAPI(title="Lisa — klant onboarden", lifespan=lifespan)

_PAGE_STYLE = """
  body {
    margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center;
    justify-content: center; padding: 2rem 1rem; background: #f9fafb; color: #1f2937;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }
  .card { max-width: 460px; width: 100%; text-align: center; }
  h1 { font-size: 1.3rem; margin-bottom: 0.4rem; }
  p { color: #6b7280; font-size: 0.95rem; line-height: 1.5; }
  form { text-align: left; margin-top: 1.5rem; }
  label { display: block; font-size: 0.85rem; font-weight: 600; margin: 0.9rem 0 0.3rem; }
  input, textarea {
    width: 100%; padding: 0.6rem 0.7rem; border: 1px solid #d1d5db; border-radius: 8px;
    font-size: 0.95rem; box-sizing: border-box; font-family: inherit;
  }
  textarea { min-height: 70px; resize: vertical; }
  button {
    margin-top: 1.5rem; padding: 0.8rem 1.8rem; border-radius: 10px; border: none;
    font-size: 1rem; cursor: pointer; background: #2563eb; color: #fff; font-weight: 600;
    width: 100%;
  }
  button:disabled { opacity: 0.5; cursor: default; }
  .status { margin-top: 1rem; font-size: 0.85rem; color: #9ca3af; }
  .error { color: #dc2626; }
  .skip { display: block; margin-top: 1rem; font-size: 0.85rem; color: #6b7280; text-decoration: underline; cursor: pointer; background: none; border: none; }
  .db-banner { background: #16a34a; color: #fff; font-size: 0.8rem; font-weight: 600; padding: 0.4rem 0.8rem; border-radius: 6px; margin-bottom: 1.2rem; display: inline-block; }
"""


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return f"""<!doctype html>
<html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lisa — klant onboarden</title><style>{_PAGE_STYLE}</style></head>
<body><div class="card" id="app">
  <div class="db-banner">✓ Verbonden met PRODUCTIE-database (Railway)</div>
  <h1>Nieuwe klant onboarden</h1>
  <p>Vul de gegevens van de zaak in. Zodra je op "Klaar" drukt, verschijnt de
  agenda-koppeling — geef de laptop dan aan de klant.</p>
  <form id="tenantForm">
    <label>Bedrijfsnaam</label>
    <input name="business_name" required placeholder="Kapsalon De Vries">
    <label>Niche</label>
    <input name="niche" required placeholder="kapper">
    <label>Twilio-nummer (E.164)</label>
    <input name="twilio_number" required placeholder="+3234000001">
    <label>Escalatiecontact — nummer eigenaar</label>
    <input name="escalation_contact" placeholder="+3247000001">
    <label>Escalatie-e-mail (leeg mag — wordt zo meteen automatisch ingevuld uit het Google-account bij de agenda-koppeling)</label>
    <input name="escalation_email" placeholder="">
    <label>Toon / begroeting / diensten (system_prompt_extra)</label>
    <textarea name="system_prompt_extra" placeholder="Wees warm en informeel. Diensten: knipbeurt, kleuring, wassen."></textarea>
    <button type="submit" id="createBtn">Klaar — ga naar agenda-koppeling</button>
  </form>
  <div class="status" id="status"></div>
</div>
<script>
document.getElementById('tenantForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const btn = document.getElementById('createBtn');
  const status = document.getElementById('status');
  btn.disabled = true;
  status.className = 'status';
  status.textContent = 'Bezig...';
  const data = Object.fromEntries(new FormData(e.target).entries());
  try {{
    const res = await fetch('/create-tenant', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify(data),
    }});
    const result = await res.json();
    if (!res.ok) throw new Error(result.detail || 'onbekende fout');
    showConnectScreen(result.business_name);
  }} catch (err) {{
    status.textContent = 'Fout: ' + err.message;
    status.className = 'status error';
    btn.disabled = false;
  }}
}});

function showConnectScreen(businessName) {{
  document.getElementById('app').innerHTML = `
    <h1>Google Agenda koppelen</h1>
    <p>Voor <b>${{businessName}}</b> — geef de laptop nu aan de klant. Ze loggen in met het
    Google-account waarvan de agenda door Lisa gebruikt moet worden. Er wordt niets
    geïnstalleerd en er wordt geen wachtwoord gedeeld.</p>
    <button id="connectBtn" onclick="startConnect()">Connecteer</button>
    <button class="skip" onclick="skipConnect()">Geen agenda-koppeling nu — later instellen</button>
    <div class="status" id="status2"></div>
  `;
}}

async function startConnect() {{
  const btn = document.getElementById('connectBtn');
  const status = document.getElementById('status2');
  btn.disabled = true;
  status.textContent = 'Bezig — een nieuw tabblad opent voor het Google-inlogscherm...';
  try {{
    const res = await fetch('/connect-calendar', {{ method: 'POST' }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'onbekende fout');
    showCalendarPicker(data.calendars);
  }} catch (err) {{
    status.textContent = 'Fout: ' + err.message;
    status.className = 'status error';
    btn.disabled = false;
  }}
}}

function showCalendarPicker(calendars) {{
  const options = calendars.map(c => `<option value="${{c.id}}">${{c.summary}}${{c.primary ? ' (hoofdagenda)' : ''}}</option>`).join('');
  document.getElementById('app').innerHTML = `
    <h1>Welke agenda?</h1>
    <p>Dit Google-account heeft meerdere agenda's — welke moet Lisa gebruiken voor afspraken?</p>
    <select id="calendarSelect" style="width:100%; padding:0.6rem; border-radius:8px; border:1px solid #d1d5db; font-size:0.95rem;">${{options}}</select>
    <button onclick="finishConnect()">Bevestigen</button>
    <div class="status" id="status3"></div>
  `;
}}

async function finishConnect() {{
  const calendarId = document.getElementById('calendarSelect').value;
  const status = document.getElementById('status3');
  status.textContent = 'Bezig...';
  try {{
    const res = await fetch('/select-calendar', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{calendar_id: calendarId}}),
    }});
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || 'onbekende fout');
    document.getElementById('app').innerHTML = `<h1>Klaar!</h1><p>${{data.business_name}} is volledig onboard: tenant aangemaakt, agenda gekoppeld (${{data.calendar_id}}).</p><p>Test nu zelf met een boeking of annulering voor de klant live gaat.</p>`;
  }} catch (err) {{
    status.textContent = 'Fout: ' + err.message;
    status.className = 'status error';
  }}
}}

function skipConnect() {{
  document.getElementById('app').innerHTML = '<h1>Klaar!</h1><p>Tenant aangemaakt zonder agenda-koppeling. Lisa noteert aanvragen enkel, een medewerker plant handmatig in tot je later alsnog koppelt.</p>';
}}
</script>
</body></html>"""


@app.post("/create-tenant")
async def create_tenant(form: TenantForm) -> dict:
    pool = await get_pool()
    await tenants.init_schema(pool)

    tenant = Tenant(
        client_id=_slugify(form.business_name),
        business_name=form.business_name,
        niche=form.niche,
        twilio_number=form.twilio_number,
        calendar_type="none",
        calendar_config={},
        system_prompt_extra=form.system_prompt_extra,
        escalation_contact=form.escalation_contact,
        escalation_email=form.escalation_email,
    )
    await tenants.upsert_tenant(pool, tenant)
    _state["tenant"] = tenant
    _state["pool"] = pool

    return {"business_name": tenant.business_name}


class CalendarChoice(BaseModel):
    calendar_id: str


@app.post("/connect-calendar")
async def connect_calendar() -> dict:
    """Stap 1: OAuth-login, dan de beschikbare agenda's van dit account
    oplijsten — NOOIT blind 'primary' aannemen, een account kan meerdere
    agenda's hebben (bv. een aparte "Kapsalon Afspraken"-agenda naast de
    persoonlijke hoofdagenda) en de klant moet zelf kunnen kiezen welke."""
    tenant = _state.get("tenant")
    pool = _state.get("pool")
    if tenant is None or pool is None:
        raise HTTPException(status_code=400, detail="Nog geen tenant aangemaakt — vul eerst het formulier in.")
    if not CLIENT_SECRET_PATH.exists():
        raise HTTPException(status_code=500, detail="Ontbreekt: google_oauth_client_secret.json")

    def _run_oauth():
        flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_SECRET_PATH), SCOPES)
        return flow.run_local_server(port=0)

    credentials = await asyncio.to_thread(_run_oauth)
    _state["credentials"] = credentials

    calendar_list = await asyncio.to_thread(
        lambda: build("calendar", "v3", credentials=credentials).calendarList().list().execute()
    )
    calendars = [
        {"id": c["id"], "summary": c.get("summary", c["id"]), "primary": c.get("primary", False)}
        for c in calendar_list.get("items", [])
    ]
    if not calendars:
        raise HTTPException(status_code=500, detail="Geen agenda's gevonden op dit Google-account.")

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

    return {"calendars": calendars}


@app.post("/select-calendar")
async def select_calendar(choice: CalendarChoice) -> dict:
    """Stap 2: de klant koos welke agenda — nu pas definitief opslaan."""
    tenant = _state.get("tenant")
    pool = _state.get("pool")
    credentials = _state.get("credentials")
    if tenant is None or pool is None or credentials is None:
        raise HTTPException(status_code=400, detail="Geen lopende agenda-koppeling — begin opnieuw met 'Connecteer'.")

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
        "calendar_id": choice.calendar_id,
    }
    await tenants.upsert_tenant(pool, tenant)

    return {"business_name": tenant.business_name, "calendar_id": choice.calendar_id}


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8004)
