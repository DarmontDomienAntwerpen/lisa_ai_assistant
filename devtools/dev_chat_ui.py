"""Dev-tool: chat met Lisa in de browser, gekoppeld aan een echte Google Calendar.

Gebruik:
  1. python onboarding/google_oauth_setup.py   (eenmalig, schrijft dev_google_token.json)
  2. python dev_chat_ui.py
  3. open http://localhost:8001

Niet onderdeel van de productiecode — puur om lokaal te testen, net als
dev_chat.py maar dan met een browser-UI en een echte agenda-koppeling
i.p.v. calendar_type="none".
"""
from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from app import conversation_store, customer_lookup, tenants, usage_log
from app.config import close_pool, get_pool
from app.realtime_client import RealtimeConversation
from app.tenants import Tenant

TOKEN_PATH = Path(__file__).resolve().parent.parent / "dev_google_token.json"
TEST_PHONE_NUMBER = "+32499999998"
# Aparte "Lisa test"-kalender in het gekoppelde Google-account, niet de
# persoonlijke hoofdagenda ("primary") van dat account.
CALENDAR_ID = "c24b156dda02f2d3c8e23e8c419dbe1d0324eb46a1f92c4ac1892c56710b9774@group.calendar.google.com"


def _load_tenant() -> Tenant:
    if not TOKEN_PATH.exists():
        raise SystemExit(
            f"Ontbreekt: {TOKEN_PATH} — draai eerst 'python onboarding/google_oauth_setup.py'."
        )
    oauth_credentials = json.loads(TOKEN_PATH.read_text())
    return Tenant(
        client_id="dev_test_google",
        business_name="Kapsalon De Vries",
        niche="kapper",
        twilio_number="+3234000008",
        calendar_type="google_calendar",
        calendar_config={"calendar_id": CALENDAR_ID, "oauth_credentials": oauth_credentials},
        system_prompt_extra="Wees warm en informeel, typisch Vlaams.",
        escalation_contact="+3247000009",
    )


TEST_TENANT = _load_tenant()

_conversation: RealtimeConversation | None = None
_events: asyncio.Queue = asyncio.Queue()


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _conversation
    pool = await get_pool()
    await tenants.init_schema(pool)
    await customer_lookup.init_schema(pool)
    await conversation_store.init_schema(pool)
    await usage_log.init_schema(pool)
    await tenants.upsert_tenant(pool, TEST_TENANT)

    _conversation = RealtimeConversation(TEST_TENANT, pool, TEST_PHONE_NUMBER, "dev-ui", audio=False)
    await _conversation.connect()
    listener = asyncio.create_task(_consume_events())
    yield
    listener.cancel()
    await _conversation.close()
    await close_pool()


async def _consume_events() -> None:
    assert _conversation is not None
    async for event in _conversation.events():
        await _events.put(event)


app = FastAPI(title="Lisa dev-chat (Google Calendar)", lifespan=lifespan)


class ChatRequest(BaseModel):
    message: str


@app.post("/api/chat")
async def chat(req: ChatRequest) -> dict:
    assert _conversation is not None
    await _conversation.send_text(req.message)

    escalated = False
    booking_events = []
    while True:
        event = await _events.get()
        if event["type"] == "assistant_text":
            return {"reply": event["text"], "escalated": escalated, "booking_events": booking_events}
        if event["type"] == "escalated":
            escalated = True
        elif event["type"] == "booking_event":
            booking_events.append(event)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    initial = TEST_TENANT.business_name.strip()[:1].upper() or "L"
    return f"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lisa — dev chat (tekstmodus voor voice-brain)</title>
<style>
  :root {{ color-scheme: light; }}
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #0b141a; font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }}
  .phone {{
    width: 380px; height: 780px; background: #fff; border-radius: 40px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.5); overflow: hidden; display: flex; flex-direction: column;
    border: 10px solid #111;
  }}
  .statusbar {{
    height: 28px; background: #075e54; color: #fff; font-size: 0.7rem;
    display: flex; align-items: center; justify-content: space-between; padding: 0 1.1rem; flex: none;
  }}
  .header {{
    background: #075e54; color: #fff; padding: 0.6rem 0.8rem; display: flex; align-items: center;
    gap: 0.7rem; flex: none; box-shadow: 0 1px 3px rgba(0,0,0,0.3);
  }}
  .header .back {{ font-size: 1.3rem; opacity: 0.9; }}
  .avatar {{
    width: 36px; height: 36px; border-radius: 50%; background: #25d366; color: #fff;
    display: flex; align-items: center; justify-content: center; font-weight: 600; flex: none;
  }}
  .header .info {{ line-height: 1.2; }}
  .header .name {{ font-size: 0.95rem; font-weight: 600; }}
  .header .status {{ font-size: 0.72rem; opacity: 0.85; }}
  .chat {{
    flex: 1; overflow-y: auto; padding: 0.7rem 0.5rem;
    background-color: #e5ddd5;
    background-image:
      linear-gradient(rgba(255,255,255,0.05) 1px, transparent 1px),
      linear-gradient(90deg, rgba(0,0,0,0.02) 1px, transparent 1px);
    background-size: 24px 24px;
  }}
  .row {{ display: flex; margin: 0.15rem 0.3rem; }}
  .row.out {{ justify-content: flex-end; }}
  .row.in {{ justify-content: flex-start; }}
  .row.sys {{ justify-content: center; }}
  .bubble {{
    max-width: 78%; padding: 0.4rem 0.55rem 0.35rem; border-radius: 7.5px; font-size: 0.88rem;
    line-height: 1.35; box-shadow: 0 1px 0.5px rgba(0,0,0,0.13); position: relative; white-space: pre-wrap;
    word-wrap: break-word;
  }}
  .out .bubble {{ background: #dcf8c6; border-top-right-radius: 0; }}
  .in .bubble {{ background: #fff; border-top-left-radius: 0; }}
  .sys .bubble {{
    background: rgba(255,255,255,0.75); color: #667781; font-size: 0.72rem; font-style: italic;
    border-radius: 8px; box-shadow: none;
  }}
  .time {{ display: block; text-align: right; font-size: 0.63rem; color: #667781; margin-top: 1px; }}
  .inputbar {{
    flex: none; display: flex; align-items: center; gap: 0.5rem; padding: 0.45rem 0.5rem;
    background: #f0f0f0;
  }}
  .inputbar input {{
    flex: 1; border: none; border-radius: 20px; padding: 0.6rem 1rem; font-size: 0.9rem; outline: none;
  }}
  .sendbtn {{
    width: 38px; height: 38px; border-radius: 50%; background: #075e54; color: #fff; border: none;
    display: flex; align-items: center; justify-content: center; font-size: 1rem; flex: none; cursor: pointer;
  }}
  .sendbtn:disabled {{ opacity: 0.5; }}
</style>
</head>
<body>
<div class="phone">
  <div class="statusbar"><span>9:41</span><span>●●●●  Wi-Fi  🔋</span></div>
  <div class="header">
    <span class="back">&#8592;</span>
    <div class="avatar">{initial}</div>
    <div class="info">
      <div class="name">{TEST_TENANT.business_name}</div>
      <div class="status" id="status">online</div>
    </div>
  </div>
  <div class="chat" id="log"></div>
  <form class="inputbar" id="form">
    <input id="input" autocomplete="off" placeholder="Typ een bericht" autofocus>
    <button class="sendbtn" type="submit">&#10148;</button>
  </form>
</div>
<script>
const log = document.getElementById('log');
const form = document.getElementById('form');
const input = document.getElementById('input');
const status = document.getElementById('status');

function timeNow() {{
  const d = new Date();
  return d.getHours().toString().padStart(2,'0') + ':' + d.getMinutes().toString().padStart(2,'0');
}}

function addMsg(text, kind) {{
  const row = document.createElement('div');
  row.className = 'row ' + kind;
  const bubble = document.createElement('div');
  bubble.className = 'bubble';
  bubble.textContent = text;
  if (kind !== 'sys') {{
    const time = document.createElement('span');
    time.className = 'time';
    time.textContent = timeNow();
    bubble.appendChild(time);
  }}
  row.appendChild(bubble);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
}}

form.addEventListener('submit', async (e) => {{
  e.preventDefault();
  const message = input.value.trim();
  if (!message) return;
  addMsg(message, 'out');
  input.value = '';
  input.disabled = true;
  status.textContent = 'aan het typen...';
  try {{
    const res = await fetch('/api/chat', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{message}})
    }});
    const data = await res.json();
    addMsg(data.reply, 'in');
    if (data.escalated) addMsg('Escalatie: een medewerker zou nu verwittigd worden', 'sys');
    for (const event of (data.booking_events || [])) {{
      addMsg('Kapper genotificeerd: afspraak ' + event.type, 'sys');
    }}
  }} catch (err) {{
    addMsg('Fout bij versturen: ' + err, 'sys');
  }} finally {{
    status.textContent = 'online';
    input.disabled = false;
    input.focus();
  }}
}});
</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8001)
