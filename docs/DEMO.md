# Demo script

A recorded take already exists at [`demo.mp4`](demo.mp4): 42 seconds, title
card, the live booking flow, outro card. It was captured from the running app
with CDP screencast, so the timings are the app's own. Re-record with
`record_demo.py` (kept outside the repo) or follow the shot list below.

Ninety seconds, recorded once, no cuts. The point is not that it talks — it is
that the booking is real. Show the database twice: before and after.

## Before you record

```bash
cd backend
.venv/Scripts/python -m uvicorn app:app --port 8000      # Windows
# or: .venv/bin/python -m uvicorn app:app --port 8000    # macOS/Linux
```

- Open `http://localhost:8000/app` in a second window.
- Open a third window on the database:

  ```bash
  python -c "import sqlite3;[print(r) for r in sqlite3.connect('phone_receptionist.db').execute('select customer_name,start_time,status from appointments')]"
  ```

- Keep the transcript pane visible next to the avatar. Judges should see the
  words, the tool result and the row, not just a voice.
- Turn on a model key for the multilingual take. With no key, the demo still
  books — the flow is deterministic — but replies stay English.

## The 90 seconds

| Time | On screen | Say |
|---|---|---|
| 0:00 | Landing page | "This is a clinic in Ahmedabad. Calls after 6pm go to voicemail." |
| 0:10 | `/app`, microphone | "Vaani answers instead. Watch — I'll speak like a real patient." |
| 0:20 | "I want to book an appointment" | "It asks for the one thing it needs. No menu, no hold music." |
| 0:30 | "Teeth cleaning tomorrow at 11" | "It checks the real calendar, offers only free slots." |
| 0:45 | "Name Meera Patel, 9988776655" | "Booked. Reference, price, day, time — all from the tool result." |
| 1:00 | Database window | "There is the row. It was not read from a script." |
| 1:10 | Ask the same slot again on a phone | "Second caller for the same slot gets the free alternatives, not a double booking." |
| 1:20 | `GET /api/llm/status` | "Open weights: Qwen on OpenRouter. No closed model in the loop." |
| 1:30 | Alan MCP: `clear-the-day` | "Now the agent's job: the dentist is out tomorrow, so it moves the whole day — one at a time, and it refuses rather than double-booking." |
| 1:50 | `python mcp/vaani_mcp.py --check` | "READY means it booked on a throwaway database and the slot really disappeared." |

## The two proofs that matter

1. **The row exists.** `select * from appointments;` — name, phone, start time,
   status `scheduled`, source `web` or `phone`.
2. **The refusal is real.** Book the same slot twice. The second attempt gets
   `taken` and a list of free times, because `book()` re-checks under a row
   lock rather than trusting the earlier availability read.

## The optional agent beat

The `clear-the-day` moment is the one a judge remembers, because it is
something a receptionist *cannot* do:

```
prompts/get clear-the-day { from_date: "2026-10-27", to_date: "2026-10-28" }
```

Book three appointments on the first date beforehand, run it, and show the
digest before and after. If a new slot is taken, the tool returns
alternatives and leaves the original appointment in place — that refusal is
worth showing, not hiding.

## Posting to X

- 1–3 minutes, no cuts, audio actually on.
- Pin a reply with the repo link and the deployed URL.
- Mention the open model by name and the licence (MIT).
- Do not claim a phone number you have not provisioned. "Twilio webhook
  implemented; add a number and a public URL" is the honest line, and it is
  still impressive.

## If something breaks on camera

- Microphone permission denied: type in the chat box. The route is identical.
- Model rate-limited: the app falls back to the deterministic flow and keeps
  booking; `/api/llm/status` will show why.
- Slot taken by an earlier take: choose another time; the refusal is the
  feature, and showing it is better than hiding it.