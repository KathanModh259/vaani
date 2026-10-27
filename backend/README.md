# Vaani backend

AI phone receptionist for Indian small businesses. It answers calls on a
Twilio number, answers questions from the business's own settings, books into
a real calendar, and hands callers to a human when needed. The same FastAPI
app serves the marketing site, the dashboard and the browser demo.

Running and deploying it: see [`../DEPLOYMENT.md`](../DEPLOYMENT.md).

## How a call flows

```
Twilio number ──► POST /api/voice/incoming      route by dialed number → business
                     │                           greet (+ AI disclosure), <Gather> speech
                     ▼
                  POST /api/voice/respond        one caller utterance
                     │
                     ├─ services/call_session.run_turn
                     │     load BusinessProfile (hours, services, FAQs)
                     │     load this call's transcript = the conversation memory
                     │     agent.handle_turn ──► LLM + tools (or deterministic flow) ──► booking engine
                     │     append caller line, action events, reply
                     ▼
                  TwiML: <Gather> again │ <Dial> staff │ <Hangup/>
```

The browser demo (`POST /api/chat`) runs the same `run_turn`, with
`channel="web"`.

## Where things live

| Path | What it does |
|---|---|
| `api/routes/voice.py` | Twilio webhooks, signature verification, TwiML |
| `api/routes/chat.py` | Web chat. History is read from the server, never from the client |
| `api/routes/business.py` | Owner settings: profile, hours, services, FAQs, holidays, appointments |
| `api/routes/dashboard.py` | Call stats and transcripts, bucketed in the business's time zone |
| `services/call_session.py` | One conversational turn, end to end |
| `services/business_profile.py` | The single source of facts the receptionist may state |
| `services/booking.py` | Availability, booking with a row lock and overlap re-check, cancel and reschedule |
| `services/escalation.py` | Policy for live transfer vs. taking a message |
| `services/call_logger.py` | Call logs: encrypted transcript, sticky outcome, demo clinic seed |
| `services/notify.py` | SMS booking confirmations (best-effort, background) |
| `llm_agent/agent.py` | Runs the turn: LLM with tools, then the deterministic booking flow, then the rules engine |
| `llm_agent/tools.py` | Tools the LLM can call. The **only** way anything happens |
| `llm_agent/providers.py` | Which open-weight provider answers, and whether its weights are open |
| `llm_agent/llm_provider.py` | Prompt, tool loop, provider failover, guard against claiming a booking that doesn't exist |
| `llm_agent/booking_flow.py` | Booking with no model at all: slot filling over the same tools |
| `db/__init__.py` | SQLAlchemy models, one shared engine |
| `db/migrate.py`, `alembic/` | Migrations (run automatically at boot) |
| `api/routes/relay.py` | ConversationRelay WebSocket: streaming, low-latency calls (`VOICE_MODE=relay`) |
| `api/routes/auth.py` | Sessions (httpOnly cookies or Bearer), reset, verification, CSRF check |
| `api/routes/account.py` | Data export and account deletion |
| `api/routes/numbers.py`, `services/provisioning.py` | Self-serve Twilio numbers |
| `api/routes/integrations.py` | Google Calendar OAuth, website import, owner test calls |
| `api/core/pages.py` | HTML serving with per-request CSP nonces |
| `services/owner_alerts.py`, `services/email.py` | Booking, callback and usage emails to owners |
| `services/notify.py`, `services/reminders.py` | SMS/WhatsApp confirmations and day-before reminders |
| `services/gcal.py` | Google Calendar busy time and event mirroring |
| `services/site_import.py` | Draft a profile from a website (SSRF-guarded fetch) |
| `services/usage.py`, `services/retention.py` | Minute allowances and alerts; transcript retention |
| `services/language.py` | Per-turn voice/recognition language from the reply's script |
| `scripts/assign_number.py` | Attach a Twilio number to a client's business |
| `scripts/seed_plans.py` | Pricing plans (also seeded automatically on an empty database) |
| `scripts/synthetic_check.py` | Production canary: holds a real conversation, fails loudly |
| `evals/run.py` | Scripted caller conversations against the real model |
| `stt/`, `tts/` | Speech engines for the browser demo (Sarvam, Piper, faster-whisper, ElevenLabs, Deepgram) |

## Rules the code relies on

- **Nothing is confirmed unless it was committed.** The LLM acts only through
  `llm_agent/tools.py`. The routes act on what the tools *recorded*, not on
  the model's text. If the model claims a booking that no tool made, it gets
  one correction round before the caller hears anything.
- **Facts come from the business's settings.** Nothing about hours, prices or
  policies is hard-coded. The demo clinic's data is seed data in
  `call_logger.py`.
- **Tenant isolation.** Calls are routed by the dialed number. Every owner
  route is scoped to the signed-in user's business, and ids from other
  businesses return 404, exactly like missing ones.
- **Datetimes are UTC in the database.** Local time only exists at the edges
  (the profile's time zone). SQLite drops tzinfo, so always use `_utc()`
  and `_aware()`.
- **Transcripts are encrypted at rest** (`services/crypto.py`), and production
  refuses to start without `ENCRYPTION_KEY`.

## Tests

```bash
python -m pytest
```

Each test gets its own copy of a database built by the real migrations. The
LLM loop is exercised with a scripted fake client, so the suite needs no
network access and no API keys. To run the same suite on Postgres (CI does,
and it's the only place the double-booking race test runs):

```bash
TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/vaani_test python -m pytest
```

For judgement rather than plumbing, run the real-model caller evals with
`python -m evals.run`.
