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
`app/agent.py` blijft bestaan als de **niche-onafhankelijke, provider-onafhankelijke
kern**: tool-definities (`TOOLS`, in OpenAI function-calling-vorm), de
system-instructies (`build_voice_instructions`), en de eigenlijke tool-uitvoering
(`execute_tool` — agenda checken/boeken, klant opzoeken/aanmaken, naam-bevestiging
bij bestaande klant). Die business-logica is wat hier getest en bewaakt wordt, niet
de keuze van LLM-provider. `app/realtime_client.py` is de dunne WebSocket-laag naar
OpenAI's Realtime API (sessie opzetten, function-calls doorsturen naar
`agent.execute_tool`, transcript/usage capteren) — gedeeld door zowel
`app/voice_stream.py` (Twilio-audio, productie) als de dev-tools (tekst, lokaal
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
- **Live call-transfer naar escalation_contact.** Bewust NIET gebouwd: een zaak heeft
  vaak maar één telefoonlijn (het zaaknummer, dat al naar Twilio doorschakelt) —
  live terugbellen daarnaartoe komt in een lus terecht, er is geen tweede lijn om
  een medewerker op te krijgen. In plaats daarvan stuurt Lisa bij een escalatie een
  e-mail naar `tenant.escalation_email` (zie `app/escalation_email.py`, getriggerd
  vanuit `app/voice_stream.py`) met klantnaam, nummer, tijdstip en reden — de
  eigenaar belt zelf terug wanneer het uitkomt. `escalation_contact` (telefoonnummer)
  blijft in het datamodel staan voor een eventuele latere, aparte-lijn-oplossing,
  maar wordt vandaag nergens actief gebruikt.
- **Outlook / eigen REST-klantsysteem-adapters** — de interface (`app/integrations/base.py`)
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

Gegroepeerd in 4 mappen naar verantwoordelijkheid: productiecode (`app/`),
het interne dashboard (`dashboard/`), klant-onboarding-stappen (`onboarding/`)
en niet-productie dev-tools (`devtools/`) — los van de automatische testsuite
(`tests/`). Binnen `app/` importeert alles via het volledige pad
(`from app.tenants import ...`, `from app.integrations.base import ...`) —
geen bare-name imports meer, ook niet tussen bestanden onderling in `app/`.

```
lisa/
├── app/                        # productiecode — alles hierin gebruikt `from app.x import y`
│   ├── main.py                   # FastAPI routes: /voice (TwiML) + /media-stream (WebSocket)
│   ├── agent.py                  # niche- en provider-onafhankelijke kern: tool-definities
│   │                             #   (OpenAI function-calling-vorm), system-instructies,
│   │                             #   tool-uitvoering (execute_tool). Geen niche-if/else's.
│   ├── realtime_client.py        # RealtimeConversation: één OpenAI Realtime-sessie —
│   │                             #   sessie opzetten, function-calls naar agent.execute_tool,
│   │                             #   transcript/usage capteren. Gedeeld door voice + dev-tools.
│   ├── voice_stream.py           # dunne brug: Twilio Media Stream audio <-> RealtimeConversation
│   ├── tenants.py                # tenant-lookup op basis van binnenkomend Twilio-nummer
│   ├── customer_lookup.py        # nieuw/bestaand-check, roept juiste integration-adapter aan
│   ├── integrations/
│   │   ├── base.py                 # abstracte interface: check_availability(), book(),
│   │   │                           #   lookup_customer(), create_customer()
│   │   ├── google_calendar.py      # eerste (en vooralsnog enige) adapter — live voor eerste klant
│   │   └── none.py                 # fallback: Lisa noteert alleen, mens plant handmatig in
│   ├── twilio_handler.py         # TwiML voor inkomende oproep: <Connect><Stream> naar /media-stream
│   ├── conversation_store.py     # gesprekshistorie per (tenant, telefoonnummer). Ook GDPR-
│   │                             #   purge-entrypoint: `python -m app.conversation_store`
│   ├── usage_log.py              # tokens, kosten, kanaal, escalaties, calls (aparte call-teller)
│   └── config.py                 # env vars, model-keuzes
├── dashboard/
│   └── router.py                 # intern, read-only: /dashboard — per tenant calls/kosten/
│                                 #   escalaties + transcript-drill-down. HTTP Basic Auth.
├── onboarding/                  # stappen om een nieuwe klant/tenant te boarden
│   ├── onboard_webapp.py         # DE onboarding-flow: één doorlopend lokaal schermpje — jij
│   │                             #   vult de tenant-gegevens in, drukt "Klaar", geeft dan de
│   │                             #   laptop aan de klant voor de agenda-koppeling (Google-
│   │                             #   login), alles in één ononderbroken flow. Schrijft altijd
│   │                             #   naar de productie-database (haalt DATABASE_PUBLIC_URL op
│   │                             #   via de railway CLI, negeert .env)
│   └── google_oauth_setup.py     # eenmalige OAuth-setup voor Domiens EIGEN testagenda (dev) —
│                                 #   los van klant-onboarding, voedt enkel de devtools
├── devtools/                    # geen productiecode: praten in tekst/via microfoon met exact
│   ├── dev_chat.py                #   dezelfde RealtimeConversation als een echte oproep
│   ├── dev_chat_ui.py
│   ├── dev_voice_ui.py
│   └── dev_test_scenarios.py
├── tests/
│   └── test_*.py
├── requirements.txt
├── Procfile                     # web: uvicorn app.main:app ...
└── CLAUDE.md                    # dit bestand
```

Geen extra abstractielagen, geen ORM-zwaargewicht, geen ongebruikte folders.
Elk bestand heeft één duidelijke verantwoordelijkheid. Niche-verschillen zitten
uitsluitend in tenant-config en in de integration-adapter — nooit hardcoded in
`app/agent.py` of `app/main.py`.

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
  "escalation_contact": "+3247...",        # nummer van de eigenaar, nog niet actief gebruikt (zie "Later")
  "escalation_email": "eigenaar@zaak.be"   # waar Lisa naartoe mailt bij een escalatie
}
```

Elke binnenkomende call wordt eerst gekoppeld aan een tenant via het `To`-nummer
(`app/tenants.py`). Alles daarna — system prompt, agenda-koppeling, escalatiecontact —
komt uit die tenant-config. Eén codebase, oneindig veel klanten en niches.

## Techstack — niet wijzigen zonder reden
- **Framework:** FastAPI (async, licht — dit is in essentie een webhook-/WebSocket-laag;
  `dashboard/router.py` is de enige webapp-achtige uitzondering, en blijft bewust minimaal:
  server-rendered HTML, geen frontend-framework)
- **Hosting:** Railway (git push = deploy, geen server-beheer, schaalt met gebruik).
  GDPR-retentiepurge (`conversation_store.purge_expired`, via `python -m
  app.conversation_store`) draait NIET automatisch mee met de `web`-service — Railway's
  cron-schedule is een handmatige stap in de Railway-dashboard (Settings van een
  tweede service die hetzelfde repo gebruikt met start command `python -m
  app.conversation_store`, dagelijks). Niet iets dat via een config-bestand alleen
  op te zetten is; controleer dit expliciet bij het opzetten van een nieuw project.
- **DB:** Postgres (Railway add-on) — tenants, gesprekshistorie, usage_log, calls
- **LLM:** OpenAI Realtime API (`gpt-realtime`) — voert het hele
  gesprek: spraak, beslissen, tool-calling. Geen apart brain-model — zie
  architectuursectie hierboven. Claude/Anthropic is niet in gebruik zolang voice het
  enige kanaal is.
- **Telefonie:** Twilio Voice — bestaand zaaknummer, inkomende oproep wordt via Media
  Streams (WebSocket) doorgestuurd naar OpenAI Realtime
- **Eerste agenda-adapter:** Google Calendar — enige adapter vandaag, andere volgen
  dezelfde interface uit `app/integrations/base.py` zodra een klant het nodig heeft

## Niet-onderhandelbaar
1. **Klant-check eerst.** Elk gesprek opent met bepalen: nieuw of bestaand. Geen
   verdere actie (afspraak, dossier ophalen) zonder dit vastgesteld te hebben.
2. **Niche-onafhankelijke kern.** `app/agent.py` en `app/main.py` bevatten geen if/else op
   niche. Verschillen tussen bedrijven leven in tenant-config en adapters.
3. **GDPR.** Gespreksdata encrypted, retentietermijn expliciet, geen data naar derde
   partijen zonder noodzaak.
4. **Geen stille faalstand.** Als OpenAI Realtime API, Twilio, of een agenda-adapter
   faalt: klant krijgt altijd een duidelijk (gesproken) bericht, nooit stilte of een
   opgehangen lijn zonder uitleg. Dit geldt OOK als Railway/de hele `web`-service zelf
   plat ligt — daarom heeft élke tenant een Twilio **Voice Fallback URL** ingesteld op
   een Twilio-gehoste TwiML Bin (nooit een eigen Railway-URL, want die kan net zo goed
   down zijn): als het primaire `/voice`-webhook faalt/timeout't, belt Twilio zelf naar
   `escalation_contact` door — zie "Klant onboarden" stap 1b.
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
1b. **Fallback bij uitval** (niet overslaan — anders valt een oproep stil bij een
    crash van onze eigen service): maak in de Twilio Console een **TwiML Bin** aan
    (Explore Products → TwiML Bins) met:
    ```xml
    <Response>
        <Say language="nl-NL">Onze telefonische assistent is momenteel tijdelijk niet
        bereikbaar. We verbinden je door.</Say>
        <Dial>+32... (escalation_contact van deze tenant, NOOIT het zaaknummer zelf)</Dial>
    </Response>
    ```
    Zet daarna bij het Twilio-nummer, onder "A call comes in", de **Voice Fallback URL**
    op de URL van deze TwiML Bin (methode POST). Twilio host dit zelf, dus dit blijft
    werken zelfs als Railway volledig onbereikbaar is — precies het scenario waarvoor
    dit dient.
2+3. **Tenant aanmaken + agenda-koppeling, in één flow**: `python
   onboarding/onboard_webapp.py`, open `http://localhost:8004` — vul zelf de
   bedrijfsgegevens in (business_name, niche, twilio_number, system_prompt_extra
   met de info die Lisa aan klanten mag geven: uren, prijzen, adres, beleid,
   escalation_contact), druk op "Klaar", en geef dan de laptop aan de klant: die
   klikt enkel nog "Connecteer", logt in met hun eigen Google-account (geen
   wachtwoord gedeeld, niets te installeren) — `calendar_config` wordt automatisch
   ingevuld, net als `escalation_email` als dat veld leeg gelaten werd. Een "geen
   agenda-koppeling nu"-optie staat er ook op (blijft dan `calendar_type="none"`,
   Lisa noteert enkel, mens plant handmatig in). Voor een Workspace-agenda met een
   service-account (zie `app/integrations/google_calendar.py`) bestaat nog geen
   stap in `onboard_webapp.py` — bouw dat pas als een klant het nodig heeft (nu
   enkel mogelijk via een handmatige database-update).
4. **Testen**: bel het nieuwe nummer zelf, loop minstens een boeking, annulering en
   escalatie-scenario door voor de klant live gaat (zie CLAUDE.md "Wat 'klaar' betekent").
5. **Opvolgen**: `/dashboard` toont vanaf de eerste call calls/kosten/escalaties voor
   deze tenant — geen aparte setup nodig, dat loopt automatisch mee.

Niet-onderhandelbaar: nooit een niche-specifieke if/else toevoegen aan `app/agent.py` of
`app/main.py` om een nieuwe klant te onboarden — alle verschillen horen in tenant-config,
`system_prompt_extra`, of een nieuwe integration-adapter.

## Wat "klaar" betekent
Een feature is pas klaar als: het werkt, het faalt netjes, de kosten zijn bekend,
de toon klopt voor de eindklant, en het is testbaar zonder handmatig door de hele
flow te bellen — en het werkt voor elke tenant, niet alleen de eerste.
