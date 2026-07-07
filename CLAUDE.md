# Lisa — AI Secretary (Darmont Digital)

## Missie
Dit is een **commercieel product**, geen prototype. Elke regel code moet bijdragen aan
een werkend, verkoopbaar, betrouwbaar systeem dat SME's geld bespaart en waar wij geld
mee verdienen. Bij twijfel: kies de robuuste, simpele oplossing boven de knappe.

## Wat het systeem doet
- Klant belt het bestaande zaaknummer van een aangesloten bedrijf → Lisa neemt de
  oproep meteen aan als AI voice-assistent en voert het gesprek live, met tools
  (agenda checken/boeken, klant opzoeken/aanmaken) die ze tijdens het gesprek aanroept
- Lisa checkt **altijd eerst**: nieuwe of bestaande klant?
  - **Bestaande klant** → koppeling met agenda/klantsysteem, haalt context/dossier op
  - **Nieuwe klant** → intake-flow, nieuwe klant aanmaken in gekoppeld systeem
- Escalatie naar mens bij klachten, complexe zaken, of onzekerheid
- **Multi-business / multi-niche vanaf dag 1**: elk aangesloten bedrijf ("zaak") is een
  eigen tenant met eigen nummer, eigen niche, eigen agenda-koppeling en eigen toon.
  Niche nog niet definitief gekozen — architectuur mag daar niet van afhangen.

## Architectuur van het voice-gesprek — één systeem
**OpenAI Realtime API (`gpt-realtime`) voert het volledige
gesprek**: spraak-in, beslissen, tools aanroepen (agenda/klantsysteem), en
spraak-uit, allemaal in één low-latency sessie over de Twilio Media Stream.
Geen los "brain"-model erachter — Claude/Anthropic zit niet in het voice-pad.

Concreet, per gespreksbeurt:
```
Twilio Media Stream → audio-in → OpenAI Realtime API
   → beslist zelf + roept tools aan (agent.execute_tool: agenda, klantsysteem)
   → audio-uit → Twilio Media Stream → klant hoort Lisa
```
`agent.py` blijft bestaan als de **niche-onafhankelijke, provider-onafhankelijke
kern**: tool-definities (`TOOLS`, in OpenAI function-calling-vorm), de
system-instructies (`build_voice_instructions`), en de eigenlijke tool-uitvoering
(`execute_tool` — agenda checken/boeken, klant opzoeken/aanmaken, naam-bevestiging
bij bestaande klant). Die business-logica is wat hier getest en bewaakt wordt, niet
de keuze van LLM-provider. `realtime_client.py` is de dunne WebSocket-laag naar
OpenAI's Realtime API (sessie opzetten, function-calls doorsturen naar
`agent.execute_tool`, transcript/usage capteren) — gedeeld door zowel
`voice_stream.py` (Twilio-audio, productie) als de dev-tools (tekst, lokaal
testen), zodat dev-testen exact dezelfde brain/tool-laag raakt als een echte
oproep.

**Waarom geen Claude meer in dit pad:** twee LLM's in serie (audio-laag + apart
brain-model) stapelt drie netwerk-rondrittes per beurt (transcript ophalen →
apart model bevragen → tekst terug naar TTS) en dwingt lelijke workarounds af om
het audio-model "stil" te houden. Eén systeem is sneller en simpeler. Claude kan
terugkomen voor een toekomstig tekst-kanaal of complexere escalatie-redenering —
zie "Later" hieronder — maar niet zolang voice het enige kanaal is.

## Later (nu expliciet buiten scope)
- **WhatsApp** (gemiste-oproep-template, direct whatsappen naar het zaaknummer) — komt
  terug zodra de voice-flow staat. Geen WhatsApp-code/routes bouwen of onderhouden
  zolang dit hier staat; als er nog restanten in de codebase staan, mogen die uit of
  gemarkeerd als dood totdat dit weer wordt opgepakt.
- **Claude/Anthropic als brain** — enkel relevant zodra er een tweede kanaal bijkomt
  (bv. tekst) of complexere escalatie-redenering nodig is. Niet toevoegen zolang voice
  het enige kanaal is en OpenAI Realtime het gesprek zelf goed genoeg voert.
- **Live call-transfer naar escalation_contact.** Vandaag hoort de klant enkel dat een
  medewerker overneemt (uitgesproken door Lisa) en wordt de escalatie gelogd
  (`usage_log`). Een echte warme overdracht van het live gesprek naar de mens (via
  Twilio's call-redirect) is een waardevolle volgende stap, geen dag-1 vereiste.
- **Outlook / eigen REST-klantsysteem-adapters** — de interface (`integrations/base.py`)
  ondersteunt dit al, maar er is nu geen tenant die het gebruikt. Bouw pas als een
  klant het nodig heeft.

## Het team (rollen die Claude Code aanneemt tijdens het werk)

Bij elke feature/wijziging denkt Claude Code actief vanuit deze rollen — niet als
decoratie, maar als checklist die je expliciet doorloopt voor je code schrijft en
nadat je test.

| Rol | Bewaakt |
|---|---|
| **Engineering Lead** | Werkende, leesbare, minimale code. Geen overengineering. |
| **Product/QA Lead** | Doet dit wat de klant écht nodig heeft? Schrijft testcases vóór het bouwen. |
| **Security & Compliance Officer** | GDPR, dataretentie, encryptie, opname-beleid gesprekken |
| **SRE / Reliability Engineer** | Wat als OpenAI Realtime, Twilio, of de agenda-koppeling faalt? Fallbacks, monitoring |
| **Cost/FinOps Analyst** | Audio-/tekst-tokens per gesprek, gespreksduur, caps per klant |
| **Customer Success** | Stem van de eindklant — is de toon goed, is er een duidelijke escape naar mens |
| **Sales/Pricing Strategist** | Is dit een feature die een SME-eigenaar de waarde meteen ziet? |
| **Marketing** | Sluit dit aan bij de Darmont Digital belofte en haakje-methodiek? |

## De loop — verplicht bij elke feature

```
1. DENKEN     → Product/QA + Sales bepalen scope en "goed genoeg". Testcases eerst.
2. MAKEN      → Engineering bouwt. Security checkt ontwerpkeuzes mee, niet achteraf.
3. TESTEN     → QA's testcases + Cost Analyst (tokenverbruik) + SRE (failure modes)
4. DEBUGGEN   → Engineering lost op
5. TESTEN     → opnieuw, tot alle rollen akkoord zijn
```
Geen feature is "af" zonder deze loop volledig doorlopen te hebben. Rapporteer kort
per rol wat gecheckt is, niet uitgebreid — dit is een werkdocument, geen rapport.

## Projectstructuur (minimaal, robuust, geen groei-rommel)

```
lisa/
├── main.py                  # FastAPI routes: /voice (TwiML) + /media-stream (WebSocket)
├── agent.py                 # niche- en provider-onafhankelijke kern: tool-definities
│                             #   (OpenAI function-calling-vorm), system-instructies,
│                             #   tool-uitvoering (execute_tool). Geen niche-if/else's.
├── realtime_client.py        # RealtimeConversation: één OpenAI Realtime-sessie —
│                             #   sessie opzetten, function-calls naar agent.execute_tool,
│                             #   transcript/usage capteren. Gedeeld door voice + dev-tools.
├── voice_stream.py           # dunne brug: Twilio Media Stream audio <-> RealtimeConversation
├── tenants.py                # tenant-lookup op basis van binnenkomend Twilio-nummer
├── customer_lookup.py        # nieuw/bestaand-check, roept juiste integration-adapter aan
├── integrations/
│   ├── base.py                # abstracte interface: check_availability(), book(),
│   │                           #   lookup_customer(), create_customer()
│   ├── google_calendar.py     # eerste (en vooralsnog enige) adapter — live voor eerste klant
│   └── none.py                # fallback: Lisa noteert alleen, mens plant handmatig in
├── twilio_handler.py          # TwiML voor inkomende oproep: <Connect><Stream> naar /media-stream
├── conversation_store.py      # gesprekshistorie per (tenant, telefoonnummer)
├── usage_log.py               # tokens, kosten, kanaal, escalaties, calls (aparte call-teller)
├── dashboard.py               # intern, read-only: /dashboard — per tenant calls/kosten/
│                             #   escalaties + transcript-drill-down. HTTP Basic Auth.
├── config.py                  # env vars, model-keuzes
├── dev_chat.py, dev_chat_ui.py, dev_voice_ui.py, dev_test_scenarios.py
│                             # dev-tools, geen productiecode: praten in tekst/via microfoon
│                             #   met exact dezelfde RealtimeConversation als een echte oproep
├── scripts/
│   ├── google_oauth_setup.py  # eenmalige OAuth-setup voor lokaal testen
│   └── onboard_tenant.py      # interactief: nieuwe klant toevoegen (zie "Klant onboarden")
├── tests/
│   └── test_*.py
├── requirements.txt
├── Procfile
└── CLAUDE.md                  # dit bestand
```

Geen extra abstractielagen, geen ORM-zwaargewicht, geen ongebruikte folders.
Elk bestand heeft één duidelijke verantwoordelijkheid. Niche-verschillen zitten
uitsluitend in tenant-config en in de integration-adapter — nooit hardcoded in
`agent.py` of `main.py`.

## Datamodel — tenants (elke aangesloten "zaak")

```python
{
  "client_id": "kapper_devries",          # unieke sleutel
  "business_name": "Kapsalon De Vries",
  "niche": "kapper",                       # vrij veld, groeit mee met klantenbestand
  "twilio_number": "+3234...",             # eigen belnummer per zaak
  "calendar_type": "google_calendar",      # of "none" — andere adapters komen pas als een klant ze nodig heeft
  "calendar_config": {...},                # credentials/endpoint, per type verschillend
  "system_prompt_extra": "...",            # niche/bedrijf-specifieke toon en context
  "escalation_contact": "+3247..."
}
```

Elke binnenkomende call wordt eerst gekoppeld aan een tenant via het `To`-nummer
(`tenants.py`). Alles daarna — system prompt, agenda-koppeling, escalatiecontact —
komt uit die tenant-config. Eén codebase, oneindig veel klanten en niches.

## Techstack — niet wijzigen zonder reden
- **Framework:** FastAPI (async, licht — dit is in essentie een webhook-/WebSocket-laag;
  `dashboard.py` is de enige webapp-achtige uitzondering, en blijft bewust minimaal:
  server-rendered HTML, geen frontend-framework)
- **Hosting:** Railway (git push = deploy, geen server-beheer, schaalt met gebruik).
  GDPR-retentiepurge (`conversation_store.purge_expired`, via `python
  conversation_store.py`) draait NIET automatisch mee met de `web`-service — Railway's
  cron-schedule is een handmatige stap in de Railway-dashboard (Settings van een
  tweede service die hetzelfde repo gebruikt met start command `python
  conversation_store.py`, dagelijks). Niet iets dat via een config-bestand alleen
  op te zetten is; controleer dit expliciet bij het opzetten van een nieuw project.
- **DB:** Postgres (Railway add-on) — tenants, gesprekshistorie, usage_log, calls
- **LLM:** OpenAI Realtime API (`gpt-realtime`) — voert het hele
  gesprek: spraak, beslissen, tool-calling. Geen apart brain-model — zie
  architectuursectie hierboven. Claude/Anthropic is niet in gebruik zolang voice het
  enige kanaal is.
- **Telefonie:** Twilio Voice — bestaand zaaknummer, inkomende oproep wordt via Media
  Streams (WebSocket) doorgestuurd naar OpenAI Realtime
- **Eerste agenda-adapter:** Google Calendar — enige adapter vandaag, andere volgen
  dezelfde interface uit `integrations/base.py` zodra een klant het nodig heeft

## Niet-onderhandelbaar
1. **Klant-check eerst.** Elk gesprek opent met bepalen: nieuw of bestaand. Geen
   verdere actie (afspraak, dossier ophalen) zonder dit vastgesteld te hebben.
2. **Niche-onafhankelijke kern.** `agent.py` en `main.py` bevatten geen if/else op
   niche. Verschillen tussen bedrijven leven in tenant-config en adapters.
3. **GDPR.** Gespreksdata encrypted, retentietermijn expliciet, geen data naar derde
   partijen zonder noodzaak.
4. **Geen stille faalstand.** Als OpenAI Realtime API, Twilio, of een agenda-adapter
   faalt: klant krijgt altijd een duidelijk (gesproken) bericht, nooit stilte of een
   opgehangen lijn zonder uitleg.
5. **Logging vanaf dag 1** — tokens, kosten, kanaal, escalaties, calls, per tenant.
   Zichtbaar via `/dashboard` (intern, HTTP Basic Auth — zie Techstack).

## Klant onboarden (nieuwe tenant toevoegen)

1. **Twilio-nummer**: koop/wijs een nummer toe voor deze zaak in de Twilio console.
   Zet de "A call comes in" webhook op `POST https://<railway-domein>/voice`.
   **De klant houdt zijn bestaand zaaknummer** — dit Twilio-nummer is enkel intern
   (tenants.py herkent de zaak via dit nummer). Twee manieren om het bestaande
   nummer te laten binnenkomen op dit Twilio-nummer:
   - **Doorschakeling (aanbevolen, snel):** de zaak zet "onvoorwaardelijk
     doorschakelen" van hun bestaande nummer naar dit Twilio-nummer, via hun eigen
     telecomprovider. Klanten bellen het vertrouwde nummer, merken niets. Meestal
     direct in te stellen, mogelijk een kleine doorschakelkost per gesprek.
   - **Nummerportering (trager, definitief):** het bestaande nummer verhuist
     volledig naar Twilio — dan is er geen apart nummer meer nodig. Duurt dagen
     tot weken via de huidige operator.
2. **Agenda-koppeling**: vandaag enkel Google Calendar — draai
   `python scripts/google_oauth_setup.py` (of gebruik een service-account voor een
   Workspace-agenda) om `calendar_config` te genereren. Zonder agenda-koppeling kan
   `calendar_type="none"` (Lisa noteert enkel, mens plant handmatig in).
3. **Tenant aanmaken**: `python scripts/onboard_tenant.py` — interactief script dat
   `tenants.upsert_tenant()` aanroept met business_name, niche, twilio_number,
   calendar_config, system_prompt_extra (toon/begroeting/diensten van deze zaak) en
   escalation_contact.
4. **Testen**: bel het nieuwe nummer zelf, loop minstens een boeking, annulering en
   escalatie-scenario door voor de klant live gaat (zie CLAUDE.md "Wat 'klaar' betekent").
5. **Opvolgen**: `/dashboard` toont vanaf de eerste call calls/kosten/escalaties voor
   deze tenant — geen aparte setup nodig, dat loopt automatisch mee.

Niet-onderhandelbaar: nooit een niche-specifieke if/else toevoegen aan `agent.py` of
`main.py` om een nieuwe klant te onboarden — alle verschillen horen in tenant-config,
`system_prompt_extra`, of een nieuwe integration-adapter.

## Wat "klaar" betekent
Een feature is pas klaar als: het werkt, het faalt netjes, de kosten zijn bekend,
de toon klopt voor de eindklant, en het is testbaar zonder handmatig door de hele
flow te bellen — en het werkt voor elke tenant, niet alleen de eerste.
