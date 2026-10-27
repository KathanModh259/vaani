# Deploying Vaani

One application. The FastAPI process serves the API, the marketing site, the
demo and the dashboard, so there is one thing to deploy, not four.

| Route | Serves |
|---|---|
| `/` | `frontend/landing.html` — marketing + pricing |
| `/app` | `frontend/index.html` — interactive voice demo |
| `/dashboard` | `frontend/dashboard.html` — owner's calls, calendar, settings |
| `/api/*` | JSON API |
| `/health` | liveness, for a load balancer |
| `/health/ready` | readiness: process **and** database |
| `/docs` | OpenAPI docs (development only unless `DOCS_ENABLED=true`) |

Three ways to run it, in order of least effort:

1. **Render** — connect the repo, it reads `render.yaml`. One click.
2. **Docker Compose on a VPS** — one command, Caddy gets the TLS certificate.
3. **Locally** — for development and for recording the demo.

The app **refuses to start** in `ENVIRONMENT=production` with a placeholder JWT
secret, a missing encryption key, `DEBUG=true`, or SQLite as the database.
That is deliberate: each of those is a silent security or data-loss bug
otherwise, and a refused boot is much easier to notice than a leak.

---

## 1. Run it locally

```bash
cd backend
python -m venv .venv
# requirements-dev.txt includes requirements.txt plus pytest, httpx and ruff.
.venv/Scripts/pip install -r requirements-dev.txt  # Linux/macOS: .venv/bin/pip
.venv/Scripts/python -m uvicorn app:app --port 8000
```

Open <http://localhost:8000>. No API key is needed: the deterministic booking
flow books real appointments and answers FAQs from the business profile.
`python -m scripts.seed_plans` seeds the pricing table (the app also does it at
startup when the table is empty).

The schema migrates itself on startup (`AUTO_MIGRATE=true`). A database created
by an older build with `create_all()` is detected, stamped as the baseline and
upgraded in place.

Tests: `.venv/Scripts/python -m pytest`. They run the real migrations against
throwaway SQLite files and never read keys from your `.env`.

**Prove the whole thing works** — no server needed:

```bash
.venv/Scripts/python -m scripts.demo_run        # 30 assertions, exit 0 = pass
```

### Free speech engines (optional, recommended)

Without these the browser's built-in speech is used: it works, but gives no
lip-sync because there is no audio buffer to drive the avatar's mouth.

```bash
pip install piper-tts faster-whisper
python -m piper.download_voices en_GB-cori-high hi_IN-priyamvada-medium --download-dir models/piper
```

Any `.onnx` voice in `backend/models/piper/` is picked up on restart. Check
`GET /api/speech/status`.

| | Engine | Licence | Cost | Notes |
|---|---|---|---|---|
| TTS | Piper | MIT | free, offline | ~6x realtime on CPU |
| STT | faster-whisper | MIT | free, offline | ~5x realtime, `base` model |
| TTS | Sarvam / ElevenLabs | paid | per character | Hindi and Gujarati voices |
| STT | Deepgram | paid | per minute | |

Selection is automatic (`TTS_PROVIDER=auto`): a paid provider if its key is
set, else the free local engine, else browser speech.

---

## 2. Deploy on Render (one click)

`render.yaml` is a complete blueprint: a web service, a managed Postgres, and
generated secrets.

1. **Render → New → Blueprint**, pick this repository.
2. Set `OPENROUTER_API_KEY` when prompted (a free OpenRouter key works; append
   `:free` to the model name for the free tier). Everything else is generated.
3. Deploy. The first build takes a few minutes because the image compiles its
   dependencies.

Why Postgres and not SQLite: a container filesystem is discarded on the next
deploy, which would erase every booking. The blueprint creates a real database,
and `scripts/config.py` rewrites Render's `postgres://` URL to
`postgresql+asyncpg://` so the dashboard value can be used unchanged.

**Verify the deploy from outside:**

```bash
cd backend && python -m scripts.preflight https://your-service.onrender.com
```

`preflight` checks TLS and the certificate, the proxy's security headers,
health and readiness, which model answers and whether its weights are open,
whether Twilio is configured, that all three pages load, and that a booking
actually commits (it books a slot ~45 days out, then proves the row persisted
by watching the same slot get refused). `--no-book` skips the booking,
`--require-phone` makes an unconfigured Twilio line a failure instead of a
warning.

### Other platforms

Anything that runs a container works: Fly, Railway, Cloud Run, ECS, Kubernetes.

- Listen on `$PORT` — the image does, and the HEALTHCHECK follows it.
- Set the same environment variables as `deploy/compose.env.example`.
- Use managed Postgres; set `AUTO_MIGRATE=false` when running more than one
  replica and run `python -m db.migrate` once per release instead.
- Terminate TLS at the platform's proxy and set `PUBLIC_BASE_URL` to the real
  https origin — Twilio signs the exact URL it calls.

---

## 3. Deploy with Docker Compose on a VPS

This is the full production stack: API, Postgres, Redis, Caddy for TLS, and a
nightly `pg_dump`.

```bash
cp deploy/compose.env.example .env
cd backend && .venv/Scripts/python -m scripts.init_env --write && cd ..

# edit .env: PUBLIC_BASE_URL, DOMAIN, CORS_ORIGINS (your real domain)
docker compose up -d --build
```

`init_env` generates `JWT_SECRET_KEY`, `ENCRYPTION_KEY` and
`POSTGRES_PASSWORD`, writes them to both `.env` (compose interpolation) and
`backend/.env` (the app), and keeps the two in sync — compose's `environment:`
block wins over `env_file`, so a mismatch there would silently deploy different
secrets than the app file says. It never replaces an existing encryption key or
database password, because replacing either destroys data.

Verify:

```bash
cd backend && .venv/Scripts/python -m scripts.preflight https://vaani.example.com
docker compose ps          # api, db, redis healthy; caddy up; backup up
docker compose logs -f api
```

What the stack guarantees:

- **Only Caddy is public.** `api`, `db` and `redis` are either unpublished or
  bound to `127.0.0.1`. The database is never reachable from the internet, and
  `X-Forwarded-For` cannot be forged from outside.
- **TLS is automatic.** Caddy obtains and renews the Let's Encrypt certificate
  from `DOMAIN` alone; the certificate state lives in a volume so restarts do
  not re-request it. `DOMAIN=:80` serves plain HTTP, which is how the stack is
  tested locally.
- **Startup is gated.** Caddy starts only once the API reports healthy, and the
  API only once Postgres and Redis are healthy.
- **Shutdown drains.** `stop_grace_period: 30s` lets an in-flight call finish
  and its transcript commit before the container dies.
- **Logs rotate.** `json-file` with `max-size: 10m`, `max-file: 3` on every
  service, so a chatty month does not fill the disk.
- **Backups run nightly** into `./backups`, keeping `BACKUP_KEEP_DAYS` (14).

**Put `./backups` somewhere else.** A backup on the same disk as the database
protects against a bad migration, not against losing the machine. Copy it to
object storage in another region.

### Restore

```bash
docker compose exec -T db pg_restore -U vaani -d vaani --clean < backups/vaani-<stamp>.dump
```

`ENCRYPTION_KEY` must come from your own backup, never from the same copy as
the database dump: with both, the encryption protects nothing.

---

## 4. Configuration

Minimum for production (the compose file sets the first five itself):

```ini
ENVIRONMENT=production
DEBUG=false
JWT_SECRET_KEY=<48 random chars>      # signs sessions; rotating logs everyone out
ENCRYPTION_KEY=<32 random chars>      # encrypts transcripts; losing it loses them
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/vaani
PUBLIC_BASE_URL=https://vaani.example.com
CORS_ORIGINS=https://vaani.example.com
OPENROUTER_API_KEY=<or GROQ_API_KEY / TOGETHER_API_KEY / LLM_BASE_URL>
TWILIO_ACCOUNT_SID=<...>              # only if you want a real phone line
TWILIO_AUTH_TOKEN=<...>
SMTP_HOST=<...>                       # plus SMTP_USERNAME / SMTP_PASSWORD / SMTP_FROM
```

Everything else is documented in `backend/.env.example` and
`deploy/compose.env.example`.

Startup **fails** on: a placeholder or short `JWT_SECRET_KEY`, a missing
`ENCRYPTION_KEY`, `DEBUG=true`, or SQLite with `ENVIRONMENT=production`.
Startup **warns** on: no model provider (the deterministic flow then answers,
in English only), a provider whose weights are not open, a missing
`TWILIO_AUTH_TOKEN` (every voice webhook is rejected), or a missing
`PUBLIC_BASE_URL` (signature checks fail behind a proxy).

**Migrations.** One replica: leave `AUTO_MIGRATE=true`. Several replicas: set
it to `false` and run `python -m db.migrate` once per deploy, so replicas do not
race each other.

---

## 5. Post-deploy checklist

```bash
cd backend
python -m scripts.preflight https://vaani.example.com     # 17 checks
python -m scripts.synthetic_check https://vaani.example.com   # holds a real conversation
```

- [ ] `preflight` passes; it prints the reference of the test appointment it made
- [ ] `/api/llm/status` shows the provider you expect and `open_weights: true`
- [ ] `JWT_SECRET_KEY` and `ENCRYPTION_KEY` are unique to this environment
- [ ] `ENCRYPTION_KEY` is backed up somewhere other than the database backup
- [ ] `.env` and `backend/.env` are not committed (`git status` — they are ignored)
- [ ] Plans seeded, prices match what you intend to charge (`/api/plans`)
- [ ] Sign up, open settings, then `/app` signed in: answers use *your* hours and prices
- [ ] `docker compose exec -T db psql -U vaani -d vaani -c 'select count(*) from appointments;'`
- [ ] Restore rehearsal done once, from a real dump, into a scratch database
- [ ] Twilio webhook pointed at `https://your.domain/api/voice/incoming`
- [ ] `python -m scripts.demo_reset --yes` before recording, so the first booking on camera is the first booking

---
## 6. Onboarding a client

1. **They sign up** at `/` and confirm their email (the link sets up booking
   and callback alerts). In **Receptionist settings** they can click **Read
   my website** to draft hours, services, prices and FAQs from their own
   site, review the draft, and apply it. The setup checklist shows what's
   still missing. Signed in, they can test the receptionist at `/app`, which
   uses their settings rather than the demo clinic's.
2. **Give them a number.** Either way works:
   - *Self-serve* (`SELF_SERVE_NUMBERS=true`): they search for and pick a
     number in the dashboard. Webhooks are configured automatically. Buying
     requires a verified email and stays within their plan's number limit.
     India needs `TWILIO_BUNDLE_SID` and `TWILIO_ADDRESS_SID` from the Twilio
     console. **Leave this off until billing exists**, because each number
     bills your Twilio account monthly.
   - *By hand*: buy the number in Twilio, then run:
     ```bash
     docker compose exec api python -m scripts.assign_number owner@clinic.in +917900000001
     ```
     Then point the number's webhooks (Twilio console → Phone Numbers):
     *A call comes in* → `POST {PUBLIC_BASE_URL}/api/voice/incoming`,
     *Call status changes* → `POST {PUBLIC_BASE_URL}/api/voice/status-callback`.
3. **Keep their existing number (optional).** The dashboard shows carrier
   call-forwarding codes that send their current line to Vaani when they're
   busy or don't answer. Their customers never learn a new number.
4. **Test call.** In the dashboard, **Call me now to test** rings the owner
   from their Vaani number. They should hear their greeting, then "I'm Vaani,
   an AI assistant, and this call is transcribed." The call then appears on
   the dashboard with its transcript.

---

## 7. Messaging: confirmations and reminders

Each business chooses SMS or WhatsApp for booking confirmations and
day-before reminders. Reminders go out 20–26 hours before the appointment,
at most once each, even with several replicas running.

- **SMS (India): DLT.** Carriers deliver commercial SMS only from a
  DLT-registered sender with pre-approved templates. Register
  `BOOKING_SMS_TEMPLATE` and `REMINDER_SMS_TEMPLATE` from
  `backend/services/notify.py` word for word on a DLT portal (Jio, Airtel or
  Vi). Then create a Twilio Messaging Service with that sender and set
  `TWILIO_MESSAGING_SERVICE_SID`.
- **WhatsApp.** Get a WhatsApp sender approved in Twilio, and create two
  templates in Content Builder. The confirmation template's variables are
  `{{1}}` business, `{{2}}` service, `{{3}}` time, `{{4}}` reference. The
  reminder template uses the same first three, with `{{4}}` as the business
  phone number. Set `TWILIO_WHATSAPP_FROM` and both `WHATSAPP_*_CONTENT_SID`
  values. If WhatsApp isn't configured, messages go by SMS instead.

A failed message never affects the booking.

## 8. Faster calls: ConversationRelay

`VOICE_MODE=relay` switches phone calls from the `<Gather>`/`<Say>` loop to
Twilio ConversationRelay over a WebSocket at `wss://{host}/api/voice/relay`.
Speech recognition and speech synthesis stream, callers can interrupt, and a
slow turn plays "One moment." instead of silence. Memory, tools, alerts and
transcripts are identical in both modes. Your TLS proxy must pass WebSocket
upgrades. The Caddy service in `docker-compose.yml` already does; for nginx,
add this to the `location` block:

```nginx
proxy_http_version 1.1;
proxy_set_header Upgrade $http_upgrade;
proxy_set_header Connection "upgrade";
```

If the socket drops mid-call, the caller is carried on in `<Gather>` mode
rather than being disconnected. Test on a real call before switching every
client over.

## 9. Integrations

- **Google Calendar.** Create an OAuth client of type "Web application" in
  Google Cloud, add the redirect URI
  `{PUBLIC_BASE_URL}/api/integrations/google/callback`, and set
  `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`. Owners connect from the
  dashboard. Their busy times then block slots, and bookings appear in their
  calendar. A Google outage never blocks a booking.
- **Sarvam AI** (`SARVAM_API_KEY`). Native Hindi and Gujarati voices for the
  web demo. When the key is set, it's preferred over ElevenLabs and Piper.
- **Backup LLM** (`LLM_FALLBACK_*`). Any OpenAI-compatible endpoint (Azure
  OpenAI, Groq, a self-hosted vLLM). It's used automatically when the primary
  model errors or times out.

## 10. Operating it

- **Monitoring.** `python -m scripts.preflight https://your.domain` checks the
  deployment from outside (TLS, headers, health, model, pages, a real booking).
  `python -m scripts.synthetic_check https://your.domain` holds a real
  conversation. It fails if anything errors, if a reply takes over 6 seconds,
  or if replies come from the deterministic path while `/api/llm/status` says a
  model is configured (meaning the provider is down or out of credit). `.github/workflows/monitor.yml`
  runs it hourly once you set the repository variable `PROD_URL`, and runs
  the real-model caller evals weekly once you set the `OPENROUTER_API_KEY`
  secret.
- **Caller evals.** Run `python -m evals.run` before changing the prompt,
  the model or the tools. It holds scripted English and Hindi calls with the
  real model, then checks what actually reached the database.
- **Errors.** Set `SENTRY_DSN` for error tracking. Personal data isn't sent:
  bodies are stripped and `send_default_pii` is off. Set `LOG_FORMAT=json`
  for log platforms. Every response carries an `X-Request-ID` that also
  appears in the logs.
- **Backups.** The compose `backup` service writes a daily `pg_dump` to
  `./backups` and keeps 14 days. Copy that folder off the machine. Restore
  with
  `docker compose exec -T db pg_restore -U vaani -d vaani --clean < backups/<file>.dump`.
  Rehearse a restore before you need one. On Render, use the database's own
  backups instead; the compose service does not run there.
- **Usage limits.** Owners are emailed at 80% and 100% of their monthly
  minutes. With `HARD_MINUTE_CAP=true`, calls over the allowance get a short
  message and a logged callback instead of the AI. That's your cost backstop
  until billing charges for overage.
- **Retention.** Transcripts older than each business's setting (or
  `DEFAULT_RETENTION_DAYS`) are scrubbed every six hours. Outcomes and
  durations are kept.

## 11. Hosting and compliance (India)

- **Region.** Host the app and database in an Indian region (AWS
  `ap-south-1` Mumbai, GCP `asia-south1`, Azure Central India). Clinic and
  customer data is sensitive under the DPDP Act, and it also keeps the calls
  fast.
- **Disclosure.** Every call opens with the AI and transcription disclosure.
  Don't remove it.
- **Consent and data rights.** Owners can download everything (**Download
  all my data**) or delete their account from the dashboard, which covers the
  access, portability and erasure requests your privacy policy promises.
- **Legal page.** Set the `LEGAL_*` variables to fill `/legal`, then have a
  lawyer review it. The contract terms still marked in brackets (billing
  period, overage rate) are decisions only you can make.
- **TRAI.** Inbound AI answering is ordinary call handling. If you ever add
  outbound or promotional calling, TRAI's telemarketing and DND rules apply.

### Still to do

- **Billing** (deferred on purpose). Trials are created and minute
  allowances are tracked and alerted on, but no payment is taken.
- **On a real phone call:** ConversationRelay, Gujarati speech, and WhatsApp
  templates are built and tested in code, but need one real-world test each
  before you rely on them. Webhook signature verification is covered by tests
  with signed fixtures; a live carrier call is the missing evidence.
