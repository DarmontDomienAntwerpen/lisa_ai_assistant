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

## Later (nu expliciet buiten scope)
- **WhatsApp** (gemiste-oproep-template, direct whatsappen naar het zaaknummer) — komt
  terug zodra de voice-flow staat. Geen WhatsApp-code/routes bouwen of onderhouden
  zolang dit hier staat; als er nog restanten in de codebase staan, mogen die uit of
  gemarkeerd als dood totdat dit weer wordt opgepakt.

## Het team (rollen die Claude Code aanneemt tijdens het werk)

Bij elke feature/wijziging denkt Claude Code actief vanuit deze rollen — niet als
decoratie, maar als checklist die je expliciet doorloopt voor je code schrijft en
nadat je test.

| Rol | Bewaakt |
|---|---|
| **Engineering Lead** | Werkende, leesbare, minimale code. Geen overengineering. |
| **Product/QA Lead** | Doet dit wat de klant écht nodig heeft? Schrijft testcases vóór het bouwen. |
| **Security & Compliance Officer** | GDPR, dataretentie, encryptie, opname-beleid gesprekken |
| **SRE / Reliability Engineer** | Wat als Claude API, Twilio, of de agenda-koppeling faalt? Fallbacks, monitoring |
| **Cost/FinOps Analyst** | Tokens per gesprek, model-keuze (Haiku vs Sonnet), caps per klant |
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
├── main.py                  # FastAPI routes: /voice, /call-status
├── agent.py                 # Claude-logica, generieke system prompt, tool-definities
│                             #   NICHE-ONAFHANKELIJK — geen niche-specifieke logica hier
├── tenants.py                # tenant-lookup op basis van binnenkomend Twilio-nummer
├── customer_lookup.py        # nieuw/bestaand-check, roept juiste integration-adapter aan
├── integrations/
│   ├── base.py                # abstracte interface: check_availability(), book(),
│   │                           #   lookup_customer(), create_customer()
│   ├── google_calendar.py     # eerste adapter — live voor eerste klant
│   ├── outlook_calendar.py    # later, zelfde interface
│   ├── generic_rest_api.py    # voor klanten met eigen CRM/booking-systeem
│   └── none.py                # fallback: Lisa noteert alleen, mens plant handmatig in
├── twilio_handler.py          # TwiML voor voice, real-time voice-integratie (tools tijdens call)
├── conversation_store.py      # gesprekshistorie per (tenant, telefoonnummer)
├── usage_log.py               # tokens, kosten, kanaal, escalaties — voor toekomstig dashboard
├── config.py                  # env vars, model-keuzes
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
  "calendar_type": "google_calendar",      # "outlook" | "custom_api" | "none"
  "calendar_config": {...},                # credentials/endpoint, per type verschillend
  "system_prompt_extra": "...",            # niche/bedrijf-specifieke toon en context
  "escalation_contact": "+3247..."
}
```

Elke binnenkomende call wordt eerst gekoppeld aan een tenant via het `To`-nummer
(`tenants.py`). Alles daarna — system prompt, agenda-koppeling, escalatiecontact —
komt uit die tenant-config. Eén codebase, oneindig veel klanten en niches.

## Techstack — niet wijzigen zonder reden
- **Framework:** FastAPI (async, licht — dit is een webhook-laag, geen webapp/dashboard)
- **Hosting:** Railway (git push = deploy, geen server-beheer, schaalt met gebruik)
- **DB:** Postgres (Railway add-on) — tenants, gesprekshistorie, usage_log
- **LLM:** `claude-haiku-4-5-20251001` als standaard voor elk gesprek (snel, goedkoop,
  goed genoeg voor intake/boeking). Escaleer naar `claude-sonnet-5` bij complexe of
  gevoelige gesprekken (klacht, twijfel, expliciete vraag om mens).
- **Telefonie:** Twilio Voice — bestaand zaaknummer, live voice-AI met tool-calling
  tijdens het gesprek
- **Eerste agenda-adapter:** Google Calendar — bouw deze eerst, andere adapters volgen
  dezelfde interface uit `integrations/base.py`

## Niet-onderhandelbaar
1. **Klant-check eerst.** Elk gesprek opent met bepalen: nieuw of bestaand. Geen
   verdere actie (afspraak, dossier ophalen) zonder dit vastgesteld te hebben.
2. **Niche-onafhankelijke kern.** `agent.py` en `main.py` bevatten geen if/else op
   niche. Verschillen tussen bedrijven leven in tenant-config en adapters.
3. **GDPR.** Gespreksdata encrypted, retentietermijn expliciet, geen data naar derde
   partijen zonder noodzaak.
4. **Geen stille faalstand.** Als Claude API, Twilio, of een agenda-adapter faalt:
   klant krijgt altijd een duidelijk bericht, nooit stilte.
5. **Logging vanaf dag 1** — tokens, kosten, kanaal, escalaties, per tenant. Geen
   dashboard nu, wel de data ervoor (toekomstig eigen dashboard voor gebruik/kosten).

## Wat "klaar" betekent
Een feature is pas klaar als: het werkt, het faalt netjes, de kosten zijn bekend,
de toon klopt voor de eindklant, en het is testbaar zonder handmatig door de hele
flow te bellen — en het werkt voor elke tenant, niet alleen de eerste.
