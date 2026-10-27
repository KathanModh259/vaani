# Vaani for Alan

The hackathon asks for a tool *for people using Alan*. So the question is not
"does this have an integration" but "what can an Alan agent do now that it
could not do before".

Before Vaani, an Alan agent had no phone and no calendar. It could write the
clinic's booking system; it could not answer the clinic's phone, and it could
not move a day of appointments when the doctor called in sick.

Vaani gives an agent two things: **a telephone line** (Twilio, in Hindi,
Gujarati and English, with real speech in and out) and **a booking engine**
that refuses to double-book. Everything else in this repository exists to make
those two things trustworthy enough to leave running.

## Set it up in two commands

```bash
python mcp/vaani_mcp.py --check          # protocol + a real booking on a throwaway DB
python mcp/vaani_mcp.py --print          # the mcpServers snippet, with absolute paths
```

`--print` emits exactly what to paste into Alan's MCP server settings. For
Claude and Cursor there is a writer that merges into the existing file instead
of replacing it:

```bash
python mcp/vaani_mcp.py --install --client claude
python mcp/vaani_mcp.py --install --client cursor
```

`--check` never touches your configured database. It runs the real protocol,
lists the tools, and books an appointment into a temporary SQLite file, then
verifies the slot disappeared from availability. If that prints `READY`, the
server works.

## Three things an agent can do that a receptionist cannot

### 1. Empty a day

The dentist is out tomorrow. Every appointment has to move.

```
prompts/get  clear-the-day { from_date: "2026-10-27", to_date: "2026-10-28" }
```

The prompt makes the agent work one appointment at a time: read the day with
`vaani_daily_digest`, get ids with `vaani_upcoming_appointments`, pick a time
near the original hour with `vaani_check_availability`, then
`vaani_reschedule_appointment`. That last tool books the new slot *first* and
only then cancels the old one — so a failed move never leaves a patient with
no appointment. If the new slot is taken, the tool returns alternatives and
says plainly that the original is still in place.

That is the difference between a chat demo and an operations tool: the failure
mode is defined, and the agent is told what it is.

### 2. Work the callback queue

`vaani_daily_digest` returns `needs_attention`: every call where the caller
asked for a human, with the first thing they said. An Alan agent can turn that
into tasks, text the callers back, or rebook them — without anyone listening
to recordings.

### 3. Stand up a business front desk

Point `VAANI_BUSINESS_ID` at another tenant and the same server answers for a
different clinic, salon or law firm. One MCP server, many businesses, all
row-scoped by `business_id`.

## The safety model, because agents act without supervision

| Risk | What stops it |
|---|---|
| Confirming a booking that never happened | The confirmation text is built from the committed row. The phone path additionally catches a model that claims success before any tool ran |
| Double booking two callers at once | `book()` takes a row lock on the business, then re-checks overlap under it. The loser gets alternatives |
| A reschedule that loses the appointment | New slot is booked and conflict-checked first; the old row is cancelled only after the new one commits |
| Inventing a price or policy | Prices, hours, services and FAQs come from the business's own profile, never from the model |
| An agent cancelling the whole calendar | Cancel-by-phone is explicit in the tool description and cancels one id unless you pass only a phone. There is no bulk-delete tool |
| A demo that lies about itself | `--check` books on a throwaway database and asserts the slot is gone before printing `READY` |

## What is deliberately not in the MCP surface

- No raw SQL, no schema access, no "delete everything".
- No telephony control (dialling, hangup). Calls are initiated by Twilio, and
  the MCP surface is about what the call produced. An outbound-call tool is the
  obvious next step once a number and public URL exist.
- No billing writes.

## Limits worth stating out loud

- A real phone number needs Twilio credentials and a public HTTPS URL. The web
  demo and the MCP server work without either.
- Gujarati speech output: Gujarati text and STT work; TTS does not,
  because Piper has no Gujarati voice.
- The MCP server is stdio, one process per client. For a team, point several
  clients at the same database — writes stay safe because the locking is in
  the database, not in the server process.

## Why this belongs in the hackathon

The submission rules are: open source, open models, works with Alan, demoable.
Vaani is MIT, defaults to open weights (Qwen on OpenRouter) and labels OpenAI
as closed rather than hiding it, and exposes itself to Alan as an MCP server
with tools *and* prompts — the part most MCP demos skip, and the part that
decides whether an agent uses the tool correctly under pressure.