# Deploying Vaani

The FastAPI app serves both the API and the website, so there is one thing to
deploy, not two.

| Route      | Serves                                        |
|------------|-----------------------------------------------|
| `/`        | `frontend/landing.html` — marketing + pricing  |
| `/app`     | `frontend/index.html` — interactive demo       |
| `/api/*`   | JSON API                                       |
| `/docs`    | OpenAPI docs                                   |
| `/health`  | Health check for load balancers                |

---

## 1. Run it locally

```bash
cd backend
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt   # Linux/macOS: .venv/bin/pip
python -m scripts.seed_plans                    # required, or pricing is empty
.venv/Scripts/python -m uvicorn main:app --port 8000
```

Open <http://localhost:8000>.

### Free speech engines (optional but recommended)

Without these, the browser's built-in speech is used — which works, but gives
**no lip-sync**, because there is no audio buffer to drive the avatar's mouth.

```bash
pip install piper-tts faster-whisper
python -m piper.download_voices en_GB-cori-high hi_IN-priyamvada-medium --download-dir models/piper
```

Any `.onnx` voice dropped in `backend/models/piper/` is picked up on restart.
Check what is live at `GET /api/speech/status`.

| | Engine | Licence | Cost | Notes |
|---|---|---|---|---|
| TTS | Piper | MIT | free, offline | ~6x realtime on CPU |
| STT | faster-whisper | MIT | free, offline | ~5x realtime, `base` model |
| TTS | ElevenLabs | paid | per character | set `ELEVENLABS_API_KEY` |
| STT | Deepgram | paid | per minute | set `DEEPGRAM_API_KEY` |

Selection is automatic (`TTS_PROVIDER=auto`): paid provider if a key is set,
else the free local engine, else browser speech. Override per client by
setting `TTS_PROVIDER` / `STT_PROVIDER` explicitly.

---

## 2. Configure for production

**Generate a real JWT secret.** The app *refuses to boot* with
`ENVIRONMENT=production` and the placeholder secret — every session token is
signed with it, so the default would let anyone mint an admin token.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Minimum production `.env`:

```ini
ENVIRONMENT=production
DEBUG=false
JWT_SECRET_KEY=<the value you just generated>
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/vaani
CORS_ORIGINS=https://yourdomain.com
OPENAI_API_KEY=<for real AI answers>
```

Startup fails loudly if `JWT_SECRET_KEY` is missing/short or `DEBUG` is on, and
warns if `CORS_ORIGINS` still contains localhost.

---

## 3. Deploy with Docker

```bash
export POSTGRES_PASSWORD=$(python -c "import secrets;print(secrets.token_urlsafe(24))")
export JWT_SECRET_KEY=$(python -c "import secrets;print(secrets.token_urlsafe(48))")
export CORS_ORIGINS=https://yourdomain.com
export WITH_SPEECH=1          # bake Piper + Whisper into the image (+~175MB)

docker compose up -d --build
docker compose exec api python -m scripts.seed_plans
```

Postgres and Redis bind to `127.0.0.1` only. The API listens on `:8000` —
put a TLS terminator in front of it (below).

**Model weights are gitignored and excluded from the build context.** Either
build with `WITH_SPEECH=1`, or mount voices into
`/app/backend/models/piper` via the `piper_models` volume.

---

## 4. Put TLS in front

The app sets HSTS automatically once it sees HTTPS. Terminate TLS at nginx,
Caddy, or your platform's load balancer.

```nginx
server {
    listen 443 ssl http2;
    server_name yourdomain.com;

    ssl_certificate     /etc/letsencrypt/live/yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/yourdomain.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # TTS synthesis on a long reply can take a few seconds.
        proxy_read_timeout 120s;
    }
}
```

The frontend calls the API same-origin, so no extra CORS setup is needed
behind a proxy. For a split deploy (static host + separate API), load the page
with `?api=https://api.yourdomain.com` once — it is remembered in
localStorage — and add that origin to `CORS_ORIGINS`.

---

## 5. Post-deploy checklist

```bash
curl https://yourdomain.com/health                 # {"status":"healthy"}
curl https://yourdomain.com/api/plans              # three plans, not []
curl https://yourdomain.com/api/speech/status      # which engines are live
```

- [ ] `JWT_SECRET_KEY` is unique to this environment and not in git
- [ ] `backend/.env` is not committed (it is gitignored — verify)
- [ ] Plans seeded, and prices match what you intend to charge
- [ ] Signup → trial → `/app` works end to end in a private window
- [ ] Postgres is backed up

### Things not wired yet

- **Stripe.** Subscriptions create a 14-day trial row in the database; no card
  is charged. `STRIPE_*` config exists but checkout is not implemented.
- **Twilio.** `/api/voice/incoming` is a stub — the web demo works, real
  inbound phone calls need the Twilio number and webhook configured.
- **Gujarati TTS.** Whisper transcribes it; Piper has no Gujarati voice, which
  is why the site marks it "coming soon". ElevenLabs/Azure cover it on a paid
  plan.
