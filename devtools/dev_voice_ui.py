"""Dev-tool: praat met Lisa via je eigen microfoon in de browser, zonder Twilio
of een echte telefoonoproep — puur om de stem/latency/tool-calling te horen.

Gebruik:
  python dev_voice_ui.py
  open http://localhost:8002, klik "Start gesprek", geef microfoon-toegang

Gebruikt dezelfde Google Calendar-testagenda als dev_chat_ui.py (via
dev_google_token.json — draai eerst onboarding/google_oauth_setup.py als dat
bestand ontbreekt). Zelfde RealtimeConversation-kern (realtime_client.py)
als een echte oproep, enkel audio_format="pcm16" i.p.v. Twilio's
"g711_ulaw" — browsers spreken van nature PCM, geen telefonie-codec. Niet
onderdeel van de productiecode.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from contextlib import asynccontextmanager
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

from app import conversation_store, customer_lookup, tenants, usage_log
from app.config import close_pool, get_pool
from app.realtime_client import RealtimeConversation
from app.tenants import Tenant

logger = logging.getLogger("lisa")

TOKEN_PATH = Path(__file__).resolve().parent.parent / "dev_google_token.json"
# Dezelfde "Lisa test"-kalender als dev_chat_ui.py.
CALENDAR_ID = "c24b156dda02f2d3c8e23e8c419dbe1d0324eb46a1f92c4ac1892c56710b9774@group.calendar.google.com"


def _load_tenant() -> Tenant:
    if not TOKEN_PATH.exists():
        raise SystemExit(
            f"Ontbreekt: {TOKEN_PATH} — draai eerst 'python onboarding/google_oauth_setup.py'."
        )
    import json as _json

    oauth_credentials = _json.loads(TOKEN_PATH.read_text())
    return Tenant(
        client_id="dev_test_mic",
        business_name="Kapsalon De Vries",
        niche="kapper",
        twilio_number="+3234000010",
        calendar_type="google_calendar",
        calendar_config={"calendar_id": CALENDAR_ID, "oauth_credentials": oauth_credentials},
        system_prompt_extra="Wees warm en informeel, typisch Vlaams.",
        escalation_contact="+3247000009",
    )


TEST_TENANT = _load_tenant()
TEST_PHONE_NUMBER = "+32499999997"

@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = await get_pool()
    await tenants.init_schema(pool)
    await customer_lookup.init_schema(pool)
    await conversation_store.init_schema(pool)
    await usage_log.init_schema(pool)
    await tenants.upsert_tenant(pool, TEST_TENANT)
    yield
    await close_pool()


app = FastAPI(title="Lisa dev-voice (microfoon)", lifespan=lifespan)


@app.websocket("/ws")
async def voice_ws(websocket: WebSocket) -> None:
    await websocket.accept()
    pool = await get_pool()

    conversation = RealtimeConversation(
        TEST_TENANT, pool, TEST_PHONE_NUMBER, "dev-mic", audio=True, audio_format="pcm16"
    )
    await conversation.connect()

    async def relay_to_browser() -> None:
        async for event in conversation.events():
            if event["type"] == "audio_delta":
                await websocket.send_bytes(base64.b64decode(event["payload"]))
            else:
                await websocket.send_text(json.dumps(event))

    relay_task = asyncio.create_task(relay_to_browser())
    await conversation.start()

    try:
        while True:
            chunk = await websocket.receive_bytes()
            await conversation.send_audio_chunk(base64.b64encode(chunk).decode())
    except WebSocketDisconnect:
        pass
    finally:
        relay_task.cancel()
        await conversation.close()


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return f"""<!doctype html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Lisa — dev voice (microfoon)</title>
<style>
  body {{
    margin: 0; min-height: 100vh; display: flex; flex-direction: column; align-items: center;
    padding: 2rem 1rem; background: #111827; color: #e5e7eb;
    font-family: -apple-system, "Segoe UI", Helvetica, Arial, sans-serif;
  }}
  h1 {{ font-size: 1.1rem; font-weight: 600; color: #f9fafb; }}
  .controls {{ display: flex; gap: 0.6rem; margin: 0.8rem 0; }}
  button {{
    padding: 0.6rem 1.2rem; border-radius: 8px; border: none; font-size: 0.95rem;
    cursor: pointer; background: #2563eb; color: #fff;
  }}
  button:disabled {{ opacity: 0.4; cursor: default; }}
  #status {{ font-size: 0.85rem; color: #9ca3af; margin-bottom: 1rem; }}
  #log {{
    width: 100%; max-width: 480px; display: flex; flex-direction: column; gap: 0.4rem;
  }}
  .row {{ padding: 0.5rem 0.7rem; border-radius: 8px; font-size: 0.9rem; line-height: 1.35; }}
  .row.in {{ background: #1f2937; align-self: flex-start; }}
  .row.out {{ background: #1e3a8a; align-self: flex-end; }}
  .row.sys {{ background: transparent; color: #9ca3af; font-style: italic; font-size: 0.8rem; text-align: center; }}
</style>
</head>
<body>
<h1>Lisa — {TEST_TENANT.business_name} (dev voice-test)</h1>
<div class="controls">
  <button id="startBtn">Start gesprek</button>
  <button id="stopBtn" disabled>Stop</button>
</div>
<div id="status">niet verbonden</div>
<div id="log"></div>
<script>
let ctx, ws, source, processor, muteGain;
let nextPlayTime = 0;
let micMuted = false;
let drainCheckTimer = null;
let activeSources = [];  // geplande AudioBufferSourceNodes, voor interrupt-cleanup

function addLog(text, kind) {{
  const div = document.createElement('div');
  div.className = 'row ' + kind;
  div.textContent = text;
  document.getElementById('log').appendChild(div);
  div.scrollIntoView({{block: 'end'}});
}}

function floatTo16BitPCM(input) {{
  const output = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {{
    const s = Math.max(-1, Math.min(1, input[i]));
    output[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }}
  return output;
}}

function playPCM16(arrayBuffer) {{
  const int16 = new Int16Array(arrayBuffer);
  const float32 = new Float32Array(int16.length);
  for (let i = 0; i < int16.length; i++) float32[i] = int16[i] / 32768;
  const buffer = ctx.createBuffer(1, float32.length, 24000);
  buffer.copyToChannel(float32, 0);
  const src = ctx.createBufferSource();
  src.buffer = buffer;
  src.connect(ctx.destination);
  const startAt = Math.max(ctx.currentTime, nextPlayTime);
  src.start(startAt);
  nextPlayTime = startAt + buffer.duration;
  activeSources.push(src);
  src.onended = () => {{ activeSources = activeSources.filter(s => s !== src); }};

  // Half-duplex: zonder koptelefoon hoort de mic Lisa's eigen stem uit de
  // speakers en stuurt dat terug als "klant spreekt" — een feedback-lus die
  // haar laat "razen". Mic blijft dicht tot haar audio-wachtrij leeg is
  // (plus een korte marge voor de akoestische staart/nagalm).
  micMuted = true;
  if (drainCheckTimer) clearInterval(drainCheckTimer);
  drainCheckTimer = setInterval(() => {{
    if (ctx.currentTime >= nextPlayTime + 0.25) {{
      micMuted = false;
      clearInterval(drainCheckTimer);
      drainCheckTimer = null;
    }}
  }}, 100);
}}

function handleInterrupt() {{
  // De klant onderbrak Lisa (bv. via de echte Twilio-lijn, of een gaatje in
  // de half-duplex mute hierboven): stop meteen alle nog geplande/spelende
  // audio, zodat de staart van het oude antwoord niet doorspeelt bovenop het
  // nieuwe — anders klinkt dat als een stem die abrupt verandert.
  for (const src of activeSources) {{
    try {{ src.stop(); }} catch (e) {{ /* al gestopt/afgelopen, negeren */ }}
  }}
  activeSources = [];
  nextPlayTime = ctx.currentTime;
  if (drainCheckTimer) {{ clearInterval(drainCheckTimer); drainCheckTimer = null; }}
  micMuted = false;
}}

async function start() {{
  ctx = new (window.AudioContext || window.webkitAudioContext)({{ sampleRate: 24000 }});
  nextPlayTime = 0;

  const stream = await navigator.mediaDevices.getUserMedia({{
    audio: {{ echoCancellation: true, noiseSuppression: true, autoGainControl: true }},
  }});
  source = ctx.createMediaStreamSource(stream);
  processor = ctx.createScriptProcessor(4096, 1, 1);
  muteGain = ctx.createGain();
  muteGain.gain.value = 0;  // enkel om de processor-graph actief te houden, geen echo
  source.connect(processor);
  processor.connect(muteGain);
  muteGain.connect(ctx.destination);

  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  ws = new WebSocket(proto + '//' + location.host + '/ws');
  ws.binaryType = 'arraybuffer';

  ws.onopen = () => {{ document.getElementById('status').textContent = 'verbonden — Lisa luistert'; }};
  ws.onclose = () => {{ document.getElementById('status').textContent = 'verbroken'; }};

  ws.onmessage = (event) => {{
    if (event.data instanceof ArrayBuffer) {{
      playPCM16(event.data);
      return;
    }}
    const msg = JSON.parse(event.data);
    if (msg.type === 'user_transcript') addLog('Jij: ' + msg.text, 'in');
    else if (msg.type === 'assistant_text') addLog('Lisa: ' + msg.text, 'out');
    else if (msg.type === 'escalated') addLog('[escalatie: een medewerker zou nu verwittigd worden]', 'sys');
    else if (msg.type === 'booking_event') addLog('[kapper genotificeerd: afspraak ' + msg.status + ']', 'sys');
    else if (msg.type === 'error') addLog('[fout: ' + JSON.stringify(msg.error) + ']', 'sys');
    else if (msg.type === 'interrupted') handleInterrupt();
  }};

  processor.onaudioprocess = (e) => {{
    if (!ws || ws.readyState !== WebSocket.OPEN || micMuted) return;
    const pcm16 = floatTo16BitPCM(e.inputBuffer.getChannelData(0));
    ws.send(pcm16.buffer);
  }};

  document.getElementById('startBtn').disabled = true;
  document.getElementById('stopBtn').disabled = false;
}}

function stop() {{
  if (processor) processor.disconnect();
  if (source) source.disconnect();
  if (ws) ws.close();
  if (ctx) ctx.close();
  document.getElementById('startBtn').disabled = false;
  document.getElementById('stopBtn').disabled = true;
  document.getElementById('status').textContent = 'gestopt';
}}

document.getElementById('startBtn').addEventListener('click', start);
document.getElementById('stopBtn').addEventListener('click', stop);
</script>
</body>
</html>"""


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8002)
