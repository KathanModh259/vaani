# Security

Vaani handles phone numbers, names, and transcripts that can contain medical or
financial details. Treat a deployment as production data handling from the
first real call.

## Reporting a vulnerability

Open a private security advisory on the repository (Security → Report a
vulnerability). Please do not open a public issue for anything that could be
exploited. Include the route or file, what an attacker can do, and the smallest
reproduction you have. This is a solo-maintained project, so allow a few days
before any disclosure.

## What is already in place

- Transcripts are encrypted at rest with `ENCRYPTION_KEY` (Fernet), and the app
  refuses to start in production without a usable cipher.
- Session tokens are signed with `JWT_SECRET_KEY`; the app refuses to start in
  production with the placeholder value or a short key.
- Twilio webhooks are signature-verified when `TWILIO_AUTH_TOKEN` is set, and
  rejected otherwise rather than trusted.
- Passwords are hashed with bcrypt; API keys are stored hashed.
- Booking writes serialise on a row lock and re-check conflicts, so two callers
  cannot double-book.
- The model cannot invent availability, prices or policies: tools and the
  business profile are the only sources.
- Rate limits are per-IP, shared through Redis when `REDIS_URL` is set.
- Only Caddy is published to the internet in the compose stack; the API,
  Postgres and Redis are not reachable from outside.

## What you must do as an operator

- Back up `ENCRYPTION_KEY` separately from the database. With both, the
  encryption protects nothing; with neither, every transcript is lost.
- Serve over HTTPS. `PUBLIC_BASE_URL` must be the exact public origin.
- Set `TWILIO_AUTH_TOKEN` before pointing a real number at the app.
- Review `/legal` with a lawyer and set the `LEGAL_*` variables.
- Rotate secrets if a `.env`, a backup, or a machine is lost.

## Out of scope

- Denial of service, or anything requiring a compromised host.
- Social engineering of a business's staff.
- Issues in third-party providers (Twilio, OpenRouter, hosting).
- Reports that only show default development settings (the app refuses those
  in production by design).
