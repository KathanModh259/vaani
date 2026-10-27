# OpenFire submission

Three things are owed: a public repo, a working link, and a 1–3 minute X post
with a demo.

## 1. The repo

```bash
git add -A
git commit -m "Real bookings, open-weight models, MCP server for Alan"
git push origin master
```

Before pushing, confirm nothing private is in the diff:

```bash
git status --short
git diff --cached --stat
```

`.env`, `*.db`, `.venv/` and speech model weights are already ignored. The
tracked tree contains no key — `backend/.env.example` documents every
variable with empty values.

## 2. The link

Render reads `render.yaml` and creates the service plus a managed Postgres:

1. Render → New → Blueprint → pick this repo.
2. Set `OPENROUTER_API_KEY` (a free OpenRouter key works).
3. Deploy, then verify: `cd backend && python -m scripts.preflight https://<host>`.

On a VPS instead:

```bash
cp deploy/compose.env.example .env
cd backend && python -m scripts.init_env --write && cd ..
# edit .env: PUBLIC_BASE_URL and DOMAIN
docker compose up -d --build
```

The demo path is `https://<your-host>/app`. The landing page is `/`, the
dashboard is `/dashboard`, and `GET /api/llm/status` proves which model is
answering.

Add a number only if you actually provisioned one. "Twilio webhook
implemented; add a number and a public URL" is honest and still impressive.

## 3. The post

> I built Vaani for @tryalan — an open-source phone receptionist for Indian
> small businesses.
>
> It answers in Hindi, Gujarati and English, checks a real calendar, and books
> the appointment. Not a demo script: the confirmation you hear is a row in
> Postgres.
>
> Two things I'm proud of:
>
> • It books with **zero API keys**. The deterministic flow asks for service,
>   day, time, name and number, then calls the same tools the model calls.
> • When a model answers, it runs on **open weights** (Qwen on OpenRouter).
>   OpenAI is supported and labelled closed — it is not the default story.
>
> Same calendar exposed as an MCP server, so an Alan agent can book without a
> phone call at all.
>
> MIT. 232 tests. Repo + live demo below.

Then attach the video, the repo link and the demo link. Mention the model by
name.

## What to show on camera

Follow [DEMO.md](DEMO.md). The two beats that matter:

1. A booking completes and the row is visible in the database.
2. A second caller asks for the same slot and is refused with alternatives.

## Checklist

- [ ] `cd backend && python -m pytest -q` — 232 passed, 1 skipped
- [ ] `cd backend && ruff check .` — clean
- [ ] `GET /health/ready` returns `ready`
- [ ] `GET /api/llm/status` shows a provider with `open_weights: true`
- [ ] `docker build` then `docker run` with `ENVIRONMENT=production` serves `/health/ready`
- [ ] `docker compose config` resolves, and the stack comes up behind Caddy
- [ ] `python -m scripts.preflight https://your.domain` passes (17 checks)
- [ ] One booking made through `/api/chat`, row checked in the database
- [ ] The same slot refused on the second attempt
- [ ] `python -m scripts.demo_run` prints `ALL CHECKS PASSED`
- [ ] `python mcp/vaani_mcp.py --check` prints `READY`
- [ ] `python mcp/vaani_mcp.py --print` emits a snippet that actually launches the server
- [ ] README quickstart works from a clean clone
- [ ] Video under 3 minutes, audio audible, no cuts
- [ ] Repo public, licence visible, no secrets in history

## Honest limits to state, not hide

- Gujarati text works; Gujarati speech output does not (Piper has no voice).
- Billing records plans, does not charge cards.
- Self-serve number purchase is off by default because it spends real money.
- The `<Gather>` voice path is turn-based; ConversationRelay streams.

Judges reward the boundary more than the claim. Every line above is
reproducible from the code.